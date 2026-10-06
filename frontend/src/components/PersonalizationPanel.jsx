import { useState } from "react";

const MODES = ["casual_fan", "analyst", "player_focus"];
const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "es", label: "Español" },
  { code: "fr", label: "Français" },
];

export default function PersonalizationPanel({ document, onPersonalize, experience, loading }) {
  const [mode, setMode] = useState("casual_fan");
  const [favoriteClub, setFavoriteClub] = useState("");
  const [favoritePlayerJersey, setFavoritePlayerJersey] = useState("");
  const [language, setLanguage] = useState("en");

  if (!document) return null;

  const teams = [document.match.home_team, document.match.away_team];
  const jerseyOptions = document.players
    .filter((p) => !favoriteClub || p.team === favoriteClub)
    .filter((p) => p.jersey_number != null)
    .sort((a, b) => (a.jersey_number ?? 0) - (b.jersey_number ?? 0));

  const submit = () => {
    onPersonalize({
      mode,
      favorite_club: favoriteClub || null,
      favorite_player_jersey: favoritePlayerJersey ? Number(favoritePlayerJersey) : null,
      language,
    });
  };

  return (
    <div className="panel personalization-panel">
      <h2>Personalize — same intelligence, two different experiences</h2>
      <div className="field-row">
        <label>
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            {MODES.map((m) => <option key={m} value={m}>{m.replace("_", " ")}</option>)}
          </select>
        </label>
        <label>
          Favorite club
          <select value={favoriteClub} onChange={(e) => setFavoriteClub(e.target.value)}>
            <option value="">(none)</option>
            {teams.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label>
          Favorite player (jersey #)
          <select value={favoritePlayerJersey} onChange={(e) => setFavoritePlayerJersey(e.target.value)}>
            <option value="">(none)</option>
            {jerseyOptions.map((p) => (
              <option key={`${p.team}-${p.jersey_number}`} value={p.jersey_number}>
                #{p.jersey_number} {p.name ? `— ${p.name}` : ""} ({p.team})
              </option>
            ))}
          </select>
        </label>
        <label>
          Language
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
          </select>
        </label>
        <button className="primary" onClick={submit} disabled={loading}>
          {loading ? "Personalizing…" : "Personalize"}
        </button>
      </div>

      {experience && (
        <div className="experience-result">
          <h3>{experience.headline}</h3>
          <p>{experience.narrative_text}</p>
          <ul className="moment-list">
            {experience.moments.map((m, i) => (
              <li key={i} className={m.is_favorite_club ? "favorite-moment" : ""}>
                <strong>{m.minute}'</strong> {m.headline} — <em>{m.context}</em>
              </li>
            ))}
          </ul>
          {experience.log.length > 0 && (
            <details className="data-quality-notes">
              <summary>Personalization notes ({experience.log.length})</summary>
              <ul>{experience.log.map((n, i) => <li key={i}>{n}</li>)}</ul>
            </details>
          )}
        </div>
      )}
    </div>
  );
}
