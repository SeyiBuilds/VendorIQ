"""Tests for the FastAPI layer over the Master Orchestrator.

These use FastAPI's TestClient against the real app -- no mocking of the
pipeline itself, since the pipeline already has its own test suites
(test_orchestrator.py and each agent's own tests). What this file checks
is the HTTP-specific plumbing: request/response shapes, status codes for
each error path, and that run_id-based state (RUN_STORE) actually lets a
later export/personalize call reuse an earlier run.
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
    assert body["timeline"]["cues"]
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


def test_personalize_after_run(client):
    run_resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "opta"})
    run_id = run_resp.json()["run_id"]

    resp = client.post(f"/runs/{run_id}/personalize", json={
        "mode": "casual_fan", "language": "es", "favorite_club": "Liverpool",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "casual_fan"
    assert body["language"] == "es"
    assert body["headline"]


def test_personalize_defaults(client):
    run_resp = client.post("/matches/PL_2025_CLEAN_001/run", json={"vendor": "opta"})
    run_id = run_resp.json()["run_id"]

    resp = client.post(f"/runs/{run_id}/personalize", json={})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "casual_fan"
    assert resp.json()["language"] == "en"


def test_personalize_unknown_run_404(client):
    resp = client.post("/runs/not-a-real-run-id/personalize", json={})
    assert resp.status_code == 404


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
