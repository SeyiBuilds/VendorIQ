import { CheckCircle2, CircleDashed, Loader2, SkipForward, XCircle } from "lucide-react";
import { Card, CardHeader, CardTitle } from "./ui/card";
import { Badge } from "./ui/badge";
import { cn } from "@/lib/utils";

const STATUS_LABEL = {
  pending: "pending",
  running: "running…",
  success: "success",
  failed: "failed",
  skipped: "skipped",
};

const BADGE_VARIANT = { success: "success", partial: "warning", failed: "destructive" };

const STAGE_ICON = {
  pending: CircleDashed,
  running: Loader2,
  success: CheckCircle2,
  failed: XCircle,
  skipped: SkipForward,
};

const STAGE_ICON_CLASS = {
  pending: "text-muted-foreground/50",
  running: "text-primary animate-spin",
  success: "text-success",
  failed: "text-destructive",
  skipped: "text-muted-foreground",
};

// `records` is a live-updating array of { stage, status, detail, retried }
// built up in App.jsx as stream events arrive -- "pending" for an agent
// that hasn't started yet, "running" for one actually in flight right
// now, then "success"/"failed" once its event arrives. This is what
// makes the pipeline visibly work stage by stage instead of appearing
// fully formed after the whole run finishes.
export default function PipelineStatus({ records, overallStatus, confidence }) {
  if (!records || records.length === 0) return null;

  return (
    <Card className="border-primary/30">
      <CardHeader className="flex-row flex-wrap items-center gap-3 space-y-0">
        <CardTitle>Pipeline run</CardTitle>
        {overallStatus && <Badge variant={BADGE_VARIANT[overallStatus] ?? "default"}>{overallStatus}</Badge>}
        {confidence != null && (
          <span className="font-mono text-sm text-muted-foreground">
            quality confidence: {(confidence * 100).toFixed(1)}%
          </span>
        )}
      </CardHeader>
      <div className="flex flex-col gap-2 px-5">
        {records.map((r) => {
          const Icon = STAGE_ICON[r.status] ?? CircleDashed;
          return (
            <div
              key={r.stage}
              className={cn(
                "flex items-center gap-3 rounded-md border border-transparent px-3 py-2 text-sm",
                r.status === "pending" && "opacity-50",
                r.status === "running" && "border-primary/25 bg-primary/8",
              )}
            >
              <Icon className={cn("size-4 shrink-0", STAGE_ICON_CLASS[r.status])} />
              <span className="min-w-[180px] font-medium capitalize">{r.stage.replace(/_/g, " ")}</span>
              <Badge variant={r.status === "success" ? "success" : r.status === "failed" ? "destructive" : "muted"}>
                {STATUS_LABEL[r.status] || r.status}
              </Badge>
              {r.retried && (
                <span className="text-xs font-bold uppercase text-warning">retried</span>
              )}
              {r.detail && <span className="truncate text-xs text-muted-foreground">{r.detail}</span>}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
