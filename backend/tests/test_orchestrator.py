"""Tests for the Master Orchestrator.

Covers: the full single-vendor pipeline succeeds end to end for every
sample match, the critical-vs-optional stage split (a critical stage
failure aborts the run with nothing usable; an optional stage failure is
logged, skipped, and the run still returns everything that DID succeed),
the one-retry-then-fail behavior, the on-demand export() method not being
part of run()'s critical path, and run_streaming()'s real-time event
sequence (what the API's /stream endpoints and the frontend's live
pipeline view are both built on top of).
"""

import json
import pytest
from conftest import SAMPLE_MATCHES_DIR
from orchestrator import MasterOrchestrator, OrchestrationLog, CRITICAL_STAGES, OPTIONAL_STAGES


@pytest.fixture
def orchestrator():
    return MasterOrchestrator(ai_escalation_enabled=False)


def _opta_vendor_data(match_name="PL_2025_CLEAN_001"):
    path = SAMPLE_MATCHES_DIR / match_name / "opta.json"
    return json.loads(path.read_text())


def _all_match_names():
    return [d.name for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_names()


# --- Integration: full pipeline, all sample matches ---

@pytest.mark.parametrize("match_name", ALL_MATCHES)
def test_full_run_succeeds_for_every_sample_match(orchestrator, match_name):
    result = orchestrator.run(_opta_vendor_data(match_name))
    assert result.orchestration_log.overall_status == "success"
    assert result.document is not None
    assert result.narrative is not None
    assert len(result.document.events) > 0


@pytest.mark.parametrize("match_name", ALL_MATCHES)
def test_every_stage_recorded_as_success(orchestrator, match_name):
    result = orchestrator.run(_opta_vendor_data(match_name))
    stage_statuses = {r.stage: r.status for r in result.orchestration_log.records}
    for stage in CRITICAL_STAGES + OPTIONAL_STAGES:
        assert stage_statuses[stage] == "success"


def test_orchestration_log_ref_set_on_document(orchestrator):
    result = orchestrator.run(_opta_vendor_data())
    assert result.document.metadata.orchestration_log_ref == result.orchestration_log.run_id


def test_run_id_is_unique_per_run(orchestrator):
    r1 = orchestrator.run(_opta_vendor_data())
    r2 = orchestrator.run(_opta_vendor_data())
    assert r1.orchestration_log.run_id != r2.orchestration_log.run_id


# --- Critical stage failure: abort immediately, nothing usable ---

def test_critical_stage_failure_aborts_run(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated schema_mapper bug")
    monkeypatch.setattr(orchestrator.mapper, "map_events", boom)

    result = orchestrator.run(_opta_vendor_data())
    assert result.orchestration_log.overall_status == "failed"
    assert result.document is None
    assert result.narrative is None


def test_critical_stage_failure_does_not_run_later_stages(orchestrator, monkeypatch):
    calls = []
    original_translate = orchestrator.translator.translate

    def boom(*args, **kwargs):
        raise RuntimeError("simulated coordinate_translator bug")

    def track_resolver(*args, **kwargs):
        calls.append("entity_resolver")
        raise AssertionError("entity_resolver should never be called after coordinate_translator fails")

    monkeypatch.setattr(orchestrator.translator, "translate", boom)
    monkeypatch.setattr(orchestrator.resolver, "resolve", track_resolver)

    result = orchestrator.run(_opta_vendor_data())
    assert calls == []
    assert result.document is None


def test_critical_stage_retries_once_before_failing(orchestrator, monkeypatch):
    attempts = {"count": 0}

    def flaky(*args, **kwargs):
        attempts["count"] += 1
        raise RuntimeError("transient failure")

    monkeypatch.setattr(orchestrator.mapper, "map_events", flaky)
    result = orchestrator.run(_opta_vendor_data())
    assert attempts["count"] == 2  # original attempt + one retry
    assert result.orchestration_log.overall_status == "failed"
    record = next(r for r in result.orchestration_log.records if r.stage == "schema_mapper")
    assert record.status == "failed"
    assert record.retried is True


def test_critical_stage_succeeds_on_retry(orchestrator, monkeypatch):
    attempts = {"count": 0}
    original = orchestrator.mapper.map_events

    def flaky_once(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator.mapper, "map_events", flaky_once)
    result = orchestrator.run(_opta_vendor_data())
    assert result.orchestration_log.overall_status == "success"
    record = next(r for r in result.orchestration_log.records if r.stage == "schema_mapper")
    assert record.status == "success"
    assert record.retried is True


# --- Optional stage failure: log, skip, continue with the rest ---

def test_optional_stage_failure_does_not_abort_run(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated narrative_generator bug")
    monkeypatch.setattr(orchestrator.narrator, "generate", boom)

    result = orchestrator.run(_opta_vendor_data())
    assert result.orchestration_log.overall_status == "partial"
    assert result.document is not None  # the critical path's output is still returned
    assert result.narrative is None
    record = next(r for r in result.orchestration_log.records if r.stage == "narrative_generator")
    assert record.status == "failed"


# --- On-demand methods: not part of run()'s critical path ---

def test_export_works_after_successful_run(orchestrator):
    result = orchestrator.run(_opta_vendor_data())
    payload, report = orchestrator.export(result.document, "statsbomb")
    assert report.events_exported == len(result.document.events)
    assert payload["match_id"] == result.document.match.match_id


def test_export_not_called_during_run(orchestrator, monkeypatch):
    calls = []
    original_export = orchestrator.exporter.export

    def track(*args, **kwargs):
        calls.append(True)
        return original_export(*args, **kwargs)

    monkeypatch.setattr(orchestrator.exporter, "export", track)
    orchestrator.run(_opta_vendor_data())
    assert calls == []  # export is on-demand only, never invoked by run()


# --- run_streaming(): the real-time event sequence run() is built on ---

ALL_STAGES = CRITICAL_STAGES + OPTIONAL_STAGES


def test_streaming_success_emits_start_then_stage_for_every_stage_then_result(orchestrator):
    events = list(orchestrator.run_streaming(_opta_vendor_data()))

    assert events[-1][0] == "result"
    result = events[-1][1]
    assert result.orchestration_log.overall_status == "success"

    stage_events = events[:-1]
    # every stage must appear as a ("start", name) immediately followed by
    # a ("stage", StageRecord) for that same name, in pipeline order
    expected_starts = [("start", name) for name in ALL_STAGES]
    actual_starts = [e for e in stage_events if e[0] == "start"]
    assert actual_starts == expected_starts

    stage_records = [e[1] for e in stage_events if e[0] == "stage"]
    assert [r.stage for r in stage_records] == list(ALL_STAGES)
    assert all(r.status == "success" for r in stage_records)


def test_streaming_matches_run_result(orchestrator):
    vendor_data = _opta_vendor_data()
    streamed_result = list(orchestrator.run_streaming(vendor_data))[-1][1]
    direct_result = orchestrator.run(vendor_data)
    assert streamed_result.orchestration_log.overall_status == direct_result.orchestration_log.overall_status
    assert len(streamed_result.document.events) == len(direct_result.document.events)


def test_streaming_critical_failure_stops_immediately_with_no_later_starts(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated schema_mapper bug")
    monkeypatch.setattr(orchestrator.mapper, "map_events", boom)

    events = list(orchestrator.run_streaming(_opta_vendor_data()))

    started = [e[1] for e in events if e[0] == "start"]
    assert started == ["format_detective", "schema_mapper"]  # never reaches coordinate_translator etc.

    result = events[-1][1]
    assert events[-1][0] == "result"
    assert result.orchestration_log.overall_status == "failed"
    assert result.document is None


def test_streaming_optional_failure_still_emits_result_with_narrative_none(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated narrative_generator bug")
    monkeypatch.setattr(orchestrator.narrator, "generate", boom)

    events = list(orchestrator.run_streaming(_opta_vendor_data()))
    narrative_stage = next(e[1] for e in events if e[0] == "stage" and e[1].stage == "narrative_generator")
    assert narrative_stage.status == "failed"

    result = events[-1][1]
    assert result.orchestration_log.overall_status == "partial"
    assert result.document is not None
    assert result.narrative is None
