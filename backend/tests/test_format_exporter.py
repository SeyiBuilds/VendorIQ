"""Tests for Agent 7: Format Exporter.

Covers: round-tripping a validated CDFDocument back to each MVP vendor
shape, coordinate inverse-scaling correctness, native event_type recovery
vs. best-effort guessing, honest handling of players with no native id for
the target vendor (synthesized placeholder, clearly flagged, never passed
off as real), and the unsupported-vendor path.
"""

import json
import pytest
from datetime import date, datetime, timezone
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper
from coordinate_translator import CoordinateTranslator
from entity_resolver import EntityResolver
from validator import Validator
from format_exporter import FormatExporter, VENDOR_PITCH_DIMENSIONS
from cdf_models import CDFDocument, CDFEvent, CDFMetadata, MatchInfo, PlayerRecord


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


@pytest.fixture(scope="module")
def validator():
    return Validator()


@pytest.fixture
def exporter():
    return FormatExporter()


def _document_for_match(detective, mapper, translator, resolver, validator, match_dir) -> CDFDocument:
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
    document, _ = validator.build_document(resolved, registry, vendor_raw_data)
    return document


@pytest.fixture(scope="module")
def clean_document(detective, mapper, translator, resolver, validator):
    return _document_for_match(detective, mapper, translator, resolver, validator,
                                SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001")


def _minimal_document(events, players=None) -> CDFDocument:
    return CDFDocument(
        match=MatchInfo(match_id="m1", date=date(2026, 1, 1), home_team="Home FC", away_team="Away FC"),
        metadata=CDFMetadata(source_vendors=["opta"], quality_confidence=1.0,
                              generation_timestamp=datetime.now(timezone.utc)),
        players=players or [],
        events=events,
    )


def _event(**overrides) -> CDFEvent:
    defaults = dict(
        event_id="e1", event_type="pass", timestamp=10.0, period=1,
        player_id=None, team="Home FC", x=50.0, y=25.0,
        outcome=None, quality_confidence=1.0, vendor_source="opta", metadata={},
    )
    defaults.update(overrides)
    return CDFEvent(**defaults)


# --- Integration: round trip against real multi-vendor sample data ---

@pytest.mark.parametrize("target", ["opta", "statsbomb", "wyscout"])
def test_export_covers_every_event(clean_document, exporter, target):
    _, report = exporter.export(clean_document, target)
    assert report.events_exported == len(clean_document.events)


@pytest.mark.parametrize("target", ["opta", "statsbomb", "wyscout"])
def test_clean_tier_every_player_has_native_id(clean_document, exporter, target):
    """Clean-tier data has jersey_number (and thus a cross-vendor match) on
    every event from every vendor, so every player should have been seen
    in every vendor's feed -- no placeholder ids needed."""
    _, report = exporter.export(clean_document, target)
    assert report.players_with_synthesized_id == 0


def test_opta_export_same_vendor_events_recover_native_type(clean_document, exporter):
    """The 300 events that actually originated from Opta should recover
    their exact original TypeName string, not a best-effort guess."""
    _, report = exporter.export(clean_document, "opta")
    same_vendor_count = sum(1 for e in clean_document.events if e.vendor_source == "opta")
    assert report.events_with_native_type_recovered == same_vendor_count
    assert report.events_with_guessed_type == len(clean_document.events) - same_vendor_count


def test_opta_export_structure_matches_native_shape(clean_document, exporter):
    payload, _ = exporter.export(clean_document, "opta")
    assert payload["MatchId"] == clean_document.match.match_id
    assert payload["HomeTeam"] == clean_document.match.home_team
    assert "Events" in payload
    sample = payload["Events"][0]
    assert set(sample) == {"EventId", "TypeName", "Min", "Sec", "PeriodId", "PlayerRef",
                            "JerseyNumber", "TeamName", "X", "Y"}


def test_statsbomb_export_structure_matches_native_shape(clean_document, exporter):
    payload, _ = exporter.export(clean_document, "statsbomb")
    assert payload["match_id"] == clean_document.match.match_id
    assert payload["home_team"]["name"] == clean_document.match.home_team
    sample = payload["events"][0]
    assert sample["type"]["name"]
    assert isinstance(sample["location"], list) and len(sample["location"]) == 2


def test_wyscout_export_structure_matches_native_shape(clean_document, exporter):
    payload, _ = exporter.export(clean_document, "wyscout")
    assert payload["wyId"] == clean_document.match.match_id
    assert payload["label"] == f"{clean_document.match.home_team} - {clean_document.match.away_team}"
    sample = payload["events"][0]
    assert sample["matchPeriod"].endswith("H")
    assert isinstance(sample["positions"], list)


# --- Unit tests: coordinate inverse scaling ---

def test_opta_coordinate_inverse_scaling_exact(exporter):
    """0-100 CDF coordinate -> Opta's native 0-120 x 0-80, exact inverse of
    Agent 3's forward scaling."""
    event = _event(x=50.0, y=50.0)
    document = _minimal_document([event])
    payload, _ = exporter.export(document, "opta")
    native_max_x, native_max_y = VENDOR_PITCH_DIMENSIONS["opta"]
    assert payload["Events"][0]["X"] == pytest.approx(50.0 / 100 * native_max_x)
    assert payload["Events"][0]["Y"] == pytest.approx(50.0 / 100 * native_max_y)


def test_statsbomb_coordinates_pass_through_unchanged(exporter):
    """StatsBomb native scale is already 0-100, so export should not rescale."""
    event = _event(x=73.5, y=12.0)
    document = _minimal_document([event])
    payload, _ = exporter.export(document, "statsbomb")
    assert payload["events"][0]["location"] == [73.5, 12.0]


def test_full_roundtrip_opta_coordinates_recover_original(detective, mapper, translator, exporter):
    """Format Mapper -> Coordinate Translator -> Format Exporter should
    recover the original raw Opta X/Y (within rounding) for an event that
    actually came from Opta."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json"
    data = json.loads(path.read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)

    raw_event = data["Events"][0]
    cdf_event = _event(x=translated[0].x, y=translated[0].y, vendor_source="opta",
                        metadata={"event_type_raw": mapped[0].event_type_raw})
    document = _minimal_document([cdf_event])
    payload, _ = exporter.export(document, "opta")
    assert payload["Events"][0]["X"] == pytest.approx(raw_event["X"], abs=0.05)
    assert payload["Events"][0]["Y"] == pytest.approx(raw_event["Y"], abs=0.05)


# --- Unit tests: player reference handling ---

def test_player_with_native_id_for_target_vendor(exporter):
    player = PlayerRecord(player_id="p1", name="Test Player", jersey_number=9, team="Home FC",
                           vendor_ids={"opta": "opta_123"}, resolution_confidence=1.0)
    event = _event(player_id="p1")
    document = _minimal_document([event], players=[player])
    payload, report = exporter.export(document, "opta")
    assert payload["Events"][0]["PlayerRef"] == "opta_123"
    assert report.players_with_native_id == 1
    assert report.players_with_synthesized_id == 0


def test_player_without_native_id_gets_flagged_placeholder(exporter):
    """Player was seen in statsbomb but never in opta -- exporting to opta
    must synthesize a placeholder and flag it, not silently invent a
    plausible-looking fake id."""
    player = PlayerRecord(player_id="p1", name="Test Player", vendor_ids={"statsbomb": "sb_1"},
                           resolution_confidence=1.0)
    event = _event(player_id="p1")
    document = _minimal_document([event], players=[player])
    payload, report = exporter.export(document, "opta")
    assert payload["Events"][0]["PlayerRef"].startswith("synth_")
    assert report.players_with_synthesized_id == 1
    assert any("no native opta id" in w for w in report.warnings)


def test_player_id_not_in_registry_at_all(exporter):
    """Defensive case: an event references a player_id Validator somehow
    didn't add to players[]. Should synthesize, not crash."""
    event = _event(player_id="ghost_id")
    document = _minimal_document([event], players=[])
    payload, report = exporter.export(document, "opta")
    assert payload["Events"][0]["PlayerRef"] == "synth_ghost_id"
    assert report.players_with_synthesized_id == 1


def test_event_with_no_player_id_exports_null_ref(exporter):
    event = _event(player_id=None)
    document = _minimal_document([event])
    payload, report = exporter.export(document, "opta")
    assert payload["Events"][0]["PlayerRef"] is None
    assert report.players_with_synthesized_id == 0
    assert report.players_with_native_id == 0


# --- Unsupported vendor ---

def test_unsupported_vendor_returns_empty_payload_with_warning(exporter):
    document = _minimal_document([_event()])
    payload, report = exporter.export(document, "genius_sports")
    assert payload == {}
    assert report.events_exported == 0
    assert any("no exporter registered" in w for w in report.warnings)
