"""Fact check for AI-written narratives (backend/agents/narrative_fact_check.py).

Every number an LLM narrative cites must come from the stats it was given.
A narrative that cites one that does not is rejected for the template.
"""

import json

import pytest

import narrative_generator
from conftest import SAMPLE_MATCHES_DIR
from narrative_fact_check import check_narrative, numbers_in
from narrative_generator import NarrativeGenerator
from orchestrator import MasterOrchestrator


# --- check_narrative on its own ---

def test_numbers_cited_from_the_source_pass():
    result = check_narrative("Arsenal had 12 shots and 54% of the ball.", "- Arsenal: 12 shots, 54% share")
    assert result.passed
    assert result.checked == 2


def test_a_number_missing_from_the_source_is_rejected():
    result = check_narrative("Arsenal had 15 shots.", "- Arsenal: 12 shots")
    assert not result.passed
    assert result.unsupported == ["15"]


def test_rounding_a_source_number_is_not_a_hallucination():
    source = "- Arsenal: 18.47m avg distance"
    assert check_narrative("passes averaged 18m", source).passed
    assert check_narrative("passes averaged 18.5m", source).passed
    assert not check_narrative("passes averaged 21m", source).passed


def test_number_words_are_checked_too():
    source = "- Arsenal: 2 goals"
    assert check_narrative("Arsenal scored two goals.", source).passed
    result = check_narrative("Arsenal scored seven goals.", source)
    assert result.unsupported == ["seven"]


def test_one_in_ordinary_prose_is_not_read_as_a_stat():
    assert numbers_in("one of the better halves") == []


def test_digits_inside_identifiers_are_not_numbers():
    assert numbers_in("PL_2025_CLEAN_001") == []


# --- wired into Narrative Generator ---

@pytest.fixture(scope="module")
def document():
    data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json").read_text())
    return MasterOrchestrator(ai_escalation_enabled=False).run(data).document


def _faithful_narrative(document) -> str:
    stats = NarrativeGenerator(ai_enabled=False).generate(document).team_stats
    home, away = stats[document.match.home_team], stats[document.match.away_team]
    return (f"{home.team} took {home.shots} shots to {away.team}'s {away.shots}, "
            f"and the score finished {home.goals}-{away.goals}.")


def _llm_returns(monkeypatch, text):
    monkeypatch.setattr(narrative_generator, "_call_llm_for_narrative", lambda *args: (text, "groq"))


def test_a_faithful_ai_narrative_is_kept(monkeypatch, document):
    _llm_returns(monkeypatch, _faithful_narrative(document))
    report = NarrativeGenerator(ai_enabled=True).generate(document)
    assert report.generation_method == "ai_enhanced"
    assert report.rejected_numbers == []
    assert any("fact check passed" in line for line in report.log)


def test_an_ai_narrative_with_an_invented_stat_falls_back_to_the_template(monkeypatch, document):
    _llm_returns(monkeypatch, _faithful_narrative(document) + " They also forced 947 corners.")
    report = NarrativeGenerator(ai_enabled=True).generate(document)
    assert report.generation_method == "template"
    assert report.rejected_numbers == ["947"]
    assert "947" not in report.narrative_text
    assert any("rejected" in line and "947" in line for line in report.log)
