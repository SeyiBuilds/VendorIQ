const VENDORS = ["opta", "statsbomb", "wyscout"];

export default function MatchPicker({ matches, selectedMatchId, selectedVendor, onChange, onRun, running }) {
  return (
    <div className="panel match-picker">
      <h2>Match &amp; feed</h2>
      <div className="field-row">
        <label>
          Match
          <select
            value={selectedMatchId}
            onChange={(e) => onChange({ matchId: e.target.value, vendor: selectedVendor })}
          >
            {matches.map((m) => (
              <option key={m.match_id} value={m.match_id}>
                {m.home_team} vs {m.away_team} ({m.quality_tier})
              </option>
            ))}
          </select>
        </label>
        <label>
          Vendor feed
          <select
            value={selectedVendor}
            onChange={(e) => onChange({ matchId: selectedMatchId, vendor: e.target.value })}
          >
            {VENDORS.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </label>
        <button className="primary" onClick={onRun} disabled={running || !selectedMatchId}>
          {running ? "Running pipeline…" : "Run pipeline"}
        </button>
      </div>
      <p className="hint">
        Single-vendor ingestion is the realistic default — pick which vendor's feed to run through
        the 7-agent pipeline (detect format → map schema → normalize coordinates → resolve
        identities → validate → narrate → export on demand).
      </p>
    </div>
  );
}
