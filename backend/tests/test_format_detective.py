"""Tests for Agent 1: Format Detective.

Covers the three fallback tiers the PRD specifies (strict match, fuzzy
match, generic fallback) plus the honesty constraint: detection must work
from structure alone, never by reading the generator's debug label.
"""

import json
import pytest
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective, VENDOR_SIGNATURES


@pytest.fixture(scope="module")
def detective():
    return FormatDetective()


def _all_sample_files():
    """(match_dir_name, vendor_name, filepath) for every generated sample."""
    cases = []
    for match_dir in sorted(SAMPLE_MATCHES_DIR.iterdir()):
        if not match_dir.is_dir():
            continue
        for f in sorted(match_dir.glob("*.json")):
            cases.append((match_dir.name, f.stem, f))
    return cases


ALL_SAMPLES = _all_sample_files()


@pytest.mark.parametrize("match_name,vendor,filepath", ALL_SAMPLES,
                          ids=[f"{m}/{v}" for m, v, _ in ALL_SAMPLES])
def test_detects_correct_vendor_on_all_sample_files(detective, match_name, vendor, filepath):
    data = json.loads(filepath.read_text())
    result = detective.detect(data)
    assert result.detected_format == vendor, (
        f"{match_name}/{vendor}.json: expected '{vendor}', got '{result.detected_format}'"
    )
    assert 0.0 <= result.confidence <= 1.0
    assert result.attempts >= 1


def test_clean_tier_always_matches_on_first_attempt(detective):
    """Clean data should never need fallback — if it does, something in the
    generator or the signatures has drifted out of sync."""
    for match_name, vendor, filepath in ALL_SAMPLES:
        if "CLEAN" not in match_name:
            continue
        data = json.loads(filepath.read_text())
        result = detective.detect(data)
        assert result.attempts == 1, f"{match_name}/{vendor}.json needed fallback on clean data"
        assert result.confidence >= 0.95


def test_ignores_internal_debug_label(detective):
    """The generator stamps '_vendor_format' for scoring purposes. Detection
    must not use it — this is the test that would catch us cheating."""
    data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json").read_text())
    assert data["_vendor_format"] == "opta"

    tampered = dict(data)
    tampered["_vendor_format"] = "wyscout"  # lie in the debug field
    result = detective.detect(tampered)
    assert result.detected_format == "opta", "detection used the debug label instead of structure"


def test_fuzzy_path_engages_when_keys_genuinely_missing(detective):
    data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "statsbomb.json").read_text())
    del data["home_team"]
    for e in data["events"]:
        e.pop("team", None)

    result = detective.detect(data)
    assert result.detected_format == "statsbomb"
    assert result.attempts == 2
    assert result.confidence < 0.98  # must be lower than a strict match


def test_fallback_on_completely_unknown_structure(detective):
    garbage = {"foo": "bar", "records": [{"a": 1}, {"a": 2}]}
    result = detective.detect(garbage)
    assert result.detected_format == "unknown"
    assert result.attempts == 3
    assert result.event_list_key == "records"  # still finds a plausible event list


def test_non_dict_input_does_not_crash(detective):
    result = detective.detect(["not", "a", "dict"])
    assert result.detected_format == "unknown"
    assert result.confidence == 0.0


def test_real_statsbomb_open_data_shape_detected_without_top_level_match_metadata(detective):
    """Regression: real StatsBomb open-data event files carry *only* the
    events array -- no match_id/home_team/away_team alongside it (that
    lives in a separate matches.json this project doesn't require), unlike
    this project's synthetic statsbomb fixtures. That used to score 0.58 on
    fuzzy matching (just under the 0.6 threshold) and fall to the generic/
    unknown-vendor path, which can't read nested type/team/location shapes
    and extracted nothing. The structural nested-shape check should catch
    this directly, on attempt 1, without needing the top-level keys."""
    real_shaped = {
        "events": [
            {
                "id": "e1", "type": {"id": 30, "name": "Pass"}, "timestamp": "00:00:01.087",
                "period": 1, "team": {"id": 223, "name": "Malaga"},
                "player": {"id": 6707, "name": "Someone"}, "location": [60.0, 40.0],
            },
            {
                "id": "e2", "type": {"id": 16, "name": "Shot"}, "timestamp": "00:00:04.512",
                "period": 1, "team": {"id": 224, "name": "Barcelona"},
                "location": [102.3, 38.1],
            },
        ],
    }
    result = detective.detect(real_shaped)
    assert result.detected_format == "statsbomb"
    assert result.attempts == 1
    assert result.confidence >= 0.9
    assert result.event_list_key == "events"


def test_real_statsbomb_shape_check_does_not_misfire_on_generic_nested_json(detective):
    """A document that merely has dict-shaped 'type'/'team' fields but isn't
    actually StatsBomb-shaped (no 'name' inside them, no location) should
    not be mistaken for statsbomb -- the nested-shape check has to require
    the specific {id, name} dict shape, not just 'type happens to be a dict'."""
    not_statsbomb = {
        "events": [
            {"id": "x1", "type": {"code": 1}, "team": {"code": "A"}},
            {"id": "x2", "type": {"code": 2}, "team": {"code": "B"}},
        ],
    }
    result = detective.detect(not_statsbomb)
    assert result.detected_format != "statsbomb"


def test_vendor_signatures_are_internally_consistent():
    """Guards against a vendor signature being edited inconsistently
    (e.g. event_fields referencing the wrong event_list_key)."""
    vendors = {s.vendor for s in VENDOR_SIGNATURES}
    assert vendors == {"opta", "statsbomb", "wyscout"}
    for sig in VENDOR_SIGNATURES:
        assert sig.top_level_keys, f"{sig.vendor} has empty top_level_keys"
        assert sig.event_fields, f"{sig.vendor} has empty event_fields"
        assert sig.event_list_key in sig.top_level_keys, (
            f"{sig.vendor}: event_list_key '{sig.event_list_key}' not in its own top_level_keys"
        )
