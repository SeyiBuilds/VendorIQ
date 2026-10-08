import { Play } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Label } from "./ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./ui/select";
import { Button } from "./ui/button";

const VENDORS = ["opta", "statsbomb", "wyscout"];

export default function MatchPicker({ matches, selectedMatchId, selectedVendor, onChange, onRun, running }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Match &amp; feed</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <Label className="flex-1 min-w-[180px] flex-col items-start gap-1.5">
            Match
            <Select
              value={selectedMatchId}
              onValueChange={(v) => onChange({ matchId: v, vendor: selectedVendor })}
            >
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {matches.map((m) => (
                  <SelectItem key={m.match_id} value={m.match_id}>
                    {m.home_team} vs {m.away_team} ({m.quality_tier})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
          <Label className="flex-col items-start gap-1.5">
            Vendor feed
            <Select
              value={selectedVendor}
              onValueChange={(v) => onChange({ matchId: selectedMatchId, vendor: v })}
            >
              <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {VENDORS.map((v) => (
                  <SelectItem key={v} value={v}>{v}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
        </div>
        <Button onClick={onRun} disabled={running || !selectedMatchId} className="w-fit">
          <Play />
          {running ? "Running pipeline…" : "Run pipeline"}
        </Button>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Single-vendor ingestion is the realistic default — pick which vendor's feed to run through
          the 7-agent pipeline (detect format → map schema → normalize coordinates → resolve
          identities → validate → narrate → export on demand).
        </p>
      </CardContent>
    </Card>
  );
}
