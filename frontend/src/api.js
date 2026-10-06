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
async function streamRequest(path, body, onEvent) {
  const res = await fetch(`${BASE_URL}${path}`, {
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

export function exportRun(runId, targetVendor) {
  return request(`/runs/${runId}/export`, {
    method: "POST",
    body: JSON.stringify({ target_vendor: targetVendor }),
  });
}

export function listMutations() {
  return request("/mutations");
}

// Runs the pipeline against a vendor feed the caller supplies directly
// (a club's own Opta/StatsBomb/Wyscout export), rather than one of the
// bundled sample matches. Same event shape as runMatchStream(); no vendor
// is named since Format Detective auto-detects it.
export function ingestRunStream(payload, onEvent) {
  return streamRequest("/ingest/run/stream", payload, onEvent);
}

// Streaming twin of the corrupt-and-run endpoint -- same event shape as
// runMatchStream(), with corruption_log attached to the final "result" event.
export function corruptAndRunStream(matchId, { mutations, rate, seed }, onEvent) {
  return streamRequest(
    `/matches/${matchId}/corrupt-and-run/stream`,
    { vendor: "opta", mutations, rate, seed: seed ?? null },
    onEvent,
  );
}
