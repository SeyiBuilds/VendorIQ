import { useEffect, useState } from "react";
import { listMutations, corruptAndRunStream } from "../api";

export default function CorruptionPanel({ matchId, onStart, onEvent, cleanConfidence }) {
  const [mutations, setMutations] = useState({});
  const [selected, setSelected] = useState(new Set());
  const [rate, setRate] = useState(0.25);
  const [seed, setSeed] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);

  useEffect(() => {
    listMutations().then(setMutations).catch((e) => setError(e.message));
  }, []);

  const toggle = (name) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  };

  const handleCorrupt = async () => {
    if (selected.size === 0) return;
    setRunning(true);
    setError(null);
    setLastResult(null);
    onStart();
    try {
      await corruptAndRunStream(
        matchId,
        { mutations: Array.from(selected), rate, seed: seed === "" ? null : Number(seed) },
        (event) => {
          onEvent(event);
          if (event.type === "result") setLastResult(event.data);
        },
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  if (!matchId) return null;

  const corruptedConfidence = lastResult?.document?.metadata?.quality_confidence;

  return (
    <div className="panel corruption-panel">
      <h2>Break it live — corrupt this feed on stage</h2>
      <p className="hint">
        Each toggle below targets one specific, already-handled failure mode in the pipeline — not
        random noise. Pick one or more, set how much of the feed to hit, and run it to watch the
        same pipeline absorb it.
      </p>

      <div className="mutation-grid">
        {Object.entries(mutations).map(([name, description]) => (
          <label key={name} className="mutation-item">
            <input type="checkbox" checked={selected.has(name)} onChange={() => toggle(name)} />
            <span>
              <strong>{name.replace(/_/g, " ")}</strong>
              <span className="mutation-desc">{description}</span>
            </span>
          </label>
        ))}
      </div>

      <div className="field-row">
        <label>
          Corruption rate
          <input
            type="range"
            min={0.05}
            max={0.8}
            step={0.05}
            value={rate}
            onChange={(e) => setRate(Number(e.target.value))}
          />
          <span>{Math.round(rate * 100)}%</span>
        </label>
        <label>
          Seed (optional, for a reproducible take)
          <input type="number" value={seed} onChange={(e) => setSeed(e.target.value)} placeholder="random" />
        </label>
        <button className="primary danger" onClick={handleCorrupt} disabled={running || selected.size === 0}>
          {running ? "Corrupting…" : "Corrupt & run"}
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {lastResult && (
        <div className="corruption-result">
          <div className="confidence-compare">
            <span>Clean confidence: {cleanConfidence != null ? `${(cleanConfidence * 100).toFixed(1)}%` : "—"}</span>
            <span>→</span>
            <span className="degraded">Corrupted confidence: {(corruptedConfidence * 100).toFixed(1)}%</span>
            <span>
              {lastResult.document.events.length} events survived validation (started from 300)
            </span>
          </div>
          <ul className="corruption-log">
            {lastResult.corruption_log.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}
