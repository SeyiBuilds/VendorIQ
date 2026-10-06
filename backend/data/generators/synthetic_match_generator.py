"""
Synthetic football match data generator.

Generates ONE underlying "ground truth" match (same events, same players,
same timeline) and then exports it into 3 vendor-shaped formats: Opta-style
XML-ish JSON, StatsBomb-style nested JSON, and Wyscout-style proprietary JSON.

Because all 3 outputs come from the same ground truth, we know the "correct"
normalized answer — which is what lets us test the agent pipeline's output
for correctness, not just "did it run."

Supports 3 quality tiers (per PRD Section 14 locked decisions):
  - clean:   no missing fields, no corruption
  - partial: some missing player IDs / coordinates, still parseable
  - messy:   malformed structure, unknown fields, out-of-range values,
             the kind of data that should trigger agent retries/fallbacks
"""

from __future__ import annotations
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Quality = Literal["clean", "partial", "messy"]

EVENT_TYPES = ["pass", "shot", "tackle", "foul", "corner", "throw_in",
               "clearance", "interception", "dribble", "save", "goal", "card"]

# Weighted so common open-play events still dominate while goal/card stay
# rare but present -- a 300-event synthetic match should land around 2-4
# goals and 2-5 cards, in line with a real Premier League match, instead
# of either never happening (uniform EVENT_TYPES without weights) or
# showing up as often as a tackle (uniform WITH goal/card just added).
EVENT_TYPE_WEIGHTS = {
    "pass": 30, "shot": 8, "tackle": 10, "foul": 8, "corner": 5,
    "throw_in": 5, "clearance": 8, "interception": 8, "dribble": 8,
    "save": 4, "goal": 1, "card": 1.5,
}

PLAYER_POOL = [
    {"name": "Lionel Messi", "jersey": 10, "position": "RW"},
    {"name": "Mohamed Salah", "jersey": 11, "position": "RW"},
    {"name": "Erling Haaland", "jersey": 9, "position": "ST"},
    {"name": "Kevin De Bruyne", "jersey": 17, "position": "CM"},
    {"name": "Virgil van Dijk", "jersey": 4, "position": "CB"},
    {"name": "Rodri", "jersey": 16, "position": "CDM"},
    {"name": "Alisson Becker", "jersey": 1, "position": "GK"},
    {"name": "Ederson", "jersey": 31, "position": "GK"},
    {"name": "Trent Alexander-Arnold", "jersey": 66, "position": "RB"},
    {"name": "Phil Foden", "jersey": 47, "position": "LW"},
    {"name": "Jack Grealish", "jersey": 10, "position": "LW"},
    {"name": "Bernardo Silva", "jersey": 20, "position": "CM"},
]

# ---------------------------------------------------------------------------
# Match-sheet data: lineups, formations, subs, officials, venue. A full
# matchday needs 2 x 18-man squads (11 starters + 7 bench) -- PLAYER_POOL's
# 12 named real players aren't enough for that, so the rest of each squad
# is filled with synthetic (non-real) names built from these pools. This is
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
    """One team's 18-man squad: 11 starters in a formation + 7 substitutes.
    Mixes in real PLAYER_POOL names where available (capped so both squads
    don't fight over the same 12 names) and fills the rest synthetically."""
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

    real_pool = [p for p in PLAYER_POOL if p["name"] not in used_names]
    rng.shuffle(real_pool)

    for i, position in enumerate(slots):
        if real_pool and rng.random() < 0.3:
            p = real_pool.pop()
            used_names.add(p["name"])
            name, jersey = p["name"], p["jersey"]
            if jersey in used_jerseys:
                jersey = next_jersey()
            else:
                used_jerseys.add(jersey)
        else:
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
    x: float                  # 0-100 ground truth coordinate
    y: float
    end_x: float | None
    end_y: float | None
    outcome: bool


@dataclass
class GroundTruthMatch:
    match_id: str
    home_team: str
    away_team: str
    events: list[GroundTruthEvent] = field(default_factory=list)


def generate_ground_truth(match_id: str, home_team: str, away_team: str,
                           n_events: int = 300, seed: int | None = None) -> GroundTruthMatch:
    """Generates one internally-consistent match. This is the 'answer key'."""
    rng = random.Random(seed)
    match = GroundTruthMatch(match_id=match_id, home_team=home_team, away_team=away_team)

    t = 0.0
    for i in range(n_events):
        t += rng.uniform(1.5, 8.0)
        period = 1 if t < 2700 else 2
        period_t = t if period == 1 else t - 2700
        if period_t > 2700:
            break  # stop generating past 2x45min in seconds

        player = rng.choice(PLAYER_POOL)
        team = home_team if rng.random() < 0.5 else away_team
        etype = rng.choices(EVENT_TYPES, weights=[EVENT_TYPE_WEIGHTS[t] for t in EVENT_TYPES], k=1)[0]
        x, y = rng.uniform(0, 100), rng.uniform(0, 100)
        has_end = etype in ("pass", "shot", "clearance")
        end_x = min(100, max(0, x + rng.uniform(-30, 30))) if has_end else None
        end_y = min(100, max(0, y + rng.uniform(-30, 30))) if has_end else None

        match.events.append(GroundTruthEvent(
            event_id=str(uuid.uuid4())[:8],
            event_type=etype,
            timestamp=round(period_t, 1),
            period=period,
            player_name=player["name"],
            jersey_number=player["jersey"],
            team=team,
            x=round(x, 2),
            y=round(y, 2),
            end_x=round(end_x, 2) if end_x is not None else None,
            end_y=round(end_y, 2) if end_y is not None else None,
            outcome=rng.random() < 0.75,
        ))

    return match


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


def to_opta_format(match: GroundTruthMatch, quality: Quality, seed: int | None = None) -> dict:
    """Opta-style: pitch 0-120 x 0-80, XML-flavored JSON (flat attributes),
    player identified by vendor-specific numeric ID."""
    rng = random.Random(seed)
    player_ids = {p["name"]: f"p{10000 + i}" for i, p in enumerate(PLAYER_POOL)}

    def scale_x(x):  # 0-100 -> 0-120
        return round(x * 1.2, 2)

    def scale_y(y):  # 0-100 -> 0-80
        return round(y * 0.8, 2)

    events = []
    for e in match.events:
        qualifier_id = player_ids.get(e.player_name)
        qualifier_id = _maybe_drop(qualifier_id, quality, rng, 0.03)

        jersey = _maybe_drop(e.jersey_number, quality, rng, 0.04)

        ev = {
            "EventId": e.event_id,
            "TypeName": e.event_type.upper(),
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
    player identified by vendor-specific integer id in a nested dict."""
    rng = random.Random(seed)
    player_ids = {p["name"]: 5000 + i for i, p in enumerate(PLAYER_POOL)}

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
        if e.end_x is not None:
            ev["pass"] = {"end_location": [round(e.end_x, 2), round(e.end_y, 2)],
                           "outcome": {"name": "Complete" if e.outcome else "Incomplete"}}

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
    known for being the messiest of the three in the real world."""
    rng = random.Random(seed)
    player_ids = {p["name"]: f"player_{7000 + i}" for i, p in enumerate(PLAYER_POOL)}

    events = []
    for e in match.events:
        pid = player_ids.get(e.player_name)
        pid = _maybe_drop(pid, quality, rng, 0.05)
        jersey = _maybe_drop(e.jersey_number, quality, rng, 0.04)

        ev = {
            "eventId": e.event_id,
            "subEventName": e.event_type,
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
                           quality: Quality, n_events: int = 300,
                           seed: int = 42) -> dict[str, dict]:
    truth = generate_ground_truth(match_id, home_team, away_team, n_events=n_events, seed=seed)
    sheet = generate_match_sheet(home_team, away_team, seed=seed + 100)
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
        ("PL_2025_CLEAN_001", "Liverpool", "Manchester City", "clean", 42),
        ("PL_2025_PARTIAL_001", "Arsenal", "Chelsea", "partial", 101),
        ("PL_2025_MESSY_001", "Manchester United", "Tottenham", "messy", 777),
    ]

    manifest = []
    for match_id, home, away, quality, seed in matches:
        bundle = generate_match_bundle(match_id, home, away, quality, n_events=300, seed=seed)
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
