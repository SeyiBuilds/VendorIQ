"""
Agent 2: Schema Mapper

Converts vendor-shaped raw events into a common intermediate shape (field
names aligned to CDF, coordinates still in the vendor's native scale —
normalization is Agent 3's job; player identity still vendor-specific —
resolution is Agent 4's job).

Scope note: the MVP demo handles Opta/StatsBomb/Wyscout, because that's
what the hackathon's synthetic data generator produces. But the agent itself
is NOT hardcoded to those three. Each vendor is a data-driven entry in
VENDOR_MAPPERS — adding a new vendor means adding one entry, not rewriting
this file. An unrecognized vendor (Format Detective returns "unknown")
still gets a best-effort generic mapping instead of failing outright.

Per PRD Section 4.1 fallback logic:
  Attempt 1: use the vendor's known/primary field name
  Attempt 2: try alternate field names (schema drift, API version differences)
  Fallback:  use null, flag the field as missing, keep going
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

CANONICAL_EVENT_TYPES = {
    "pass", "shot", "tackle", "foul", "card", "substitution",
    "corner", "throw_in", "goal_kick", "offside", "goal",
    "save", "interception", "clearance", "dribble", "other",
}


@dataclass
class MappedEvent:
    """Intermediate shape: field names aligned to CDF, but coordinates are
    still vendor-native scale and player_id is still unresolved (raw vendor
    identifiers only). Agents 3 and 4 finish the job."""
    event_id: str
    event_type: str                  # canonicalized, "other" if unmapped
    event_type_raw: Optional[str]    # original vendor string, kept for traceability + export-back
    timestamp: float
    period: Optional[int]
    team: Optional[str]
    raw_player_ref: dict             # vendor-specific identifiers, e.g. {"vendor_id": "p10000", "jersey_number": None, "name": None}
    x: Optional[float]               # native vendor coordinate scale
    y: Optional[float]
    end_x: Optional[float]
    end_y: Optional[float]
    outcome: Optional[bool]
    vendor_source: str
    mapping_confidence: float
    mapping_attempts: int
    mapping_log: list[str] = field(default_factory=list)
    raw_metadata: dict = field(default_factory=dict)


def _get_first_present(d: dict, candidate_keys: list[str]) -> tuple[Any, int]:
    """Tries candidate_keys in order. Returns (value, attempt_number) where
    attempt_number=1 means the primary/first key worked, 2+ means a fallback
    key name was needed. Returns (None, len(candidate_keys)+1) if none present
    or all values are None."""
    for i, key in enumerate(candidate_keys, start=1):
        if key in d and d[key] is not None:
            return d[key], i
    return None, len(candidate_keys) + 1


def _nested_get(d: dict, path: list[str]) -> Any:
    """Walks a dotted path through nested dicts, e.g. ['player', 'id']."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


# ---------------------------------------------------------------------------
# Per-vendor mapping functions
# ---------------------------------------------------------------------------

def _map_opta_event(raw: dict, index: int) -> MappedEvent:
    log = []

    event_id, _ = _get_first_present(raw, ["EventId", "EventID", "Id"])
    if event_id is None:
        event_id = f"opta_unknown_{index}"
        log.append("event_id missing, generated placeholder")

    type_raw, attempt = _get_first_present(raw, ["TypeName", "Type", "QualifierName"])
    event_type = type_raw.lower() if isinstance(type_raw, str) else None
    if event_type not in CANONICAL_EVENT_TYPES:
        if event_type is not None:
            log.append(f"unrecognized event type '{type_raw}', mapped to 'other'")
        event_type = "other"
    elif attempt > 1:
        log.append(f"event_type found via fallback key (attempt {attempt})")

    minute, min_attempt = _get_first_present(raw, ["Min", "Minute"])
    second, sec_attempt = _get_first_present(raw, ["Sec", "Second"])
    if minute is not None and second is not None:
        timestamp = float(minute) * 60 + float(second)
        time_attempt = max(min_attempt, sec_attempt)
    else:
        timestamp = 0.0
        time_attempt = 99
        log.append("Min/Sec missing, timestamp defaulted to 0.0")

    period, period_attempt = _get_first_present(raw, ["PeriodId", "Period"])
    if period is None:
        log.append("period missing")
        period_attempt = 99

    team, team_attempt = _get_first_present(raw, ["TeamName", "Team"])

    player_ref, player_attempt = _get_first_present(raw, ["PlayerRef", "PlayerId", "Player_Ref"])
    if player_ref is None:
        log.append("player reference missing (null in source)")

    x, x_attempt = _get_first_present(raw, ["X"])
    y, y_attempt = _get_first_present(raw, ["Y"])
    end_x, _ = _get_first_present(raw, ["PassEndX", "EndX"])
    end_y, _ = _get_first_present(raw, ["PassEndY", "EndY"])

    outcome_raw, outcome_attempt = _get_first_present(raw, ["Outcome"])
    outcome = bool(outcome_raw) if outcome_raw is not None else None

    attempts_used = [time_attempt, period_attempt, team_attempt, player_attempt, x_attempt, y_attempt, outcome_attempt]
    mapping_attempts = 1 if all(a == 1 for a in attempts_used if a < 90) else (
        2 if any(1 < a < 90 for a in attempts_used) else 1
    )
    missing_critical = sum(1 for v in [player_ref, x, y, period] if v is None)
    confidence = max(0.3, 1.0 - 0.15 * missing_critical)

    known_keys = {"EventId", "TypeName", "Min", "Sec", "PeriodId", "PlayerRef",
                  "TeamName", "X", "Y", "PassEndX", "PassEndY", "Outcome"}
    leftover = {k: v for k, v in raw.items() if k not in known_keys}

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=timestamp, period=period, team=team,
        raw_player_ref={"vendor_id": player_ref, "jersey_number": None, "name": None},
        x=x, y=y, end_x=end_x, end_y=end_y, outcome=outcome,
        vendor_source="opta", mapping_confidence=round(confidence, 3),
        mapping_attempts=mapping_attempts, mapping_log=log, raw_metadata=leftover,
    )


def _map_statsbomb_event(raw: dict, index: int) -> MappedEvent:
    log = []

    event_id, _ = _get_first_present(raw, ["id", "event_id"])
    if event_id is None:
        event_id = f"statsbomb_unknown_{index}"
        log.append("event_id missing, generated placeholder")

    type_raw = _nested_get(raw, ["type", "name"])
    attempt = 1
    if type_raw is None:
        type_raw, attempt = _get_first_present(raw, ["type_name", "event_type"])
    event_type = type_raw.lower() if isinstance(type_raw, str) else None
    if event_type not in CANONICAL_EVENT_TYPES:
        if event_type is not None:
            log.append(f"unrecognized event type '{type_raw}', mapped to 'other'")
        event_type = "other"
    elif attempt > 1:
        log.append(f"event_type found via fallback key (attempt {attempt})")

    ts_raw, time_attempt = _get_first_present(raw, ["timestamp"])
    timestamp = 0.0
    if isinstance(ts_raw, str):
        try:
            parts = ts_raw.split(":")
            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
            timestamp = h * 3600 + m * 60 + s
        except (ValueError, IndexError):
            log.append(f"could not parse timestamp '{ts_raw}', defaulted to 0.0")
            time_attempt = 99
    else:
        log.append("timestamp missing")
        time_attempt = 99

    period, period_attempt = _get_first_present(raw, ["period"])
    if period is None:
        log.append("period missing")
        period_attempt = 99

    team = _nested_get(raw, ["team", "name"])
    team_attempt = 1 if team is not None else 99

    player_id = _nested_get(raw, ["player", "id"])
    player_name = _nested_get(raw, ["player", "name"])
    player_attempt = 1 if player_id is not None else 99
    if player_id is None:
        log.append("player reference missing (null player object in source)")

    location = raw.get("location")
    x_attempt = y_attempt = 1
    if isinstance(location, list) and len(location) == 2:
        x, y = location[0], location[1]
    else:
        x, y = None, None
        x_attempt = y_attempt = 99
        log.append("location missing or malformed")

    end_location = _nested_get(raw, ["pass", "end_location"])
    end_x, end_y = (end_location[0], end_location[1]) if isinstance(end_location, list) and len(end_location) == 2 else (None, None)

    outcome_name = _nested_get(raw, ["pass", "outcome", "name"])
    outcome = (outcome_name == "Complete") if outcome_name is not None else None
    outcome_attempt = 1

    attempts_used = [time_attempt, period_attempt, team_attempt, player_attempt, x_attempt, y_attempt]
    mapping_attempts = 1 if all(a == 1 for a in attempts_used) else 2
    missing_critical = sum(1 for v in [player_id, x, y, period] if v is None)
    confidence = max(0.3, 1.0 - 0.15 * missing_critical)

    known_keys = {"id", "type", "timestamp", "period", "player", "team", "location", "pass"}
    leftover = {k: v for k, v in raw.items() if k not in known_keys}

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=timestamp, period=period, team=team,
        raw_player_ref={"vendor_id": player_id, "jersey_number": None, "name": player_name},
        x=x, y=y, end_x=end_x, end_y=end_y, outcome=outcome,
        vendor_source="statsbomb", mapping_confidence=round(confidence, 3),
        mapping_attempts=mapping_attempts, mapping_log=log, raw_metadata=leftover,
    )


def _map_wyscout_event(raw: dict, index: int) -> MappedEvent:
    log = []

    event_id, _ = _get_first_present(raw, ["eventId", "wyId", "id"])
    if event_id is None:
        event_id = f"wyscout_unknown_{index}"
        log.append("event_id missing, generated placeholder")

    type_raw, attempt = _get_first_present(raw, ["subEventName", "eventName"])
    event_type = type_raw.lower() if isinstance(type_raw, str) else None
    if event_type not in CANONICAL_EVENT_TYPES:
        if event_type is not None:
            log.append(f"unrecognized event type '{type_raw}', mapped to 'other'")
        else:
            log.append("event type missing (null in source)")
        event_type = "other"
    elif attempt > 1:
        log.append(f"event_type found via fallback key (attempt {attempt})")

    timestamp, time_attempt = _get_first_present(raw, ["eventSec"])
    if timestamp is None:
        timestamp = 0.0
        log.append("eventSec missing, timestamp defaulted to 0.0")

    period_raw, period_attempt = _get_first_present(raw, ["matchPeriod"])
    period = None
    if isinstance(period_raw, str) and period_raw.endswith("H"):
        try:
            period = int(period_raw[0])
        except ValueError:
            log.append(f"could not parse matchPeriod '{period_raw}'")
            period_attempt = 99
    else:
        log.append("matchPeriod missing")
        period_attempt = 99

    team, team_attempt = _get_first_present(raw, ["teamName", "team"])

    player_ref, player_attempt = _get_first_present(raw, ["trackable_object", "playerId"])
    jersey, jersey_attempt = _get_first_present(raw, ["jerseyNumber"])
    if player_ref is None and jersey is None:
        log.append("both player identifiers missing (trackable_object AND jerseyNumber null) — worst case for entity resolution")
        player_attempt = 99

    positions = raw.get("positions")
    x = y = end_x = end_y = None
    x_attempt = y_attempt = 1
    if isinstance(positions, list) and len(positions) >= 1:
        x, y = positions[0].get("x"), positions[0].get("y")
        if len(positions) >= 2:
            end_x, end_y = positions[1].get("x"), positions[1].get("y")
    else:
        x_attempt = y_attempt = 99
        log.append("positions missing or empty")

    outcome, outcome_attempt = _get_first_present(raw, ["accurate"])

    attempts_used = [period_attempt, team_attempt, player_attempt, x_attempt, y_attempt]
    mapping_attempts = 1 if all(a == 1 for a in attempts_used) else 2
    missing_critical = sum(1 for v in [player_ref, jersey, x, y, period] if v is None)
    confidence = max(0.3, 1.0 - 0.12 * missing_critical)

    known_keys = {"eventId", "subEventName", "matchPeriod", "eventSec",
                  "trackable_object", "jerseyNumber", "teamName", "positions", "accurate"}
    leftover = {k: v for k, v in raw.items() if k not in known_keys}

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=float(timestamp), period=period, team=team,
        raw_player_ref={"vendor_id": player_ref, "jersey_number": jersey, "name": None},
        x=x, y=y, end_x=end_x, end_y=end_y,
        outcome=bool(outcome) if outcome is not None else None,
        vendor_source="wyscout", mapping_confidence=round(confidence, 3),
        mapping_attempts=mapping_attempts, mapping_log=log, raw_metadata=leftover,
    )


def _map_generic_event(raw: dict, index: int) -> MappedEvent:
    """Fallback mapper for a vendor Format Detective couldn't identify.
    Best-effort key-name sniffing across common patterns seen in all known
    vendors. Confidence is capped low — this is explicitly a degraded path."""
    log = ["unknown vendor: using generic best-effort field sniffing"]

    def sniff(*candidates: str) -> Any:
        for c in candidates:
            for k in raw.keys():
                if k.lower() == c.lower():
                    return raw[k]
        return None

    event_id = sniff("id", "eventId", "EventId") or f"unknown_vendor_{index}"
    type_raw = sniff("type", "TypeName", "subEventName", "eventName")
    event_type = type_raw.lower() if isinstance(type_raw, str) else "other"
    if event_type not in CANONICAL_EVENT_TYPES:
        event_type = "other"

    x = sniff("x", "X")
    y = sniff("y", "Y")
    if x is None or y is None:
        log.append("could not locate x/y coordinates via generic sniffing")

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=float(sniff("timestamp", "eventSec", "Sec") or 0.0),
        period=sniff("period", "PeriodId"),
        team=sniff("team", "TeamName", "teamName"),
        raw_player_ref={"vendor_id": sniff("player", "PlayerRef", "trackable_object"), "jersey_number": None, "name": None},
        x=x, y=y, end_x=None, end_y=None, outcome=None,
        vendor_source="unknown", mapping_confidence=0.4,
        mapping_attempts=3, mapping_log=log, raw_metadata=raw,
    )


VENDOR_MAPPERS: dict[str, Callable[[dict, int], MappedEvent]] = {
    "opta": _map_opta_event,
    "statsbomb": _map_statsbomb_event,
    "wyscout": _map_wyscout_event,
    "unknown": _map_generic_event,
}


class SchemaMapper:
    """Agent 2. Registry-driven so adding a new vendor is adding one entry
    to VENDOR_MAPPERS, not editing this class."""

    def map_events(self, data: dict, detected_format: str, event_list_key: str) -> list[MappedEvent]:
        mapper_fn = VENDOR_MAPPERS.get(detected_format, _map_generic_event)
        events = data.get(event_list_key, []) if event_list_key else []
        return [mapper_fn(raw, i) for i, raw in enumerate(events) if isinstance(raw, dict)]


if __name__ == "__main__":
    import json
    from pathlib import Path
    import sys
    sys.path.insert(0, "backend/agents")
    from format_detective import FormatDetective

    detective = FormatDetective()
    mapper = SchemaMapper()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Schema Mapper — test run (Format Detective -> Schema Mapper pipeline)")
    print("=" * 78)

    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        for vendor_file in sorted(match_dir.glob("*.json")):
            data = json.loads(vendor_file.read_text())
            detection = detective.detect(data)
            mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)

            avg_conf = sum(e.mapping_confidence for e in mapped) / len(mapped) if mapped else 0
            missing_player = sum(1 for e in mapped if e.raw_player_ref["vendor_id"] is None and e.raw_player_ref["jersey_number"] is None)
            missing_coords = sum(1 for e in mapped if e.x is None or e.y is None)
            other_type = sum(1 for e in mapped if e.event_type == "other")
            needed_fallback = sum(1 for e in mapped if e.mapping_attempts > 1)

            print(f"\n{match_dir.name}/{vendor_file.name}  (detected={detection.detected_format})")
            print(f"  events mapped: {len(mapped)} | avg confidence: {avg_conf:.3f}")
            print(f"  missing player ref: {missing_player} | missing coords: {missing_coords} | "
                  f"unmapped event_type->other: {other_type} | needed fallback keys: {needed_fallback}")

    # Show one fully-logged example from the messy wyscout file (worst-case player loss)
    print("\n" + "=" * 78)
    print("Detailed example: Wyscout messy-tier event with both player IDs missing")
    print("=" * 78)
    data = json.loads((sample_root / "PL_2025_MESSY_001" / "wyscout.json").read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    worst = next((e for e in mapped if e.raw_player_ref["vendor_id"] is None and e.raw_player_ref["jersey_number"] is None), None)
    if worst:
        print(json.dumps({
            "event_id": worst.event_id, "event_type": worst.event_type,
            "raw_player_ref": worst.raw_player_ref, "mapping_confidence": worst.mapping_confidence,
            "mapping_attempts": worst.mapping_attempts, "mapping_log": worst.mapping_log,
        }, indent=2))
