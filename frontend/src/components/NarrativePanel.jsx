import { useMemo, useState } from "react";
import { BookOpen, ChevronDown, Crosshair, Microscope, ShieldAlert, Sparkles, Users } from "lucide-react";
import { Card, CardContent } from "./ui/card";
import { Badge } from "./ui/badge";
import { cn } from "@/lib/utils";
import TeamBadge from "./narrative/TeamBadge";
import ShotMap from "./narrative/ShotMap";
import MatchTimeline from "./narrative/MatchTimeline";
import { PairBars, PossessionDonut, StatCompare, StatTile } from "./narrative/Stats";
import { teamPalette } from "./narrative/teamColors";
import {
  cardColor,
  clockLabel,
  dataCoverage,
  describeEvent,
  eventMinute,
  evidenceFor,
  findTurningPoint,
  goalEvents,
  playerLabel,
  playerLookup,
  resolveTeams,
  shotEvents,
  splitSentences,
  teamStat,
} from "./narrative/matchInsights";

function SectionLabel({ icon: Icon, children, aside }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <h3 className="flex items-center gap-2 text-sm font-bold">
        {Icon && <Icon className="size-4 text-primary" />}
        {children}
      </h3>
      {aside}
    </div>
  );
}

function ViewToggle({ view, onChange }) {
  const options = [
    { id: "fan", label: "Fan view", icon: Users },
    { id: "analyst", label: "Analyst view", icon: Microscope },
  ];
  return (
    <div className="inline-flex rounded-full border border-border bg-muted p-1" role="group" aria-label="Report view">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          aria-pressed={view === o.id}
          onClick={() => onChange(o.id)}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-sm font-semibold transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            view === o.id
              ? "bg-primary text-primary-foreground shadow-[0_4px_14px_rgba(157,92,255,0.35)]"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <o.icon className="size-4" />
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Scoreboard({ home, away, palette, homeStats, awayStats, coverage, method, liveRunning }) {
  const hg = homeStats?.goals ?? 0;
  const ag = awayStats?.goals ?? 0;
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 sm:gap-6">
        <div className="flex min-w-0 flex-col-reverse items-center gap-1.5 sm:flex-row sm:justify-end sm:gap-3">
          <span className="text-center text-sm font-bold sm:text-right sm:text-xl">{home}</span>
          <TeamBadge colors={palette[home]} size={44} />
        </div>
        <div className="flex flex-col items-center">
          {/* Keyed on the score so a goal arriving mid-feed replays the pop. */}
          <span
            key={`${hg}-${ag}`}
            className="animate-score-pop font-mono text-4xl font-bold tracking-tight tabular-nums sm:text-5xl"
          >
            {hg}
            <span className="mx-2 text-muted-foreground">-</span>
            {ag}
          </span>
        </div>
        <div className="flex min-w-0 flex-col items-center gap-1.5 sm:flex-row sm:gap-3">
          <TeamBadge colors={palette[away]} size={44} />
          <span className="text-center text-sm font-bold sm:text-left sm:text-xl">{away}</span>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-center gap-2 text-xs text-muted-foreground">
        <span>
          {liveRunning
            ? `${coverage.eventCount} events so far, live at ${coverage.maxMinute}'`
            : `${coverage.eventCount} events, data through ${coverage.maxMinute}'`}
        </span>
        <span aria-hidden="true">·</span>
        <Badge variant={method === "ai_enhanced" ? "default" : "muted"}>
          {method === "ai_enhanced" ? "AI-enhanced text" : "Template text"}
        </Badge>
      </div>
    </div>
  );
}

function TurningPoint({ turning, evidence, players, palette, home, away, liveRunning }) {
  if (!turning) {
    return (
      <div className="rounded-xl border border-border bg-muted/60 p-5 text-sm text-muted-foreground">
        {liveRunning
          ? "No goals yet. The turning point appears once a goal changes who is ahead."
          : "No goals in this run's data, so there is no turning point to call."}
      </div>
    );
  }
  const g = turning.goal;
  const color = palette[g.team].mark;
  const scorer = playerLabel(g.player_id, players);
  const s = turning.scoreAfter;
  const f = turning.finalScore;
  const settled = s[home] === f[home] && s[away] === f[away];
  const headline = {
    opener: `${g.team} opened the scoring`,
    "go-ahead goal": `${g.team} went back in front`,
    equaliser: `${g.team} drew level`,
  }[turning.kind];
  const after = liveRunning
    ? turning.kind === "equaliser"
      ? "Level since then, so far. Can change while the feed is live."
      : `${g.team} has led since, so far. Can change while the feed is live.`
    : turning.kind === "equaliser"
      ? "Neither side scored again in the data, so it finished level."
      : settled
        ? `${g.team} never gave the lead back.`
        : `${g.team} added to it and kept the lead to the end of the data.`;

  return (
    <div className="flex flex-col gap-4">
      <div
        className="rounded-xl border p-5"
        style={{
          borderColor: `color-mix(in srgb, ${color} 45%, transparent)`,
          background: `linear-gradient(135deg, color-mix(in srgb, ${color} 14%, transparent), transparent 70%)`,
        }}
      >
        <p className="text-xs font-bold tracking-widest uppercase" style={{ color }}>
          Turning point · {eventMinute(g)}'
        </p>
        <p className="mt-2 text-lg leading-snug font-bold text-balance">
          {headline}
          {scorer ? `, ${scorer} scoring` : ""}, making it {s[home]}-{s[away]}.
        </p>
        <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{after}</p>
        <p className="mt-3 text-[11px] text-muted-foreground">
          Rule: the last goal that changed who was ahead. Worked out from the score, not predicted.
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <h4 className="text-xs font-bold tracking-widest text-muted-foreground uppercase">
          Evidence from match events
        </h4>
        <ol className="flex flex-col divide-y divide-border/70 rounded-xl border border-border bg-muted/60">
          {evidence.map((e, i) => {
            const isGoal = e.event_id === g.event_id;
            const who = playerLabel(e.player_id, players);
            return (
              <li key={e.event_id} className={cn("flex items-center gap-3 px-3.5 py-2.5 text-sm", isGoal && "bg-white/[0.03]")}>
                <span className="w-11 shrink-0 font-mono text-xs text-muted-foreground tabular-nums">{clockLabel(e)}</span>
                <span
                  className="flex size-5 shrink-0 items-center justify-center rounded-full font-mono text-[10px] font-bold"
                  style={
                    isGoal
                      ? { background: palette[e.team]?.mark, color: "#0d0620" }
                      : { boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.6)" }
                  }
                >
                  {isGoal ? "G" : i + 1}
                </span>
                <span className={cn("flex-1", isGoal && "font-semibold")}>
                  {describeEvent(e)}
                  {who && <span className="text-muted-foreground"> · {who}</span>}
                </span>
                <span className="shrink-0 text-xs font-semibold" style={{ color: palette[e.team]?.mark }}>
                  {palette[e.team]?.code || e.team}
                </span>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

function KeyMomentList({ moments, palette }) {
  if (!moments.length) return <p className="text-sm text-muted-foreground">No key moments in this run.</p>;
  return (
    <ul className="flex flex-col divide-y divide-border/70 rounded-xl border border-border bg-muted/60">
      {moments.map((m, i) => {
        const sec = Math.floor(m.second_in_period % 60);
        return (
          <li key={i} className="flex items-center gap-3 px-3.5 py-2.5 text-sm">
            <span className="w-[4.75rem] shrink-0 font-mono text-xs whitespace-nowrap text-muted-foreground tabular-nums">
              P{m.period} {m.minute}:{String(sec).padStart(2, "0")}
            </span>
            {m.event_type === "card" ? (
              <span className="block h-3.5 w-2.5 shrink-0 rounded-[2px]" style={{ background: cardColor(m.card_type) }} title={m.card_type || "card"} />
            ) : (
              <span className="size-2.5 shrink-0 rounded-full" style={{ background: palette[m.team]?.mark || "#9d93ab" }} />
            )}
            <span className="flex-1 leading-relaxed">{m.description}</span>
            <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">
              {Math.round(m.confidence * 100)}%
            </span>
            {m.confidence < 0.6 && <Badge variant="warning">low confidence</Badge>}
          </li>
        );
      })}
    </ul>
  );
}

export default function NarrativePanel({ narrative, document, live = null }) {
  const [view, setView] = useState("fan");
  const liveRunning = live != null && (live.status === "running" || live.status === "connecting");

  const model = useMemo(() => {
    if (!narrative) return null;
    const { home, away } = resolveTeams(document, narrative);
    const palette = teamPalette(home, away);
    const players = playerLookup(document);
    const turning = findTurningPoint(goalEvents(document), home, away);
    return {
      home,
      away,
      palette,
      players,
      homeStats: teamStat(narrative, home),
      awayStats: teamStat(narrative, away),
      coverage: dataCoverage(document, narrative),
      shots: shotEvents(document),
      turning,
      evidence: turning ? evidenceFor(turning.goal, document) : [],
      moments: narrative.key_moments || [],
      story: splitSentences(narrative.narrative_text),
      // The fan story drops the template's key-moment and data-quality
      // sentences: the timeline above already shows the moments, and the
      // quality notes live in the Analyst view. Nothing is hidden overall.
      fanStory: splitSentences(narrative.narrative_text).filter(
        (s) => !/^(Key moments|Data quality)\b/.test(s),
      ),
    };
  }, [narrative, document]);

  if (!model) return null;
  const { home, away, palette, homeStats, awayStats } = model;
  const pairRows = (key, kind) =>
    [home, away].map((t) => {
      const v = teamStat(narrative, t)?.[key] ?? null;
      return {
        team: t,
        value: v,
        display: v == null ? "n/a" : kind === "pct" ? `${Math.round(v * 100)}%` : String(v),
      };
    });

  return (
    <Card className="gap-0 overflow-hidden py-0">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4">
        <div className="flex items-center gap-3">
          <h2 className="flex items-center gap-2 text-base font-bold">
            <Sparkles className="size-4 text-primary" />
            Match report
          </h2>
          {liveRunning && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-destructive/15 px-2.5 py-0.5 font-mono text-xs font-bold text-destructive">
              <span className="size-1.5 rounded-full bg-destructive" />
              LIVE {live.minute != null ? `${live.minute}'` : ""}
            </span>
          )}
          {live?.status === "done" && <Badge variant="success">Feed complete</Badge>}
        </div>
        <ViewToggle view={view} onChange={setView} />
      </div>

      <CardContent className="flex flex-col gap-7 py-6">
        <Scoreboard
          home={home}
          away={away}
          palette={palette}
          homeStats={homeStats}
          awayStats={awayStats}
          coverage={model.coverage}
          method={narrative.generation_method}
          liveRunning={liveRunning}
        />

        <section className="flex flex-col gap-3">
          <SectionLabel icon={BookOpen}>Key moments</SectionLabel>
          <MatchTimeline
            moments={model.moments}
            palette={palette}
            maxMinute={model.coverage.maxMinute}
            highlightMinute={model.turning ? eventMinute(model.turning.goal) : null}
          />
        </section>

        {view === "fan" ? (
          <>
            <div className="grid items-start gap-6 lg:grid-cols-2">
              <TurningPoint
                turning={model.turning}
                evidence={model.evidence}
                players={model.players}
                palette={palette}
                home={home}
                away={away}
                liveRunning={liveRunning}
              />
              <section className="flex flex-col gap-3">
                <SectionLabel icon={Crosshair}>Shot map</SectionLabel>
                <ShotMap shots={model.shots} palette={palette} evidence={model.evidence} compact />
              </section>
            </div>

            <div className="grid gap-4 sm:grid-cols-3">
              <StatTile label="Possession" note="event-share proxy">
                <PossessionDonut home={home} away={away} palette={palette} homeStats={homeStats} awayStats={awayStats} />
              </StatTile>
              <StatTile label="Shots" note="bar = all shots">
                <PairBars rows={pairRows("shots")} palette={palette} />
                <p className="text-xs text-muted-foreground">
                  On target: {homeStats?.shots_on_target ?? 0} vs {awayStats?.shots_on_target ?? 0}
                </p>
              </StatTile>
              <StatTile label="Pass accuracy">
                <PairBars rows={pairRows("pass_accuracy", "pct")} palette={palette} max={1} />
                <p className="text-xs text-muted-foreground">
                  Completed: {homeStats?.passes_completed ?? 0}/{homeStats?.passes ?? 0} vs{" "}
                  {awayStats?.passes_completed ?? 0}/{awayStats?.passes ?? 0}
                </p>
              </StatTile>
            </div>

            <section className="flex flex-col gap-3">
              <SectionLabel icon={BookOpen}>The story</SectionLabel>
              <div className="flex max-w-prose flex-col gap-2.5 text-[15px] leading-relaxed text-foreground/85">
                {model.fanStory.map((s, i) => (
                  <p key={i}>{s}</p>
                ))}
              </div>
              {narrative.data_quality_notes.length > 0 && (
                <button
                  type="button"
                  onClick={() => setView("analyst")}
                  className="w-fit text-sm font-semibold text-primary hover:underline"
                >
                  {narrative.data_quality_notes.length} data quality notes in Analyst view
                </button>
              )}
            </section>
          </>
        ) : (
          <>
            <div className="grid items-start gap-6 lg:grid-cols-[1.1fr_1fr]">
              <section className="flex flex-col gap-3">
                <SectionLabel icon={Crosshair}>Shot map</SectionLabel>
                <ShotMap shots={model.shots} palette={palette} evidence={model.evidence} />
              </section>
              <section className="flex flex-col gap-3">
                <SectionLabel icon={Microscope}>Team comparison</SectionLabel>
                <StatCompare home={home} away={away} homeStats={homeStats} awayStats={awayStats} palette={palette} />
                <p className="text-xs leading-relaxed text-muted-foreground">
                  Possession is an event-share proxy (pass, dribble, shot and set-piece share), not tracked ball
                  time, since no vendor here supplies that. Duels won is tackles plus interceptions.
                </p>
              </section>
            </div>

            <section className="flex flex-col gap-3">
              <SectionLabel icon={BookOpen} aside={<span className="text-xs text-muted-foreground">exact time · confidence</span>}>
                Key moment log
              </SectionLabel>
              <KeyMomentList moments={model.moments} palette={palette} />
            </section>

            {narrative.data_quality_notes.length > 0 && (
              <section className="flex flex-col gap-3">
                <SectionLabel icon={ShieldAlert}>Data quality notes ({narrative.data_quality_notes.length})</SectionLabel>
                <ul className="flex list-disc flex-col gap-1.5 rounded-xl border border-warning/25 bg-warning/[0.04] py-3 pr-4 pl-8 text-sm text-muted-foreground">
                  {narrative.data_quality_notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </section>
            )}

            <details className="group">
              <summary className="flex cursor-pointer list-none items-center gap-1.5 text-sm font-semibold text-muted-foreground hover:text-foreground">
                <ChevronDown className="size-4 transition-transform group-open:rotate-180" />
                Full generated report text
              </summary>
              <div className="mt-3 flex max-w-prose flex-col gap-2.5 text-sm leading-relaxed text-muted-foreground">
                {model.story.map((s, i) => (
                  <p key={i}>{s}</p>
                ))}
              </div>
            </details>
          </>
        )}
      </CardContent>
    </Card>
  );
}
