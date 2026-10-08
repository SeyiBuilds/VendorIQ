import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Badge } from "./ui/badge";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "./ui/table";
import { Separator } from "./ui/separator";

export default function NarrativePanel({ narrative }) {
  if (!narrative) return null;
  const teams = Object.values(narrative.team_stats);
  const moments = narrative.key_moments || [];

  return (
    <Card className="border-warning/30">
      <CardHeader className="flex-row items-center gap-2 space-y-0">
        <CardTitle>Narrative</CardTitle>
        <Badge variant={narrative.generation_method === "ai_enhanced" ? "default" : "muted"}>
          {narrative.generation_method === "ai_enhanced" ? "AI-enhanced" : "template"}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {moments.length > 0 && (
          <section>
            <h3 className="mb-3 text-xs font-bold tracking-wide text-muted-foreground uppercase">
              Key moments
            </h3>
            <ul className="flex flex-col gap-2 text-sm">
              {moments.map((m, i) => (
                <li
                  key={i}
                  className="flex items-baseline gap-3 rounded-md border border-border/70 bg-muted px-3 py-2"
                >
                  <span className="min-w-[32px] font-mono font-bold text-primary tabular-nums">{m.minute}'</span>
                  <span className="flex-1 leading-relaxed">{m.description}</span>
                  {m.confidence < 0.6 && (
                    <Badge variant="warning" className="shrink-0">low confidence</Badge>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        <section>
          <h3 className="mb-3 text-xs font-bold tracking-wide text-muted-foreground uppercase">Team stats</h3>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Team</TableHead>
                <TableHead>Poss. (proxy)</TableHead>
                <TableHead>Goals</TableHead>
                <TableHead>Shots (on target)</TableHead>
                <TableHead>Passes (acc.)</TableHead>
                <TableHead>Avg pass (m)</TableHead>
                <TableHead>Duels won</TableHead>
                <TableHead>Fouls</TableHead>
                <TableHead>Cards</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {teams.map((t) => (
                <TableRow key={t.team}>
                  <TableCell className="font-medium">{t.team}</TableCell>
                  <TableCell className="font-mono tabular-nums">
                    {t.possession_pct != null ? `${t.possession_pct.toFixed(0)}%` : "—"}
                  </TableCell>
                  <TableCell className="font-mono tabular-nums">{t.goals}</TableCell>
                  <TableCell className="font-mono tabular-nums">{t.shots} ({t.shots_on_target})</TableCell>
                  <TableCell className="font-mono tabular-nums">{t.passes_completed}/{t.passes}</TableCell>
                  <TableCell className="font-mono tabular-nums">{t.avg_pass_distance_m?.toFixed(1) ?? "—"}</TableCell>
                  <TableCell className="font-mono tabular-nums">{t.duels_won}</TableCell>
                  <TableCell className="font-mono tabular-nums">{t.fouls}</TableCell>
                  <TableCell className="font-mono tabular-nums">{t.cards}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            Possession is an event-share proxy (pass/dribble/shot/set-piece share), not tracked ball
            time — no vendor here supplies that. Duels won = tackles + interceptions.
          </p>
        </section>

        <Separator />

        <details>
          <summary className="cursor-pointer text-sm font-semibold text-muted-foreground hover:text-foreground">
            Full generated report text
          </summary>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">{narrative.narrative_text}</p>
        </details>

        {narrative.data_quality_notes.length > 0 && (
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-muted-foreground hover:text-foreground">
              Data quality notes ({narrative.data_quality_notes.length})
            </summary>
            <ul className="mt-3 flex list-disc flex-col gap-1 pl-5 text-sm text-muted-foreground">
              {narrative.data_quality_notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          </details>
        )}
      </CardContent>
    </Card>
  );
}
