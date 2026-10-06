"""
Agent 10: Personalization Engine

Pipeline stage (e) from the hackathon brief: "Personalize it so the same
intelligence reaches a data-hungry analyst and a casual fan as two
genuinely different experiences... customized modes, favorite club(s),
favorite player(s), player focused mode... viewing modes shaped by
language preference, favorite club, favorite player, or a single
performance metric a viewer cares about."

This agent invents no new facts -- it reshapes the SAME underlying
intelligence (Agents 6, 8 and 9's output) for a specific viewer profile.
Three dimensions, each independently settable:

  mode: "analyst" | "casual_fan" | "player_focus"
    - analyst: full stat breakdown, every moment, every overlay cue,
      confidence/data-quality caveats included -- nothing hidden.
    - casual_fan: headline + key moments only, trimmed stats, jargon and
      data-quality caveats stripped out, momentum-shift cues dropped
      (those read as analyst content, not something a casual viewer needs).
    - player_focus: requires favorite_player_name. Filters moments and
      cues down to that player's events only. Honestly reports when no
      per-player stat breakdown exists for this MVP (team_stats aggregates
      by team, not by player) rather than fabricating player numbers that
      were never computed.

  favorite_club: when set, moments/cues are re-ordered (not filtered, in
    analyst/casual_fan modes) so that club's moments surface first, and
    the headline is framed from that club's perspective.

  language: deterministic template-phrase translation (stage vi,
    "multi-language storytelling") via a registry (LANGUAGE_PHRASES), same
    extensibility pattern as every vendor registry in this pipeline --
    adding a language is one new dict entry. This is NOT full narrative
    translation (that would need the same optional Claude escalation
    pattern as Agents 4/6, and is flagged below as the natural upgrade
    path); it translates the fixed phrases a broadcast graphic would
    actually show (score line, goal/card announcements), which is what
    "sits on screen" needs most.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cdf_models import CDFDocument
from narrative_generator import NarrativeReport
from explainability_engine import ExplainabilityReport, ExplainedMoment
from overlay_renderer import OverlayTimeline, OverlayCue

VALID_MODES = {"analyst", "casual_fan", "player_focus"}

# Registry-driven, same pattern as VENDOR_MAPPERS/VENDOR_EXPORTERS elsewhere
# in this pipeline. Translates only the fixed phrases a broadcast overlay
# needs -- full narrative translation is a documented future upgrade via
# the same optional-Claude-escalation pattern Agents 4 and 6 already use.
LANGUAGE_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "final_score": "{home} {home_goals} - {away_goals} {away}",
        "goal": "GOAL for {team}: {who}",
        "card": "CARD for {team}: {who}",
        "your_team_prefix": "Your team, {club}: ",
    },
    "es": {
        "final_score": "{home} {home_goals} - {away_goals} {away}",
        "goal": "¡GOL de {team}: {who}!",
        "card": "TARJETA para {team}: {who}",
        "your_team_prefix": "Tu equipo, {club}: ",
    },
    "fr": {
        "final_score": "{home} {home_goals} - {away_goals} {away}",
        "goal": "BUT pour {team} : {who}",
        "card": "CARTON pour {team} : {who}",
        "your_team_prefix": "Votre équipe, {club} : ",
    },
}
DEFAULT_LANGUAGE = "en"


@dataclass
class PersonalizationProfile:
    mode: str = "casual_fan"
    favorite_club: Optional[str] = None
    favorite_player_name: Optional[str] = None
    # Jersey number is often the ONLY reliable per-player identifier --
    # e.g. single-vendor Opta ingestion never carries player names at all.
    # Jersey alone isn't unique across both teams, so matching on jersey
    # requires favorite_club to disambiguate which team's #9, say.
    favorite_player_jersey: Optional[int] = None
    language: str = DEFAULT_LANGUAGE


@dataclass
class PersonalizedMoment:
    minute: int
    period: int
    team: Optional[str]
    player_name: Optional[str]
    headline: str
    context: str
    is_favorite_club: bool = False


@dataclass
class PersonalizedExperience:
    mode: str
    language: str
    headline: str
    narrative_text: str
    stats_view: dict
    moments: list[PersonalizedMoment] = field(default_factory=list)
    overlay_cues: list[OverlayCue] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def _phrases_for(language: str, log: list[str]) -> dict[str, str]:
    phrases = LANGUAGE_PHRASES.get(language)
    if phrases is None:
        log.append(f"language '{language}' not in LANGUAGE_PHRASES; falling back to '{DEFAULT_LANGUAGE}'")
        return LANGUAGE_PHRASES[DEFAULT_LANGUAGE]
    return phrases


def _team_stats_to_dict(stats) -> dict:
    return {
        "events_total": stats.events_total, "passes": stats.passes, "pass_accuracy": stats.pass_accuracy,
        "avg_pass_distance_m": stats.avg_pass_distance_m, "short_passes": stats.short_passes,
        "medium_passes": stats.medium_passes, "long_passes": stats.long_passes,
        "shots": stats.shots, "goals": stats.goals, "tackles": stats.tackles, "fouls": stats.fouls,
        "cards": stats.cards, "corners": stats.corners, "low_confidence_events": stats.low_confidence_events,
        "avg_confidence": stats.avg_confidence,
    }


class PersonalizationEngine:
    """Agent 10. Takes Agents 6/8/9's outputs for the SAME CDFDocument plus
    a PersonalizationProfile and produces one viewer-specific experience.
    Does not mutate its inputs."""

    def personalize(self, document: CDFDocument, narrative: NarrativeReport,
                     explainability: ExplainabilityReport, timeline: OverlayTimeline,
                     profile: PersonalizationProfile) -> PersonalizedExperience:
        log: list[str] = []
        mode = profile.mode if profile.mode in VALID_MODES else "casual_fan"
        if profile.mode not in VALID_MODES:
            log.append(f"unknown mode '{profile.mode}'; defaulted to 'casual_fan'")

        phrases = _phrases_for(profile.language, log)
        resolved_language = profile.language if profile.language in LANGUAGE_PHRASES else DEFAULT_LANGUAGE

        home, away = document.match.home_team, document.match.away_team
        home_stats = narrative.team_stats.get(home)
        away_stats = narrative.team_stats.get(away)
        home_goals = home_stats.goals if home_stats else 0
        away_goals = away_stats.goals if away_stats else 0

        headline = phrases["final_score"].format(home=home, home_goals=home_goals, away=away, away_goals=away_goals)
        if profile.favorite_club in (home, away):
            headline = phrases["your_team_prefix"].format(club=profile.favorite_club) + headline

        moments = self._build_moments(explainability.explained_moments, phrases, profile, log)
        cues = self._filter_cues(timeline.cues, mode)
        stats_view = self._build_stats_view(narrative, mode, profile, log)
        narrative_text = self._build_narrative_text(narrative, mode)

        return PersonalizedExperience(
            mode=mode, language=resolved_language, headline=headline, narrative_text=narrative_text,
            stats_view=stats_view, moments=moments, overlay_cues=cues, log=log,
        )

    def _build_moments(self, explained_moments: list[ExplainedMoment], phrases: dict[str, str],
                        profile: PersonalizationProfile, log: list[str]) -> list[PersonalizedMoment]:
        candidates = [em for em in explained_moments if em.event_type in ("goal", "card")]

        if profile.mode == "player_focus":
            if not profile.favorite_player_name and profile.favorite_player_jersey is None:
                log.append("mode='player_focus' but neither favorite_player_name nor favorite_player_jersey set; returning no moments")
                return []

            def _matches(em: ExplainedMoment) -> bool:
                if profile.favorite_player_name and em.player_name == profile.favorite_player_name:
                    return True
                if profile.favorite_player_jersey is not None and em.player_jersey == profile.favorite_player_jersey:
                    # Jersey alone isn't unique across both teams -- require favorite_club to
                    # be set AND match, so an ambiguous jersey-only profile matches nothing
                    # rather than silently guessing which team's #9 was meant.
                    return profile.favorite_club is not None and em.team == profile.favorite_club
                return False

            candidates = [em for em in candidates if _matches(em)]
            if not candidates:
                log.append(
                    f"no moments found for favorite_player_name='{profile.favorite_player_name}' "
                    f"/ favorite_player_jersey={profile.favorite_player_jersey}"
                )
        elif profile.mode == "casual_fan":
            # Cards are analyst-level detail for a casual viewer; keep goals only.
            candidates = [em for em in candidates if em.event_type == "goal"]

        results = []
        for em in candidates:
            phrase_key = "goal" if em.event_type == "goal" else "card"
            who = em.player_name or "an unidentified player"
            headline = phrases[phrase_key].format(team=em.team or "unknown team", who=who)
            is_favorite = profile.favorite_club is not None and em.team == profile.favorite_club
            results.append(PersonalizedMoment(
                minute=em.minute, period=em.period, team=em.team, player_name=em.player_name,
                headline=headline, context=em.context, is_favorite_club=is_favorite,
            ))

        # Favorite-club moments surface first without dropping the rest
        # (analyst/casual_fan still see everything relevant, just reordered).
        if profile.favorite_club:
            results.sort(key=lambda m: not m.is_favorite_club)
        return results

    def _filter_cues(self, cues: list[OverlayCue], mode: str) -> list[OverlayCue]:
        if mode == "casual_fan":
            return [c for c in cues if c.cue_type in ("score_bug", "goal")]
        if mode == "player_focus":
            return [c for c in cues if c.cue_type in ("score_bug", "goal", "card")]
        return list(cues)  # analyst: everything

    def _build_stats_view(self, narrative: NarrativeReport, mode: str,
                           profile: PersonalizationProfile, log: list[str]) -> dict:
        if mode == "analyst":
            return {team: _team_stats_to_dict(s) for team, s in narrative.team_stats.items()}

        if mode == "player_focus":
            log.append(
                "per-player stat breakdown is not computed in this MVP (team_stats aggregates by team, not "
                "by player) -- showing the favorite player's team context instead of fabricated player numbers"
            )
            if profile.favorite_club and profile.favorite_club in narrative.team_stats:
                return {profile.favorite_club: _team_stats_to_dict(narrative.team_stats[profile.favorite_club])}
            log.append("no favorite_club set alongside favorite_player_name; cannot resolve team context")
            return {}

        # casual_fan: goals + shots only, no pass-distance/confidence jargon
        return {
            team: {"goals": s.goals, "shots": s.shots}
            for team, s in narrative.team_stats.items()
        }

    def _build_narrative_text(self, narrative: NarrativeReport, mode: str) -> str:
        if mode == "analyst":
            return narrative.narrative_text
        # casual_fan / player_focus: strip the trailing data-quality clause,
        # which is analyst-level caveat language a casual viewer doesn't need.
        text = narrative.narrative_text
        if "Data quality:" in text:
            text = text.split("Data quality:")[0].strip()
        return text


if __name__ == "__main__":
    import json
    from pathlib import Path
    from format_detective import FormatDetective
    from schema_mapper import SchemaMapper
    from coordinate_translator import CoordinateTranslator
    from entity_resolver import EntityResolver
    from validator import Validator
    from narrative_generator import NarrativeGenerator
    from explainability_engine import ExplainabilityEngine
    from overlay_renderer import OverlayRenderer

    detective = FormatDetective()
    mapper = SchemaMapper()
    translator = CoordinateTranslator()
    resolver = EntityResolver()
    validator = Validator()
    narrator = NarrativeGenerator(ai_enabled=False)
    explainer = ExplainabilityEngine()
    renderer = OverlayRenderer()
    personalizer = PersonalizationEngine()

    match_dir = Path("backend/data/sample_matches/PL_2025_CLEAN_001")
    data = json.loads((match_dir / "opta.json").read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)
    resolved, registry = resolver.resolve(translated)
    document, _ = validator.build_document(resolved, registry, {detection.detected_format: data})
    narrative = narrator.generate(document)
    explainability = explainer.generate(document)
    timeline = renderer.render(document, narrative, explainability)

    print("=" * 78)
    print("Personalization Engine -- test run (same match, 3 viewer profiles)")
    print("=" * 78)

    profiles = [
        PersonalizationProfile(mode="analyst"),
        PersonalizationProfile(mode="casual_fan", favorite_club=document.match.home_team, language="es"),
        PersonalizationProfile(mode="player_focus", favorite_player_jersey=16,
                                favorite_club=document.match.home_team),
    ]
    for profile in profiles:
        exp = personalizer.personalize(document, narrative, explainability, timeline, profile)
        print(f"\n[{exp.mode} / {exp.language}] {exp.headline}")
        print(f"  narrative: {exp.narrative_text[:150]}...")
        print(f"  moments: {len(exp.moments)} | overlay cues: {len(exp.overlay_cues)} | stats keys: {list(exp.stats_view)}")
        if exp.log:
            for l in exp.log:
                print(f"  log: {l}")
