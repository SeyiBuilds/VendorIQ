# VendorIQ frontend

React + Vite UI over the FastAPI layer (`backend/api/`). It shows the 7-agent pipeline working live, replays a match as a live feed with a match tracker, and presents the result as a match report. No business logic lives here: every panel is a view onto what the pipeline produced.

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

| Panel | What you see |
|---|---|
| Bring your own match data | Upload Opta, StatsBomb or Wyscout JSON (single files or a .zip). The vendor is auto-detected. Run one file, run a batch, combine several vendors' files for one match into one CDF, or pick any loaded file to play as a live feed. |
| Match & feed | Pick a sample match and vendor file, then Run pipeline or Play as live feed at Fast, Normal or Watch speed. |
| Pipeline run | Each agent's status streams in as it actually runs (pending, running, success or failed, retries flagged). |
| Resilience | Corrupt a feed live (strip player ids, null coordinates, drop timestamps) or make an agent fail, and watch the pipeline degrade or retry instead of crashing. |
| Live feed | Match clock, events received so far, and the result of each minute's full pipeline re-run. |
| Match tracker | The live feed's events played on a pitch: the ball moves through each action, the player on the ball is marked with his number, shots stay on the pitch, goals and cards get an overlay, and a commentary feed names each action. Only event data is drawn; other players' positions would need tracking data. |
| Match report | Scoreboard with team badges, key-moment timeline, and two views. Fan view: turning point (the last goal that changed who was ahead) with the events that led to it, shot map, possession, shots and pass accuracy, and the story. Analyst view: full team comparison, key moment log with exact times, card colours and confidence, and data quality notes. |
| Export | Re-export the run's document into another vendor's shape and download it. |
| Match sheet | Lineups, formations, substitutions, officials, venue, attendance and weather, when the run had a match sheet. |

## Where things live

| Path | Contents |
|---|---|
| `src/App.jsx` | Page layout and run state (normal runs and live feed) |
| `src/api.js` | Fetch and NDJSON stream helpers, one per endpoint |
| `src/components/` | One component per panel |
| `src/components/narrative/` | Match report and tracker pieces: pitch, timeline, stats, badges, and the pure helpers in `matchInsights.js` |
| `src/components/ui/` | shadcn/ui primitives |
