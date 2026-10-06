"""
FastAPI layer over the Master Orchestrator.

This is a thin HTTP wrapper, not a rewrite: every endpoint below is a
direct call into MasterOrchestrator.run()/.export()/.personalize(). No
business logic lives here -- the orchestrator and its agents already own
all of it, with their own test suites. This file only does three things:
request/response plumbing, an in-memory run cache (see RunStore below),
and the API's own error-to-HTTP-status mapping.

RunStore is a deliberate, documented shortcut: a single-process,
in-memory dict keyed by run_id. A pipeline run produces objects
(CDFDocument, NarrativeReport, ...) that export() and personalize() need
as live Python objects, not their serialized JSON -- so a run has to be
held somewhere between "POST /matches/{id}/run" and a later
"POST /runs/{id}/personalize" call. For a hackathon demo / single-instance
deployment this is fine and honest; it is explicitly NOT how a
club-facing production service would do this (that needs a real cache --
Redis, a DB-backed session, or re-running the deterministic stages
on demand -- so a run survives a restart and works across replicas).
Called out here rather than hidden so the tradeoff is visible to anyone
reading this file, including future-us.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend" / "agents"))
sys.path.insert(0, str(ROOT / "backend" / "data" / "schemas"))

from orchestrator import MasterOrchestrator, PipelineResult  # noqa: E402
from personalization_engine import PersonalizationProfile  # noqa: E402
from serialization import to_jsonable  # noqa: E402

SAMPLE_MATCHES_DIR = ROOT / "backend" / "data" / "sample_matches"
VALID_VENDOR_FILES = {"opta", "statsbomb", "wyscout"}

app = FastAPI(
    title="Synthetic Match Insights Engine API",
    description="HTTP layer over the 10-agent football-data orchestration pipeline.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo-scope: a real deployment would scope this to the actual frontend origin(s)
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = MasterOrchestrator(ai_escalation_enabled=True)

# See module docstring -- in-memory, single-process, documented shortcut.
RUN_STORE: dict[str, PipelineResult] = {}


def _load_manifest() -> list[dict]:
    manifest_path = SAMPLE_MATCHES_DIR / "manifest.json"
    return json.loads(manifest_path.read_text())


def _manifest_entry(match_id: str) -> dict:
    for entry in _load_manifest():
        if entry["match_id"] == match_id:
            return entry
    raise HTTPException(status_code=404, detail=f"unknown match_id '{match_id}'")


class RunRequest(BaseModel):
    vendor: str = "opta"  # which single vendor file to ingest -- see orchestrator's single-vendor-ingestion design note


class PersonalizeRequest(BaseModel):
    mode: str = "casual_fan"
    favorite_club: Optional[str] = None
    favorite_player_name: Optional[str] = None
    favorite_player_jersey: Optional[int] = None
    language: str = "en"


class ExportRequest(BaseModel):
    target_vendor: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/matches")
def list_matches() -> list[dict]:
    return _load_manifest()


@app.post("/matches/{match_id}/run")
def run_match(match_id: str, body: RunRequest = RunRequest()) -> dict:
    if body.vendor not in VALID_VENDOR_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown vendor '{body.vendor}'; supported ingest vendors: {sorted(VALID_VENDOR_FILES)}",
        )
    entry = _manifest_entry(match_id)
    vendor_file = SAMPLE_MATCHES_DIR / match_id / f"{body.vendor}.json"
    if not vendor_file.exists():
        raise HTTPException(status_code=404, detail=f"match '{match_id}' has no '{body.vendor}' file")

    vendor_data = json.loads(vendor_file.read_text())
    result = orchestrator.run(vendor_data)

    run_id = result.orchestration_log.run_id
    RUN_STORE[run_id] = result

    return {
        "run_id": run_id,
        "match_id": match_id,
        "quality_tier": entry.get("quality_tier"),
        "overall_status": result.orchestration_log.overall_status,
        "orchestration_log": to_jsonable(result.orchestration_log),
        "document": to_jsonable(result.document),
        "narrative": to_jsonable(result.narrative),
        "explainability": to_jsonable(result.explainability),
        "timeline": result.timeline.to_dict() if result.timeline else None,
    }


def _get_run(run_id: str) -> PipelineResult:
    result = RUN_STORE.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown or expired run_id '{run_id}'")
    return result


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    result = _get_run(run_id)
    return {
        "run_id": run_id,
        "overall_status": result.orchestration_log.overall_status,
        "orchestration_log": to_jsonable(result.orchestration_log),
        "document": to_jsonable(result.document),
        "narrative": to_jsonable(result.narrative),
        "explainability": to_jsonable(result.explainability),
        "timeline": result.timeline.to_dict() if result.timeline else None,
    }


@app.post("/runs/{run_id}/export")
def export_run(run_id: str, body: ExportRequest) -> dict:
    result = _get_run(run_id)
    if result.document is None:
        raise HTTPException(status_code=409, detail="run has no document to export (critical stage failed)")
    payload, report = orchestrator.export(result.document, body.target_vendor)
    return {"payload": payload, "report": to_jsonable(report)}


@app.post("/runs/{run_id}/personalize")
def personalize_run(run_id: str, body: PersonalizeRequest) -> dict:
    result = _get_run(run_id)
    profile = PersonalizationProfile(
        mode=body.mode,
        favorite_club=body.favorite_club,
        favorite_player_name=body.favorite_player_name,
        favorite_player_jersey=body.favorite_player_jersey,
        language=body.language,
    )
    experience = orchestrator.personalize(result, profile)
    if experience is None:
        raise HTTPException(
            status_code=409,
            detail="run is missing narrative/explainability/timeline output (optional stage failed or was skipped) -- "
                   "cannot personalize",
        )
    return to_jsonable(experience)
