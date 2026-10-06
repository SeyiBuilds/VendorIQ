function playerName(team, playerId) {
  const all = [...team.starters, ...team.substitutes];
  return all.find((p) => p.player_id === playerId)?.name ?? playerId;
}

function TeamColumn({ team }) {
  return (
    <div className="sheet-team">
      <h3>
        {team.team} <span className="formation-badge">{team.formation ?? "formation unknown"}</span>
      </h3>
      <ol className="lineup-list">
        {team.starters.map((p) => (
          <li key={p.player_id}>
            <span className="lineup-jersey">{p.jersey_number ?? "–"}</span>
            <span className="lineup-name">
              {p.name ?? "Unknown player"}
              {p.is_captain && " (C)"}
            </span>
            <span className="lineup-pos">{p.position ?? ""}</span>
          </li>
        ))}
      </ol>
      {team.substitutes.length > 0 && (
        <details className="sheet-bench">
          <summary>Bench ({team.substitutes.length})</summary>
          <ul>
            {team.substitutes.map((p) => (
              <li key={p.player_id}>
                {p.jersey_number ?? "–"} {p.name ?? "Unknown player"} ({p.position ?? "—"})
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// A separate data pillar from the event timeline (NarrativePanel) -- only
// renders when the run actually had a match-sheet payload to map (see
// orchestrator.py's match_sheet_mapper stage, "skipped" when none was
// supplied, same honest-absence rule as everything else in this pipeline).
export default function MatchSheetPanel({ matchSheet }) {
  if (!matchSheet) return null;

  const allSubs = [...matchSheet.home.substitutions, ...matchSheet.away.substitutions].sort(
    (a, b) => a.minute - b.minute,
  );
  const nameFor = (id) => {
    const home = playerName(matchSheet.home, id);
    return home !== id ? home : playerName(matchSheet.away, id);
  };

  const officials = matchSheet.officials;

  return (
    <div className="panel match-sheet-panel">
      <h2>Match sheet</h2>
      <p className="hint">
        Lineups, formations, substitutions, officials and venue — a separate pillar from the event
        timeline below, mapped from its own vendor-shaped feed.
      </p>

      <div className="sheet-meta">
        {matchSheet.weather && <span>{matchSheet.weather}</span>}
        {matchSheet.attendance != null && <span>{matchSheet.attendance.toLocaleString()} attendance</span>}
        {matchSheet.kickoff && <span>kickoff {new Date(matchSheet.kickoff).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>}
      </div>

      <div className="sheet-teams">
        <TeamColumn team={matchSheet.home} />
        <TeamColumn team={matchSheet.away} />
      </div>

      {allSubs.length > 0 && (
        <section className="narrative-section">
          <h3>Substitutions</h3>
          <ul className="moment-list">
            {allSubs.map((s, i) => (
              <li key={i}>
                <span className="moment-minute">{s.minute}'</span>
                <span className="moment-desc">
                  {s.team}: {nameFor(s.player_out_id)} → {nameFor(s.player_in_id)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {officials && (
        <section className="narrative-section">
          <h3>Officials</h3>
          <p className="hint">
            Referee: {officials.referee ?? "—"}
            {officials.assistant_referees?.length > 0 && ` · Assistants: ${officials.assistant_referees.join(", ")}`}
            {officials.fourth_official && ` · Fourth: ${officials.fourth_official}`}
            {officials.var_official && ` · VAR: ${officials.var_official}`}
          </p>
        </section>
      )}
    </div>
  );
}
