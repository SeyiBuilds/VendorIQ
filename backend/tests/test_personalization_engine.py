"""Tests for Agent 10: Personalization Engine.

Covers: analyst/casual_fan/player_focus modes genuinely differ in content
depth, favorite_club biasing/framing, multi-language phrase translation
(including the unsupported-language fallback), player-focus matching by
BOTH name and jersey number (jersey is often the only identifier
available -- single-vendor Opta ingestion never carries names), and the
honest "no per-player stats computed" disclosure rather than fabricated
numbers.
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
from narrative_generator import NarrativeGenerator, NarrativeReport, TeamStats
from explainability_engine import ExplainabilityEngine, ExplainabilityReport, ExplainedMoment
from overlay_renderer import OverlayRenderer, OverlayTimeline, OverlayCue
from personalization_engine import (
    PersonalizationEngine, PersonalizationProfile, LANGUAGE_PHRASES, DEFAULT_LANGUAGE,
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


@pytest.fixture(scope="module")
def narrator():
    return NarrativeGenerator(ai_enabled=False)


@pytest.fixture(scope="module")
def explainer():
    return ExplainabilityEngine()


@pytest.fixture(scope="module")
def renderer():
    return OverlayRenderer()


@pytest.fixture
def personalizer():
    return PersonalizationEngine()


def _full_single_vendor_pipeline(detective, mapper, translator, resolver, validator, narrator, explainer, renderer, match_dir, vendor="opta"):
    path = match_dir / f"{vendor}.json"
    data = json.loads(path.read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)
    resolved, registry = resolver.resolve(translated)
    document, _ = validator.build_document(resolved, registry, {detection.detected_format: data})
    narrative = narrator.generate(document)
    explainability = explainer.generate(document)
    timeline = renderer.render(document, narrative, explainability)
    return document, narrative, explainability, timeline


def _minimal_fixture(home="Home FC", away="Away FC"):
    document = CDFDocument(
        match=MatchInfo(match_id="m1", date=date(2026, 1, 1), home_team=home, away_team=away),
        metadata=CDFMetadata(source_vendors=["opta"], quality_confidence=1.0,
                              generation_timestamp=datetime.now(timezone.utc)),
        players=[], events=[],
    )
    narrative = NarrativeReport(
        narrative_text=f"{home} vs {away}: final score {home} 2 - 1 {away}. Data quality: some caveat here.",
        generation_method="template",
        team_stats={
            home: TeamStats(team=home, goals=2, shots=5, passes=10, passes_completed=8),
            away: TeamStats(team=away, goals=1, shots=3, passes=9, passes_completed=6),
        },
        key_moments=[],
    )
    explainability = ExplainabilityReport(
        windows=[], momentum_shifts=[],
        explained_moments=[
            ExplainedMoment(minute=10, period=1, second_in_period=600.0, event_type="goal",
                             team=home, player_name="Home Scorer", player_jersey=9,
                             base_description=f"Goal for {home}", context="came during control"),
            ExplainedMoment(minute=20, period=1, second_in_period=1200.0, event_type="card",
                             team=away, player_name=None, player_jersey=5,
                             base_description=f"Card for {away}", context="chaotic"),
            ExplainedMoment(minute=30, period=1, second_in_period=1800.0, event_type="goal",
                             team=away, player_name=None, player_jersey=11,
                             base_description=f"Goal for {away}", context="against the run of play"),
        ],
    )
    timeline = OverlayTimeline(
        match_id="m1",
        cues=[
            OverlayCue(cue_id="score_bug_0", cue_type="score_bug", period=1, minute=0, second_in_period=0.0,
                       absolute_second=0.0, duration_seconds=None, priority=4, title="0-0", subtitle=None, team=None),
            OverlayCue(cue_id="goal_0", cue_type="goal", period=1, minute=10, second_in_period=600.0,
                       absolute_second=600.0, duration_seconds=8.0, priority=1, title="GOAL", subtitle=None, team=home),
            OverlayCue(cue_id="card_0", cue_type="card", period=1, minute=20, second_in_period=1200.0,
                       absolute_second=1200.0, duration_seconds=5.0, priority=2, title="CARD", subtitle=None, team=away),
            OverlayCue(cue_id="momentum_0", cue_type="momentum_shift", period=1, minute=15, second_in_period=900.0,
                       absolute_second=900.0, duration_seconds=6.0, priority=3, title="MOMENTUM", subtitle=None, team=home),
        ],
    )
    return document, narrative, explainability, timeline


def _all_match_dirs():
    return [d for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_dirs()


# --- Integration: full pipeline for all 3 modes ---

@pytest.mark.parametrize("mode", ["analyst", "casual_fan", "player_focus"])
def test_full_pipeline_produces_experience_for_each_mode(detective, mapper, translator, resolver, validator,
                                                           narrator, explainer, renderer, personalizer, mode):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    document, narrative, explainability, timeline = _full_single_vendor_pipeline(
        detective, mapper, translator, resolver, validator, narrator, explainer, renderer, match_dir)
    profile = PersonalizationProfile(mode=mode, favorite_player_jersey=9, favorite_club=document.match.home_team)
    experience = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert experience.mode == mode
    assert experience.headline


# --- Mode behavior differences ---

def test_analyst_sees_full_stats_and_all_moments(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="analyst")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert "avg_pass_distance_m" in exp.stats_view["Home FC"]
    assert len(exp.moments) == 3  # 2 goals + 1 card
    assert len(exp.overlay_cues) == 4  # every cue, unfiltered
    assert "Data quality:" in exp.narrative_text


def test_casual_fan_sees_trimmed_stats_and_only_goals(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="casual_fan")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert set(exp.stats_view["Home FC"].keys()) == {"goals", "shots"}
    assert len(exp.moments) == 2  # cards dropped, goals kept
    assert all(m.headline.upper().startswith("GOAL") for m in exp.moments)
    assert "Data quality:" not in exp.narrative_text


def test_casual_fan_drops_momentum_shift_cues(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="casual_fan")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    cue_types = {c.cue_type for c in exp.overlay_cues}
    assert "momentum_shift" not in cue_types
    assert cue_types == {"score_bug", "goal"}


def test_player_focus_filters_to_matching_player_by_jersey(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="player_focus", favorite_player_jersey=9, favorite_club="Home FC")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert len(exp.moments) == 1
    assert exp.moments[0].team == "Home FC"


def test_player_focus_jersey_requires_team_to_disambiguate(personalizer):
    """Jersey 9 could exist on either team -- without favorite_club, a
    jersey-only match is ambiguous and must not silently pick one side."""
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="player_focus", favorite_player_jersey=9)  # no favorite_club
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert len(exp.moments) == 0  # jersey match with no club to disambiguate -> no match, not a guess


def test_player_focus_matches_by_name_when_available(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="player_focus", favorite_player_name="Home Scorer")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert len(exp.moments) == 1
    assert exp.moments[0].player_name == "Home Scorer"


def test_player_focus_no_identifier_set_returns_nothing_and_logs(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="player_focus")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.moments == []
    assert any("neither favorite_player_name nor favorite_player_jersey" in l for l in exp.log)


def test_player_focus_discloses_missing_per_player_stats(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="player_focus", favorite_player_jersey=9, favorite_club="Home FC")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert any("per-player stat breakdown is not computed" in l for l in exp.log)
    assert set(exp.stats_view.keys()) == {"Home FC"}  # team-level context only, not fabricated player numbers
    assert "avg_pass_distance_m" in exp.stats_view["Home FC"]


def test_player_focus_no_club_means_no_team_context(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="player_focus", favorite_player_name="Home Scorer")  # no favorite_club
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.stats_view == {}


# --- favorite_club behavior ---

def test_favorite_club_prefixes_headline(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="analyst", favorite_club="Home FC")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.headline.startswith("Your team, Home FC:")


def test_favorite_club_moments_sorted_first(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="analyst", favorite_club="Away FC")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.moments[0].team == "Away FC"
    assert exp.moments[0].is_favorite_club is True


def test_no_favorite_club_leaves_headline_unprefixed(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="analyst")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert not exp.headline.startswith("Your team")


# --- Multi-language ---

def test_spanish_phrases_applied(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="analyst", language="es")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.language == "es"
    goal_moment = next(m for m in exp.moments if "Scorer" in m.headline)
    assert "GOL" in goal_moment.headline


def test_unsupported_language_falls_back_to_english(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="analyst", language="de")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.language == DEFAULT_LANGUAGE
    assert any("not in LANGUAGE_PHRASES" in l for l in exp.log)
    goal_moment = next(m for m in exp.moments if "Scorer" in m.headline)
    assert goal_moment.headline.startswith("GOAL")


def test_all_registered_languages_have_the_same_phrase_keys():
    """Registry consistency: every language must support every phrase key,
    or a translated run could KeyError on a language that's missing one."""
    key_sets = {lang: set(phrases) for lang, phrases in LANGUAGE_PHRASES.items()}
    reference = key_sets[DEFAULT_LANGUAGE]
    for lang, keys in key_sets.items():
        assert keys == reference, f"language '{lang}' phrase keys {keys} don't match '{DEFAULT_LANGUAGE}' {reference}"


# --- Invalid mode handling ---

def test_invalid_mode_falls_back_to_casual_fan(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    profile = PersonalizationProfile(mode="not_a_real_mode")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.mode == "casual_fan"
    assert any("unknown mode" in l for l in exp.log)


def test_never_crashes_on_empty_document(personalizer):
    document, narrative, explainability, timeline = _minimal_fixture()
    document = CDFDocument(
        match=document.match,
        metadata=document.metadata,
        players=[], events=[],
    )
    narrative = NarrativeReport(narrative_text="", generation_method="template", team_stats={}, key_moments=[])
    explainability = ExplainabilityReport()
    timeline = OverlayTimeline(match_id="m1")
    profile = PersonalizationProfile(mode="analyst")
    exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
    assert exp.moments == []
    assert exp.overlay_cues == []
