import { useState } from "react";
import { unzipSync, strFromU8 } from "fflate";
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

export default function UploadPanel({ onStart, onEvent }) {
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
    setLoadErrors(errs);
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
      onStart();
      let captured = null;
      try {
        await ingestRunStream(item.payload, (event) => {
          onEvent(event);
          if (event.type === "result") captured = event.data;
        });
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
          Upload Opta, StatsBomb, or Wyscout JSON as-is — including bare event arrays. Pick multiple
          .json files or a .zip of them; each runs through the 7-agent pipeline on its own, vendor
          auto-detected. Unrecognized shapes degrade honestly, never crash or fabricate data.
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
            <Button onClick={handleRunAll} disabled={items.length === 0 || running}>
              {running
                ? `Running ${progress ? `${progress.index}/${progress.total}` : "…"}`
                : `Run ${items.length > 1 ? `all ${items.length} matches` : "uploaded match"}`}
            </Button>
          )}
        </div>

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
              <div className="flex flex-wrap items-end gap-4">
                <Label className="flex-col items-start gap-1.5">
                  Primary (event source)
                  <Select
                    value={String(primaryIndex)}
                    onValueChange={(v) => setPrimaryIndex(Number(v))}
                    disabled={running}
                  >
                    <SelectTrigger className="w-[260px]"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {items.map((it, i) => (
                        <SelectItem key={i} value={String(i)}>{it.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Label>
                <Button onClick={handleRunCombined} disabled={running}>
                  {running ? "Combining…" : "Combine into 1 CDF"}
                </Button>
              </div>
            )}
          </div>
        )}

        {combineMode && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Events come only from the primary file, so counts stay real — every other file is used to
            cross-check player identity and fill in match info, never double-counted as events.
          </p>
        )}

        {items.length > 0 && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Loaded {items.length} match file{items.length === 1 ? "" : "s"}:{" "}
            {shown.map((it) => it.label).join(", ")}
            {extra > 0 && ` + ${extra} more`}
          </p>
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
