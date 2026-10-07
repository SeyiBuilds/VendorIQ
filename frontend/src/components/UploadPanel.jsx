import { useState } from "react";
import { unzipSync, strFromU8 } from "fflate";
import { ingestRunStream } from "../api";

const MAX_FILES_DISPLAYED = 8;

function isZip(file) {
  return /\.zip$/i.test(file.name) || file.type === "application/zip" || file.type === "application/x-zip-compressed";
}

function parseJsonEntry(text) {
  const parsed = JSON.parse(text);
  // Accept a bare array too -- real StatsBomb open-data event files are
  // shaped exactly that way (a top-level list of event objects, no
  // wrapper). The backend wraps it as {"events": [...]} before detection;
  // this just rejects anything that isn't an object or a non-empty array.
  const isUsableArray = Array.isArray(parsed) && parsed.length > 0 && parsed.every((e) => typeof e === "object" && e !== null);
  const isUsableObject = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed);
  if (!isUsableObject && !isUsableArray) {
    throw new Error("must contain a JSON object, or a non-empty array of event objects");
  }
  return parsed;
}

// Expands whatever the person selected into a flat list of { label, payload }
// items -- one per vendor-shaped JSON document. A .zip is unzipped entirely
// client-side (fflate, no upload round-trip just to look inside it); every
// .json entry inside it becomes its own item, same as a loose .json file
// picked directly. Nothing here pairs files together (e.g. a StatsBomb
// events file with its matching lineups file in the same zip) -- each JSON
// document is still run through the pipeline as its own independent vendor
// payload, exactly like a single upload always has been.
async function expandFiles(fileList) {
  const items = [];
  const loadErrors = [];

  for (const file of fileList) {
    if (isZip(file)) {
      let unzipped;
      try {
        const buffer = await file.arrayBuffer();
        unzipped = unzipSync(new Uint8Array(buffer));
      } catch (err) {
        loadErrors.push(`${file.name}: could not unzip (${err.message})`);
        continue;
      }
      for (const [entryName, data] of Object.entries(unzipped)) {
        if (entryName.endsWith("/") || !entryName.toLowerCase().endsWith(".json")) continue;
        try {
          const payload = parseJsonEntry(strFromU8(data));
          items.push({ label: `${file.name} → ${entryName}`, payload });
        } catch (err) {
          loadErrors.push(`${file.name} → ${entryName}: ${err.message}`);
        }
      }
    } else {
      try {
        const payload = parseJsonEntry(await file.text());
        items.push({ label: file.name, payload });
      } catch (err) {
        loadErrors.push(`${file.name}: ${err.message}`);
      }
    }
  }

  return { items, loadErrors };
}

export default function UploadPanel({ onStart, onEvent }) {
  const [items, setItems] = useState([]);
  const [loadErrors, setLoadErrors] = useState([]);
  const [results, setResults] = useState([]);
  const [progress, setProgress] = useState(null); // { index, total, label }
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  const handleFiles = async (e) => {
    const fileList = Array.from(e.target.files || []);
    if (fileList.length === 0) return;
    setError(null);
    setResults([]);
    setProgress(null);
    const { items: loaded, loadErrors: errs } = await expandFiles(fileList);
    setItems(loaded);
    setLoadErrors(errs);
  };

  const handleRunAll = async () => {
    if (items.length === 0) return;
    setRunning(true);
    setError(null);
    setResults([]);

    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      setProgress({ index: i + 1, total: items.length, label: item.label });
      onStart();
      let captured = null;
      try {
        await ingestRunStream(item.payload, (event) => {
          onEvent(event);
          if (event.type === "result") captured = event.data;
        });
        setResults((prev) => [...prev, {
          label: item.label,
          status: captured?.overall_status ?? "unknown",
          confidence: captured?.document?.metadata?.quality_confidence ?? null,
        }]);
      } catch (err) {
        setResults((prev) => [...prev, { label: item.label, status: "error", error: err.message }]);
      }
    }

    setProgress(null);
    setRunning(false);
  };

  const shown = items.slice(0, MAX_FILES_DISPLAYED);
  const extra = items.length - shown.length;

  return (
    <div className="panel upload-panel">
      <h2>Bring your own match data</h2>
      <p className="hint">
        Upload vendor-shaped match feeds — Opta, StatsBomb, or Wyscout JSON, exactly as that vendor
        delivers it, including a bare array of events (how StatsBomb's own public open-data files
        ship, no wrapper object) — and run them through the same 7-agent pipeline. Pick several
        .json files at once, or a .zip containing any number of them (unzipped right here in the
        browser, nothing uploaded until you hit run); each one runs as its own independent pipeline
        run, one after another. Format Detective auto-detects the vendor for each -- nothing to
        select up front. An unrecognized shape won't crash or fabricate data -- it degrades to a
        zero-confidence, zero-event document with the reason logged, same as any other data-quality
        fallback here.
      </p>

      <div className="field-row">
        <label>
          Match file(s) (.json or .zip)
          <input
            type="file"
            accept="application/json,.json,.zip,application/zip,application/x-zip-compressed"
            multiple
            onChange={handleFiles}
            disabled={running}
          />
        </label>
        <button className="primary" onClick={handleRunAll} disabled={items.length === 0 || running}>
          {running
            ? `Running ${progress ? `${progress.index}/${progress.total}` : "…"}`
            : `Run ${items.length > 1 ? `all ${items.length} matches` : "uploaded match"}`}
        </button>
      </div>

      {items.length > 0 && (
        <p className="hint">
          Loaded {items.length} match file{items.length === 1 ? "" : "s"}:{" "}
          {shown.map((it) => it.label).join(", ")}
          {extra > 0 && ` + ${extra} more`}
        </p>
      )}

      {progress && <p className="hint">Running {progress.index} of {progress.total}: {progress.label}</p>}

      {loadErrors.length > 0 && (
        <details className="narrative-section data-quality-notes">
          <summary>{loadErrors.length} file(s) skipped while loading</summary>
          <ul>{loadErrors.map((e, i) => <li key={i}>{e}</li>)}</ul>
        </details>
      )}

      {error && <div className="error-banner">{error}</div>}

      {results.length > 0 && (
        <table className="stats-table upload-results">
          <thead>
            <tr><th>File</th><th>Status</th><th>Confidence</th></tr>
          </thead>
          <tbody>
            {results.map((r, i) => (
              <tr key={i}>
                <td>{r.label}</td>
                <td>{r.error ? `error: ${r.error}` : r.status}</td>
                <td>{r.confidence != null ? `${(r.confidence * 100).toFixed(1)}%` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
