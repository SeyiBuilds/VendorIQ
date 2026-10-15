"""
Agent 7: Format Exporter

The export-back half of the vendor-lock-in argument: a club that adopts
CDF internally shouldn't be stuck feeding it to a downstream tool (a
broadcaster graphics package, a legacy analytics dashboard) that only
understands Opta's shape. This agent takes a validated CDFDocument and
converts it BACK into one vendor's native format.

Per the PRD decision (Section: Core Components, Agent 7 / MVP scope), this
is explicitly ON-DEMAND ONLY -- never run automatically as part of the main
pipeline. Nothing upstream calls this; something (the API layer, a CLI
flag, a user action) has to ask for a specific target vendor.

Round-trip honesty: this is a best-effort, DOCUMENTED-LOSSY export, not a
guaranteed bit-for-bit reconstruction of the original file. What's
recoverable:
  - coordinates: inverse of Agent 3's scaling, exact for the three MVP
    vendors since their native pitch dimensions are known
  - event_type: inverse of Agent 2's canonicalization, using the vendor's
    own original label when Validator preserved it (metadata.event_type_raw)
    for an event that originated from THIS vendor; best-effort canonical
    label guess otherwise
  - player identity: the target vendor's own id/jersey/name, when that
    player's PlayerRecord carries a vendor_ids entry for this vendor
    (meaning this player was actually seen in that vendor's feed somewhere
    in this match); a synthesized placeholder, clearly marked, otherwise
  - vendor-specific leftover fields: round-tripped from CDFEvent.metadata's
    preserved raw_metadata keys when they originated from this vendor

What's NOT recoverable and never faked: an event that only ever existed in
a different vendor's feed has no original native id for Opta, say -- this
agent fabricates a plausible-shaped placeholder, flags it in the export
report, and never passes that placeholder off as the real vendor ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from cdf_models import CDFDocument, CDFEvent, MatchSheet, TeamSheet

# Mirrors coordinate_translator.VENDOR_PITCH_DIMENSIONS -- duplicated
# deliberately rather than imported, so this agent's coordinate contract
# doesn't silently drift if Agent 3's dict ever gets vendor-specific
# normalization quirks added that don't apply symmetrically on the way out.
VENDOR_PITCH_DIMENSIONS: dict[str, tuple[float, float]] = {
    "opta": (120.0, 80.0),
    "statsbomb": (100.0, 100.0),
    "wyscout": (100.0, 100.0),
}

# Reverse of Schema Mapper's canonicalization. Picks ONE representative
# native label per canonical type per vendor -- used only when the event's
# own vendor-native label (event_type_raw) isn't available, i.e. the event
# originated from a different vendor than the export target.
CANONICAL_TO_NATIVE_TYPE: dict[str, dict[str, str]] = {
    "opta": {
        "pass": "PASS", "shot": "SHOT", "tackle": "TACKLE", "foul": "FOUL",
        "card": "CARD", "substitution": "SUBSTITUTION", "corner": "CORNER",
        "throw_in": "THROWIN", "goal_kick": "GOALKICK", "offside": "OFFSIDE",
        "goal": "GOAL", "save": "SAVE", "interception": "INTERCEPTION",
        "clearance": "CLEARANCE", "dribble": "DRIBBLE", "other": "OTHER",
    },
    "statsbomb": {
        "pass": "Pass", "shot": "Shot", "tackle": "Tackle", "foul": "Foul Committed",
        "card": "Card", "substitution": "Substitution", "corner": "Corner",
        "throw_in": "Throw In", "goal_kick": "Goal Kick", "offside": "Offside",
        "goal": "Goal", "save": "Save", "interception": "Interception",
        "clearance": "Clearance", "dribble": "Dribble", "other": "Other",
    },
    "wyscout": {
        "pass": "Pass", "shot": "Shot", "tackle": "Tackle", "foul": "Foul",
        "card": "Card", "substitution": "Substitution", "corner": "Corner",
        "throw_in": "Throw in", "goal_kick": "Goal kick", "offside": "Offside",
        "goal": "Goal", "save": "Save", "interception": "Interception",
        "clearance": "Clearance", "dribble": "Dribble", "other": "Other",
    },
}


@dataclass
class ExportReport:
    target_vendor: str
    events_exported: int = 0
    events_with_native_type_recovered: int = 0  # used event_type_raw (same-vendor origin)
    events_with_guessed_type: int = 0            # used CANONICAL_TO_NATIVE_TYPE fallback
    players_with_native_id: int = 0
    players_with_synthesized_id: int = 0
    warnings: list[str] = field(default_factory=list)


def _scale_to_native(value: Optional[float], native_max: float) -> Optional[float]:
    if value is None:
        return None
    return round((value / 100.0) * native_max, 2)


def _player_ref_for_export(document: CDFDocument, player_id: Optional[str], target_vendor: str,
                            report: ExportReport) -> dict:
    """Returns the best available player reference for the target vendor.
    Never claims a synthesized id is a real vendor id."""
    if player_id is None:
        return {"vendor_id": None, "jersey_number": None, "name": None, "synthesized": False}

    player = next((p for p in document.players if p.player_id == player_id), None)
    if player is None:
        report.warnings.append(f"player_id {player_id} referenced by an event but not found in players[] -- synthesizing placeholder")
        report.players_with_synthesized_id += 1
        return {"vendor_id": f"synth_{player_id}", "jersey_number": None, "name": None, "synthesized": True}

    native_id = player.vendor_ids.get(target_vendor)
    if native_id is not None:
        report.players_with_native_id += 1
        return {"vendor_id": native_id, "jersey_number": player.jersey_number, "name": player.name, "synthesized": False}

    report.warnings.append(
        f"player {player_id} ({player.name or 'unnamed'}) has no native {target_vendor} id on record "
        f"(never observed in a {target_vendor} feed for this match) -- synthesizing placeholder id"
    )
    report.players_with_synthesized_id += 1
    return {
        "vendor_id": f"synth_{player_id}", "jersey_number": player.jersey_number,
        "name": player.name, "synthesized": True,
    }


def _native_event_type(e: CDFEvent, target_vendor: str, report: ExportReport) -> str:
    raw = (e.metadata or {}).get("event_type_raw")
    if e.vendor_source == target_vendor and isinstance(raw, str) and raw:
        report.events_with_native_type_recovered += 1
        return raw
    report.events_with_guessed_type += 1
    return CANONICAL_TO_NATIVE_TYPE.get(target_vendor, {}).get(e.event_type, e.event_type.upper())


# Card colour and shot result, written back in each vendor's own encoding
# (the inverse of schema_mapper's vendor vocabularies).
OPTA_SHOT_TYPE_BY_OUTCOME = {
    "off_target": "MISS", "post": "POST", "saved": "ATTEMPT_SAVED", "blocked": "ATTEMPT_SAVED", "goal": "GOAL",
}
OPTA_CARD_QUALIFIER = {"yellow": 31, "second_yellow": 32, "red": 33}
OPTA_BLOCKED_QUALIFIER = 82
STATSBOMB_SHOT_OUTCOME_NAME = {
    "goal": "Goal", "saved": "Saved", "off_target": "Off T", "blocked": "Blocked", "post": "Post",
}
STATSBOMB_CARD_NAME = {"yellow": "Yellow Card", "second_yellow": "Second Yellow", "red": "Red Card"}
WYSCOUT_SHOT_TAGS = {
    "goal": [101, 1801], "saved": [1801], "off_target": [1802], "blocked": [2101, 1802], "post": [1802],
}
WYSCOUT_CARD_TAG = {"yellow": 1702, "second_yellow": 1703, "red": 1701}


def _value(enum_or_str) -> Optional[str]:
    return getattr(enum_or_str, "value", enum_or_str)


def _export_opta(document: CDFDocument, report: ExportReport) -> dict:
    native_max_x, native_max_y = VENDOR_PITCH_DIMENSIONS["opta"]
    events = []
    for e in document.events:
        ref = _player_ref_for_export(document, e.player_id, "opta", report)
        type_name = _native_event_type(e, "opta", report)
        shot_outcome, card_type = _value(e.shot_outcome), _value(e.card_type)
        qualifiers = []
        if e.event_type in ("shot", "goal") and shot_outcome in OPTA_SHOT_TYPE_BY_OUTCOME:
            type_name = OPTA_SHOT_TYPE_BY_OUTCOME[shot_outcome]
            if shot_outcome == "blocked":
                qualifiers.append({"QualifierId": OPTA_BLOCKED_QUALIFIER})
        if card_type in OPTA_CARD_QUALIFIER:
            qualifiers.append({"QualifierId": OPTA_CARD_QUALIFIER[card_type]})
        record = {
            "EventId": e.event_id,
            "TypeName": type_name,
            "Min": int(e.timestamp // 60),
            "Sec": round(e.timestamp % 60, 1),
            "PeriodId": e.period,
            "PlayerRef": ref["vendor_id"],
            "JerseyNumber": ref["jersey_number"],
            "TeamName": e.team,
            "X": _scale_to_native(e.x, native_max_x),
            "Y": _scale_to_native(e.y, native_max_y),
        }
        if qualifiers:
            record["Qualifiers"] = qualifiers
        events.append(record)
        report.events_exported += 1
    return {
        "MatchId": document.match.match_id,
        "HomeTeam": document.match.home_team,
        "AwayTeam": document.match.away_team,
        "Events": events,
    }


def _export_statsbomb(document: CDFDocument, report: ExportReport) -> dict:
    events = []
    for e in document.events:
        ref = _player_ref_for_export(document, e.player_id, "statsbomb", report)
        player_obj = None
        if ref["vendor_id"] is not None:
            player_obj = {"id": ref["vendor_id"], "name": ref["name"]}
            if ref["jersey_number"] is not None:
                player_obj["jersey_number"] = ref["jersey_number"]
        record = {
            "id": e.event_id,
            "type": {"name": _native_event_type(e, "statsbomb", report)},
            "timestamp": f"00:{int(e.timestamp // 60):02d}:{e.timestamp % 60:05.2f}",
            "period": e.period,
            "player": player_obj,
            "team": {"name": e.team},
            "location": [e.x, e.y],
        }
        shot_outcome, card_type = _value(e.shot_outcome), _value(e.card_type)
        if e.event_type in ("shot", "goal") and shot_outcome in STATSBOMB_SHOT_OUTCOME_NAME:
            # StatsBomb has no Goal type: a goal is a Shot with outcome "Goal".
            record["type"] = {"name": "Shot"}
            record["shot"] = {"outcome": {"name": STATSBOMB_SHOT_OUTCOME_NAME[shot_outcome]}}
        if card_type in STATSBOMB_CARD_NAME:
            card = {"card": {"name": STATSBOMB_CARD_NAME[card_type]}}
            if e.event_type == "foul":
                record["type"] = {"name": "Foul Committed"}
                record["foul_committed"] = card
            else:
                record["type"] = {"name": "Bad Behaviour"}
                record["bad_behaviour"] = card
        events.append(record)
        report.events_exported += 1
    return {
        "match_id": document.match.match_id,
        "home_team": {"name": document.match.home_team},
        "away_team": {"name": document.match.away_team},
        "events": events,
    }


def _export_wyscout(document: CDFDocument, report: ExportReport) -> dict:
    events = []
    for e in document.events:
        ref = _player_ref_for_export(document, e.player_id, "wyscout", report)
        record = {
            "eventId": e.event_id,
            "subEventName": _native_event_type(e, "wyscout", report),
            "matchPeriod": f"{e.period}H",
            "eventSec": e.timestamp,
            "trackable_object": ref["vendor_id"],
            "jerseyNumber": ref["jersey_number"],
            "teamName": e.team,
            "positions": [{"x": e.x, "y": e.y}],
            "accurate": e.outcome,
        }
        shot_outcome, card_type = _value(e.shot_outcome), _value(e.card_type)
        tags = []
        if e.event_type in ("shot", "goal") and shot_outcome in WYSCOUT_SHOT_TAGS:
            # Wyscout records a goal as a Shot tagged 101.
            record["subEventName"] = "Shot"
            tags.extend(WYSCOUT_SHOT_TAGS[shot_outcome])
        if card_type in WYSCOUT_CARD_TAG:
            tags.append(WYSCOUT_CARD_TAG[card_type])
        if tags:
            record["tags"] = [{"id": t} for t in tags]
        events.append(record)
        report.events_exported += 1
    return {
        "wyId": document.match.match_id,
        "label": f"{document.match.home_team} - {document.match.away_team}",
        "events": events,
    }


VENDOR_EXPORTERS: dict[str, Callable] = {
    "opta": _export_opta,
    "statsbomb": _export_statsbomb,
    "wyscout": _export_wyscout,
}


# ---------------------------------------------------------------------------
# Match-sheet export-back -- same on-demand, documented-lossy philosophy as
# the event exporters above, kept as separate functions since the shapes
# don't overlap at all (see schema_mapper's match-sheet mapping doc note).
# ---------------------------------------------------------------------------

def _export_team_sheet_opta(team: TeamSheet) -> dict:
    players = [
        {"PlayerRef": p.player_id, "PlayerName": p.name, "ShirtNumber": p.jersey_number,
         "Position": p.position, "Status": "Start" if p.is_starter else "Sub", "Captain": p.is_captain}
        for p in [*team.starters, *team.substitutes]
    ]
    return {"TeamName": team.team, "Formation": team.formation, "MatchPlayer": players}


def _export_match_sheet_opta(sheet: MatchSheet) -> dict:
    subs = [
        {"TeamName": s.team, "PlayerOffRef": s.player_out_id, "PlayerOnRef": s.player_in_id,
         "Min": s.minute, "PeriodId": s.period}
        for s in [*sheet.home.substitutions, *sheet.away.substitutions]
    ]
    officials = []
    if sheet.officials:
        officials = [
            {"Role": "Main", "Name": sheet.officials.referee},
            *[{"Role": f"Assistant{i+1}", "Name": a} for i, a in enumerate(sheet.officials.assistant_referees)],
            {"Role": "Fourth", "Name": sheet.officials.fourth_official},
            {"Role": "VAR", "Name": sheet.officials.var_official},
        ]
    return {
        "TeamData": [_export_team_sheet_opta(sheet.home), _export_team_sheet_opta(sheet.away)],
        "Substitution": subs, "MatchOfficial": officials,
        "Attendance": sheet.attendance, "Weather": sheet.weather,
    }


def _export_team_sheet_statsbomb(team: TeamSheet) -> dict:
    lineup = [
        {"player_id": p.player_id, "player_name": p.name, "jersey_number": p.jersey_number,
         "position_name": p.position, "starter": p.is_starter, "captain": p.is_captain}
        for p in [*team.starters, *team.substitutes]
    ]
    return {"team_name": team.team, "formation": team.formation, "lineup": lineup}


def _export_match_sheet_statsbomb(sheet: MatchSheet) -> dict:
    subs = [
        {"team": s.team, "player_out": s.player_out_id, "player_in": s.player_in_id,
         "minute": s.minute, "period": s.period, "reason": s.reason}
        for s in [*sheet.home.substitutions, *sheet.away.substitutions]
    ]
    return {
        "home_team": _export_team_sheet_statsbomb(sheet.home),
        "away_team": _export_team_sheet_statsbomb(sheet.away),
        "substitutions": subs,
        "referee": {"name": sheet.officials.referee if sheet.officials else None},
        "attendance": sheet.attendance, "weather": sheet.weather,
    }


def _export_team_sheet_wyscout(team: TeamSheet) -> dict:
    lineups = [
        {"playerId": p.player_id, "shortName": p.name, "shirtNumber": p.jersey_number,
         "role": p.position, "bench": not p.is_starter}
        for p in [*team.starters, *team.substitutes]
    ]
    return {"formation": team.formation, "lineups": lineups, "teamName": team.team}


def _export_match_sheet_wyscout(sheet: MatchSheet) -> dict:
    subs = [
        {"teamName": s.team, "playerOut": s.player_out_id, "playerIn": s.player_in_id, "minute": s.minute}
        for s in [*sheet.home.substitutions, *sheet.away.substitutions]
    ]
    return {
        "teams": {"home": _export_team_sheet_wyscout(sheet.home), "away": _export_team_sheet_wyscout(sheet.away)},
        "substitutions": subs,
        "attendance": sheet.attendance, "weatherConditions": sheet.weather,
    }


MATCH_SHEET_EXPORTERS: dict[str, Callable[[MatchSheet], dict]] = {
    "opta": _export_match_sheet_opta,
    "statsbomb": _export_match_sheet_statsbomb,
    "wyscout": _export_match_sheet_wyscout,
}


class FormatExporter:
    """Agent 7. Registry-driven like Agents 2-4: adding a new export target
    is one new entry in VENDOR_EXPORTERS, not a rewrite of this class.

    Deliberately has no automatic trigger anywhere in this codebase --
    call export() explicitly when something downstream actually needs a
    vendor-shaped file."""

    def export(self, document: CDFDocument, target_vendor: str) -> tuple[dict, ExportReport]:
        exporter_fn = VENDOR_EXPORTERS.get(target_vendor)
        report = ExportReport(target_vendor=target_vendor)
        if exporter_fn is None:
            report.warnings.append(
                f"no exporter registered for vendor '{target_vendor}'; supported: {sorted(VENDOR_EXPORTERS)}"
            )
            return {}, report

        payload = exporter_fn(document, report)
        if report.players_with_synthesized_id:
            report.warnings.append(
                f"{report.players_with_synthesized_id} of "
                f"{report.players_with_native_id + report.players_with_synthesized_id} player references "
                f"are synthesized placeholders, not real {target_vendor} ids"
            )

        if document.match_sheet is not None:
            sheet_exporter_fn = MATCH_SHEET_EXPORTERS.get(target_vendor)
            if sheet_exporter_fn is not None:
                payload["match_sheet"] = sheet_exporter_fn(document.match_sheet)
            else:
                report.warnings.append(
                    f"match sheet not exported: no match-sheet exporter registered for '{target_vendor}'"
                )

        return payload, report


if __name__ == "__main__":
    import json
    from pathlib import Path
    from format_detective import FormatDetective
    from schema_mapper import SchemaMapper
    from coordinate_translator import CoordinateTranslator
    from entity_resolver import EntityResolver
    from validator import Validator

    detective = FormatDetective()
    mapper = SchemaMapper()
    translator = CoordinateTranslator()
    resolver = EntityResolver()
    validator = Validator()
    exporter = FormatExporter()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Format Exporter -- test run (round-trip back to each MVP vendor)")
    print("=" * 78)

    match_dir = sample_root / "PL_2025_CLEAN_001"
    all_events, vendor_raw_data = [], {}
    for vendor_file in sorted(match_dir.glob("*.json")):
        data = json.loads(vendor_file.read_text())
        detection = detective.detect(data)
        vendor_raw_data[detection.detected_format] = data
        mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
        all_events.extend(translator.translate(mapped))

    resolved, registry = resolver.resolve(all_events)
    document, _ = validator.build_document(resolved, registry, vendor_raw_data)

    for target in ["opta", "statsbomb", "wyscout"]:
        payload, report = exporter.export(document, target)
        print(f"\nExport -> {target}: {report.events_exported} events, "
              f"{report.events_with_native_type_recovered} native-type recovered, "
              f"{report.events_with_guessed_type} type-guessed, "
              f"{report.players_with_native_id} native player ids, "
              f"{report.players_with_synthesized_id} synthesized player ids")
        for w in report.warnings:
            print(f"  warning: {w}")
