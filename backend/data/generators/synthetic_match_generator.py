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
               "clearance", "interception", "dribble", "save"]

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
        etype = rng.choice(EVENT_TYPES)
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
# Top-level: generate one match at one quality tier, all 3 vendor files.
# ---------------------------------------------------------------------------

def generate_match_bundle(match_id: str, home_team: str, away_team: str,
                           quality: Quality, n_events: int = 300,
                           seed: int = 42) -> dict[str, dict]:
    truth = generate_ground_truth(match_id, home_team, away_team, n_events=n_events, seed=seed)
    return {
        "opta": to_opta_format(truth, quality, seed=seed + 1),
        "statsbomb": to_statsbomb_format(truth, quality, seed=seed + 2),
        "wyscout": to_wyscout_format(truth, quality, seed=seed + 3),
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
        for vendor, data in bundle.items():
            path = match_dir / f"{vendor}.json"
            path.write_text(json.dumps(data, indent=2))

        manifest.append({
            "match_id": match_id,
            "quality_tier": quality,
            "home_team": home,
            "away_team": away,
            "ground_truth_event_count": ground_truth_count,
            "files": [f"{match_id}/{v}.json" for v in bundle.keys()],
        })
        print(f"  {match_id} ({quality}): {ground_truth_count} ground-truth events -> {match_dir}/")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {out}/manifest.json")
    return manifest


if __name__ == "__main__":
    print("Generating synthetic match bundles (clean / partial / messy)...\n")
    write_sample_matches()
