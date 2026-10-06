# Synthetic Match Insights Engine

Built for Microsoft × Premier League's **Inside the Game: Developer Hackathon**.

Turns synthetic, football-realistic match events into explainable match
intelligence: a 10-agent pipeline that ingests a vendor feed (Opta/
StatsBomb/Wyscout shaped), interprets it into a normalized format,
explains why moments matter (not just that they happened), renders a
broadcast-sync'd overlay timeline, and personalizes the same intelligence
for an analyst, a casual fan, or a fan of one specific player — in
English, Spanish, or French.

Built with the mindset that this already won and a club wants it in
production: real test suites (271 passing), pinned dependencies, honest
confidence scoring and graceful degradation instead of fabricated
numbers, and a documented fallback path for every place real data goes
missing or wrong.

## Quickstart

Requires Python 3.11+ and Node 18+.

```bash
# 1. backend — install deps and run the test suite
pip install -r requirements.txt
pytest backend/tests -q          # 271 passed

# 2. backend — start the API
uvicorn backend.api.main:app --reload --port 8000

# 3. frontend — in a second terminal
cd frontend
npm install
cp .env.example .env             # points at http://localhost:8000
npm run dev                      # opens on http://localhost:5173
```

Open the frontend URL, pick a sample match, hit **Run pipeline**. Try
**Break it live** to corrupt a clean feed on the spot (strip player
identities, null out coordinates, drop timestamps...) and watch the same
pipeline degrade gracefully instead of crashing or fabricating data.

## Architecture

```
backend/
  data/
    generators/synthetic_match_generator.py   synthetic Opta/StatsBomb/Wyscout fixtures
    corruption.py                             live fallback-path injection (demo feature)
    sample_matches/                           3 pre-generated matches (clean/messy/partial tiers)
    schemas/cdf_models.py                     the Common Data Format (CDF) — Pydantic
  agents/
    format_detective.py        Agent 1 — detect vendor format
    schema_mapper.py           Agent 2 — map to CDF, per-vendor
    coordinate_translator.py   Agent 3 — normalize pitch coordinates to 0-100 x 0-100
    entity_resolver.py         Agent 4 — resolve player identity (jersey/name, cross-vendor)
    validator.py               Agent 5 — build the final CDFDocument, score confidence
    narrative_generator.py     Agent 6 — stats + key moments + narrative text
    format_exporter.py         Agent 7 — export CDF back to a vendor shape (on-demand)
    explainability_engine.py   Agent 8 — control/chaos rhythm, momentum shifts, "why it matters"
    overlay_renderer.py        Agent 9 — broadcast-sync'd overlay cue timeline
    personalization_engine.py  Agent 10 — analyst/casual-fan/player-focus x language
    orchestrator.py            Master Orchestrator — wires 1-9, critical/optional stage split
  api/main.py                  FastAPI layer over the orchestrator
  tests/                       271 tests, one file per agent/module
frontend/                      React + Vite UI — see frontend/README.md
```

Full design rationale (fallback tiers, confidence scoring, why
single-vendor ingestion, why export/personalize are on-demand rather
than pipeline stages) is documented inline in each agent's own
docstring — start with `backend/agents/orchestrator.py`.
