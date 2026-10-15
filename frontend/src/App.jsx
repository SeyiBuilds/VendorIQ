import { useEffect, useRef, useState } from "react";
import { AlertCircle, Goal, Sparkles } from "lucide-react";
import { ingestLiveStream, listMatches, liveMatchStream, runMatchStream } from "./api";
import MatchPicker from "./components/MatchPicker";
import PipelineStatus from "./components/PipelineStatus";
import LiveFeedPanel from "./components/LiveFeedPanel";
import { LiveMatchTracker } from "./components/narrative/MatchTracker";
import { CARD_LABELS } from "./components/narrative/matchInsights";
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

const LIVE_LOG_LIMIT = 6;
const DEFAULT_LIVE_SPEED = 2; // wall-clock seconds per match minute

function momentKey(m) {
  return `${m.period}-${m.second_in_period}-${m.event_type}-${m.team}-${m.player_jersey}`;
}

function momentText(m) {
  const kind =
    m.event_type === "goal"
      ? "Goal"
      : m.event_type === "card"
        ? CARD_LABELS[m.card_type] || "Card (colour not in feed)"
        : m.event_type;
  const who = m.player_name || (m.player_jersey != null ? `#${m.player_jersey}` : null);
  return `${kind}, ${m.team}${who ? ` (${who})` : ""}`;
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
  // Live feed replay state (see LiveFeedPanel / backend live_feed.py).
  // Each tick ships only the events that arrived that minute, so the
  // running event list is accumulated here; seenMoments tracks which key
  // moments have already been announced in the live log.
  const [live, setLive] = useState(null);
  const liveEventsRef = useRef([]);
  const seenMomentsRef = useRef(new Set());
  // Live playback happens here, not on the server: the backend streams
  // every minute's update as fast as it computes them, and they wait in
  // this queue to be released one match minute at a time at the chosen
  // speed. That is what lets a viewer pause, change speed or skip ahead
  // mid-match. Each update is still a real pipeline run on the server.
  const liveQueueRef = useRef([]);
  const liveTimerRef = useRef(null);
  const livePausedRef = useRef(false);
  const liveSpeedRef = useRef(DEFAULT_LIVE_SPEED);

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
    setLive(null);
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

  const applyLiveEvent = (event) => {
    if (event.type === "live_start") {
      setLive((l) => ({
        ...l,
        status: "running",
        replayable: event.replayable,
        updates: event.updates,
        totalEvents: event.total_events,
        detail: event.detail,
      }));
    } else if (event.type === "tick") {
      liveEventsRef.current = liveEventsRef.current.concat(event.new_events);
      const document = event.document ? { ...event.document, events: liveEventsRef.current } : null;
      setRunResult({ document, narrative: event.narrative, overall_status: event.overall_status });

      const entries = [];
      for (const m of event.narrative?.key_moments || []) {
        const key = momentKey(m);
        if (!seenMomentsRef.current.has(key)) {
          seenMomentsRef.current.add(key);
          entries.push({ id: key, minute: m.minute, text: momentText(m), highlight: true });
        }
      }
      entries.push({
        id: `tick-${event.period}-${event.minute}`,
        minute: event.minute,
        text: `+${event.new_raw_events} event${event.new_raw_events === 1 ? "" : "s"} · pipeline re-run ${event.overall_status}`,
        highlight: false,
      });

      setLive((l) => ({
        ...l,
        tick: event,
        updateCount: (l?.updateCount ?? 0) + 1,
        // Newest first: this minute's key moments, then its arrival line.
        log: [...entries, ...(l?.log ?? [])].slice(0, LIVE_LOG_LIMIT),
      }));
    } else if (event.type === "result") {
      setRunResult(event.data);
      setRecords(event.data.orchestration_log.records);
      setLive((l) => ({ ...l, status: "done", paused: false }));
      setRunning(false);
    }
  };

  // Releases queued live events: everything up to and including the next
  // minute's update, then waits the chosen time before the following one.
  const pumpLive = () => {
    if (livePausedRef.current || liveTimerRef.current) return;
    while (liveQueueRef.current.length) {
      const next = liveQueueRef.current.shift();
      applyLiveEvent(next);
      if (next.type === "tick" && !next.final) {
        liveTimerRef.current = setTimeout(() => {
          liveTimerRef.current = null;
          pumpLive();
        }, liveSpeedRef.current * 1000);
        return;
      }
    }
  };

  const stopLivePlayback = () => {
    clearTimeout(liveTimerRef.current);
    liveTimerRef.current = null;
    liveQueueRef.current = [];
  };

  const toggleLivePause = () => {
    livePausedRef.current = !livePausedRef.current;
    setLive((l) => ({ ...l, paused: livePausedRef.current }));
    if (livePausedRef.current) {
      clearTimeout(liveTimerRef.current);
      liveTimerRef.current = null;
    } else {
      pumpLive();
    }
  };

  const setLivePlaybackSpeed = (secondsPerMinute) => {
    liveSpeedRef.current = secondsPerMinute;
    setLive((l) => (l ? { ...l, secondsPerMinute } : l));
    // Restart the current wait at the new speed.
    if (liveTimerRef.current) {
      clearTimeout(liveTimerRef.current);
      liveTimerRef.current = setTimeout(() => {
        liveTimerRef.current = null;
        pumpLive();
      }, secondsPerMinute * 1000);
    }
  };

  // Releases everything already computed. If the server is still working,
  // later updates arrive straight away too.
  const skipLiveToEnd = () => {
    liveSpeedRef.current = 0;
    livePausedRef.current = false;
    clearTimeout(liveTimerRef.current);
    liveTimerRef.current = null;
    setLive((l) => ({ ...l, paused: false, skipped: true }));
    pumpLive();
  };

  // Shared by both live sources (a sample match, or a single uploaded file).
  const handleLive = async (startStream) => {
    stopLivePlayback();
    livePausedRef.current = false;
    if (liveSpeedRef.current === 0) liveSpeedRef.current = DEFAULT_LIVE_SPEED;
    setRunning(true);
    setError(null);
    setRunResult(null);
    setRecords([]);
    setPipelineSource("live");
    liveEventsRef.current = [];
    seenMomentsRef.current = new Set();
    setLive({ status: "connecting", log: [], updateCount: 0, secondsPerMinute: liveSpeedRef.current, paused: false });
    scrollToPipeline();
    try {
      await startStream((event) => {
        liveQueueRef.current.push(event);
        pumpLive();
      });
    } catch (e) {
      stopLivePlayback();
      setError(e.message);
      setLive((l) => ({ ...l, status: "error" }));
      setRunning(false);
    }
  };

  // seconds_per_minute=0: the server computes every update straight away;
  // pacing happens in pumpLive above.
  const handleLiveMatch = () =>
    handleLive((onEvent) => liveMatchStream(selectedMatchId, selectedVendor, 0, onEvent));
  const handleLiveUpload = (payload) => handleLive((onEvent) => ingestLiveStream(payload, 0, onEvent));

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
            onLive={handleLiveUpload}
            liveBusy={running}
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
            onLive={handleLiveMatch}
            running={running}
          />
        </div>
      </section>

      {pipelineSource === "live" && (
        <div ref={pipelineRef} className="mb-10 flex scroll-mt-24 flex-col gap-6">
          <LiveFeedPanel
            live={live}
            onTogglePause={toggleLivePause}
            onSpeedChange={setLivePlaybackSpeed}
            onSkipToEnd={skipLiveToEnd}
          />
          {runResult?.document?.events?.length > 0 && (
            <LiveMatchTracker document={runResult.document} narrative={runResult.narrative} live={live} />
          )}
          {runResult?.narrative && (
            <NarrativePanel
              narrative={runResult.narrative}
              document={runResult.document}
              live={{ status: live?.status, minute: live?.tick?.minute, period: live?.tick?.period }}
            />
          )}
        </div>
      )}

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
        {/* A live run shows its match report next to the live feed panel
            instead, where it updates minute by minute. */}
        {runResult && pipelineSource !== "live" && (
          <NarrativePanel narrative={runResult.narrative} document={runResult.document} />
        )}
      </section>
    </div>
  );
}
