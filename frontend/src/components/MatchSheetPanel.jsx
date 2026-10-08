import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Separator } from "./ui/separator";

function playerName(team, playerId) {
  const all = [...team.starters, ...team.substitutes];
  return all.find((p) => p.player_id === playerId)?.name ?? playerId;
}

function TeamColumn({ team }) {
  return (
    <div>
      <h3 className="mb-2 flex items-baseline gap-2 text-sm font-bold">
        {team.team}
        <span className="font-mono text-xs font-normal text-muted-foreground">
          {team.formation ?? "formation unknown"}
        </span>
      </h3>
      <ol className="flex flex-col gap-1 text-sm">
        {team.starters.map((p) => (
          <li key={p.player_id} className="flex items-baseline gap-2">
            <span className="min-w-[20px] font-mono font-bold text-primary tabular-nums">
              {p.jersey_number ?? "–"}
            </span>
            <span className="flex-1">
              {p.name ?? "Unknown player"}
              {p.is_captain && " (C)"}
            </span>
            <span className="text-xs text-muted-foreground">{p.position ?? ""}</span>
          </li>
        ))}
      </ol>
      {team.substitutes.length > 0 && (
        <details className="mt-2 text-xs text-muted-foreground">
          <summary className="cursor-pointer font-semibold">Bench ({team.substitutes.length})</summary>
          <ul className="mt-1.5 flex flex-col gap-1">
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
    <Card className="border-warning/30">
      <CardHeader>
        <CardTitle>Match sheet</CardTitle>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Lineups, formations, substitutions, officials and venue — a separate pillar from the event
          timeline below, mapped from its own vendor-shaped feed.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="flex flex-wrap gap-4 font-mono text-sm text-muted-foreground">
          {matchSheet.weather && <span>{matchSheet.weather}</span>}
          {matchSheet.attendance != null && <span>{matchSheet.attendance.toLocaleString()} attendance</span>}
          {matchSheet.kickoff && (
            <span>
              kickoff {new Date(matchSheet.kickoff).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </span>
          )}
        </div>

        <div className="grid gap-5 sm:grid-cols-2">
          <TeamColumn team={matchSheet.home} />
          <TeamColumn team={matchSheet.away} />
        </div>

        {allSubs.length > 0 && (
          <>
            <Separator />
            <section>
              <h3 className="mb-3 text-xs font-bold tracking-wide text-muted-foreground uppercase">
                Substitutions
              </h3>
              <ul className="flex flex-col gap-2 text-sm">
                {allSubs.map((s, i) => (
                  <li
                    key={i}
                    className="flex items-baseline gap-3 rounded-md border border-border/70 bg-muted px-3 py-2"
                  >
                    <span className="min-w-[32px] font-mono font-bold text-primary tabular-nums">{s.minute}'</span>
                    <span className="flex-1">
                      {s.team}: {nameFor(s.player_out_id)} → {nameFor(s.player_in_id)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}

        {officials && (
          <>
            <Separator />
            <section>
              <h3 className="mb-2 text-xs font-bold tracking-wide text-muted-foreground uppercase">Officials</h3>
              <p className="text-sm leading-relaxed text-muted-foreground">
                Referee: {officials.referee ?? "—"}
                {officials.assistant_referees?.length > 0 &&
                  ` · Assistants: ${officials.assistant_referees.join(", ")}`}
                {officials.fourth_official && ` · Fourth: ${officials.fourth_official}`}
                {officials.var_official && ` · VAR: ${officials.var_official}`}
              </p>
            </section>
          </>
        )}
      </CardContent>
    </Card>
  );
}
