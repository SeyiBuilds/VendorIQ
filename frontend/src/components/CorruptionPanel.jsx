import { useEffect, useState } from "react";
import { listMutations, listAgentFaults, corruptAndRunStream } from "../api";
import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { Input } from "./ui/input";
import { Checkbox } from "./ui/checkbox";
import { Slider } from "./ui/slider";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./ui/select";
import { Alert, AlertDescription } from "./ui/alert";
import { Separator } from "./ui/separator";
import { cn } from "@/lib/utils";

export default function CorruptionPanel({ matchId, onStart, onEvent, cleanConfidence }) {
  const [mutations, setMutations] = useState({});
  const [selected, setSelected] = useState(new Set());
  const [rate, setRate] = useState(0.25);
  const [seed, setSeed] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);

  // Orchestrator-level fault injection -- a DIFFERENT feature than the data
  // mutations above. Mutations show an agent gracefully absorbing bad
  // data; this makes an agent genuinely raise so the Master Orchestrator's
  // own retry-then-decide logic actually fires. See fault_injection.py.
  const [agentFaults, setAgentFaults] = useState({});
  const [agentFault, setAgentFault] = useState("");
  const [failAttempts, setFailAttempts] = useState(2);

  useEffect(() => {
    listMutations().then(setMutations).catch((e) => setError(e.message));
    listAgentFaults().then(setAgentFaults).catch((e) => setError(e.message));
  }, []);

  const toggle = (name) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(name) ? next.delete(name) : next.add(name);
      return next;
    });
  };

  const handleCorrupt = async () => {
    if (selected.size === 0 && !agentFault) return;
    setRunning(true);
    setError(null);
    setLastResult(null);
    onStart();
    try {
      await corruptAndRunStream(
        matchId,
        {
          mutations: Array.from(selected),
          rate,
          seed: seed === "" ? null : Number(seed),
          agentFault: agentFault || null,
          failAttempts,
        },
        (event) => {
          onEvent(event);
          if (event.type === "result") setLastResult(event.data);
        },
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  if (!matchId) return null;

  const corruptedConfidence = lastResult?.document?.metadata?.quality_confidence;
  const hasDocument = !!lastResult?.document;

  return (
    <Card className="border-destructive/30">
      <CardHeader>
        <CardTitle>Resilience stress test</CardTitle>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Each toggle below targets one specific, already-handled failure mode in the pipeline — not
          random noise. Pick one or more, set how much of the feed to hit, and run it to see the
          same pipeline absorb it instead of crashing or fabricating data.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="grid gap-3 sm:grid-cols-2">
          {Object.entries(mutations).map(([name, description]) => (
            <label
              key={name}
              className={cn(
                "flex items-start gap-3 rounded-md border px-3 py-2.5 text-sm transition-colors",
                selected.has(name) ? "border-primary/50 bg-primary/8" : "border-border bg-muted",
              )}
            >
              <Checkbox checked={selected.has(name)} onCheckedChange={() => toggle(name)} className="mt-0.5" />
              <span className="flex flex-col gap-0.5">
                <strong className="font-semibold capitalize">{name.replace(/_/g, " ")}</strong>
                <span className="text-xs text-muted-foreground">{description}</span>
              </span>
            </label>
          ))}
        </div>

        <div className="flex flex-wrap items-end gap-5">
          <div className="flex min-w-[220px] flex-col gap-2">
            <Label>
              Corruption rate{" "}
              <span className="font-mono font-normal text-foreground">{Math.round(rate * 100)}%</span>
            </Label>
            <Slider min={0.05} max={0.8} step={0.05} value={[rate]} onValueChange={([v]) => setRate(v)} className="w-full" />
          </div>
          <Label className="flex-col items-start gap-1.5">
            Seed (optional, for a reproducible run)
            <Input
              type="number"
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              placeholder="random"
              className="w-[140px]"
            />
          </Label>
          <Button
            variant="destructive"
            onClick={handleCorrupt}
            disabled={running || (selected.size === 0 && !agentFault)}
          >
            {running ? "Corrupting…" : "Corrupt & run"}
          </Button>
        </div>

        <Separator />

        <section className="flex flex-col gap-3">
          <div>
            <h3 className="text-sm font-bold">Orchestrator decision test</h3>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
              A different thing than the mutations above. Mutations show an agent quietly absorbing
              bad data. This makes one agent genuinely fail, so you can see the Master Orchestrator's
              own retry-then-decide logic actually fire — self-heals on 1 failure, aborts or degrades
              on 2.
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-4">
            <Label className="flex-col items-start gap-1.5">
              Agent to fail
              <Select value={agentFault || "__none__"} onValueChange={(v) => setAgentFault(v === "__none__" ? "" : v)}>
                <SelectTrigger className="w-[220px]"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="__none__">— none —</SelectItem>
                  {Object.entries(agentFaults).map(([name, description]) => (
                    <SelectItem key={name} value={name} title={description}>
                      {name.replace(/_/g, " ")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>
            {agentFault && (
              <Label className="flex-col items-start gap-1.5">
                Failures before recovery
                <Select value={String(failAttempts)} onValueChange={(v) => setFailAttempts(Number(v))}>
                  <SelectTrigger className="w-[260px]"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="1">1 — self-heals on retry</SelectItem>
                    <SelectItem value="2">2 — genuinely fails (abort/degrade)</SelectItem>
                  </SelectContent>
                </Select>
              </Label>
            )}
          </div>
          {agentFault && (
            <p className="text-sm leading-relaxed text-muted-foreground">{agentFaults[agentFault]}</p>
          )}
        </section>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {lastResult && (
          <div className="flex flex-col gap-3">
            {!hasDocument ? (
              <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-sm leading-relaxed text-destructive">
                Run aborted — {agentFault.replace(/_/g, " ")} failed {failAttempts} time
                {failAttempts === 1 ? "" : "s"} and the orchestrator gave up rather than return a
                document it couldn't stand behind. Status: {lastResult.overall_status}.
              </p>
            ) : (
              <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-muted px-3 py-2.5 text-sm">
                <span>
                  Clean confidence: {cleanConfidence != null ? `${(cleanConfidence * 100).toFixed(1)}%` : "—"}
                </span>
                <span className="text-muted-foreground">→</span>
                <span className="font-semibold text-destructive">
                  {lastResult.overall_status === "partial" ? "Degraded" : "Corrupted"} confidence:{" "}
                  {corruptedConfidence != null ? `${(corruptedConfidence * 100).toFixed(1)}%` : "—"}
                </span>
                <span className="text-muted-foreground">
                  {lastResult.document.events.length} events survived validation (started from 300)
                </span>
              </div>
            )}
            {lastResult.corruption_log?.length > 0 && (
              <ul className="flex flex-col gap-1 rounded-md border border-border bg-muted px-3 py-2.5 font-mono text-xs text-muted-foreground">
                {lastResult.corruption_log.map((line, i) => <li key={i}>{line}</li>)}
              </ul>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
