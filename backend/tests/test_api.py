"""Tests for the FastAPI layer over the Master Orchestrator.

These use FastAPI's TestClient against the real app -- no mocking of the
pipeline itself, since the pipeline already has its own test suites
(test_orchestrator.py and each agent's own tests). What this file checks
is the HTTP-specific plumbing: request/response shapes, status codes for
each error path, and that run_id-based state (RUN_STORE) actually lets a
later export call reuse an earlier run.
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "api"))

from main import app, RUN_STORE  # noqa: E402


@pytest.fixture
def client():
    RUN_STORE.clear()
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_matches(client):
    resp = client.get("/matches")
    assert resp.status_code == 200
    matches = resp.json()
    match_ids = {m["match_id"] for m in matches}
    assert {"PL_2025_CLEAN_001", "PL_2025_MESSY_001", "PL_2025_PARTIAL_001"} <= match_ids


def test_run_match_success(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "opta"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "success"
    assert body["match_id"] == "PL_2025_CLEAN_001"
    assert body["quality_tier"] == "clean"
    assert len(body["document"]["events"]) > 0
    assert body["narrative"]["narrative_text"]
    assert body["run_id"] in RUN_STORE


def test_run_match_defaults_to_opta_vendor(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run", json={})
    assert resp.status_code == 200
    assert resp.json()["overall_status"] == "success"


def test_run_unknown_match_404(client):
    resp = client.post("/matches/NOPE/run", json={"vendor": "opta"})
    assert resp.status_code == 404


def test_run_unknown_vendor_400(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "sportradar"})
    assert resp.status_code == 400


def test_get_run_round_trips(client):
    run_resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "opta"})
    run_id = run_resp.json()["run_id"]

    get_resp = client.get(f"/runs/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["run_id"] == run_id
    assert get_resp.json()["overall_status"] == "success"


def test_get_unknown_run_404(client):
    resp = client.get("/runs/not-a-real-run-id")
    assert resp.status_code == 404


def test_export_after_run(client):
    run_resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "opta"})
    run_id = run_resp.json()["run_id"]

    export_resp = client.post(f"/runs/{run_id}/export", json={"target_vendor": "statsbomb"})
    assert export_resp.status_code == 200
    body = export_resp.json()
    assert body["report"]["target_vendor"] == "statsbomb"
    assert body["report"]["events_exported"] == len(run_resp.json()["document"]["events"])
    assert body["payload"]["match_id"] == "PL_2025_CLEAN_001"


def test_export_unknown_run_404(client):
    resp = client.post("/runs/not-a-real-run-id/export", json={"target_vendor": "statsbomb"})
    assert resp.status_code == 404


# --- Streaming endpoints: live stage-by-stage progress as NDJSON ---

def _parse_ndjson(response_text):
    import json
    return [json.loads(line) for line in response_text.strip().split("\n") if line.strip()]


def test_run_stream_emits_start_and_stage_per_agent_then_one_result(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run/stream", json={"vendor": "opta"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    events = _parse_ndjson(resp.text)
    types = [e["type"] for e in events]
    assert types.count("result") == 1
    assert types[-1] == "result"
    assert types.count("start") == types.count("stage") == 7  # 5 critical + match_sheet_mapper + narrative_generator

    result_event = events[-1]
    assert result_event["data"]["overall_status"] == "success"
    assert result_event["data"]["match_id"] == "PL_2025_CLEAN_001"
    assert result_event["data"]["run_id"] in RUN_STORE


def test_run_stream_unknown_vendor_400(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run/stream", json={"vendor": "sportradar"})
    assert resp.status_code == 400


def test_run_stream_unknown_match_404(client):
    resp = client.post("/matches/NOPE/run/stream", json={"vendor": "opta"})
    assert resp.status_code == 404


def test_run_stream_result_can_be_exported(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run/stream", json={"vendor": "opta"})
    run_id = _parse_ndjson(resp.text)[-1]["data"]["run_id"]

    export_resp = client.post(f"/runs/{run_id}/export", json={"target_vendor": "statsbomb"})
    assert export_resp.status_code == 200


def _opta_bundle(match_id="PL_2025_CLEAN_001"):
    import json
    return json.loads((ROOT / "backend" / "data" / "sample_matches" / match_id / "opta.json").read_text())


def test_ingest_stream_runs_uploaded_opta_feed_successfully(client):
    resp = client.post("/ingest/run/stream", json=_opta_bundle())
    assert resp.status_code == 200
    events = _parse_ndjson(resp.text)
    result_event = events[-1]
    assert result_event["type"] == "result"
    assert result_event["data"]["overall_status"] == "success"
    assert result_event["data"]["match_id"] is None  # no sample-match bookkeeping for uploaded data
    assert result_event["data"]["run_id"] in RUN_STORE


def test_ingest_stream_result_can_be_exported(client):
    resp = client.post("/ingest/run/stream", json=_opta_bundle())
    run_id = _parse_ndjson(resp.text)[-1]["data"]["run_id"]

    export_resp = client.post(f"/runs/{run_id}/export", json={"target_vendor": "wyscout"})
    assert export_resp.status_code == 200


def test_ingest_stream_unrecognized_shape_degrades_honestly_not_500(client):
    # Not a crash, and not fabricated data either: Format Detective's own
    # documented fallback (PRD 4.1) is to tag the vendor "unknown" and let
    # the rest of the pipeline run structurally, landing on a
    # zero-confidence, zero-event document with explicit warnings --
    # rather than either a 500 or invented stats.
    resp = client.post("/ingest/run/stream", json={"this": "is not a recognized vendor shape"})
    assert resp.status_code == 200
    result_event = _parse_ndjson(resp.text)[-1]
    data = result_event["data"]
    assert data["overall_status"] == "success"
    assert data["document"]["metadata"]["quality_confidence"] == 0.0
    assert data["document"]["events"] == []
    assert any("fallback" in w for w in data["document"]["metadata"]["warnings"])


def test_ingest_stream_rejects_empty_body(client):
    resp = client.post("/ingest/run/stream", json={})
    assert resp.status_code == 400


def test_ingest_stream_rejects_array_of_non_dicts(client):
    # A bare JSON array is accepted (see the StatsBomb-shaped array tests
    # below), but every element has to actually be an event object.
    resp = client.post("/ingest/run/stream", json=["not", "an", "object"])
    assert resp.status_code == 400


def test_ingest_stream_rejects_empty_array(client):
    resp = client.post("/ingest/run/stream", json=[])
    assert resp.status_code == 400


def test_ingest_stream_rejects_scalar_body(client):
    resp = client.post("/ingest/run/stream", json="just a string")
    assert resp.status_code == 400


# --- Bare JSON array body: real StatsBomb open-data event files are shaped
# this way (a top-level list of event objects, no wrapper object) ---

def _statsbomb_bare_event_array():
    return [
        {
            "id": "e1", "type": {"name": "Pass"}, "timestamp": "00:00:01.000", "period": 1,
            "team": {"name": "Home FC"}, "player": {"id": 10, "name": "Someone"},
            "location": [50.0, 40.0], "pass": {"end_location": [60.0, 45.0], "outcome": {"name": "Complete"}},
        },
        {
            "id": "e2", "type": {"name": "Shot"}, "timestamp": "00:00:05.000", "period": 1,
            "team": {"name": "Home FC"}, "player": {"id": 11, "name": "Someone Else"},
            "location": [88.0, 40.0],
        },
    ]


def test_ingest_stream_accepts_bare_event_array(client):
    resp = client.post("/ingest/run/stream", json=_statsbomb_bare_event_array())
    assert resp.status_code == 200
    result = _parse_ndjson(resp.text)[-1]
    assert result["type"] == "result"
    assert result["data"]["overall_status"] == "success"
    assert len(result["data"]["document"]["events"]) == 2
    # Format Detective now recognizes StatsBomb's real nested-dict event
    # shape directly (no top-level match_id/home_team/away_team needed, see
    # format_detective._looks_like_real_statsbomb_event), so this isn't just
    # "didn't crash" -- it's actually extracted real values, not a 0%-
    # confidence empty document via the generic-vendor fallback.
    assert result["data"]["document"]["metadata"]["quality_confidence"] > 0.0


# --- Match sheet: auto-attached for sample matches, opt-in for uploads ---

def _opta_match_sheet_bundle(match_id="PL_2025_CLEAN_001"):
    import json
    return json.loads(
        (ROOT / "backend" / "data" / "sample_matches" / match_id / "match_sheets" / "opta_match_sheet.json").read_text()
    )


def test_run_attaches_match_sheet_for_sample_match(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "opta"})
    assert resp.status_code == 200
    sheet = resp.json()["document"]["match_sheet"]
    assert sheet is not None
    assert sheet["home"]["team"] == "Liverpool"


def test_run_stream_attaches_match_sheet_for_sample_match(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/run/stream", json={"vendor": "opta"})
    result = _parse_ndjson(resp.text)[-1]
    sheet = result["data"]["document"]["match_sheet"]
    assert sheet is not None
    assert sheet["away"]["team"] == "Manchester City"


def test_ingest_stream_without_match_sheet_data_leaves_it_none(client):
    resp = client.post("/ingest/run/stream", json=_opta_bundle())
    result = _parse_ndjson(resp.text)[-1]
    assert result["data"]["document"]["match_sheet"] is None


def test_ingest_stream_with_match_sheet_data_sibling_key_attaches_it(client):
    payload = _opta_bundle()
    payload["match_sheet_data"] = _opta_match_sheet_bundle()
    resp = client.post("/ingest/run/stream", json=payload)
    assert resp.status_code == 200
    result = _parse_ndjson(resp.text)[-1]
    assert result["data"]["overall_status"] == "success"
    sheet = result["data"]["document"]["match_sheet"]
    assert sheet is not None
    assert sheet["home"]["team"] == "Liverpool"


def test_corrupt_and_run_stream_success(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run/stream",
        json={"vendor": "opta", "mutations": ["null_coordinates", "drop_period"], "rate": 0.3, "seed": 7},
    )
    assert resp.status_code == 200
    events = _parse_ndjson(resp.text)
    result_event = events[-1]
    assert result_event["type"] == "result"
    assert len(result_event["data"]["corruption_log"]) == 2
    assert result_event["data"]["document"]["metadata"]["quality_confidence"] < 1.0


def test_corrupt_and_run_stream_rejects_unknown_mutation(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run/stream",
        json={"mutations": ["not_a_real_mutation"]},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("match_id,tier", [
    ("PL_2025_MESSY_001", "messy"),
    ("PL_2025_PARTIAL_001", "partial"),
])
def test_run_lower_quality_tiers_still_succeed_with_degraded_confidence(client, match_id, tier):
    resp = client.post(f"/matches/{match_id}/run", json={"vendor": "opta"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["quality_tier"] == tier
    assert body["document"]["metadata"]["quality_confidence"] < 1.0


# --- Live corruption endpoints (the resilience stress-test feature) ---

def test_list_mutations(client):
    resp = client.get("/mutations")
    assert resp.status_code == 200
    body = resp.json()
    assert "strip_player_identity" in body
    assert "null_coordinates" in body


def test_corrupt_and_run_success(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"vendor": "opta", "mutations": ["null_coordinates", "drop_period"], "rate": 0.3, "seed": 7},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "success"
    assert len(body["corruption_log"]) == 2
    assert body["document"]["metadata"]["quality_confidence"] < 1.0
    assert body["run_id"] in RUN_STORE


def test_corrupt_and_run_is_reproducible_with_seed(client):
    resp_a = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": ["strip_player_identity"], "rate": 0.4, "seed": 123},
    )
    resp_b = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": ["strip_player_identity"], "rate": 0.4, "seed": 123},
    )
    conf_a = resp_a.json()["document"]["metadata"]["quality_confidence"]
    conf_b = resp_b.json()["document"]["metadata"]["quality_confidence"]
    assert conf_a == conf_b


def test_corrupt_and_run_rejects_non_opta_vendor(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"vendor": "statsbomb", "mutations": ["drop_period"]},
    )
    assert resp.status_code == 400


def test_corrupt_and_run_rejects_unknown_mutation(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": ["not_a_real_mutation"]},
    )
    assert resp.status_code == 400


def test_corrupt_and_run_unknown_match_404(client):
    resp = client.post(
        "/matches/NOPE/corrupt-and-run",
        json={"mutations": ["drop_period"]},
    )
    assert resp.status_code == 404


def test_corrupt_and_run_result_can_be_exported(client):
    run_resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": ["garble_event_types"], "rate": 0.2, "seed": 5},
    )
    run_id = run_resp.json()["run_id"]

    export_resp = client.post(f"/runs/{run_id}/export", json={"target_vendor": "statsbomb"})
    assert export_resp.status_code == 200


# --- Agent fault injection (the "watch the orchestrator decide" demo feature) ---
# Distinct from the mutation tests above: these make an agent genuinely
# raise so MasterOrchestrator's own retry/abort/degrade logic fires, rather
# than exercising an agent's own graceful per-field fallback logic.

def test_list_agent_faults(client):
    resp = client.get("/agent-faults")
    assert resp.status_code == 200
    body = resp.json()
    assert "schema_mapper" in body
    assert "match_sheet_mapper" in body


def test_corrupt_and_run_rejects_unknown_agent_fault(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": [], "agent_fault": "not_a_real_fault"},
    )
    assert resp.status_code == 400


def test_corrupt_and_run_rejects_bad_fail_attempts(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": [], "agent_fault": "schema_mapper", "fail_attempts": 3},
    )
    assert resp.status_code == 400


def test_corrupt_and_run_agent_fault_self_heals_on_one_failure(client):
    """fail_attempts=1 means the orchestrator's single automatic retry
    succeeds -- the run completes normally with a real document."""
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": [], "agent_fault": "schema_mapper", "fail_attempts": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "success"
    assert body["document"]["events"]
    # the stage log shows the retry actually happened
    schema_mapper_record = next(r for r in body["orchestration_log"]["records"] if r["stage"] == "schema_mapper")
    assert schema_mapper_record["retried"] is True
    assert schema_mapper_record["status"] == "success"


def test_corrupt_and_run_agent_fault_aborts_critical_stage_on_two_failures(client):
    """fail_attempts=2 exhausts the retry too -- a critical-stage fault
    aborts the whole run with no usable document."""
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": [], "agent_fault": "schema_mapper", "fail_attempts": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "failed"
    assert body["document"] is None


def test_corrupt_and_run_agent_fault_degrades_optional_stage_on_two_failures(client):
    """An optional-stage fault still returns a usable document, just
    marked 'partial' -- it degrades instead of aborting."""
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": [], "agent_fault": "narrative_generator", "fail_attempts": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "partial"
    assert body["document"] is not None


def test_corrupt_and_run_agent_fault_restores_original_method_after_run(client):
    """The monkeypatch must not leak into the next request against the
    same (module-level, reused) orchestrator instance."""
    faulted = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": [], "agent_fault": "schema_mapper", "fail_attempts": 2},
    )
    assert faulted.json()["overall_status"] == "failed"

    clean = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": []},
    )
    assert clean.json()["overall_status"] == "success"


def test_corrupt_and_run_agent_fault_combines_with_data_mutations(client):
    """agent_fault and mutations are independent and stack: the corrupted
    data still flows through once the orchestrator's retry recovers from
    the injected fault."""
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run",
        json={"mutations": ["null_coordinates"], "rate": 0.3, "seed": 7, "agent_fault": "schema_mapper", "fail_attempts": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_status"] == "success"
    assert len(body["corruption_log"]) == 1


def test_corrupt_and_run_stream_agent_fault_abort_has_no_document(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run/stream",
        json={"mutations": [], "agent_fault": "coordinate_translator", "fail_attempts": 2},
    )
    assert resp.status_code == 200
    events = _parse_ndjson(resp.text)
    result_event = events[-1]
    assert result_event["type"] == "result"
    assert result_event["data"]["overall_status"] == "failed"
    assert result_event["data"]["document"] is None


def test_corrupt_and_run_stream_rejects_unknown_agent_fault(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/corrupt-and-run/stream",
        json={"mutations": [], "agent_fault": "not_a_real_fault"},
    )
    assert resp.status_code == 400


# --- Multi-vendor combine: POST /ingest/run/stream/multi ---

def _opta_sample(match_id="PL_2025_MESSY_001"):
    import json
    return json.loads(((ROOT / "backend" / "data" / "sample_matches") / match_id / "opta.json").read_text())


def _statsbomb_sample(match_id="PL_2025_MESSY_001"):
    import json
    return json.loads(((ROOT / "backend" / "data" / "sample_matches") / match_id / "statsbomb.json").read_text())


def test_ingest_multi_rejects_fewer_than_two_payloads(client):
    resp = client.post("/ingest/run/stream/multi", json={"vendor_payloads": [_opta_sample()]})
    assert resp.status_code == 400


def test_ingest_multi_rejects_missing_vendor_payloads_key(client):
    resp = client.post("/ingest/run/stream/multi", json={"primary_index": 0})
    assert resp.status_code == 400


def test_ingest_multi_rejects_out_of_range_primary_index(client):
    resp = client.post("/ingest/run/stream/multi", json={
        "vendor_payloads": [_opta_sample(), _statsbomb_sample()], "primary_index": 7,
    })
    assert resp.status_code == 400


def test_ingest_multi_rejects_bad_entry_shape(client):
    resp = client.post("/ingest/run/stream/multi", json={
        "vendor_payloads": [_opta_sample(), "not a valid entry"],
    })
    assert resp.status_code == 400


def test_ingest_multi_combines_two_sample_vendor_files(client):
    resp = client.post("/ingest/run/stream/multi", json={
        "vendor_payloads": [_opta_sample(), _statsbomb_sample()], "primary_index": 0,
    })
    assert resp.status_code == 200
    result = _parse_ndjson(resp.text)[-1]
    assert result["type"] == "result"
    assert result["data"]["overall_status"] == "success"
    assert result["data"]["document"]["metadata"]["source_vendors"] == ["opta"]
    assert any("multi-vendor combine" in w for w in result["data"]["document"]["metadata"]["warnings"])


def test_ingest_multi_accepts_a_bare_array_entry_alongside_an_object(client):
    """Same StatsBomb-open-data accommodation as the single-file endpoint,
    extended per-entry here: a bare array file sits alongside a normal
    vendor object in the same combine request."""
    resp = client.post("/ingest/run/stream/multi", json={
        "vendor_payloads": [_opta_sample(), _statsbomb_bare_event_array()], "primary_index": 0,
    })
    assert resp.status_code == 200
    result = _parse_ndjson(resp.text)[-1]
    assert result["data"]["overall_status"] == "success"


# --- Live feed replay -------------------------------------------------------

def test_live_stream_emits_start_then_one_tick_per_minute_then_result(client):
    resp = client.post(
        "/matches/PL_2025_CLEAN_001/live/stream?seconds_per_minute=0", json={"vendor": "opta"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = _parse_ndjson(resp.text)

    start = events[0]
    assert start["type"] == "live_start"
    assert start["replayable"] is True
    ticks = [e for e in events if e["type"] == "tick"]
    assert len(ticks) == start["updates"]
    assert events[-1]["type"] == "result"
    assert events[-1]["data"]["mode"] == "live"

    minutes = [(t["period"], t["minute"]) for t in ticks]
    assert minutes == sorted(minutes)
    assert [t["final"] for t in ticks] == [False] * (len(ticks) - 1) + [True]
    assert ticks[-1]["received"] == ticks[-1]["total"] == start["total_events"]


def test_live_stream_new_events_add_up_to_the_final_document(client):
    resp = client.post(
        "/matches/PL_2025_MESSY_001/live/stream?seconds_per_minute=0", json={"vendor": "statsbomb"},
    )
    events = _parse_ndjson(resp.text)
    ticks = [e for e in events if e["type"] == "tick"]
    delivered = sum(len(t["new_events"]) for t in ticks)
    final_doc = events[-1]["data"]["document"]
    assert delivered == len(final_doc["events"])
    # Ticks ship the document without its event list (the client accumulates).
    assert all("events" not in t["document"] for t in ticks)


def test_live_stream_score_never_goes_backwards(client):
    resp = client.post(
        "/matches/PL_2025_PARTIAL_001/live/stream?seconds_per_minute=0", json={"vendor": "wyscout"},
    )
    events = _parse_ndjson(resp.text)
    ticks = [e for e in events if e["type"] == "tick"]
    totals = [sum(s["goals"] for s in t["narrative"]["team_stats"].values()) for t in ticks]
    assert totals == sorted(totals)
    final_goals = sum(s["goals"] for s in events[-1]["data"]["narrative"]["team_stats"].values())
    assert totals[-1] == final_goals > 0


def test_live_stream_result_can_be_exported(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/live/stream?seconds_per_minute=0", json={"vendor": "opta"})
    run_id = _parse_ndjson(resp.text)[-1]["data"]["run_id"]
    export = client.post(f"/runs/{run_id}/export", json={"target_vendor": "statsbomb"})
    assert export.status_code == 200


def test_live_stream_rejects_out_of_range_speed(client):
    resp = client.post("/matches/PL_2025_CLEAN_001/live/stream?seconds_per_minute=99", json={"vendor": "opta"})
    assert resp.status_code == 422


def test_live_stream_unknown_vendor_400_and_unknown_match_404(client):
    assert client.post("/matches/PL_2025_CLEAN_001/live/stream", json={"vendor": "nope"}).status_code == 400
    assert client.post("/matches/NOPE/live/stream", json={"vendor": "opta"}).status_code == 404


def test_ingest_live_stream_replays_an_uploaded_feed(client):
    resp = client.post("/ingest/live/stream?seconds_per_minute=0", json=_opta_bundle())
    events = _parse_ndjson(resp.text)
    assert events[0]["replayable"] is True
    assert events[-1]["type"] == "result"
    assert events[-1]["data"]["source"] == "uploaded"


def test_ingest_live_stream_unrecognized_shape_falls_back_honestly(client):
    resp = client.post("/ingest/live/stream?seconds_per_minute=0", json={"hello": "world"})
    assert resp.status_code == 200
    events = _parse_ndjson(resp.text)
    assert events[0]["replayable"] is False
    assert events[0]["detail"]
    assert [e["type"] for e in events] == ["live_start", "result"]


def test_ingest_live_stream_rejects_bad_bodies(client):
    assert client.post("/ingest/live/stream", json={}).status_code == 400
    assert client.post("/ingest/live/stream", json=[1, 2]).status_code == 400
