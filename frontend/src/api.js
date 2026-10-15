// Thin fetch wrapper over the FastAPI backend (backend/api/main.py). One
// function per endpoint, no caching or state here -- that lives in
// React state in App.jsx. Every function throws on a non-2xx response so
// callers can rely on try/catch rather than checking `.ok` everywhere.

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // response wasn't JSON -- fall back to statusText
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// Reads one of the /stream endpoints as NDJSON (one JSON object per line),
// calling onEvent as each line arrives -- not after the whole response
// body is buffered. This is what lets the UI show each agent actually
// starting and finishing in real time instead of painting a finished
// pipeline all at once. Throws the same shape of error as request() if
// the initial response itself is a non-2xx (validation errors, 404s).
// pace=false drops the backend's demo think/settle pauses between stages --
// used for a batch of many files (see ingestRunStream's `pace` param) where
// those pauses would otherwise multiply into minutes of apparent freeze.
async function streamRequest(path, body, onEvent, { pace = true, query = null } = {}) {
  const params = new URLSearchParams(query || {});
  if (!pace) params.set("pace", "false");
  const qs = params.toString();
  const url = qs ? `${BASE_URL}${path}?${qs}` : `${BASE_URL}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const errBody = await res.json();
      detail = errBody.detail || detail;
    } catch {
      // response wasn't JSON -- fall back to statusText
    }
    throw new Error(`${res.status}: ${detail}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newlineIndex;
    while ((newlineIndex = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
  const trailing = buffer.trim();
  if (trailing) onEvent(JSON.parse(trailing));
}

export function listMatches() {
  return request("/matches");
}

// Streams live stage-by-stage progress for a pipeline run. onEvent
// receives one of:
//   { type: "start", stage }
//   { type: "stage", stage, status, detail, retried }
//   { type: "result", data }   -- the same shape runMatch() used to resolve with
export function runMatchStream(matchId, vendor, onEvent) {
  return streamRequest(`/matches/${matchId}/run/stream`, { vendor }, onEvent);
}

// Live feed replay: the vendor file is delivered one match minute at a time
// and the full pipeline re-runs after each chunk. onEvent receives:
//   { type: "live_start", replayable, updates, total_events, last_minute, ... }
//   { type: "tick", period, minute, received, total, final, overall_status,
//     quality_confidence, stages, document (no events), new_events, narrative }
//   { type: "result", data }   -- same shape as every other stream's result
// secondsPerMinute is the replay speed (wall-clock seconds per match minute).
export function liveMatchStream(matchId, vendor, secondsPerMinute, onEvent) {
  return streamRequest(`/matches/${matchId}/live/stream`, { vendor }, onEvent, {
    query: { seconds_per_minute: String(secondsPerMinute) },
  });
}

export function ingestLiveStream(payload, secondsPerMinute, onEvent) {
  return streamRequest("/ingest/live/stream", payload, onEvent, {
    query: { seconds_per_minute: String(secondsPerMinute) },
  });
}

export function exportRun(runId, targetVendor) {
  return request(`/runs/${runId}/export`, {
    method: "POST",
    body: JSON.stringify({ target_vendor: targetVendor }),
  });
}

export function listMutations() {
  return request("/mutations");
}

// Orchestrator-level fault targets (fault_injection.py) -- a different demo
// than the field-level mutations above: this makes an agent genuinely raise
// so the Master Orchestrator's own retry/abort/degrade logic runs visibly.
export function listAgentFaults() {
  return request("/agent-faults");
}

// Runs the pipeline against a vendor feed the caller supplies directly
// (a club's own Opta/StatsBomb/Wyscout export), rather than one of the
// bundled sample matches. Same event shape as runMatchStream(); no vendor
// is named since Format Detective auto-detects it. pace:false (used by
// UploadPanel's "run all" batch loop) skips the backend's demo think/settle
// pauses -- essential once there's more than a handful of files, since
// those pauses are per-stage, per-file, and a multi-minute batch otherwise
// looks like the page has frozen.
export function ingestRunStream(payload, onEvent, { pace = true } = {}) {
  return streamRequest("/ingest/run/stream", payload, onEvent, { pace });
}

// Streaming twin of the corrupt-and-run endpoint -- same event shape as
// runMatchStream(), with corruption_log attached to the final "result" event.
// agentFault/failAttempts are independent of and combinable with the
// field-level mutations: mutations test an agent's own graceful-degradation
// logic, agentFault tests the orchestrator's real retry/abort/degrade logic
// (see fault_injection.py). failAttempts=1 self-heals on the automatic
// retry; failAttempts=2 genuinely fails (abort for a critical stage,
// "partial" for an optional one).
export function corruptAndRunStream(matchId, { mutations, rate, seed, agentFault, failAttempts }, onEvent) {
  return streamRequest(
    `/matches/${matchId}/corrupt-and-run/stream`,
    {
      vendor: "opta",
      mutations: mutations ?? [],
      rate,
      seed: seed ?? null,
      agent_fault: agentFault ?? null,
      fail_attempts: failAttempts ?? 2,
    },
    onEvent,
  );
}

// Genuine multi-vendor combine: 2+ uploaded vendor files for the SAME
// match, one picked as the primary event source (its events become the
// document's actual event stream -- every other file only contributes
// player-identity corroboration and match-info fallback, never counted as
// events, so stats don't get double-counted). Same event shape as
// runMatchStream(). See orchestrator.run_streaming_multi's docstring for
// why this isn't a naive concatenation of every vendor's events.
export function ingestRunStreamMulti(vendorPayloads, primaryIndex, onEvent, matchSheetData) {
  return streamRequest(
    "/ingest/run/stream/multi",
    { vendor_payloads: vendorPayloads, primary_index: primaryIndex, match_sheet_data: matchSheetData ?? null },
    onEvent,
  );
}
