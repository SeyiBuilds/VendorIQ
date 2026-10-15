import { CheckCircle2, FastForward, Pause, Play, Radio, XCircle } from "lucide-react";
import { Card } from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

const STAGE_SHORT = {
  format_detective: "Detect",
  schema_mapper: "Map",
  coordinate_translator: "Coords",
  entity_resolver: "Resolve",
  match_sheet_mapper: "Sheet",
  validator: "Validate",
  narrative_generator: "Narrate",
};

const STAGE_DOT = {
  success: "bg-success",
  failed: "bg-destructive",
  skipped: "bg-muted-foreground/40",
};

// Wall-clock seconds per match minute.
const SPEEDS = [
  { value: 0.5, label: "Fast" },
  { value: 2, label: "Normal" },
  { value: 5, label: "Watch" },
];

function halfLabel(period) {
  return { 1: "1st half", 2: "2nd half", 3: "Extra time", 4: "Extra time", 5: "Penalty shootout (not match play)" }[period] || "";
}

// Live feed replay status: match clock, how much of the feed has arrived,
// and what the latest full pipeline run (one per match minute) reported.
// The match report below it is what actually changes with each update.
export default function LiveFeedPanel({ live, onTogglePause, onSpeedChange, onSkipToEnd }) {
  if (!live) return null;
  const { status, tick, updates, updateCount, totalEvents, log, detail, replayable, secondsPerMinute, paused } = live;
  const running = status === "connecting" || status === "running";
  const received = tick?.received ?? 0;
  const total = tick?.total ?? totalEvents ?? 0;
  const pct = total ? (received / total) * 100 : 0;

  return (
    <Card className="gap-0 overflow-hidden border-primary/30 py-0">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-border px-5 py-4">
        <h2 className="flex items-center gap-2 text-base font-bold">
          <Radio className="size-4 text-primary" />
          Live feed
        </h2>
        {running && paused ? (
          <Badge variant="warning">Paused</Badge>
        ) : running ? (
          <span className="inline-flex items-center gap-2 rounded-full bg-destructive/15 px-2.5 py-0.5 text-[0.7rem] font-bold tracking-wide text-destructive uppercase">
            <span className="relative flex size-2">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-destructive opacity-70 motion-reduce:animate-none" />
              <span className="relative inline-flex size-2 rounded-full bg-destructive" />
            </span>
            Live
          </span>
        ) : status === "done" ? (
          <Badge variant="success">Feed complete</Badge>
        ) : (
          <Badge variant="destructive">Stopped</Badge>
        )}
        {running && replayable !== false && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Button size="sm" variant="secondary" onClick={onTogglePause} aria-pressed={paused}>
              {paused ? <Play /> : <Pause />}
              {paused ? "Resume" : "Pause"}
            </Button>
            <div className="inline-flex rounded-md border border-border bg-muted p-0.5" role="group" aria-label="Replay speed">
              {SPEEDS.map((s) => (
                <button
                  key={s.value}
                  type="button"
                  aria-pressed={secondsPerMinute === s.value}
                  onClick={() => onSpeedChange(s.value)}
                  title={`${s.value}s per match minute`}
                  className={cn(
                    "rounded px-2.5 py-1 text-xs font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                    secondsPerMinute === s.value
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
            <Button size="sm" variant="ghost" onClick={onSkipToEnd}>
              <FastForward />
              Full time
            </Button>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-5 px-5 py-5">
        {replayable === false ? (
          <p className="text-sm text-muted-foreground">{detail}</p>
        ) : (
          <>
            <div className="flex flex-wrap items-end gap-x-8 gap-y-4">
              <div className="flex flex-col">
                <span className="font-mono text-5xl font-bold tracking-tight tabular-nums">
                  {tick ? `${tick.minute}'` : "--"}
                </span>
                <span className="mt-1 text-xs text-muted-foreground">
                  {tick ? halfLabel(tick.period) : "Waiting for the first minute"}
                </span>
              </div>

              <div className="flex min-w-[220px] flex-1 flex-col gap-2">
                <div className="flex items-baseline justify-between text-sm">
                  <span className="font-semibold">Events received</span>
                  <span className="font-mono tabular-nums text-muted-foreground">
                    {received} / {total}
                  </span>
                </div>
                <div className="h-2 rounded-full bg-white/[0.06]">
                  <div
                    className="h-full rounded-full bg-primary transition-[width] duration-300 motion-reduce:transition-none"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <span className="text-xs text-muted-foreground">
                  Update {updateCount}
                  {updates ? ` of ${updates}` : ""}. Each update is a full 7-agent pipeline run on everything
                  received so far.
                </span>
              </div>
            </div>

            {tick && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-border bg-muted/60 px-4 py-3">
                <span className="text-xs font-bold tracking-widest text-muted-foreground uppercase">Latest run</span>
                <div className="flex flex-wrap items-center gap-2">
                  {tick.stages.map((s) => (
                    <span key={s.stage} className="inline-flex items-center gap-1.5 text-xs text-muted-foreground" title={`${s.stage}: ${s.status}`}>
                      <span className={cn("size-2 rounded-full", STAGE_DOT[s.status] ?? "bg-muted-foreground")} />
                      {STAGE_SHORT[s.stage] ?? s.stage}
                    </span>
                  ))}
                </div>
                <span className="ml-auto inline-flex items-center gap-1.5 text-xs font-semibold">
                  {tick.overall_status === "failed" ? (
                    <XCircle className="size-4 text-destructive" />
                  ) : (
                    <CheckCircle2 className="size-4 text-success" />
                  )}
                  {tick.overall_status}
                  {tick.quality_confidence != null && (
                    <span className="font-mono font-normal text-muted-foreground">
                      · confidence {(tick.quality_confidence * 100).toFixed(1)}%
                    </span>
                  )}
                </span>
              </div>
            )}

            {log.length > 0 && (
              <ol className="flex flex-col gap-1.5" aria-live="polite">
                {log.map((entry) => (
                  <li key={entry.id} className="flex items-baseline gap-3 text-sm">
                    <span className="w-10 shrink-0 font-mono text-xs text-muted-foreground tabular-nums">{entry.minute}'</span>
                    <span className={cn(entry.highlight ? "font-semibold" : "text-muted-foreground")}>{entry.text}</span>
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </div>
    </Card>
  );
}
