"""Tests for Agent 5: Validator.

Covers: the full pipeline produces a schema-valid CDFDocument for every
sample file, period repair (the recurring data-quality gap in this
dataset), coordinate-range defense, player-record assembly for both
cross-vendor-resolved and vendor-scoped-fallback identities, and match-info
extraction across all three vendor shapes (including the honest fallback
when nothing supplies a usable value).
"""

import json
import pytest
from datetime import date
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper, MappedEvent
from coordinate_translator import CoordinateTranslator
from entity_resolver import EntityResolver, ResolvedEvent
from validator import Validator, _resolve_match_info, _repair_period
from cdf_models import CDFDocument


@pytest.fixture(scope="module")
def detective():
    return FormatDetective()


@pytest.fixture(scope="module")
def mapper():
    return SchemaMapper()


@pytest.fixture(scope="module")
def translator():
    return CoordinateTranslator()


@pytest.fixture(scope="module")
def resolver():
    return EntityResolver()


@pytest.fixture
def validator():
    return Validator()


def _full_pipeline(detective, mapper, translator, resolver, validator, match_dir):
    all_events = []
    vendor_raw_data = {}
    for vendor_file in sorted(match_dir.glob("*.json")):
        data = json.loads(vendor_file.read_text())
        detection = detective.detect(data)
        vendor_raw_data[detection.detected_format] = data
        mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
        translated = translator.translate(mapped)
        all_events.extend(translated)
    resolved, registry = resolver.resolve(all_events)
    document, report = validator.build_document(resolved, registry, vendor_raw_data)
    return document, report


def _all_match_dirs():
    return [d for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_dirs()


def _bare_event(**overrides) -> MappedEvent:
    defaults = dict(
        event_id="e1", event_type="pass", event_type_raw="Pass", timestamp=1.0,
        period=1, team="Liverpool", raw_player_ref={"vendor_id": None, "jersey_number": None, "name": None},
        x=50.0, y=50.0, end_x=None, end_y=None, outcome=None,
        vendor_source="opta", mapping_confidence=1.0, mapping_attempts=1,
        mapping_log=[], raw_metadata={},
    )
    defaults.update(overrides)
    return MappedEvent(**defaults)


# --- Integration: full pipeline produces a valid CDFDocument ---

@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_full_pipeline_produces_valid_document(detective, mapper, translator, resolver, validator, match_dir):
    document, report = _full_pipeline(detective, mapper, translator, resolver, validator, match_dir)
    assert isinstance(document, CDFDocument)  # pydantic already validated this on construction
    assert report.events_in == len(document.events) + report.events_dropped
    assert document.metadata.coordinate_system == "normalized_0_100"


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_no_events_dropped_for_this_dataset(detective, mapper, translator, resolver, validator, match_dir):
    """Nothing in the synthetic generator ever nulls out x/y, so Validator
    should never need to drop an event for this dataset -- period repair,
    yes; event drops, no. If this starts failing, something upstream
    changed in a way that needs attention, not a quiet pass-through."""
    document, report = _full_pipeline(detective, mapper, translator, resolver, validator, match_dir)
    assert report.events_dropped == 0


def test_clean_tier_has_no_period_repairs(detective, mapper, translator, resolver, validator):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    document, report = _full_pipeline(detective, mapper, translator, resolver, validator, match_dir)
    assert report.periods_repaired == 0
    assert document.metadata.quality_confidence == 1.0


def test_messy_tier_needs_period_repairs(detective, mapper, translator, resolver, validator):
    """Confirms the fixture assumption this agent's repair path exists for."""
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001"
    document, report = _full_pipeline(detective, mapper, translator, resolver, validator, match_dir)
    assert report.periods_repaired > 0
    assert any("period" in w for w in report.warnings)
    assert document.metadata.quality_confidence < 1.0


def test_every_event_player_id_references_a_real_player_record(detective, mapper, translator, resolver, validator, ):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001"
    document, _ = _full_pipeline(detective, mapper, translator, resolver, validator, match_dir)
    known_ids = {p.player_id for p in document.players}
    for e in document.events:
        assert e.player_id in known_ids, f"event {e.event_id} references unknown player_id {e.player_id}"


def test_match_info_extracted_from_vendor_data(detective, mapper, translator, resolver, validator):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    document, _ = _full_pipeline(detective, mapper, translator, resolver, validator, match_dir)
    assert document.match.match_id == "PL_2025_CLEAN_001"
    assert document.match.home_team == "Liverpool"
    assert document.match.away_team == "Manchester City"


# --- Unit tests: period repair ---

def test_repair_period_valid_passes_through():
    log = []
    period, penalty = _repair_period(2, 3000.0, log)
    assert period == 2
    assert penalty == 0.0
    assert log == []


def test_repair_period_missing_inferred_from_timestamp_first_half():
    log = []
    period, penalty = _repair_period(None, 1200.0, log)
    assert period == 1
    assert penalty > 0.0
    assert log


def test_repair_period_missing_inferred_from_timestamp_second_half():
    log = []
    period, penalty = _repair_period(None, 3200.0, log)
    assert period == 2
    assert penalty > 0.0


def test_repair_period_out_of_range_also_repaired():
    log = []
    period, penalty = _repair_period(7, 500.0, log)
    assert period == 1
    assert penalty > 0.0


# --- Unit tests: match info extraction ---

def test_match_info_opta_shape():
    raw = {"opta": {"MatchId": "m1", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea"}}
    info, log = _resolve_match_info(raw, date(2026, 1, 1))
    assert info.match_id == "m1"
    assert info.home_team == "Arsenal"
    assert info.away_team == "Chelsea"
    assert log == []


def test_match_info_statsbomb_shape():
    raw = {"statsbomb": {"match_id": "m2", "home_team": {"name": "Liverpool"}, "away_team": {"name": "Everton"}}}
    info, _ = _resolve_match_info(raw, date(2026, 1, 1))
    assert info.match_id == "m2"
    assert info.home_team == "Liverpool"


def test_match_info_wyscout_shape():
    raw = {"wyscout": {"wyId": "m3", "label": "Spurs - West Ham"}}
    info, _ = _resolve_match_info(raw, date(2026, 1, 1))
    assert info.match_id == "m3"
    assert info.home_team == "Spurs"
    assert info.away_team == "West Ham"


def test_match_info_falls_back_when_nothing_usable():
    info, log = _resolve_match_info({"unknown": {"garbage": True}}, date(2026, 1, 1))
    assert info.match_id == "unknown_match"
    assert info.home_team == "unknown_home_team"
    assert info.away_team == "unknown_away_team"
    assert len(log) >= 2


def test_match_date_defaults_when_not_supplied():
    info, log = _resolve_match_info({"opta": {"MatchId": "m1", "HomeTeam": "A", "AwayTeam": "B"}}, None)
    assert isinstance(info.date, date)
    assert any("defaulted to today's date" in w for w in log)


# --- Unit tests: coordinate defense ---

def test_event_with_out_of_range_coordinate_is_dropped(validator):
    e = _bare_event(x=150.0, y=50.0)
    r = ResolvedEvent(event=e, canonical_player_id="player::Liverpool::9",
                       resolution_confidence=1.0, resolution_method="deterministic_jersey_team")
    registry = {}
    vendor_raw_data = {"opta": {"MatchId": "m", "HomeTeam": "A", "AwayTeam": "B"}}
    document, report = validator.build_document([r], registry, vendor_raw_data, match_date=date(2026, 1, 1))
    assert report.events_dropped == 1
    assert len(document.events) == 0
    assert any("dropped" in w for w in report.warnings)


def test_event_with_null_coordinate_is_dropped(validator):
    e = _bare_event(x=None, y=None)
    r = ResolvedEvent(event=e, canonical_player_id="player::Liverpool::9",
                       resolution_confidence=1.0, resolution_method="deterministic_jersey_team")
    vendor_raw_data = {"opta": {"MatchId": "m", "HomeTeam": "A", "AwayTeam": "B"}}
    document, report = validator.build_document([r], {}, vendor_raw_data, match_date=date(2026, 1, 1))
    assert report.events_dropped == 1
    assert len(document.events) == 0


# --- Match-sheet assembly ---

@pytest.fixture(scope="module")
def mapper_for_sheets():
    return SchemaMapper()


def _minimal_valid_run(validator):
    """A single trivially-valid event, just enough to get build_document to
    run -- these tests are about the match_sheet argument, not the event
    path (already covered above)."""
    e = _bare_event()
    r = ResolvedEvent(event=e, canonical_player_id="player::Liverpool::9",
                       resolution_confidence=1.0, resolution_method="deterministic_jersey_team")
    vendor_raw_data = {"opta": {"MatchId": "PL_2025_CLEAN_001", "HomeTeam": "Liverpool", "AwayTeam": "Manchester City"}}
    return r, vendor_raw_data


def test_no_match_sheet_payload_leaves_document_match_sheet_none(validator):
    r, vendor_raw_data = _minimal_valid_run(validator)
    document, report = validator.build_document([r], {}, vendor_raw_data, match_date=date(2026, 1, 1))
    assert document.match_sheet is None


def test_match_sheet_payload_assembles_into_document(validator, mapper_for_sheets):
    r, vendor_raw_data = _minimal_valid_run(validator)
    sheet_data = json.loads(
        (SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "match_sheets" / "opta_match_sheet.json").read_text()
    )
    mapped_sheet = mapper_for_sheets.map_match_sheet(sheet_data)

    document, report = validator.build_document(
        [r], {}, vendor_raw_data, match_date=date(2026, 1, 1), mapped_match_sheet=mapped_sheet,
    )

    assert document.match_sheet is not None
    assert document.match_sheet.home.team == "Liverpool"
    assert document.match_sheet.away.team == "Manchester City"
    assert len(document.match_sheet.home.starters) == 11
    assert len(document.match_sheet.home.substitutes) == 7
    assert len(document.match_sheet.home.substitutions) > 0
    assert document.match_sheet.officials.referee is not None
    assert document.match_sheet.attendance is not None
    assert document.match_sheet.kickoff is not None


def test_match_sheet_with_no_lineup_data_is_dropped_not_fabricated(validator, mapper_for_sheets):
    r, vendor_raw_data = _minimal_valid_run(validator)
    mapped_sheet = mapper_for_sheets.map_match_sheet({"garbage": True})

    document, report = validator.build_document(
        [r], {}, vendor_raw_data, match_date=date(2026, 1, 1), mapped_match_sheet=mapped_sheet,
    )

    assert document.match_sheet is None
    assert any("match sheet unavailable" in w for w in report.warnings)
