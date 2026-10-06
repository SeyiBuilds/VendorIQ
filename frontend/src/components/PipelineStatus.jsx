const STATUS_LABEL = {
  pending: "pending",
  running: "running…",
  success: "success",
  failed: "failed",
  skipped: "skipped",
};

// `records` is a live-updating array of { stage, status, detail, retried }
// built up in App.jsx as stream events arrive -- "pending" for an agent
// that hasn't started yet, "running" for one actually in flight right
// now, then "success"/"failed" once its event arrives. This is what
// makes the pipeline visibly work stage by stage instead of appearing
// fully formed after the whole run finishes.
export default function PipelineStatus({ records, overallStatus, confidence }) {
  if (!records || records.length === 0) return null;

  return (
    <div className="panel pipeline-status">
      <div className="pipeline-status-header">
        <h2>Pipeline run</h2>
        {overallStatus && <span className={`badge badge-${overallStatus}`}>{overallStatus}</span>}
        {confidence != null && (
          <span className="confidence">quality confidence: {(confidence * 100).toFixed(1)}%</span>
        )}
      </div>
      <ol className="stage-list">
        {records.map((r) => (
          <li key={r.stage} className={`stage stage-${r.status}`}>
            <span className="stage-name">{r.stage.replace(/_/g, " ")}</span>
            <span className={`stage-pill stage-pill-${r.status}`}>{STATUS_LABEL[r.status] || r.status}</span>
            {r.retried && <span className="retried-tag">retried</span>}
            {r.detail && <span className="stage-detail">{r.detail}</span>}
          </li>
        ))}
      </ol>
    </div>
  );
}
