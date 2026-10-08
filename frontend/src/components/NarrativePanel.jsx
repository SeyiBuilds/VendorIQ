export default function NarrativePanel({ narrative }) {
  if (!narrative) return null;
  const teams = Object.values(narrative.team_stats);
  const moments = narrative.key_moments || [];

  return (
    <div className="panel narrative-panel">
      <h2>Narrative {narrative.generation_method === "ai_enhanced" ? "(AI-enhanced)" : "(template)"}</h2>

      {moments.length > 0 && (
        <section className="narrative-section">
          <h3>Key moments</h3>
          <ul className="moment-list">
            {moments.map((m, i) => (
              <li key={i}>
                <span className="moment-minute">{m.minute}'</span>
                <span className="moment-desc">{m.description}</span>
                {m.confidence < 0.6 && <span className="moment-low-conf">low confidence</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="narrative-section">
        <h3>Team stats</h3>
        <table className="stats-table">
          <thead>
            <tr>
              <th>Team</th><th>Poss. (proxy)</th><th>Goals</th><th>Shots (on target)</th><th>Passes (acc.)</th>
              <th>Avg pass (m)</th><th>Duels won</th><th>Fouls</th><th>Cards</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((t) => (
              <tr key={t.team}>
                <td>{t.team}</td>
                <td>{t.possession_pct != null ? `${t.possession_pct.toFixed(0)}%` : "—"}</td>
                <td>{t.goals}</td>
                <td>{t.shots} ({t.shots_on_target})</td>
                <td>{t.passes_completed}/{t.passes}</td>
                <td>{t.avg_pass_distance_m?.toFixed(1) ?? "—"}</td>
                <td>{t.duels_won}</td>
                <td>{t.fouls}</td>
                <td>{t.cards}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="hint">
          Possession is an event-share proxy (pass/dribble/shot/set-piece share), not tracked ball
          time — no vendor here supplies that. Duels won = tackles + interceptions.
        </p>
      </section>

      <details className="narrative-section narrative-raw">
        <summary>Full generated report text</summary>
        <p className="narrative-text">{narrative.narrative_text}</p>
      </details>

      {narrative.data_quality_notes.length > 0 && (
        <details className="narrative-section data-quality-notes">
          <summary>Data quality notes ({narrative.data_quality_notes.length})</summary>
          <ul>
            {narrative.data_quality_notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}
