import { useEffect, useState } from "react";
import { listMutations, listAgentFaults, corruptAndRunStream } from "../api";

export default function CorruptionPanel({ matchId, onStart, onEvent, cleanConfidence }) {
  const [mutations, setMutations] = useState({});
  const [selected, setSelected] = useState(new Set());
  const [rate, setRate] = useState(0.25);
  const [seed, setSeed] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);

  // Orchestrator-level fault injection -- a DIFFERENT demo than the data
  // mutations above. Mutations show an agent gracefully absorbing bad
  // data; this makes an agent genuinely raise so the Master Orchestrator's
  // own retry-then-decide logic fires on camera. See fault_injection.py.
  const [agentFaults, setAgentFaults] = useState({});
  const [agentFault, setAgentFault] = useState("");
  const [failAttempts, setFailAttempts] = useState(2);

  useEffect(() => {
    listMutations().then(setMutations).catch((e) => setError(e.message));
    listAgentFaults().then(setAgentFaults).catch((e) => setError(e.message));
  }, []);

  const toggle = (name) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  };

  const handleCorrupt = async () => {
    if (selected.size === 0 && !agentFault) return;
    setRunning(true);
    setError(null);
    setLastResult(null);
    onStart();
    try {
      await corruptAndRunStream(
        matchId,
        {
          mutations: Array.from(selected),
          rate,
          seed: seed === "" ? null : Number(seed),
          agentFault: agentFault || null,
          failAttempts,
        },
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
  const hasDocument = !!lastResult?.document;

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
        <button className="primary danger" onClick={handleCorrupt} disabled={running || (selected.size === 0 && !agentFault)}>
          {running ? "Corrupting…" : "Corrupt & run"}
        </button>
      </div>

      <div className="field-row fault-row">
        <h3 className="fault-heading">Or: make the orchestrator decide something</h3>
        <p className="hint">
          A different thing than the mutations above. Mutations show an agent quietly absorbing
          bad data. This makes one agent genuinely fail, so you can watch the Master
          Orchestrator's own retry-then-decide logic run live — self-heal on 1 failure, or
          abort/degrade on 2.
        </p>
        <label>
          Agent to fail
          <select value={agentFault} onChange={(e) => setAgentFault(e.target.value)}>
            <option value="">— none —</option>
            {Object.entries(agentFaults).map(([name, description]) => (
              <option key={name} value={name} title={description}>{name.replace(/_/g, " ")}</option>
            ))}
          </select>
        </label>
        {agentFault && (
          <>
            <label>
              Failures before recovery
              <select value={failAttempts} onChange={(e) => setFailAttempts(Number(e.target.value))}>
                <option value={1}>1 — self-heals on retry</option>
                <option value={2}>2 — genuinely fails (abort/degrade)</option>
              </select>
            </label>
            <p className="hint fault-desc">{agentFaults[agentFault]}</p>
          </>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {lastResult && (
        <div className="corruption-result">
          {!hasDocument ? (
            <div className="confidence-compare">
              <span className="degraded">
                Run aborted — {agentFault.replace(/_/g, " ")} failed {failAttempts} time
                {failAttempts === 1 ? "" : "s"} and the orchestrator gave up rather than return a
                document it couldn't stand behind. Status: {lastResult.overall_status}.
              </span>
            </div>
          ) : (
            <div className="confidence-compare">
              <span>Clean confidence: {cleanConfidence != null ? `${(cleanConfidence * 100).toFixed(1)}%` : "—"}</span>
              <span>→</span>
              <span className="degraded">
                {lastResult.overall_status === "partial" ? "Degraded" : "Corrupted"} confidence:{" "}
                {corruptedConfidence != null ? `${(corruptedConfidence * 100).toFixed(1)}%` : "—"}
              </span>
              <span>
                {lastResult.document.events.length} events survived validation (started from 300)
              </span>
            </div>
          )}
          {lastResult.corruption_log?.length > 0 && (
            <ul className="corruption-log">
              {lastResult.corruption_log.map((line, i) => <li key={i}>{line}</li>)}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
