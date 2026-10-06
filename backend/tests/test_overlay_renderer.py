"""Tests for Agent 9: Overlay Renderer.

Covers: the full presentation-layer pipeline produces a chronologically
sorted, machine-readable cue timeline; score-bug cues track the running
score correctly through every goal; goal/card cues land at the EXACT
same absolute second as their corresponding score-bug update (the
precision bug this agent's smoke test surfaced and narrative_generator /
explainability_engine were both fixed to carry exact timestamps for);
and the continuous match-clock math across periods.
"""

import json
import pytest
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper
from coordinate_translator import CoordinateTranslator
from entity_resolver import EntityResolver
from validator import Validator
from narrative_generator import NarrativeGenerator
from explainability_engine import ExplainabilityEngine, ExplainedMoment, ExplainabilityReport, MomentumShift, RhythmWindow
from overlay_renderer import OverlayRenderer, _absolute_second, PERIOD_LENGTH_SECONDS
from cdf_models import CDFDocument, CDFEvent, CDFMetadata, MatchInfo
from datetime import date, datetime, timezone


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


@pytest.fixture(scope="module")
def narrator():
    return NarrativeGenerator(ai_enabled=False)


@pytest.fixture(scope="module")
def explainer():
    return ExplainabilityEngine()


@pytest.fixture
def renderer():
    return OverlayRenderer()


def _single_vendor_pipeline(detective, mapper, translator, resolver, validator, narrator, explainer, match_dir, vendor="opta"):
    path = match_dir / f"{vendor}.json"
    data = json.loads(path.read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)
    resolved, registry = resolver.resolve(translated)
    document, _ = validator.build_document(resolved, registry, {detection.detected_format: data})
    narrative = narrator.generate(document)
    explainability = explainer.generate(document)
    return document, narrative, explainability


def _all_match_dirs():
    return [d for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_dirs()


def _minimal_document(events) -> CDFDocument:
    return CDFDocument(
        match=MatchInfo(match_id="m1", date=date(2026, 1, 1), home_team="Home FC", away_team="Away FC"),
        metadata=CDFMetadata(source_vendors=["opta"], quality_confidence=1.0,
                              generation_timestamp=datetime.now(timezone.utc)),
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


# --- Integration: full pipeline -> overlay timeline ---

@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_timeline_is_chronologically_sorted(detective, mapper, translator, resolver, validator, narrator, explainer, renderer, match_dir):
    document, narrative, explainability = _single_vendor_pipeline(
        detective, mapper, translator, resolver, validator, narrator, explainer, match_dir)
    timeline = renderer.render(document, narrative, explainability)
    seconds = [c.absolute_second for c in timeline.cues]
    assert seconds == sorted(seconds)


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_every_goal_has_matching_score_bug_at_same_second(detective, mapper, translator, resolver, validator, narrator, explainer, renderer, match_dir):
    document, narrative, explainability = _single_vendor_pipeline(
        detective, mapper, translator, resolver, validator, narrator, explainer, match_dir)
    timeline = renderer.render(document, narrative, explainability)
    goal_seconds = {c.absolute_second for c in timeline.cues if c.cue_type == "goal"}
    score_bug_seconds = {c.absolute_second for c in timeline.cues if c.cue_type == "score_bug"}
    for gs in goal_seconds:
        assert gs in score_bug_seconds, f"goal cue at {gs}s has no matching score_bug update at the same second"


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_final_score_bug_matches_goal_count(detective, mapper, translator, resolver, validator, narrator, explainer, renderer, match_dir):
    document, narrative, explainability = _single_vendor_pipeline(
        detective, mapper, translator, resolver, validator, narrator, explainer, match_dir)
    timeline = renderer.render(document, narrative, explainability)
    score_bugs = [c for c in timeline.cues if c.cue_type == "score_bug"]
    final = score_bugs[-1]
    home_goals = sum(1 for e in document.events if e.event_type == "goal" and e.team == document.match.home_team)
    away_goals = sum(1 for e in document.events if e.event_type == "goal" and e.team == document.match.away_team)
    assert final.payload["home_goals"] == home_goals
    assert final.payload["away_goals"] == away_goals


def test_json_export_is_well_formed(detective, mapper, translator, resolver, validator, narrator, explainer, renderer):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    document, narrative, explainability = _single_vendor_pipeline(
        detective, mapper, translator, resolver, validator, narrator, explainer, match_dir)
    timeline = renderer.render(document, narrative, explainability)
    payload = timeline.to_dict()
    json.dumps(payload)  # must be JSON-serializable, a hard requirement for a render-layer output
    assert payload["match_id"] == document.match.match_id
    assert len(payload["cues"]) == len(timeline.cues)


# --- Unit tests: absolute second math ---

def test_absolute_second_period_1_is_unchanged():
    assert _absolute_second(1, 100.0) == 100.0


def test_absolute_second_period_2_offset_by_period_length():
    assert _absolute_second(2, 50.0) == PERIOD_LENGTH_SECONDS + 50.0


# --- Unit tests: score bug progression ---

def test_score_bug_starts_at_zero_zero():
    renderer = OverlayRenderer()
    document = _minimal_document([])
    cues = renderer._score_bug_cues(document)
    assert len(cues) == 1
    assert cues[0].payload == {"home_team": "Home FC", "away_team": "Away FC", "home_goals": 0, "away_goals": 0}


def test_score_bug_increments_correct_team():
    renderer = OverlayRenderer()
    events = [
        _event(event_id="g1", event_type="goal", team="Home FC", timestamp=100.0),
        _event(event_id="g2", event_type="goal", team="Away FC", timestamp=200.0),
        _event(event_id="g3", event_type="goal", team="Home FC", timestamp=300.0),
    ]
    document = _minimal_document(events)
    cues = renderer._score_bug_cues(document)
    assert len(cues) == 4  # kickoff + 3 goals
    assert cues[-1].payload["home_goals"] == 2
    assert cues[-1].payload["away_goals"] == 1


def test_score_bug_cues_are_persistent():
    renderer = OverlayRenderer()
    events = [_event(event_id="g1", event_type="goal", team="Home FC")]
    document = _minimal_document(events)
    cues = renderer._score_bug_cues(document)
    assert all(c.duration_seconds is None for c in cues)


# --- Unit tests: moment and momentum cue construction ---

def test_moment_cue_for_goal_has_correct_type_and_duration():
    renderer = OverlayRenderer()
    explainability = ExplainabilityReport(
        windows=[], momentum_shifts=[],
        explained_moments=[ExplainedMoment(minute=10, period=1, second_in_period=600.0, event_type="goal", player_name="Test Scorer", player_jersey=9,
                                            team="Home FC", base_description="Goal for Home FC", context="came during control")],
    )
    cues = renderer._moment_cues(explainability)
    assert len(cues) == 1
    assert cues[0].cue_type == "goal"
    assert cues[0].duration_seconds == 8.0
    assert cues[0].absolute_second == 600.0


def test_moment_cue_for_card_has_correct_type_and_duration():
    renderer = OverlayRenderer()
    explainability = ExplainabilityReport(
        windows=[], momentum_shifts=[],
        explained_moments=[ExplainedMoment(minute=10, period=1, second_in_period=600.0, event_type="card", player_name="Test Defender", player_jersey=5,
                                            team="Home FC", base_description="Card for Home FC", context="chaotic")],
    )
    cues = renderer._moment_cues(explainability)
    assert cues[0].cue_type == "card"
    assert cues[0].duration_seconds == 5.0


def test_momentum_cue_built_from_shift():
    renderer = OverlayRenderer()
    window = RhythmWindow(period=1, window_index=1, start_second=300.0, end_second=600.0,
                           dominant_team="Away FC", control_share=0.6, total_events=10)
    shift = MomentumShift(period=1, from_window_index=0, to_window_index=1, from_team="Home FC", to_team="Away FC",
                           from_share=0.6, to_share=0.6, description="Momentum swung...")
    explainability = ExplainabilityReport(windows=[window], momentum_shifts=[shift], explained_moments=[])
    cues = renderer._momentum_cues(explainability)
    assert len(cues) == 1
    assert cues[0].cue_type == "momentum_shift"
    assert cues[0].absolute_second == 300.0
    assert cues[0].payload["to_team"] == "Away FC"


def test_renderer_never_crashes_on_empty_inputs(renderer):
    document = _minimal_document([])
    narrative = NarrativeGenerator(ai_enabled=False).generate(document)
    explainability = ExplainabilityEngine().generate(document)
    timeline = renderer.render(document, narrative, explainability)
    assert timeline.cues == [] or len(timeline.cues) == 1  # may still have the kickoff score_bug
    assert timeline.match_id == "m1"
