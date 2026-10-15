"""
Synthetic football match data generator.

Generates ONE underlying "ground truth" match (same events, same players,
same timeline) and then exports it into 3 vendor-shaped formats: Opta-style
XML-ish JSON, StatsBomb-style nested JSON, and Wyscout-style proprietary JSON.

Because all 3 outputs come from the same ground truth, we know the "correct"
normalized answer — which is what lets us test the agent pipeline's output
for correctness, not just "did it run."

The event timeline is a simple possession simulation, not independent
random events: passes start where the previous action ended, the ball
changes hands through tackles, interceptions and clearances, shots end up
saved, blocked, off target or on the post (with the goalkeeper's save,
the corner or the goal kick that follows), fouls can draw yellow, second
yellow or red cards (a sent-off player takes no further part), fouls in
the box give penalties, and substitutes come on at the minutes on the team
sheet. Home attacks toward x=100 in the first half and toward x=0 in the
second. Every player in the events is from that team's own squad on the
match sheet; all names are fictional.

Each vendor export encodes card colour and shot result the way that vendor
does: Opta card qualifiers (31/32/33) and shot event types (MISS / POST /
ATTEMPT_SAVED / GOAL), StatsBomb shot.outcome and bad_behaviour.card,
Wyscout tags (101 goal, 2101 blocked, 1801/1802 accurate, 1701-1703 cards).

Supports 3 quality tiers (per PRD Section 14 locked decisions):
  - clean:   no missing fields, no corruption
  - partial: some missing player IDs / coordinates, still parseable
  - messy:   malformed structure, unknown fields, out-of-range values,
             the kind of data that should trigger agent retries/fallbacks
"""

from __future__ import annotations
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Quality = Literal["clean", "partial", "messy"]

# Positions -> broad role, used to pick who is on the ball in each zone.
POSITION_ROLE = {
    "GK": "GK", "RB": "DEF", "LB": "DEF", "CB": "DEF",
    "CDM": "MID", "CM": "MID", "CAM": "MID", "RM": "MID", "LM": "MID",
    "RW": "FWD", "LW": "FWD", "ST": "FWD",
}

# ---------------------------------------------------------------------------
# Match-sheet data: lineups, formations, subs, officials, venue. A full
# matchday needs 2 x 18-man squads (11 starters + 7 bench), all with
# fictional names built from these pools (synthetic events are never
# attributed to real players). This is
# a separate, discrete pillar from event data -- real vendors (Opta/
# StatsBomb/Wyscout) ship it as its own feed, not bundled into the event
# stream -- unlike tracking/video/physical data, which come from entirely
# different vendor systems and are intentionally out of scope here.
_FILLER_FIRST_NAMES = [
    "Marcus", "Diego", "Kwame", "Luca", "Noah", "Theo", "Yusuf", "Mateo",
    "Felix", "Omar", "Jamal", "Rafael", "Elias", "Hugo", "Bruno", "Axel",
    "Tariq", "Iker", "Sven", "Nico", "Dario", "Amadou", "Enzo", "Kasper",
]
_FILLER_LAST_NAMES = [
    "Owusu", "Fernandez", "Larsson", "Okafor", "Bianchi", "Kowalski",
    "Dubois", "Haddad", "Nakamura", "Silva", "Mensah", "Petrov", "Costa",
    "Berg", "Diallo", "Hassan", "Novak", "Rossi", "Ivanov", "Lindqvist",
]

FORMATIONS = {
    "4-3-3": ["GK", "RB", "CB", "CB", "LB", "CDM", "CM", "CM", "RW", "ST", "LW"],
    "4-4-2": ["GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "ST", "ST"],
    "3-5-2": ["GK", "CB", "CB", "CB", "RM", "CM", "CDM", "CM", "LM", "ST", "ST"],
    "4-2-3-1": ["GK", "RB", "CB", "CB", "LB", "CDM", "CDM", "CAM", "RW", "LW", "ST"],
}
BENCH_POSITIONS = ["GK", "CB", "LB", "CM", "CDM", "ST", "RW"]

REFEREE_FIRST_NAMES = ["Michael", "Anthony", "Paul", "Craig", "Simon", "Andre", "David"]
REFEREE_LAST_NAMES = ["Oliver", "Taylor", "Tierney", "Pawson", "Hooper", "Marriner", "Attwell"]

TEAM_VENUES = {
    "Liverpool": "Anfield",
    "Manchester City": "Etihad Stadium",
    "Arsenal": "Emirates Stadium",
    "Chelsea": "Stamford Bridge",
    "Manchester United": "Old Trafford",
    "Tottenham": "Tottenham Hotspur Stadium",
}
WEATHER_OPTIONS = ["Clear", "Overcast", "Light rain", "Heavy rain", "Windy"]


@dataclass
class GroundTruthLineupPlayer:
    name: str
    jersey_number: int
    position: str
    is_starter: bool
    is_captain: bool = False


@dataclass
class GroundTruthSubstitution:
    team: str
    player_out: str
    player_in: str
    minute: int
    period: int = 2
    reason: str | None = None


@dataclass
class GroundTruthMatchSheet:
    home_formation: str
    away_formation: str
    home_lineup: list[GroundTruthLineupPlayer]
    away_lineup: list[GroundTruthLineupPlayer]
    substitutions: list[GroundTruthSubstitution]
    referee: str
    assistant_referees: list[str]
    fourth_official: str
    var_official: str
    venue: str
    attendance: int
    weather: str
    kickoff_time: str  # "HH:MM", local kickoff time -- date comes from MatchInfo


def _unique_name(rng: random.Random, used: set[str]) -> str:
    """Builds a synthetic (non-real) player name not already used in this
    match, so no two players on the pitch collide."""
    for _ in range(200):
        name = f"{rng.choice(_FILLER_FIRST_NAMES)} {rng.choice(_FILLER_LAST_NAMES)}"
        if name not in used:
            used.add(name)
            return name
    # Pool exhausted (shouldn't happen at 24x20 combinations for 36 players) --
    # fall back to a numbered tag rather than looping forever.
    name = f"Player {len(used) + 1}"
    used.add(name)
    return name


def _generate_squad(team: str, rng: random.Random, used_names: set[str]) -> tuple[str, list[GroundTruthLineupPlayer]]:
    """One team's 18-man squad: 11 starters in a formation + 7 substitutes,
    all with fictional names and unique shirt numbers."""
    formation = rng.choice(list(FORMATIONS.keys()))
    slots = FORMATIONS[formation]

    squad: list[GroundTruthLineupPlayer] = []
    used_jerseys: set[int] = set()

    def next_jersey() -> int:
        for _ in range(200):
            n = rng.randint(1, 99)
            if n not in used_jerseys:
                used_jerseys.add(n)
                return n
        n = max(used_jerseys, default=0) + 1
        used_jerseys.add(n)
        return n

    for i, position in enumerate(slots):
        name, jersey = _unique_name(rng, used_names), next_jersey()
        squad.append(GroundTruthLineupPlayer(
            name=name, jersey_number=jersey, position=position,
            is_starter=True, is_captain=(i == 0),
        ))

    for position in rng.sample(BENCH_POSITIONS, k=len(BENCH_POSITIONS)):
        name, jersey = _unique_name(rng, used_names), next_jersey()
        squad.append(GroundTruthLineupPlayer(
            name=name, jersey_number=jersey, position=position, is_starter=False,
        ))

    return formation, squad


def generate_match_sheet(home_team: str, away_team: str, seed: int | None = None) -> GroundTruthMatchSheet:
    """Generates the administrative record for a match: lineups, formations,
    substitutions, officials, venue, attendance, weather, kickoff time --
    independent of (and not cross-referenced against) the event timeline,
    since real vendors ship these as separate feeds too."""
    rng = random.Random(seed)
    used_names: set[str] = set()

    home_formation, home_squad = _generate_squad(home_team, rng, used_names)
    away_formation, away_squad = _generate_squad(away_team, rng, used_names)

    substitutions = []
    for team, squad in ((home_team, home_squad), (away_team, away_squad)):
        starters = [p for p in squad if p.is_starter]
        bench = [p for p in squad if not p.is_starter]
        n_subs = rng.randint(2, 4)
        out_players = rng.sample(starters, k=min(n_subs, len(starters)))
        in_players = rng.sample(bench, k=min(n_subs, len(bench)))
        for out_p, in_p in zip(out_players, in_players):
            minute = rng.randint(46, 90)
            substitutions.append(GroundTruthSubstitution(
                team=team, player_out=out_p.name, player_in=in_p.name,
                minute=minute, period=2,
                reason=rng.choice(["tactical", "injury", "tactical", "fatigue"]),
            ))
    substitutions.sort(key=lambda s: s.minute)

    referee = f"{rng.choice(REFEREE_FIRST_NAMES)} {rng.choice(REFEREE_LAST_NAMES)}"
    assistants = [f"{rng.choice(REFEREE_FIRST_NAMES)} {rng.choice(REFEREE_LAST_NAMES)}" for _ in range(2)]
    fourth = f"{rng.choice(REFEREE_FIRST_NAMES)} {rng.choice(REFEREE_LAST_NAMES)}"
    var = f"{rng.choice(REFEREE_FIRST_NAMES)} {rng.choice(REFEREE_LAST_NAMES)}"

    venue = TEAM_VENUES.get(home_team, f"{home_team} Stadium")
    attendance = rng.randint(40_000, 75_000)
    weather = rng.choice(WEATHER_OPTIONS)
    kickoff_time = rng.choice(["12:30", "15:00", "17:30", "20:00"])

    return GroundTruthMatchSheet(
        home_formation=home_formation, away_formation=away_formation,
        home_lineup=home_squad, away_lineup=away_squad,
        substitutions=substitutions, referee=referee,
        assistant_referees=assistants, fourth_official=fourth, var_official=var,
        venue=venue, attendance=attendance, weather=weather, kickoff_time=kickoff_time,
    )


@dataclass
class GroundTruthEvent:
    event_id: str
    event_type: str
    timestamp: float          # seconds into the period
    period: int
    player_name: str
    jersey_number: int
    team: str
    x: float                  # 0-100 ground truth coordinate, absolute frame
    y: float
    end_x: float | None
    end_y: float | None
    outcome: bool
    card_type: str | None = None     # card events: "yellow" | "second_yellow" | "red"
    shot_outcome: str | None = None  # shots/goals: "goal" | "saved" | "off_target" | "blocked" | "post"


@dataclass
class GroundTruthMatch:
    match_id: str
    home_team: str
    away_team: str
    events: list[GroundTruthEvent] = field(default_factory=list)
    # Every squad member in match-sheet order (home then away). Event and
    # lineup exports derive vendor player ids from this one order, so a
    # player has the same id in a vendor's event feed and its lineup feed,
    # as with a real vendor.
    roster: list[str] = field(default_factory=list)


def _clip(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


class _MatchSimulator:
    """Possession-based simulation of one match.

    Positions are tracked relative to the team on the ball: `p` is progress
    from its own goal line (0) to the opponent's (100), `w` is across the
    pitch (0-100). They are converted to the absolute frame (home attacks
    toward x=100 in the first half) only when an event is recorded, so every
    event location agrees with the direction of play."""

    def __init__(self, rng: random.Random, home: str, away: str, sheet: "GroundTruthMatchSheet"):
        self.rng = rng
        self.home, self.away = home, away
        lineups = {home: sheet.home_lineup, away: sheet.away_lineup}
        self.players = {team: {p.name: p for p in squad} for team, squad in lineups.items()}
        self.on_pitch = {team: [p for p in squad if p.is_starter] for team, squad in lineups.items()}
        self.subs = sorted(sheet.substitutions, key=lambda sub: sub.minute)
        self.yellow: set[str] = set()
        self.sent_off: set[str] = set()
        self.score = {home: 0, away: 0}
        self.events: list[GroundTruthEvent] = []
        self.period = 1
        self.t = 0.0

    # --- helpers -----------------------------------------------------------
    def other(self, team: str) -> str:
        return self.away if team == self.home else self.home

    def attacks_right(self, team: str) -> bool:
        return (team == self.home) == (self.period == 1)

    def absolute(self, team: str, p: float, w: float) -> tuple[float, float]:
        if self.attacks_right(team):
            return round(_clip(p), 2), round(_clip(w), 2)
        return round(_clip(100 - p), 2), round(_clip(100 - w), 2)

    def wait(self, lo: float, hi: float) -> None:
        self.t += self.rng.uniform(lo, hi)

    def pick(self, team: str, p: float, roles: tuple[str, ...] | None = None):
        """Who is on the ball: weighted by role for where the ball is."""
        squad = self.on_pitch[team]
        if roles is None:
            if p < 20:
                weights = {"GK": 0.15, "DEF": 0.6, "MID": 0.25, "FWD": 0.0}
            elif p < 40:
                weights = {"GK": 0.0, "DEF": 0.5, "MID": 0.4, "FWD": 0.1}
            elif p < 70:
                weights = {"GK": 0.0, "DEF": 0.15, "MID": 0.6, "FWD": 0.25}
            else:
                weights = {"GK": 0.0, "DEF": 0.05, "MID": 0.4, "FWD": 0.55}
        else:
            weights = {r: 1.0 for r in roles}
        options = [pl for pl in squad if weights.get(POSITION_ROLE.get(pl.position, "MID"), 0) > 0]
        if not options:
            options = [pl for pl in squad if pl.position != "GK"] or squad
        w = [weights.get(POSITION_ROLE.get(pl.position, "MID"), 0.1) or 0.1 for pl in options]
        return self.rng.choices(options, weights=w, k=1)[0]

    def keeper(self, team: str):
        gks = [pl for pl in self.on_pitch[team] if pl.position == "GK"]
        return gks[0] if gks else self.pick(team, 5)

    def emit(self, event_type: str, team: str, player, p: float, w: float,
             end: tuple[float, float] | None = None, outcome: bool = True, **detail) -> None:
        x, y = self.absolute(team, p, w)
        end_x = end_y = None
        if end is not None:
            end_x, end_y = self.absolute(team, end[0], end[1])
        self.events.append(GroundTruthEvent(
            event_id=f"{self.rng.getrandbits(32):08x}",
            event_type=event_type,
            timestamp=round(self.t, 1),
            period=self.period,
            player_name=player.name,
            jersey_number=player.jersey_number,
            team=team,
            x=x, y=y, end_x=end_x, end_y=end_y,
            outcome=outcome,
            card_type=detail.get("card_type"),
            shot_outcome=detail.get("shot_outcome"),
        ))

    def apply_due_substitutions(self) -> None:
        minute = self.t / 60 + (45 if self.period == 2 else 0)
        while self.subs and self.period == 2 and self.subs[0].minute <= minute:
            sub = self.subs.pop(0)
            squad = self.on_pitch[sub.team]
            out = next((pl for pl in squad if pl.name == sub.player_out), None)
            incoming = self.players[sub.team].get(sub.player_in)
            if out is not None and incoming is not None and out.name not in self.sent_off:
                squad[squad.index(out)] = incoming

    # --- phases of play ----------------------------------------------------
    def play_period(self, period: int, kickoff_team: str, length_s: float) -> None:
        self.period, self.t = period, 0.0
        team, p, w = self.kickoff(kickoff_team)
        while self.t < length_s:
            self.apply_due_substitutions()
            team, p, w = self.step(team, p, w)

    def step(self, team: str, p: float, w: float) -> tuple[str, float, float]:
        rng = self.rng
        opp = self.other(team)
        in_box = p >= 83 and 21 <= w <= 79

        # Defending side gets a chance to foul or win the ball first.
        foul_chance = 0.008 if in_box else (0.022 if p > 60 else 0.016)
        if rng.random() < foul_chance:
            return self.foul(team, p, w, in_box)
        if rng.random() < 0.018:
            tackler = self.pick(opp, 100 - p)
            self.emit("tackle", opp, tackler, 100 - p, 100 - w, outcome=True)
            self.wait(1.5, 3.5)
            return opp, 100 - p, 100 - w

        shoot = 0.0 if p < 68 else (0.025 if not in_box else 0.12)
        roll = rng.random()
        if roll < shoot:
            return self.shot(team, p, w)
        if roll < shoot + 0.035:
            return self.dribble(team, p, w)
        if p > 70 and (w < 15 or w > 85) and rng.random() < 0.35:
            return self.cross(team, p, w)
        return self.pass_(team, p, w)

    def kickoff(self, team: str) -> tuple[str, float, float]:
        """Every kickoff is a short pass from the centre spot, back into the
        kicking side's own half."""
        rng = self.rng
        taker = self.pick(team, 50, roles=("FWD", "MID"))
        target = (rng.uniform(36, 46), _clip(50 + rng.gauss(0, 12), 20, 80))
        self.emit("pass", team, taker, 50.0, 50.0, end=target, outcome=True)
        self.wait(1.5, 3)
        return team, target[0], target[1]

    def pass_(self, team: str, p: float, w: float) -> tuple[str, float, float]:
        rng = self.rng
        player = self.pick(team, p)
        advance = rng.gauss(14, 12) if p < 25 else rng.gauss(6, 11)
        tp = _clip(p + advance, 1, 99)
        tw = _clip(w + rng.gauss(0, 20), 0.5, 99.5)
        length = math.hypot((tp - p) * 1.05, (tw - w) * 0.68)
        success = (0.91 if p < 40 else 0.84 if p < 70 else 0.72) - 0.006 * max(0.0, length - 20)
        self.wait(1.8 + length * 0.05, 3.4 + length * 0.07)
        if rng.random() < success:
            self.emit("pass", team, player, p, w, end=(tp, tw), outcome=True)
            return team, tp, tw
        self.emit("pass", team, player, p, w, end=(tp, tw), outcome=False)
        opp = self.other(team)
        if tw <= 3 or tw >= 97:
            self.wait(7, 14)
            thrower = self.pick(opp, 100 - tp, roles=("DEF", "MID"))
            # Ball out on the attacking side's far touchline is the
            # opponent's near one: mirror the width into its frame.
            ow = 0.5 if tw >= 97 else 99.5
            inward = rng.uniform(4, 16)
            land = (_clip(100 - tp + rng.uniform(2, 12), 1, 99), _clip(ow + inward if ow < 50 else ow - inward, 1, 99))
            kept = rng.random() < 0.8
            self.emit("throw_in", opp, thrower, 100 - tp, ow, end=land, outcome=kept)
            return (opp, land[0], land[1]) if kept else (team, 100 - land[0], 100 - land[1])
        # Most misplaced passes are simply picked up (no event of their own,
        # the next action is the other side's); some are read and cut out.
        if rng.random() < 0.25:
            winner = self.pick(opp, 100 - tp)
            self.emit("interception", opp, winner, 100 - tp, 100 - tw, outcome=True)
        self.wait(0.5, 1.5)
        return opp, 100 - tp, 100 - tw

    def dribble(self, team: str, p: float, w: float) -> tuple[str, float, float]:
        rng = self.rng
        player = self.pick(team, p, roles=("MID", "FWD"))
        dp, dw = rng.uniform(4, 11), rng.gauss(0, 7)
        self.wait(2, 4)
        if rng.random() < 0.6:
            self.emit("dribble", team, player, p, w, outcome=True)
            return team, _clip(p + dp, 1, 99), _clip(w + dw, 1, 99)
        self.emit("dribble", team, player, p, w, outcome=False)
        opp = self.other(team)
        tackler = self.pick(opp, 100 - p)
        self.emit("tackle", opp, tackler, 100 - p, 100 - w, outcome=True)
        self.wait(1, 2.5)
        return opp, 100 - p, 100 - w

    def cross(self, team: str, p: float, w: float) -> tuple[str, float, float]:
        rng = self.rng
        player = self.pick(team, p, roles=("DEF", "MID", "FWD"))
        target = (rng.uniform(86, 95), rng.uniform(38, 62))
        self.wait(1.5, 3)
        if rng.random() < 0.3:
            self.emit("pass", team, player, p, w, end=target, outcome=True)
            return self.shot(team, target[0], target[1], header=True)
        self.emit("pass", team, player, p, w, end=target, outcome=False)
        return self.clearance(self.other(team), 100 - target[0], 100 - target[1])

    def clearance(self, team: str, p: float, w: float) -> tuple[str, float, float]:
        """`team` is defending deep and clears the ball."""
        rng = self.rng
        player = self.pick(team, p, roles=("DEF", "MID"))
        if p < 18 and rng.random() < 0.2:
            # Hacked behind under pressure: corner to the attacking side.
            self.emit("clearance", team, player, p, w, end=(0.5, _clip(w + rng.gauss(0, 15), 3, 97)), outcome=False)
            return self.corner(self.other(team))
        land = (_clip(p + rng.uniform(25, 50), 5, 80), _clip(w + rng.gauss(0, 20), 3, 97))
        self.emit("clearance", team, player, p, w, end=land, outcome=True)
        self.wait(2, 4)
        if rng.random() < 0.55:
            return team, land[0], land[1]
        return self.other(team), 100 - land[0], 100 - land[1]

    def shot(self, team: str, p: float, w: float, header: bool = False, penalty: bool = False) -> tuple[str, float, float]:
        rng = self.rng
        opp = self.other(team)
        shooter = self.pick(team, p, roles=("FWD",) if penalty else ("MID", "FWD"))
        distance = math.hypot((100 - p) * 1.05, (w - 50) * 0.68)
        if penalty:
            goal_p = 0.78
        else:
            goal_p = min(0.4, 0.5 * math.exp(-distance / 8.0)) * (0.7 if header else 1.0)
        roll = rng.random()
        if roll < goal_p:
            result = "goal"
        else:
            r = rng.random()
            result = "saved" if r < 0.3 else "off_target" if r < 0.68 else "post" if r < 0.73 else "blocked"
            if penalty and result == "blocked":
                result = "saved"
        on_frame_w = rng.uniform(45.5, 54.5)
        wide_w = rng.choice([rng.uniform(30, 44.5), rng.uniform(55.5, 70)])
        end = {
            "goal": (100, on_frame_w), "saved": (99.5, on_frame_w),
            "off_target": (100, wide_w), "post": (100, rng.choice([44.6, 55.4])),
            "blocked": (_clip(p + rng.uniform(2, 6), 1, 99), _clip(w + rng.uniform(-4, 4), 1, 99)),
        }[result]
        self.wait(1, 2.5)
        if result == "goal":
            self.emit("goal", team, shooter, p, w, end=end, outcome=True, shot_outcome="goal")
            self.score[team] += 1
            self.wait(50, 90)  # celebration and restart
            return self.kickoff(opp)
        self.emit("shot", team, shooter, p, w, end=end, outcome=(result == "saved"), shot_outcome=result)
        if result == "saved":
            self.wait(0.5, 1.5)
            gk = self.keeper(opp)
            self.emit("save", opp, gk, rng.uniform(1, 4), 100 - end[1], outcome=True)
            if rng.random() < 0.55:
                self.wait(4, 9)
                return opp, 5.0, 50.0
            return self.corner(team)
        if result == "blocked":
            if rng.random() < 0.35:
                return self.corner(team)
            return self.clearance(opp, 100 - end[0], 100 - end[1])
        if result == "post" and rng.random() < 0.5:
            return team, _clip(p + 4, 1, 95), 50.0
        return self.goal_kick(opp)

    def corner(self, team: str) -> tuple[str, float, float]:
        rng = self.rng
        self.wait(18, 32)
        taker = self.pick(team, 95, roles=("MID", "FWD", "DEF"))
        side = rng.choice([0.5, 99.5])
        target = (rng.uniform(88, 96), rng.uniform(36, 64))
        roll = rng.random()
        if roll < 0.22:
            self.emit("corner", team, taker, 99.5, side, end=target, outcome=True)
            self.wait(1, 2)
            return self.shot(team, target[0], target[1], header=True)
        if roll < 0.72:
            self.emit("corner", team, taker, 99.5, side, end=target, outcome=False)
            self.wait(1, 2)
            return self.clearance(self.other(team), 100 - target[0], 100 - target[1])
        short = (rng.uniform(80, 90), _clip(side + (12 if side < 50 else -12), 1, 99))
        self.emit("corner", team, taker, 99.5, side, end=short, outcome=True)
        return team, short[0], short[1]

    def goal_kick(self, team: str) -> tuple[str, float, float]:
        rng = self.rng
        self.wait(12, 24)
        gk = self.keeper(team)
        land = (rng.uniform(30, 62), rng.uniform(15, 85))
        won = rng.random() < 0.55
        self.emit("goal_kick", team, gk, 6, 50, end=land, outcome=won)
        self.wait(2, 4)
        if won:
            return team, land[0], land[1]
        return self.other(team), 100 - land[0], 100 - land[1]

    def foul(self, team: str, p: float, w: float, in_box: bool) -> tuple[str, float, float]:
        """The defending side fouls the team on the ball at (p, w)."""
        rng = self.rng
        opp = self.other(team)
        offender = self.pick(opp, 100 - p, roles=("DEF", "MID", "FWD"))
        self.emit("foul", opp, offender, 100 - p, 100 - w, outcome=True)

        roll = rng.random()
        card = None
        if roll < 0.004:
            card = "red"
        elif roll < (0.2 if in_box or p > 65 else 0.11):
            card = "second_yellow" if offender.name in self.yellow else "yellow"
        if card:
            self.wait(18, 40)
            self.emit("card", opp, offender, 100 - p, 100 - w, outcome=True, card_type=card)
            if card == "yellow":
                self.yellow.add(offender.name)
            else:
                self.sent_off.add(offender.name)
                squad = self.on_pitch[opp]
                if offender in squad and len(squad) > 7:
                    squad.remove(offender)

        if in_box:
            self.wait(45, 80)  # penalty awarded, set up
            return self.shot(team, 88.5, 50.0, penalty=True)
        self.wait(8, 20)
        if p > 72 and rng.random() < 0.3:
            return self.shot(team, p, w)
        return team, p, w


def generate_ground_truth(match_id: str, home_team: str, away_team: str,
                           n_events: int | None = None, seed: int | None = None,
                           sheet: "GroundTruthMatchSheet | None" = None) -> GroundTruthMatch:
    """Simulates one internally-consistent match: the 'answer key'.

    Plays both halves with stoppage time. `sheet` supplies the squads and
    substitutions (generated from seed + 100 when not given, matching
    generate_match_bundle). `n_events`, if set, truncates the timeline to
    that many events (handy for small test fixtures)."""
    rng = random.Random(seed)
    if sheet is None:
        sheet = generate_match_sheet(home_team, away_team, seed=(seed or 0) + 100)
    sim = _MatchSimulator(rng, home_team, away_team, sheet)
    sim.play_period(1, home_team, 45 * 60 + rng.uniform(60, 240))
    sim.play_period(2, away_team, 45 * 60 + rng.uniform(120, 360))

    events = sim.events[:n_events] if n_events else sim.events
    roster = [pl.name for pl in sheet.home_lineup + sheet.away_lineup]
    return GroundTruthMatch(match_id=match_id, home_team=home_team, away_team=away_team,
                            events=events, roster=roster)


# ---------------------------------------------------------------------------
# Vendor exporters: same ground truth, 3 different shapes.
# Each exporter also applies quality-tier degradation (missing fields,
# corruption, unknown-format drift) so the agents have something real to
# recover from.
# ---------------------------------------------------------------------------

def _maybe_drop(value, quality: Quality, rng: random.Random, drop_rate: float):
    """Simulates missing data. Higher drop_rate = more likely to return None."""
    if quality == "clean":
        return value
    rate = drop_rate if quality == "partial" else drop_rate * 2.5
    return None if rng.random() < rate else value


# Vendor event encodings. Card colour and shot result use each vendor's real
# scheme (see the module docstring); schema_mapper reads exactly these.
OPTA_SHOT_TYPE = {"off_target": "MISS", "post": "POST", "saved": "ATTEMPT_SAVED", "blocked": "ATTEMPT_SAVED"}
OPTA_TYPE_NAME = {"throw_in": "THROW_IN", "goal_kick": "GOAL_KICK"}
OPTA_CARD_QUALIFIER = {"yellow": 31, "second_yellow": 32, "red": 33}
STATSBOMB_SHOT_OUTCOME = {"goal": "Goal", "saved": "Saved", "off_target": "Off T", "blocked": "Blocked", "post": "Post"}
STATSBOMB_CARD = {"yellow": "Yellow Card", "second_yellow": "Second Yellow", "red": "Red Card"}
STATSBOMB_PASS_TYPE = {"corner": "Corner", "throw_in": "Throw-in", "goal_kick": "Goal Kick"}
WYSCOUT_SUB_EVENT = {
    "pass": "Simple pass", "shot": "Shot", "goal": "Shot", "foul": "Foul", "card": "Card",
    "tackle": "Ground defending duel", "dribble": "Ground attacking duel",
    "interception": "Interception", "clearance": "Clearance", "save": "Save attempt",
    "corner": "Corner", "throw_in": "Throw in", "goal_kick": "Goal kick",
}
WYSCOUT_SHOT_TAGS = {"goal": [101, 1801], "saved": [1801], "off_target": [1802], "blocked": [2101, 1802], "post": [1802]}
WYSCOUT_CARD_TAG = {"yellow": 1702, "second_yellow": 1703, "red": 1701}


def to_opta_format(match: GroundTruthMatch, quality: Quality, seed: int | None = None) -> dict:
    """Opta-style: pitch 0-120 x 0-80, flat attributes, player identified by
    vendor-specific id. Shot results are separate event types (MISS, POST,
    ATTEMPT_SAVED, GOAL; qualifier 82 marks a blocked attempt) and card
    colour is a qualifier on the CARD event (31 yellow, 32 second yellow,
    33 red), as in Opta's own feeds."""
    rng = random.Random(seed)
    player_ids = {name: f"p{20000 + i}" for i, name in enumerate(match.roster)}

    def scale_x(x):  # 0-100 -> 0-120
        return round(x * 1.2, 2)

    def scale_y(y):  # 0-100 -> 0-80
        return round(y * 0.8, 2)

    events = []
    for e in match.events:
        qualifier_id = player_ids.get(e.player_name)
        qualifier_id = _maybe_drop(qualifier_id, quality, rng, 0.03)

        jersey = _maybe_drop(e.jersey_number, quality, rng, 0.04)

        if e.event_type == "shot":
            type_name = OPTA_SHOT_TYPE.get(e.shot_outcome, "SHOT")
        else:
            type_name = OPTA_TYPE_NAME.get(e.event_type, e.event_type.upper())
        qualifiers = []
        if e.shot_outcome == "blocked":
            qualifiers.append({"QualifierId": 82})
        if e.card_type in OPTA_CARD_QUALIFIER:
            qualifiers.append({"QualifierId": OPTA_CARD_QUALIFIER[e.card_type]})

        ev = {
            "EventId": e.event_id,
            "TypeName": type_name,
            "Min": int(e.timestamp // 60),
            "Sec": round(e.timestamp % 60, 1),
            "PeriodId": e.period,
            "PlayerRef": qualifier_id,
            "JerseyNumber": jersey,
            "TeamName": e.team,
            "X": scale_x(e.x),
            "Y": scale_y(e.y),
        }
        if e.end_x is not None:
            ev["PassEndX"] = scale_x(e.end_x)
            ev["PassEndY"] = scale_y(e.end_y)
        ev["Outcome"] = 1 if e.outcome else 0
        if qualifiers:
            ev["Qualifiers"] = qualifiers

        if quality == "messy":
            # Simulate real-world Opta weirdness: occasional out-of-range values,
            # unknown qualifier codes, inconsistent casing
            if rng.random() < 0.08:
                ev["X"] = round(ev["X"] * rng.uniform(1.1, 1.4), 2)  # out of bounds
            if rng.random() < 0.05:
                ev["TypeName"] = f"UNKNOWN_QUALIFIER_{rng.randint(100,999)}"
            if rng.random() < 0.04:
                del ev["PeriodId"]  # missing required-ish field

        events.append(ev)

    return {
        "MatchId": match.match_id,
        "HomeTeam": match.home_team,
        "AwayTeam": match.away_team,
        "Events": events,
        "_vendor_format": "opta",
    }


def to_statsbomb_format(match: GroundTruthMatch, quality: Quality, seed: int | None = None) -> dict:
    """StatsBomb-style: pitch 0-100 x 0-100 already, deeply nested JSON,
    player identified by vendor-specific integer id in a nested dict. Goals
    are Shots with shot.outcome "Goal"; corners, throw-ins and goal kicks
    are Passes with pass.type; completed passes carry no outcome; cards are
    Bad Behaviour events with bad_behaviour.card -- StatsBomb's own shapes."""
    rng = random.Random(seed)
    player_ids = {name: 6000 + i for i, name in enumerate(match.roster)}

    events = []
    for e in match.events:
        pid = player_ids.get(e.player_name)
        pid = _maybe_drop(pid, quality, rng, 0.02)
        jersey = _maybe_drop(e.jersey_number, quality, rng, 0.05)

        player_obj = None
        if pid:
            player_obj = {"id": pid, "name": e.player_name}
            if jersey is not None:
                player_obj["jersey_number"] = jersey

        ev = {
            "id": e.event_id,
            "type": {"name": e.event_type},
            "timestamp": f"00:{int(e.timestamp // 60):02d}:{e.timestamp % 60:05.2f}",
            "period": e.period,
            "player": player_obj,
            "team": {"name": e.team},
            "location": [round(e.x, 2), round(e.y, 2)],
        }
        end = [round(e.end_x, 2), round(e.end_y, 2)] if e.end_x is not None else None

        if e.event_type in ("pass", "corner", "throw_in", "goal_kick"):
            ev["type"] = {"name": "Pass"}
            pass_obj = {}
            if end:
                pass_obj["end_location"] = end
            if e.event_type in STATSBOMB_PASS_TYPE:
                pass_obj["type"] = {"name": STATSBOMB_PASS_TYPE[e.event_type]}
            if not e.outcome:
                pass_obj["outcome"] = {"name": "Incomplete"}
            ev["pass"] = pass_obj
        elif e.event_type in ("shot", "goal"):
            ev["type"] = {"name": "Shot"}
            shot_obj = {"outcome": {"name": STATSBOMB_SHOT_OUTCOME.get(e.shot_outcome, "Off T")}}
            if end:
                shot_obj["end_location"] = end + [round(rng.uniform(0.2, 2.4), 1)]
            ev["shot"] = shot_obj
        elif e.event_type == "foul":
            ev["type"] = {"name": "Foul Committed"}
        elif e.event_type == "card":
            ev["type"] = {"name": "Bad Behaviour"}
            ev["bad_behaviour"] = {"card": {"name": STATSBOMB_CARD[e.card_type]}}
        elif e.event_type == "tackle":
            ev["type"] = {"name": "Duel"}
            ev["duel"] = {"type": {"name": "Tackle"}, "outcome": {"name": "Won" if e.outcome else "Lost In Play"}}
        elif e.event_type == "dribble":
            ev["type"] = {"name": "Dribble"}
            ev["dribble"] = {"outcome": {"name": "Complete" if e.outcome else "Incomplete"}}
        elif e.event_type == "interception":
            ev["type"] = {"name": "Interception"}
            ev["interception"] = {"outcome": {"name": "Won"}}
        elif e.event_type == "clearance":
            ev["type"] = {"name": "Clearance"}
        elif e.event_type == "save":
            ev["type"] = {"name": "Goal Keeper"}
            ev["goalkeeper"] = {"type": {"name": "Shot Saved"}}

        if quality == "messy":
            if rng.random() < 0.06:
                ev["location"] = [round(ev["location"][0] + rng.uniform(5, 50), 2), ev["location"][1]]
            if rng.random() < 0.05:
                ev["type"] = {"name": f"custom_event_{rng.randint(1,50)}"}
            if rng.random() < 0.03:
                ev.pop("period", None)

        events.append(ev)

    return {
        "match_id": match.match_id,
        "home_team": {"name": match.home_team},
        "away_team": {"name": match.away_team},
        "events": events,
        "_vendor_format": "statsbomb",
    }


def to_wyscout_format(match: GroundTruthMatch, quality: Quality, seed: int | None = None) -> dict:
    """Wyscout-style: proprietary-ish pixel/percentage coordinates (we simulate
    as 0-100 but with different field names and a different player ID scheme),
    known for being the messiest of the three in the real world. Uses
    Wyscout's descriptive subEventNames and numeric tags for result detail
    (101 goal, 2101 blocked, 1801/1802 accurate, 1701-1703 cards). One
    simplification: real Wyscout puts the card tag on the Foul itself; here
    the card is its own "Card" record so every vendor file carries the same
    events (schema_mapper handles both shapes)."""
    rng = random.Random(seed)
    player_ids = {name: f"player_{8000 + i}" for i, name in enumerate(match.roster)}

    events = []
    for e in match.events:
        pid = player_ids.get(e.player_name)
        pid = _maybe_drop(pid, quality, rng, 0.05)
        jersey = _maybe_drop(e.jersey_number, quality, rng, 0.04)

        ev = {
            "eventId": e.event_id,
            "subEventName": WYSCOUT_SUB_EVENT.get(e.event_type, e.event_type),
            "matchPeriod": f"{e.period}H",
            "eventSec": e.timestamp,
            "trackable_object": pid,
            "jerseyNumber": jersey,
            "teamName": e.team,
            "positions": [{"x": round(e.x, 1), "y": round(e.y, 1)}],
        }
        if e.end_x is not None:
            ev["positions"].append({"x": round(e.end_x, 1), "y": round(e.end_y, 1)})
        ev["accurate"] = e.outcome
        tags = list(WYSCOUT_SHOT_TAGS.get(e.shot_outcome, [])) if e.event_type in ("shot", "goal") else []
        if e.card_type in WYSCOUT_CARD_TAG:
            tags.append(WYSCOUT_CARD_TAG[e.card_type])
        if tags:
            ev["tags"] = [{"id": t} for t in tags]

        if quality == "messy":
            if rng.random() < 0.10:
                # Wyscout's real-world quirk: positions sometimes come as percentages > 100
                ev["positions"][0]["x"] = round(ev["positions"][0]["x"] * rng.uniform(1.0, 1.3), 1)
            if rng.random() < 0.07:
                ev["subEventName"] = None
            if rng.random() < 0.06:
                del ev["trackable_object"]
                ev.pop("jerseyNumber", None)  # both player identifiers gone — worst case for Entity Resolver

        events.append(ev)

    return {
        "wyId": match.match_id,
        "label": f"{match.home_team} - {match.away_team}",
        "events": events,
        "_vendor_format": "wyscout",
    }


# ---------------------------------------------------------------------------
# Match-sheet vendor exporters: same ground-truth sheet, 3 different shapes --
# mirroring how the event exporters above work, including quality-tier
# degradation via _maybe_drop() so Schema Mapper has something real to
# recover from on this pillar too.
# ---------------------------------------------------------------------------

def _lineup_entry_dict(p: GroundTruthLineupPlayer, player_ids: dict, quality: Quality, rng: random.Random,
                        id_key: str, name_key: str, jersey_key: str, position_key: str) -> dict:
    pid = player_ids.get(p.name)
    pid = _maybe_drop(pid, quality, rng, 0.03)
    jersey = _maybe_drop(p.jersey_number, quality, rng, 0.04)
    position = _maybe_drop(p.position, quality, rng, 0.03)
    return {
        id_key: pid,
        name_key: p.name,
        jersey_key: jersey,
        position_key: position,
    }


def to_opta_match_sheet(sheet: GroundTruthMatchSheet, home_team: str, away_team: str,
                         quality: Quality, seed: int | None = None) -> dict:
    """Opta-style team-sheet feed: flat MatchPlayer records per side, Start/
    Sub status instead of a separate starters/bench list."""
    rng = random.Random(seed)
    all_names = [p.name for p in sheet.home_lineup + sheet.away_lineup]
    player_ids = {name: f"p{20000 + i}" for i, name in enumerate(all_names)}

    def team_block(team_name, formation, squad):
        players = []
        for p in squad:
            entry = _lineup_entry_dict(p, player_ids, quality, rng, "PlayerRef", "PlayerName", "ShirtNumber", "Position")
            entry["Status"] = "Start" if p.is_starter else "Sub"
            entry["Captain"] = p.is_captain
            players.append(entry)
        return {"TeamName": team_name, "Formation": formation, "MatchPlayer": players}

    subs = []
    for s in sheet.substitutions:
        subs.append({
            "TeamName": s.team,
            "PlayerOffRef": player_ids.get(s.player_out),
            "PlayerOnRef": player_ids.get(s.player_in),
            "Min": s.minute,
            "PeriodId": s.period,
        })

    return {
        "TeamData": [
            team_block(home_team, sheet.home_formation, sheet.home_lineup),
            team_block(away_team, sheet.away_formation, sheet.away_lineup),
        ],
        "Substitution": subs,
        "MatchOfficial": [
            {"Role": "Main", "Name": sheet.referee},
            {"Role": "Assistant1", "Name": sheet.assistant_referees[0] if sheet.assistant_referees else None},
            {"Role": "Assistant2", "Name": sheet.assistant_referees[1] if len(sheet.assistant_referees) > 1 else None},
            {"Role": "Fourth", "Name": sheet.fourth_official},
            {"Role": "VAR", "Name": sheet.var_official},
        ],
        "Venue": {"Name": sheet.venue},
        "Attendance": sheet.attendance,
        "Weather": sheet.weather,
        "KickOffTime": sheet.kickoff_time,
        "_vendor_format": "opta_match_sheet",
    }


def to_statsbomb_match_sheet(sheet: GroundTruthMatchSheet, home_team: str, away_team: str,
                              quality: Quality, seed: int | None = None) -> dict:
    """StatsBomb-style lineups feed: nested per-team lineup objects with a
    `positions` list, separate from the event stream."""
    rng = random.Random(seed)
    all_names = [p.name for p in sheet.home_lineup + sheet.away_lineup]
    player_ids = {name: 6000 + i for i, name in enumerate(all_names)}

    def team_block(team_name, formation, squad):
        lineup = []
        for p in squad:
            entry = _lineup_entry_dict(p, player_ids, quality, rng, "player_id", "player_name", "jersey_number", "position_name")
            entry["starter"] = p.is_starter
            entry["captain"] = p.is_captain
            lineup.append(entry)
        return {"team_name": team_name, "formation": formation, "lineup": lineup}

    subs = [{
        "team": s.team, "player_out": s.player_out, "player_in": s.player_in,
        "minute": s.minute, "period": s.period, "reason": s.reason,
    } for s in sheet.substitutions]

    return {
        "home_team": team_block(home_team, sheet.home_formation, sheet.home_lineup),
        "away_team": team_block(away_team, sheet.away_formation, sheet.away_lineup),
        "substitutions": subs,
        "referee": {"name": sheet.referee},
        "officials": {
            "assistants": sheet.assistant_referees,
            "fourth_official": sheet.fourth_official,
            "var": sheet.var_official,
        },
        "stadium": {"name": sheet.venue},
        "attendance": sheet.attendance,
        "weather": sheet.weather,
        "kick_off": sheet.kickoff_time,
        "_vendor_format": "statsbomb_match_sheet",
    }


def to_wyscout_match_sheet(sheet: GroundTruthMatchSheet, home_team: str, away_team: str,
                            quality: Quality, seed: int | None = None) -> dict:
    """Wyscout-style lineups feed: lower-cased compact keys, formation as a
    raw string code, officials folded into one flat `referees` list --
    the messiest of the three shapes, same as its event feed."""
    rng = random.Random(seed)
    all_names = [p.name for p in sheet.home_lineup + sheet.away_lineup]
    player_ids = {name: f"player_{8000 + i}" for i, name in enumerate(all_names)}

    def team_block(squad):
        lineups = []
        for p in squad:
            entry = _lineup_entry_dict(p, player_ids, quality, rng, "playerId", "shortName", "shirtNumber", "role")
            entry["bench"] = not p.is_starter
            lineups.append(entry)
        return lineups

    teams = {
        "home": {"formation": sheet.home_formation, "lineups": team_block(sheet.home_lineup), "teamName": home_team},
        "away": {"formation": sheet.away_formation, "lineups": team_block(sheet.away_lineup), "teamName": away_team},
    }

    subs = [{
        "teamName": s.team, "playerOut": s.player_out, "playerIn": s.player_in,
        "minute": s.minute,
    } for s in sheet.substitutions]

    referees = [
        {"role": "referee", "name": sheet.referee},
        {"role": "fourthOfficial", "name": sheet.fourth_official},
        {"role": "var", "name": sheet.var_official},
    ] + [{"role": "assistant", "name": a} for a in sheet.assistant_referees]

    if quality == "messy" and rng.random() < 0.1:
        teams["home"]["formation"] = None  # Wyscout's real-world quirk: formation sometimes just absent

    return {
        "teams": teams,
        "substitutions": subs,
        "referees": referees,
        "venue": {"name": sheet.venue},
        "attendance": sheet.attendance,
        "weatherConditions": sheet.weather,
        "kickOffTime": sheet.kickoff_time,
        "_vendor_format": "wyscout_match_sheet",
    }


# ---------------------------------------------------------------------------
# Top-level: generate one match at one quality tier, all 3 vendor files.
# ---------------------------------------------------------------------------

def generate_match_bundle(match_id: str, home_team: str, away_team: str,
                           quality: Quality, n_events: int | None = None,
                           seed: int = 42) -> dict[str, dict]:
    sheet = generate_match_sheet(home_team, away_team, seed=seed + 100)
    truth = generate_ground_truth(match_id, home_team, away_team, n_events=n_events, seed=seed, sheet=sheet)
    return {
        "opta": to_opta_format(truth, quality, seed=seed + 1),
        "statsbomb": to_statsbomb_format(truth, quality, seed=seed + 2),
        "wyscout": to_wyscout_format(truth, quality, seed=seed + 3),
        "opta_match_sheet": to_opta_match_sheet(sheet, home_team, away_team, quality, seed=seed + 4),
        "statsbomb_match_sheet": to_statsbomb_match_sheet(sheet, home_team, away_team, quality, seed=seed + 5),
        "wyscout_match_sheet": to_wyscout_match_sheet(sheet, home_team, away_team, quality, seed=seed + 6),
        "_ground_truth_event_count": len(truth.events),
    }


def write_sample_matches(output_dir: str = "backend/data/sample_matches"):
    """Generates the 3 fixed matches required by PRD Section 14 locked decisions:
    one clean, one partial, one messy — each exported in all 3 vendor formats."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    matches = [
        # Seeds are fixed for reproducible tests; the clean match's seed was
        # picked so the default demo has goals in both halves and a booking.
        ("PL_2025_CLEAN_001", "Liverpool", "Manchester City", "clean", 50),
        ("PL_2025_PARTIAL_001", "Arsenal", "Chelsea", "partial", 101),
        ("PL_2025_MESSY_001", "Manchester United", "Tottenham", "messy", 777),
    ]

    manifest = []
    for match_id, home, away, quality, seed in matches:
        bundle = generate_match_bundle(match_id, home, away, quality, seed=seed)
        ground_truth_count = bundle.pop("_ground_truth_event_count")

        match_dir = out / match_id
        match_dir.mkdir(exist_ok=True)
        # Match-sheet files go in their own subdirectory, not match_dir
        # itself -- every existing agent's `match_dir.glob("*.json")` (a
        # non-recursive glob) expects exactly the 3 event vendor files
        # there, and match sheets are a separate feed in the first place.
        sheet_dir = match_dir / "match_sheets"
        sheet_dir.mkdir(exist_ok=True)
        event_files, match_sheet_files = [], []
        for name, data in bundle.items():
            if name.endswith("_match_sheet"):
                path = sheet_dir / f"{name}.json"
                match_sheet_files.append(f"{match_id}/match_sheets/{name}.json")
            else:
                path = match_dir / f"{name}.json"
                event_files.append(f"{match_id}/{name}.json")
            path.write_text(json.dumps(data, indent=2))

        manifest.append({
            "match_id": match_id,
            "quality_tier": quality,
            "home_team": home,
            "away_team": away,
            "ground_truth_event_count": ground_truth_count,
            "files": event_files,
            "match_sheet_files": match_sheet_files,
        })
        print(f"  {match_id} ({quality}): {ground_truth_count} ground-truth events -> {match_dir}/")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {out}/manifest.json")
    return manifest


if __name__ == "__main__":
    print("Generating synthetic match bundles (clean / partial / messy)...\n")
    write_sample_matches()
