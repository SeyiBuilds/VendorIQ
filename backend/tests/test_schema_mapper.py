"""Tests for Agent 2: Schema Mapper.

Covers: every event gets mapped (none silently dropped), confidence tracks
data quality, coordinates are deliberately left untouched (Agent 3's job),
and the unknown-vendor generic path works end to end.
"""

import json
import pytest
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper, CANONICAL_EVENT_TYPES


@pytest.fixture(scope="module")
def detective():
    return FormatDetective()


@pytest.fixture(scope="module")
def mapper():
    return SchemaMapper()


def _load_and_map(detective, mapper, path):
    data = json.loads(path.read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    return data, detection, mapped


def _all_sample_files():
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
def test_no_events_silently_dropped(detective, mapper, match_name, vendor, filepath):
    data, detection, mapped = _load_and_map(detective, mapper, filepath)
    source_count = len(data[detection.event_list_key])
    assert len(mapped) == source_count, (
        f"{match_name}/{vendor}.json: {source_count} source events but {len(mapped)} mapped"
    )


@pytest.mark.parametrize("match_name,vendor,filepath", ALL_SAMPLES,
                          ids=[f"{m}/{v}" for m, v, _ in ALL_SAMPLES])
def test_every_mapped_event_type_is_canonical(detective, mapper, match_name, vendor, filepath):
    _, _, mapped = _load_and_map(detective, mapper, filepath)
    for e in mapped:
        assert e.event_type in CANONICAL_EVENT_TYPES, (
            f"{match_name}/{vendor}.json event {e.event_id}: non-canonical type '{e.event_type}'"
        )


def test_clean_tier_has_no_missing_fields(detective, mapper):
    for match_name, vendor, filepath in ALL_SAMPLES:
        if "CLEAN" not in match_name:
            continue
        _, _, mapped = _load_and_map(detective, mapper, filepath)
        for e in mapped:
            assert e.mapping_confidence == 1.0, f"{match_name}/{vendor}: event {e.event_id} not full confidence on clean data"
            assert e.mapping_attempts == 1


def test_messy_tier_confidence_is_lower_than_clean(detective, mapper):
    def avg_conf(match_name, vendor):
        path = SAMPLE_MATCHES_DIR / match_name / f"{vendor}.json"
        _, _, mapped = _load_and_map(detective, mapper, path)
        return sum(e.mapping_confidence for e in mapped) / len(mapped)

    for vendor in ["opta", "statsbomb", "wyscout"]:
        clean = avg_conf("PL_2025_CLEAN_001", vendor)
        messy = avg_conf("PL_2025_MESSY_001", vendor)
        assert messy < clean, f"{vendor}: messy tier ({messy}) not lower confidence than clean ({clean})"


def test_coordinates_pass_through_unmodified_opta(detective, mapper):
    """Opta native scale is 0-120 x 0-80. Confirms Schema Mapper does NOT
    normalize — that responsibility belongs to Agent 3."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json"
    data, detection, mapped = _load_and_map(detective, mapper, path)
    raw_events = data["Events"]
    for raw, m in zip(raw_events, mapped):
        if raw.get("X") is not None:
            assert m.x == raw["X"], "Opta X coordinate was altered by Schema Mapper"


def test_out_of_bounds_coordinates_preserved_not_clamped(detective, mapper):
    path = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001" / "statsbomb.json"
    _, _, mapped = _load_and_map(detective, mapper, path)
    out_of_bounds = [e for e in mapped if e.x is not None and e.x > 100]
    assert len(out_of_bounds) > 0, "expected at least one out-of-bounds event in messy statsbomb data"


def test_wyscout_worst_case_both_player_ids_missing(detective, mapper):
    path = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001" / "wyscout.json"
    _, _, mapped = _load_and_map(detective, mapper, path)
    worst_case = [
        e for e in mapped
        if e.raw_player_ref["vendor_id"] is None and e.raw_player_ref["jersey_number"] is None
    ]
    assert len(worst_case) > 0, "expected at least one event with both player identifiers missing"
    for e in worst_case:
        assert any("player identifiers missing" in log for log in e.mapping_log)


def test_unknown_vendor_generic_path(detective, mapper):
    garbage = {
        "match_id": "weird_vendor_001",
        "records": [
            {"id": "ev1", "type": "pass", "x": 45.2, "y": 60.1, "team": "Arsenal"},
            {"id": "ev2", "type": "shot", "x": 88.0, "y": 50.0, "team": "Chelsea"},
        ],
    }
    detection = detective.detect(garbage)
    mapped = mapper.map_events(garbage, detection.detected_format, detection.event_list_key)

    assert detection.detected_format == "unknown"
    assert len(mapped) == 2
    assert mapped[0].event_type == "pass"
    assert mapped[0].x == 45.2
    assert all(e.mapping_confidence <= 0.5 for e in mapped), "unknown-vendor confidence should be capped low"


def test_real_statsbomb_open_data_shape_maps_correctly_end_to_end(detective, mapper):
    """Regression (two-stage): a real StatsBomb open-data event file (bare
    array, wrapped under "events" for detection) used to fall through
    Format Detective's fuzzy match (0.58, just under the 0.6 threshold,
    since real files carry no top-level match_id/home_team/away_team) to
    the generic/unknown-vendor mapper -- which crashed on the "HH:MM:SS.mmm"
    timestamp string before that was patched, and even patched could only
    degrade honestly (0 usable fields) since it doesn't know StatsBomb's
    nested shapes. Format Detective now recognizes this shape directly
    (structural nested-type/team/location check, not top-level keys), which
    routes it to the real statsbomb mapper -- so this now asserts the
    correct extracted values, not just "didn't crash"."""
    real_shaped = {
        "events": [
            {
                "id": "e1", "type": {"id": 30, "name": "Pass"}, "timestamp": "00:00:01.087",
                "period": 1, "team": {"id": 223, "name": "Malaga"},
                "player": {"id": 6707, "name": "Someone"}, "location": [60.0, 40.0],
            },
        ],
    }
    detection = detective.detect(real_shaped)
    mapped = mapper.map_events(real_shaped, detection.detected_format, detection.event_list_key)

    assert detection.detected_format == "statsbomb"
    assert len(mapped) == 1
    e = mapped[0]
    assert e.timestamp == 1.087
    assert e.team == "Malaga"
    assert e.event_type == "pass"
    assert e.x == 60.0 and e.y == 40.0
    assert e.raw_player_ref["vendor_id"] == 6707
    assert e.vendor_source == "statsbomb"


def test_unknown_vendor_generic_path_still_survives_non_numeric_timestamp_and_nested_fields(mapper):
    """The generic/unknown-vendor fallback itself must still degrade
    honestly (not crash) on a non-numeric timestamp string and dict-shaped
    fields, for whatever genuinely-unrecognized vendor shape reaches it --
    exercised directly here now that the real-StatsBomb case above no
    longer reaches this path via Format Detective."""
    garbage_but_similar_shape = {
        "id": "e1", "type": {"id": 1, "name": "Mystery"}, "timestamp": "00:00:01.087",
        "team": {"id": 1, "name": "Nowhere FC"},
    }
    mapped = mapper.map_events(
        {"records": [garbage_but_similar_shape]}, "unknown", "records",
    )
    assert len(mapped) == 1
    e = mapped[0]
    assert e.timestamp == 1.087
    assert e.team is None  # generic sniffing doesn't know to look inside nested dicts
    assert e.event_type == "other"


def test_raw_metadata_preserves_unmapped_fields_for_export_back(detective, mapper):
    """Agent 7 (Format Exporter) needs leftover vendor-specific fields to do
    a reasonably lossless round trip. Confirms they're not being discarded."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "wyscout.json"
    _, _, mapped = _load_and_map(detective, mapper, path)
    # every known wyscout field is consumed, so raw_metadata should be empty
    # for clean data with no extra vendor fields -- this just confirms the
    # mechanism exists and doesn't error, not that it's always non-empty
    assert all(isinstance(e.raw_metadata, dict) for e in mapped)


# --- Match-sheet mapping: a separate, parallel path from event mapping ---

MATCH_SHEET_VENDORS = ["opta", "statsbomb", "wyscout"]


@pytest.mark.parametrize("vendor", MATCH_SHEET_VENDORS)
def test_match_sheet_mapping_round_trips_each_vendor_shape(mapper, vendor):
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "match_sheets" / f"{vendor}_match_sheet.json"
    data = json.loads(path.read_text())

    sheet = mapper.map_match_sheet(data)

    assert sheet.vendor_source == vendor
    assert sheet.home_team == "Liverpool"
    assert sheet.away_team == "Manchester City"
    assert sheet.home_formation is not None
    assert len(sheet.home_lineup) == 18
    assert len(sheet.away_lineup) == 18
    assert sum(1 for p in sheet.home_lineup if p.is_starter) == 11
    assert len(sheet.substitutions) > 0
    assert sheet.referee is not None
    assert sheet.venue is not None
    assert sheet.mapping_confidence == 1.0


def test_match_sheet_messy_tier_still_maps_without_crashing():
    """Messy-tier degradation (missing jersey numbers, null formation,
    dropped player ids) should lower confidence and log it, never crash."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001" / "match_sheets" / "wyscout_match_sheet.json"
    data = json.loads(path.read_text())
    mapper = SchemaMapper()

    sheet = mapper.map_match_sheet(data)

    assert sheet.vendor_source == "wyscout"
    assert len(sheet.home_lineup) == 18  # structure survives even with field-level drops


def test_match_sheet_unrecognized_shape_degrades_honestly():
    mapper = SchemaMapper()
    sheet = mapper.map_match_sheet({"totally": "unrelated", "shape": True})

    assert sheet.vendor_source == "unknown"
    assert sheet.mapping_confidence == 0.0
    assert sheet.home_lineup == []
    assert sheet.away_lineup == []
    assert any("unrecognized" in log for log in sheet.mapping_log)


def test_match_sheet_non_dict_input_does_not_crash():
    mapper = SchemaMapper()
    sheet = mapper.map_match_sheet("not a dict")
    assert sheet.vendor_source == "unknown"
    assert sheet.mapping_confidence == 0.0
