// Stat visuals for the match report. Every value comes from the narrative
// report's team_stats; proxies are labelled as proxies where they appear.

function fmt(v, kind) {
  if (v == null) return "n/a";
  if (kind === "pct") return `${Math.round(v * 100)}%`;
  if (kind === "pct100") return `${Math.round(v)}%`;
  if (kind === "m") return `${v.toFixed(1)}m`;
  return String(v);
}

export function StatTile({ label, note, children }) {
  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-muted/60 p-4">
      <div className="flex items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold">{label}</h4>
        {note && <span className="text-[11px] text-muted-foreground">{note}</span>}
      </div>
      {children}
    </div>
  );
}

export function PossessionDonut({ home, away, palette, homeStats, awayStats }) {
  const hp = homeStats?.possession_pct;
  const ap = awayStats?.possession_pct;
  if (hp == null || ap == null) {
    return <p className="text-sm text-muted-foreground">Not enough ball events to estimate.</p>;
  }
  const r = 15.9155; // circumference 100, so dash lengths read as percentages
  return (
    <div className="flex items-center gap-4">
      <svg viewBox="0 0 42 42" className="size-24 shrink-0 -rotate-90">
        <circle cx="21" cy="21" r={r} fill="none" stroke={palette[away].mark} strokeWidth="5" />
        <circle
          cx="21"
          cy="21"
          r={r}
          fill="none"
          stroke={palette[home].mark}
          strokeWidth="5"
          strokeDasharray={`${hp} ${100 - hp}`}
        />
      </svg>
      <div className="flex flex-col gap-1.5 font-mono text-sm tabular-nums">
        <span className="flex items-center gap-2">
          <span className="size-2.5 rounded-full" style={{ background: palette[home].mark }} />
          {fmt(hp, "pct100")} <span className="font-sans text-xs text-muted-foreground">{palette[home].code}</span>
        </span>
        <span className="flex items-center gap-2">
          <span className="size-2.5 rounded-full" style={{ background: palette[away].mark }} />
          {fmt(ap, "pct100")} <span className="font-sans text-xs text-muted-foreground">{palette[away].code}</span>
        </span>
      </div>
    </div>
  );
}

// Two horizontal bars, one per team, scaled against each other (or against
// `max` for values that are already a 0-1 share).
export function PairBars({ rows, palette, max }) {
  const top = max ?? Math.max(1, ...rows.map((r) => r.value ?? 0));
  return (
    <div className="flex flex-col gap-2.5">
      {rows.map((r) => (
        <div key={r.team} className="flex items-center gap-3">
          <span className="w-12 shrink-0 font-mono text-sm font-semibold tabular-nums">{r.display}</span>
          <div className="h-2 flex-1 rounded-full bg-white/[0.06]">
            <div
              className="h-full rounded-full"
              style={{ width: `${((r.value ?? 0) / top) * 100}%`, background: palette[r.team].mark }}
            />
          </div>
          <span className="w-9 shrink-0 text-right text-xs text-muted-foreground">{palette[r.team].code}</span>
        </div>
      ))}
    </div>
  );
}

const ANALYST_ROWS = [
  { group: "Attack", label: "Goals", key: "goals" },
  { group: "Attack", label: "Shots", key: "shots" },
  { group: "Attack", label: "Shots on target", key: "shots_on_target" },
  { group: "Attack", label: "Shot accuracy", key: "shot_accuracy", kind: "pct", share: true },
  { group: "Attack", label: "Corners", key: "corners" },
  { group: "Passing", label: "Passes", key: "passes" },
  { group: "Passing", label: "Pass accuracy", key: "pass_accuracy", kind: "pct", share: true },
  { group: "Passing", label: "Avg pass length", key: "avg_pass_distance_m", kind: "m" },
  { group: "Passing", label: "Short / medium / long", key: "_mix" },
  { group: "Defending", label: "Tackles", key: "tackles" },
  { group: "Defending", label: "Interceptions", key: "interceptions" },
  { group: "Defending", label: "Duels won (tackles + interceptions)", key: "duels_won" },
  { group: "Defending", label: "Clearances", key: "clearances" },
  { group: "Defending", label: "Saves", key: "saves" },
  { group: "Discipline", label: "Fouls", key: "fouls" },
  { group: "Discipline", label: "Cards", key: "cards" },
  { group: "Discipline", label: "Offsides", key: "offsides" },
  { group: "Data quality", label: "Avg event confidence", key: "avg_confidence", kind: "pct", share: true },
  { group: "Data quality", label: "Low-confidence events", key: "low_confidence_events" },
];

// Mirrored comparison rows: home bar grows left, away bar grows right.
export function StatCompare({ home, away, homeStats, awayStats, palette }) {
  const groups = [...new Set(ANALYST_ROWS.map((r) => r.group))];
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 text-xs font-bold tracking-wide uppercase">
        <span style={{ color: palette[home].mark }}>{home}</span>
        <span />
        <span className="text-right" style={{ color: palette[away].mark }}>{away}</span>
      </div>
      {groups.map((g) => (
        <section key={g} className="flex flex-col gap-2">
          <h4 className="text-[11px] font-bold tracking-widest text-muted-foreground uppercase">{g}</h4>
          {ANALYST_ROWS.filter((r) => r.group === g).map((r) => {
            if (r.key === "_mix") {
              const mix = (s) => (s ? `${s.short_passes} / ${s.medium_passes} / ${s.long_passes}` : "n/a");
              return (
                <div key={r.label} className="grid grid-cols-[1fr_auto_1fr] items-center gap-3 text-sm">
                  <span className="font-mono tabular-nums">{mix(homeStats)}</span>
                  <span className="text-center text-xs text-muted-foreground">{r.label}</span>
                  <span className="text-right font-mono tabular-nums">{mix(awayStats)}</span>
                </div>
              );
            }
            const hv = homeStats?.[r.key] ?? null;
            const av = awayStats?.[r.key] ?? null;
            const top = r.share ? 1 : Math.max(1e-9, hv ?? 0, av ?? 0);
            const w = (v) => `${((v ?? 0) / top) * 100}%`;
            return (
              <div key={r.label} className="flex flex-col gap-1">
                <div className="grid grid-cols-[1fr_auto_1fr] items-baseline gap-3 text-sm">
                  <span className="font-mono font-semibold tabular-nums">{fmt(hv, r.kind)}</span>
                  <span className="text-center text-xs text-muted-foreground">{r.label}</span>
                  <span className="text-right font-mono font-semibold tabular-nums">{fmt(av, r.kind)}</span>
                </div>
                <div className="grid grid-cols-2 gap-1">
                  <div className="flex h-1.5 justify-end rounded-l-full bg-white/[0.05]">
                    <div className="h-full rounded-l-full" style={{ width: w(hv), background: palette[home].mark }} />
                  </div>
                  <div className="h-1.5 rounded-r-full bg-white/[0.05]">
                    <div className="h-full rounded-r-full" style={{ width: w(av), background: palette[away].mark }} />
                  </div>
                </div>
              </div>
            );
          })}
        </section>
      ))}
    </div>
  );
}
