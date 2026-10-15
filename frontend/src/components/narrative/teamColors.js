// Club colours and the standard three-letter broadcast codes. Badges are
// drawn as a plain generic shield with the code on it: club crests are
// trademarks, so they are deliberately not reproduced.
//   fill  - shield background
//   text  - code colour on the shield
//   mark  - colour for dots and bars, tuned to stay readable on the dark UI
const CLUBS = {
  Liverpool: { code: "LIV", fill: "#c8102e", text: "#ffffff", mark: "#ff5a6e" },
  "Manchester City": { code: "MCI", fill: "#6cabdd", text: "#0b1f33", mark: "#7cc4f5" },
  "Manchester United": { code: "MUN", fill: "#da291c", text: "#ffffff", mark: "#ff6a5c" },
  Tottenham: { code: "TOT", fill: "#132257", text: "#ffffff", mark: "#e9ecf5" },
  "Tottenham Hotspur": { code: "TOT", fill: "#132257", text: "#ffffff", mark: "#e9ecf5" },
  Arsenal: { code: "ARS", fill: "#ef0107", text: "#ffffff", mark: "#ff5c5c" },
  Chelsea: { code: "CHE", fill: "#034694", text: "#ffffff", mark: "#5aa2ff" },
};

const FALLBACK = {
  home: { fill: "#6d3fd1", text: "#ffffff", mark: "#b98cff" },
  away: { fill: "#c97a2c", text: "#1a1203", mark: "#f2a65a" },
};

function codeFor(team) {
  const words = team
    .replace(/\b(FC|AFC|CF)\b/gi, "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 1) return words[0].slice(0, 3).toUpperCase();
  return words.map((w) => w[0]).join("").slice(0, 3).toUpperCase();
}

function hue(hex) {
  const n = parseInt(hex.slice(1), 16);
  const r = ((n >> 16) & 255) / 255;
  const g = ((n >> 8) & 255) / 255;
  const b = (n & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  if (d < 0.15) return null; // near-grey: treat as neutral, never clashes on hue
  let h;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

function clash(a, b) {
  const ha = hue(a);
  const hb = hue(b);
  if (ha == null || hb == null) return false;
  const diff = Math.abs(ha - hb);
  return Math.min(diff, 360 - diff) < 30;
}

export function teamPalette(home, away) {
  const homeClub = CLUBS[home];
  const awayClub = CLUBS[away];
  const h = { code: homeClub?.code || codeFor(home), ...(homeClub || FALLBACK.home) };
  let a = { code: awayClub?.code || codeFor(away), ...(awayClub || FALLBACK.away) };
  // Two red clubs (say Liverpool v Arsenal) would be unreadable side by
  // side, so the away side switches to the neutral away colour.
  if (clash(h.mark, a.mark)) a = { ...a, ...FALLBACK.away };
  return { [home]: h, [away]: a };
}
