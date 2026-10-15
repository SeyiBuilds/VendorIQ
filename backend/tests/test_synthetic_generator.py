"""Tests for the synthetic match simulator (synthetic_match_generator.py).

The sample matches are only useful as test data if they behave like
football: these check the simulator's own rules rather than exact numbers.
Covers: determinism per seed, every event player belongs to that team's
squad (no player for both sides, no real names), a sent-off player never
appears again, substitutes only appear after coming on, goals are followed
by a kickoff from the conceding side, coordinates stay on the pitch, the
match runs into the second half, card colour and shot outcome are set
exactly where they belong, and vendor files agree on goals and cards.
"""

import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "data" / "generators"))

import synthetic_match_generator as gen  # noqa: E402

SEEDS = [42, 101, 777, 5, 2024]


def _match(seed):
    sheet = gen.generate_match_sheet("Home FC", "Away FC", seed=seed + 100)
    return gen.generate_ground_truth("T", "Home FC", "Away FC", seed=seed, sheet=sheet), sheet


@pytest.mark.parametrize("seed", SEEDS)
def test_same_seed_same_match(seed):
    a, _ = _match(seed)
    b, _ = _match(seed)
    assert [(e.event_id, e.event_type, e.timestamp) for e in a.events] == \
           [(e.event_id, e.event_type, e.timestamp) for e in b.events]


@pytest.mark.parametrize("seed", SEEDS)
def test_players_belong_to_their_own_squad(seed):
    match, sheet = _match(seed)
    squads = {"Home FC": {p.name for p in sheet.home_lineup}, "Away FC": {p.name for p in sheet.away_lineup}}
    assert not squads["Home FC"] & squads["Away FC"]
    for e in match.events:
        assert e.player_name in squads[e.team], (e.event_type, e.team, e.player_name)


@pytest.mark.parametrize("seed", SEEDS)
def test_sent_off_players_take_no_further_part(seed):
    match, _ = _match(seed)
    order = {id(e): i for i, e in enumerate(match.events)}
    for card in (e for e in match.events if e.card_type in ("red", "second_yellow")):
        later = [e for e in match.events if order[id(e)] > order[id(card)] and e.player_name == card.player_name]
        assert not later, f"{card.player_name} sent off but appears again"


@pytest.mark.parametrize("seed", SEEDS)
def test_second_yellow_follows_a_first_yellow(seed):
    match, _ = _match(seed)
    seen_yellow = set()
    for e in match.events:
        if e.card_type == "yellow":
            seen_yellow.add(e.player_name)
        elif e.card_type == "second_yellow":
            assert e.player_name in seen_yellow


@pytest.mark.parametrize("seed", SEEDS)
def test_substitutes_only_appear_after_coming_on(seed):
    match, sheet = _match(seed)
    on_minute = {s.player_in: s.minute for s in sheet.substitutions}
    for e in match.events:
        if e.player_name in on_minute:
            minute = e.timestamp / 60 + (45 if e.period == 2 else 0)
            assert e.period == 2 and minute >= on_minute[e.player_name]


@pytest.mark.parametrize("seed", SEEDS)
def test_goal_is_followed_by_kickoff_from_the_conceding_side(seed):
    match, _ = _match(seed)
    events = match.events
    for i, e in enumerate(events[:-1]):
        if e.event_type == "goal":
            nxt = events[i + 1]
            if nxt.period != e.period:
                continue
            assert nxt.team != e.team
            assert (nxt.x, nxt.y) == (50.0, 50.0)


@pytest.mark.parametrize("seed", SEEDS)
def test_full_match_with_valid_coordinates_and_detail(seed):
    match, _ = _match(seed)
    assert {e.period for e in match.events} == {1, 2}
    assert max(e.timestamp for e in match.events if e.period == 2) > 44 * 60
    for e in match.events:
        assert 0 <= e.x <= 100 and 0 <= e.y <= 100
        if e.end_x is not None:
            assert 0 <= e.end_x <= 100 and 0 <= e.end_y <= 100
        assert (e.card_type is not None) == (e.event_type == "card")
        assert (e.shot_outcome is not None) == (e.event_type in ("shot", "goal"))
        if e.event_type == "goal":
            assert e.shot_outcome == "goal"


def test_averages_look_like_football():
    totals = Counter()
    n = 30
    for seed in range(n):
        match, _ = _match(seed)
        totals.update(e.event_type for e in match.events)
    per_match = {k: v / n for k, v in totals.items()}
    assert 1.5 <= per_match["goal"] <= 4.5
    assert 15 <= per_match["shot"] + per_match["goal"] <= 40
    assert 12 <= per_match["foul"] <= 30
    assert 600 <= per_match["pass"] <= 1400


def test_vendor_files_agree_on_goals_and_cards():
    bundle = gen.generate_match_bundle("T", "Home FC", "Away FC", "clean", seed=7)
    opta_goals = sum(1 for e in bundle["opta"]["Events"] if e["TypeName"] == "GOAL")
    sb_goals = sum(1 for e in bundle["statsbomb"]["events"]
                   if e.get("shot", {}).get("outcome", {}).get("name") == "Goal")
    wy_goals = sum(1 for e in bundle["wyscout"]["events"] if {"id": 101} in e.get("tags", []))
    assert opta_goals == sb_goals == wy_goals

    opta_cards = sum(1 for e in bundle["opta"]["Events"] if e["TypeName"] == "CARD")
    sb_cards = sum(1 for e in bundle["statsbomb"]["events"] if "bad_behaviour" in e)
    wy_cards = sum(1 for e in bundle["wyscout"]["events"]
                   if any(t["id"] in (1701, 1702, 1703) for t in e.get("tags", [])))
    assert opta_cards == sb_cards == wy_cards


def test_event_and_lineup_feeds_share_player_ids():
    bundle = gen.generate_match_bundle("T", "Home FC", "Away FC", "clean", seed=7)
    lineup_ids = {p["PlayerRef"] for team in bundle["opta_match_sheet"]["TeamData"] for p in team["MatchPlayer"]}
    event_ids = {e["PlayerRef"] for e in bundle["opta"]["Events"]}
    assert lineup_ids and event_ids <= lineup_ids
