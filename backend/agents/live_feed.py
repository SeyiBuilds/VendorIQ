"""
Live feed replay.

Feeds a complete vendor file into the pipeline the way a live match feed
would arrive: in chunks of match time, one match minute at a time. After
each chunk the FULL 7-agent pipeline re-runs on everything received so
far, so every intermediate update (score, stats, key moments, shot map) is
exactly what VendorIQ would output if the feed stopped at that minute. No
stat is interpolated or computed by a second code path.

How a raw event gets its match time without a second, vendor-specific
time parser: Schema Mapper maps every raw event dict to exactly one
MappedEvent, in order (see SchemaMapper.map_events). So the pipeline's own
Format Detective + Schema Mapper are run once on the whole file up front,
and mapped[i]'s (period, timestamp) is raw_events[i]'s match time. The
same vendor knowledge that powers a normal run decides when each event
"arrives".

Two orchestrators, on purpose:
  - live_orchestrator runs every intermediate update with AI escalation
    off. With ANTHROPIC_API_KEY set, AI-on would fire one LLM narrative
    call per match minute, which is slow and costly for text that is
    replaced a second later. Intermediate narratives are therefore always
    the deterministic template, and say so (generation_method="template").
  - final_orchestrator runs the last update (the complete feed) with the
    caller's normal settings, so the end state is identical to a regular
    run of the same file.

What this is NOT: a connection to a real live vendor socket. It replays a
finished file at a chosen speed. The chunking, the re-run per chunk and
the streamed updates are the same mechanics a real live integration would
use; only the source of the chunks differs.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterator, Optional

from coordinate_translator import CoordinateTranslator, detect_team_relative
from narrative_generator import PERIOD_START_MINUTE
from format_detective import FormatDetective
from schema_mapper import SchemaMapper


@dataclass
class FeedTick:
    """One live update: everything up to and including (period, minute)."""

    period: int
    minute: int          # match minute, same formula as Narrative Generator's key moments
    received: int        # raw events delivered so far (cumulative)
    total: int           # raw events in the whole feed
    new_events: int      # raw events delivered in this chunk


@dataclass
class FeedPlan:
    event_list_key: str
    raw_events: list          # the feed's event dicts, original vendor order
    arrival: list             # arrival[i] = (period, minute) for raw_events[i]
    ticks: list               # list[FeedTick], in match-time order
    # Direction of play, decided once from the whole file and applied to
    # every update: "team_relative" or "fixed" (see coordinate_translator).
    direction_convention: str = "fixed"


def match_minute(period: int, timestamp: float) -> int:
    """Mirrors narrative_generator.py (minute = timestamp // 60 plus the
    period's start: 45 in the second half, 90 and 105 in extra time) so a
    live clock minute always lines up with the minute on the key moments
    the same run produces."""
    return int(timestamp // 60) + PERIOD_START_MINUTE.get(period, 0)


def plan_feed(vendor_data: dict, detective: Optional[FormatDetective] = None,
              mapper: Optional[SchemaMapper] = None) -> Optional[FeedPlan]:
    """Work out when each raw event arrives. Returns None when the feed
    cannot be replayed (format not detected, or no events), so the caller
    can fall back to a normal single run instead of failing."""
    detective = detective or FormatDetective()
    mapper = mapper or SchemaMapper()

    detection = detective.detect(vendor_data)
    key = detection.event_list_key
    if not key or not isinstance(vendor_data.get(key), list):
        return None
    raw_events = [e for e in vendor_data[key] if isinstance(e, dict)]
    if not raw_events:
        return None

    mapped = mapper.map_events(vendor_data, detection.detected_format, key)
    if len(mapped) != len(raw_events):
        # The 1:1 guarantee is what makes this safe; refuse rather than guess.
        return None

    arrival = []
    for m in mapped:
        timestamp = m.timestamp if isinstance(m.timestamp, (int, float)) and m.timestamp >= 0 else 0.0
        if m.raw_metadata.get("penalty_shootout"):
            # Not match play (Validator keeps it out of the document): it
            # arrives after everything else so it never disturbs the clock.
            arrival.append((5, 120))
            continue
        # A missing/invalid period gets the exact repair Validator applies
        # (validator._repair_period: before 45:00 is the first half), so an
        # event's arrival minute always equals the minute it carries in the
        # finished CDF document. That equality is what lets each live update
        # ship only its own minute's events (see events_in_tick below).
        if isinstance(m.period, int) and 1 <= m.period <= 4:
            period = m.period
        else:
            period = 1 if timestamp < 2700 else 2
        arrival.append((period, match_minute(period, timestamp)))

    buckets = sorted(set(arrival))
    total = len(raw_events)
    ticks = []
    received = 0
    for period, minute in buckets:
        new = sum(1 for a in arrival if a == (period, minute))
        received += new
        ticks.append(FeedTick(period=period, minute=minute, received=received, total=total, new_events=new))

    # A partial feed early on has too few shots to read the direction of
    # play from, so decide it here from the whole file; every update then
    # uses the same frame (and so does the final, full run).
    translated = CoordinateTranslator().translate(mapped)
    convention = "team_relative" if detect_team_relative(translated) else "fixed"

    return FeedPlan(event_list_key=key, raw_events=raw_events, arrival=arrival, ticks=ticks,
                    direction_convention=convention)


def payload_up_to(vendor_data: dict, plan: FeedPlan, tick: FeedTick) -> dict:
    """The vendor file as it would look if the feed had only delivered
    events up to `tick`: same top-level fields (team names, match info),
    event list cut down to what has arrived, original vendor order kept."""
    cutoff = (tick.period, tick.minute)
    payload = {k: v for k, v in vendor_data.items() if k != plan.event_list_key}
    payload = copy.deepcopy(payload)
    payload[plan.event_list_key] = [
        copy.deepcopy(e) for e, a in zip(plan.raw_events, plan.arrival) if a <= cutoff
    ]
    return payload


def events_in_tick(document, tick: FeedTick) -> list:
    """The CDF events that arrived in this tick's chunk. Matched on match
    time, not event_id: placeholder ids for events with no vendor id are
    index-based and shift as the partial feed grows, while (period, minute)
    is fixed per event. Every validated event belongs to exactly one tick."""
    if document is None:
        return []
    return [
        e for e in document.events
        if (e.period, match_minute(e.period, e.timestamp)) == (tick.period, tick.minute)
    ]


def replay(vendor_data: dict, live_orchestrator, final_orchestrator,
           match_sheet_data: Optional[dict] = None) -> Iterator[tuple]:
    """Yields:
      ("plan", FeedPlan)                         -- once, before any update
      ("tick", FeedTick, PipelineResult)         -- one per match minute with events
      ("result", PipelineResult)                 -- exactly once, last; the complete feed

    A feed that cannot be planned is not an error: it yields a plan of None
    and then the normal single-run result, so the caller still gets the
    same honest outcome a regular run would give (including a failed
    Format Detective stage for an unrecognized shape)."""
    plan = plan_feed(vendor_data, live_orchestrator.detective, live_orchestrator.mapper)
    yield ("plan", plan)
    if plan is None:
        yield ("result", final_orchestrator.run(vendor_data, match_sheet_data=match_sheet_data))
        return

    for tick in plan.ticks[:-1]:
        partial = payload_up_to(vendor_data, plan, tick)
        yield ("tick", tick, live_orchestrator.run(partial, match_sheet_data=match_sheet_data,
                                                   direction_convention=plan.direction_convention))

    # Last chunk: the whole feed, through the normally configured pipeline.
    final = final_orchestrator.run(vendor_data, match_sheet_data=match_sheet_data,
                                   direction_convention=plan.direction_convention)
    yield ("tick", plan.ticks[-1], final)
    yield ("result", final)
