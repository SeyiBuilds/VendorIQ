import { useState } from "react";
import { unzipSync, strFromU8 } from "fflate";
import { Radio, X } from "lucide-react";
import { ingestRunStream, ingestRunStreamMulti } from "../api";
import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { Input } from "./ui/input";
import { Checkbox } from "./ui/checkbox";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./ui/select";
import { Alert, AlertDescription } from "./ui/alert";
import { Badge } from "./ui/badge";
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "./ui/table";

// Auto-collapse the per-file table once a batch gets long -- a 190-row
// table is just noise to scroll past; the live stage board above and the
// status-count summary are what actually show what happened.
const AUTO_COLLAPSE_THRESHOLD = 15;

const MAX_FILES_DISPLAYED = 8;

function isZip(file) {
  return /\.zip$/i.test(file.name) || file.type === "application/zip" || file.type === "application/x-zip-compressed";
}

// Not an event feed at all -- a competition's match list (data/matches/...)
// or a lineup/team-sheet file (data/lineups/...), both real shapes in
// StatsBomb's open-data zips but neither is a list of match events. Running
// either through the event pipeline isn't a vendor-detection failure, it's
// the wrong kind of document, so these are filtered out before a run rather
// than left to score an honest-but-confusing 0% confidence.
function isNonEventStatsBombFile(entryName) {
  return /(^|\/)data\/matches\//i.test(entryName) || /(^|\/)data\/lineups\//i.test(entryName) || /(^|\/)competitions\.json$/i.test(entryName);
}

function parseJsonEntry(text) {
  const parsed = JSON.parse(text);
  // Accept a bare array too -- real StatsBomb open-data event files are
  // shaped exactly that way (a top-level list of event objects, no
  // wrapper). The backend wraps it as {"events": [...]} before detection;
  // this just rejects anything that isn't an object or a non-empty array.
  const isUsableArray = Array.isArray(parsed) && parsed.length > 0 && parsed.every((e) => typeof e === "object" && e !== null);
  const isUsableObject = typeof parsed === "object" && parsed !== null && !Array.isArray(parsed);
  if (!isUsableObject && !isUsableArray) {
    throw new Error("must contain a JSON object, or a non-empty array of event objects");
  }
  return parsed;
}

// Expands whatever the person selected into a flat list of { label, payload }
// items -- one per vendor-shaped JSON document. A .zip is unzipped entirely
// client-side (fflate, no upload round-trip just to look inside it); every
// .json entry inside it becomes its own item, same as a loose .json file
// picked directly. Nothing here pairs files together (e.g. a StatsBomb
// events file with its matching lineups file in the same zip) -- each JSON
// document is still run through the pipeline as its own independent vendor
// payload, exactly like a single upload always has been.
async function expandFiles(fileList) {
  const items = [];
  const loadErrors = [];

  for (const file of fileList) {
    if (isZip(file)) {
      let unzipped;
      try {
        const buffer = await file.arrayBuffer();
        unzipped = unzipSync(new Uint8Array(buffer));
      } catch (err) {
        loadErrors.push(`${file.name}: could not unzip (${err.message})`);
        continue;
      }
      for (const [entryName, data] of Object.entries(unzipped)) {
        if (entryName.endsWith("/") || !entryName.toLowerCase().endsWith(".json")) continue;
        if (isNonEventStatsBombFile(entryName)) {
          loadErrors.push(`${file.name} → ${entryName}: skipped (competition/lineup file, not a match-events feed)`);
          continue;
        }
        try {
          const payload = parseJsonEntry(strFromU8(data));
          items.push({ label: `${file.name} → ${entryName}`, payload });
        } catch (err) {
          loadErrors.push(`${file.name} → ${entryName}: ${err.message}`);
        }
      }
    } else {
      try {
        const payload = parseJsonEntry(await file.text());
        items.push({ label: file.name, payload });
      } catch (err) {
        loadErrors.push(`${file.name}: ${err.message}`);
      }
    }
  }

  return { items, loadErrors };
}

export default function UploadPanel({ onStart, onEvent, onBatchComplete, onLive, liveBusy = false }) {
  const [items, setItems] = useState([]);
  const [loadErrors, setLoadErrors] = useState([]);
  const [results, setResults] = useState([]);
  const [progress, setProgress] = useState(null); // { index, total, label }
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [showAllResults, setShowAllResults] = useState(false);
  // Combine mode: 2+ files of the SAME match (e.g. Opta + StatsBomb),
  // merged into one CDF document instead of run as independent pipeline
  // runs. See api.ingestRunStreamMulti / orchestrator.run_streaming_multi
  // for why this is "primary feed + enrichment", not a naive concatenation.
  const [combineMode, setCombineMode] = useState(false);
  const [primaryIndex, setPrimaryIndex] = useState(0);
  const [combinedResult, setCombinedResult] = useState(null);
  // Which loaded file "Play as live feed" replays when several are loaded
  // (e.g. a zip of many matches). Live replay is one match at a time.
  const [liveIndex, setLiveIndex] = useState(0);

  const handleFiles = async (e) => {
    const fileList = Array.from(e.target.files || []);
    if (fileList.length === 0) return;
    setError(null);
    setResults([]);
    setCombinedResult(null);
    setProgress(null);
    setShowAllResults(false);
    const { items: loaded, loadErrors: errs } = await expandFiles(fileList);
    setItems(loaded);
    setPrimaryIndex(0);
    setLiveIndex(0);
    setLoadErrors(errs);
  };

  // Drop one loaded file before running -- e.g. a wrong pick, or a file that
  // shouldn't be part of a combine. Keeps primaryIndex pointing at the same
  // logical item (or resets to 0 if that one was just removed).
  const removeItem = (index) => {
    setItems((prev) => prev.filter((_, i) => i !== index));
    setPrimaryIndex((prev) => {
      if (index < prev) return prev - 1;
      if (index === prev) return 0;
      return prev;
    });
    setLiveIndex((prev) => {
      if (index < prev) return prev - 1;
      if (index === prev) return 0;
      return prev;
    });
  };

  const clearAll = () => {
    setItems([]);
    setLoadErrors([]);
    setPrimaryIndex(0);
    setLiveIndex(0);
    setCombinedResult(null);
    setResults([]);
    setCombineMode(false);
  };

  const handleRunAll = async () => {
    if (items.length === 0) return;
    setRunning(true);
    setError(null);
    setResults([]);
    setShowAllResults(items.length <= AUTO_COLLAPSE_THRESHOLD);

    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      setProgress({ index: i + 1, total: items.length, label: item.label });
      // Only the first file scrolls the view to PipelineStatus -- scrolling
      // on every one of potentially hundreds of files would be pure jitter.
      onStart(i === 0);
      let captured = null;
      try {
        // Paced (visible per-stage thinking) only for a single file -- the
        // same pauses repeated per stage, per file would turn a batch of
        // dozens/hundreds of files into minutes of apparent freeze.
        await ingestRunStream(item.payload, (event) => {
          onEvent(event);
          if (event.type === "result") captured = event.data;
        }, { pace: items.length === 1 });
        setResults((prev) => [...prev, {
          label: item.label,
          status: captured?.overall_status ?? "unknown",
          confidence: captured?.document?.metadata?.quality_confidence ?? null,
        }]);
      } catch (err) {
        setResults((prev) => [...prev, { label: item.label, status: "error", error: err.message }]);
      }
    }

    setProgress(null);
    setRunning(false);
    // Scroll to Outputs once here, after the whole batch -- not per file,
    // which would otherwise bounce the person down to Outputs (briefly
    // showing that file's result) and back to blank between every file.
    onBatchComplete?.();
  };

  const handleRunCombined = async () => {
    if (items.length < 2) return;
    setRunning(true);
    setError(null);
    setCombinedResult(null);
    onStart();
    try {
      let captured = null;
      await ingestRunStreamMulti(items.map((it) => it.payload), primaryIndex, (event) => {
        onEvent(event);
        if (event.type === "result") captured = event.data;
      });
      setCombinedResult({
        status: captured?.overall_status ?? "unknown",
        confidence: captured?.document?.metadata?.quality_confidence ?? null,
        eventCount: captured?.document?.events?.length ?? 0,
        sourceVendors: captured?.document?.metadata?.source_vendors ?? [],
      });
      onBatchComplete?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  };

  const shown = items.slice(0, MAX_FILES_DISPLAYED);
  const extra = items.length - shown.length;

  return (
    <Card className="border-primary/30">
      <CardHeader>
        <CardTitle>Bring your own match data</CardTitle>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Upload any vendor's match JSON — pick .json files or a .zip of them. Format is
          auto-detected; unrecognized shapes still run, just with lower confidence.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <Label className="flex-col items-start gap-1.5">
            Match file(s) (.json or .zip)
            <Input
              type="file"
              accept="application/json,.json,.zip,application/zip,application/x-zip-compressed"
              multiple
              onChange={handleFiles}
              disabled={running}
              className="h-auto py-1.5 file:mr-2 file:rounded file:border-0 file:bg-secondary file:px-2 file:py-1"
            />
          </Label>
          {!combineMode && (
            <Button onClick={handleRunAll} disabled={items.length === 0 || running || liveBusy}>
              {running
                ? `Running ${progress ? `${progress.index}/${progress.total}` : "…"}`
                : `Run ${items.length > 1 ? `all ${items.length} matches` : "uploaded match"}`}
            </Button>
          )}
          {/* Live replay is one match at a time. With one file loaded the
              button sits here; with several, the picker below chooses which. */}
          {!combineMode && items.length === 1 && onLive && (
            <Button variant="secondary" onClick={() => onLive(items[0].payload)} disabled={running || liveBusy}>
              <Radio />
              Play as live feed
            </Button>
          )}
        </div>

        {!combineMode && items.length >= 2 && onLive && (
          <div className="flex items-end gap-3 border-t border-border/70 pt-3">
            <Label className="min-w-0 flex-1 flex-col items-start gap-1.5">
              Play one match as a live feed
              <Select
                value={String(Math.min(liveIndex, items.length - 1))}
                onValueChange={(v) => setLiveIndex(Number(v))}
                disabled={running || liveBusy}
              >
                <SelectTrigger className="w-full min-w-0">
                  <SelectValue>
                    <span className="block min-w-0 truncate text-left">
                      {items[Math.min(liveIndex, items.length - 1)]?.label}
                    </span>
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {items.map((it, i) => (
                    <SelectItem key={i} value={String(i)}>{it.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Label>
            <Button
              variant="secondary"
              onClick={() => onLive(items[Math.min(liveIndex, items.length - 1)].payload)}
              disabled={running || liveBusy}
              className="shrink-0"
            >
              <Radio />
              Play as live feed
            </Button>
          </div>
        )}

        {items.length >= 2 && (
          <div className="flex flex-col gap-3 border-t border-border/70 pt-3">
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={combineMode}
                onCheckedChange={(v) => { setCombineMode(v === true); setCombinedResult(null); }}
                disabled={running}
              />
              Combine these {items.length} files into one CDF (same match, multiple vendors)
            </label>
            {combineMode && (
              <div className="flex items-end gap-3">
                <Label className="min-w-0 flex-1 flex-col items-start gap-1.5">
                  Primary (event source)
                  <Select
                    value={String(primaryIndex)}
                    onValueChange={(v) => setPrimaryIndex(Number(v))}
                    disabled={running}
                  >
                    {/* w-full + min-w-0 so this sizes to the row, never past it. Radix's
                        own Select.Value drops className/style, so truncation has to be
                        on an explicit span we pass as its children -- a long zip-expanded
                        label (file.zip -> data/events/x.json) truncates inside the box
                        instead of pushing the button out. */}
                    <SelectTrigger className="w-full min-w-0">
                      <SelectValue>
                        <span className="block min-w-0 truncate text-left">{items[primaryIndex]?.label}</span>
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {items.map((it, i) => (
                        <SelectItem key={i} value={String(i)}>{it.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Label>
                <Button onClick={handleRunCombined} disabled={running} className="shrink-0">
                  {running ? "Combining…" : "Combine into 1 CDF"}
                </Button>
              </div>
            )}
          </div>
        )}

        {combineMode && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Events come from the primary file only — others enrich player identity and match info.
          </p>
        )}

        {items.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <p className="text-sm text-muted-foreground">
                Loaded {items.length} match file{items.length === 1 ? "" : "s"}
              </p>
              <button
                type="button"
                onClick={clearAll}
                disabled={running}
                className="text-sm font-semibold text-muted-foreground underline underline-offset-2 hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
              >
                Clear all
              </button>
            </div>
            <ul className="flex flex-col gap-1.5">
              {shown.map((it, i) => (
                <li
                  key={i}
                  className="flex items-center gap-2 rounded-md border border-border bg-muted px-2.5 py-1.5 text-sm"
                >
                  <span className="flex-1 truncate" title={it.label}>{it.label}</span>
                  <button
                    type="button"
                    onClick={() => removeItem(i)}
                    disabled={running}
                    aria-label={`Remove ${it.label}`}
                    className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-destructive disabled:pointer-events-none disabled:opacity-50"
                  >
                    <X className="size-3.5" />
                  </button>
                </li>
              ))}
            </ul>
            {extra > 0 && (
              <p className="text-sm text-muted-foreground">+ {extra} more (remove some to see them)</p>
            )}
          </div>
        )}

        {progress && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Running {progress.index} of {progress.total}: {progress.label}
          </p>
        )}

        {loadErrors.length > 0 && (
          <details>
            <summary className="cursor-pointer text-sm font-semibold text-muted-foreground hover:text-foreground">
              {loadErrors.length} file(s) skipped while loading
            </summary>
            <ul className="mt-2 flex list-disc flex-col gap-1 pl-5 text-sm text-muted-foreground">
              {loadErrors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          </details>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {combinedResult && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Combined into one CDF — <strong className="text-foreground">{combinedResult.status}</strong>,{" "}
            {combinedResult.eventCount} event{combinedResult.eventCount === 1 ? "" : "s"} from{" "}
            {combinedResult.sourceVendors.join(", ") || "primary"},{" "}
            {combinedResult.confidence != null ? `${(combinedResult.confidence * 100).toFixed(1)}%` : "—"} confidence.
            Full document below.
          </p>
        )}

        {results.length > 0 && (
          <ResultsSummary
            results={results}
            showAll={showAllResults}
            onToggleShowAll={() => setShowAllResults((v) => !v)}
          />
        )}
      </CardContent>
    </Card>
  );
}

function ResultsSummary({ results, showAll, onToggleShowAll }) {
  const counts = results.reduce((acc, r) => {
    const key = r.error ? "error" : r.status;
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm leading-relaxed text-muted-foreground">
        {results.length} run{results.length === 1 ? "" : "s"} so far —{" "}
        {Object.entries(counts).map(([status, n], i) => (
          <span key={status}>
            {i > 0 && ", "}
            <strong className="text-foreground">{n}</strong> {status}
          </span>
        ))}
        {results.length > AUTO_COLLAPSE_THRESHOLD && (
          <>
            {" "}
            <button
              className="font-semibold text-primary underline underline-offset-2"
              onClick={onToggleShowAll}
            >
              {showAll ? "hide per-file table" : "show per-file table"}
            </button>
          </>
        )}
      </p>

      {showAll && (
        <Table>
          <TableHeader>
            <TableRow><TableHead>File</TableHead><TableHead>Status</TableHead><TableHead>Confidence</TableHead></TableRow>
          </TableHeader>
          <TableBody>
            {results.map((r, i) => (
              <TableRow key={i}>
                <TableCell className="max-w-[420px] truncate font-normal">{r.label}</TableCell>
                <TableCell>
                  {r.error ? (
                    <Badge variant="destructive">error</Badge>
                  ) : (
                    <Badge variant={r.status === "success" ? "success" : r.status === "partial" ? "warning" : "muted"}>
                      {r.status}
                    </Badge>
                  )}
                  {r.error && <span className="ml-2 text-xs text-muted-foreground">{r.error}</span>}
                </TableCell>
                <TableCell className="font-mono tabular-nums">
                  {r.confidence != null ? `${(r.confidence * 100).toFixed(1)}%` : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
