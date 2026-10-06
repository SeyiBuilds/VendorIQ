# Synthetic Match Insights Engine

Built for Microsoft × Premier League's **Inside the Game: Developer Hackathon**.

Turns synthetic, football-realistic match events into explainable match
intelligence: a 7-agent pipeline that ingests a vendor feed (Opta/
StatsBomb/Wyscout shaped), interprets it into a normalized format, and
explains it back out as stats + narrative text -- with each agent's
progress streamed to the UI live as it actually runs, not revealed all at
once after the whole pipeline finishes.

Covers two of the Common Data Format's data pillars honestly: **event
data** (the pipeline above) and **match sheet data** (lineups, formations,
substitutions, officials, venue, attendance -- mapped from its own
vendor-shaped feed, a separate optional stage, never fabricated when no
match-sheet payload is supplied). Tracking data, video footage and
physical/physiological data are deliberately out of scope -- they come
from entirely different vendor systems (TRACAB/Second Spectrum, raw media,
club-internal GPS wearables) that aren't bundled into an Opta/StatsBomb/
Wyscout event or lineup feed, so faking them would misrepresent what this
tool actually does.

Built with the mindset that this already won and a club wants it in
production: real test suites (234 passing), pinned dependencies, honest
confidence scoring and graceful degradation instead of fabricated
numbers, and a documented fallback path for every place real data goes
missing or wrong.

## Quickstart

Requires Python 3.11+ and Node 18+.

```bash
# 1. backend — install deps and run the test suite
pip install -r requirements.txt
pytest backend/tests -q          # 234 passed
# (Windows: if `pytest` / `uvicorn` aren't recognized, use `python -m pytest` / `python -m uvicorn` instead)

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
Or use **Bring your own match data** to upload a vendor-shaped JSON file
of your own (Opta/StatsBomb/Wyscout) and run it through the same
pipeline — format is auto-detected. Once a run produces a document, use
**Export** to re-export it into another vendor's shape and download it.

## Architecture

```
backend/
  data/
    generators/synthetic_match_generator.py   synthetic Opta/StatsBomb/Wyscout fixtures
                                               (events + match sheets)
    corruption.py                             live fallback-path injection (demo feature)
    sample_matches/                           3 pre-generated matches (clean/messy/partial tiers);
                                               each has 3 event files + match_sheets/ (3 more)
    schemas/cdf_models.py                     the Common Data Format (CDF) — Pydantic
  agents/
    format_detective.py        Agent 1 — detect vendor format (event feeds)
    schema_mapper.py           Agent 2 — map to CDF, per-vendor; also maps match-sheet feeds
                                (own vendor detection, separate from Format Detective)
    coordinate_translator.py   Agent 3 — normalize pitch coordinates to 0-100 x 0-100
    entity_resolver.py         Agent 4 — resolve player identity (jersey/name, cross-vendor)
    validator.py               Agent 5 — build the final CDFDocument, score confidence,
                                assemble match_sheet when one was supplied
    narrative_generator.py     Agent 6 — stats + key moments + narrative text
    format_exporter.py         Agent 7 — export CDF (events + match sheet) back to a vendor
                                shape (on-demand)
    orchestrator.py            Master Orchestrator — wires 1-6 + the optional match_sheet_mapper
                                stage, critical/optional stage split
  api/main.py                  FastAPI layer over the orchestrator
  tests/                       234 tests, one file per agent/module
frontend/                      React + Vite UI — see frontend/README.md
```

Full design rationale (fallback tiers, confidence scoring, why
single-vendor ingestion, why export is on-demand rather than a pipeline
stage) is documented inline in each agent's own docstring — start with
`backend/agents/orchestrator.py`.
