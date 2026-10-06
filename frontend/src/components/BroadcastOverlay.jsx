import { useEffect, useMemo, useRef, useState } from "react";

const MATCH_DURATION_SECONDS = 2 * 2700; // two 45-minute periods, matches PERIOD_LENGTH_SECONDS in overlay_renderer.py
const PLAYBACK_SPEEDS = [60, 180, 360];

function formatClock(absoluteSecond) {
  const period = absoluteSecond < 2700 ? 1 : 2;
  const inPeriod = absoluteSecond < 2700 ? absoluteSecond : absoluteSecond - 2700;
  const minute = Math.floor(inPeriod / 60) + (period === 2 ? 45 : 0);
  const second = Math.floor(inPeriod % 60);
  return `${minute}:${String(second).padStart(2, "0")}${period === 2 ? " (2H)" : " (1H)"}`;
}

export default function BroadcastOverlay({ timeline }) {
  const [clock, setClock] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(PLAYBACK_SPEEDS[1]);
  const frameRef = useRef(null);
  const lastTickRef = useRef(null);

  useEffect(() => {
    if (!playing) {
      if (frameRef.current) cancelAnimationFrame(frameRef.current);
      return;
    }
    lastTickRef.current = performance.now();
    const tick = (now) => {
      const deltaSeconds = (now - lastTickRef.current) / 1000;
      lastTickRef.current = now;
      setClock((c) => {
        const next = c + deltaSeconds * speed;
        if (next >= MATCH_DURATION_SECONDS) {
          setPlaying(false);
          return MATCH_DURATION_SECONDS;
        }
        return next;
      });
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frameRef.current);
  }, [playing, speed]);

  const cues = timeline?.cues ?? [];

  const currentScoreBug = useMemo(() => {
    const scoreBugs = cues.filter((c) => c.cue_type === "score_bug" && c.absolute_second <= clock);
    return scoreBugs.length ? scoreBugs[scoreBugs.length - 1] : null;
  }, [cues, clock]);

  const activeCues = useMemo(
    () =>
      cues.filter(
        (c) =>
          c.cue_type !== "score_bug" &&
          c.absolute_second <= clock &&
          clock <= c.absolute_second + (c.duration_seconds ?? 0),
      ),
    [cues, clock],
  );

  if (!timeline) return null;

  return (
    <div className="panel broadcast-overlay">
      <h2>Render — synchronized broadcast overlay</h2>
      <div className="broadcast-surface">
        <div className="score-bug">
          {currentScoreBug ? currentScoreBug.title : "— : —"}
        </div>
        <div className="match-clock">{formatClock(clock)}</div>

        <div className="cue-stack">
          {activeCues.map((c) => (
            <div key={c.cue_id} className={`cue cue-${c.cue_type}`}>
              <span className="cue-title">{c.title}</span>
              {c.subtitle && <span className="cue-subtitle">{c.subtitle}</span>}
            </div>
          ))}
        </div>
      </div>

      <div className="playback-controls">
        <button onClick={() => setPlaying((p) => !p)}>{playing ? "Pause" : "Play"}</button>
        <input
          type="range"
          min={0}
          max={MATCH_DURATION_SECONDS}
          step={1}
          value={clock}
          onChange={(e) => setClock(Number(e.target.value))}
        />
        <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
          {PLAYBACK_SPEEDS.map((s) => (
            <option key={s} value={s}>{s}x</option>
          ))}
        </select>
      </div>
      <p className="hint">
        Every cue carries an exact absolute-second timestamp (period offset applied) from Overlay
        Renderer — this scrubber is just playing that timeline back; a real broadcast graphics
        engine would consume the same cue list.
      </p>
    </div>
  );
}
