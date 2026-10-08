import { useState } from "react";
import { Download, UploadCloud } from "lucide-react";
import { exportRun } from "../api";
import { Card, CardHeader, CardTitle, CardContent } from "./ui/card";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "./ui/select";
import { Alert, AlertDescription } from "./ui/alert";
import { Separator } from "./ui/separator";

const VENDORS = ["opta", "statsbomb", "wyscout"];

// Only shown once a run has produced a document -- export (Agent 7) reads
// the validated CDF document, so there's nothing to export from a failed
// critical-stage run.
export default function ExportPanel({ runId, hasDocument, document: cdfDocument }) {
  const [targetVendor, setTargetVendor] = useState("statsbomb");
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  if (!runId || !hasDocument) return null;

  const handleDownloadCDF = () => {
    if (!cdfDocument) return;
    const blob = new Blob([JSON.stringify(cdfDocument, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${runId}_cdf.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleExport = async () => {
    setExporting(true);
    setError(null);
    try {
      const data = await exportRun(runId, targetVendor);
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setExporting(false);
    }
  };

  const handleDownload = () => {
    if (!result) return;
    const blob = new Blob([JSON.stringify(result.payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${runId}_${targetVendor}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card className="border-success/30">
      <CardHeader>
        <CardTitle>Export (Agent 7)</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-md text-sm leading-relaxed text-muted-foreground">
            The actual output of this pipeline — one normalized document, whatever vendor(s) went in.
          </p>
          <Button variant="outline" onClick={handleDownloadCDF} disabled={!cdfDocument}>
            <Download />
            Download CDF (raw .json)
          </Button>
        </div>

        <Separator />

        <p className="text-sm leading-relaxed text-muted-foreground">
          Re-export this run's validated CDF document back into another vendor's shape — on demand,
          never part of the pipeline run itself (see orchestrator.py's design note on why).
        </p>

        <div className="flex flex-wrap items-end gap-4">
          <Label className="flex-col items-start gap-1.5">
            Target vendor
            <Select value={targetVendor} onValueChange={(v) => { setTargetVendor(v); setResult(null); }}>
              <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
              <SelectContent>
                {VENDORS.map((v) => <SelectItem key={v} value={v}>{v}</SelectItem>)}
              </SelectContent>
            </Select>
          </Label>
          <Button onClick={handleExport} disabled={exporting}>
            <UploadCloud />
            {exporting ? "Exporting…" : "Export"}
          </Button>
          {result && (
            <Button variant="outline" onClick={handleDownload}>
              <Download />
              Download {targetVendor}.json
            </Button>
          )}
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {result && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Exported {result.report.events_exported} events to {result.report.target_vendor} shape
            {result.report.players_with_synthesized_id > 0 &&
              ` (${result.report.players_with_synthesized_id} player ID${result.report.players_with_synthesized_id === 1 ? "" : "s"} synthesized -- no native ID for that vendor)`}
            .
          </p>
        )}
      </CardContent>
    </Card>
  );
}
