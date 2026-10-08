"""Tests for backend/data/corruption.py -- the live, on-stage feed
corruption used for the resilience stress-test feature.

Each mutation is tied to a specific, already-tested fallback path in an
upstream agent (see corruption.py's own docstrings for the mapping). The
point of these tests is narrower than "does the pipeline still run": it's
that each mutation produces the SPECIFIC degraded-but-recovered signal it
claims to, not just "some lower number somewhere."
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "agents"))
sys.path.insert(0, str(ROOT / "backend" / "data" / "schemas"))
sys.path.insert(0, str(ROOT / "backend" / "data"))

from conftest import SAMPLE_MATCHES_DIR  # noqa: E402
from corruption import corrupt_opta_payload, MUTATIONS, MUTATION_DESCRIPTIONS  # noqa: E402
from orchestrator import MasterOrchestrator  # noqa: E402


@pytest.fixture
def clean_opta_data():
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json"
    return json.loads(path.read_text())


@pytest.fixture
def orchestrator():
    return MasterOrchestrator(ai_escalation_enabled=False)


def test_every_mutation_has_a_description():
    assert set(MUTATIONS) == set(MUTATION_DESCRIPTIONS)


def test_corrupt_does_not_mutate_the_original(clean_opta_data):
    original_snapshot = json.loads(json.dumps(clean_opta_data))  # deep copy for comparison
    corrupt_opta_payload(clean_opta_data, ["strip_player_identity"], rate=0.5, seed=1)
    assert clean_opta_data == original_snapshot


def test_unknown_mutation_raises():
    with pytest.raises(ValueError, match="unknown mutation"):
        corrupt_opta_payload({"Events": []}, ["not_a_real_mutation"], rate=0.2)


@pytest.mark.parametrize("rate", [0.0, 1.5, -0.1])
def test_invalid_rate_raises(rate):
    with pytest.raises(ValueError, match="rate must be"):
        corrupt_opta_payload({"Events": []}, ["drop_period"], rate=rate)


def test_seed_gives_reproducible_corruption(clean_opta_data):
    corrupted_a, log_a = corrupt_opta_payload(clean_opta_data, ["null_coordinates"], rate=0.3, seed=99)
    corrupted_b, log_b = corrupt_opta_payload(clean_opta_data, ["null_coordinates"], rate=0.3, seed=99)
    assert corrupted_a == corrupted_b
    assert log_a == log_b


def test_strip_player_identity_removes_jersey_numbers(clean_opta_data):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["strip_player_identity"], rate=1.0, seed=1)
    assert all("JerseyNumber" not in e for e in corrupted["Events"])


def test_null_coordinates_nulls_x_and_y(clean_opta_data):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["null_coordinates"], rate=1.0, seed=1)
    assert all(e["X"] is None and e["Y"] is None for e in corrupted["Events"])


def test_drop_period_removes_period_id(clean_opta_data):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["drop_period"], rate=1.0, seed=1)
    assert all("PeriodId" not in e for e in corrupted["Events"])


def test_drop_timestamps_removes_min_and_sec(clean_opta_data):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["drop_timestamps"], rate=1.0, seed=1)
    assert all("Min" not in e and "Sec" not in e for e in corrupted["Events"])


def test_garble_event_types_replaces_type_name(clean_opta_data):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["garble_event_types"], rate=1.0, seed=1)
    assert all(e["TypeName"] == "ZZZ_UNKNOWN_VENDOR_CODE" for e in corrupted["Events"])


# --- End-to-end: each mutation produces its claimed effect through the full pipeline ---

def test_strip_player_identity_forces_vendor_scoped_fallback(clean_opta_data, orchestrator):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["strip_player_identity"], rate=0.5, seed=3)
    result = orchestrator.run(corrupted)
    assert result.document is not None
    fallback_players = [p for p in result.document.players if p.resolution_confidence <= 0.2]
    assert len(fallback_players) > 0


def test_null_coordinates_drops_events_without_crashing(clean_opta_data, orchestrator):
    clean_result = orchestrator.run(clean_opta_data)
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["null_coordinates"], rate=0.3, seed=3)
    corrupt_result = orchestrator.run(corrupted)

    assert corrupt_result.orchestration_log.overall_status == "success"
    assert len(corrupt_result.document.events) < len(clean_result.document.events)
    assert any("dropped" in w for w in corrupt_result.document.metadata.warnings)


def test_drop_period_triggers_period_repair(clean_opta_data, orchestrator):
    corrupted, _ = corrupt_opta_payload(clean_opta_data, ["drop_period"], rate=0.5, seed=3)
    result = orchestrator.run(corrupted)
    assert result.document is not None
    assert any("period repair" in w for w in result.document.metadata.warnings)


def test_full_mutation_combo_degrades_confidence_but_still_succeeds(clean_opta_data, orchestrator):
    clean_result = orchestrator.run(clean_opta_data)
    corrupted, log = corrupt_opta_payload(clean_opta_data, list(MUTATIONS), rate=0.3, seed=42)
    corrupt_result = orchestrator.run(corrupted)

    assert len(log) == len(MUTATIONS)
    assert corrupt_result.orchestration_log.overall_status == "success"
    assert corrupt_result.document.metadata.quality_confidence < clean_result.document.metadata.quality_confidence
    # narrative still produced despite the corruption
    assert corrupt_result.narrative is not None
