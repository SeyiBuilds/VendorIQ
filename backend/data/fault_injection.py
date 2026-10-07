"""
Agent-level fault injection, for demoing the MASTER ORCHESTRATOR's own
resilience (retry-then-recover, retry-then-abort, retry-then-degrade) --
distinct from corruption.py's field-level data corruption.

Why this is a separate module: every mutation in corruption.py is
deliberately engineered to be absorbed by an agent's own per-field
fallback logic (a missing jersey number, a null coordinate) and never
raise an exception -- see that module's docstring. That's the right
behavior for "watch an agent handle bad data gracefully," but it means
none of those mutations ever reach orchestrator.py's own try/retry/abort
machinery (_run_critical_stage / _run_optional_stage). If the demo goal
is specifically "watch the Master Orchestrator catch a real failure and
decide what to do about it," corruption.py has nothing that reaches that
code path at all.

This module injects an ACTUAL failure -- a monkeypatched agent method
that raises -- so the orchestrator's real retry-once-then-decide logic
runs on camera: one failed attempt that self-heals on retry, or two
failed attempts that genuinely abort (critical stage) or degrade to
"partial" (optional stage). Every fault is loudly labeled in its own
exception message as a simulated demo failure, so it can never be
mistaken for an organic bug in a recording, a log, or a bug report.
"""

from __future__ import annotations

import contextlib
from typing import Optional

# fault_name -> (attribute name on MasterOrchestrator, method name to
# patch, the STAGE_ORDER name it corresponds to for display/validation).
# Deliberately covers both a critical stage (aborts the run on genuine
# failure) and optional stages (degrade to "partial", run still returns
# a usable document) -- the orchestrator treats these differently and a
# demo should be able to show both outcomes.
AGENT_FAULTS: dict[str, tuple[str, str, str]] = {
    "schema_mapper": ("mapper", "map_events", "schema_mapper"),
    "coordinate_translator": ("translator", "translate", "coordinate_translator"),
    "entity_resolver": ("resolver", "resolve", "entity_resolver"),
    "validator": ("validator", "build_document", "validator"),
    "match_sheet_mapper": ("mapper", "map_match_sheet", "match_sheet_mapper"),
    "narrative_generator": ("narrator", "generate", "narrative_generator"),
}

FAULT_DESCRIPTIONS: dict[str, str] = {
    "schema_mapper": "critical stage -- 2 failures abort the run with no usable document; 1 failure self-heals on retry",
    "coordinate_translator": "critical stage -- 2 failures abort the run with no usable document; 1 failure self-heals on retry",
    "entity_resolver": "critical stage -- 2 failures abort the run with no usable document; 1 failure self-heals on retry",
    "validator": "critical stage -- 2 failures abort the run with no usable document; 1 failure self-heals on retry",
    "match_sheet_mapper": "optional stage -- 2 failures degrade to 'partial' (document still produced, no match sheet); 1 failure self-heals",
    "narrative_generator": "optional stage -- 2 failures degrade to 'partial' (document still produced, no narrative); 1 failure self-heals",
}


class SimulatedAgentFailure(RuntimeError):
    """Raised only by inject_fault() below, never by real agent code --
    distinguishable on sight from a genuine bug."""


@contextlib.contextmanager
def inject_fault(orchestrator, fault_name: str, fail_attempts: int = 2):
    """Monkeypatches ONE agent method on this orchestrator instance to
    raise SimulatedAgentFailure for the first `fail_attempts` calls, then
    restores its original behavior for any call after that -- matching
    _run_critical_stage's real one-retry-then-decide logic:
      fail_attempts=1 -> the first call fails, the automatic retry
                          succeeds -- the run completes normally. This is
                          "the orchestrator catches it and recovers."
      fail_attempts=2 -> both the first call AND the retry fail -- the
                          orchestrator logs it as genuinely failed and
                          either aborts (critical stage) or degrades to
                          "partial" (optional stage). This is "the
                          orchestrator catches it and makes a call."
    Always restores the original method on exit, even if the caller
    never exhausts the generator (e.g. an early return).
    """
    if fault_name not in AGENT_FAULTS:
        raise ValueError(f"unknown fault '{fault_name}'; supported: {sorted(AGENT_FAULTS)}")
    if fail_attempts not in (1, 2):
        raise ValueError(f"fail_attempts must be 1 or 2 (matches the orchestrator's real retry count), got {fail_attempts}")

    attr_name, method_name, _ = AGENT_FAULTS[fault_name]
    target = getattr(orchestrator, attr_name)
    original = getattr(target, method_name)
    calls = {"count": 0}

    def faulty(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] <= fail_attempts:
            raise SimulatedAgentFailure(
                f"SIMULATED FAILURE (demo fault injection, not a real bug): "
                f"'{fault_name}' call #{calls['count']} of {fail_attempts} configured to fail"
            )
        return original(*args, **kwargs)

    setattr(target, method_name, faulty)
    try:
        yield
    finally:
        setattr(target, method_name, original)


@contextlib.contextmanager
def maybe_inject_fault(orchestrator, fault_name: Optional[str], fail_attempts: int = 2):
    """Convenience wrapper: a no-op context manager when fault_name is
    None, so callers don't need an if/else around the `with` statement."""
    if fault_name is None:
        yield
        return
    with inject_fault(orchestrator, fault_name, fail_attempts):
        yield
