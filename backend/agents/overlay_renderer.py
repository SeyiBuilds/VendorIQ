"""
Agent 9: Overlay Renderer

Pipeline stage (d) from the hackathon brief: "Render that insight into
something that can sit on screen alongside the match... timed and
machine-readable enough to be rendered as synchronized graphical overlays
on a live match feed, in the way the platform's rendering partner handles
overlays in the production path."

This agent is the one place in the pipeline whose output is NOT prose --
it's a timed sequence of structured cues a graphics engine could consume
directly to trigger on-screen elements at the right moment: a persistent
score bug, a short "GOAL" graphic, a card graphic, a momentum-shift
stat flash. Every cue carries an absolute match-clock second (continuous
across periods, the way broadcast timing actually works) so a renderer
can schedule it without doing period-offset math itself.

Inputs are the outputs of Agents 5, 6 and 8 -- this agent invents no new
analysis of its own, it only re-shapes what Validator/Narrative
Generator/Explainability Engine already produced into a presentation-
ready timeline. That separation matters: if the render format needs to
change for a different broadcast partner, nothing upstream has to.

Known data-model limitation, documented rather than silently patched
around: CDF's canonical event_type has one "card" type with no yellow/red
distinction (see cdf_models.EventType), so card cues are labeled
generically. A real production CDF would need a card_color field; out of
scope for this MVP schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cdf_models import CDFDocument
from narrative_generator import NarrativeReport
from explainability_engine import ExplainabilityReport

PERIOD_LENGTH_SECONDS = 2700.0  # 45 minutes -- see narrative_generator's identical minute-math assumption

GOAL_CUE_DURATION = 8.0
CARD_CUE_DURATION = 5.0
MOMENTUM_CUE_DURATION = 6.0

# Lower number = higher priority when a renderer must choose which cue to
# show if two overlap in time (e.g. a goal and a momentum shift in the same
# instant -- the goal should win).
CUE_PRIORITY = {"goal": 1, "card": 2, "momentum_shift": 3, "score_bug": 4}


@dataclass
class OverlayCue:
    cue_id: str
    cue_type: str  # "score_bug" | "goal" | "card" | "momentum_shift"
    period: int
    minute: int
    second_in_period: float
    absolute_second: float  # continuous match clock, period offsets applied
    duration_seconds: Optional[float]  # None = persistent until the next score_bug cue
    priority: int
    title: str
    subtitle: Optional[str]
    team: Optional[str]
    payload: dict = field(default_factory=dict)


@dataclass
class OverlayTimeline:
    match_id: str
    cues: list[OverlayCue] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """The actual wire format a graphics engine would consume."""
        return {
            "match_id": self.match_id,
            "cues": [
                {
                    "cue_id": c.cue_id, "cue_type": c.cue_type, "period": c.period,
                    "minute": c.minute, "second_in_period": c.second_in_period,
                    "absolute_second": c.absolute_second, "duration_seconds": c.duration_seconds,
                    "priority": c.priority, "title": c.title, "subtitle": c.subtitle,
                    "team": c.team, "payload": c.payload,
                }
                for c in self.cues
            ],
        }


def _absolute_second(period: int, second_in_period: float) -> float:
    return (period - 1) * PERIOD_LENGTH_SECONDS + second_in_period


class OverlayRenderer:
    """Agent 9. Consumes Validator's CDFDocument plus Narrative Generator's
    and Explainability Engine's reports for the SAME document, and produces
    a chronologically-sorted OverlayTimeline. Does not mutate its inputs."""

    def render(self, document: CDFDocument, narrative: NarrativeReport,
               explainability: ExplainabilityReport) -> OverlayTimeline:
        cues: list[OverlayCue] = []
        log: list[str] = []

        cues.extend(self._score_bug_cues(document))
        cues.extend(self._moment_cues(explainability))
        cues.extend(self._momentum_cues(explainability))

        cues.sort(key=lambda c: (c.absolute_second, c.priority))
        if not cues:
            log.append("no cues generated -- document had no events to render")

        return OverlayTimeline(match_id=document.match.match_id, cues=cues, log=log)

    def _score_bug_cues(self, document: CDFDocument) -> list[OverlayCue]:
        home, away = document.match.home_team, document.match.away_team
        cues = []
        home_goals = away_goals = 0

        cues.append(OverlayCue(
            cue_id="score_bug_kickoff", cue_type="score_bug", period=1, minute=0, second_in_period=0.0,
            absolute_second=0.0, duration_seconds=None, priority=CUE_PRIORITY["score_bug"],
            title=f"{home} 0 - 0 {away}", subtitle=None, team=None,
            payload={"home_team": home, "away_team": away, "home_goals": 0, "away_goals": 0},
        ))

        goal_events = sorted(
            (e for e in document.events if e.event_type == "goal"),
            key=lambda e: _absolute_second(e.period, e.timestamp),
        )
        for e in goal_events:
            if e.team == home:
                home_goals += 1
            elif e.team == away:
                away_goals += 1
            abs_sec = _absolute_second(e.period, e.timestamp)
            cues.append(OverlayCue(
                cue_id=f"score_bug_{e.event_id}", cue_type="score_bug", period=e.period,
                minute=int(e.timestamp // 60) + (45 if e.period == 2 else 0), second_in_period=e.timestamp,
                absolute_second=abs_sec, duration_seconds=None, priority=CUE_PRIORITY["score_bug"],
                title=f"{home} {home_goals} - {away_goals} {away}", subtitle=None, team=e.team,
                payload={"home_team": home, "away_team": away, "home_goals": home_goals, "away_goals": away_goals},
            ))
        return cues

    def _moment_cues(self, explainability: ExplainabilityReport) -> list[OverlayCue]:
        cues = []
        for i, em in enumerate(explainability.explained_moments):
            second_in_period = em.second_in_period
            abs_sec = _absolute_second(em.period, second_in_period)
            if em.event_type == "goal":
                cues.append(OverlayCue(
                    cue_id=f"goal_cue_{i}", cue_type="goal", period=em.period, minute=em.minute,
                    second_in_period=second_in_period, absolute_second=abs_sec,
                    duration_seconds=GOAL_CUE_DURATION, priority=CUE_PRIORITY["goal"],
                    title="GOAL!", subtitle=em.base_description, team=em.team,
                    payload={"context": em.context},
                ))
            elif em.event_type == "card":
                cues.append(OverlayCue(
                    cue_id=f"card_cue_{i}", cue_type="card", period=em.period, minute=em.minute,
                    second_in_period=second_in_period, absolute_second=abs_sec,
                    duration_seconds=CARD_CUE_DURATION, priority=CUE_PRIORITY["card"],
                    title="CARD", subtitle=em.base_description, team=em.team,
                    payload={"context": em.context},
                ))
        return cues

    def _momentum_cues(self, explainability: ExplainabilityReport) -> list[OverlayCue]:
        cues = []
        for i, shift in enumerate(explainability.momentum_shifts):
            window = next(
                (w for w in explainability.windows if w.period == shift.period and w.window_index == shift.to_window_index),
                None,
            )
            second_in_period = window.start_second if window else 0.0
            abs_sec = _absolute_second(shift.period, second_in_period)
            cues.append(OverlayCue(
                cue_id=f"momentum_cue_{i}", cue_type="momentum_shift", period=shift.period,
                minute=int(second_in_period // 60) + (45 if shift.period == 2 else 0),
                second_in_period=second_in_period, absolute_second=abs_sec,
                duration_seconds=MOMENTUM_CUE_DURATION, priority=CUE_PRIORITY["momentum_shift"],
                title="MOMENTUM SHIFT", subtitle=shift.description, team=shift.to_team,
                payload={"from_team": shift.from_team, "to_team": shift.to_team,
                         "from_share": shift.from_share, "to_share": shift.to_share},
            ))
        return cues


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

    detective = FormatDetective()
    mapper = SchemaMapper()
    translator = CoordinateTranslator()
    resolver = EntityResolver()
    validator = Validator()
    narrator = NarrativeGenerator()
    explainer = ExplainabilityEngine()
    renderer = OverlayRenderer()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Overlay Renderer -- test run (single-vendor ingestion, Opta only)")
    print("=" * 78)

    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        opta_file = match_dir / "opta.json"
        if not opta_file.exists():
            continue
        data = json.loads(opta_file.read_text())
        detection = detective.detect(data)
        mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
        translated = translator.translate(mapped)
        resolved, registry = resolver.resolve(translated)
        document, _ = validator.build_document(resolved, registry, {detection.detected_format: data})
        narrative = narrator.generate(document)
        explainability = explainer.generate(document)
        timeline = renderer.render(document, narrative, explainability)

        print(f"\n{match_dir.name}: {len(timeline.cues)} cues")
        for cue in timeline.cues[:10]:
            print(f"  [{cue.absolute_second:>6.0f}s] {cue.cue_type:16s} {cue.title} -- {cue.subtitle or ''}")
