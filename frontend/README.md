# Synthetic Match Insights Engine — frontend

React + Vite UI over the FastAPI layer (`backend/api/`), demonstrating the
hackathon brief's **render** and **personalize** pipeline stages: a
synchronized broadcast overlay driven by the Overlay Renderer's cue
timeline, and side-by-side analyst/casual-fan/player-focus/multi-language
views driven by the Personalization Engine.

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
2. **Pipeline run** — shows every stage's status (success/failed/skipped,
   retried), mirroring `OrchestrationLog`.
3. **Render** — plays back the Overlay Renderer's cue timeline against a
   scrubbable match clock: a persistent score bug plus transient goal/
   card/momentum-shift cues, each triggered off its exact `absolute_second`.
4. **Explain** — a control/contested/chaotic rhythm strip across the
   match plus the explained key moments ("why it matters", not just that
   it happened).
5. **Narrative** — the generated match report and team stats table.
6. **Personalize** — choose a mode (analyst / casual fan / player focus),
   a favorite club/player, and a language, and get back a distinct
   experience generated from the exact same underlying data.

No business logic lives here — every panel is a direct view onto what the
pipeline already produced; see `backend/api/main.py`'s docstring for the
API's own design notes.
