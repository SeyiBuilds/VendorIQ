# Synthetic Match Insights Engine — frontend

React + Vite UI over the FastAPI layer (`backend/api/`), demonstrating the
7-agent ingest → interpret → narrate pipeline live -- each agent's status
streams in as it actually runs, not as one lump result after the whole
pipeline finishes -- plus a live "break it on stage" data-corruption demo.

## Running locally

Backend (from the repo root):

```bash
pip install -r requirements.txt
uvicorn backend.api.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
npm install
cp .env.example .env   # points at http://localhost:8000 by default
npm run dev
```

## What it does

1. **Match & feed** — pick one of the three sample matches (clean/messy/
   partial data-quality tiers) and which vendor file to ingest, then run
   the full pipeline.
2. **Break it live** — pick one or more corruption mutations (strip player
   identity, null coordinates, drop timestamps...), set a rate and
   optional seed, and watch the same pipeline degrade gracefully instead
   of crashing or fabricating data, with a clean-vs-corrupted confidence
   comparison.
3. **Bring your own match data** — upload a vendor-shaped JSON file (Opta/
   StatsBomb/Wyscout, exactly as that vendor delivers it) and run it
   through the same pipeline. Format Detective auto-detects the vendor;
   nothing to select. An unrecognized shape degrades to a zero-confidence,
   zero-event result with the reason logged, rather than crashing or
   fabricating data.
4. **Pipeline run** — shows every stage live: pending → running → success/
   failed (retried flagged), streamed over NDJSON from
   `POST /matches/{id}/run/stream` (or `/ingest/run/stream` for an
   uploaded file) as each agent actually executes -- mirroring
   `OrchestrationLog`.
5. **Export** — re-export the current run's validated document into
   another vendor's shape (Agent 7, on demand) and download the result as
   JSON -- includes the match sheet too, when the run has one.
6. **Match sheet** — lineups, formations, substitutions, officials, venue,
   attendance and weather. A separate data pillar from the event timeline,
   mapped from its own vendor-shaped feed; only renders when the run
   actually had one (auto-attached for the 3 sample matches, optional for
   an upload).
7. **Narrative** — the generated match report, key moments, and team
   stats table.

No business logic lives here — every panel is a direct view onto what the
pipeline already produced; see `backend/api/main.py`'s docstring for the
API's own design notes.
