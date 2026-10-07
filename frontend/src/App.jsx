import { useEffect, useState } from "react";
import { listMatches, runMatchStream } from "./api";
import MatchPicker from "./components/MatchPicker";
import PipelineStatus from "./components/PipelineStatus";
import NarrativePanel from "./components/NarrativePanel";
import CorruptionPanel from "./components/CorruptionPanel";
import UploadPanel from "./components/UploadPanel";
import ExportPanel from "./components/ExportPanel";
import MatchSheetPanel from "./components/MatchSheetPanel";
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
  "match_sheet_mapper",
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
  // Which section triggered the run currently shown in PipelineStatus --
  // there's exactly one shared panel (one real run happens at a time), and
  // it renders right after whichever section started it, instead of at a
  // fixed spot everyone has to scroll to find.
  const [pipelineSource, setPipelineSource] = useState(null); // "upload" | "match"

  useEffect(() => {
    listMatches()
      .then((data) => {
        setMatches(data);
        if (data.length) setSelectedMatchId(data[0].match_id);
      })
      .catch((e) => setError(`Could not reach the API: ${e.message}`));
  }, []);

  // Shared by every run source (sample match, corrupted sample match, or
  // an uploaded file) -- same live stage list, same result shape, so one
  // handler drives PipelineStatus/NarrativePanel/ExportPanel regardless
  // of where the run came from.
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

  const handleStreamStart = (source = "match") => {
    setError(null);
    setRunResult(null);
    setRecords(pendingRecords());
    setPipelineSource(source);
  };

  const handleRun = async () => {
    setRunning(true);
    handleStreamStart("match");
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

      <UploadPanel onStart={() => handleStreamStart("upload")} onEvent={applyStreamEvent} />

      {pipelineSource === "upload" && (
        <PipelineStatus
          records={records}
          overallStatus={runResult?.overall_status}
          confidence={runResult?.document?.metadata?.quality_confidence}
        />
      )}

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
        onStart={() => handleStreamStart("match")}
        onEvent={applyStreamEvent}
        cleanConfidence={cleanConfidence}
      />

      {pipelineSource === "match" && (
        <PipelineStatus
          records={records}
          overallStatus={runResult?.overall_status}
          confidence={runResult?.document?.metadata?.quality_confidence}
        />
      )}

      <ExportPanel runId={runResult?.run_id} hasDocument={!!runResult?.document} document={runResult?.document} />

      <MatchSheetPanel matchSheet={runResult?.document?.match_sheet} />

      {runResult && <NarrativePanel narrative={runResult.narrative} />}
    </div>
  );
}
