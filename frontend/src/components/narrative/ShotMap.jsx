import { describeEvent, eventMinute } from "./matchInsights";

// Pitch drawn in metres (105 x 68). Events arrive on VendorIQ's normalized
// 0-100 grid (the Coordinate Translator's guarantee), so the same scale
// works for Opta, StatsBomb and Wyscout input alike.
const W = 105;
const H = 68;
const px = (x) => (x / 100) * W;
const py = (y) => (y / 100) * H;

export function PitchLines() {
  const line = { fill: "none", stroke: "rgba(255,255,255,0.32)", strokeWidth: 0.35 };
  const boxW = 40.32;
  const sixW = 18.32;
  return (
    <g {...line}>
      <rect x="0" y="0" width={W} height={H} />
      <line x1={W / 2} y1="0" x2={W / 2} y2={H} />
      <circle cx={W / 2} cy={H / 2} r="9.15" />
      <rect x="0" y={(H - boxW) / 2} width="16.5" height={boxW} />
      <rect x={W - 16.5} y={(H - boxW) / 2} width="16.5" height={boxW} />
      <rect x="0" y={(H - sixW) / 2} width="5.5" height={sixW} />
      <rect x={W - 5.5} y={(H - sixW) / 2} width="5.5" height={sixW} />
      <path d={`M16.5 ${H / 2 - 7.3} A9.15 9.15 0 0 1 16.5 ${H / 2 + 7.3}`} />
      <path d={`M${W - 16.5} ${H / 2 - 7.3} A9.15 9.15 0 0 0 ${W - 16.5} ${H / 2 + 7.3}`} />
      <circle cx="11" cy={H / 2} r="0.4" fill="rgba(255,255,255,0.4)" />
      <circle cx={W - 11} cy={H / 2} r="0.4" fill="rgba(255,255,255,0.4)" />
      <circle cx={W / 2} cy={H / 2} r="0.4" fill="rgba(255,255,255,0.4)" />
    </g>
  );
}

export default function ShotMap({ shots, palette, evidence, compact = false }) {
  const stripes = Array.from({ length: 10 }, (_, i) => i);
  const path = evidence?.filter((e) => e.x != null && e.y != null) || [];

  return (
    <figure className="flex flex-col gap-3">
      <div className="overflow-hidden rounded-lg border border-white/10">
        <svg viewBox={`-2 -2 ${W + 4} ${H + 4}`} className="block h-auto w-full" role="img" aria-label="Shot map">
          <rect x="-2" y="-2" width={W + 4} height={H + 4} fill="#0d2419" />
          {stripes.map((i) => (
            <rect
              key={i}
              x={(i * W) / 10}
              y="0"
              width={W / 10}
              height={H}
              fill={i % 2 ? "#10291d" : "#0e261a"}
            />
          ))}
          <PitchLines />

          {/* Numbered markers only, no connecting line: these are separate
              events from both teams, not a tracked ball path, and a line
              between them would imply movement the data does not record. */}
          {path.slice(0, -1).map((e, i) => (
            <g key={e.event_id}>
              <circle cx={px(e.x)} cy={py(e.y)} r="1.5" fill="#0e261a" stroke="rgba(255,255,255,0.8)" strokeWidth="0.35" />
              <text x={px(e.x)} y={py(e.y) + 0.65} textAnchor="middle" fontSize="1.9" fontWeight="700" fill="#fff" fontFamily="JetBrains Mono, monospace">
                {i + 1}
              </text>
            </g>
          ))}

          {shots.map((s) => {
            const c = palette[s.team]?.mark || "#fff";
            const cx = px(s.x);
            const cy = py(s.y);
            if (s.event_type === "goal") {
              return (
                <g key={s.event_id} className="animate-pop-in">
                  <circle cx={cx} cy={cy} r="3.1" fill="none" stroke={c} strokeOpacity="0.45" strokeWidth="0.5" />
                  <circle cx={cx} cy={cy} r="1.9" fill={c} stroke="#fff" strokeWidth="0.5">
                    <title>{`Goal, ${s.team}, ${eventMinute(s)}'`}</title>
                  </circle>
                </g>
              );
            }
            return (
              <circle
                key={s.event_id}
                className="animate-pop-in"
                cx={cx}
                cy={cy}
                r="1.05"
                fill={s.outcome ? c : "none"}
                fillOpacity={s.outcome ? 0.85 : 1}
                stroke={c}
                strokeWidth="0.4"
              >
                <title>{`${describeEvent(s)}, ${s.team}, ${eventMinute(s)}'`}</title>
              </circle>
            );
          })}
        </svg>
      </div>

      <figcaption className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
        {Object.entries(palette).map(([team, c]) => (
          <span key={team} className="inline-flex items-center gap-1.5">
            <span className="size-2.5 rounded-full" style={{ background: c.mark }} />
            {team}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <svg width="10" height="10" viewBox="0 0 10 10"><circle cx="5" cy="5" r="3.5" fill="#9d93ab" /></svg>
          on target
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="10" height="10" viewBox="0 0 10 10"><circle cx="5" cy="5" r="3.5" fill="none" stroke="#9d93ab" strokeWidth="1.2" /></svg>
          off target
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="12" height="12" viewBox="0 0 12 12"><circle cx="6" cy="6" r="3.2" fill="#9d93ab" stroke="#fff" strokeWidth="1" /></svg>
          goal
        </span>
        {path.length > 1 && <span>numbered: the events just before the turning point</span>}
      </figcaption>
      {!compact && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          {shots.length} shots plotted where the feed recorded them, on VendorIQ's normalized 0 to 100 grid. The
          same pitch works whichever vendor the data came from.
        </p>
      )}
      {shots.length === 0 && (
        <p className="text-sm text-muted-foreground">No shot events with positions in this run.</p>
      )}
    </figure>
  );
}
