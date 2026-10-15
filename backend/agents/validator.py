"""
Agent 5: Validator

The last stop before a CDFDocument leaves the pipeline. Takes Entity
Resolver's output (ResolvedEvents + a player registry) for one match,
pulls in whatever match-level metadata the raw vendor files carried, and
assembles + validates the final CDFDocument against the Pydantic schema
that is the actual runtime contract (backend/data/schemas/cdf_models.py).

This agent owns two different kinds of "validation":
  1. Schema validation -- the assembled document must satisfy CDFDocument,
     full stop. Anything that can't be made to satisfy it (after repair
     attempts below) is dropped from the output, never silently coerced
     into something the schema wouldn't actually accept.
  2. Data-quality repair -- upstream agents can hand this agent events with
     fields the schema requires but the source data didn't reliably supply
     (period is the recurring one: Opta/StatsBomb messy-tier data can be
     missing it entirely). This agent tries to recover those honestly
     before falling back to a documented default, exactly like Agents 1-4.

Per PRD Section 4.1 fallback logic for period (the one field that
regularly needs repair in this dataset):
  Attempt 1: use the period value Schema Mapper produced, if it's a valid
             1-4 int
  Attempt 2: infer it from the event's own timestamp (first 45 minutes of
             match time -> period 1, else period 2) -- this mirrors the
             exact rule the synthetic generator itself uses to assign
             periods, so it's a real recovery, not a guess dressed up as one
  Fallback:  default to period 1, confidence penalty, logged as a repair,
             never silently passed through

x/y are NOT re-validated here beyond a defensive range check -- Agent 3
(Coordinate Translator) owns that invariant. If a coordinate somehow still
violates it, that's a bug upstream, not data this agent should quietly
patch, so the event is dropped with a log entry pointing at the real cause.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional

from pydantic import ValidationError

from entity_resolver import ResolvedEvent, PlayerIdentity
from schema_mapper import MappedMatchSheet
from cdf_models import (
    CDFDocument, CDFEvent, CDFMetadata, MatchInfo, PlayerRecord,
    LineupEntry, MatchOfficials, MatchSheet, SubstitutionRecord, TeamSheet,
    CardType, ShotOutcome,
)


# --- Match-level metadata extraction (per-vendor, same registry pattern as
# Format Detective / Schema Mapper) ---

def _extract_opta_match(raw: dict) -> tuple[Optional[str], Optional[str], Optional[str], list[str]]:
    log = []
    match_id = raw.get("MatchId")
    home = raw.get("HomeTeam")
    away = raw.get("AwayTeam")
    if match_id is None:
        log.append("opta: MatchId missing from top-level match data")
    return match_id, home, away, log


def _extract_statsbomb_match(raw: dict) -> tuple[Optional[str], Optional[str], Optional[str], list[str]]:
    log = []
    match_id = raw.get("match_id")
    home = (raw.get("home_team") or {}).get("name")
    away = (raw.get("away_team") or {}).get("name")
    if match_id is None:
        log.append("statsbomb: match_id missing from top-level match data")
    return match_id, home, away, log


def _extract_wyscout_match(raw: dict) -> tuple[Optional[str], Optional[str], Optional[str], list[str]]:
    log = []
    match_id = raw.get("wyId")
    label = raw.get("label")
    home = away = None
    if isinstance(label, str) and " - " in label:
        home, away = (part.strip() for part in label.split(" - ", 1))
    else:
        log.append(f"wyscout: could not split label '{label}' into home/away teams")
    if match_id is None:
        log.append("wyscout: wyId missing from top-level match data")
    return match_id, home, away, log


MATCH_INFO_EXTRACTORS: dict[str, Callable] = {
    "opta": _extract_opta_match,
    "statsbomb": _extract_statsbomb_match,
    "wyscout": _extract_wyscout_match,
}


def _resolve_match_info(vendor_raw_data: dict[str, dict], match_date: Optional[date_cls]) -> tuple[MatchInfo, list[str]]:
    """Attempt 1: any known vendor's extractor. Attempt 2: cross-check a
    second vendor if the first left gaps. Fallback: 'unknown' placeholders,
    confidence-affecting warning, never a crash."""
    log: list[str] = []
    match_id = home_team = away_team = None

    for vendor, raw in vendor_raw_data.items():
        extractor = MATCH_INFO_EXTRACTORS.get(vendor)
        if extractor is None:
            continue
        mid, home, away, extractor_log = extractor(raw)
        log.extend(extractor_log)
        match_id = match_id or mid
        home_team = home_team or home
        away_team = away_team or away
        if match_id and home_team and away_team:
            break

    if match_id is None:
        match_id = "unknown_match"
        log.append("fallback: no vendor supplied a usable match_id, defaulted to 'unknown_match'")
    if home_team is None:
        home_team = "unknown_home_team"
        log.append("fallback: no vendor supplied a usable home_team")
    if away_team is None:
        away_team = "unknown_away_team"
        log.append("fallback: no vendor supplied a usable away_team")

    if match_date is None:
        match_date = datetime.now(timezone.utc).date()
        log.append(
            "match_date not supplied by caller and no vendor file in this dataset carries one; "
            "defaulted to today's date -- NOT sourced from vendor data"
        )

    return MatchInfo(match_id=match_id, date=match_date, home_team=home_team, away_team=away_team), log


# --- Per-event period repair ---

_CARD_TYPES = {c.value for c in CardType}
_SHOT_OUTCOMES = {o.value for o in ShotOutcome}


def _repair_period(period: Optional[int], timestamp: float, log: list[str]) -> tuple[int, float]:
    """Returns (period, confidence_penalty)."""
    if isinstance(period, int) and 1 <= period <= 4:
        return period, 0.0

    # Attempt 2: infer from timestamp using the same 45-minute boundary rule
    # the generator itself uses to assign periods.
    inferred = 1 if timestamp < 2700 else 2
    log.append(f"period missing/invalid (was {period!r}); inferred period={inferred} from timestamp={timestamp}")
    return inferred, 0.1


# --- Match-sheet assembly (lineups, formations, subs, officials, venue) ---
#
# Deliberately NOT run through Entity Resolver: cross-vendor player-identity
# reconciliation (Agent 4's job) exists because the same event stream can be
# fed from more than one vendor over time. A match sheet is one coherent
# document from one vendor describing one team's own squad -- there's
# nothing to cross-reconcile, so lineup player_ids are taken straight from
# Schema Mapper's output (vendor id when present, a stable placeholder when
# not), matching the same honest-fallback spirit as everything else here.

def _resolve_substitution_ref(ref: dict, name_to_id: dict[str, str], vendor_to_id: dict[str, str]) -> Optional[str]:
    if not ref:
        return None
    name = ref.get("name")
    if name and name in name_to_id:
        return name_to_id[name]
    vendor_id = ref.get("vendor_id")
    if vendor_id is not None and str(vendor_id) in vendor_to_id:
        return vendor_to_id[str(vendor_id)]
    return None


def _build_match_sheet(mapped: Optional[MappedMatchSheet], match_info: MatchInfo) -> tuple[Optional[MatchSheet], list[str]]:
    """Attempt: assemble Schema Mapper's MappedMatchSheet into the schema's
    MatchSheet. Fallback: no match-sheet payload was supplied, or the one
    supplied carried no usable lineup data -- document.match_sheet stays
    None (never fabricated), same honest-degradation rule as everything
    else in this pipeline."""
    if mapped is None:
        return None, []

    log = list(mapped.mapping_log)
    if not mapped.home_lineup and not mapped.away_lineup:
        log.append("match sheet unavailable: no lineup data could be mapped from the supplied payload")
        return None, log

    home_team_name = mapped.home_team or match_info.home_team
    away_team_name = mapped.away_team or match_info.away_team

    name_to_id: dict[str, str] = {}
    vendor_to_id: dict[str, str] = {}

    def build_lineup(entries, label: str) -> list[LineupEntry]:
        out = []
        for i, e in enumerate(entries):
            vendor_id = (e.raw_player_ref or {}).get("vendor_id")
            player_id = str(vendor_id) if vendor_id is not None else f"{label}_lineup_{i}"
            if e.name:
                name_to_id[e.name] = player_id
            if vendor_id is not None:
                vendor_to_id[str(vendor_id)] = player_id
            out.append(LineupEntry(
                player_id=player_id, name=e.name, jersey_number=e.jersey_number,
                position=e.position, is_starter=e.is_starter, is_captain=e.is_captain,
            ))
        return out

    home_lineup = build_lineup(mapped.home_lineup, "home")
    away_lineup = build_lineup(mapped.away_lineup, "away")

    substitution_records: list[SubstitutionRecord] = []
    for s in mapped.substitutions:
        out_id = _resolve_substitution_ref(s.player_out_ref, name_to_id, vendor_to_id)
        in_id = _resolve_substitution_ref(s.player_in_ref, name_to_id, vendor_to_id)
        if out_id is None or in_id is None or s.minute is None:
            log.append(f"substitution dropped: could not resolve player(s) or minute (team={s.team!r})")
            continue
        team_name = s.team or home_team_name
        try:
            substitution_records.append(SubstitutionRecord(
                team=team_name, player_out_id=out_id, player_in_id=in_id,
                minute=int(s.minute), period=s.period or 2, reason=s.reason,
            ))
        except ValidationError as exc:
            log.append(f"substitution dropped: failed SubstitutionRecord validation ({exc})")

    home_sheet = TeamSheet(
        team=home_team_name, formation=mapped.home_formation,
        starters=[p for p in home_lineup if p.is_starter],
        substitutes=[p for p in home_lineup if not p.is_starter],
        substitutions=[s for s in substitution_records if s.team == home_team_name],
    )
    away_sheet = TeamSheet(
        team=away_team_name, formation=mapped.away_formation,
        starters=[p for p in away_lineup if p.is_starter],
        substitutes=[p for p in away_lineup if not p.is_starter],
        substitutions=[s for s in substitution_records if s.team == away_team_name],
    )

    officials = None
    if any([mapped.referee, mapped.assistant_referees, mapped.fourth_official, mapped.var_official]):
        officials = MatchOfficials(
            referee=mapped.referee, assistant_referees=mapped.assistant_referees,
            fourth_official=mapped.fourth_official, var_official=mapped.var_official,
        )

    kickoff_dt = None
    if mapped.kickoff_time and match_info.date:
        try:
            hour, minute = mapped.kickoff_time.split(":")
            kickoff_dt = datetime(match_info.date.year, match_info.date.month, match_info.date.day,
                                   int(hour), int(minute), tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            log.append(f"could not parse kickoff_time '{mapped.kickoff_time}'")

    try:
        sheet = MatchSheet(
            home=home_sheet, away=away_sheet, officials=officials,
            kickoff=kickoff_dt, attendance=mapped.attendance, weather=mapped.weather,
        )
    except ValidationError as exc:
        log.append(f"match sheet dropped: failed MatchSheet validation ({exc})")
        return None, log

    return sheet, log


@dataclass
class ValidationReport:
    events_in: int = 0
    events_validated: int = 0
    events_dropped: int = 0
    periods_repaired: int = 0
    players_in_document: int = 0
    warnings: list[str] = field(default_factory=list)


class Validator:
    """Agent 5. Assembles and validates the final CDFDocument for one match
    from Entity Resolver's output. Does not mutate its inputs."""

    def build_document(
        self,
        resolved_events: list[ResolvedEvent],
        registry: dict[str, PlayerIdentity],
        vendor_raw_data: dict[str, dict],
        match_date: Optional[date_cls] = None,
        mapped_match_sheet: Optional[MappedMatchSheet] = None,
    ) -> tuple[CDFDocument, ValidationReport]:
        report = ValidationReport(events_in=len(resolved_events))

        match_info, match_log = _resolve_match_info(vendor_raw_data, match_date)
        report.warnings.extend(match_log)

        match_sheet, match_sheet_log = _build_match_sheet(mapped_match_sheet, match_info)
        report.warnings.extend(match_sheet_log)

        players = self._build_players(resolved_events, registry)
        report.players_in_document = len(players)
        conflicted_players = sum(1 for p in players if p.resolution_confidence < 0.7)
        if conflicted_players:
            report.warnings.append(
                f"{conflicted_players} player(s) have resolution_confidence below 0.7 "
                "(unresolved cross-vendor name conflicts or vendor-scoped fallback identities)"
            )

        events: list[CDFEvent] = []
        for r in resolved_events:
            cdf_event, event_warning = self._build_event(r, report)
            if cdf_event is None:
                report.events_dropped += 1
                if event_warning:
                    report.warnings.append(event_warning)
                continue
            events.append(cdf_event)
            report.events_validated += 1

        source_vendors = sorted({r.event.vendor_source for r in resolved_events})
        if events:
            avg_confidence = round(sum(e.quality_confidence for e in events) / len(events), 4)
        else:
            avg_confidence = 0.0
            report.warnings.append("no events survived validation -- document contains zero events")

        if report.events_dropped:
            report.warnings.append(f"{report.events_dropped} of {report.events_in} events were dropped during validation")
        if report.periods_repaired:
            report.warnings.append(f"{report.periods_repaired} event(s) needed period repair (missing/invalid in source)")

        metadata = CDFMetadata(
            source_vendors=source_vendors,
            quality_confidence=avg_confidence,
            generation_timestamp=datetime.now(timezone.utc),
            warnings=list(report.warnings),
        )

        try:
            document = CDFDocument(
                match=match_info, metadata=metadata, players=players, events=events, match_sheet=match_sheet,
            )
        except ValidationError as exc:
            # Everything above is supposed to make this unreachable -- if it
            # fires, that's a real bug in this agent's repair logic, not a
            # data-quality issue to swallow. Surface it clearly.
            raise RuntimeError(f"Validator produced a document that fails CDFDocument validation: {exc}") from exc

        return document, report

    def _build_players(self, resolved_events: list[ResolvedEvent], registry: dict[str, PlayerIdentity]) -> list[PlayerRecord]:
        records: list[PlayerRecord] = []
        seen_ids: set[str] = set()

        for identity in registry.values():
            name = next(iter(identity.names_seen.values()), None)
            vendor_ids = {vendor: str(next(iter(ids))) for vendor, ids in identity.vendor_refs.items() if ids}
            method_attempts = 1 if identity.resolution_method == "deterministic_jersey_team" else 2
            records.append(PlayerRecord(
                player_id=identity.canonical_id,
                name=name,
                jersey_number=identity.jersey_number,
                team=identity.team,
                vendor_ids=vendor_ids,
                resolution_confidence=identity.confidence,
                resolution_attempts=method_attempts,
            ))
            seen_ids.add(identity.canonical_id)

        # Vendor-scoped fallback identities never entered the registry
        # (they're explicitly not a cross-vendor cluster) -- still need a
        # PlayerRecord each so events can legally reference player_id.
        for r in resolved_events:
            pid = r.canonical_player_id
            if pid in seen_ids:
                continue
            ref = r.event.raw_player_ref
            records.append(PlayerRecord(
                player_id=pid,
                name=ref.get("name"),
                jersey_number=ref.get("jersey_number"),
                team=r.event.team,
                vendor_ids={r.event.vendor_source: str(ref.get("vendor_id"))} if ref.get("vendor_id") is not None else {},
                resolution_confidence=r.resolution_confidence,
                resolution_attempts=2,
            ))
            seen_ids.add(pid)

        return records

    def _build_event(self, r: ResolvedEvent, report: ValidationReport) -> tuple[Optional[CDFEvent], Optional[str]]:
        e = r.event
        log: list[str] = []

        if e.x is None or e.y is None:
            return None, (
                f"event {e.event_id} dropped: missing coordinates after Coordinate Translator -- "
                "this should never happen and indicates an upstream bug, not a data-quality gap"
            )
        if not (0.0 <= e.x <= 100.0 and 0.0 <= e.y <= 100.0):
            return None, f"event {e.event_id} dropped: coordinates out of [0,100] range post-translation (x={e.x}, y={e.y})"

        period, period_penalty = _repair_period(e.period, e.timestamp, log)
        if period_penalty > 0:
            report.periods_repaired += 1

        # Detail fields: only CDF vocabulary passes; anything else is dropped
        # to None with a log line rather than failing the whole event.
        card_type = e.card_type if e.card_type in _CARD_TYPES else None
        if e.card_type is not None and card_type is None:
            log.append(f"card_type {e.card_type!r} is not CDF vocabulary; left unset")
        shot_outcome = e.shot_outcome if e.shot_outcome in _SHOT_OUTCOMES else None
        if e.shot_outcome is not None and shot_outcome is None:
            log.append(f"shot_outcome {e.shot_outcome!r} is not CDF vocabulary; left unset")

        confidence = round(min(e.mapping_confidence, r.resolution_confidence) - period_penalty, 3)
        confidence = max(0.0, confidence)

        metadata = dict(e.raw_metadata)
        metadata["mapping_log"] = e.mapping_log
        metadata["resolution_log"] = r.resolution_log
        metadata["resolution_method"] = r.resolution_method
        metadata["event_type_raw"] = e.event_type_raw  # preserved for lossless export-back (Agent 7)
        if log:
            metadata["validator_log"] = log

        try:
            cdf_event = CDFEvent(
                event_id=e.event_id,
                event_type=e.event_type,
                timestamp=max(0.0, e.timestamp),
                period=period,
                player_id=r.canonical_player_id,
                team=e.team,
                x=e.x, y=e.y, end_x=e.end_x, end_y=e.end_y,
                outcome=e.outcome,
                card_type=card_type,
                shot_outcome=shot_outcome,
                quality_confidence=confidence,
                vendor_source=e.vendor_source,
                metadata=metadata,
            )
        except ValidationError as exc:
            return None, f"event {e.event_id} dropped: failed CDFEvent validation even after repair attempts: {exc}"

        return cdf_event, None


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, "backend/data/schemas")
    from format_detective import FormatDetective
    from schema_mapper import SchemaMapper
    from coordinate_translator import CoordinateTranslator
    from entity_resolver import EntityResolver

    detective = FormatDetective()
    mapper = SchemaMapper()
    translator = CoordinateTranslator()
    resolver = EntityResolver()
    validator = Validator()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Validator -- test run (full pipeline, Detective through Validator)")
    print("=" * 78)

    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        all_events = []
        vendor_raw_data = {}
        for vendor_file in sorted(match_dir.glob("*.json")):
            data = json.loads(vendor_file.read_text())
            detection = detective.detect(data)
            vendor_raw_data[detection.detected_format] = data
            mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
            translated = translator.translate(mapped)
            all_events.extend(translated)

        resolved, registry = resolver.resolve(all_events)
        document, report = validator.build_document(resolved, registry, vendor_raw_data)

        print(f"\n{match_dir.name}")
        print(f"  match: {document.match.home_team} vs {document.match.away_team} ({document.match.match_id})")
        print(f"  events: in={report.events_in} validated={report.events_validated} dropped={report.events_dropped}")
        print(f"  periods repaired: {report.periods_repaired} | players: {report.players_in_document}")
        print(f"  overall quality_confidence: {document.metadata.quality_confidence}")
        print(f"  confidence_summary: {document.confidence_summary()}")
        if report.warnings:
            print(f"  warnings:")
            for w in report.warnings:
                print(f"    - {w}")
