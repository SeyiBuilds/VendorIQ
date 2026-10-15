import { Play, Radio } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Label } from "./ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./ui/select";
import { Button } from "./ui/button";

const VENDORS = ["opta", "statsbomb", "wyscout"];

// Wall-clock seconds per match minute for live feed replay.
const LIVE_SPEEDS = [
  { value: "0.3", label: "Fast" },
  { value: "0.6", label: "Normal" },
  { value: "1.2", label: "Slow" },
];

export default function MatchPicker({
  matches, selectedMatchId, selectedVendor, onChange, onRun, onLive, liveSpeed, onLiveSpeedChange, running,
}) {
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
        <div className="flex flex-wrap items-end gap-3">
          <Button onClick={onRun} disabled={running || !selectedMatchId} className="w-fit">
            <Play />
            {running ? "Running pipeline…" : "Run pipeline"}
          </Button>
          <Button variant="secondary" onClick={onLive} disabled={running || !selectedMatchId} className="w-fit">
            <Radio />
            Play as live feed
          </Button>
          <Label className="flex-col items-start gap-1.5">
            Live speed
            <Select value={String(liveSpeed)} onValueChange={(v) => onLiveSpeedChange(Number(v))} disabled={running}>
              <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {LIVE_SPEEDS.map((s) => (
                  <SelectItem key={s.value} value={s.value}>{s.label} ({s.value}s/min)</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Label>
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Single-vendor ingestion is the realistic default — pick which vendor's feed to run through
          the 7-agent pipeline (detect format → map schema → normalize coordinates → resolve
          identities → validate → narrate → export on demand). Live feed delivers the same file one
          match minute at a time and re-runs the pipeline after each minute, so the match report
          updates as the game unfolds.
        </p>
      </CardContent>
    </Card>
  );
}
