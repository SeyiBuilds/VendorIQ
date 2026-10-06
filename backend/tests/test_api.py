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
    assert types.count("start") == types.count("stage") == 6  # 5 critical + narrative_generator

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


def test_ingest_stream_rejects_non_object_body(client):
    # FastAPI's own request-body validation rejects a non-dict JSON value
    # (a `dict` type annotation) before our handler even runs -- a 422,
    # not our 400, since this is Pydantic/FastAPI's own validation layer.
    resp = client.post("/ingest/run/stream", json=["not", "an", "object"])
    assert resp.status_code == 422


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


# --- Live corruption endpoints (the "break it on stage" demo feature) ---

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
