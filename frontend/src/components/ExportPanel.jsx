import { useState } from "react";
import { exportRun } from "../api";

const VENDORS = ["opta", "statsbomb", "wyscout"];

// Only shown once a run has produced a document -- export (Agent 7) reads
// the validated CDF document, so there's nothing to export from a failed
// critical-stage run.
export default function ExportPanel({ runId, hasDocument, document: cdfDocument }) {
  const [targetVendor, setTargetVendor] = useState("statsbomb");
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  if (!runId || !hasDocument) return null;

  const handleDownloadCDF = () => {
    if (!cdfDocument) return;
    const blob = new Blob([JSON.stringify(cdfDocument, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${runId}_cdf.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleExport = async () => {
    setExporting(true);
    setError(null);
    try {
      const data = await exportRun(runId, targetVendor);
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setExporting(false);
    }
  };

  const handleDownload = () => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result.payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${runId}_${targetVendor}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="panel export-panel">
      <h2>Export (Agent 7)</h2>

      <div className="field-row">
        <div>
          <p className="hint" style={{ margin: 0 }}>
            The actual output of this pipeline — one normalized document, whatever vendor(s) went in.
          </p>
          <button onClick={handleDownloadCDF} disabled={!cdfDocument}>Download CDF (raw .json)</button>
        </div>
      </div>

      <p className="hint">
        Re-export this run's validated CDF document back into another vendor's shape — on demand,
        never part of the pipeline run itself (see orchestrator.py's design note on why).
      </p>

      <div className="field-row">
        <label>
          Target vendor
          <select value={targetVendor} onChange={(e) => { setTargetVendor(e.target.value); setResult(null); }}>
            {VENDORS.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
        <button className="primary" onClick={handleExport} disabled={exporting}>
          {exporting ? "Exporting…" : "Export"}
        </button>
        {result && (
          <button onClick={handleDownload}>Download {targetVendor}.json</button>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {result && (
        <p className="hint">
          Exported {result.report.events_exported} events to {result.report.target_vendor} shape
          {result.report.players_with_synthesized_id > 0 &&
            ` (${result.report.players_with_synthesized_id} player ID${result.report.players_with_synthesized_id === 1 ? "" : "s"} synthesized -- no native ID for that vendor)`}
          .
        </p>
      )}
    </div>
  );
}
