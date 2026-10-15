"""
Master Orchestrator

Wires Agents 1-6 into one entry point and owns the TOP-LEVEL error handling
across stages -- distinct from the per-field retry/fallback logic each
agent already owns internally. Every agent in this pipeline already
degrades gracefully on bad DATA (a missing field, an unknown vendor, a
name conflict with no API key) -- that is handled inside each agent and
never raises. What this orchestrator catches is an agent-level failure:
a genuine bug, an unexpected input shape, a dependency problem -- the
kind of thing no amount of per-field fallback logic prevents.

Stage criticality, per the PRD's "ingest/interpret" vs. "explain" split:
  CRITICAL stages (1-5: Format Detective -> Schema Mapper -> Coordinate
  Translator -> Entity Resolver -> Validator) produce the CDFDocument
  itself. If any of these fails, there is no usable output at all, so the
  run aborts immediately and returns a failed PipelineResult with the
  reason logged -- there's nothing honest to fall back to.

  OPTIONAL stage (6: Narrative Generator) is a presentation layer built ON
  TOP of a valid CDFDocument. If it fails unexpectedly, the run does NOT
  abort -- it logs the failure, skips that stage, and still returns
  everything that DID succeed. A club getting normalized match data with
  no narrative text is in a far better position than getting nothing at
  all.

Each stage gets exactly one retry before being treated as failed. This
matters most for the optional Claude-escalation calls nested inside
Entity Resolver/Narrative Generator, which already catch their own
network/API exceptions internally and never raise -- so in practice a
retry here mostly guards against transient, non-data-quality problems
(an import hiccup, a resource blip), which is a legitimate thing to guard
against even in a fully deterministic pipeline.

Format Exporter (Agent 7) is deliberately NOT part of run() -- per the
locked MVP decision, export is on-demand only. It is exposed as a
separate method on this class instead.

Realistic default: run() ingests ONE vendor file at a time (single-feed
ingestion is the common case). Passing events from multiple vendors
through Entity Resolver for genuine cross-vendor identity reconciliation
is a distinct, advanced use documented on Entity Resolver itself -- this
orchestrator's run() does not attempt to merge multiple vendor event
streams, since that would double/triple-count every real-world event for
stats purposes (see Narrative Generator's docstring caveats).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Callable, Optional, TypeVar

from format_detective import FormatDetective, DetectionResult
from schema_mapper import SchemaMapper
from coordinate_translator import CoordinateTranslator
from entity_resolver import EntityResolver, PlayerIdentity, ResolvedEvent
from validator import Validator, ValidationReport
from narrative_generator import NarrativeGenerator, NarrativeReport
from format_exporter import FormatExporter, ExportReport
from cdf_models import CDFDocument

T = TypeVar("T")

CRITICAL_STAGES = ("format_detective", "schema_mapper", "coordinate_translator", "entity_resolver", "validator")
OPTIONAL_STAGES = ("match_sheet_mapper", "narrative_generator")

# Actual run_streaming() execution order -- distinct from CRITICAL_STAGES +
# OPTIONAL_STAGES, which group stages by abort-vs-continue semantics, not
# by when they run. match_sheet_mapper runs between entity_resolver and
# validator (validator needs its output to attach match_sheet to the
# document), ahead of validator even though validator is "critical" and
# match_sheet_mapper is "optional".
STAGE_ORDER = (
    "format_detective", "schema_mapper", "coordinate_translator", "entity_resolver",
    "match_sheet_mapper", "validator", "narrative_generator",
)


@dataclass
class StageRecord:
    stage: str
    status: str  # "success" | "failed" | "skipped"
    detail: str = ""
    retried: bool = False


@dataclass
class OrchestrationLog:
    run_id: str
    records: list[StageRecord] = field(default_factory=list)
    overall_status: str = "success"  # "success" | "partial" | "failed"

    def record(self, stage: str, status: str, detail: str = "", retried: bool = False) -> None:
        self.records.append(StageRecord(stage=stage, status=status, detail=detail, retried=retried))


@dataclass
class PipelineResult:
    document: Optional[CDFDocument]
    narrative: Optional[NarrativeReport]
    orchestration_log: OrchestrationLog


class MasterOrchestrator:
    """Wires Agents 1-6 into one run() call. export() is a separate,
    on-demand entry point per the locked MVP decision."""

    def __init__(self, ai_escalation_enabled: bool = True, narrative_in_progress: bool = False):
        self.detective = FormatDetective()
        self.mapper = SchemaMapper()
        self.translator = CoordinateTranslator()
        self.resolver = EntityResolver(ai_escalation_enabled=ai_escalation_enabled)
        self.validator = Validator()
        # narrative_in_progress: set for live feed replay's mid-match updates
        # (live_feed.py), so the narrative never calls a partial score final.
        self.narrator = NarrativeGenerator(ai_enabled=ai_escalation_enabled, in_progress=narrative_in_progress)
        self.exporter = FormatExporter()

    def run(self, vendor_data: dict, match_date: Optional[date_cls] = None,
            match_sheet_data: Optional[dict] = None) -> PipelineResult:
        """Blocking convenience wrapper: drains run_streaming() and returns
        just the final PipelineResult, discarding the per-stage events.
        Existing callers (tests, the CLI block below) that just want the
        end result keep working unchanged."""
        result: Optional[PipelineResult] = None
        for kind, payload in self.run_streaming(vendor_data, match_date=match_date, match_sheet_data=match_sheet_data):
            if kind == "result":
                result = payload
        assert result is not None  # run_streaming always ends with a "result" event
        return result

    def run_streaming(self, vendor_data: dict, match_date: Optional[date_cls] = None,
                       match_sheet_data: Optional[dict] = None):
        """Generator version of run(): yields real-time progress events as
        each agent actually executes, instead of handing back one lump
        result after the whole pipeline has already finished. This is what
        lets a UI show agents working live rather than a static final
        state -- including genuine wall-clock gaps when Entity Resolver or
        Narrative Generator's optional Claude-escalation calls are
        actually in flight (ANTHROPIC_API_KEY set).

        Yields:
          ("start", stage_name)              -- right before a stage begins
          ("stage", StageRecord)             -- right after it finishes (success/failed)
          ("result", PipelineResult)         -- exactly once, always last
        """
        run_id = uuid.uuid4().hex[:12]
        log = OrchestrationLog(run_id=run_id)

        yield ("start", "format_detective")
        detection = self._run_critical_stage(log, "format_detective", lambda: self.detective.detect(vendor_data))
        yield ("stage", log.records[-1])
        if detection is None:
            yield ("result", self._failed_result(log))
            return

        yield ("start", "schema_mapper")
        mapped = self._run_critical_stage(
            log, "schema_mapper",
            lambda: self.mapper.map_events(vendor_data, detection.detected_format, detection.event_list_key),
        )
        yield ("stage", log.records[-1])
        if mapped is None:
            yield ("result", self._failed_result(log))
            return

        yield ("start", "coordinate_translator")
        translated = self._run_critical_stage(log, "coordinate_translator", lambda: self.translator.translate(mapped))
        yield ("stage", log.records[-1])
        if translated is None:
            yield ("result", self._failed_result(log))
            return

        yield ("start", "entity_resolver")
        resolution = self._run_critical_stage(log, "entity_resolver", lambda: self.resolver.resolve(translated))
        yield ("stage", log.records[-1])
        if resolution is None:
            yield ("result", self._failed_result(log))
            return
        resolved, registry = resolution

        # Match sheet is a separate pillar from the event stream above --
        # a different payload, optionally supplied, mapped independently
        # (see schema_mapper.map_match_sheet). Optional stage: no payload
        # supplied means "skipped", never attempted; a mapping failure
        # degrades the run to "partial" rather than aborting it, exactly
        # like Narrative Generator below.
        yield ("start", "match_sheet_mapper")
        if match_sheet_data is not None:
            mapped_match_sheet = self._run_optional_stage(
                log, "match_sheet_mapper", lambda: self.mapper.map_match_sheet(match_sheet_data),
            )
        else:
            mapped_match_sheet = None
            log.record("match_sheet_mapper", "skipped", detail="no match-sheet payload supplied")
        yield ("stage", log.records[-1])

        yield ("start", "validator")
        validation = self._run_critical_stage(
            log, "validator",
            lambda: self.validator.build_document(
                resolved, registry, {detection.detected_format: vendor_data}, match_date=match_date,
                mapped_match_sheet=mapped_match_sheet,
            ),
        )
        yield ("stage", log.records[-1])
        if validation is None:
            yield ("result", self._failed_result(log))
            return
        document, validation_report = validation
        document.metadata.orchestration_log_ref = run_id

        yield ("start", "narrative_generator")
        narrative = self._run_optional_stage(log, "narrative_generator", lambda: self.narrator.generate(document))
        yield ("stage", log.records[-1])

        # "skipped" (match_sheet_mapper with no payload supplied) is a
        # deliberate no-op, not a degradation -- only an actual failure of
        # an optional stage knocks the run down to "partial".
        failed_optional = [r for r in log.records if r.stage in OPTIONAL_STAGES and r.status == "failed"]
        log.overall_status = "partial" if failed_optional else "success"

        yield ("result", PipelineResult(document=document, narrative=narrative, orchestration_log=log))

    def run_streaming_multi(self, vendor_payloads: list[dict], primary_index: int = 0,
                             match_date: Optional[date_cls] = None, match_sheet_data: Optional[dict] = None):
        """Genuine multi-vendor combine -- "primary feed + enrichment", not a
        naive concatenation of every vendor's events. Why not concatenate:
        every vendor records the SAME real-world match independently, so an
        Opta shot at minute 23 and a StatsBomb shot at minute 23 are the
        same kick, not two kicks. Nothing in this pipeline matches events
        across vendors as "the same real event" (that's a genuinely hard
        problem -- temporal/spatial matching with its own conflict-resolution
        logic), so counting every vendor's events would silently double or
        triple-count every stat in the narrative. That's the double-count
        problem run_streaming()'s own docstring already flags as the reason
        single-vendor is the default.

        What this DOES do instead: one vendor's file (vendor_payloads[primary_index])
        is the actual event timeline -- only its events become CDFEvents, so
        counts stay real. Every OTHER supplied vendor file still earns its
        keep: its events are fed into Entity Resolver too (translator +
        resolver run across the full combined list), so cross-vendor
        corroboration genuinely improves player-identity confidence and
        fills in players Entity Resolver would otherwise have flagged
        unresolved -- then only the primary vendor's slice of the resolved
        list goes to Validator for the event stream, while the FULL
        cross-vendor registry still backs the player list. Secondary
        vendors' raw payloads also feed Validator's match-info extraction
        (home/away team, date, venue), which already does its own
        cross-vendor fallback when the primary file leaves a gap.
        """
        run_id = uuid.uuid4().hex[:12]
        log = OrchestrationLog(run_id=run_id)

        if not vendor_payloads:
            log.record("format_detective", "failed", detail="no vendor payloads supplied")
            yield ("result", self._failed_result(log))
            return
        if not (0 <= primary_index < len(vendor_payloads)):
            log.record("format_detective", "failed", detail=f"primary_index {primary_index} out of range for {len(vendor_payloads)} file(s)")
            yield ("result", self._failed_result(log))
            return

        # Primary file first so its mapped events land at the front of the
        # combined list -- translator/resolver preserve input order 1:1, so
        # a simple length-based split recovers "which events were primary"
        # after both stages run, without needing to trust vendor_source
        # strings (two files could legitimately detect as the same vendor).
        ordered_payloads = [vendor_payloads[primary_index]] + [
            p for i, p in enumerate(vendor_payloads) if i != primary_index
        ]

        yield ("start", "format_detective")
        detections = self._run_critical_stage(
            log, "format_detective", lambda: [self.detective.detect(p) for p in ordered_payloads],
        )
        yield ("stage", log.records[-1])
        if detections is None:
            yield ("result", self._failed_result(log))
            return
        per_file_detail = ", ".join(
            f"file {i + 1}: {d.detected_format} ({d.confidence:.2f})" for i, d in enumerate(detections)
        )
        log.records[-1].detail = f"primary=file 1 ({detections[0].detected_format}); {per_file_detail}"

        yield ("start", "schema_mapper")
        def _map_all():
            per_file = [
                self.mapper.map_events(p, d.detected_format, d.event_list_key)
                for p, d in zip(ordered_payloads, detections)
            ]
            vendor_raw_data = {d.detected_format: p for p, d in zip(ordered_payloads, detections)}
            return per_file, vendor_raw_data
        mapping_result = self._run_critical_stage(log, "schema_mapper", _map_all)
        yield ("stage", log.records[-1])
        if mapping_result is None:
            yield ("result", self._failed_result(log))
            return
        per_file_mapped, vendor_raw_data = mapping_result
        primary_count = len(per_file_mapped[0])
        combined_mapped = [e for file_events in per_file_mapped for e in file_events]
        log.records[-1].detail = (
            f"{primary_count} primary event(s) + {len(combined_mapped) - primary_count} "
            f"secondary event(s) (secondary used for identity resolution only, not counted in the document)"
        )

        yield ("start", "coordinate_translator")
        translated = self._run_critical_stage(log, "coordinate_translator", lambda: self.translator.translate(combined_mapped))
        yield ("stage", log.records[-1])
        if translated is None:
            yield ("result", self._failed_result(log))
            return

        yield ("start", "entity_resolver")
        resolution = self._run_critical_stage(log, "entity_resolver", lambda: self.resolver.resolve(translated))
        yield ("stage", log.records[-1])
        if resolution is None:
            yield ("result", self._failed_result(log))
            return
        resolved, registry = resolution
        # Split back out by the same length boundary used going in -- only
        # the primary vendor's resolved events become the document's event
        # stream. The registry (passed to Validator whole, below) still
        # reflects corroboration from every file.
        primary_resolved = resolved[:primary_count]
        if len(vendor_raw_data) > 1:
            log.records[-1].detail = (
                f"{len(vendor_raw_data) - 1} secondary vendor(s) ({', '.join(v for v in vendor_raw_data if v != detections[0].detected_format)}) "
                "used to corroborate player identity, not counted as events"
            )

        yield ("start", "match_sheet_mapper")
        if match_sheet_data is not None:
            mapped_match_sheet = self._run_optional_stage(
                log, "match_sheet_mapper", lambda: self.mapper.map_match_sheet(match_sheet_data),
            )
        else:
            mapped_match_sheet = None
            log.record("match_sheet_mapper", "skipped", detail="no match-sheet payload supplied")
        yield ("stage", log.records[-1])

        yield ("start", "validator")
        validation = self._run_critical_stage(
            log, "validator",
            lambda: self.validator.build_document(
                primary_resolved, registry, vendor_raw_data, match_date=match_date,
                mapped_match_sheet=mapped_match_sheet,
            ),
        )
        yield ("stage", log.records[-1])
        if validation is None:
            yield ("result", self._failed_result(log))
            return
        document, validation_report = validation
        document.metadata.orchestration_log_ref = run_id
        if len(vendor_raw_data) > 1:
            document.metadata.warnings.append(
                f"multi-vendor combine: events sourced from '{detections[0].detected_format}' only "
                f"(primary); {', '.join(v for v in vendor_raw_data if v != detections[0].detected_format)} "
                "used only for player-identity corroboration and match-info/lineup fallback, not counted "
                "as events, to avoid double-counting the same real-world match twice"
            )

        yield ("start", "narrative_generator")
        narrative = self._run_optional_stage(log, "narrative_generator", lambda: self.narrator.generate(document))
        yield ("stage", log.records[-1])

        failed_optional = [r for r in log.records if r.stage in OPTIONAL_STAGES and r.status == "failed"]
        log.overall_status = "partial" if failed_optional else "success"

        yield ("result", PipelineResult(document=document, narrative=narrative, orchestration_log=log))

    def export(self, document: CDFDocument, target_vendor: str) -> tuple[dict, ExportReport]:
        """On-demand only (Agent 7) -- never called from run()."""
        return self.exporter.export(document, target_vendor)

    def _run_critical_stage(self, log: OrchestrationLog, name: str, fn: Callable[[], T]) -> Optional[T]:
        """Shared by both critical and optional stages -- the one-retry
        behavior and logging are identical either way. What differs is
        what the CALLER does with a None result: abort the run (critical)
        or skip and continue (optional). Named for the critical path since
        that's the stricter caller; _run_optional_stage is an alias."""
        try:
            result = fn()
            log.record(name, "success")
            return result
        except Exception as first_exc:
            try:
                result = fn()
                log.record(name, "success", detail=f"succeeded on retry after: {first_exc}", retried=True)
                return result
            except Exception as retry_exc:
                log.record(name, "failed", detail=f"{type(retry_exc).__name__}: {retry_exc}", retried=True)
                return None

    _run_optional_stage = _run_critical_stage

    def _failed_result(self, log: OrchestrationLog) -> PipelineResult:
        log.overall_status = "failed"
        return PipelineResult(document=None, narrative=None, orchestration_log=log)


if __name__ == "__main__":
    import json
    from pathlib import Path

    orchestrator = MasterOrchestrator(ai_escalation_enabled=True)
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Master Orchestrator -- test run (single-vendor ingestion, Opta only)")
    print("=" * 78)

    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        opta_file = match_dir / "opta.json"
        if not opta_file.exists():
            continue
        vendor_data = json.loads(opta_file.read_text())
        result = orchestrator.run(vendor_data)

        print(f"\n{match_dir.name}: overall_status={result.orchestration_log.overall_status} "
              f"(run_id={result.orchestration_log.run_id})")
        for r in result.orchestration_log.records:
            retry_note = " (retried)" if r.retried else ""
            print(f"  [{r.status:7s}] {r.stage}{retry_note}" + (f" -- {r.detail}" if r.detail else ""))

        if result.document:
            print(f"  events: {len(result.document.events)} | quality_confidence: {result.document.metadata.quality_confidence}")
            print(f"  orchestration_log_ref on document: {result.document.metadata.orchestration_log_ref}")
        if result.narrative:
            print(f"  narrative: {result.narrative.narrative_text[:120]}...")

        # Exercise export() as an on-demand call
        if result.document:
            payload, export_report = orchestrator.export(result.document, "statsbomb")
            print(f"  export->statsbomb: {export_report.events_exported} events")
