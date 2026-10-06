import { useEffect, useState } from "react";
import { listMatches, runMatchStream } from "./api";
import MatchPicker from "./components/MatchPicker";
import PipelineStatus from "./components/PipelineStatus";
import NarrativePanel from "./components/NarrativePanel";
import CorruptionPanel from "./components/CorruptionPanel";
import "./App.css";

// Fixed display order for the live stage list -- mirrors
// orchestrator.CRITICAL_STAGES + OPTIONAL_STAGES. Every stage starts
// "pending" the moment a run kicks off, so the full pipeline shape is
// visible immediately and fills in live as stream events arrive.
const STAGE_ORDER = [
  "format_detective",
  "schema_mapper",
  "coordinate_translator",
  "entity_resolver",
  "validator",
  "narrative_generator",
];

function pendingRecords() {
  return STAGE_ORDER.map((stage) => ({ stage, status: "pending" }));
}

export default function App() {
  const [matches, setMatches] = useState([]);
  const [selectedMatchId, setSelectedMatchId] = useState("");
  const [selectedVendor, setSelectedVendor] = useState("opta");
  const [running, setRunning] = useState(false);
  const [records, setRecords] = useState([]);
  const [runResult, setRunResult] = useState(null);
  const [cleanConfidence, setCleanConfidence] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    listMatches()
      .then((data) => {
        setMatches(data);
        if (data.length) setSelectedMatchId(data[0].match_id);
      })
      .catch((e) => setError(`Could not reach the API: ${e.message}`));
  }, []);

  const applyStreamEvent = (event) => {
    if (event.type === "start") {
      setRecords((prev) => prev.map((r) => (r.stage === event.stage ? { ...r, status: "running" } : r)));
    } else if (event.type === "stage") {
      setRecords((prev) =>
        prev.map((r) =>
          r.stage === event.stage
            ? { stage: r.stage, status: event.status, detail: event.detail, retried: event.retried }
            : r,
        ),
      );
    } else if (event.type === "result") {
      setRunResult(event.data);
      // Reconcile against the authoritative orchestration log, in case any
      // individual stream event was missed -- same data, just a safety net.
      setRecords(event.data.orchestration_log.records);
    }
  };

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    setRunResult(null);
    setRecords(pendingRecords());
    try {
      await runMatchStream(selectedMatchId, selectedVendor, (event) => {
        applyStreamEvent(event);
        if (event.type === "result") {
          setCleanConfidence(event.data.document?.metadata?.quality_confidence ?? null);
        }
      });
    } catch (e) {
      setError(e.message);
      setRunResult(null);
      setRecords([]);
    } finally {
      setRunning(false);
    }
  };

  const handleCorruptEvent = (event) => {
    applyStreamEvent(event);
  };

  const handleCorruptStart = () => {
    setRunResult(null);
    setRecords(pendingRecords());
  };

  return (
    <div className="app">
      <header className="app-header">
        <h1>Synthetic Match Insights Engine</h1>
        <p className="tagline">
          Inside the Game: Developer Hackathon — turning synthetic football events into explainable
          match intelligence, in real time.
        </p>
      </header>

      {error && <div className="error-banner">{error}</div>}

      <MatchPicker
        matches={matches}
        selectedMatchId={selectedMatchId}
        selectedVendor={selectedVendor}
        onChange={({ matchId, vendor }) => {
          setSelectedMatchId(matchId);
          setSelectedVendor(vendor);
        }}
        onRun={handleRun}
        running={running}
      />

      <CorruptionPanel
        matchId={selectedMatchId}
        onStart={handleCorruptStart}
        onEvent={handleCorruptEvent}
        cleanConfidence={cleanConfidence}
      />

      <PipelineStatus
        records={records}
        overallStatus={runResult?.overall_status}
        confidence={runResult?.document?.metadata?.quality_confidence}
      />

      {runResult && <NarrativePanel narrative={runResult.narrative} />}
    </div>
  );
}
