"""Tests for Agent 6: Narrative Generator.

Covers: team stats are correct arithmetic over CDFEvents (not approximations),
the template narrative is always produced with zero external dependencies,
the optional AI-escalation path degrades exactly like Entity Resolver's
(no key -> template, never a crash), low-confidence events are counted
but flagged rather than silently blended in, and key-moment extraction
works correctly on synthetic events built for that purpose, and pass
distance/difficulty banding (short/medium/long, converted from the
normalized 0-100 coordinate space into meters via a documented standard
pitch-dimension assumption).
"""

import json
import pytest
from datetime import date
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper
from coordinate_translator import CoordinateTranslator
from entity_resolver import EntityResolver
from validator import Validator
from narrative_generator import NarrativeGenerator, TeamStats, _pass_distance_m, _pass_difficulty_band
from cdf_models import CDFDocument, CDFEvent, CDFMetadata, MatchInfo, PlayerRecord
from datetime import datetime, timezone


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
def narrator():
    return NarrativeGenerator()


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


def _all_match_dirs():
    return [d for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_dirs()


def _minimal_document(events: list[CDFEvent], players: list[PlayerRecord] = None, warnings: list[str] = None) -> CDFDocument:
    return CDFDocument(
        match=MatchInfo(match_id="m1", date=date(2026, 1, 1), home_team="Home FC", away_team="Away FC"),
        metadata=CDFMetadata(
            source_vendors=["opta"], quality_confidence=1.0,
            generation_timestamp=datetime.now(timezone.utc), warnings=warnings or [],
        ),
        players=players or [],
        events=events,
    )


def _event(**overrides) -> CDFEvent:
    defaults = dict(
        event_id="e1", event_type="pass", timestamp=10.0, period=1,
        player_id=None, team="Home FC", x=50.0, y=50.0,
        outcome=None, quality_confidence=1.0, vendor_source="opta", metadata={},
    )
    defaults.update(overrides)
    return CDFEvent(**defaults)


# --- Integration: full pipeline -> narrative ---

@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_full_pipeline_produces_narrative(detective, mapper, translator, resolver, validator, narrator, match_dir):
    document = _document_for_match(detective, mapper, translator, resolver, validator, match_dir)
    report = narrator.generate(document)
    assert report.narrative_text
    assert document.match.home_team in report.narrative_text
    assert document.match.away_team in report.narrative_text
    assert report.generation_method in ("template", "ai_enhanced")


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_team_stats_cover_both_teams(detective, mapper, translator, resolver, validator, narrator, match_dir):
    document = _document_for_match(detective, mapper, translator, resolver, validator, match_dir)
    report = narrator.generate(document)
    assert document.match.home_team in report.team_stats
    assert document.match.away_team in report.team_stats


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_event_totals_match_document_event_count(detective, mapper, translator, resolver, validator, narrator, match_dir):
    document = _document_for_match(detective, mapper, translator, resolver, validator, match_dir)
    report = narrator.generate(document)
    total_from_stats = sum(s.events_total for s in report.team_stats.values())
    assert total_from_stats == len(document.events)


def test_sample_data_produces_key_moments(detective, mapper, translator, resolver, validator, narrator):
    """The generator's EVENT_TYPES includes weighted goal/card events, so a
    300-event sample match should produce at least one key moment -- and
    every one found must be attributed to a real player name, not a
    dangling unresolved id, since clean-tier data has full jersey coverage."""
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    document = _document_for_match(detective, mapper, translator, resolver, validator, match_dir)
    report = narrator.generate(document)
    assert len(report.key_moments) > 0
    assert all(m.event_type in ("goal", "card") for m in report.key_moments)
    assert all(m.player_name is not None for m in report.key_moments)


def test_key_moments_are_chronologically_sorted(detective, mapper, translator, resolver, validator, narrator):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001"
    document = _document_for_match(detective, mapper, translator, resolver, validator, match_dir)
    report = narrator.generate(document)
    keys = [(m.period, m.minute) for m in report.key_moments]
    assert keys == sorted(keys)


def test_messy_tier_surfaces_low_confidence_note(detective, mapper, translator, resolver, validator, narrator):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001"
    document = _document_for_match(detective, mapper, translator, resolver, validator, match_dir)
    report = narrator.generate(document)
    assert any("confidence threshold" in n for n in report.data_quality_notes)


# --- Unit tests: team stats arithmetic ---

def test_pass_accuracy_computed_correctly():
    narrator = NarrativeGenerator(ai_enabled=False)
    events = [
        _event(event_id="1", event_type="pass", team="Home FC", outcome=True),
        _event(event_id="2", event_type="pass", team="Home FC", outcome=True),
        _event(event_id="3", event_type="pass", team="Home FC", outcome=False),
        _event(event_id="4", event_type="pass", team="Home FC", outcome=None),
    ]
    document = _minimal_document(events)
    report = narrator.generate(document)
    home = report.team_stats["Home FC"]
    assert home.passes == 4
    assert home.passes_completed == 2
    assert home.pass_accuracy == pytest.approx(0.5)


def test_goal_counts_as_both_goal_and_shot():
    narrator = NarrativeGenerator(ai_enabled=False)
    events = [_event(event_id="1", event_type="goal", team="Home FC", player_id=None)]
    document = _minimal_document(events)
    report = narrator.generate(document)
    home = report.team_stats["Home FC"]
    assert home.goals == 1
    assert home.shots == 1


def test_pass_accuracy_none_when_no_passes():
    s = TeamStats(team="X")
    assert s.pass_accuracy is None


# --- Unit tests: pass distance / difficulty ---

def test_pass_distance_m_computed_from_coordinates():
    # full pitch length pass: x=0 -> end_x=100, same y -> should be ~105m
    distance = _pass_distance_m(0.0, 50.0, 100.0, 50.0)
    assert distance == pytest.approx(105.0, abs=0.01)


def test_pass_distance_none_when_no_end_coordinates():
    assert _pass_distance_m(50.0, 50.0, None, None) is None


def test_pass_difficulty_bands():
    assert _pass_difficulty_band(5.0) == "short"
    assert _pass_difficulty_band(15.0) == "short"
    assert _pass_difficulty_band(20.0) == "medium"
    assert _pass_difficulty_band(30.0) == "medium"
    assert _pass_difficulty_band(50.0) == "long"


def test_team_stats_pass_distance_aggregation():
    narrator = NarrativeGenerator(ai_enabled=False)
    events = [
        _event(event_id="1", event_type="pass", team="Home FC", x=0.0, y=50.0, end_x=10.0, end_y=50.0),   # ~10.5m short
        _event(event_id="2", event_type="pass", team="Home FC", x=0.0, y=50.0, end_x=50.0, end_y=50.0),   # ~52.5m long
        _event(event_id="3", event_type="pass", team="Home FC", x=0.0, y=50.0, end_x=None, end_y=None),   # no distance data
    ]
    document = _minimal_document(events)
    report = narrator.generate(document)
    home = report.team_stats["Home FC"]
    assert home.passes_with_distance == 2
    assert home.short_passes == 1
    assert home.long_passes == 1
    assert home.avg_pass_distance_m == pytest.approx((10.5 + 52.5) / 2, abs=0.1)


def test_avg_pass_distance_none_when_no_distance_data():
    s = TeamStats(team="X")
    assert s.avg_pass_distance_m is None


def test_low_confidence_events_tracked_separately():
    narrator = NarrativeGenerator(ai_enabled=False)
    events = [
        _event(event_id="1", event_type="pass", team="Home FC", quality_confidence=0.9),
        _event(event_id="2", event_type="pass", team="Home FC", quality_confidence=0.3),
    ]
    document = _minimal_document(events)
    report = narrator.generate(document)
    home = report.team_stats["Home FC"]
    assert home.low_confidence_events == 1
    assert home.events_total == 2


def test_event_for_unknown_team_not_silently_dropped():
    """A team name Validator didn't expect still gets counted somewhere,
    rather than vanishing from the stat line."""
    narrator = NarrativeGenerator(ai_enabled=False)
    events = [_event(event_id="1", event_type="pass", team="Some Other Team")]
    document = _minimal_document(events)
    report = narrator.generate(document)
    assert sum(s.events_total for s in report.team_stats.values()) == 1


# --- Unit tests: key moments ---

def test_key_moments_extracted_and_sorted():
    narrator = NarrativeGenerator(ai_enabled=False)
    player = PlayerRecord(player_id="p1", name="Test Player", resolution_confidence=1.0)
    events = [
        _event(event_id="g2", event_type="goal", team="Home FC", period=2, timestamp=600.0, player_id="p1"),
        _event(event_id="g1", event_type="goal", team="Away FC", period=1, timestamp=300.0, player_id="p1"),
    ]
    document = _minimal_document(events, players=[player])
    report = narrator.generate(document)
    assert len(report.key_moments) == 2
    assert report.key_moments[0].event_type == "goal"
    assert report.key_moments[0].period == 1  # period-1 goal sorted before period-2 goal
    assert "Test Player" in report.key_moments[0].description


def test_key_moment_with_unresolved_player_id_handled_gracefully():
    narrator = NarrativeGenerator(ai_enabled=False)
    events = [_event(event_id="g1", event_type="goal", team="Home FC", player_id="unresolved::opta::Home FC::5")]
    document = _minimal_document(events, players=[])
    report = narrator.generate(document)
    assert len(report.key_moments) == 1
    assert "unresolved identity" in report.key_moments[0].description


# --- AI escalation: graceful no-key behavior (same contract as Agent 4) ---

def test_ai_enabled_without_api_key_falls_back_to_template(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    narrator = NarrativeGenerator(ai_enabled=True)
    document = _minimal_document([_event()])
    report = narrator.generate(document)
    assert report.generation_method == "template"
    assert any("ANTHROPIC_API_KEY not configured or call failed" in l for l in report.log)


def test_ai_disabled_flag_skips_cleanly_even_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    narrator = NarrativeGenerator(ai_enabled=False)
    document = _minimal_document([_event()])
    report = narrator.generate(document)
    assert report.generation_method == "template"
    assert any("AI narrative disabled by caller" in l for l in report.log)


def test_template_narrative_never_crashes_on_empty_events():
    narrator = NarrativeGenerator(ai_enabled=False)
    document = _minimal_document([])
    report = narrator.generate(document)
    assert report.narrative_text
    assert "0 - 0" in report.narrative_text
