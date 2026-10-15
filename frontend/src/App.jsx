import { useEffect, useRef, useState } from "react";
import { AlertCircle, Goal, Sparkles } from "lucide-react";
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
  // Scroll target for the live PipelineStatus section -- jumped to the
  // moment a run starts, so the person lands on the stage-by-stage view
  // (where the deliberate per-stage pacing is actually visible) instead of
  // staying wherever they clicked from.
  const pipelineRef = useRef(null);
  // Scroll target for the Outputs section -- only used when something
  // *other* than a single live run wants to jump there (an upload batch
  // finishing). A single run no longer auto-scrolls here: that used to cut
  // the pipeline view short the instant the last stage finished.
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
      // No scroll here: the person already landed on the PipelineStatus
      // section when the run started (see handleStreamStart) and watches
      // it finish there. Jumping to Outputs the instant the last stage
      // completes used to cut that view short before they could see it.
      setRecords(event.data.orchestration_log.records);
    }
  };

  const scrollToOutputs = () => {
    // Wait a tick for the Outputs section to actually render the new
    // result before scrolling to it.
    setTimeout(() => outputsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };

  const scrollToPipeline = () => {
    // Wait a tick for the PipelineStatus section to actually mount (it's
    // conditional on pipelineSource, set just before this is called).
    setTimeout(() => pipelineRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  };

  const handleStreamStart = (source = "match", { scroll = true } = {}) => {
    setError(null);
    setRunResult(null);
    setRecords(pendingRecords());
    setPipelineSource(source);
    // scroll:false is for a multi-file upload batch's per-item onStart --
    // scrolling on every one of potentially hundreds of files would be
    // nothing but jitter. UploadPanel only passes scroll:true on the
    // first item, so the view settles on PipelineStatus once per batch.
    if (scroll) scrollToPipeline();
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
      <header className="flex items-start gap-5 pt-10">
        <div className="relative flex size-16 shrink-0 items-center justify-center rounded-2xl bg-gradient-to-br from-primary to-purple-900 shadow-[0_8px_24px_rgba(157,92,255,0.35)]">
          <Goal className="size-8 text-primary-foreground" />
          <div className="absolute -top-1.5 -right-1.5 flex size-6 items-center justify-center rounded-full bg-background shadow">
            <Sparkles className="size-3.5 text-primary" />
          </div>
        </div>
        <div>
          <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">VendorIQ</h1>
          <p className="mt-2 text-lg text-muted-foreground">
            Turns raw match feeds into explainable match intelligence, in real time.
          </p>
        </div>
      </header>

      {error && (
        <Alert variant="destructive" className="mt-8 mb-5">
          <AlertCircle />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <section className="mt-8 mb-10">
        <p className="mb-4 ml-0.5 text-xs font-bold tracking-widest text-muted-foreground uppercase">
          Bring data in
        </p>
        <div className="grid items-start gap-6 md:grid-cols-2">
          <UploadPanel
            onStart={(scroll = true) => handleStreamStart("upload", { scroll })}
            onEvent={applyStreamEvent}
            onBatchComplete={scrollToOutputs}
          />
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
        <div ref={pipelineRef} className="mb-10 scroll-mt-24">
          <PipelineStatus
            records={records}
            overallStatus={runResult?.overall_status}
            confidence={runResult?.document?.metadata?.quality_confidence}
          />
        </div>
      )}

      <section className="mb-10">
        <p className="mb-4 ml-0.5 text-xs font-bold tracking-widest text-muted-foreground uppercase">
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
        <div ref={pipelineRef} className="mb-10 scroll-mt-24">
          <PipelineStatus
            records={records}
            overallStatus={runResult?.overall_status}
            confidence={runResult?.document?.metadata?.quality_confidence}
          />
        </div>
      )}

      <section ref={outputsRef} className="mb-10 flex scroll-mt-24 flex-col gap-6">
        <p className="ml-0.5 text-xs font-bold tracking-widest text-muted-foreground uppercase">
          Outputs
        </p>
        <ExportPanel runId={runResult?.run_id} hasDocument={!!runResult?.document} document={runResult?.document} />
        <MatchSheetPanel matchSheet={runResult?.document?.match_sheet} />
        {runResult && <NarrativePanel narrative={runResult.narrative} document={runResult.document} />}
      </section>
    </div>
  );
}
