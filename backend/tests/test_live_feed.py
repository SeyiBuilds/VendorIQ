"""Tests for live feed replay (live_feed.py).

Covers: every raw event arrives exactly once and in match-time order, each
live update is a real pipeline run over only what has arrived (nothing
from a later minute leaks in), the per-minute event deltas add up to the
finished document exactly, the final update equals a normal run of the
whole file, missing period/timestamp values arrive where Validator will
place them, and a feed that cannot be split falls back to one honest
normal run instead of raising.
"""

import json

import pytest
from conftest import SAMPLE_MATCHES_DIR
from live_feed import events_in_tick, match_minute, payload_up_to, plan_feed, replay
from orchestrator import MasterOrchestrator

VENDORS = ("opta", "statsbomb", "wyscout")


@pytest.fixture
def orchestrator():
    return MasterOrchestrator(ai_escalation_enabled=False)


def _vendor_data(vendor="opta", match_name="PL_2025_CLEAN_001"):
    return json.loads((SAMPLE_MATCHES_DIR / match_name / f"{vendor}.json").read_text())


def _all_match_names():
    return [d.name for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


def _event_minute(e):
    return (e.period, match_minute(e.period, e.timestamp))


@pytest.mark.parametrize("vendor", VENDORS)
def test_plan_covers_every_raw_event_once_in_match_time_order(vendor):
    data = _vendor_data(vendor)
    plan = plan_feed(data)
    assert plan is not None
    assert len(plan.raw_events) == len(plan.arrival)
    keys = [(t.period, t.minute) for t in plan.ticks]
    assert keys == sorted(set(keys)), "one update per match minute, in order"
    assert sum(t.new_events for t in plan.ticks) == plan.ticks[-1].total == len(plan.raw_events)
    received = [t.received for t in plan.ticks]
    assert received == sorted(received) and received[-1] == len(plan.raw_events)


def test_match_minute_mirrors_narrative_generator():
    assert match_minute(1, 0.0) == 0
    assert match_minute(1, 209.3) == 3
    assert match_minute(2, 0.0) == 45
    assert match_minute(2, 125.0) == 47


def test_payload_up_to_keeps_top_level_fields_and_vendor_order():
    data = _vendor_data("opta")
    plan = plan_feed(data)
    tick = plan.ticks[len(plan.ticks) // 2]
    partial = payload_up_to(data, plan, tick)

    for key, value in data.items():
        if key != plan.event_list_key:
            assert partial[key] == value
    events = partial[plan.event_list_key]
    assert len(events) == tick.received
    original_positions = [plan.raw_events.index(e) for e in events]
    assert original_positions == sorted(original_positions)
    # The source file itself is never mutated.
    assert len(data[plan.event_list_key]) == len(plan.raw_events)


@pytest.mark.parametrize("match_name", _all_match_names())
@pytest.mark.parametrize("vendor", VENDORS)
def test_every_update_only_contains_events_that_have_arrived(orchestrator, match_name, vendor):
    data = _vendor_data(vendor, match_name)
    previous_count = -1
    for item in replay(data, orchestrator, orchestrator):
        if item[0] != "tick":
            continue
        tick, result = item[1], item[2]
        assert result.orchestration_log.overall_status == "success"
        events = result.document.events
        assert all(_event_minute(e) <= (tick.period, tick.minute) for e in events)
        assert len(events) >= previous_count
        previous_count = len(events)


@pytest.mark.parametrize("vendor", VENDORS)
def test_per_minute_deltas_add_up_to_the_finished_document(orchestrator, vendor):
    data = _vendor_data(vendor, "PL_2025_MESSY_001")
    delivered = []
    final = None
    for item in replay(data, orchestrator, orchestrator):
        if item[0] == "tick":
            delivered.extend(events_in_tick(item[2].document, item[1]))
        elif item[0] == "result":
            final = item[1]
    final_keys = sorted((e.period, e.timestamp, e.event_type, e.team or "") for e in final.document.events)
    delivered_keys = sorted((e.period, e.timestamp, e.event_type, e.team or "") for e in delivered)
    assert delivered_keys == final_keys


def test_score_builds_up_as_goals_arrive(orchestrator):
    data = _vendor_data("opta", "PL_2025_CLEAN_001")
    goals_in_file = sum(1 for e in data["Events"] if e.get("TypeName") == "GOAL")
    assert goals_in_file > 0, "fixture should contain goals"
    totals = []
    for item in replay(data, orchestrator, orchestrator):
        if item[0] == "tick":
            stats = item[2].narrative.team_stats
            totals.append(sum(s.goals for s in stats.values()))
    assert totals == sorted(totals), "goal count never goes down as the feed advances"
    assert totals[0] == 0
    assert totals[-1] == goals_in_file


def test_final_update_matches_a_normal_run_of_the_whole_file(orchestrator):
    data = _vendor_data("statsbomb", "PL_2025_PARTIAL_001")
    normal = orchestrator.run(data)
    last_tick_result = None
    final = None
    for item in replay(data, orchestrator, orchestrator):
        if item[0] == "tick":
            last_tick_result = item[2]
        elif item[0] == "result":
            final = item[1]
    assert final is last_tick_result
    assert len(final.document.events) == len(normal.document.events)
    assert {t: s.goals for t, s in final.narrative.team_stats.items()} == {
        t: s.goals for t, s in normal.narrative.team_stats.items()
    }


def test_intermediate_updates_use_the_live_orchestrator_and_final_uses_the_other():
    calls = []

    class Recording(MasterOrchestrator):
        def __init__(self, name):
            super().__init__(ai_escalation_enabled=False)
            self.name = name

        def run(self, *args, **kwargs):
            calls.append(self.name)
            return super().run(*args, **kwargs)

    data = _vendor_data("opta")
    items = list(replay(data, Recording("live"), Recording("final")))
    ticks = [i for i in items if i[0] == "tick"]
    assert calls[:-1] == ["live"] * (len(ticks) - 1)
    assert calls[-1] == "final"


def test_missing_period_and_timestamp_arrive_where_validator_places_them(orchestrator):
    data = _vendor_data("opta")
    events = data["Events"]
    events[0].pop("PeriodId", None)          # validator infers period from timestamp
    events[1].pop("Min", None)               # timestamp defaults to 0.0
    events[1].pop("Sec", None)
    plan = plan_feed(data)
    assert plan is not None

    delivered = 0
    final = None
    for item in replay(data, orchestrator, orchestrator):
        if item[0] == "tick":
            delivered += len(events_in_tick(item[2].document, item[1]))
        elif item[0] == "result":
            final = item[1]
    assert delivered == len(final.document.events)


def test_unrecognized_feed_falls_back_to_one_normal_run(orchestrator):
    feed = {"something": "not a vendor feed"}
    items = list(replay(feed, orchestrator, orchestrator))
    assert items[0] == ("plan", None)
    assert [i[0] for i in items] == ["plan", "result"]
    normal = orchestrator.run(feed)
    assert items[-1][1].orchestration_log.overall_status == normal.orchestration_log.overall_status


def test_feed_with_empty_event_list_is_not_replayable():
    data = _vendor_data("opta")
    data["Events"] = []
    assert plan_feed(data) is None


def test_mid_match_narrative_never_calls_the_score_final():
    live = MasterOrchestrator(ai_escalation_enabled=False, narrative_in_progress=True)
    final = MasterOrchestrator(ai_escalation_enabled=False)
    data = _vendor_data("opta")
    ticks = [i for i in replay(data, live, final) if i[0] == "tick"]
    for _, _, result in ticks[:-1]:
        assert "score so far" in result.narrative.narrative_text
        assert "final score" not in result.narrative.narrative_text
    assert "final score" in ticks[-1][2].narrative.narrative_text
