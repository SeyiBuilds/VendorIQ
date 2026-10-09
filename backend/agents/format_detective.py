"""
Agent 1: Format Detective

Identifies which vendor format a raw match data file is in, before any
other agent touches it. Everything downstream (Schema Mapper, Coordinate
Translator, Entity Resolver) depends on knowing which vendor's field names
and shapes it's looking at.

Per PRD Section 4.1, fallback logic is:
  Attempt 1: strict signature match on known vendor field sets
  Attempt 2: fuzzy/partial match, scored, pick best candidate
  Fallback:  assume generic JSON, locate the most plausible event list,
             flag vendor as "unknown"

Important: detection must work from structure alone. The synthetic data
generator stamps a "_vendor_format" key into its own output for testing/
scoring purposes only — this agent explicitly ignores any key starting
with "_" so it can't cheat off that label. In production, real vendor
files obviously won't have it either.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Known vendor signatures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VendorSignature:
    vendor: str
    top_level_keys: frozenset[str]      # keys expected at the document root
    event_list_key: str                 # which top-level key holds the events array
    event_fields: frozenset[str]        # keys expected on each event object


VENDOR_SIGNATURES: list[VendorSignature] = [
    VendorSignature(
        vendor="opta",
        top_level_keys=frozenset({"MatchId", "HomeTeam", "AwayTeam", "Events"}),
        event_list_key="Events",
        event_fields=frozenset({"EventId", "TypeName", "PlayerRef", "TeamName", "X", "Y"}),
    ),
    VendorSignature(
        vendor="statsbomb",
        top_level_keys=frozenset({"match_id", "home_team", "away_team", "events"}),
        event_list_key="events",
        event_fields=frozenset({"id", "type", "player", "team", "location"}),
    ),
    VendorSignature(
        vendor="wyscout",
        top_level_keys=frozenset({"wyId", "label", "events"}),
        event_list_key="events",
        event_fields=frozenset({"eventId", "subEventName", "trackable_object", "positions"}),
    ),
]

FUZZY_MATCH_THRESHOLD = 0.6
EVENT_SAMPLE_SIZE = 8


def _looks_like_real_statsbomb_event(e: dict) -> bool:
    """Real StatsBomb open-data event files ship *only* the events array --
    no match_id/home_team/away_team alongside it, that metadata lives in a
    separate matches.json this project never requires. That means the
    top-level-key half of the fuzzy score (VENDOR_SIGNATURES below) can
    never score well on genuine StatsBomb data, only on this project's own
    synthetic fixtures (which flatten team/player to strings and do include
    those top-level keys). This is a second, independent structural check
    for StatsBomb's actual event shape: type/team nested as {id, name}
    dicts and location as an [x, y] pair -- distinct enough from every
    other vendor (and from generic JSON) that a sample matching it
    consistently is strong evidence on its own, regardless of what's
    missing at the top level."""
    if not isinstance(e, dict):
        return False
    type_ok = isinstance(e.get("type"), dict) and isinstance(e["type"].get("name"), str)
    team_ok = isinstance(e.get("team"), dict) and isinstance(e["team"].get("name"), str)
    has_id = isinstance(e.get("id"), str)
    location = e.get("location")
    location_ok = location is None or (
        isinstance(location, list) and len(location) >= 2 and all(isinstance(v, (int, float)) for v in location)
    )
    return type_ok and team_ok and has_id and location_ok


def _looks_like_real_wyscout_event(e: dict) -> bool:
    """Real Wyscout open-data event files (as actually downloaded, e.g. a
    competition's events/<matchId>.json) ship only {"events", "teams",
    "players"} at the top level -- no wyId/label match-info wrapper, that
    lives in a separate matches file this project never requires. Same gap
    as _looks_like_real_statsbomb_event above, just undetected until a real
    file surfaced it: the top-level-key half of the fuzzy score can only
    ever score well against this project's own synthetic fixtures. Real
    exports also commonly carry "playerId" instead of the synthetic
    fixture's "trackable_object" for the per-event player reference, which
    schema_mapper already accepts as a fallback key -- it's detection,
    not mapping, that was missing this shape.

    Checked independently of top-level keys: eventId + subEventName/
    eventName + a positions list of {x, y} dicts is distinct enough from
    every other vendor's real shape (and from generic JSON) to be strong
    evidence on its own."""
    if not isinstance(e, dict):
        return False
    has_event_id = "eventId" in e or "id" in e
    has_type_name = isinstance(e.get("subEventName"), str) or isinstance(e.get("eventName"), str)
    positions = e.get("positions")
    positions_ok = (
        isinstance(positions, list) and len(positions) >= 1
        and all(isinstance(p, dict) and "x" in p and "y" in p for p in positions)
    )
    return has_event_id and has_type_name and positions_ok


@dataclass
class DetectionResult:
    detected_format: str                 # "opta" | "statsbomb" | "wyscout" | "unknown"
    confidence: float                    # 0-1, this agent's own assessment
    vendor_schema_version: Optional[str]
    file_type: str
    attempts: int
    attempt_log: list[str] = field(default_factory=list)
    event_list_key: Optional[str] = None  # which top-level key holds the events, even if vendor unknown

    def to_dict(self) -> dict:
        return {
            "detected_format": self.detected_format,
            "confidence": round(self.confidence, 4),
            "vendor_schema_version": self.vendor_schema_version,
            "file_type": self.file_type,
            "recovery_attempts": self.attempts,
            "attempt_log": self.attempt_log,
            "event_list_key": self.event_list_key,
        }


def _clean_keys(d: dict) -> set[str]:
    """Top-level keys, excluding internal testing/debug keys (anything prefixed '_')."""
    return {k for k in d.keys() if not k.startswith("_")}


def _sample_events(data: dict, event_list_key: str, n: int = EVENT_SAMPLE_SIZE) -> list[dict]:
    events = data.get(event_list_key)
    if not isinstance(events, list):
        return []
    return [e for e in events[:n] if isinstance(e, dict)]


def _strict_match(data: dict, sig: VendorSignature) -> bool:
    """Attempt 1: every expected top-level key present, event list exists and
    non-empty, and every sampled event has every expected field (structurally
    present — a null value still counts, since that's a data-quality issue,
    not a format-identification issue)."""
    if not sig.top_level_keys.issubset(_clean_keys(data)):
        return False
    events = _sample_events(data, sig.event_list_key)
    if not events:
        return False
    return all(sig.event_fields.issubset(set(e.keys())) for e in events)


def _fuzzy_score(data: dict, sig: VendorSignature) -> float:
    """Attempt 2: weighted fraction of expected fields present, top-level and
    per-event. Tolerant of some missing fields (real-world messy data)."""
    top_present = len(sig.top_level_keys & _clean_keys(data))
    top_score = top_present / len(sig.top_level_keys) if sig.top_level_keys else 0.0

    events = _sample_events(data, sig.event_list_key)
    if not events:
        event_score = 0.0
    else:
        per_event_scores = []
        for e in events:
            present = len(sig.event_fields & set(e.keys()))
            per_event_scores.append(present / len(sig.event_fields) if sig.event_fields else 0.0)
        event_score = sum(per_event_scores) / len(per_event_scores)

    return top_score * 0.4 + event_score * 0.6


def _find_largest_list_of_dicts(data: dict) -> Optional[str]:
    """Fallback heuristic: when vendor is unknown, guess which top-level key
    holds the event list by finding the longest list-of-dicts in the document."""
    best_key, best_len = None, 0
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if len(v) > best_len:
                best_key, best_len = k, len(v)
    return best_key


class FormatDetective:
    """Agent 1. Stateless — call detect() per file."""

    def detect(self, data: Any) -> DetectionResult:
        attempt_log: list[str] = []

        if not isinstance(data, dict):
            attempt_log.append("Input is not a JSON object at the top level — cannot proceed with structural detection.")
            return DetectionResult(
                detected_format="unknown",
                confidence=0.0,
                vendor_schema_version=None,
                file_type="unknown",
                attempts=3,
                attempt_log=attempt_log,
                event_list_key=None,
            )

        # --- Attempt 1: strict signature match ---
        strict_matches = [sig for sig in VENDOR_SIGNATURES if _strict_match(data, sig)]
        attempt_log.append(
            f"Attempt 1 (strict signature match): {[s.vendor for s in strict_matches] or 'no exact match'}"
        )

        if len(strict_matches) == 1:
            sig = strict_matches[0]
            return DetectionResult(
                detected_format=sig.vendor,
                confidence=0.98,
                vendor_schema_version=None,
                file_type="json",
                attempts=1,
                attempt_log=attempt_log,
                event_list_key=sig.event_list_key,
            )

        if len(strict_matches) > 1:
            attempt_log.append(
                f"Ambiguous: {len(strict_matches)} vendors matched strictly. Breaking tie with fuzzy scoring."
            )
            # fall through to fuzzy scoring, but restricted to the tied candidates

        # --- Attempt 1b: known real-world alternate shape, structural check ---
        # Only runs when nothing matched strictly -- a real open-data file
        # (events only, no match metadata) can't, by construction. Checked
        # before fuzzy scoring because that scoring method structurally
        # can't see this shape either (see _looks_like_real_statsbomb_event).
        if not strict_matches and "events" in _clean_keys(data):
            sb_events = _sample_events(data, "events")
            if sb_events and all(_looks_like_real_statsbomb_event(e) for e in sb_events):
                attempt_log.append(
                    "Attempt 1b (real-world nested-shape check): statsbomb "
                    "(nested type/team {id,name} dicts, [x,y] location array -- "
                    "StatsBomb's actual open-data event shape, no top-level match metadata)"
                )
                return DetectionResult(
                    detected_format="statsbomb",
                    confidence=0.9,
                    vendor_schema_version="statsbomb_open_data",
                    file_type="json",
                    attempts=1,
                    attempt_log=attempt_log,
                    event_list_key="events",
                )
            wy_events = sb_events
            if wy_events and all(_looks_like_real_wyscout_event(e) for e in wy_events):
                attempt_log.append(
                    "Attempt 1b (real-world nested-shape check): wyscout "
                    "(eventId + subEventName/eventName + positions [{x,y},...] -- "
                    "Wyscout's actual open-data event shape, no top-level wyId/label wrapper)"
                )
                return DetectionResult(
                    detected_format="wyscout",
                    confidence=0.9,
                    vendor_schema_version="wyscout_open_data",
                    file_type="json",
                    attempts=1,
                    attempt_log=attempt_log,
                    event_list_key="events",
                )

        # --- Attempt 2: fuzzy / partial match ---
        candidates = strict_matches if strict_matches else VENDOR_SIGNATURES
        scores = {sig.vendor: _fuzzy_score(data, sig) for sig in candidates}
        attempt_log.append(f"Attempt 2 (fuzzy match scores): {scores}")

        best_vendor = max(scores, key=scores.get)
        best_score = scores[best_vendor]

        if best_score >= FUZZY_MATCH_THRESHOLD:
            sig = next(s for s in VENDOR_SIGNATURES if s.vendor == best_vendor)
            return DetectionResult(
                detected_format=best_vendor,
                confidence=round(best_score, 4),
                vendor_schema_version=None,
                file_type="json",
                attempts=2,
                attempt_log=attempt_log,
                event_list_key=sig.event_list_key,
            )

        # --- Fallback: unknown vendor, generic parse ---
        attempt_log.append(
            f"Attempt 2 best score ({best_score:.2f}) below threshold ({FUZZY_MATCH_THRESHOLD}). "
            "Falling back to generic parse."
        )
        fallback_key = _find_largest_list_of_dicts(data)
        attempt_log.append(f"Fallback: using largest list-of-dicts field as event source: '{fallback_key}'")

        return DetectionResult(
            detected_format="unknown",
            confidence=0.3 if fallback_key else 0.0,
            vendor_schema_version=None,
            file_type="json",
            attempts=3,
            attempt_log=attempt_log,
            event_list_key=fallback_key,
        )


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    detective = FormatDetective()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 70)
    print("Format Detective — test run against all generated sample files")
    print("=" * 70)

    correct, total = 0, 0
    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        for vendor_file in sorted(match_dir.glob("*.json")):
            expected_vendor = vendor_file.stem  # e.g. "opta"
            data = json.loads(vendor_file.read_text())
            result = detective.detect(data)
            total += 1
            is_correct = result.detected_format == expected_vendor
            correct += is_correct
            status = "OK " if is_correct else "FAIL"
            print(f"[{status}] {match_dir.name}/{vendor_file.name:14s} "
                  f"expected={expected_vendor:10s} got={result.detected_format:10s} "
                  f"conf={result.confidence:.2f} attempts={result.attempts}")

    print("-" * 70)
    print(f"Accuracy: {correct}/{total} ({100*correct/total:.1f}%)")

    print("\n" + "=" * 70)
    print("Edge case: completely unknown format (garbage structure)")
    print("=" * 70)
    garbage = {"foo": "bar", "records": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}
    result = detective.detect(garbage)
    print(json.dumps(result.to_dict(), indent=2))
