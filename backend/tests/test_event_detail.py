"""Tests for event detail: card colour and shot outcome.

Covers: each vendor's own encoding maps to the same CDF vocabulary (Opta
shot event types and card qualifiers, StatsBomb shot.outcome and card
objects, Wyscout tags), goals recorded as shots (StatsBomb, Wyscout) become
CDF goals, cards riding on fouls are counted once, key moments name the
card colour (and never guess one), shots on target follow the shot outcome,
and export -> re-ingest keeps card colour and shot outcome for every vendor.
"""

import json

import pytest
from conftest import SAMPLE_MATCHES_DIR
from schema_mapper import SchemaMapper
from orchestrator import MasterOrchestrator

mapper = SchemaMapper()


def _opta(type_name, qualifiers=None, outcome=1):
    raw = {"EventId": "e1", "TypeName": type_name, "Min": 10, "Sec": 0, "PeriodId": 1,
           "PlayerRef": "p1", "TeamName": "Home FC", "X": 100, "Y": 40, "Outcome": outcome}
    if qualifiers:
        raw["Qualifiers"] = [{"QualifierId": q} for q in qualifiers]
    return mapper.map_events({"Events": [raw]}, "opta", "Events")[0]


def _statsbomb(raw_extra):
    raw = {"id": "e1", "timestamp": "00:10:00.00", "period": 1, "player": {"id": 1},
           "team": {"name": "Home FC"}, "location": [90, 50]}
    raw.update(raw_extra)
    return mapper.map_events({"events": [raw]}, "statsbomb", "events")[0]


def _wyscout(sub_event, tags=(), accurate=None):
    raw = {"eventId": "e1", "subEventName": sub_event, "matchPeriod": "1H", "eventSec": 600.0,
           "trackable_object": "player_1", "teamName": "Home FC", "positions": [{"x": 90, "y": 50}]}
    if tags:
        raw["tags"] = [{"id": t} for t in tags]
    if accurate is not None:
        raw["accurate"] = accurate
    return mapper.map_events({"events": [raw]}, "wyscout", "events")[0]


# --- Opta -------------------------------------------------------------------

@pytest.mark.parametrize("type_name, qualifiers, event_type, shot_outcome", [
    ("MISS", None, "shot", "off_target"),
    ("POST", None, "shot", "post"),
    ("ATTEMPT_SAVED", None, "shot", "saved"),
    ("Attempt Saved", [82], "shot", "blocked"),
    ("GOAL", None, "goal", "goal"),
    ("SHOT", None, "shot", None),
])
def test_opta_shot_types_map_to_shot_outcome(type_name, qualifiers, event_type, shot_outcome):
    e = _opta(type_name, qualifiers)
    assert (e.event_type, e.shot_outcome) == (event_type, shot_outcome)


@pytest.mark.parametrize("qualifier, card_type", [(31, "yellow"), (32, "second_yellow"), (33, "red")])
def test_opta_card_qualifiers(qualifier, card_type):
    e = _opta("CARD", [qualifier])
    assert e.event_type == "card"
    assert e.card_type == card_type


def test_opta_card_without_qualifier_has_no_colour():
    assert _opta("CARD").card_type is None


# --- StatsBomb --------------------------------------------------------------

@pytest.mark.parametrize("outcome_name, event_type, shot_outcome, on_target", [
    ("Goal", "goal", "goal", True),
    ("Saved", "shot", "saved", True),
    ("Off T", "shot", "off_target", False),
    ("Blocked", "shot", "blocked", False),
    ("Post", "shot", "post", False),
    ("Wayward", "shot", "off_target", False),
])
def test_statsbomb_shot_outcomes(outcome_name, event_type, shot_outcome, on_target):
    e = _statsbomb({"type": {"name": "Shot"}, "shot": {"outcome": {"name": outcome_name},
                                                       "end_location": [120, 40, 1.2]}})
    assert (e.event_type, e.shot_outcome, e.outcome) == (event_type, shot_outcome, on_target)
    assert (e.end_x, e.end_y) == (120, 40)  # height value ignored


@pytest.mark.parametrize("container, type_name, event_type", [
    ("bad_behaviour", "Bad Behaviour", "card"),
    ("foul_committed", "Foul Committed", "foul"),
])
@pytest.mark.parametrize("card_name, card_type", [
    ("Yellow Card", "yellow"), ("Second Yellow", "second_yellow"), ("Red Card", "red"),
])
def test_statsbomb_cards(container, type_name, event_type, card_name, card_type):
    e = _statsbomb({"type": {"name": type_name}, container: {"card": {"name": card_name}}})
    assert e.event_type == event_type
    assert e.card_type == card_type


def test_statsbomb_set_piece_passes_and_pass_outcome_convention():
    corner = _statsbomb({"type": {"name": "Pass"}, "pass": {"type": {"name": "Corner"}, "end_location": [110, 40]}})
    assert corner.event_type == "corner"
    completed = _statsbomb({"type": {"name": "Pass"}, "pass": {"end_location": [60, 40]}})
    assert completed.event_type == "pass" and completed.outcome is True  # no outcome = complete
    failed = _statsbomb({"type": {"name": "Pass"}, "pass": {"end_location": [60, 40], "outcome": {"name": "Incomplete"}}})
    assert failed.outcome is False


def test_statsbomb_duel_and_goal_keeper_aliases():
    assert _statsbomb({"type": {"name": "Duel"}, "duel": {"outcome": {"name": "Won"}}}).event_type == "tackle"
    assert _statsbomb({"type": {"name": "Goal Keeper"}}).event_type == "save"


# --- Wyscout ----------------------------------------------------------------

@pytest.mark.parametrize("tags, event_type, shot_outcome", [
    ((101, 1801), "goal", "goal"),
    ((1801,), "shot", "saved"),
    ((1802,), "shot", "off_target"),
    ((2101, 1802), "shot", "blocked"),
    ((), "shot", None),
])
def test_wyscout_shot_tags(tags, event_type, shot_outcome):
    e = _wyscout("Shot", tags)
    assert (e.event_type, e.shot_outcome) == (event_type, shot_outcome)


@pytest.mark.parametrize("tag, card_type", [(1702, "yellow"), (1703, "second_yellow"), (1701, "red")])
def test_wyscout_card_tags_on_a_foul(tag, card_type):
    e = _wyscout("Foul", (tag,))
    assert e.event_type == "foul"
    assert e.card_type == card_type


@pytest.mark.parametrize("sub_event, event_type", [
    ("Simple pass", "pass"), ("High pass", "pass"), ("Ground defending duel", "tackle"),
    ("Ground attacking duel", "dribble"), ("Throw in", "throw_in"), ("Goal kick", "goal_kick"),
    ("Save attempt", "save"), ("Free kick shot", "shot"),
])
def test_wyscout_real_sub_event_names(sub_event, event_type):
    assert _wyscout(sub_event).event_type == event_type


def test_wyscout_accuracy_tags_when_no_accurate_field():
    assert _wyscout("Simple pass", (1801,)).outcome is True
    assert _wyscout("Simple pass", (1802,)).outcome is False


# --- Through the pipeline ---------------------------------------------------

def _run(vendor_data):
    return MasterOrchestrator(ai_escalation_enabled=False).run(vendor_data)


def _opta_feed(events):
    return {"MatchId": "T1", "HomeTeam": "Home FC", "AwayTeam": "Away FC", "Events": events}


def _opta_event(i, type_name, team="Home FC", qualifiers=None, outcome=1, minute=10, jersey=9):
    raw = {"EventId": f"e{i}", "TypeName": type_name, "Min": minute, "Sec": i, "PeriodId": 1,
           "PlayerRef": f"p{jersey}", "JerseyNumber": jersey, "TeamName": team, "X": 100, "Y": 40, "Outcome": outcome}
    if qualifiers:
        raw["Qualifiers"] = [{"QualifierId": q} for q in qualifiers]
    return raw


def test_key_moments_name_the_card_colour_and_never_guess():
    result = _run(_opta_feed([
        _opta_event(1, "CARD", qualifiers=[31]),
        _opta_event(2, "CARD", qualifiers=[33], team="Away FC"),
        _opta_event(3, "CARD"),
        _opta_event(4, "PASS", team="Away FC"),
    ]))
    descriptions = [m.description for m in result.narrative.key_moments]
    assert descriptions[0].startswith("Yellow card for Home FC")
    assert descriptions[1].startswith("Red card, sent off for Away FC")
    assert descriptions[2].startswith("Card (colour not in feed) for Home FC")
    stats = result.narrative.team_stats
    assert (stats["Home FC"].cards, stats["Home FC"].yellow_cards, stats["Home FC"].red_cards) == (2, 1, 0)
    assert (stats["Away FC"].cards, stats["Away FC"].red_cards) == (1, 1)


def test_card_riding_on_a_foul_is_one_card_and_one_key_moment():
    feed = {"events": [
        {"eventId": "f1", "subEventName": "Foul", "matchPeriod": "1H", "eventSec": 600.0,
         "trackable_object": "player_4", "jerseyNumber": 4, "teamName": "Home FC",
         "positions": [{"x": 40, "y": 50}], "tags": [{"id": 1702}]},
        {"eventId": "p1", "subEventName": "Simple pass", "matchPeriod": "1H", "eventSec": 610.0,
         "trackable_object": "player_7", "jerseyNumber": 7, "teamName": "Away FC",
         "positions": [{"x": 40, "y": 50}, {"x": 60, "y": 50}], "accurate": True},
    ], "label": "Home FC - Away FC"}
    result = _run(feed)
    home = result.narrative.team_stats["Home FC"]
    assert (home.fouls, home.cards, home.yellow_cards) == (1, 1, 1)
    cards = [m for m in result.narrative.key_moments if m.event_type == "card"]
    assert len(cards) == 1 and cards[0].card_type == "yellow"


def test_shots_on_target_follow_shot_outcome():
    result = _run(_opta_feed([
        _opta_event(1, "ATTEMPT_SAVED", outcome=0),        # saved: on target even with Outcome 0
        _opta_event(2, "MISS", outcome=1),                 # off target even with Outcome 1
        _opta_event(3, "ATTEMPT_SAVED", qualifiers=[82]),  # blocked: not on target
        _opta_event(4, "GOAL"),
        _opta_event(5, "PASS", team="Away FC"),
    ]))
    home = result.narrative.team_stats["Home FC"]
    assert (home.shots, home.shots_on_target, home.goals) == (4, 2, 1)


def test_key_moment_takes_the_name_from_the_match_sheet_when_the_feed_has_none():
    data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json").read_text())
    sheet = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "match_sheets" / "opta_match_sheet.json").read_text())
    result = MasterOrchestrator(ai_escalation_enabled=False).run(data, match_sheet_data=sheet)
    goals = [m for m in result.narrative.key_moments if m.event_type == "goal"]
    assert goals and all(m.player_name for m in goals)
    assert all("Jersey #" not in m.description for m in goals)


@pytest.mark.parametrize("source", ["opta", "statsbomb", "wyscout"])
@pytest.mark.parametrize("target", ["opta", "statsbomb", "wyscout"])
def test_export_and_reingest_keeps_card_colour_and_shot_outcome(source, target):
    orchestrator = MasterOrchestrator(ai_escalation_enabled=False)
    data = json.loads((SAMPLE_MATCHES_DIR / "PL_2025_PARTIAL_001" / f"{source}.json").read_text())
    original = orchestrator.run(data).document
    exported, _ = orchestrator.export(original, target)
    again = orchestrator.run(exported).document

    def detail(doc):
        cards = sorted((e.timestamp, e.card_type) for e in doc.events if e.card_type)
        shots = sorted((e.timestamp, e.event_type, e.shot_outcome) for e in doc.events if e.shot_outcome)
        return cards, shots

    original_cards, original_shots = detail(original)
    again_cards, again_shots = detail(again)
    assert original_cards and original_shots
    assert again_cards == original_cards
    if target == "wyscout":
        # Wyscout tags have no "hit the post" code; a post comes back as off target.
        original_shots = [(t, et, "off_target" if so == "post" else so) for t, et, so in original_shots]
    assert again_shots == original_shots
