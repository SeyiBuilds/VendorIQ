"""
FastAPI layer over the Master Orchestrator.

This is a thin HTTP wrapper, not a rewrite: every endpoint below is a
direct call into MasterOrchestrator.run()/.export(). No business logic
lives here -- the orchestrator and its agents already own all of it, with
their own test suites. This file only does three things: request/response
plumbing, an in-memory run cache (see RunStore below), and the API's own
error-to-HTTP-status mapping.

RunStore is a deliberate, documented shortcut: a single-process,
in-memory dict keyed by run_id. A pipeline run produces objects
(CDFDocument, NarrativeReport, ...) that export() needs as live Python
objects, not their serialized JSON -- so a run has to be held somewhere
between "POST /matches/{id}/run" and a later "POST /runs/{id}/export"
call. For a hackathon demo / single-instance deployment this is fine and
honest; it is explicitly NOT how a club-facing production service would
do this (that needs a real cache -- Redis, a DB-backed session, or
re-running the deterministic stages on demand -- so a run survives a
restart and works across replicas).
Called out here rather than hidden so the tradeoff is visible to anyone
reading this file, including future-us.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend" / "agents"))
sys.path.insert(0, str(ROOT / "backend" / "data" / "schemas"))
sys.path.insert(0, str(ROOT / "backend" / "data"))

from orchestrator import MasterOrchestrator, PipelineResult  # noqa: E402
from serialization import to_jsonable  # noqa: E402
from corruption import corrupt_opta_payload, MUTATION_DESCRIPTIONS  # noqa: E402

SAMPLE_MATCHES_DIR = ROOT / "backend" / "data" / "sample_matches"
VALID_VENDOR_FILES = {"opta", "statsbomb", "wyscout"}

app = FastAPI(
    title="Synthetic Match Insights Engine API",
    description="HTTP layer over the 7-agent football-data orchestration pipeline.",
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


class ExportRequest(BaseModel):
    target_vendor: str


class CorruptAndRunRequest(BaseModel):
    vendor: str = "opta"
    mutations: list[str]
    rate: float = 0.2  # fraction of events each selected mutation targets
    seed: Optional[int] = None  # pin for a reproducible run on stage


def _serialize_run(result: PipelineResult, run_id: str, extra: Optional[dict] = None) -> dict:
    base = {
        "run_id": run_id,
        "overall_status": result.orchestration_log.overall_status,
        "orchestration_log": to_jsonable(result.orchestration_log),
        "document": to_jsonable(result.document),
        "narrative": to_jsonable(result.narrative),
    }
    if extra:
        base.update(extra)
    return base


def _match_sheet_file(match_id: str, vendor: str) -> Optional[Path]:
    path = SAMPLE_MATCHES_DIR / match_id / "match_sheets" / f"{vendor}_match_sheet.json"
    return path if path.exists() else None


def _stream_run(vendor_data: dict, extra: Optional[dict] = None, match_sheet_data: Optional[dict] = None):
    """Shared by both streaming endpoints below. Drives
    orchestrator.run_streaming() and re-emits each event as one NDJSON
    line (newline-delimited JSON) as it actually happens -- this is a
    chunked HTTP response, not a buffered one, so the frontend sees each
    agent start/finish in real time rather than all at once after the
    whole pipeline completes. Ends with exactly one {"type": "result", ...}
    line carrying the same shape /matches/{id}/run already returns, so
    existing non-streaming callers and RUN_STORE stay unaffected."""
    for kind, payload in orchestrator.run_streaming(vendor_data, match_sheet_data=match_sheet_data):
        if kind == "start":
            yield json.dumps({"type": "start", "stage": payload}) + "\n"
        elif kind == "stage":
            yield json.dumps({
                "type": "stage",
                "stage": payload.stage,
                "status": payload.status,
                "detail": payload.detail,
                "retried": payload.retried,
            }) + "\n"
        else:  # kind == "result"
            result = payload
            run_id = result.orchestration_log.run_id
            RUN_STORE[run_id] = result
            final = _serialize_run(result, run_id, extra)
            yield json.dumps({"type": "result", "data": final}) + "\n"


@app.post("/matches/{match_id}/run/stream")
def run_match_stream(match_id: str, body: RunRequest = RunRequest()) -> StreamingResponse:
    """Streaming twin of POST /matches/{match_id}/run -- same validation,
    same orchestrator, same final payload shape; the only difference is
    that stage progress is pushed as NDJSON lines as each agent actually
    finishes, instead of one response after the whole pipeline is done."""
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
    sheet_file = _match_sheet_file(match_id, body.vendor)
    match_sheet_data = json.loads(sheet_file.read_text()) if sheet_file else None
    extra = {"match_id": match_id, "quality_tier": entry.get("quality_tier")}
    return StreamingResponse(_stream_run(vendor_data, extra, match_sheet_data), media_type="application/x-ndjson")


@app.post("/ingest/run/stream")
def ingest_run_stream(payload: dict = Body(...)) -> StreamingResponse:
    """Run the pipeline against a vendor feed the caller supplies directly
    -- e.g. a club's own Opta/StatsBomb/Wyscout export -- rather than one
    of the bundled sample matches. No vendor needs to be named: Format
    Detective (Agent 1) auto-detects it exactly as it does for the sample
    matches, so this is the same pipeline, just pointed at different
    input. An unrecognized shape isn't a 500 -- Format Detective's
    critical-stage failure path (orchestrator.py) turns it into a normal
    "failed" result with the reason in the orchestration log, same as any
    other critical-stage failure.

    An optional sibling "match_sheet_data" key (a vendor-shaped lineups/
    officials/venue payload, same as the bundled matches' own
    opta_match_sheet.json etc.) is popped off before the rest is treated
    as the event feed -- so the existing event-only contract keeps working
    unchanged for callers that don't send it."""
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="request body must be a non-empty JSON object")
    match_sheet_data = payload.pop("match_sheet_data", None)
    if not isinstance(match_sheet_data, dict):
        match_sheet_data = None
    return StreamingResponse(
        _stream_run(payload, {"match_id": None, "quality_tier": None, "source": "uploaded"}, match_sheet_data),
        media_type="application/x-ndjson",
    )


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
    sheet_file = _match_sheet_file(match_id, body.vendor)
    match_sheet_data = json.loads(sheet_file.read_text()) if sheet_file else None
    result = orchestrator.run(vendor_data, match_sheet_data=match_sheet_data)

    run_id = result.orchestration_log.run_id
    RUN_STORE[run_id] = result

    return _serialize_run(result, run_id, {"match_id": match_id, "quality_tier": entry.get("quality_tier")})


def _get_run(run_id: str) -> PipelineResult:
    result = RUN_STORE.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown or expired run_id '{run_id}'")
    return result


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    result = _get_run(run_id)
    return _serialize_run(result, run_id)


@app.get("/mutations")
def list_mutations() -> dict:
    return MUTATION_DESCRIPTIONS


@app.post("/matches/{match_id}/corrupt-and-run")
def corrupt_and_run_match(match_id: str, body: CorruptAndRunRequest) -> dict:
    """Live, on-demand data corruption for stage demos -- see
    backend/data/corruption.py's module docstring for why this exists and
    exactly which upstream fallback each mutation is built to exercise.
    Currently Opta-only, matching that module's scope."""
    if body.vendor != "opta":
        raise HTTPException(
            status_code=400,
            detail=f"corrupt-and-run currently only supports the 'opta' vendor feed, got '{body.vendor}'",
        )
    entry = _manifest_entry(match_id)
    vendor_file = SAMPLE_MATCHES_DIR / match_id / "opta.json"
    if not vendor_file.exists():
        raise HTTPException(status_code=404, detail=f"match '{match_id}' has no 'opta' file")

    vendor_data = json.loads(vendor_file.read_text())
    try:
        corrupted_data, corruption_log = corrupt_opta_payload(
            vendor_data, body.mutations, rate=body.rate, seed=body.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sheet_file = _match_sheet_file(match_id, "opta")
    match_sheet_data = json.loads(sheet_file.read_text()) if sheet_file else None
    result = orchestrator.run(corrupted_data, match_sheet_data=match_sheet_data)
    run_id = result.orchestration_log.run_id
    RUN_STORE[run_id] = result

    return _serialize_run(result, run_id, {
        "match_id": match_id,
        "quality_tier": entry.get("quality_tier"),
        "corruption_log": corruption_log,
    })


@app.post("/matches/{match_id}/corrupt-and-run/stream")
def corrupt_and_run_match_stream(match_id: str, body: CorruptAndRunRequest) -> StreamingResponse:
    """Streaming twin of POST /matches/{match_id}/corrupt-and-run -- same
    corruption + validation, same NDJSON stage-by-stage shape as
    /run/stream above. The corruption_log is attached to the final
    {"type": "result"} line, same as the non-streaming endpoint."""
    if body.vendor != "opta":
        raise HTTPException(
            status_code=400,
            detail=f"corrupt-and-run currently only supports the 'opta' vendor feed, got '{body.vendor}'",
        )
    entry = _manifest_entry(match_id)
    vendor_file = SAMPLE_MATCHES_DIR / match_id / "opta.json"
    if not vendor_file.exists():
        raise HTTPException(status_code=404, detail=f"match '{match_id}' has no 'opta' file")

    vendor_data = json.loads(vendor_file.read_text())
    try:
        corrupted_data, corruption_log = corrupt_opta_payload(
            vendor_data, body.mutations, rate=body.rate, seed=body.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sheet_file = _match_sheet_file(match_id, "opta")
    match_sheet_data = json.loads(sheet_file.read_text()) if sheet_file else None
    extra = {"match_id": match_id, "quality_tier": entry.get("quality_tier"), "corruption_log": corruption_log}
    return StreamingResponse(_stream_run(corrupted_data, extra, match_sheet_data), media_type="application/x-ndjson")


@app.post("/runs/{run_id}/export")
def export_run(run_id: str, body: ExportRequest) -> dict:
    result = _get_run(run_id)
    if result.document is None:
        raise HTTPException(status_code=409, detail="run has no document to export (critical stage failed)")
    payload, report = orchestrator.export(result.document, body.target_vendor)
    return {"payload": payload, "report": to_jsonable(report)}
