"""Tests for backend/data/fault_injection.py -- the orchestrator-level
"make it genuinely fail" demo feature, as opposed to corruption.py's
field-level data corruption.

These tests exercise inject_fault()/maybe_inject_fault() directly against
a real MasterOrchestrator + real sample match data, independent of the
API layer (backend/tests/test_api.py covers the HTTP wiring). The point
is narrower than "does the pipeline still run": it's that the injected
fault reaches the SPECIFIC retry/abort/degrade branch it claims to, and
that the monkeypatch is always restored afterward.
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
from fault_injection import (  # noqa: E402
    AGENT_FAULTS,
    FAULT_DESCRIPTIONS,
    SimulatedAgentFailure,
    inject_fault,
    maybe_inject_fault,
)
from orchestrator import MasterOrchestrator, CRITICAL_STAGES, OPTIONAL_STAGES  # noqa: E402


@pytest.fixture
def clean_opta_data():
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json"
    return json.loads(path.read_text())


@pytest.fixture
def orchestrator():
    return MasterOrchestrator()


def test_every_registered_fault_maps_to_a_real_stage():
    """AGENT_FAULTS and FAULT_DESCRIPTIONS must agree on their keys, and
    every stage name they advertise must be either critical or optional
    in the orchestrator's own stage classification -- a typo here would
    silently make a "critical" fault actually degrade, or vice versa."""
    assert set(AGENT_FAULTS) == set(FAULT_DESCRIPTIONS)
    for fault_name, (_, _, stage_name) in AGENT_FAULTS.items():
        assert stage_name in CRITICAL_STAGES or stage_name in OPTIONAL_STAGES, fault_name


def test_every_fault_target_exists_on_a_fresh_orchestrator(orchestrator):
    """Every (attribute, method) pair AGENT_FAULTS references must
    actually resolve on a real orchestrator instance, or inject_fault
    would raise AttributeError instead of doing anything useful."""
    for fault_name, (attr_name, method_name, _) in AGENT_FAULTS.items():
        target = getattr(orchestrator, attr_name)
        assert callable(getattr(target, method_name)), fault_name


def test_inject_fault_rejects_unknown_fault_name(orchestrator):
    with pytest.raises(ValueError):
        with inject_fault(orchestrator, "not_a_real_fault"):
            pass


@pytest.mark.parametrize("bad_attempts", [0, 3, -1])
def test_inject_fault_rejects_fail_attempts_outside_one_or_two(orchestrator, bad_attempts):
    with pytest.raises(ValueError):
        with inject_fault(orchestrator, "schema_mapper", fail_attempts=bad_attempts):
            pass


def test_maybe_inject_fault_is_a_true_no_op_when_fault_name_is_none(orchestrator, clean_opta_data):
    """The convenience wrapper must not change behavior at all when no
    fault is requested -- a plain clean run through it."""
    with maybe_inject_fault(orchestrator, None):
        result = orchestrator.run(clean_opta_data)
    assert result.orchestration_log.overall_status == "success"


def test_inject_fault_raises_simulated_failure_from_the_patched_method(orchestrator):
    attr_name, method_name, _ = AGENT_FAULTS["schema_mapper"]
    with inject_fault(orchestrator, "schema_mapper", fail_attempts=2):
        target = getattr(orchestrator, attr_name)
        with pytest.raises(SimulatedAgentFailure):
            getattr(target, method_name)()


def _underlying_func(bound_or_plain):
    # Bound methods aren't `is`-identical across separate getattr() calls
    # even when nothing changed (a fresh bound-method wrapper each time),
    # so compare the underlying function instead; the monkeypatch itself
    # is assigned as a plain function (not bound), so normalize both.
    return getattr(bound_or_plain, "__func__", bound_or_plain)


def test_inject_fault_restores_the_original_method_on_exit(orchestrator):
    attr_name, method_name, _ = AGENT_FAULTS["schema_mapper"]
    target = getattr(orchestrator, attr_name)
    original_func = _underlying_func(getattr(target, method_name))
    with inject_fault(orchestrator, "schema_mapper", fail_attempts=2):
        assert _underlying_func(getattr(target, method_name)) is not original_func
    assert _underlying_func(getattr(target, method_name)) is original_func


def test_inject_fault_restores_original_method_even_on_exception(orchestrator):
    """The restoration must happen via `finally`, not just on a clean
    exit -- a run that aborts mid-pipeline must not leave the agent
    permanently broken for the next request."""
    attr_name, method_name, _ = AGENT_FAULTS["schema_mapper"]
    target = getattr(orchestrator, attr_name)
    original_func = _underlying_func(getattr(target, method_name))
    with pytest.raises(RuntimeError):
        with inject_fault(orchestrator, "schema_mapper", fail_attempts=2):
            raise RuntimeError("something else went wrong mid-run")
    assert _underlying_func(getattr(target, method_name)) is original_func


def test_one_failure_self_heals_on_the_orchestrators_automatic_retry(orchestrator, clean_opta_data):
    with maybe_inject_fault(orchestrator, "schema_mapper", fail_attempts=1):
        result = orchestrator.run(clean_opta_data)
    assert result.orchestration_log.overall_status == "success"
    record = next(r for r in result.orchestration_log.records if r.stage == "schema_mapper")
    assert record.retried is True
    assert record.status == "success"


def test_two_failures_on_a_critical_stage_abort_the_run(orchestrator, clean_opta_data):
    with maybe_inject_fault(orchestrator, "schema_mapper", fail_attempts=2):
        result = orchestrator.run(clean_opta_data)
    assert result.orchestration_log.overall_status == "failed"
    assert result.document is None


def test_two_failures_on_an_optional_stage_degrade_instead_of_abort(orchestrator, clean_opta_data):
    with maybe_inject_fault(orchestrator, "narrative_generator", fail_attempts=2):
        result = orchestrator.run(clean_opta_data)
    assert result.orchestration_log.overall_status == "partial"
    assert result.document is not None


def test_fault_is_scoped_to_one_run_not_the_orchestrator_instance(orchestrator, clean_opta_data):
    """After the `with` block ends, a fresh run against the SAME
    orchestrator instance must behave normally again."""
    with maybe_inject_fault(orchestrator, "schema_mapper", fail_attempts=2):
        faulted = orchestrator.run(clean_opta_data)
    assert faulted.orchestration_log.overall_status == "failed"

    clean = orchestrator.run(clean_opta_data)
    assert clean.orchestration_log.overall_status == "success"
