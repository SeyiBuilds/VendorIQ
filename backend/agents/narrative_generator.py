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
    paragraph via an LLM call, gated on ANTHROPIC_API_KEY or GROQ_API_KEY
    (whichever is set; Anthropic tried first if both are) -- no key, or
    every configured call failing -> skip, log why, serve the template
    version. The template version is not a lesser stand-in; it states the
    same facts, just more mechanically. Either provider gets the exact
    same prompt built from the exact same computed stats, so the choice of
    provider changes the prose, never the facts it's allowed to state.

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
    shots_on_target: int = 0
    goals: int = 0
    tackles: int = 0
    interceptions: int = 0
    clearances: int = 0
    saves: int = 0
    fouls: int = 0
    cards: int = 0
    yellow_cards: int = 0
    red_cards: int = 0         # straight reds and second yellows: every sending-off
    corners: int = 0
    offsides: int = 0
    possession_proxy_events: int = 0
    possession_pct: Optional[float] = None  # filled in after both teams' totals are known
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
    def shot_accuracy(self) -> Optional[float]:
        if self.shots == 0:
            return None
        return round(self.shots_on_target / self.shots, 3)

    @property
    def duels_won(self) -> int:
        # Tackles + interceptions is a defensive-duel proxy, not a vendor
        # field -- no vendor here reports a duel-outcome event directly.
        return self.tackles + self.interceptions

    @property
    def avg_pass_distance_m(self) -> Optional[float]:
        if self.passes_with_distance == 0:
            return None
        return round(self.pass_distance_total_m / self.passes_with_distance, 2)


@dataclass
class KeyMoment:
    minute: int
    period: int
    second_in_period: float  # exact timestamp, for downstream agents needing tighter sync than minute-granularity
    event_type: str
    team: Optional[str]
    player_name: Optional[str]
    player_jersey: Optional[int]  # often the only reliable per-player identifier (names aren't always resolved)
    confidence: float
    description: str
    card_type: Optional[str] = None     # "yellow" | "second_yellow" | "red" for card moments, when the feed says
    shot_outcome: Optional[str] = None  # set on goal moments when the feed carries shot detail


@dataclass
class NarrativeReport:
    narrative_text: str
    generation_method: str  # "template" | "ai_enhanced"
    team_stats: dict[str, TeamStats]
    key_moments: list[KeyMoment]
    data_quality_notes: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def _build_narrative_prompt(match_summary_line: str, team_stats: dict[str, TeamStats],
                             key_moments: list[KeyMoment], data_quality_notes: list[str]) -> str:
    stats_lines = []
    for team, s in team_stats.items():
        acc = f"{s.pass_accuracy * 100:.0f}%" if s.pass_accuracy is not None else "n/a"
        avg_dist = f"{s.avg_pass_distance_m}m avg" if s.avg_pass_distance_m is not None else "n/a"
        poss = f"{s.possession_pct:.0f}%" if s.possession_pct is not None else "n/a"
        stats_lines.append(
            f"- {team}: {s.goals} goals, {s.shots} shots ({s.shots_on_target} on target), "
            f"{poss} ball-event share, {s.passes} passes ({acc} completion, {avg_dist} distance, "
            f"{s.short_passes} short/{s.medium_passes} medium/{s.long_passes} long), "
            f"{s.duels_won} defensive duels won ({s.tackles} tackles + {s.interceptions} interceptions), "
            f"{s.clearances} clearances, {s.saves} saves, "
            f"{s.fouls} fouls, {s.cards} cards ({s.yellow_cards} yellow, {s.red_cards} red or second yellow), "
            f"{s.corners} corners, {s.offsides} offsides"
        )
    moments_lines = [f"- {m.minute}' ({m.period}H): {m.description}" for m in key_moments]
    quality_lines = "\n".join(f"- {n}" for n in data_quality_notes) or "- none"

    return (
        f"Write a concise, analyst-style tactical summary (3-5 sentences) of this football match, "
        f"using ONLY the data below. Do not invent details not present here.\n\n"
        f"{match_summary_line}\n\nTeam stats:\n" + "\n".join(stats_lines) +
        "\n\nKey moments:\n" + "\n".join(moments_lines) +
        f"\n\nData quality caveats (mention briefly if material):\n{quality_lines}"
    )


def _call_anthropic_for_narrative(prompt: str, api_key: str) -> Optional[str]:
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        return None


def _call_groq_for_narrative(prompt: str, api_key: str) -> Optional[str]:
    """Groq serves open-weight models (Llama 3.x) behind an OpenAI-compatible
    chat-completions endpoint -- same prompt, different wire format, no
    Anthropic SDK dependency. A free-tier-friendly alternative when no
    ANTHROPIC_API_KEY is available; see GROQ_MODEL below for the default."""
    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return None


GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


def _call_llm_for_narrative(match_summary_line: str, team_stats: dict[str, TeamStats],
                             key_moments: list[KeyMoment], data_quality_notes: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Optional upgrade path, same contract as Entity Resolver's escalation:
    returns (narrative_text, provider_name), or (None, None) if no provider
    is configured or every configured provider's call failed -- the caller
    treats that exactly like "no key", template path always covers it.

    Provider choice: ANTHROPIC_API_KEY is tried first (if set), then
    GROQ_API_KEY as a fallback/alternative -- independent providers, not a
    preference ranking; whichever key is present and succeeds wins. Both
    get the identical prompt built from the SAME computed stats, so the
    choice of provider changes the prose, never the facts.
    """
    prompt = _build_narrative_prompt(match_summary_line, team_stats, key_moments, data_quality_notes)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        text = _call_anthropic_for_narrative(prompt, anthropic_key)
        if text:
            return text, "anthropic"

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        text = _call_groq_for_narrative(prompt, groq_key)
        if text:
            return text, "groq"

    return None, None


# Minute each period starts at: second half 45', extra time 90' and 105'.
# Timestamps are seconds into their own period.
PERIOD_START_MINUTE = {1: 0, 2: 45, 3: 90, 4: 105}

CARD_LABELS = {
    "yellow": "Yellow card",
    "second_yellow": "Second yellow card, sent off",
    "red": "Red card, sent off",
}


def _is_card_moment(e) -> bool:
    """A card event, or a foul carrying the card itself (how StatsBomb and
    Wyscout record a booking for a foul)."""
    return e.event_type == "card" or (e.event_type == "foul" and e.card_type is not None)


def extract_key_moments(document: CDFDocument) -> list[KeyMoment]:
    """Module-level so Agent 8 (Explainability Engine) can reuse the exact
    same goal/card extraction and minute math without importing private
    NarrativeGenerator internals or duplicating the logic."""
    players_by_id = {p.player_id: p for p in document.players}
    # The match sheet (same document) lists every squad member by team and
    # shirt number. An event feed with numbers but no names (Opta's) can
    # take the name from there: a lookup inside one document, not a guess.
    sheet_names: dict[tuple[str, int], str] = {}
    if document.match_sheet is not None:
        for side in (document.match_sheet.home, document.match_sheet.away):
            for entry in side.starters + side.substitutes:
                if entry.name and entry.jersey_number is not None:
                    sheet_names[(side.team, entry.jersey_number)] = entry.name
    moments: list[KeyMoment] = []

    for e in document.events:
        if e.event_type != "goal" and not _is_card_moment(e):
            continue
        minute = int(e.timestamp // 60) + PERIOD_START_MINUTE.get(e.period, 0)
        player = players_by_id.get(e.player_id) if e.player_id else None
        player_name = player.name if player else None
        if not player_name and player and player.jersey_number is not None:
            player_name = sheet_names.get((e.team, player.jersey_number))

        if player_name:
            who = player_name
        elif player and player.jersey_number is not None:
            # Resolved identity (a real cross-vendor cluster or a vendor's
            # own jersey-based record), just no name on record -- this is
            # common for single-vendor Opta ingestion, which carries no
            # player names at all. Not the same thing as an unresolved id.
            who = f"Jersey #{player.jersey_number} ({e.team or 'unknown team'})"
        elif e.player_id and e.player_id.startswith("unresolved::"):
            who = "an unidentified player (no cross-vendor or jersey signal available)"
        elif e.player_id:
            who = f"a player (id={e.player_id})"
        else:
            who = "an unidentified player"

        card_type = e.card_type if _is_card_moment(e) else None
        if e.event_type == "goal":
            description = f"Goal for {e.team}: {who}"
        else:
            # The vendor feed decides the colour; with no card detail it is
            # reported as a card of unknown colour, never guessed.
            label = CARD_LABELS.get(card_type, "Card (colour not in feed)")
            description = f"{label} for {e.team}: {who}"

        moments.append(KeyMoment(
            minute=minute, period=e.period, second_in_period=e.timestamp,
            event_type="card" if _is_card_moment(e) else e.event_type,
            team=e.team, player_name=player_name, player_jersey=player.jersey_number if player else None,
            confidence=e.quality_confidence, description=description,
            card_type=card_type, shot_outcome=e.shot_outcome if e.event_type == "goal" else None,
        ))

    moments.sort(key=lambda m: (m.period, m.minute))
    return moments


class NarrativeGenerator:
    """Agent 7 in the PRD's numbering is Format Exporter; this is Agent 6.
    Consumes a validated CDFDocument -- the output of Agent 5 -- and is
    deliberately vendor-blind: it never looks at vendor_source for anything
    except surfacing it in data-quality notes."""

    def __init__(self, ai_enabled: bool = AI_NARRATIVE_ENABLED_DEFAULT, in_progress: bool = False):
        self.ai_enabled = ai_enabled
        # True for a match still being fed in (live feed replay): the score
        # is then "so far", never "final". Only the wording changes; every
        # stat is computed identically either way.
        self.in_progress = in_progress

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

        ai_text, provider = None, None
        if self.ai_enabled:
            ai_text, provider = _call_llm_for_narrative(match_summary_line, team_stats, key_moments, data_quality_notes)

        if ai_text:
            log.append(f"narrative generated via {provider} ({provider.upper()}_API_KEY configured)")
            return NarrativeReport(
                narrative_text=ai_text, generation_method="ai_enhanced",
                team_stats=team_stats, key_moments=key_moments,
                data_quality_notes=data_quality_notes, log=log,
            )

        if self.ai_enabled:
            log.append("AI narrative escalation skipped or failed: no ANTHROPIC_API_KEY/GROQ_API_KEY configured, or every configured call failed; using template narrative")
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
            # An event with no team (or one Validator didn't recognize) still
            # counts, under its own heading, rather than being dropped.
            team = e.team or "unknown_team"
            if team not in stats:
                stats[team] = TeamStats(team=team)

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
                # Shot detail beats the generic success flag when the feed has it:
                # only saved shots (and goals, below) are on target.
                on_target = (e.shot_outcome == "saved") if e.shot_outcome else bool(e.outcome)
                if on_target:
                    s.shots_on_target += 1
            elif e.event_type == "goal":
                s.goals += 1
                s.shots += 1  # a goal is also a shot, for shot-count purposes
                s.shots_on_target += 1  # a goal is definitionally on target
            elif e.event_type == "tackle":
                s.tackles += 1
            elif e.event_type == "interception":
                s.interceptions += 1
            elif e.event_type == "clearance":
                s.clearances += 1
            elif e.event_type == "save":
                s.saves += 1
            elif e.event_type == "foul":
                s.fouls += 1
            elif e.event_type == "card":
                s.cards += 1

            # A card is counted once: from its card event, or from the foul it
            # rides on when the vendor attaches it there (StatsBomb, Wyscout).
            if _is_card_moment(e):
                if e.event_type == "foul":
                    s.cards += 1
                if e.card_type == "yellow":
                    s.yellow_cards += 1
                elif e.card_type in ("second_yellow", "red"):
                    s.red_cards += 1
            elif e.event_type == "corner":
                s.corners += 1
            elif e.event_type == "offside":
                s.offsides += 1

            if e.event_type in POSSESSION_PROXY_TYPES:
                s.possession_proxy_events += 1

        total_possession_events = sum(s.possession_proxy_events for s in stats.values())
        for s in stats.values():
            team_events = [e.quality_confidence for e in document.events if e.team == s.team]
            s.avg_confidence = round(sum(team_events) / len(team_events), 3) if team_events else 1.0
            s.possession_pct = (
                round(100 * s.possession_proxy_events / total_possession_events, 1)
                if total_possession_events else None
            )

        return stats

    def _extract_key_moments(self, document: CDFDocument) -> list[KeyMoment]:
        return extract_key_moments(document)

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

        home_poss_pct = (home_stats.possession_pct if home_stats else None) or 0
        away_poss_pct = (away_stats.possession_pct if away_stats else None) or 0

        lines = [
            f"{match_summary_line}: {'score so far' if self.in_progress else 'final score'} "
            f"{home} {home_goals} - {away_goals} {away}.",
            f"{home} saw an estimated {home_poss_pct:.0f}% of ball-event share to {away}'s {away_poss_pct:.0f}% "
            f"(proxy from pass/dribble/shot/set-piece events, not true possession time).",
        ]

        if home_stats and away_stats:
            lines.append(
                f"Shots: {home} {home_stats.shots} ({home_stats.shots_on_target} on target, "
                f"{home_stats.pass_accuracy and round(home_stats.pass_accuracy*100)}% pass completion) vs "
                f"{away} {away_stats.shots} ({away_stats.shots_on_target} on target, "
                f"{away_stats.pass_accuracy and round(away_stats.pass_accuracy*100)}% pass completion)."
            )
            lines.append(
                f"Pass distance: {home} averaged {home_stats.avg_pass_distance_m or 0}m per pass "
                f"({home_stats.short_passes} short, {home_stats.medium_passes} medium, {home_stats.long_passes} long) "
                f"vs {away} {away_stats.avg_pass_distance_m or 0}m "
                f"({away_stats.short_passes} short, {away_stats.medium_passes} medium, {away_stats.long_passes} long)."
            )
            lines.append(
                f"Defensive duels won (tackles + interceptions): {home} {home_stats.duels_won} vs {away} {away_stats.duels_won}."
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
