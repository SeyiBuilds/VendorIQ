"""
Agent 8: Explainability Engine

Pipeline stage (c) from the hackathon brief: "Explain why a moment matters
rather than only reporting that it happened." This is explicitly called
out as a separate judged criterion from narrative generation (stage iii) --
a narrative says a goal happened and who scored it; this agent says
whether that goal came from sustained pressure or against the run of
play, and surfaces the broader control-vs-chaos and momentum patterns the
brief names directly: "control vs. chaos, tactical pressure changes, and
game rhythm patterns."

Approach: rule-based and deterministic, same engineering spirit as every
other agent in this pipeline -- no LLM call needed to say "team X had 71%
of the ball-event share in the last five minutes," that's arithmetic. The
match timeline is split into fixed windows (default 5 minutes, the
granularity a broadcast graphic would actually use), each window is
classified by territorial control and foul/tackle density ("chaos"), and
consecutive windows are compared to detect momentum shifts. Each goal/card
key moment (from Narrative Generator's extract_key_moments) is then
explained in the context of the window it fell in.

This agent is vendor-blind, same as Narrative Generator: it only reads
normalized CDFEvent fields (team, event_type, timestamp, period), so it
runs identically regardless of which vendor(s) fed the match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cdf_models import CDFDocument, CDFEvent
from narrative_generator import extract_key_moments, KeyMoment

WINDOW_SECONDS = 300.0  # 5-minute windows -- standard broadcast-graphic granularity
CONTROL_SHARE_THRESHOLD = 0.65   # dominant team's event share at/above this -> "control"
CHAOS_RATIO_THRESHOLD = 0.35     # (fouls+tackles+cards)/total events at/above this -> "chaotic"
MOMENTUM_SHIFT_MIN_SHARE = 0.55  # both windows must show at least this much dominance to count as a real shift

CHAOS_EVENT_TYPES = {"foul", "tackle", "card"}


@dataclass
class RhythmWindow:
    period: int
    window_index: int
    start_second: float
    end_second: float
    team_event_counts: dict[str, int] = field(default_factory=dict)
    total_events: int = 0
    dominant_team: Optional[str] = None
    control_share: float = 0.0
    chaos_events: int = 0
    chaos_ratio: float = 0.0
    rhythm_label: str = "contested"  # "control" | "contested" | "chaotic"


@dataclass
class MomentumShift:
    period: int
    from_window_index: int
    to_window_index: int
    from_team: Optional[str]
    to_team: Optional[str]
    from_share: float
    to_share: float
    description: str


@dataclass
class ExplainedMoment:
    minute: int
    period: int
    second_in_period: float
    event_type: str
    team: Optional[str]
    base_description: str
    context: str


@dataclass
class ExplainabilityReport:
    windows: list[RhythmWindow] = field(default_factory=list)
    momentum_shifts: list[MomentumShift] = field(default_factory=list)
    explained_moments: list[ExplainedMoment] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def _classify_rhythm(control_share: float, chaos_ratio: float) -> str:
    # Chaos takes priority: a foul-heavy, broken-up stretch is worth
    # flagging even if one team still nominally "controls" more events in
    # it -- a stop-start five minutes isn't really "control" in the sense
    # a viewer means it.
    if chaos_ratio >= CHAOS_RATIO_THRESHOLD:
        return "chaotic"
    if control_share >= CONTROL_SHARE_THRESHOLD:
        return "control"
    return "contested"


def _build_windows(document: CDFDocument) -> list[RhythmWindow]:
    teams = {document.match.home_team, document.match.away_team}
    by_period: dict[int, list[CDFEvent]] = {}
    for e in document.events:
        by_period.setdefault(e.period, []).append(e)

    windows: list[RhythmWindow] = []
    for period, events in sorted(by_period.items()):
        max_t = max((e.timestamp for e in events), default=0.0)
        n_windows = int(max_t // WINDOW_SECONDS) + 1
        for idx in range(n_windows):
            start, end = idx * WINDOW_SECONDS, (idx + 1) * WINDOW_SECONDS
            window_events = [e for e in events if start <= e.timestamp < end]
            w = RhythmWindow(period=period, window_index=idx, start_second=start, end_second=end)
            if not window_events:
                windows.append(w)
                continue

            for team in teams:
                count = sum(1 for e in window_events if e.team == team)
                if count:
                    w.team_event_counts[team] = count
            w.total_events = len(window_events)
            w.chaos_events = sum(1 for e in window_events if e.event_type in CHAOS_EVENT_TYPES)
            w.chaos_ratio = round(w.chaos_events / w.total_events, 3) if w.total_events else 0.0

            if w.team_event_counts:
                w.dominant_team = max(w.team_event_counts, key=w.team_event_counts.get)
                w.control_share = round(w.team_event_counts[w.dominant_team] / w.total_events, 3)
            w.rhythm_label = _classify_rhythm(w.control_share, w.chaos_ratio)
            windows.append(w)

    return windows


def _detect_momentum_shifts(windows: list[RhythmWindow]) -> list[MomentumShift]:
    shifts: list[MomentumShift] = []
    by_period: dict[int, list[RhythmWindow]] = {}
    for w in windows:
        by_period.setdefault(w.period, []).append(w)

    for period, period_windows in by_period.items():
        period_windows.sort(key=lambda w: w.window_index)
        for prev, curr in zip(period_windows, period_windows[1:]):
            if prev.dominant_team is None or curr.dominant_team is None:
                continue
            if prev.dominant_team == curr.dominant_team:
                continue
            if prev.control_share < MOMENTUM_SHIFT_MIN_SHARE or curr.control_share < MOMENTUM_SHIFT_MIN_SHARE:
                continue
            start_min = int(curr.start_second // 60) + (45 if period == 2 else 0)
            shifts.append(MomentumShift(
                period=period, from_window_index=prev.window_index, to_window_index=curr.window_index,
                from_team=prev.dominant_team, to_team=curr.dominant_team,
                from_share=prev.control_share, to_share=curr.control_share,
                description=(
                    f"Momentum swung from {prev.dominant_team} control ({round(prev.control_share*100)}% event share) "
                    f"to {curr.dominant_team} ({round(curr.control_share*100)}%) around the {start_min}' mark"
                ),
            ))
    return shifts


def _window_for_event(windows: list[RhythmWindow], period: int, timestamp: float) -> Optional[RhythmWindow]:
    idx = int(timestamp // WINDOW_SECONDS)
    return next((w for w in windows if w.period == period and w.window_index == idx), None)


def _explain_moment(moment: KeyMoment, window: Optional[RhythmWindow]) -> str:
    if window is None or window.dominant_team is None:
        return "occurred with no clear territorial pattern in the surrounding window (insufficient data in this stretch)"

    if window.rhythm_label == "chaotic":
        return (
            f"arrived amid a chaotic, foul-heavy stretch ({round(window.chaos_ratio*100)}% of events in this window "
            "were fouls, tackles, or cards) rather than a settled passage of play"
        )

    if moment.team == window.dominant_team and window.rhythm_label == "control":
        return (
            f"came during a sustained {moment.team} control spell "
            f"({round(window.control_share*100)}% of event share in the preceding/surrounding window)"
        )

    if moment.team is not None and moment.team != window.dominant_team:
        return (
            f"came against the run of play -- {window.dominant_team} held "
            f"{round(window.control_share*100)}% of event share in this window"
        )

    return "occurred in a closely contested spell with no clear territorial dominance"


class ExplainabilityEngine:
    """Agent 8. Consumes a validated CDFDocument (Agent 5's output) and
    produces rhythm/control analysis plus moment-level 'why it matters'
    explanations. Does not mutate the document."""

    def generate(self, document: CDFDocument) -> ExplainabilityReport:
        log: list[str] = []
        windows = _build_windows(document)
        if not windows:
            log.append("no events in document -- no rhythm windows could be built")
        shifts = _detect_momentum_shifts(windows)

        key_moments = extract_key_moments(document)
        explained: list[ExplainedMoment] = []
        for m in key_moments:
            window = _window_for_event(windows, m.period, m.second_in_period)
            context = _explain_moment(m, window)
            explained.append(ExplainedMoment(
                minute=m.minute, period=m.period, second_in_period=m.second_in_period,
                event_type=m.event_type, team=m.team,
                base_description=m.description, context=context,
            ))

        return ExplainabilityReport(windows=windows, momentum_shifts=shifts, explained_moments=explained, log=log)


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
    engine = ExplainabilityEngine()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Explainability Engine -- test run (single-vendor ingestion, Opta only)")
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

        report = engine.generate(document)
        print(f"\n{match_dir.name}: {len(report.windows)} windows, {len(report.momentum_shifts)} momentum shifts")
        rhythm_counts = {}
        for w in report.windows:
            rhythm_counts[w.rhythm_label] = rhythm_counts.get(w.rhythm_label, 0) + 1
        print(f"  rhythm distribution: {rhythm_counts}")
        for shift in report.momentum_shifts[:3]:
            print(f"  shift: {shift.description}")
        for em in report.explained_moments[:5]:
            print(f"  {em.minute}' {em.base_description} -- {em.context}")
