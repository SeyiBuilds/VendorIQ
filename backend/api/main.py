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

import contextlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "backend" / "agents"))
sys.path.insert(0, str(ROOT / "backend" / "data" / "schemas"))
sys.path.insert(0, str(ROOT / "backend" / "data"))

from orchestrator import MasterOrchestrator, PipelineResult  # noqa: E402
from live_feed import events_in_tick, replay  # noqa: E402
from serialization import to_jsonable  # noqa: E402
from corruption import corrupt_opta_payload, MUTATION_DESCRIPTIONS  # noqa: E402
from fault_injection import AGENT_FAULTS, FAULT_DESCRIPTIONS, maybe_inject_fault  # noqa: E402

SAMPLE_MATCHES_DIR = ROOT / "backend" / "data" / "sample_matches"
VALID_VENDOR_FILES = {"opta", "statsbomb", "wyscout"}

app = FastAPI(
    title="VendorIQ API",
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
# Live feed replay re-runs the pipeline once per match minute; its
# in-progress updates use this AI-off instance (see live_feed.py's
# docstring for why), and only the final update uses `orchestrator` above.
live_orchestrator = MasterOrchestrator(ai_escalation_enabled=False, narrative_in_progress=True)

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
    mutations: list[str] = []
    rate: float = 0.2  # fraction of events each selected mutation targets
    seed: Optional[int] = None  # pin for a reproducible run
    # Orchestrator-level fault injection (fault_injection.py) -- a
    # DIFFERENT demo than `mutations` above. Mutations corrupt event data
    # and show an agent's own field-level fallback absorb it silently;
    # agent_fault makes an agent itself genuinely raise, so the Master
    # Orchestrator's real retry/abort/degrade logic fires visibly. The two
    # are independent and may be combined in one request.
    agent_fault: Optional[str] = None
    fail_attempts: int = 2  # 1 = self-heals on retry, 2 = genuinely fails (abort or degrade)


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


# Every stage computes in milliseconds, so an unpaced stream flashes
# through all 7 agents before anyone can read a single status line --
# fine for a test assertion, useless for a human watching the pipeline
# work. These pauses are presentation-only: they sit in the HTTP
# streaming wrapper, after the real result is already computed, never
# inside the orchestrator or an agent, so nothing about the actual
# pipeline logic, timing-sensitive retry behavior, or test suite is
# touched. STAGE_THINK_SECONDS holds the "running" row on screen before
# its result appears (lets you read what's about to happen);
# STAGE_SETTLE_SECONDS holds the finished row before the next one starts
# (lets you read what happened, retries/fallbacks included).
STAGE_THINK_SECONDS = 0.35
STAGE_SETTLE_SECONDS = 0.15


def _match_sheet_file(match_id: str, vendor: str) -> Optional[Path]:
    path = SAMPLE_MATCHES_DIR / match_id / "match_sheets" / f"{vendor}_match_sheet.json"
    return path if path.exists() else None


def _stream_run(vendor_data: dict, extra: Optional[dict] = None, match_sheet_data: Optional[dict] = None,
                 agent_fault: Optional[str] = None, fail_attempts: int = 2, pace: bool = True):
    """Shared by both streaming endpoints below. Drives
    orchestrator.run_streaming() and re-emits each event as one NDJSON
    line (newline-delimited JSON) as it actually happens -- this is a
    chunked HTTP response, not a buffered one, so the frontend sees each
    agent start/finish in real time rather than all at once after the
    whole pipeline completes. Ends with exactly one {"type": "result", ...}
    line carrying the same shape /matches/{id}/run already returns, so
    existing non-streaming callers and RUN_STORE stay unaffected.

    agent_fault/fail_attempts (fault_injection.py), when set, wrap the
    whole run in maybe_inject_fault() -- the `with` block stays open
    across every `yield` below since this is a generator, so the
    monkeypatched agent method is live for exactly this one run and
    restored no matter how the generator ends.

    pace=True (the default) adds the STAGE_THINK/SETTLE pauses, for a
    single demo run someone is watching live. A multi-file batch (the
    upload panel's "Run all N matches") calls this once per file in a
    loop, so the same pauses would multiply into minutes of apparent
    freeze for a large batch -- callers running a batch pass pace=False
    to get each file's real, near-instant result with no demo pacing."""
    with maybe_inject_fault(orchestrator, agent_fault, fail_attempts):
        for kind, payload in orchestrator.run_streaming(vendor_data, match_sheet_data=match_sheet_data):
            if kind == "start":
                yield json.dumps({"type": "start", "stage": payload}) + "\n"
                if pace:
                    time.sleep(STAGE_THINK_SECONDS)
            elif kind == "stage":
                yield json.dumps({
                    "type": "stage",
                    "stage": payload.stage,
                    "status": payload.status,
                    "detail": payload.detail,
                    "retried": payload.retried,
                }) + "\n"
                if pace:
                    time.sleep(STAGE_SETTLE_SECONDS)
            else:  # kind == "result"
                result = payload
                run_id = result.orchestration_log.run_id
                RUN_STORE[run_id] = result
                final = _serialize_run(result, run_id, extra)
                yield json.dumps({"type": "result", "data": final}) + "\n"


def _stream_run_multi(vendor_payloads: list[dict], primary_index: int, extra: Optional[dict] = None,
                       match_sheet_data: Optional[dict] = None, pace: bool = True):
    """Multi-vendor twin of _stream_run() -- drives
    orchestrator.run_streaming_multi() instead of run_streaming(), same
    NDJSON shape so the frontend's existing stream handling needs no
    special-casing to consume either one. Same pace flag, same reason."""
    for kind, payload in orchestrator.run_streaming_multi(
        vendor_payloads, primary_index=primary_index, match_sheet_data=match_sheet_data,
    ):
        if kind == "start":
            yield json.dumps({"type": "start", "stage": payload}) + "\n"
            if pace:
                time.sleep(STAGE_THINK_SECONDS)
        elif kind == "stage":
            yield json.dumps({
                "type": "stage",
                "stage": payload.stage,
                "status": payload.status,
                "detail": payload.detail,
                "retried": payload.retried,
            }) + "\n"
            if pace:
                time.sleep(STAGE_SETTLE_SECONDS)
        else:  # kind == "result"
            result = payload
            run_id = result.orchestration_log.run_id
            RUN_STORE[run_id] = result
            final = _serialize_run(result, run_id, extra)
            yield json.dumps({"type": "result", "data": final}) + "\n"


@app.post("/ingest/run/stream/multi")
def ingest_run_stream_multi(payload: dict = Body(...), pace: bool = Query(True)) -> StreamingResponse:
    """Genuine multi-vendor combine: {"vendor_payloads": [<file1>, <file2>, ...],
    "primary_index": 0, "match_sheet_data": {...}?}. Each entry in
    vendor_payloads is one uploaded file's parsed JSON, either a vendor
    object or (same StatsBomb-open-data accommodation as the single-file
    endpoint) a bare array, wrapped as {"events": [...]} here before it
    reaches the orchestrator. primary_index picks which file's events
    become the document's actual event stream -- see
    orchestrator.run_streaming_multi's docstring for why only one file's
    events are counted (avoiding double-counting the same real match
    twice) while every file still contributes to player-identity
    resolution and match-info fallback."""
    raw_payloads = payload.get("vendor_payloads")
    if not isinstance(raw_payloads, list) or len(raw_payloads) < 2:
        raise HTTPException(status_code=400, detail="vendor_payloads must be a list of 2 or more files")

    vendor_payloads = []
    for i, p in enumerate(raw_payloads):
        if isinstance(p, list):
            if not p or not all(isinstance(e, dict) for e in p):
                raise HTTPException(status_code=400, detail=f"vendor_payloads[{i}]: a JSON array entry must be non-empty and contain only event objects")
            vendor_payloads.append({"events": p})
        elif isinstance(p, dict) and p:
            vendor_payloads.append(p)
        else:
            raise HTTPException(status_code=400, detail=f"vendor_payloads[{i}] must be a non-empty JSON object or array")

    primary_index = payload.get("primary_index", 0)
    if not isinstance(primary_index, int) or not (0 <= primary_index < len(vendor_payloads)):
        raise HTTPException(status_code=400, detail=f"primary_index must be an integer in range [0, {len(vendor_payloads) - 1}]")

    match_sheet_data = payload.get("match_sheet_data")
    if not isinstance(match_sheet_data, dict):
        match_sheet_data = None

    extra = {"match_id": None, "quality_tier": None, "source": "uploaded_multi"}
    return StreamingResponse(
        _stream_run_multi(vendor_payloads, primary_index, extra, match_sheet_data, pace=pace),
        media_type="application/x-ndjson",
    )


@app.post("/matches/{match_id}/run/stream")
def run_match_stream(match_id: str, body: RunRequest = RunRequest(), pace: bool = Query(True)) -> StreamingResponse:
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
    return StreamingResponse(_stream_run(vendor_data, extra, match_sheet_data, pace=pace), media_type="application/x-ndjson")


@app.post("/ingest/run/stream")
def ingest_run_stream(payload: Any = Body(...), pace: bool = Query(True)) -> StreamingResponse:
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
    unchanged for callers that don't send it.

    Accepts a bare JSON array too, not just an object: StatsBomb's own
    public event files (as downloaded straight from their open-data repo)
    are shaped exactly that way -- a top-level list of event objects, no
    wrapper. Format Detective/Schema Mapper both need a top-level dict with
    a named event-list key, so an array is wrapped as {"events": [...]}
    before detection runs. This is the same honest-degradation philosophy
    as everything else here: real-world vendor exports don't all share one
    top-level shape, so we adapt to the ones we can rather than rejecting
    them outright. A bare array can't carry a "match_sheet_data" sibling
    key (there's no object to hang it off), so that stays dict-only."""
    if isinstance(payload, list):
        if not payload or not all(isinstance(e, dict) for e in payload):
            raise HTTPException(
                status_code=400,
                detail="a JSON array body must be non-empty and contain only event objects",
            )
        return StreamingResponse(
            _stream_run({"events": payload}, {"match_id": None, "quality_tier": None, "source": "uploaded"}, pace=pace),
            media_type="application/x-ndjson",
        )
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="request body must be a non-empty JSON object or array")
    match_sheet_data = payload.pop("match_sheet_data", None)
    if not isinstance(match_sheet_data, dict):
        match_sheet_data = None
    return StreamingResponse(
        _stream_run(payload, {"match_id": None, "quality_tier": None, "source": "uploaded"}, match_sheet_data, pace=pace),
        media_type="application/x-ndjson",
    )


# Default replay speed: wall-clock seconds per match minute. 0.6s plays a
# 25-minute sample in about 15 seconds and a full 90 in about a minute.
LIVE_DEFAULT_SECONDS_PER_MINUTE = 0.6
LIVE_MAX_SECONDS_PER_MINUTE = 5.0


def _stream_live(vendor_data: dict, extra: Optional[dict] = None, match_sheet_data: Optional[dict] = None,
                 seconds_per_minute: float = LIVE_DEFAULT_SECONDS_PER_MINUTE):
    """NDJSON live feed replay (see backend/agents/live_feed.py).

    Lines, in order:
      {"type": "live_start", ...}   -- once: whether the feed could be
                                       replayed, how many updates, how many events
      {"type": "tick", ...}         -- one per match minute that has events
      {"type": "result", "data": {...}}  -- once, same shape as every other
                                       stream's result, stored in RUN_STORE
                                       so export works on a live run too

    Each tick carries the document WITHOUT its event list plus only the
    events that arrived in that minute (`new_events`); the client keeps a
    running list. Re-sending every event every minute would grow the stream
    quadratically (a real 1,700-event match over 95 updates is roughly
    160,000 event objects). The narrative and stats are sent whole each
    tick since they are small and are what actually changes."""
    plan_seen = False
    for item in replay(vendor_data, live_orchestrator, orchestrator, match_sheet_data=match_sheet_data):
        kind = item[0]
        if kind == "plan":
            plan = item[1]
            plan_seen = plan is not None
            yield json.dumps({
                "type": "live_start",
                "replayable": plan_seen,
                "updates": len(plan.ticks) if plan else 0,
                "total_events": plan.ticks[-1].total if plan else 0,
                "last_minute": plan.ticks[-1].minute if plan else None,
                "seconds_per_minute": seconds_per_minute,
                "detail": None if plan else (
                    "feed could not be split by match time (format not detected or no events); "
                    "ran as a single normal update instead"
                ),
            }) + "\n"
        elif kind == "tick":
            tick, result = item[1], item[2]
            document = result.document
            doc_json = to_jsonable(document) if document is not None else None
            if doc_json is not None:
                doc_json.pop("events", None)
            yield json.dumps({
                "type": "tick",
                "period": tick.period,
                "minute": tick.minute,
                "received": tick.received,
                "total": tick.total,
                "new_raw_events": tick.new_events,
                "final": tick.received == tick.total,
                "overall_status": result.orchestration_log.overall_status,
                "quality_confidence": document.metadata.quality_confidence if document else None,
                "stages": [
                    {"stage": r.stage, "status": r.status, "retried": r.retried}
                    for r in result.orchestration_log.records
                ],
                "document": doc_json,
                "new_events": to_jsonable(events_in_tick(document, tick)),
                "narrative": to_jsonable(result.narrative),
            }) + "\n"
            if tick.received < tick.total and seconds_per_minute > 0:
                time.sleep(seconds_per_minute)
        else:  # "result"
            result = item[1]
            run_id = result.orchestration_log.run_id
            RUN_STORE[run_id] = result
            data = _serialize_run(result, run_id, {**(extra or {}), "mode": "live"})
            yield json.dumps({"type": "result", "data": data}) + "\n"


@app.post("/matches/{match_id}/live/stream")
def live_match_stream(
    match_id: str,
    body: RunRequest = RunRequest(),
    seconds_per_minute: float = Query(LIVE_DEFAULT_SECONDS_PER_MINUTE, ge=0.0, le=LIVE_MAX_SECONDS_PER_MINUTE),
) -> StreamingResponse:
    """Replays a sample match as a live feed: the vendor file is delivered
    one match minute at a time and the full pipeline re-runs after each
    chunk, so stats, score and key moments update as the match unfolds.
    seconds_per_minute sets the replay speed (0 = as fast as possible)."""
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
    return StreamingResponse(
        _stream_live(vendor_data, extra, match_sheet_data, seconds_per_minute),
        media_type="application/x-ndjson",
    )


@app.post("/ingest/live/stream")
def ingest_live_stream(
    payload: Any = Body(...),
    seconds_per_minute: float = Query(LIVE_DEFAULT_SECONDS_PER_MINUTE, ge=0.0, le=LIVE_MAX_SECONDS_PER_MINUTE),
) -> StreamingResponse:
    """Live feed replay for an uploaded vendor file. Accepts the same body
    shapes as POST /ingest/run/stream (object, optional match_sheet_data
    sibling key, or a bare StatsBomb-style event array)."""
    extra = {"match_id": None, "quality_tier": None, "source": "uploaded"}
    if isinstance(payload, list):
        if not payload or not all(isinstance(e, dict) for e in payload):
            raise HTTPException(
                status_code=400,
                detail="a JSON array body must be non-empty and contain only event objects",
            )
        return StreamingResponse(
            _stream_live({"events": payload}, extra, None, seconds_per_minute),
            media_type="application/x-ndjson",
        )
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail="request body must be a non-empty JSON object or array")
    match_sheet_data = payload.pop("match_sheet_data", None)
    if not isinstance(match_sheet_data, dict):
        match_sheet_data = None
    return StreamingResponse(
        _stream_live(payload, extra, match_sheet_data, seconds_per_minute),
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


@app.get("/agent-faults")
def list_agent_faults() -> dict:
    """Orchestrator-level fault injection targets (fault_injection.py) --
    a different demo than /mutations above: this makes an agent genuinely
    raise so the Master Orchestrator's own retry/abort/degrade logic runs
    visibly, instead of an agent quietly absorbing bad data."""
    return FAULT_DESCRIPTIONS


def _validate_agent_fault(body: CorruptAndRunRequest) -> None:
    if body.agent_fault is None:
        return
    if body.agent_fault not in AGENT_FAULTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown agent_fault '{body.agent_fault}'; supported: {sorted(AGENT_FAULTS)}",
        )
    if body.fail_attempts not in (1, 2):
        raise HTTPException(
            status_code=400,
            detail=f"fail_attempts must be 1 or 2 (matches the orchestrator's real retry count), got {body.fail_attempts}",
        )


@app.post("/matches/{match_id}/corrupt-and-run")
def corrupt_and_run_match(match_id: str, body: CorruptAndRunRequest) -> dict:
    """Live, on-demand data corruption -- see backend/data/corruption.py's
    module docstring for why this exists and exactly which upstream
    fallback each mutation is built to exercise. Currently Opta-only,
    matching that module's scope. Also accepts an
    orchestrator-level agent_fault (fault_injection.py), independent of
    and combinable with the event-level mutations above."""
    if body.vendor != "opta":
        raise HTTPException(
            status_code=400,
            detail=f"corrupt-and-run currently only supports the 'opta' vendor feed, got '{body.vendor}'",
        )
    _validate_agent_fault(body)
    entry = _manifest_entry(match_id)
    vendor_file = SAMPLE_MATCHES_DIR / match_id / "opta.json"
    if not vendor_file.exists():
        raise HTTPException(status_code=404, detail=f"match '{match_id}' has no 'opta' file")

    vendor_data = json.loads(vendor_file.read_text())
    try:
        corrupted_data, corruption_log = corrupt_opta_payload(
            vendor_data, body.mutations, rate=body.rate, seed=body.seed,
        ) if body.mutations else (vendor_data, [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sheet_file = _match_sheet_file(match_id, "opta")
    match_sheet_data = json.loads(sheet_file.read_text()) if sheet_file else None
    with maybe_inject_fault(orchestrator, body.agent_fault, body.fail_attempts):
        result = orchestrator.run(corrupted_data, match_sheet_data=match_sheet_data)
    run_id = result.orchestration_log.run_id
    RUN_STORE[run_id] = result

    return _serialize_run(result, run_id, {
        "match_id": match_id,
        "quality_tier": entry.get("quality_tier"),
        "corruption_log": corruption_log,
        "agent_fault": body.agent_fault,
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
    _validate_agent_fault(body)
    entry = _manifest_entry(match_id)
    vendor_file = SAMPLE_MATCHES_DIR / match_id / "opta.json"
    if not vendor_file.exists():
        raise HTTPException(status_code=404, detail=f"match '{match_id}' has no 'opta' file")

    vendor_data = json.loads(vendor_file.read_text())
    try:
        corrupted_data, corruption_log = corrupt_opta_payload(
            vendor_data, body.mutations, rate=body.rate, seed=body.seed,
        ) if body.mutations else (vendor_data, [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    sheet_file = _match_sheet_file(match_id, "opta")
    match_sheet_data = json.loads(sheet_file.read_text()) if sheet_file else None
    extra = {
        "match_id": match_id, "quality_tier": entry.get("quality_tier"),
        "corruption_log": corruption_log, "agent_fault": body.agent_fault,
    }
    return StreamingResponse(
        _stream_run(corrupted_data, extra, match_sheet_data, body.agent_fault, body.fail_attempts),
        media_type="application/x-ndjson",
    )


@app.post("/runs/{run_id}/export")
def export_run(run_id: str, body: ExportRequest) -> dict:
    result = _get_run(run_id)
    if result.document is None:
        raise HTTPException(status_code=409, detail="run has no document to export (critical stage failed)")
    payload, report = orchestrator.export(result.document, body.target_vendor)
    return {"payload": payload, "report": to_jsonable(report)}
