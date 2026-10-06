"""
Master Orchestrator

Wires Agents 1-9 into one entry point and owns the TOP-LEVEL error handling
across stages -- distinct from the per-field retry/fallback logic each
agent already owns internally. Every agent in this pipeline already
degrades gracefully on bad DATA (a missing field, an unknown vendor, a
name conflict with no API key) -- that is handled inside each agent and
never raises. What this orchestrator catches is an agent-level failure:
a genuine bug, an unexpected input shape, a dependency problem -- the
kind of thing no amount of per-field fallback logic prevents.

Stage criticality, per the PRD's "ingest/interpret" vs. "explain/render/
personalize" split:
  CRITICAL stages (1-5: Format Detective -> Schema Mapper -> Coordinate
  Translator -> Entity Resolver -> Validator) produce the CDFDocument
  itself. If any of these fails, there is no usable output at all, so the
  run aborts immediately and returns a failed PipelineResult with the
  reason logged -- there's nothing honest to fall back to.

  OPTIONAL stages (6/8/9: Narrative Generator, Explainability Engine,
  Overlay Renderer) are presentation layers built ON TOP of a valid
  CDFDocument. If one of these fails unexpectedly, the run does NOT abort
  -- it logs the failure, skips that stage (and anything downstream of it
  that depends on its output), and still returns everything that DID
  succeed. A club getting normalized match data with no narrative text is
  in a far better position than getting nothing at all.

Each stage gets exactly one retry before being treated as failed. This
matters most for the optional Claude-escalation calls nested inside
Entity Resolver/Narrative Generator, which already catch their own
network/API exceptions internally and never raise -- so in practice a
retry here mostly guards against transient, non-data-quality problems
(an import hiccup, a resource blip), which is a legitimate thing to guard
against even in a fully deterministic pipeline.

Format Exporter (Agent 7) and Personalization Engine (Agent 10) are
deliberately NOT part of run() -- per the locked MVP decision, export is
on-demand only, and personalization is inherently per-viewer-profile, not
a pipeline stage that happens once per match. Both are exposed as
separate methods on this class instead.

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
from explainability_engine import ExplainabilityEngine, ExplainabilityReport
from overlay_renderer import OverlayRenderer, OverlayTimeline
from format_exporter import FormatExporter, ExportReport
from personalization_engine import PersonalizationEngine, PersonalizationProfile, PersonalizedExperience
from cdf_models import CDFDocument

T = TypeVar("T")

CRITICAL_STAGES = ("format_detective", "schema_mapper", "coordinate_translator", "entity_resolver", "validator")
OPTIONAL_STAGES = ("narrative_generator", "explainability_engine", "overlay_renderer")


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
    explainability: Optional[ExplainabilityReport]
    timeline: Optional[OverlayTimeline]
    orchestration_log: OrchestrationLog


class MasterOrchestrator:
    """Wires Agents 1-9 into one run() call. export() and personalize() are
    separate, on-demand entry points per the locked MVP decision."""

    def __init__(self, ai_escalation_enabled: bool = True):
        self.detective = FormatDetective()
        self.mapper = SchemaMapper()
        self.translator = CoordinateTranslator()
        self.resolver = EntityResolver(ai_escalation_enabled=ai_escalation_enabled)
        self.validator = Validator()
        self.narrator = NarrativeGenerator(ai_enabled=ai_escalation_enabled)
        self.explainer = ExplainabilityEngine()
        self.renderer = OverlayRenderer()
        self.exporter = FormatExporter()
        self.personalizer = PersonalizationEngine()

    def run(self, vendor_data: dict, match_date: Optional[date_cls] = None) -> PipelineResult:
        run_id = uuid.uuid4().hex[:12]
        log = OrchestrationLog(run_id=run_id)

        detection = self._run_critical_stage(log, "format_detective", lambda: self.detective.detect(vendor_data))
        if detection is None:
            return self._failed_result(log)

        mapped = self._run_critical_stage(
            log, "schema_mapper",
            lambda: self.mapper.map_events(vendor_data, detection.detected_format, detection.event_list_key),
        )
        if mapped is None:
            return self._failed_result(log)

        translated = self._run_critical_stage(log, "coordinate_translator", lambda: self.translator.translate(mapped))
        if translated is None:
            return self._failed_result(log)

        resolution = self._run_critical_stage(log, "entity_resolver", lambda: self.resolver.resolve(translated))
        if resolution is None:
            return self._failed_result(log)
        resolved, registry = resolution

        validation = self._run_critical_stage(
            log, "validator",
            lambda: self.validator.build_document(
                resolved, registry, {detection.detected_format: vendor_data}, match_date=match_date,
            ),
        )
        if validation is None:
            return self._failed_result(log)
        document, validation_report = validation
        document.metadata.orchestration_log_ref = run_id

        narrative = self._run_optional_stage(log, "narrative_generator", lambda: self.narrator.generate(document))
        explainability = self._run_optional_stage(log, "explainability_engine", lambda: self.explainer.generate(document))

        timeline = None
        if narrative is not None and explainability is not None:
            timeline = self._run_optional_stage(
                log, "overlay_renderer", lambda: self.renderer.render(document, narrative, explainability),
            )
        else:
            log.record("overlay_renderer", "skipped", "requires both narrative_generator and explainability_engine output")

        failed_or_skipped = [r for r in log.records if r.stage in OPTIONAL_STAGES and r.status != "success"]
        log.overall_status = "partial" if failed_or_skipped else "success"

        return PipelineResult(document=document, narrative=narrative, explainability=explainability,
                               timeline=timeline, orchestration_log=log)

    def export(self, document: CDFDocument, target_vendor: str) -> tuple[dict, ExportReport]:
        """On-demand only (Agent 7) -- never called from run()."""
        return self.exporter.export(document, target_vendor)

    def personalize(self, result: PipelineResult, profile: PersonalizationProfile) -> Optional[PersonalizedExperience]:
        """On-demand, per-viewer (Agent 10) -- never called from run().
        Returns None (not a crash) if the run didn't produce the optional
        stages this needs; the caller is told exactly why via the log."""
        if result.narrative is None or result.explainability is None or result.timeline is None:
            return None
        return self.personalizer.personalize(result.document, result.narrative, result.explainability,
                                              result.timeline, profile)

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
        return PipelineResult(document=None, narrative=None, explainability=None, timeline=None, orchestration_log=log)


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

        # Exercise export() and personalize() as on-demand calls
        if result.document:
            payload, export_report = orchestrator.export(result.document, "statsbomb")
            print(f"  export->statsbomb: {export_report.events_exported} events")

        if result.narrative and result.explainability and result.timeline:
            profile = PersonalizationProfile(mode="casual_fan", language="es")
            experience = orchestrator.personalize(result, profile)
            print(f"  personalized (casual_fan/es): {experience.headline}")
