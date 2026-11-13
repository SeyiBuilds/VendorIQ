"""Optional TraceRoot tracing (backend/agents/tracing.py).

Tracing must be a no-op without TRACEROOT_API_KEY, and when it is on, a
stage that fails once and succeeds on retry must show both attempts.
Spans go to an in-memory exporter here; nothing is sent anywhere.
"""

import json

import pytest

import tracing
from conftest import SAMPLE_MATCHES_DIR
from orchestrator import MasterOrchestrator


def _sample_opta() -> dict:
    return json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json").read_text())


def _flaky_translator(orchestrator: MasterOrchestrator) -> None:
    """Make Coordinate Translator fail on its first call only."""
    real_translate = orchestrator.translator.translate
    calls = {"n": 0}

    def translate(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient failure")
        return real_translate(events)

    orchestrator.translator.translate = translate


_exporter = None


@pytest.fixture
def spans(monkeypatch):
    """Turn tracing on against an in-memory exporter for one test."""
    global _exporter
    pytest.importorskip("traceroot")
    if _exporter is None:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        _exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
    monkeypatch.delenv("TRACEROOT_API_KEY", raising=False)
    monkeypatch.setattr(tracing, "_enabled", True)
    _exporter.clear()
    yield _exporter
    _exporter.clear()


def test_tracing_is_off_without_api_key(monkeypatch):
    monkeypatch.delenv("TRACEROOT_API_KEY", raising=False)
    monkeypatch.setattr(tracing, "_enabled", None)

    assert tracing.tracing_enabled() is False
    assert tracing.run_in_span("stage", lambda: 42) == 42

    result = MasterOrchestrator(ai_escalation_enabled=False).run(_sample_opta())
    assert result.orchestration_log.overall_status == "success"


def test_retried_stage_records_both_attempts(spans):
    orchestrator = MasterOrchestrator(ai_escalation_enabled=False)
    _flaky_translator(orchestrator)

    result = orchestrator.run(_sample_opta())

    assert result.orchestration_log.overall_status == "success"
    attempts = sorted(
        (s for s in spans.get_finished_spans() if s.name == "coordinate_translator"),
        key=lambda s: s.start_time,
    )
    assert len(attempts) == 2
    assert attempts[0].status.status_code.name == "ERROR"
    assert attempts[1].status.status_code.name != "ERROR"


@pytest.mark.xfail(
    strict=False,
    reason="traceroot <= 0.5.1 does not make a generator's span current, so stage "
           "spans under run_streaming() start their own traces",
)
def test_pipeline_run_is_one_trace(spans):
    MasterOrchestrator(ai_escalation_enabled=False).run(_sample_opta())

    finished = spans.get_finished_spans()
    run = next(s for s in finished if s.name == "pipeline_run")
    stages = [s for s in finished if s.name != "pipeline_run"]
    assert stages
    assert {s.context.trace_id for s in finished} == {run.context.trace_id}
    assert all(s.parent is not None and s.parent.span_id == run.context.span_id for s in stages)
