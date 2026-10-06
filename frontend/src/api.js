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

export function listMatches() {
  return request("/matches");
}

export function runMatch(matchId, vendor) {
  return request(`/matches/${matchId}/run`, {
    method: "POST",
    body: JSON.stringify({ vendor }),
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

export function corruptAndRun(matchId, { mutations, rate, seed }) {
  return request(`/matches/${matchId}/corrupt-and-run`, {
    method: "POST",
    body: JSON.stringify({ vendor: "opta", mutations, rate, seed: seed ?? null }),
  });
}
