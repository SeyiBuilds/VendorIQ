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
from orchestrator import MasterOrchestrator, OrchestrationLog, CRITICAL_STAGES, OPTIONAL_STAGES, STAGE_ORDER


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
    # match_sheet_mapper gets its own assertion below -- no match-sheet
    # payload was supplied here, so it's "skipped", not "success".
    for stage in CRITICAL_STAGES + ("narrative_generator",):
        assert stage_statuses[stage] == "success"
    assert stage_statuses["match_sheet_mapper"] == "skipped"


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

ALL_STAGES = STAGE_ORDER


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
    # No match-sheet payload supplied -- that one stage is "skipped" by
    # design, not a failure; every other stage succeeds.
    for r in stage_records:
        assert r.status == ("skipped" if r.stage == "match_sheet_mapper" else "success")


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


# --- Match-sheet wiring: a separate, optional payload run() can take ---

def _opta_match_sheet_data(match_name="PL_2025_CLEAN_001"):
    path = SAMPLE_MATCHES_DIR / match_name / "match_sheets" / "opta_match_sheet.json"
    return json.loads(path.read_text())


def test_match_sheet_data_attaches_to_document_when_supplied(orchestrator):
    result = orchestrator.run(_opta_vendor_data(), match_sheet_data=_opta_match_sheet_data())
    assert result.orchestration_log.overall_status == "success"
    assert result.document.match_sheet is not None
    assert result.document.match_sheet.home.team == "Liverpool"
    record = next(r for r in result.orchestration_log.records if r.stage == "match_sheet_mapper")
    assert record.status == "success"


def test_match_sheet_mapper_skipped_without_a_payload(orchestrator):
    result = orchestrator.run(_opta_vendor_data())
    assert result.orchestration_log.overall_status == "success"
    assert result.document.match_sheet is None
    record = next(r for r in result.orchestration_log.records if r.stage == "match_sheet_mapper")
    assert record.status == "skipped"


def test_match_sheet_mapper_failure_degrades_to_partial_not_abort(orchestrator, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated match_sheet_mapper bug")
    monkeypatch.setattr(orchestrator.mapper, "map_match_sheet", boom)

    result = orchestrator.run(_opta_vendor_data(), match_sheet_data=_opta_match_sheet_data())
    assert result.orchestration_log.overall_status == "partial"
    assert result.document is not None  # critical path still produced output
    assert result.document.match_sheet is None
    record = next(r for r in result.orchestration_log.records if r.stage == "match_sheet_mapper")
    assert record.status == "failed"


# --- Multi-vendor combine: "primary feed + enrichment" (run_streaming_multi) ---

def _statsbomb_vendor_data(match_name="PL_2025_MESSY_001"):
    path = SAMPLE_MATCHES_DIR / match_name / "statsbomb.json"
    return json.loads(path.read_text())


def _run_multi(orchestrator, payloads, primary_index=0, **kwargs):
    result = None
    for kind, payload in orchestrator.run_streaming_multi(payloads, primary_index=primary_index, **kwargs):
        if kind == "result":
            result = payload
    return result


def test_multi_vendor_events_are_not_double_counted(orchestrator):
    """The whole point of 'primary + enrichment' over naive concatenation:
    combining opta (primary) with statsbomb (secondary) must produce the
    SAME event count as running opta alone -- statsbomb's events inform
    player-identity resolution but never become CDFEvents themselves."""
    opta = _opta_vendor_data("PL_2025_MESSY_001")
    statsbomb = _statsbomb_vendor_data("PL_2025_MESSY_001")

    solo = orchestrator.run(opta)
    combined = _run_multi(orchestrator, [opta, statsbomb], primary_index=0)

    assert combined.orchestration_log.overall_status == "success"
    assert len(combined.document.events) == len(solo.document.events)
    assert combined.document.metadata.source_vendors == ["opta"]


def test_multi_vendor_primary_index_selects_which_file_is_the_event_stream(orchestrator):
    opta = _opta_vendor_data("PL_2025_MESSY_001")
    statsbomb = _statsbomb_vendor_data("PL_2025_MESSY_001")

    primary_statsbomb = _run_multi(orchestrator, [opta, statsbomb], primary_index=1)
    solo_statsbomb = orchestrator.run(statsbomb)

    assert len(primary_statsbomb.document.events) == len(solo_statsbomb.document.events)
    assert primary_statsbomb.document.metadata.source_vendors == ["statsbomb"]


def test_multi_vendor_recovers_match_info_from_secondary_vendor(orchestrator):
    """A believable real case: the primary feed carries only the event
    stream, no match header. The secondary vendor's file should still let
    Validator's existing cross-vendor match-info fallback fill the gap --
    this was already built into validator.py, just never reachable because
    run()/run_streaming() only ever passed one vendor's raw data in."""
    import copy
    opta_stripped = copy.deepcopy(_opta_vendor_data("PL_2025_MESSY_001"))
    for k in ("HomeTeam", "AwayTeam", "MatchId"):
        opta_stripped.pop(k, None)
    statsbomb = _statsbomb_vendor_data("PL_2025_MESSY_001")

    result = _run_multi(orchestrator, [opta_stripped, statsbomb], primary_index=0)

    assert result.document.match.home_team == "Manchester United"
    assert result.document.match.away_team == "Tottenham"
    assert result.document.match.match_id == "PL_2025_MESSY_001"


def test_multi_vendor_metadata_notes_which_vendors_were_combined(orchestrator):
    opta = _opta_vendor_data("PL_2025_MESSY_001")
    statsbomb = _statsbomb_vendor_data("PL_2025_MESSY_001")
    result = _run_multi(orchestrator, [opta, statsbomb], primary_index=0)

    assert any("multi-vendor combine" in w and "statsbomb" in w for w in result.document.metadata.warnings)


def test_multi_vendor_rejects_empty_payload_list(orchestrator):
    result = _run_multi(orchestrator, [], primary_index=0)
    assert result.orchestration_log.overall_status == "failed"
    assert result.document is None


def test_multi_vendor_rejects_out_of_range_primary_index(orchestrator):
    opta = _opta_vendor_data("PL_2025_MESSY_001")
    statsbomb = _statsbomb_vendor_data("PL_2025_MESSY_001")
    result = _run_multi(orchestrator, [opta, statsbomb], primary_index=5)
    assert result.orchestration_log.overall_status == "failed"


def test_multi_vendor_single_payload_still_produces_a_valid_document(orchestrator):
    """Not the intended use (the API layer requires 2+), but the
    orchestrator method itself should degrade gracefully to a normal
    single-vendor run rather than erroring on a 1-item list."""
    opta = _opta_vendor_data("PL_2025_MESSY_001")
    result = _run_multi(orchestrator, [opta], primary_index=0)
    assert result.orchestration_log.overall_status == "success"
    assert result.document.metadata.source_vendors == ["opta"]
    assert not any("multi-vendor combine" in w for w in result.document.metadata.warnings)
