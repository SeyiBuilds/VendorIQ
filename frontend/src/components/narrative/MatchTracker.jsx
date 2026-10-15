import { useEffect, useMemo, useRef, useState } from "react";
import { Goal, Radio } from "lucide-react";
import { Card } from "../ui/card";
import TeamBadge from "./TeamBadge";
import { PitchLines } from "./ShotMap";
import { teamPalette } from "./teamColors";
import {
  CARD_LABELS, cardColor, describeEvent, eventMinute, playerLabel, playerLookup, resolveTeams, shortName,
} from "./matchInsights";

// Live match tracker. Plays the received CDF events back on a pitch in step
// with the live feed: the ball glides through each recorded action (along a
// pass to its end point, a shot to where it ended), the player on the ball
// is marked with his number, shots stay on the pitch afterwards, and goals
// and cards get an overlay. Only what event data records is drawn: the ball
// and the player acting on it. Positions of the other players are tracking
// data, which no event feed carries, so they are not invented here.

const W = 105;
const H = 68;
const px = (x) => (x / 100) * W;
const py = (y) => (y / 100) * H;

// Periods are laid end to end on one playback axis; the gap between them is
// skipped over (with a half-time beat) rather than played through.
const PERIOD_SPAN = 100000;
const g = (e) => (e.period - 1) * PERIOD_SPAN + e.timestamp;

const QUIET = new Set(["other"]);
const OVERLAY_MS = 2600;

function easeInOut(t) {
  return t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
}

function clockText(e) {
  return `${eventMinute(e)}'`;
}

// Ball keyframes: each event's start point at its own time, then its end
// point (pass target, shot end) after a travel time based on distance.
function buildKeyframes(events) {
  const frames = [];
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    const t = g(e);
    frames.push({ t, x: e.x, y: e.y, i });
    if (e.end_x != null && e.end_y != null) {
      const next = events[i + 1];
      const nextT = next ? g(next) : t + 4;
      const dist = Math.hypot((e.end_x - e.x) * 1.05, (e.end_y - e.y) * 0.68);
      const travel = Math.min(3, Math.max(0.4, dist / 22));
      const endT = Math.min(t + travel, nextT - 0.05);
      if (endT > t + 0.05) frames.push({ t: endT, x: e.end_x, y: e.end_y, i });
    }
  }
  return frames;
}

function lastIndexAtOrBefore(arr, t, key = (a) => a.t) {
  let lo = 0;
  let hi = arr.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (key(arr[mid]) <= t) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}

function ballAt(frames, t) {
  const k = lastIndexAtOrBefore(frames, t);
  if (k < 0) return frames[0] ? { x: frames[0].x, y: frames[0].y } : { x: 50, y: 50 };
  const a = frames[k];
  const b = frames[k + 1];
  if (!b) return { x: a.x, y: a.y };
  const gap = b.t - a.t;
  if (gap > 6) {
    // Stoppage (set piece, injury, goal): hold, then move to the restart.
    const moveFrom = b.t - 1.5;
    if (t < moveFrom) return { x: a.x, y: a.y };
    const u = easeInOut(Math.min(1, (t - moveFrom) / 1.5));
    return { x: a.x + (b.x - a.x) * u, y: a.y + (b.y - a.y) * u };
  }
  const u = easeInOut(Math.min(1, (t - a.t) / gap));
  return { x: a.x + (b.x - a.x) * u, y: a.y + (b.y - a.y) * u };
}

// Which way each team is attacking this half, read from where its shots
// were taken (more than half of them past halfway = attacking right).
function attackDirections(events, period) {
  const dir = {};
  const tally = {};
  for (const e of events) {
    if (e.period !== period || (e.event_type !== "shot" && e.event_type !== "goal")) continue;
    tally[e.team] = tally[e.team] || { right: 0, n: 0 };
    tally[e.team].n += 1;
    if (e.x > 50) tally[e.team].right += 1;
  }
  for (const [team, t] of Object.entries(tally)) {
    if (t.n >= 2) dir[team] = t.right / t.n > 0.5 ? "right" : "left";
  }
  return dir;
}

function commentaryLine(e, players) {
  if (QUIET.has(e.event_type)) return null;
  const kickoff = e.event_type === "pass" && e.x === 50 && e.y === 50;
  // Routine passes are left out, and so are misplaced passes and lost
  // dribbles: the interception or tackle that follows tells that story.
  if (e.event_type === "pass" && !kickoff) return null;
  if (e.event_type === "dribble" && e.outcome === false) return null;
  const who = playerLabel(e.player_id, players);
  let text = describeEvent(e);
  if (e.event_type === "pass" && e.x === 50 && e.y === 50) text = "Kick-off";
  if (e.event_type === "goal") text = "GOAL!";
  return { id: `${e.event_id}-${e.period}-${e.timestamp}`, minute: clockText(e), text, who, team: e.team, e };
}

export default function MatchTracker({ events, home, away, palette, players, secondsPerMinute, status, paused, skipped }) {
  const sorted = useMemo(
    () => [...(events || [])].sort((a, b) => a.period - b.period || a.timestamp - b.timestamp),
    [events],
  );
  const frames = useMemo(() => buildKeyframes(sorted), [sorted]);
  const latestT = sorted.length ? g(sorted[sorted.length - 1]) : 0;

  const [playhead, setPlayhead] = useState(null);
  const [overlay, setOverlay] = useState(null);
  const [halfBanner, setHalfBanner] = useState(null);
  const [feed, setFeed] = useState([]);
  const [trail, setTrail] = useState([]);

  const playheadRef = useRef(null);
  const processedRef = useRef(0);
  const firstIdRef = useRef(null);
  const latestRef = useRef(latestT);
  const framesRef = useRef(frames);
  const sortedRef = useRef(sorted);
  const [trajectory, setTrajectory] = useState(null); // latest shot, drawn briefly
  const trajectoryTimer = useRef(null);
  const overlayQueue = useRef([]);
  const overlayTimer = useRef(null);
  const playersRef = useRef(players);

  // The animation loop reads the latest data through refs, synced after
  // each render, so it never restarts when a new minute arrives.
  useEffect(() => {
    latestRef.current = latestT;
    framesRef.current = frames;
    sortedRef.current = sorted;
    playersRef.current = players;
  }, [latestT, frames, sorted, players]);

  // Match seconds per real second; paused holds the picture still.
  const rate = paused ? 0 : 60 / Math.max(0.05, secondsPerMinute || 2);
  const skippedRef = useRef(false);
  useEffect(() => {
    skippedRef.current = Boolean(skipped);
  }, [skipped]);

  // A new live run (different first event) resets playback.
  const firstId = sorted[0]?.event_id ?? null;
  useEffect(() => {
    if (firstId !== firstIdRef.current) {
      firstIdRef.current = firstId;
      playheadRef.current = sorted.length ? g(sorted[0]) - 0.5 : null;
      processedRef.current = 0;
      overlayQueue.current = [];
      setTrajectory(null);
      setFeed([]);
      setTrail([]);
      setOverlay(null);
      setPlayhead(playheadRef.current);
    }
  }, [firstId, sorted]);

  useEffect(() => {
    const showNextOverlay = () => {
      if (overlayTimer.current || overlayQueue.current.length === 0) return;
      const next = overlayQueue.current.shift();
      setOverlay(next);
      overlayTimer.current = setTimeout(() => {
        overlayTimer.current = null;
        setOverlay(null);
        showNextOverlay();
      }, OVERLAY_MS);
    };

    let raf;
    let last = performance.now();
    let lastTrail = 0;
    const loop = (now) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const fr = framesRef.current;
      const evs = sortedRef.current;
      let p = playheadRef.current;
      if (p != null && fr.length) {
        const latest = latestRef.current;
        // Skipped to full time: go straight to the latest action.
        p = skippedRef.current ? latest + 0.5 : Math.min(p + dt * rate, latest + 0.5);
        // Fell far behind (tab in background): catch up near the live edge.
        if (latest - p > 180 && Math.floor(latest / PERIOD_SPAN) === Math.floor(p / PERIOD_SPAN)) p = latest - 90;
        // Half-time: jump the gap between periods.
        const k = lastIndexAtOrBefore(fr, p);
        const nextFrame = fr[k + 1];
        if (nextFrame && nextFrame.t - p > 600) {
          p = nextFrame.t - 0.5;
          setHalfBanner(`Second half`);
          setTimeout(() => setHalfBanner(null), 1800);
        }
        playheadRef.current = p;
        setPlayhead(p);

        // Events the playhead just passed: commentary, overlays, shots.
        let i = processedRef.current;
        const lines = [];
        while (i < evs.length && g(evs[i]) <= p) {
          const e = evs[i];
          const line = commentaryLine(e, playersRef.current);
          if (line) lines.push(line);
          const isGoal = e.event_type === "goal";
          if (isGoal || (e.card_type && (e.event_type === "card" || e.event_type === "foul"))) {
            const item = { id: line?.id ?? `${i}`, e, who: playerLabel(e.player_id, playersRef.current) };
            if (isGoal) {
              // A goal takes over at once instead of waiting behind a card banner.
              clearTimeout(overlayTimer.current);
              overlayTimer.current = null;
              overlayQueue.current = [item, ...overlayQueue.current.filter((q) => q.e.event_type === "goal")];
            } else {
              overlayQueue.current.push(item);
            }
          }
          if ((e.event_type === "shot" || e.event_type === "goal") && e.end_x != null) {
            setTrajectory(e);
            clearTimeout(trajectoryTimer.current);
            trajectoryTimer.current = setTimeout(() => setTrajectory(null), 1800);
          }
          i += 1;
        }
        if (lines.length) setFeed((f) => [...lines.reverse(), ...f].slice(0, 7));
        processedRef.current = i;
        showNextOverlay();

        if (now - lastTrail > 90) {
          lastTrail = now;
          const b = ballAt(fr, p);
          setTrail((t) => [b, ...t].slice(0, 7));
        }
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
    };
  }, [rate]);

  useEffect(
    () => () => {
      clearTimeout(overlayTimer.current);
      clearTimeout(trajectoryTimer.current);
    },
    [],
  );

  if (!sorted.length || playhead == null) {
    return (
      <Card className="items-center justify-center py-16 text-sm text-muted-foreground">Waiting for the first minute of the feed…</Card>
    );
  }

  const ball = ballAt(frames, playhead);
  const curIdx = lastIndexAtOrBefore(sorted, playhead, g);
  const current = curIdx >= 0 ? sorted[curIdx] : null;
  const actorColor = current ? palette[current.team]?.mark || "#fff" : "#fff";
  const actorFill = current ? palette[current.team]?.fill || "#333" : "#333";
  const jersey = current ? players.get(current.player_id)?.jersey_number : null;
  const actorName = current ? shortName(playerLabel(current.player_id, players)) : null;

  const passedShots = sorted.slice(0, curIdx + 1).filter((e) => e.event_type === "shot" || e.event_type === "goal");
  const score = { [home]: 0, [away]: 0 };
  for (const e of sorted.slice(0, curIdx + 1)) if (e.event_type === "goal" && e.team in score) score[e.team] += 1;
  const dirs = current ? attackDirections(sorted, current.period) : {};
  const minute = current ? clockText(current) : "0'";
  const caughtUp = status === "done" && curIdx === sorted.length - 1;

  return (
    <Card className="gap-0 overflow-hidden py-0">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-5 py-3.5">
        <h2 className="flex items-center gap-2 text-base font-bold">
          <Radio className="size-4 text-primary" />
          Match tracker
        </h2>
        <div className="ml-auto flex items-center gap-3">
          <TeamBadge colors={palette[home]} size={26} />
          <span key={`${score[home]}-${score[away]}`} className="animate-score-pop font-mono text-xl font-bold tabular-nums">
            {score[home]} - {score[away]}
          </span>
          <TeamBadge colors={palette[away]} size={26} />
          <span className="rounded-md bg-muted px-2 py-0.5 font-mono text-sm font-bold tabular-nums">{caughtUp ? "FT" : minute}</span>
        </div>
      </div>

      <div className="grid gap-0 lg:grid-cols-[1fr_300px]">
        <div className="relative p-4">
          <div className="relative overflow-hidden rounded-lg border border-white/10">
            <svg viewBox={`-2 -2 ${W + 4} ${H + 4}`} className="block h-auto w-full" role="img" aria-label="Live match tracker">
              <rect x="-2" y="-2" width={W + 4} height={H + 4} fill="#0d2419" />
              {Array.from({ length: 10 }, (_, i) => (
                <rect key={i} x={(i * W) / 10} y="0" width={W / 10} height={H} fill={i % 2 ? "#10291d" : "#0e261a"} />
              ))}
              <PitchLines />

              {/* Shots so far stay on the pitch. */}
              {passedShots.map((s) => {
                const c = palette[s.team]?.mark || "#fff";
                const onTarget = s.event_type === "goal" || s.shot_outcome === "saved" || (!s.shot_outcome && s.outcome);
                // Earlier-half shots fade back: teams changed ends at half-time.
                const earlierHalf = current && s.period < current.period;
                return (
                  <g key={s.event_id + s.timestamp} className="animate-pop-in" opacity={earlierHalf ? 0.28 : 1}>
                    {s.event_type === "goal" && <circle cx={px(s.x)} cy={py(s.y)} r="2.6" fill="none" stroke={c} strokeOpacity="0.5" strokeWidth="0.45" />}
                    <circle
                      cx={px(s.x)}
                      cy={py(s.y)}
                      r={s.event_type === "goal" ? 1.6 : 1}
                      fill={onTarget ? c : "none"}
                      fillOpacity="0.85"
                      stroke={s.event_type === "goal" ? "#fff" : c}
                      strokeWidth="0.35"
                    />
                  </g>
                );
              })}

              {/* The latest shot's path, drawn out and then faded. */}
              {trajectory && (
                <line
                  key={`traj-${trajectory.event_id}-${trajectory.timestamp}`}
                  x1={px(trajectory.x)}
                  y1={py(trajectory.y)}
                  x2={px(trajectory.end_x)}
                  y2={py(trajectory.end_y)}
                  stroke={palette[trajectory.team]?.mark || "#fff"}
                  strokeWidth="0.45"
                  strokeLinecap="round"
                  pathLength="1"
                  className="animate-draw-line"
                />
              )}

              {/* Recent touches fade behind the ball. */}
              {trail.slice(1).map((t, i) => (
                <circle key={i} cx={px(t.x)} cy={py(t.y)} r={0.55} fill="#ffffff" opacity={0.28 - i * 0.04} />
              ))}

              {/* Player on the ball, at the spot of his latest action. */}
              {current && (
                // Glides to each new action's spot (no remount), so the
                // marker stays readable even when the actor changes often.
                <g
                  style={{
                    transform: `translate(${px(current.x)}px, ${py(current.y)}px)`,
                    transition: "transform 160ms ease-out",
                  }}
                >
                  <circle cx="0" cy="0" r="2.3" fill={actorFill} stroke={actorColor} strokeWidth="0.45" />
                  <text
                    x="0"
                    y="0.85"
                    textAnchor="middle"
                    fontSize="2.3"
                    fontWeight="800"
                    fill={palette[current.team]?.text || "#fff"}
                    fontFamily="JetBrains Mono, monospace"
                  >
                    {jersey ?? ""}
                  </text>
                  {actorName && (
                    <text
                      x="0"
                      y="-3.4"
                      textAnchor="middle"
                      fontSize="2.2"
                      fontWeight="700"
                      fill="#f1edf8"
                      fontFamily="Manrope, system-ui, sans-serif"
                      style={{ paintOrder: "stroke", stroke: "rgba(0,0,0,0.55)", strokeWidth: 0.5 }}
                    >
                      {actorName}
                    </text>
                  )}
                </g>
              )}

              {/* The ball. */}
              <circle cx={px(ball.x)} cy={py(ball.y)} r="0.95" fill="#ffffff" stroke="#0d0620" strokeWidth="0.25" />
            </svg>

            {overlay && <EventOverlay key={overlay.id} overlay={overlay} palette={palette} />}
            {halfBanner && !overlay && (
              <div className="pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2 text-center">
                <span className="animate-banner-in inline-block rounded-lg bg-black/70 px-5 py-2 text-lg font-bold tracking-wide">{halfBanner}</span>
              </div>
            )}
          </div>

          <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              {Object.entries(dirs).map(([team, d]) => (
                <span key={team} className="mr-3" style={{ color: palette[team]?.mark }}>
                  {palette[team]?.code} {d === "right" ? "attack →" : "← attack"}
                </span>
              ))}
            </span>
            <span>Ball and player on the ball, from event data. Event feeds do not track the other players.</span>
          </div>
        </div>

        <div className="flex flex-col gap-2 border-t border-border p-4 lg:border-t-0 lg:border-l">
          <h3 className="text-xs font-bold tracking-widest text-muted-foreground uppercase">Live commentary</h3>
          <ol className="flex flex-col gap-1.5" aria-live="polite">
            {feed.map((line) => (
              <li key={line.id} className="animate-slide-in flex items-start gap-2.5 rounded-md px-2 py-1.5 text-sm odd:bg-white/[0.02]">
                <span className="w-8 shrink-0 font-mono text-xs leading-5 text-muted-foreground tabular-nums">{line.minute}</span>
                <span className="mt-1.5 size-2 shrink-0 rounded-full" style={{ background: palette[line.team]?.mark || "#9d93ab" }} />
                <span className="min-w-0 leading-5">
                  <span className={line.e.event_type === "goal" || line.e.card_type ? "font-bold" : "font-semibold"}>{line.text}</span>
                  {line.who && <span className="text-muted-foreground"> · {line.who}</span>}
                </span>
              </li>
            ))}
            {feed.length === 0 && <li className="text-sm text-muted-foreground">Kick-off coming up…</li>}
          </ol>
        </div>
      </div>
    </Card>
  );
}

function EventOverlay({ overlay, palette }) {
  const { e, who } = overlay;
  const team = palette[e.team];
  if (e.event_type === "goal") {
    return (
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-black/35">
        <div className="animate-banner-in flex flex-col items-center gap-2 rounded-2xl border border-white/15 bg-[#0e0a16]/90 px-8 py-5 text-center shadow-2xl">
          <span className="flex items-center gap-2 text-3xl font-extrabold tracking-wider sm:text-4xl" style={{ color: team?.mark }}>
            <Goal className="size-8" /> GOAL
          </span>
          <span className="text-sm font-semibold">
            {e.team}
            {who ? ` · ${who}` : ""} · {clockText(e)}
          </span>
        </div>
      </div>
    );
  }
  return (
    <div className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2">
      <div className="animate-banner-in flex items-center gap-3 rounded-xl border border-white/15 bg-[#0e0a16]/90 px-4 py-2.5 shadow-xl">
        <span className="block h-7 w-5 rounded-[3px] shadow" style={{ background: cardColor(e.card_type) }} />
        <span className="text-sm">
          <span className="font-bold">{CARD_LABELS[e.card_type] || "Card"}</span>
          <span className="text-muted-foreground">
            {" "}
            · {who || e.team} ({team?.code}) · {clockText(e)}
          </span>
        </span>
      </div>
    </div>
  );
}

// Wrapper used by App: derives teams, colours and player names from the
// live document so the tracker itself only deals with playback.
export function LiveMatchTracker({ document, narrative, live }) {
  const { home, away } = resolveTeams(document, narrative);
  const palette = useMemo(() => teamPalette(home, away), [home, away]);
  const players = useMemo(() => playerLookup(document), [document]);
  return (
    <MatchTracker
      events={document?.events || []}
      home={home}
      away={away}
      palette={palette}
      players={players}
      secondsPerMinute={live?.secondsPerMinute}
      status={live?.status}
      paused={live?.paused}
      skipped={live?.skipped}
    />
  );
}
