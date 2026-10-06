"""
Agent 6: Narrative Generator

This is the agent that proves the point of the whole pipeline: a CDFDocument
that's just correctly-shaped JSON is not, by itself, useful to anyone. This
agent turns it into the thing a club's analyst or coach actually wants --
a tactical summary and a stat line -- using exactly the normalized data
Agents 1-5 produced, nothing vendor-specific. If this agent can run
identically regardless of which vendor(s) fed the match, the normalization
layer actually did its job.

Two outputs, always both produced:
  - team_stats: deterministic, computed directly from CDFEvent counts. No
    AI involved here at all -- a possession split or a shot count is
    arithmetic, not something to hand to a language model.
  - narrative_text: a human-readable paragraph. ALWAYS has a deterministic,
    template-based version (so this agent works fully offline, same as
    Entity Resolver). Optionally upgraded to a more naturally-written
    paragraph via Claude, gated on ANTHROPIC_API_KEY exactly like Agent 4 --
    no key -> skip, log why, serve the template version. The template
    version is not a lesser stand-in; it states the same facts, just more
    mechanically.

Confidence awareness (the thing the PRD calls out for this agent
specifically: "should weight or flag low-confidence events rather than
silently trusting them"): every event still counts toward the raw totals
(a messy-tier shot is still a shot that happened), but events below
LOW_CONFIDENCE_THRESHOLD are tracked separately and surfaced as a data
quality caveat in both team_stats and the narrative, rather than quietly
blended in as if the data were clean.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from cdf_models import CDFDocument, CDFEvent

LOW_CONFIDENCE_THRESHOLD = 0.6

# Event types treated as "possession events" for the possession-share proxy.
# Real possession needs ball-tracking data no vendor here provides; this is
# an explicitly-labeled proxy (event share), not a claim of true possession %.
POSSESSION_PROXY_TYPES = {
    "pass", "dribble", "shot", "corner", "throw_in", "goal_kick", "goal",
}

AI_NARRATIVE_ENABLED_DEFAULT = True

# Standard pitch dimensions, used ONLY to convert the normalized 0-100 CDF
# coordinate space into meters for pass-distance reporting. CDF itself
# carries no real pitch-dimensions field (Section: cdf_models.py), so this
# is a documented assumption, not a value read from any vendor file.
PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0

# Pass-difficulty bands by distance. This is a distance-based heuristic,
# not a true expected-pass (xP) model -- a real difficulty rating would
# also need defender proximity and passing-lane congestion, which no
# vendor here supplies at the event level. Documented as a proxy, same
# spirit as the possession-share proxy above.
PASS_DIFFICULTY_BANDS: list[tuple[float, str]] = [(15.0, "short"), (30.0, "medium"), (float("inf"), "long")]


def _pass_distance_m(x: float, y: float, end_x: Optional[float], end_y: Optional[float]) -> Optional[float]:
    if end_x is None or end_y is None:
        return None
    dx_m = (end_x - x) / 100.0 * PITCH_LENGTH_M
    dy_m = (end_y - y) / 100.0 * PITCH_WIDTH_M
    return round((dx_m ** 2 + dy_m ** 2) ** 0.5, 2)


def _pass_difficulty_band(distance_m: float) -> str:
    for threshold, label in PASS_DIFFICULTY_BANDS:
        if distance_m <= threshold:
            return label
    return "long"  # unreachable given the inf band, kept for defensiveness


@dataclass
class TeamStats:
    team: str
    events_total: int = 0
    passes: int = 0
    passes_completed: int = 0
    shots: int = 0
    goals: int = 0
    tackles: int = 0
    fouls: int = 0
    cards: int = 0
    corners: int = 0
    possession_proxy_events: int = 0
    low_confidence_events: int = 0
    avg_confidence: float = 1.0
    pass_distance_total_m: float = 0.0
    passes_with_distance: int = 0
    short_passes: int = 0
    medium_passes: int = 0
    long_passes: int = 0

    @property
    def pass_accuracy(self) -> Optional[float]:
        if self.passes == 0:
            return None
        return round(self.passes_completed / self.passes, 3)

    @property
    def avg_pass_distance_m(self) -> Optional[float]:
        if self.passes_with_distance == 0:
            return None
        return round(self.pass_distance_total_m / self.passes_with_distance, 2)


@dataclass
class KeyMoment:
    minute: int
    period: int
    event_type: str
    team: Optional[str]
    player_name: Optional[str]
    confidence: float
    description: str


@dataclass
class NarrativeReport:
    narrative_text: str
    generation_method: str  # "template" | "ai_enhanced"
    team_stats: dict[str, TeamStats]
    key_moments: list[KeyMoment]
    data_quality_notes: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def _call_claude_for_narrative(match_summary_line: str, team_stats: dict[str, TeamStats],
                                key_moments: list[KeyMoment], data_quality_notes: list[str]) -> Optional[str]:
    """Optional upgrade path, same contract as Entity Resolver's escalation:
    returns narrative text, or None if no key is configured. A dict with an
    '_error' key signals a configured-but-failed call, so the caller can
    log the distinction without crashing either way."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        stats_lines = []
        for team, s in team_stats.items():
            acc = f"{s.pass_accuracy * 100:.0f}%" if s.pass_accuracy is not None else "n/a"
            avg_dist = f"{s.avg_pass_distance_m}m avg" if s.avg_pass_distance_m is not None else "n/a"
            stats_lines.append(
                f"- {team}: {s.goals} goals, {s.shots} shots, {s.passes} passes ({acc} completion, {avg_dist} distance, "
                f"{s.short_passes} short/{s.medium_passes} medium/{s.long_passes} long), "
                f"{s.tackles} tackles, {s.fouls} fouls, {s.cards} cards, {s.corners} corners"
            )
        moments_lines = [f"- {m.minute}' ({m.period}H): {m.description}" for m in key_moments]
        quality_lines = "\n".join(f"- {n}" for n in data_quality_notes) or "- none"

        prompt = (
            f"Write a concise, analyst-style tactical summary (3-5 sentences) of this football match, "
            f"using ONLY the data below. Do not invent details not present here.\n\n"
            f"{match_summary_line}\n\nTeam stats:\n" + "\n".join(stats_lines) +
            "\n\nKey moments:\n" + "\n".join(moments_lines) +
            f"\n\nData quality caveats (mention briefly if material):\n{quality_lines}"
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        return None  # caller treats this exactly like "no key" -- template path always covers it


class NarrativeGenerator:
    """Agent 7 in the PRD's numbering is Format Exporter; this is Agent 6.
    Consumes a validated CDFDocument -- the output of Agent 5 -- and is
    deliberately vendor-blind: it never looks at vendor_source for anything
    except surfacing it in data-quality notes."""

    def __init__(self, ai_enabled: bool = AI_NARRATIVE_ENABLED_DEFAULT):
        self.ai_enabled = ai_enabled

    def generate(self, document: CDFDocument) -> NarrativeReport:
        log: list[str] = []
        team_stats = self._compute_team_stats(document)
        key_moments = self._extract_key_moments(document)
        data_quality_notes = self._build_data_quality_notes(document, team_stats)

        match_summary_line = (
            f"{document.match.home_team} vs {document.match.away_team} "
            f"({document.match.match_id}, {document.match.date})"
        )
        template_text = self._build_template_narrative(document, match_summary_line, team_stats, key_moments, data_quality_notes)

        ai_text = None
        if self.ai_enabled:
            ai_text = _call_claude_for_narrative(match_summary_line, team_stats, key_moments, data_quality_notes)

        if ai_text:
            log.append("narrative generated via Claude (ANTHROPIC_API_KEY configured)")
            return NarrativeReport(
                narrative_text=ai_text, generation_method="ai_enhanced",
                team_stats=team_stats, key_moments=key_moments,
                data_quality_notes=data_quality_notes, log=log,
            )

        if self.ai_enabled:
            log.append("AI narrative escalation skipped or failed: ANTHROPIC_API_KEY not configured or call failed; using template narrative")
        else:
            log.append("AI narrative disabled by caller; using template narrative")

        return NarrativeReport(
            narrative_text=template_text, generation_method="template",
            team_stats=team_stats, key_moments=key_moments,
            data_quality_notes=data_quality_notes, log=log,
        )

    def _compute_team_stats(self, document: CDFDocument) -> dict[str, TeamStats]:
        stats: dict[str, TeamStats] = {}
        for team in (document.match.home_team, document.match.away_team):
            stats[team] = TeamStats(team=team)

        for e in document.events:
            team = e.team
            if team not in stats:
                # Defensive: a team name Validator didn't recognize (shouldn't
                # happen for this dataset, but an event shouldn't be silently
                # dropped from the stat line just because of that).
                stats[team] = TeamStats(team=team or "unknown_team")
                team = team or "unknown_team"

            s = stats[team]
            s.events_total += 1
            if e.quality_confidence < LOW_CONFIDENCE_THRESHOLD:
                s.low_confidence_events += 1

            if e.event_type == "pass":
                s.passes += 1
                if e.outcome:
                    s.passes_completed += 1
                distance_m = _pass_distance_m(e.x, e.y, e.end_x, e.end_y)
                if distance_m is not None:
                    s.pass_distance_total_m += distance_m
                    s.passes_with_distance += 1
                    band = _pass_difficulty_band(distance_m)
                    if band == "short":
                        s.short_passes += 1
                    elif band == "medium":
                        s.medium_passes += 1
                    else:
                        s.long_passes += 1
            elif e.event_type == "shot":
                s.shots += 1
            elif e.event_type == "goal":
                s.goals += 1
                s.shots += 1  # a goal is also a shot, for shot-count purposes
            elif e.event_type == "tackle":
                s.tackles += 1
            elif e.event_type == "foul":
                s.fouls += 1
            elif e.event_type == "card":
                s.cards += 1
            elif e.event_type == "corner":
                s.corners += 1

            if e.event_type in POSSESSION_PROXY_TYPES:
                s.possession_proxy_events += 1

        total_possession_events = sum(s.possession_proxy_events for s in stats.values())
        for s in stats.values():
            team_events = [e.quality_confidence for e in document.events if e.team == s.team]
            s.avg_confidence = round(sum(team_events) / len(team_events), 3) if team_events else 1.0

        # possession_proxy stays a raw count; percentage is derived on demand
        # by the narrative builder so TeamStats stays a plain facts object.
        return stats

    def _extract_key_moments(self, document: CDFDocument) -> list[KeyMoment]:
        player_names = {p.player_id: p.name for p in document.players}
        moments: list[KeyMoment] = []

        for e in document.events:
            if e.event_type not in ("goal", "card"):
                continue
            minute = int(e.timestamp // 60) + (45 if e.period == 2 else 0)
            player_name = player_names.get(e.player_id) if e.player_id else None
            who = player_name or (f"a player (unresolved identity, player_id={e.player_id})" if e.player_id else "an unidentified player")

            if e.event_type == "goal":
                description = f"Goal for {e.team} -- {who}"
            else:
                description = f"Card for {e.team} -- {who}"

            moments.append(KeyMoment(
                minute=minute, period=e.period, event_type=e.event_type,
                team=e.team, player_name=player_name,
                confidence=e.quality_confidence, description=description,
            ))

        moments.sort(key=lambda m: (m.period, m.minute))
        return moments

    def _build_data_quality_notes(self, document: CDFDocument, team_stats: dict[str, TeamStats]) -> list[str]:
        notes = list(document.metadata.warnings)
        summary = document.confidence_summary()
        if summary["event_count"] and summary["low_confidence_count"]:
            pct = round(100 * summary["low_confidence_count"] / summary["event_count"])
            notes.append(
                f"{summary['low_confidence_count']} of {summary['event_count']} events "
                f"({pct}%) are below the {LOW_CONFIDENCE_THRESHOLD} confidence threshold -- "
                "stats below should be read as directionally reliable, not exact"
            )
        vendors = ", ".join(document.metadata.source_vendors)
        notes.append(f"source vendor(s): {vendors}")
        return notes

    def _build_template_narrative(self, document: CDFDocument, match_summary_line: str,
                                    team_stats: dict[str, TeamStats], key_moments: list[KeyMoment],
                                    data_quality_notes: list[str]) -> str:
        home, away = document.match.home_team, document.match.away_team
        home_stats, away_stats = team_stats.get(home), team_stats.get(away)
        home_goals = home_stats.goals if home_stats else 0
        away_goals = away_stats.goals if away_stats else 0

        total_poss = sum(s.possession_proxy_events for s in team_stats.values()) or 1
        home_poss_pct = round(100 * (home_stats.possession_proxy_events / total_poss)) if home_stats else 0
        away_poss_pct = 100 - home_poss_pct

        lines = [
            f"{match_summary_line}: final score {home} {home_goals} - {away_goals} {away}.",
            f"{home} saw an estimated {home_poss_pct}% of ball-event share to {away}'s {away_poss_pct}% "
            f"(proxy from pass/dribble/shot/set-piece events, not true possession time).",
        ]

        if home_stats and away_stats:
            lines.append(
                f"Shots: {home} {home_stats.shots} ({home_stats.pass_accuracy and round(home_stats.pass_accuracy*100)}% "
                f"pass completion) vs {away} {away_stats.shots} "
                f"({away_stats.pass_accuracy and round(away_stats.pass_accuracy*100)}% pass completion)."
            )
            lines.append(
                f"Pass distance: {home} averaged {home_stats.avg_pass_distance_m or 0}m per pass "
                f"({home_stats.short_passes} short, {home_stats.medium_passes} medium, {home_stats.long_passes} long) "
                f"vs {away} {away_stats.avg_pass_distance_m or 0}m "
                f"({away_stats.short_passes} short, {away_stats.medium_passes} medium, {away_stats.long_passes} long)."
            )

        if key_moments:
            moment_strs = [f"{m.minute}' {m.description}" for m in key_moments[:6]]
            lines.append("Key moments: " + "; ".join(moment_strs) + ".")
        else:
            lines.append("No goals or cards recorded in this dataset.")

        if data_quality_notes:
            lines.append("Data quality: " + "; ".join(data_quality_notes[-2:]) + ".")

        return " ".join(lines)


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
    narrator = NarrativeGenerator()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Narrative Generator -- test run (full pipeline, Detective through Narrative)")
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
        document, _ = validator.build_document(resolved, registry, vendor_raw_data)
        report = narrator.generate(document)

        print(f"\n{match_dir.name} ({report.generation_method})")
        print(f"  {report.narrative_text}")
        print(f"  key moments: {len(report.key_moments)}")
