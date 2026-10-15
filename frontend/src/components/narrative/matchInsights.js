// Pure helpers that turn a finished run (CDF document + narrative report)
// into what the match report view draws. Nothing here invents a number:
// every value is read from, or counted out of, the pipeline's own output.

// Mirrors narrative_generator.py's minute calculation exactly, so a minute
// shown on the pitch or in the evidence list always matches the minute on
// the matching key moment.
export function eventMinute(e) {
  return Math.floor(e.timestamp / 60) + (e.period === 2 ? 45 : 0);
}

export function clockLabel(e) {
  const total = e.timestamp + (e.period === 2 ? 45 * 60 : 0);
  const m = Math.floor(total / 60);
  const s = Math.floor(total % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function byMatchTime(a, b) {
  return a.period - b.period || a.timestamp - b.timestamp;
}

// Player records by id. When the event feed carried no name (Opta's), the
// name is filled from the match sheet in the same document by team and
// shirt number -- the same lookup the narrative generator makes.
export function playerLookup(document) {
  const sheetNames = new Map();
  const sheet = document?.match_sheet;
  for (const side of sheet ? [sheet.home, sheet.away] : []) {
    for (const entry of [...(side?.starters || []), ...(side?.substitutes || [])]) {
      if (entry.name && entry.jersey_number != null) sheetNames.set(`${side.team}|${entry.jersey_number}`, entry.name);
    }
  }
  const map = new Map();
  for (const p of document?.players || []) {
    const name = p.name || sheetNames.get(`${p.team}|${p.jersey_number}`) || null;
    map.set(p.player_id, name === p.name ? p : { ...p, name });
  }
  return map;
}

export function shortName(name) {
  if (!name) return null;
  const parts = name.trim().split(/\s+/);
  return parts.length > 1 ? parts[parts.length - 1] : name;
}

export const CARD_LABELS = {
  yellow: "Yellow card",
  second_yellow: "Second yellow, sent off",
  red: "Red card, sent off",
};

export const SHOT_LABELS = {
  goal: "Goal",
  saved: "Shot saved",
  off_target: "Shot off target",
  blocked: "Shot blocked",
  post: "Hit the post",
};

export function cardColor(cardType) {
  return cardType === "yellow" ? "#f5c518" : cardType ? "#ef4444" : "#9d93ab";
}

// "Jersey #17" when the name was never resolved, which is the honest
// fallback the narrative generator uses too.
export function playerLabel(playerId, players) {
  if (!playerId) return null;
  const p = players.get(playerId);
  if (p?.name) return p.name;
  if (p?.jersey_number != null) return `#${p.jersey_number}`;
  const tail = playerId.split("::").pop();
  return /^\d+$/.test(tail) ? `#${tail}` : null;
}

export function resolveTeams(document, narrative) {
  const statTeams = Object.keys(narrative?.team_stats || {});
  const home = document?.match?.home_team || statTeams[0] || "Home";
  const away = document?.match?.away_team || statTeams.find((t) => t !== home) || "Away";
  return { home, away };
}

export function teamStat(narrative, team) {
  return narrative?.team_stats?.[team] || null;
}

export function dataCoverage(document, narrative) {
  const events = document?.events || [];
  const minutes = events.map(eventMinute);
  for (const k of narrative?.key_moments || []) minutes.push(k.minute);
  const maxMinute = minutes.length ? Math.max(...minutes) : 0;
  return { eventCount: events.length, maxMinute };
}

export function goalEvents(document) {
  return (document?.events || [])
    .filter((e) => e.event_type === "goal" && e.team)
    .sort(byMatchTime);
}

export function shotEvents(document) {
  return (document?.events || []).filter(
    (e) => (e.event_type === "shot" || e.event_type === "goal") && e.team,
  );
}

// Rule, stated in the UI as a rule: the turning point is the LAST goal that
// changed who was ahead (an opener, a go-ahead goal, or an equaliser).
// Every goal after it only extended a lead it had already settled, so it is
// the goal that decided the result the data ends on. No model, no
// probabilities, just the score state.
export function findTurningPoint(goals, home, away) {
  const score = { [home]: 0, [away]: 0 };
  const leader = () =>
    score[home] === score[away] ? null : score[home] > score[away] ? home : away;

  let turning = null;
  for (const g of goals) {
    if (!(g.team in score)) continue;
    const before = leader();
    const wasLevelAtZero = score[home] === 0 && score[away] === 0;
    score[g.team] += 1;
    const after = leader();
    if (before !== after) {
      const kind = after === null ? "equaliser" : wasLevelAtZero ? "opener" : "go-ahead goal";
      turning = { goal: g, kind, scoreAfter: { ...score } };
    }
  }
  if (!turning) return null;
  return { ...turning, finalScore: { ...score } };
}

// The events that led into a moment: up to `limit` events in the 150
// seconds before it (same period), then the moment itself. The window is
// wide enough to reach across a stoppage (a penalty takes over a minute to
// set up after the foul). These are real CDF rows, shown as the evidence
// behind the claim.
export function evidenceFor(target, document, limit = 4) {
  const events = (document?.events || []).filter(
    (e) =>
      e.period === target.period &&
      e.timestamp < target.timestamp &&
      target.timestamp - e.timestamp <= 150,
  );
  events.sort(byMatchTime);
  return [...events.slice(-limit), target];
}

export function splitSentences(text) {
  if (!text) return [];
  return text.split(/(?<=\.)\s+(?=[A-Z])/);
}

export const EVENT_LABELS = {
  pass: "Pass",
  shot: "Shot",
  tackle: "Tackle",
  foul: "Foul",
  card: "Card",
  substitution: "Substitution",
  corner: "Corner",
  throw_in: "Throw-in",
  goal_kick: "Goal kick",
  offside: "Offside",
  goal: "Goal",
  save: "Save",
  interception: "Interception",
  clearance: "Clearance",
  dribble: "Dribble",
  other: "Other event",
};

export function describeEvent(e) {
  const base = EVENT_LABELS[e.event_type] || e.event_type;
  if (e.event_type === "shot") {
    if (e.shot_outcome) return SHOT_LABELS[e.shot_outcome];
    return e.outcome ? "Shot on target" : "Shot off target";
  }
  if (e.card_type && (e.event_type === "card" || e.event_type === "foul")) {
    return e.event_type === "foul" ? `Foul, ${CARD_LABELS[e.card_type].toLowerCase()}` : CARD_LABELS[e.card_type];
  }
  if (e.event_type === "card") return "Card (colour not in feed)";
  if (e.event_type === "pass" && e.outcome === false) return "Pass, not completed";
  if (e.event_type === "dribble") return e.outcome === false ? "Dribble, lost the ball" : "Dribble, beat his man";
  if (e.event_type === "tackle") return "Tackle won";
  return base;
}
