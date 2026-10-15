"""Tests for shapes real vendor files take that the synthetic samples don't.

Covers: Wyscout files that bundle teams and players as id lookups (team
and player names read by id, escaped accents decoded, playerId 0 treated as
no player, home/away from the teams list, numeric match ids), duel outcome
tags, an event with no team no longer breaking the narrative, and the
direction of play: team-relative positions are detected from where each
team shoots and converted to one frame, fixed-frame feeds are left alone,
and too little evidence keeps positions as delivered.
"""

import json

from conftest import SAMPLE_MATCHES_DIR
from coordinate_translator import detect_team_relative, normalize_attack_direction
from orchestrator import MasterOrchestrator
from schema_mapper import SchemaMapper, MappedEvent

mapper = SchemaMapper()


def _wyscout_file(events, side=False):
    teams = {
        "10": {"team": {"wyId": 10, "name": "Home Side"}},
        "20": {"team": {"wyId": 20, "name": "Away Side"}},
    }
    if side:
        teams["10"]["side"] = "away"
        teams["20"]["side"] = "home"
    players = {
        "10": [{"player": {"wyId": 1, "shortName": "A. Chiriche\\u0219"}}],
        "20": [{"player": {"wyId": 2, "firstName": "Bo", "lastName": "Berg"}}],
    }
    return {"events": events, "teams": teams, "players": players}


def _wy_event(i, team_id, player_id, sub_event="Simple pass", x=50, y=50, period="1H", sec=10.0, tags=()):
    return {"eventId": i, "subEventName": sub_event, "teamId": team_id, "playerId": player_id,
            "matchId": 999, "matchPeriod": period, "eventSec": sec,
            "positions": [{"x": x, "y": y}, {"x": x + 5, "y": y}], "tags": [{"id": t} for t in tags]}


def test_wyscout_bundled_teams_and_players_are_read_by_id():
    data = _wyscout_file([_wy_event(1, 10, 1), _wy_event(2, 20, 2), _wy_event(3, 20, 0)])
    mapped = mapper.map_events(data, "wyscout", "events")
    assert [m.team for m in mapped] == ["Home Side", "Away Side", "Away Side"]
    assert mapped[0].raw_player_ref["name"] == "A. Chiricheș"
    assert mapped[1].raw_player_ref["name"] == "Bo Berg"
    assert mapped[2].raw_player_ref["vendor_id"] is None  # playerId 0 = no player


def test_wyscout_duel_outcome_tags():
    data = _wyscout_file([
        _wy_event(1, 10, 1, "Ground defending duel", tags=(703,)),
        _wy_event(2, 20, 2, "Ground attacking duel", tags=(701,)),
    ])
    mapped = mapper.map_events(data, "wyscout", "events")
    assert [m.outcome for m in mapped] == [True, False]


def test_wyscout_match_info_from_teams_list_and_numeric_match_id():
    result = MasterOrchestrator(ai_escalation_enabled=False).run(_wyscout_file([_wy_event(1, 10, 1), _wy_event(2, 20, 2)]))
    match = result.document.match
    assert (match.match_id, match.home_team, match.away_team) == ("999", "Home Side", "Away Side")
    assert any("assigned from the order" in w for w in result.document.metadata.warnings)

    with_sides = MasterOrchestrator(ai_escalation_enabled=False).run(
        _wyscout_file([_wy_event(1, 10, 1), _wy_event(2, 20, 2)], side=True))
    assert (with_sides.document.match.home_team, with_sides.document.match.away_team) == ("Away Side", "Home Side")


def test_event_without_a_team_does_not_break_the_narrative():
    data = {"events": [
        {"eventId": 1, "subEventName": "Simple pass", "matchPeriod": "1H", "eventSec": 5.0,
         "playerId": 7, "positions": [{"x": 40, "y": 50}], "tags": [{"id": 1801}]},
    ]}
    result = MasterOrchestrator(ai_escalation_enabled=False).run(data)
    assert result.orchestration_log.overall_status == "success"
    assert "unknown_team" in result.narrative.team_stats


# --- Direction of play ------------------------------------------------------

def _mapped(team, period, x, event_type="shot", y=50.0):
    return MappedEvent(event_id=f"{team}{period}{x}", event_type=event_type, event_type_raw=event_type,
                       timestamp=10.0, period=period, team=team, raw_player_ref={}, x=x, y=y,
                       end_x=None, end_y=None, outcome=True, vendor_source="wyscout",
                       mapping_confidence=1.0, mapping_attempts=1)


def _relative_match():
    events = []
    for team in ("H", "A"):
        for period in (1, 2):
            events += [_mapped(team, period, x) for x in (80, 85, 90)]
    return events


def test_team_relative_feed_is_detected_and_converted():
    events = _relative_match()
    assert detect_team_relative(events) is True
    out, log = normalize_attack_direction(events, "H")
    by_group = {}
    for e in out:
        by_group.setdefault((e.team, e.period), []).append(e.x)
    assert min(by_group[("H", 1)]) >= 80 and max(by_group[("H", 2)]) <= 20
    assert max(by_group[("A", 1)]) <= 20 and min(by_group[("A", 2)]) >= 80
    assert any("team-relative" in line for line in log)
    assert events[3].x == 80  # input not mutated


def test_fixed_frame_feed_is_left_alone():
    events = [_mapped("H", 1, x) for x in (80, 85, 90)] + [_mapped("A", 1, x) for x in (10, 15, 20)]
    assert detect_team_relative(events) is False
    out, log = normalize_attack_direction(events, "H")
    assert out == events and log == []


def test_too_few_shots_keeps_positions_as_delivered():
    events = [_mapped("H", 1, 85), _mapped("A", 1, 85)]
    assert detect_team_relative(events) is None
    out, log = normalize_attack_direction(events, "H")
    assert out == events and "kept as delivered" in log[0]


def test_sample_matches_are_fixed_frame_and_unchanged():
    orchestrator = MasterOrchestrator(ai_escalation_enabled=False)
    for vendor in ("opta", "statsbomb", "wyscout"):
        data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / f"{vendor}.json").read_text())
        warnings = orchestrator.run(data).document.metadata.warnings
        assert not any("team-relative" in w for w in warnings)


def test_team_relative_version_of_a_sample_converts_back_to_the_original():
    orchestrator = MasterOrchestrator(ai_escalation_enabled=False)
    data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "statsbomb.json").read_text())
    original = orchestrator.run(data).document
    home = original.match.home_team

    relative = json.loads(json.dumps(data))
    for e in relative["events"]:
        attacking_right = (e["team"]["name"] == home) == (e.get("period") == 1)
        if not attacking_right:
            e["location"] = [100 - e["location"][0], 100 - e["location"][1]]
            for key in ("pass", "shot"):
                end = (e.get(key) or {}).get("end_location")
                if end:
                    e[key]["end_location"] = [100 - end[0], 100 - end[1]] + end[2:]
    converted = orchestrator.run(relative).document
    assert any("team-relative" in w for w in converted.metadata.warnings)
    for a, b in zip(original.events, converted.events):
        assert abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) < 1e-6


def test_wyscout_placeholder_shot_end_is_dropped():
    data = _wyscout_file([
        {**_wy_event(1, 10, 1, "Shot", tags=(1802,)), "positions": [{"x": 88, "y": 50}, {"x": 100, "y": 100}]},
        {**_wy_event(2, 10, 1, "Shot", tags=(1801,)), "positions": [{"x": 88, "y": 50}, {"x": 0, "y": 0}]},
        {**_wy_event(3, 10, 1, "Simple pass"), "positions": [{"x": 50, "y": 50}, {"x": 100, "y": 100}]},
    ])
    mapped = mapper.map_events(data, "wyscout", "events")
    assert (mapped[0].end_x, mapped[0].end_y) == (None, None)
    assert (mapped[1].end_x, mapped[1].end_y) == (None, None)
    assert (mapped[2].end_x, mapped[2].end_y) == (100, 100)  # only shots carry the placeholder


def test_vendor_id_with_name_ranks_between_name_match_and_unknown():
    result = MasterOrchestrator(ai_escalation_enabled=False).run(
        _wyscout_file([_wy_event(1, 10, 1), _wy_event(2, 20, 2), _wy_event(3, 20, 99)]))
    by_name = {p.name: p for p in result.document.players}
    assert by_name["A. Chiricheș"].resolution_confidence == 0.7
    assert by_name["A. Chiricheș"].player_id.startswith("vendor::wyscout::Home Side::")
    unknown = [p for p in result.document.players if p.name is None]
    assert unknown and unknown[0].resolution_confidence == 0.2  # id with no name stays a fallback


def test_extra_time_minutes_and_penalty_shootout_kept_out_of_the_score():
    from live_feed import plan_feed
    events = [
        _wy_event(1, 10, 1, "Shot", period="1H", sec=60.0, tags=(101, 1801)),
        _wy_event(2, 20, 2, "Shot", period="E1", sec=120.0, tags=(101, 1801)),
        _wy_event(3, 10, 1, "Penalty", period="P", sec=30.0, tags=(101, 1801)),
        _wy_event(4, 20, 2, "Penalty", period="P", sec=90.0, tags=(1802,)),
    ]
    result = MasterOrchestrator(ai_escalation_enabled=False).run(_wyscout_file(events))
    goals = {team: s.goals for team, s in result.narrative.team_stats.items()}
    assert goals == {"Home Side": 1, "Away Side": 1}  # shootout goal not counted
    assert len(result.document.events) == 2
    assert any("shootout" in w for w in result.document.metadata.warnings)
    et_goal = next(m for m in result.narrative.key_moments if m.team == "Away Side")
    assert et_goal.period == 3 and et_goal.minute == 92  # 90' + 2 minutes into extra time

    plan = plan_feed(_wyscout_file(events))
    assert (plan.ticks[-1].period, plan.ticks[-1].minute) == (5, 120)  # shootout arrives last
