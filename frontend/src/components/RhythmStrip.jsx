const RHYTHM_COLOR = { control: "#2f9e68", contested: "#8a8f98", chaotic: "#c6482b" };

export default function RhythmStrip({ explainability }) {
  if (!explainability) return null;
  const { windows, momentum_shifts: momentumShifts } = explainability;
  const totalWindows = windows.length || 1;

  return (
    <div className="panel rhythm-strip">
      <h2>Explain — control vs. chaos, game rhythm</h2>
      <div className="rhythm-bar">
        {windows.map((w, i) => (
          <div
            key={`${w.period}-${w.window_index}`}
            className="rhythm-segment"
            style={{
              width: `${100 / totalWindows}%`,
              background: RHYTHM_COLOR[w.rhythm_label] || "#8a8f98",
            }}
            title={`P${w.period} ${Math.round(w.start_second / 60)}'–${Math.round(w.end_second / 60)}' — ${w.rhythm_label}${
              w.dominant_team ? ` (${w.dominant_team} ${Math.round(w.control_share * 100)}%)` : ""
            }`}
          />
        ))}
      </div>
      <div className="rhythm-legend">
        {Object.entries(RHYTHM_COLOR).map(([label, color]) => (
          <span key={label} className="legend-item">
            <span className="legend-swatch" style={{ background: color }} />
            {label}
          </span>
        ))}
      </div>

      {momentumShifts.length > 0 && (
        <ul className="momentum-list">
          {momentumShifts.map((shift, i) => (
            <li key={i}>{shift.description}</li>
          ))}
        </ul>
      )}

      <ul className="moment-list">
        {explainability.explained_moments.map((m, i) => (
          <li key={i}>
            <strong>{m.minute}'</strong> {m.base_description} — <em>{m.context}</em>
          </li>
        ))}
      </ul>
    </div>
  );
}
