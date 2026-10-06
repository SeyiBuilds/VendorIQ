"""Tests for Agent 8: Explainability Engine.

Covers: rhythm-window construction and classification (control/contested/
chaotic), momentum-shift detection between consecutive windows, and
moment-level "why it matters" context for goals/cards -- directly
exercising the hackathon brief's explicit "control vs. chaos, tactical
pressure changes, and game rhythm patterns" requirement.
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
from explainability_engine import (
    ExplainabilityEngine, _classify_rhythm, _build_windows, _detect_momentum_shifts,
    _window_for_event, RhythmWindow, CONTROL_SHARE_THRESHOLD, CHAOS_RATIO_THRESHOLD,
)
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
def engine():
    return ExplainabilityEngine()


def _single_vendor_document(detective, mapper, translator, resolver, validator, match_dir, vendor="opta") -> CDFDocument:
    """Realistic single-feed ingestion -- one vendor file, not a merge of
    all three (merging would triple-count events for stats purposes)."""
    path = match_dir / f"{vendor}.json"
    data = json.loads(path.read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)
    resolved, registry = resolver.resolve(translated)
    document, _ = validator.build_document(resolved, registry, {detection.detected_format: data})
    return document


def _all_match_dirs():
    return [d for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_dirs()


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
        player_id=None, team="Home FC", x=50.0, y=50.0,
        outcome=None, quality_confidence=1.0, vendor_source="opta", metadata={},
    )
    defaults.update(overrides)
    return CDFEvent(**defaults)


# --- Integration: full single-vendor pipeline -> explainability ---

@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_full_pipeline_produces_windows_covering_all_events(detective, mapper, translator, resolver, validator, engine, match_dir):
    document = _single_vendor_document(detective, mapper, translator, resolver, validator, match_dir)
    report = engine.generate(document)
    assert len(report.windows) > 0
    total_in_windows = sum(w.total_events for w in report.windows)
    assert total_in_windows == len(document.events)


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_every_window_has_a_valid_rhythm_label(detective, mapper, translator, resolver, validator, engine, match_dir):
    document = _single_vendor_document(detective, mapper, translator, resolver, validator, match_dir)
    report = engine.generate(document)
    for w in report.windows:
        assert w.rhythm_label in ("control", "contested", "chaotic")


@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_explained_moments_cover_every_goal_and_card(detective, mapper, translator, resolver, validator, engine, match_dir):
    document = _single_vendor_document(detective, mapper, translator, resolver, validator, match_dir)
    report = engine.generate(document)
    goal_card_count = sum(1 for e in document.events if e.event_type in ("goal", "card"))
    assert len(report.explained_moments) == goal_card_count
    for em in report.explained_moments:
        assert em.context  # never an empty explanation


# --- Unit tests: rhythm classification ---

def test_classify_rhythm_control():
    assert _classify_rhythm(control_share=0.8, chaos_ratio=0.1) == "control"


def test_classify_rhythm_contested():
    assert _classify_rhythm(control_share=0.5, chaos_ratio=0.1) == "contested"


def test_classify_rhythm_chaotic_overrides_control():
    """Even with a high dominant-team share, a foul-heavy window should be
    labeled chaotic, not control -- a stop-start five minutes isn't what
    a viewer means by 'control'."""
    assert _classify_rhythm(control_share=0.9, chaos_ratio=0.5) == "chaotic"


def test_classify_rhythm_boundary_exact_threshold():
    assert _classify_rhythm(control_share=CONTROL_SHARE_THRESHOLD, chaos_ratio=0.0) == "control"
    assert _classify_rhythm(control_share=0.0, chaos_ratio=CHAOS_RATIO_THRESHOLD) == "chaotic"


# --- Unit tests: window building ---

def test_build_windows_splits_by_period_and_time():
    events = [
        _event(event_id="1", period=1, timestamp=10.0, team="Home FC"),
        _event(event_id="2", period=1, timestamp=310.0, team="Away FC"),  # next 5-min window
        _event(event_id="3", period=2, timestamp=10.0, team="Home FC"),
    ]
    document = _minimal_document(events)
    windows = _build_windows(document)
    period1_windows = [w for w in windows if w.period == 1]
    period2_windows = [w for w in windows if w.period == 2]
    assert len(period1_windows) == 2  # window 0 (0-300s) and window 1 (300-600s)
    assert len(period2_windows) == 1


def test_window_dominant_team_and_control_share():
    events = [
        _event(event_id=str(i), period=1, timestamp=float(i), team="Home FC") for i in range(8)
    ] + [
        _event(event_id=f"a{i}", period=1, timestamp=float(i), team="Away FC") for i in range(2)
    ]
    document = _minimal_document(events)
    windows = _build_windows(document)
    w = windows[0]
    assert w.total_events == 10
    assert w.dominant_team == "Home FC"
    assert w.control_share == pytest.approx(0.8)
    assert w.rhythm_label == "control"


def test_window_chaos_ratio_computed_from_foul_tackle_card():
    events = [
        _event(event_id="1", event_type="foul", team="Home FC"),
        _event(event_id="2", event_type="tackle", team="Home FC"),
        _event(event_id="3", event_type="card", team="Home FC"),
        _event(event_id="4", event_type="pass", team="Home FC"),
    ]
    document = _minimal_document(events)
    windows = _build_windows(document)
    assert windows[0].chaos_events == 3
    assert windows[0].chaos_ratio == pytest.approx(0.75)
    assert windows[0].rhythm_label == "chaotic"


def test_empty_window_has_no_dominant_team():
    """A window with zero events (match had a lull) shouldn't crash or
    fabricate a dominant team."""
    events = [_event(event_id="1", period=1, timestamp=10.0)]  # window 0 only
    document = _minimal_document(events)
    windows = _build_windows(document)
    # only one window should exist since max timestamp is 10.0
    assert len(windows) == 1


# --- Unit tests: momentum shift detection ---

def test_momentum_shift_detected_between_strong_windows():
    w1 = RhythmWindow(period=1, window_index=0, start_second=0, end_second=300,
                       dominant_team="Home FC", control_share=0.7, total_events=10)
    w2 = RhythmWindow(period=1, window_index=1, start_second=300, end_second=600,
                       dominant_team="Away FC", control_share=0.65, total_events=10)
    shifts = _detect_momentum_shifts([w1, w2])
    assert len(shifts) == 1
    assert shifts[0].from_team == "Home FC"
    assert shifts[0].to_team == "Away FC"


def test_no_shift_when_same_team_dominates_consecutive_windows():
    w1 = RhythmWindow(period=1, window_index=0, start_second=0, end_second=300,
                       dominant_team="Home FC", control_share=0.7, total_events=10)
    w2 = RhythmWindow(period=1, window_index=1, start_second=300, end_second=600,
                       dominant_team="Home FC", control_share=0.8, total_events=10)
    assert _detect_momentum_shifts([w1, w2]) == []


def test_no_shift_when_dominance_too_weak():
    """Below MOMENTUM_SHIFT_MIN_SHARE, a change in dominant team is noise,
    not a real momentum swing worth flagging."""
    w1 = RhythmWindow(period=1, window_index=0, start_second=0, end_second=300,
                       dominant_team="Home FC", control_share=0.51, total_events=10)
    w2 = RhythmWindow(period=1, window_index=1, start_second=300, end_second=600,
                       dominant_team="Away FC", control_share=0.52, total_events=10)
    assert _detect_momentum_shifts([w1, w2]) == []


def test_no_shift_across_different_periods():
    """Window indices reset each period -- a 'shift' comparison should
    never compare period 1's last window to period 2's first."""
    w1 = RhythmWindow(period=1, window_index=5, start_second=1500, end_second=1800,
                       dominant_team="Home FC", control_share=0.7, total_events=10)
    w2 = RhythmWindow(period=2, window_index=0, start_second=0, end_second=300,
                       dominant_team="Away FC", control_share=0.7, total_events=10)
    assert _detect_momentum_shifts([w1, w2]) == []


# --- Unit tests: moment explanation ---

def test_goal_against_the_run_of_play_explained():
    player = PlayerRecord(player_id="p1", name="Scorer", team="Away FC", resolution_confidence=1.0)
    events = [
        _event(event_id=str(i), period=1, timestamp=float(i), team="Home FC") for i in range(8)
    ] + [
        _event(event_id="goal1", period=1, timestamp=5.0, team="Away FC", event_type="goal", player_id="p1")
    ]
    document = _minimal_document(events, players=[player])
    engine = ExplainabilityEngine()
    report = engine.generate(document)
    goal_moment = next(em for em in report.explained_moments if em.event_type == "goal")
    assert "against the run of play" in goal_moment.context


def test_goal_during_control_spell_explained():
    events = [
        _event(event_id=str(i), period=1, timestamp=float(i), team="Home FC") for i in range(9)
    ] + [
        _event(event_id="goal1", period=1, timestamp=5.0, team="Home FC", event_type="goal")
    ]
    document = _minimal_document(events)
    engine = ExplainabilityEngine()
    report = engine.generate(document)
    goal_moment = next(em for em in report.explained_moments if em.event_type == "goal")
    assert "control spell" in goal_moment.context


def test_card_during_chaotic_window_explained():
    events = [
        _event(event_id="1", event_type="foul", team="Home FC"),
        _event(event_id="2", event_type="tackle", team="Home FC"),
        _event(event_id="3", event_type="foul", team="Away FC"),
        _event(event_id="card1", event_type="card", team="Home FC", timestamp=2.0),
    ]
    document = _minimal_document(events)
    engine = ExplainabilityEngine()
    report = engine.generate(document)
    card_moment = next(em for em in report.explained_moments if em.event_type == "card")
    assert "chaotic" in card_moment.context


def test_window_for_event_lookup():
    events = [_event(event_id="1", period=1, timestamp=10.0)]
    document = _minimal_document(events)
    windows = _build_windows(document)
    found = _window_for_event(windows, period=1, timestamp=10.0)
    assert found is not None
    assert found.window_index == 0
    assert _window_for_event(windows, period=99, timestamp=10.0) is None


def test_explainability_engine_never_crashes_on_empty_document():
    engine = ExplainabilityEngine()
    document = _minimal_document([])
    report = engine.generate(document)
    assert report.windows == []
    assert report.momentum_shifts == []
    assert report.explained_moments == []
    assert report.log
