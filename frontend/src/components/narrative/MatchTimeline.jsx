import { Goal } from "lucide-react";

function axisEnd(maxMinute) {
  if (maxMinute >= 85) return Math.max(90, Math.ceil(maxMinute / 5) * 5);
  return Math.max(10, Math.ceil((maxMinute + 1) / 5) * 5);
}

function MomentIcon({ type, color }) {
  if (type === "goal") {
    return (
      <span className="flex size-6 items-center justify-center rounded-full bg-card" style={{ boxShadow: `0 0 0 2px ${color}` }}>
        <Goal className="size-3.5" style={{ color }} />
      </span>
    );
  }
  if (type === "card") {
    return <span className="block h-4 w-3 rounded-[2px] bg-warning ring-2 ring-card" />;
  }
  return <span className="block size-3 rounded-full ring-2 ring-card" style={{ background: color }} />;
}

// Key moments on a horizontal axis. The axis only runs as far as the data
// does: if the feed stops at 25', the timeline says so instead of drawing
// an empty 90 minutes.
export default function MatchTimeline({ moments, palette, maxMinute, highlightMinute }) {
  const end = axisEnd(maxMinute);
  const step = end > 45 ? 15 : 5;
  const ticks = [];
  for (let t = 0; t <= end; t += step) ticks.push(t);
  const pct = (m) => `${Math.min(100, (m / end) * 100)}%`;

  // Moments on the same minute are nudged into separate lanes so their
  // icons do not sit on top of each other.
  const laneOf = new Map();
  const taken = [];
  moments.forEach((m, i) => {
    let lane = 0;
    while (taken.some((t) => t.lane === lane && Math.abs(t.minute - m.minute) < end / 40)) lane += 1;
    taken.push({ lane, minute: m.minute });
    laneOf.set(i, lane);
  });
  const lanes = Math.max(1, ...[...laneOf.values()].map((l) => l + 1));

  return (
    <div className="flex flex-col gap-2">
      <div className="relative" style={{ height: `${lanes * 30 + 6}px` }}>
        {moments.map((m, i) => {
          const color = palette[m.team]?.mark || "#9d93ab";
          return (
            <div
              key={i}
              className="absolute -translate-x-1/2"
              style={{ left: pct(m.minute), bottom: `${laneOf.get(i) * 30}px` }}
              title={`${m.minute}' ${m.description}`}
            >
              <MomentIcon type={m.event_type} color={color} />
            </div>
          );
        })}
      </div>

      <div className="relative h-2 rounded-full bg-white/[0.06]">
        <div className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-primary/60 to-primary/20" style={{ width: pct(maxMinute) }} />
        {moments.map((m, i) => (
          <span
            key={i}
            className="absolute top-1/2 h-3.5 w-[3px] -translate-x-1/2 -translate-y-1/2 rounded-full"
            style={{
              left: pct(m.minute),
              background: palette[m.team]?.mark || "#9d93ab",
              boxShadow: m.minute === highlightMinute ? "0 0 0 3px rgba(157,92,255,0.45)" : undefined,
            }}
          />
        ))}
      </div>

      <div className="relative h-4 font-mono text-[11px] text-muted-foreground tabular-nums">
        {ticks.map((t) => (
          <span key={t} className="absolute -translate-x-1/2" style={{ left: pct(t) }}>
            {t}'
          </span>
        ))}
      </div>
    </div>
  );
}
