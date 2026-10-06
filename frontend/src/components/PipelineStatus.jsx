const STATUS_LABEL = { success: "success", failed: "failed", skipped: "skipped" };

export default function PipelineStatus({ orchestrationLog, overallStatus, confidence }) {
  if (!orchestrationLog) return null;

  return (
    <div className="panel pipeline-status">
      <div className="pipeline-status-header">
        <h2>Pipeline run</h2>
        <span className={`badge badge-${overallStatus}`}>{overallStatus}</span>
        {confidence != null && (
          <span className="confidence">quality confidence: {(confidence * 100).toFixed(1)}%</span>
        )}
      </div>
      <ol className="stage-list">
        {orchestrationLog.records.map((r) => (
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
