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
import re
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
    # Event detail, already in CDF vocabulary (cdf_models.CardType /
    # ShotOutcome). None means the vendor record carried no such detail.
    card_type: Optional[str] = None
    shot_outcome: Optional[str] = None


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
# Vendor vocabularies: event-type aliases, card colours, shot results.
#
# Each vendor names events its own way and encodes card colour and shot
# result differently. These tables are the mapper's knowledge of each; the
# values are CDF vocabulary. Anything not listed still falls through to
# "other" with a log line, never a guess.
# ---------------------------------------------------------------------------

# Opta reports each shot result as its own event type (Miss, Post, Attempt
# Saved, Goal) rather than one "shot" type plus an outcome field, and puts
# card colour in a qualifier on the Card event.
OPTA_SHOT_TYPES: dict[str, tuple[str, str]] = {
    "miss": ("shot", "off_target"),
    "post": ("shot", "post"),
    "attempt_saved": ("shot", "saved"),
    "goal": ("goal", "goal"),
}
OPTA_BLOCKED_QUALIFIER = 82          # on an Attempt Saved: blocked by an outfield player, not the keeper
OPTA_CARD_QUALIFIERS = {31: "yellow", 32: "second_yellow", 33: "red"}
OPTA_TYPE_ALIASES = {"throwin": "throw_in", "goalkick": "goal_kick", "take_on": "dribble"}

# StatsBomb: one "Shot" type with shot.outcome, goals included (a goal is a
# Shot whose outcome is "Goal"); cards ride on Foul Committed
# (foul_committed.card) or Bad Behaviour (bad_behaviour.card); corners,
# throw-ins and goal kicks are Passes with pass.type set.
STATSBOMB_SHOT_OUTCOMES = {
    "goal": "goal", "saved": "saved", "saved to post": "saved",
    "off t": "off_target", "wayward": "off_target", "saved off target": "off_target",
    "blocked": "blocked", "post": "post",
}
STATSBOMB_CARDS = {"yellow card": "yellow", "second yellow": "second_yellow", "red card": "red"}
STATSBOMB_TYPE_ALIASES = {
    "foul committed": "foul", "bad behaviour": "card", "duel": "tackle",
    "goal keeper": "save", "throw in": "throw_in", "goal kick": "goal_kick",
}
STATSBOMB_PASS_TYPES = {"corner": "corner", "throw-in": "throw_in", "goal kick": "goal_kick"}
STATSBOMB_OUTCOME_TRUE = {"complete", "won", "success", "success in play", "success to team", "success out"}
STATSBOMB_OUTCOME_FALSE = {"incomplete", "lost", "lost in play", "lost out", "out", "unknown", "pass offside"}

# Wyscout (v2): descriptive subEventNames, and result detail as numeric tags.
WYSCOUT_TYPE_ALIASES = {
    "simple pass": "pass", "high pass": "pass", "cross": "pass", "launch": "pass",
    "smart pass": "pass", "head pass": "pass", "hand pass": "pass",
    "free kick": "pass", "free kick cross": "pass",
    "free kick shot": "shot", "penalty": "shot",
    "hand foul": "foul", "late card foul": "foul", "out of game foul": "foul",
    "protest": "foul", "simulation": "foul", "time lost foul": "foul", "violent foul": "foul",
    "ground defending duel": "tackle", "ground attacking duel": "dribble",
    "throw in": "throw_in", "goal kick": "goal_kick",
    "save attempt": "save", "reflexes": "save",
}
WYSCOUT_TAG_GOAL = 101
WYSCOUT_TAG_BLOCKED = 2101
WYSCOUT_TAG_ACCURATE = 1801
WYSCOUT_TAG_NOT_ACCURATE = 1802
WYSCOUT_CARD_TAGS = {1702: "yellow", 1703: "second_yellow", 1701: "red"}


def _resolve_event_type(type_raw: Any, aliases: dict[str, str]) -> Optional[str]:
    """Canonical CDF event type for a vendor type name, or None if unknown.
    Tries the name as-is, then with spaces/hyphens as underscores
    ("Throw-in" -> "throw_in"), then the vendor's alias table."""
    if not isinstance(type_raw, str):
        return None
    lower = type_raw.strip().lower()
    normalized = re.sub(r"[\s\-]+", "_", lower)
    for candidate in (lower, normalized):
        if candidate in CANONICAL_EVENT_TYPES:
            return candidate
    return aliases.get(lower) or aliases.get(normalized)


def _ids_from(items: Any, key: str) -> set[int]:
    """Numeric ids from a list of {key: n} dicts or bare ints (Opta
    qualifiers, Wyscout tags). Anything unparseable is skipped."""
    ids: set[int] = set()
    if not isinstance(items, list):
        return ids
    for item in items:
        value = item.get(key) if isinstance(item, dict) else item
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return ids


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
    qualifier_ids = _ids_from(raw.get("Qualifiers"), "QualifierId")
    shot_outcome = None
    type_key = re.sub(r"[\s\-]+", "_", type_raw.strip().lower()) if isinstance(type_raw, str) else None
    if type_key in OPTA_SHOT_TYPES:
        event_type, shot_outcome = OPTA_SHOT_TYPES[type_key]
        if shot_outcome == "saved" and OPTA_BLOCKED_QUALIFIER in qualifier_ids:
            shot_outcome = "blocked"
    else:
        event_type = _resolve_event_type(type_raw, OPTA_TYPE_ALIASES)
    if event_type is None:
        if type_raw is not None:
            log.append(f"unrecognized event type '{type_raw}', mapped to 'other'")
        event_type = "other"
    elif attempt > 1:
        log.append(f"event_type found via fallback key (attempt {attempt})")
    card_type = next((OPTA_CARD_QUALIFIERS[q] for q in sorted(qualifier_ids) if q in OPTA_CARD_QUALIFIERS), None)

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
    jersey, _ = _get_first_present(raw, ["JerseyNumber", "ShirtNumber"])
    if player_ref is None:
        log.append("player reference missing (null in source)")

    x, x_attempt = _get_first_present(raw, ["X"])
    y, y_attempt = _get_first_present(raw, ["Y"])
    end_x, _ = _get_first_present(raw, ["PassEndX", "EndX"])
    end_y, _ = _get_first_present(raw, ["PassEndY", "EndY"])

    outcome_raw, outcome_attempt = _get_first_present(raw, ["Outcome"])
    outcome = bool(outcome_raw) if outcome_raw is not None else None
    if outcome is None and shot_outcome is not None:
        outcome = shot_outcome in ("goal", "saved")

    attempts_used = [time_attempt, period_attempt, team_attempt, player_attempt, x_attempt, y_attempt, outcome_attempt]
    mapping_attempts = 1 if all(a == 1 for a in attempts_used if a < 90) else (
        2 if any(1 < a < 90 for a in attempts_used) else 1
    )
    missing_critical = sum(1 for v in [player_ref, x, y, period] if v is None)
    confidence = max(0.3, 1.0 - 0.15 * missing_critical)

    known_keys = {"EventId", "TypeName", "Min", "Sec", "PeriodId", "PlayerRef", "JerseyNumber",
                  "TeamName", "X", "Y", "PassEndX", "PassEndY", "Outcome", "Qualifiers"}
    leftover = {k: v for k, v in raw.items() if k not in known_keys}

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=timestamp, period=period, team=team,
        raw_player_ref={"vendor_id": player_ref, "jersey_number": jersey, "name": None},
        x=x, y=y, end_x=end_x, end_y=end_y, outcome=outcome,
        vendor_source="opta", mapping_confidence=round(confidence, 3),
        mapping_attempts=mapping_attempts, mapping_log=log, raw_metadata=leftover,
        card_type=card_type, shot_outcome=shot_outcome,
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
    event_type = _resolve_event_type(type_raw, STATSBOMB_TYPE_ALIASES)
    if event_type is None:
        if type_raw is not None:
            log.append(f"unrecognized event type '{type_raw}', mapped to 'other'")
        event_type = "other"
    elif attempt > 1:
        log.append(f"event_type found via fallback key (attempt {attempt})")

    # Set pieces are Passes with a pass.type in StatsBomb.
    pass_type = _nested_get(raw, ["pass", "type", "name"])
    if event_type == "pass" and isinstance(pass_type, str):
        event_type = STATSBOMB_PASS_TYPES.get(pass_type.strip().lower(), event_type)

    # A goal is a Shot whose outcome is "Goal".
    shot_outcome = None
    shot_outcome_name = _nested_get(raw, ["shot", "outcome", "name"])
    if isinstance(shot_outcome_name, str):
        shot_outcome = STATSBOMB_SHOT_OUTCOMES.get(shot_outcome_name.strip().lower())
        if shot_outcome is None:
            log.append(f"unrecognized shot outcome '{shot_outcome_name}', left unset")
    if event_type == "shot" and shot_outcome == "goal":
        event_type = "goal"
    elif event_type == "goal":
        shot_outcome = "goal"

    card_name = (_nested_get(raw, ["foul_committed", "card", "name"])
                 or _nested_get(raw, ["bad_behaviour", "card", "name"]))
    card_type = STATSBOMB_CARDS.get(card_name.strip().lower()) if isinstance(card_name, str) else None

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
    jersey = _nested_get(raw, ["player", "jersey_number"])
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

    end_location = _nested_get(raw, ["pass", "end_location"]) or _nested_get(raw, ["shot", "end_location"])
    # Shot end locations can carry a third (height) value; only x/y map to the CDF.
    end_x, end_y = (end_location[0], end_location[1]) if isinstance(end_location, list) and len(end_location) >= 2 else (None, None)

    if shot_outcome is not None:
        outcome = shot_outcome in ("goal", "saved")
    else:
        outcome = None
        for obj_key in ("pass", "dribble", "duel", "interception"):
            obj = raw.get(obj_key)
            if not isinstance(obj, dict):
                continue
            name = _nested_get(obj, ["outcome", "name"])
            if isinstance(name, str):
                lowered = name.strip().lower()
                if lowered in STATSBOMB_OUTCOME_TRUE:
                    outcome = True
                elif lowered in STATSBOMB_OUTCOME_FALSE:
                    outcome = False
            elif obj_key == "pass":
                # StatsBomb leaves outcome off a completed pass entirely.
                outcome = True
            break
    outcome_attempt = 1

    attempts_used = [time_attempt, period_attempt, team_attempt, player_attempt, x_attempt, y_attempt]
    mapping_attempts = 1 if all(a == 1 for a in attempts_used) else 2
    missing_critical = sum(1 for v in [player_id, x, y, period] if v is None)
    confidence = max(0.3, 1.0 - 0.15 * missing_critical)

    known_keys = {"id", "type", "timestamp", "period", "player", "team", "location", "pass",
                  "shot", "foul_committed", "bad_behaviour", "duel", "dribble", "interception"}
    leftover = {k: v for k, v in raw.items() if k not in known_keys}

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=timestamp, period=period, team=team,
        raw_player_ref={"vendor_id": player_id, "jersey_number": jersey, "name": player_name},
        x=x, y=y, end_x=end_x, end_y=end_y, outcome=outcome,
        vendor_source="statsbomb", mapping_confidence=round(confidence, 3),
        mapping_attempts=mapping_attempts, mapping_log=log, raw_metadata=leftover,
        card_type=card_type, shot_outcome=shot_outcome,
    )


def _map_wyscout_event(raw: dict, index: int) -> MappedEvent:
    log = []

    event_id, _ = _get_first_present(raw, ["eventId", "wyId", "id"])
    if event_id is None:
        event_id = f"wyscout_unknown_{index}"
        log.append("event_id missing, generated placeholder")

    type_raw, attempt = _get_first_present(raw, ["subEventName", "eventName"])
    event_type = _resolve_event_type(type_raw, WYSCOUT_TYPE_ALIASES)
    if event_type is None:
        if type_raw is not None:
            log.append(f"unrecognized event type '{type_raw}', mapped to 'other'")
        else:
            log.append("event type missing (null in source)")
        event_type = "other"
    elif attempt > 1:
        log.append(f"event_type found via fallback key (attempt {attempt})")

    tags = _ids_from(raw.get("tags"), "id")
    shot_outcome = None
    if event_type in ("shot", "goal"):
        if WYSCOUT_TAG_GOAL in tags or event_type == "goal":
            event_type, shot_outcome = "goal", "goal"
        elif WYSCOUT_TAG_BLOCKED in tags:
            shot_outcome = "blocked"
        elif WYSCOUT_TAG_ACCURATE in tags:
            shot_outcome = "saved"
        elif WYSCOUT_TAG_NOT_ACCURATE in tags:
            shot_outcome = "off_target"
    card_type = next((WYSCOUT_CARD_TAGS[t] for t in sorted(tags) if t in WYSCOUT_CARD_TAGS), None)

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
    if outcome is None:
        if shot_outcome is not None:
            outcome = shot_outcome in ("goal", "saved")
        elif WYSCOUT_TAG_ACCURATE in tags:
            outcome = True
        elif WYSCOUT_TAG_NOT_ACCURATE in tags:
            outcome = False

    attempts_used = [period_attempt, team_attempt, player_attempt, x_attempt, y_attempt]
    mapping_attempts = 1 if all(a == 1 for a in attempts_used) else 2
    missing_critical = sum(1 for v in [player_ref, jersey, x, y, period] if v is None)
    confidence = max(0.3, 1.0 - 0.12 * missing_critical)

    known_keys = {"eventId", "subEventName", "matchPeriod", "eventSec",
                  "trackable_object", "jerseyNumber", "teamName", "positions", "accurate", "tags"}
    leftover = {k: v for k, v in raw.items() if k not in known_keys}

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=float(timestamp), period=period, team=team,
        raw_player_ref={"vendor_id": player_ref, "jersey_number": jersey, "name": None},
        x=x, y=y, end_x=end_x, end_y=end_y,
        outcome=bool(outcome) if outcome is not None else None,
        vendor_source="wyscout", mapping_confidence=round(confidence, 3),
        mapping_attempts=mapping_attempts, mapping_log=log, raw_metadata=leftover,
        card_type=card_type, shot_outcome=shot_outcome,
    )


def _safe_float(value: Any, log: list[str], field_label: str) -> float:
    """Best-effort numeric coercion for the generic/unknown-vendor path,
    where the sniffed value's shape is a total unknown -- could be a plain
    number, a numeric string, a timestamp string ("00:00:01.087", as real
    StatsBomb event files use), a nested dict, or missing entirely. Never
    raises: falls back to 0.0 and logs it, same honest-degradation rule as
    every other fallback in this pipeline."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        # "HH:MM:SS.ms"-shaped timestamp string -- try that before giving up.
        parts = value.split(":")
        if len(parts) == 3:
            try:
                h, m, s = (float(p) for p in parts)
                return h * 3600 + m * 60 + s
            except ValueError:
                pass
    log.append(f"could not coerce {field_label}={value!r} to a number via generic sniffing, defaulted to 0.0")
    return 0.0


def _safe_str(value: Any) -> Optional[str]:
    """Generic path's sniffed fields can turn out to be a nested dict
    (e.g. a vendor's team/type objects) rather than the plain string every
    downstream model field expects. Returns None rather than handing a
    dict to a str field and letting Validator drop the event over it --
    still logged there as a dropped event if it matters, this just avoids
    masking an honest 'missing' as a malformed value."""
    return value if isinstance(value, str) else None


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

    x_raw = sniff("x", "X")
    y_raw = sniff("y", "Y")
    x = x_raw if isinstance(x_raw, (int, float)) else None
    y = y_raw if isinstance(y_raw, (int, float)) else None
    if x is None or y is None:
        log.append("could not locate x/y coordinates via generic sniffing")

    return MappedEvent(
        event_id=str(event_id), event_type=event_type, event_type_raw=type_raw,
        timestamp=_safe_float(sniff("timestamp", "eventSec", "Sec"), log, "timestamp"),
        period=sniff("period", "PeriodId"),
        team=_safe_str(sniff("team", "TeamName", "teamName")),
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


# ---------------------------------------------------------------------------
# Match-sheet mapping (lineups, formations, subs, officials, venue).
#
# A separate, parallel path from event mapping above, not a branch inside
# it -- match sheets are a structurally distinct feed (real vendors ship
# them separately from the event stream too), so Format Detective's
# event-shaped signatures don't apply here. Detection is its own lightweight
# top-level-key sniff (_detect_match_sheet_vendor), and mapping follows the
# same fallback-field-resolution pattern (_get_first_present/_nested_get)
# as the event mappers above.
# ---------------------------------------------------------------------------

@dataclass
class MappedLineupEntry:
    name: Optional[str]
    jersey_number: Optional[int]
    position: Optional[str]
    is_starter: bool
    is_captain: bool
    raw_player_ref: dict = field(default_factory=dict)


@dataclass
class MappedSubstitution:
    team: Optional[str]
    player_out_ref: dict
    player_in_ref: dict
    minute: Optional[int]
    period: int
    reason: Optional[str] = None


@dataclass
class MappedMatchSheet:
    home_team: Optional[str]
    away_team: Optional[str]
    home_formation: Optional[str]
    away_formation: Optional[str]
    home_lineup: list[MappedLineupEntry]
    away_lineup: list[MappedLineupEntry]
    substitutions: list[MappedSubstitution]
    referee: Optional[str]
    assistant_referees: list[str]
    fourth_official: Optional[str]
    var_official: Optional[str]
    venue: Optional[str]
    attendance: Optional[int]
    weather: Optional[str]
    kickoff_time: Optional[str]
    vendor_source: str
    mapping_confidence: float
    mapping_log: list[str] = field(default_factory=list)


def _detect_match_sheet_vendor(data: dict) -> str:
    """Top-level-key sniff for which vendor's match-sheet shape this is.
    Separate from FormatDetective's event-signature matching -- these
    documents don't have an event list to score against."""
    if not isinstance(data, dict):
        return "unknown"
    keys = {k for k in data.keys() if not k.startswith("_")}
    if {"TeamData", "MatchOfficial"}.issubset(keys):
        return "opta"
    home = data.get("home_team")
    if {"home_team", "away_team"}.issubset(keys) and isinstance(home, dict) and "lineup" in home:
        return "statsbomb"
    teams = data.get("teams")
    if "teams" in keys and isinstance(teams, dict) and {"home", "away"}.issubset(teams.keys()):
        return "wyscout"
    return "unknown"


def _map_opta_match_sheet(data: dict) -> MappedMatchSheet:
    log = []
    team_data = data.get("TeamData") or []
    if len(team_data) < 2:
        log.append("TeamData missing or incomplete -- expected 2 teams")
    home_block = team_data[0] if len(team_data) > 0 else {}
    away_block = team_data[1] if len(team_data) > 1 else {}

    def map_squad(block: dict) -> list[MappedLineupEntry]:
        entries = []
        for p in (block.get("MatchPlayer") or []):
            pid, _ = _get_first_present(p, ["PlayerRef", "PlayerId"])
            name, _ = _get_first_present(p, ["PlayerName", "Name"])
            jersey, _ = _get_first_present(p, ["ShirtNumber", "JerseyNumber"])
            position, _ = _get_first_present(p, ["Position"])
            status, _ = _get_first_present(p, ["Status"])
            captain, _ = _get_first_present(p, ["Captain"])
            entries.append(MappedLineupEntry(
                name=name, jersey_number=jersey, position=position,
                is_starter=(status != "Sub") if status is not None else True,
                is_captain=bool(captain),
                raw_player_ref={"vendor_id": pid, "jersey_number": jersey, "name": name},
            ))
        return entries

    home_team, _ = _get_first_present(home_block, ["TeamName"])
    away_team, _ = _get_first_present(away_block, ["TeamName"])
    home_formation, _ = _get_first_present(home_block, ["Formation"])
    away_formation, _ = _get_first_present(away_block, ["Formation"])

    subs = []
    for s in (data.get("Substitution") or []):
        out_ref, _ = _get_first_present(s, ["PlayerOffRef"])
        in_ref, _ = _get_first_present(s, ["PlayerOnRef"])
        minute, _ = _get_first_present(s, ["Min", "Minute"])
        period, _ = _get_first_present(s, ["PeriodId", "Period"])
        team, _ = _get_first_present(s, ["TeamName"])
        subs.append(MappedSubstitution(
            team=team, player_out_ref={"vendor_id": out_ref}, player_in_ref={"vendor_id": in_ref},
            minute=minute, period=period or 2,
        ))

    officials = data.get("MatchOfficial") or []

    def official_name(role: str) -> Optional[str]:
        return next((o.get("Name") for o in officials if o.get("Role") == role), None)

    venue, _ = _get_first_present(data.get("Venue") or {}, ["Name"])
    attendance, _ = _get_first_present(data, ["Attendance"])
    weather, _ = _get_first_present(data, ["Weather"])
    kickoff, _ = _get_first_present(data, ["KickOffTime"])

    missing = sum(1 for v in [home_team, away_team, venue] if v is None)
    confidence = max(0.3, 1.0 - 0.2 * missing)
    if missing:
        log.append(f"{missing} top-level field(s) missing (team names / venue)")

    return MappedMatchSheet(
        home_team=home_team, away_team=away_team,
        home_formation=home_formation, away_formation=away_formation,
        home_lineup=map_squad(home_block), away_lineup=map_squad(away_block),
        substitutions=subs, referee=official_name("Main"),
        assistant_referees=[n for n in (official_name("Assistant1"), official_name("Assistant2")) if n],
        fourth_official=official_name("Fourth"), var_official=official_name("VAR"),
        venue=venue, attendance=attendance, weather=weather, kickoff_time=kickoff,
        vendor_source="opta", mapping_confidence=round(confidence, 3), mapping_log=log,
    )


def _map_statsbomb_match_sheet(data: dict) -> MappedMatchSheet:
    log = []
    home_block = data.get("home_team") or {}
    away_block = data.get("away_team") or {}

    def map_squad(block: dict) -> list[MappedLineupEntry]:
        entries = []
        for p in (block.get("lineup") or []):
            pid, _ = _get_first_present(p, ["player_id"])
            name, _ = _get_first_present(p, ["player_name"])
            jersey, _ = _get_first_present(p, ["jersey_number"])
            position, _ = _get_first_present(p, ["position_name"])
            starter, _ = _get_first_present(p, ["starter"])
            captain, _ = _get_first_present(p, ["captain"])
            entries.append(MappedLineupEntry(
                name=name, jersey_number=jersey, position=position,
                is_starter=bool(starter) if starter is not None else True,
                is_captain=bool(captain),
                raw_player_ref={"vendor_id": pid, "jersey_number": jersey, "name": name},
            ))
        return entries

    home_team, _ = _get_first_present(home_block, ["team_name"])
    away_team, _ = _get_first_present(away_block, ["team_name"])
    home_formation, _ = _get_first_present(home_block, ["formation"])
    away_formation, _ = _get_first_present(away_block, ["formation"])

    subs = []
    for s in (data.get("substitutions") or []):
        team, _ = _get_first_present(s, ["team"])
        out_name, _ = _get_first_present(s, ["player_out"])
        in_name, _ = _get_first_present(s, ["player_in"])
        minute, _ = _get_first_present(s, ["minute"])
        period, _ = _get_first_present(s, ["period"])
        reason, _ = _get_first_present(s, ["reason"])
        subs.append(MappedSubstitution(
            team=team, player_out_ref={"name": out_name}, player_in_ref={"name": in_name},
            minute=minute, period=period or 2, reason=reason,
        ))

    referee = _nested_get(data, ["referee", "name"])
    officials_block = data.get("officials") or {}
    assistants = officials_block.get("assistants") or []
    fourth = officials_block.get("fourth_official")
    var = officials_block.get("var")

    venue = _nested_get(data, ["stadium", "name"])
    attendance, _ = _get_first_present(data, ["attendance"])
    weather, _ = _get_first_present(data, ["weather"])
    kickoff, _ = _get_first_present(data, ["kick_off"])

    missing = sum(1 for v in [home_team, away_team, venue] if v is None)
    confidence = max(0.3, 1.0 - 0.2 * missing)
    if missing:
        log.append(f"{missing} top-level field(s) missing (team names / venue)")

    return MappedMatchSheet(
        home_team=home_team, away_team=away_team,
        home_formation=home_formation, away_formation=away_formation,
        home_lineup=map_squad(home_block), away_lineup=map_squad(away_block),
        substitutions=subs, referee=referee, assistant_referees=list(assistants),
        fourth_official=fourth, var_official=var,
        venue=venue, attendance=attendance, weather=weather, kickoff_time=kickoff,
        vendor_source="statsbomb", mapping_confidence=round(confidence, 3), mapping_log=log,
    )


def _map_wyscout_match_sheet(data: dict) -> MappedMatchSheet:
    log = []
    teams = data.get("teams") or {}
    home_block = teams.get("home") or {}
    away_block = teams.get("away") or {}

    def map_squad(block: dict) -> list[MappedLineupEntry]:
        entries = []
        for p in (block.get("lineups") or []):
            pid, _ = _get_first_present(p, ["playerId"])
            name, _ = _get_first_present(p, ["shortName"])
            jersey, _ = _get_first_present(p, ["shirtNumber"])
            position, _ = _get_first_present(p, ["role"])
            bench, _ = _get_first_present(p, ["bench"])
            entries.append(MappedLineupEntry(
                name=name, jersey_number=jersey, position=position,
                is_starter=not bool(bench) if bench is not None else True,
                is_captain=False,
                raw_player_ref={"vendor_id": pid, "jersey_number": jersey, "name": name},
            ))
        return entries

    home_team, _ = _get_first_present(home_block, ["teamName"])
    away_team, _ = _get_first_present(away_block, ["teamName"])
    home_formation, _ = _get_first_present(home_block, ["formation"])
    away_formation, _ = _get_first_present(away_block, ["formation"])
    if home_formation is None:
        log.append("home formation missing (null in source)")

    subs = []
    for s in (data.get("substitutions") or []):
        team, _ = _get_first_present(s, ["teamName"])
        out_name, _ = _get_first_present(s, ["playerOut"])
        in_name, _ = _get_first_present(s, ["playerIn"])
        minute, _ = _get_first_present(s, ["minute"])
        subs.append(MappedSubstitution(
            team=team, player_out_ref={"name": out_name}, player_in_ref={"name": in_name},
            minute=minute, period=2,
        ))

    referees = data.get("referees") or []

    def official_name(role: str) -> Optional[str]:
        return next((r.get("name") for r in referees if r.get("role") == role), None)

    assistants = [r.get("name") for r in referees if r.get("role") == "assistant" and r.get("name")]

    venue = _nested_get(data, ["venue", "name"])
    attendance, _ = _get_first_present(data, ["attendance"])
    weather, _ = _get_first_present(data, ["weatherConditions"])
    kickoff, _ = _get_first_present(data, ["kickOffTime"])

    missing = sum(1 for v in [home_team, away_team, venue] if v is None)
    confidence = max(0.3, 1.0 - 0.2 * missing)
    if missing:
        log.append(f"{missing} top-level field(s) missing (team names / venue)")

    return MappedMatchSheet(
        home_team=home_team, away_team=away_team,
        home_formation=home_formation, away_formation=away_formation,
        home_lineup=map_squad(home_block), away_lineup=map_squad(away_block),
        substitutions=subs, referee=official_name("referee"), assistant_referees=assistants,
        fourth_official=official_name("fourthOfficial"), var_official=official_name("var"),
        venue=venue, attendance=attendance, weather=weather, kickoff_time=kickoff,
        vendor_source="wyscout", mapping_confidence=round(confidence, 3), mapping_log=log,
    )


def _map_generic_match_sheet(data: dict) -> MappedMatchSheet:
    """Fallback for an unrecognized match-sheet shape -- same honest-
    degradation philosophy as _map_generic_event: never crash, return a
    low-confidence, mostly-empty structure with the reason logged."""
    return MappedMatchSheet(
        home_team=None, away_team=None, home_formation=None, away_formation=None,
        home_lineup=[], away_lineup=[], substitutions=[],
        referee=None, assistant_referees=[], fourth_official=None, var_official=None,
        venue=None, attendance=None, weather=None, kickoff_time=None,
        vendor_source="unknown", mapping_confidence=0.0,
        mapping_log=["unrecognized match-sheet shape: no vendor signature matched, returning empty structure"],
    )


MATCH_SHEET_MAPPERS: dict[str, Callable[[dict], MappedMatchSheet]] = {
    "opta": _map_opta_match_sheet,
    "statsbomb": _map_statsbomb_match_sheet,
    "wyscout": _map_wyscout_match_sheet,
    "unknown": _map_generic_match_sheet,
}


class SchemaMapper:
    """Agent 2. Registry-driven so adding a new vendor is adding one entry
    to VENDOR_MAPPERS, not editing this class."""

    def map_events(self, data: dict, detected_format: str, event_list_key: str) -> list[MappedEvent]:
        mapper_fn = VENDOR_MAPPERS.get(detected_format, _map_generic_event)
        events = data.get(event_list_key, []) if event_list_key else []
        return [mapper_fn(raw, i) for i, raw in enumerate(events) if isinstance(raw, dict)]

    def map_match_sheet(self, data: dict) -> MappedMatchSheet:
        """Separate entry point from map_events -- match-sheet payloads are
        detected and mapped independently (see _detect_match_sheet_vendor)."""
        if not isinstance(data, dict):
            return _map_generic_match_sheet({})
        vendor = _detect_match_sheet_vendor(data)
        mapper_fn = MATCH_SHEET_MAPPERS.get(vendor, _map_generic_match_sheet)
        return mapper_fn(data)


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
