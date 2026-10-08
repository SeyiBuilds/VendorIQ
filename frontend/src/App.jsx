import { useEffect, useRef, useState } from "react";
import { AlertCircle } from "lucide-react";
import { listMatches, runMatchStream } from "./api";
import MatchPicker from "./components/MatchPicker";
import PipelineStatus from "./components/PipelineStatus";
import NarrativePanel from "./components/NarrativePanel";
import CorruptionPanel from "./components/CorruptionPanel";
import UploadPanel from "./components/UploadPanel";
import ExportPanel from "./components/ExportPanel";
import MatchSheetPanel from "./components/MatchSheetPanel";
import { Alert, AlertDescription } from "./components/ui/alert";

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
  // Scroll target for the Outputs section -- once a run actually finishes
  // (CDF document produced), jump the person down to it instead of leaving
  // them to scroll past the pipeline stage list themselves.
  const outputsRef = useRef(null);

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
      // Wait a tick for the Outputs section to actually render the new
      // result before scrolling to it.
      if (event.data.document) {
        setTimeout(() => outputsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
      }
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
    <div className="mx-auto max-w-6xl px-5 pb-16">
      <header className="sticky top-0 z-20 -mx-5 border-b border-border/60 bg-background/95 px-5 py-4 backdrop-blur-sm">
        <h1 className="text-lg font-bold tracking-tight">Synthetic Match Insights Engine</h1>
        <p className="mt-0.5 truncate text-sm text-muted-foreground">
          Turns raw match feeds into explainable match intelligence, in real time.
        </p>
      </header>

      {error && (
        <Alert variant="destructive" className="mb-5">
          <AlertCircle />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <section className="mb-8">
        <p className="mb-3 ml-0.5 text-xs font-bold tracking-widest text-muted-foreground uppercase">
          Bring data in
        </p>
        <div className="grid items-start gap-6 md:grid-cols-2">
          <UploadPanel onStart={() => handleStreamStart("upload")} onEvent={applyStreamEvent} />
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
        </div>
      </section>

      {pipelineSource === "upload" && (
        <div className="mb-8">
          <PipelineStatus
            records={records}
            overallStatus={runResult?.overall_status}
            confidence={runResult?.document?.metadata?.quality_confidence}
          />
        </div>
      )}

      <section className="mb-8">
        <p className="mb-3 ml-0.5 text-xs font-bold tracking-widest text-muted-foreground uppercase">
          Resilience
        </p>
        <CorruptionPanel
          matchId={selectedMatchId}
          onStart={() => handleStreamStart("match")}
          onEvent={applyStreamEvent}
          cleanConfidence={cleanConfidence}
        />
      </section>

      {pipelineSource === "match" && (
        <div className="mb-8">
          <PipelineStatus
            records={records}
            overallStatus={runResult?.overall_status}
            confidence={runResult?.document?.metadata?.quality_confidence}
          />
        </div>
      )}

      <section ref={outputsRef} className="mb-8 flex scroll-mt-20 flex-col gap-6">
        <p className="-mb-3 ml-0.5 text-xs font-bold tracking-widest text-muted-foreground uppercase">
          Outputs
        </p>
        <ExportPanel runId={runResult?.run_id} hasDocument={!!runResult?.document} document={runResult?.document} />
        <MatchSheetPanel matchSheet={runResult?.document?.match_sheet} />
        {runResult && <NarrativePanel narrative={runResult.narrative} />}
      </section>
    </div>
  );
}
