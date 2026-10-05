"""Tests for Agent 4: Entity Resolver.

Covers: deterministic jersey+team matching across vendors, the name-fallback
path when jersey_number is missing, the vendor-scoped fallback when there's
no cross-vendor signal at all, name-conflict detection, and the optional
Claude escalation path's graceful no-key behavior (the explicit requirement
this agent was built around).
"""

import json
import os
import pytest
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper, MappedEvent
from entity_resolver import EntityResolver, PlayerIdentity


@pytest.fixture(scope="module")
def detective():
    return FormatDetective()


@pytest.fixture(scope="module")
def mapper():
    return SchemaMapper()


@pytest.fixture
def resolver():
    return EntityResolver()


def _mapped_events_for_match(detective, mapper, match_dir):
    all_events = []
    for vendor_file in sorted(match_dir.glob("*.json")):
        data = json.loads(vendor_file.read_text())
        detection = detective.detect(data)
        mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
        all_events.extend(mapped)
    return all_events


def _all_match_dirs():
    return [d for d in sorted(SAMPLE_MATCHES_DIR.iterdir()) if d.is_dir()]


ALL_MATCHES = _all_match_dirs()


def _bare_event(**overrides) -> MappedEvent:
    """Builds a minimal MappedEvent for unit-level resolver tests, without
    needing to round-trip through Format Detective / Schema Mapper."""
    defaults = dict(
        event_id="e1", event_type="pass", event_type_raw="Pass", timestamp=1.0,
        period=1, team="Liverpool", raw_player_ref={"vendor_id": None, "jersey_number": None, "name": None},
        x=50.0, y=50.0, end_x=None, end_y=None, outcome=None,
        vendor_source="opta", mapping_confidence=1.0, mapping_attempts=1,
        mapping_log=[], raw_metadata={},
    )
    defaults.update(overrides)
    return MappedEvent(**defaults)


# --- Integration: resolver over real multi-vendor sample data ---

@pytest.mark.parametrize("match_dir", ALL_MATCHES, ids=[d.name for d in ALL_MATCHES])
def test_every_event_gets_a_resolution(detective, mapper, resolver, match_dir):
    events = _mapped_events_for_match(detective, mapper, match_dir)
    resolved, registry = resolver.resolve(events)
    assert len(resolved) == len(events), "every input event must produce exactly one ResolvedEvent"
    for r in resolved:
        assert r.canonical_player_id
        assert 0.0 <= r.resolution_confidence <= 1.0


def test_clean_tier_resolves_fully_deterministic(detective, mapper, resolver):
    """Clean data has jersey_number on every event from every vendor, so
    nothing should need the name-fallback or vendor-scoped-fallback path."""
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    events = _mapped_events_for_match(detective, mapper, match_dir)
    resolved, _ = resolver.resolve(events)
    methods = {r.resolution_method for r in resolved}
    assert methods == {"deterministic_jersey_team"}
    assert all(r.resolution_confidence == 1.0 for r in resolved)


def test_messy_tier_uses_every_fallback_tier(detective, mapper, resolver):
    """Confirms the test fixture actually exercises all three resolution
    paths -- if this assumption breaks, the degraded-data tests below are
    not testing what they claim to."""
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001"
    events = _mapped_events_for_match(detective, mapper, match_dir)
    resolved, _ = resolver.resolve(events)
    methods = {r.resolution_method for r in resolved}
    assert "deterministic_jersey_team" in methods
    assert "deterministic_name_fallback" in methods
    assert "vendor_scoped_fallback" in methods


def test_registry_player_count_matches_distinct_jersey_team_pairs(detective, mapper, resolver):
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    events = _mapped_events_for_match(detective, mapper, match_dir)
    resolved, registry = resolver.resolve(events)
    expected_pairs = {(e.team, e.raw_player_ref["jersey_number"]) for e in events
                       if e.raw_player_ref["jersey_number"] is not None}
    assert len(registry) == len(expected_pairs)


def test_same_jersey_same_vendor_group_reuses_canonical_id(detective, mapper, resolver):
    """Every event for one real player, across however many vendor feeds
    report them, should collapse onto a single canonical_id."""
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001"
    events = _mapped_events_for_match(detective, mapper, match_dir)
    resolved, _ = resolver.resolve(events)

    by_jersey_team: dict[tuple, set] = {}
    for e, r in zip(events, resolved):
        jersey = e.raw_player_ref["jersey_number"]
        if jersey is None:
            continue
        by_jersey_team.setdefault((e.team, jersey), set()).add(r.canonical_player_id)

    for key, ids in by_jersey_team.items():
        assert len(ids) == 1, f"team/jersey {key} resolved to multiple canonical ids: {ids}"


# --- Unit tests on resolver logic in isolation ---

def test_cross_vendor_matching_without_name(resolver):
    """Two vendors, same team+jersey, neither carries a name -- should still
    collapse onto one canonical identity (jersey+team is sufficient alone)."""
    e1 = _bare_event(event_id="a", vendor_source="opta",
                      raw_player_ref={"vendor_id": "opta_55", "jersey_number": 9, "name": None})
    e2 = _bare_event(event_id="b", vendor_source="wyscout",
                      raw_player_ref={"vendor_id": "wy_200", "jersey_number": 9, "name": None})
    resolved, registry = resolver.resolve([e1, e2])
    assert resolved[0].canonical_player_id == resolved[1].canonical_player_id
    assert len(registry) == 1
    identity = next(iter(registry.values()))
    assert identity.vendor_refs["opta"] == {"opta_55"}
    assert identity.vendor_refs["wyscout"] == {"wy_200"}


def test_name_fallback_recovers_missing_jersey(resolver):
    """StatsBomb event carries jersey+name; a second StatsBomb-vendor event
    for the same player is missing jersey_number but still carries the name
    -- should recover via the name-lookup path, not fall all the way back."""
    e1 = _bare_event(event_id="a", vendor_source="statsbomb",
                      raw_player_ref={"vendor_id": "sb_1", "jersey_number": 10, "name": "Lionel Messi"})
    e2 = _bare_event(event_id="b", vendor_source="statsbomb",
                      raw_player_ref={"vendor_id": "sb_1", "jersey_number": None, "name": "Lionel Messi"})
    resolved, _ = resolver.resolve([e1, e2])
    assert resolved[0].resolution_method == "deterministic_jersey_team"
    assert resolved[1].resolution_method == "deterministic_name_fallback"
    assert resolved[1].canonical_player_id == resolved[0].canonical_player_id
    assert resolved[1].resolution_confidence < resolved[0].resolution_confidence


def test_vendor_scoped_fallback_when_no_signal_at_all(resolver):
    e1 = _bare_event(event_id="a", vendor_source="opta",
                      raw_player_ref={"vendor_id": None, "jersey_number": None, "name": None})
    resolved, registry = resolver.resolve([e1])
    assert resolved[0].resolution_method == "vendor_scoped_fallback"
    assert resolved[0].resolution_confidence <= 0.2
    assert resolved[0].canonical_player_id.startswith("unresolved::")
    assert registry == {}  # never resolved into a real cluster


def test_different_unresolved_events_get_different_ids(resolver):
    """Two events with zero signal shouldn't be silently merged into the
    same canonical identity just because they both fell back."""
    e1 = _bare_event(event_id="a", vendor_source="opta",
                      raw_player_ref={"vendor_id": None, "jersey_number": None, "name": None})
    e2 = _bare_event(event_id="b", vendor_source="opta",
                      raw_player_ref={"vendor_id": None, "jersey_number": None, "name": None})
    resolved, _ = resolver.resolve([e1, e2])
    assert resolved[0].canonical_player_id != resolved[1].canonical_player_id


def test_different_teams_same_jersey_number_not_merged(resolver):
    """Jersey 9 on Liverpool and jersey 9 on Chelsea are different people --
    team must be part of the matching key, not jersey alone."""
    e1 = _bare_event(event_id="a", team="Liverpool",
                      raw_player_ref={"vendor_id": "x", "jersey_number": 9, "name": None})
    e2 = _bare_event(event_id="b", team="Chelsea",
                      raw_player_ref={"vendor_id": "y", "jersey_number": 9, "name": None})
    resolved, registry = resolver.resolve([e1, e2])
    assert resolved[0].canonical_player_id != resolved[1].canonical_player_id
    assert len(registry) == 2


# --- The optional AI escalation path: name conflict + no-key behavior ---

def test_name_conflict_without_api_key_falls_back_deterministically(resolver, monkeypatch):
    """The exact scenario the AI escalation exists for: two vendors agree on
    (team, jersey) but disagree on name. With no ANTHROPIC_API_KEY set, the
    resolver must NOT crash, must NOT silently guess -- it keeps the
    jersey-based cluster, flags the conflict, and drops confidence."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    e1 = _bare_event(event_id="a", vendor_source="statsbomb",
                      raw_player_ref={"vendor_id": "sb_1", "jersey_number": 7, "name": "K. De Bruyne"})
    e2 = _bare_event(event_id="b", vendor_source="opta",
                      raw_player_ref={"vendor_id": "op_1", "jersey_number": 7, "name": "Kevin De Bruyne"})
    # Force a genuine cross-vendor conflict by giving opta a name too --
    # normally opta carries no name, but this unit test targets the
    # conflict-resolution branch directly.
    resolved, registry = resolver.resolve([e1, e2])

    assert resolved[0].canonical_player_id == resolved[1].canonical_player_id
    identity = next(iter(registry.values()))
    assert any("name conflict" in f for f in identity.flags)
    assert any("AI escalation skipped: ANTHROPIC_API_KEY not configured" in f for f in identity.flags)
    assert identity.resolution_method == "deterministic_jersey_team_unresolved_conflict"
    assert identity.confidence == 0.6
    for r in resolved:
        assert r.resolution_confidence == 0.6


def test_ai_escalation_disabled_flag_skips_even_with_key(monkeypatch):
    """ai_escalation_enabled=False must behave identically to no-key, even
    if a key happens to be present in the environment -- lets a demo force
    deterministic-only behavior for reproducibility."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
    resolver = EntityResolver(ai_escalation_enabled=False)

    e1 = _bare_event(event_id="a", vendor_source="statsbomb",
                      raw_player_ref={"vendor_id": "sb_1", "jersey_number": 7, "name": "K. De Bruyne"})
    e2 = _bare_event(event_id="b", vendor_source="opta",
                      raw_player_ref={"vendor_id": "op_1", "jersey_number": 7, "name": "Kevin De Bruyne"})
    resolved, registry = resolver.resolve([e1, e2])

    identity = next(iter(registry.values()))
    assert any("AI escalation skipped: ANTHROPIC_API_KEY not configured" in f for f in identity.flags)


def test_no_conflict_clusters_never_mention_ai_escalation(detective, mapper, resolver):
    """Flags about AI escalation should only ever appear on clusters that
    actually had a name conflict -- not sprinkled onto every player."""
    match_dir = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001"
    events = _mapped_events_for_match(detective, mapper, match_dir)
    _, registry = resolver.resolve(events)
    for identity in registry.values():
        has_conflict = any("name conflict" in f for f in identity.flags)
        has_ai_flag = any("AI escalation" in f for f in identity.flags)
        assert has_ai_flag == has_conflict
