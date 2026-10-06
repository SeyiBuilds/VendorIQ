import { useState } from "react";
import { ingestRunStream } from "../api";

export default function UploadPanel({ onStart, onEvent }) {
  const [fileName, setFileName] = useState("");
  const [payload, setPayload] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  const handleFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    setPayload(null);
    setFileName("");
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("file must contain a single JSON object, not an array or scalar");
      }
      setPayload(parsed);
      setFileName(file.name);
    } catch (err) {
      setError(`Could not read ${file.name}: ${err.message}`);
    }
  };

  const handleRun = async () => {
    if (!payload) return;
    setRunning(true);
    setError(null);
    onStart();
    try {
      await ingestRunStream(payload, onEvent);
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="panel upload-panel">
      <h2>Bring your own match data</h2>
      <p className="hint">
        Upload a vendor-shaped match feed — Opta, StatsBomb, or Wyscout JSON, exactly as that vendor
        delivers it — and run it through the same 7-agent pipeline. Format Detective auto-detects
        which vendor it is; nothing to select up front. An unrecognized shape won't crash or
        fabricate data — it degrades to a zero-confidence, zero-event document with the reason
        logged, same as any other data-quality fallback in this pipeline.
      </p>

      <div className="field-row">
        <label>
          Match file (.json)
          <input type="file" accept="application/json,.json" onChange={handleFile} />
        </label>
        <button className="primary" onClick={handleRun} disabled={!payload || running}>
          {running ? "Running pipeline…" : "Run uploaded match"}
        </button>
      </div>

      {fileName && !error && <p className="hint">Loaded: {fileName}</p>}
      {error && <div className="error-banner">{error}</div>}
    </div>
  );
}
