"""
Agent 4: Entity Resolver

Real-world trigger for this agent: a club running more than one vendor feed
for the same match (e.g. Opta as the primary feed, StatsBomb pulled in
separately for its xG model). Each vendor names players with its own
identifiers, so before anything downstream can compare or merge events
across those feeds, something has to decide "this is the same player."

Input is a list of MappedEvent (post Schema Mapper, coordinates may or may
not yet be normalized -- this agent doesn't care about x/y at all). Output
is a list of ResolvedEvent: each MappedEvent paired with a canonical
cross-vendor player_id, a confidence score, and a log of how that id was
decided, plus a PlayerRegistry describing every canonical player found.

Per PRD Section 4.1 fallback logic for this agent:
  Attempt 1: deterministic match on (team, jersey_number) -- jersey numbers
             are unique within a team at a given time, and (after the
             Section-5 generator fix) all three MVP vendors carry one.
  Attempt 2: corroborate / fall back to name matching when jersey_number is
             missing from this event's vendor but another vendor's record
             for a name-bearing feed (StatsBomb) lets us infer it.
  Fallback:  no cross-vendor signal at all -> vendor-scoped synthetic id,
             confidence capped low, explicitly flagged as unresolved rather
             than guessed.

Ambiguous cases -- the same (team, jersey_number) cluster disagreeing on
name across vendors (a genuine data conflict, not just a missing field) --
are the one case where this agent calls out to Claude for a judgment call.
That call is OPTIONAL: gated on the ANTHROPIC_API_KEY environment variable.
No key -> skip the escalation, log why, and fall through to the
deterministic tie-break (keep the jersey-based cluster, flag the name
conflict as unresolved, apply a confidence penalty). This keeps the agent
fully functional with zero API cost/setup, and ready to upgrade the moment
a key is configured -- nothing else about the pipeline has to change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from schema_mapper import MappedEvent

# Flip to True only once an Anthropic API key is configured and the escalation
# path has been exercised against real ambiguous cases. Kept as an explicit
# switch (rather than purely env-driven) so a demo run can force the
# deterministic-only path even with a key present, for reproducibility.
AI_ESCALATION_ENABLED_DEFAULT = True


@dataclass
class PlayerIdentity:
    """One canonical, cross-vendor player as this agent currently understands
    them. `vendor_refs` and `names_seen` accumulate evidence from every
    vendor event that resolved into this cluster -- useful for the demo UI
    and for debugging a bad resolution."""
    canonical_id: str
    team: Optional[str]
    jersey_number: Optional[int]
    names_seen: dict[str, str] = field(default_factory=dict)       # vendor -> name
    vendor_refs: dict[str, set] = field(default_factory=dict)      # vendor -> {vendor_ids}
    resolution_method: str = "deterministic_jersey_team"
    confidence: float = 1.0
    flags: list[str] = field(default_factory=list)


@dataclass
class ResolvedEvent:
    """A MappedEvent plus the cross-vendor player identity this agent
    assigned to it. The original event is never mutated."""
    event: MappedEvent
    canonical_player_id: str
    resolution_confidence: float
    resolution_method: str
    resolution_log: list[str] = field(default_factory=list)


def _call_claude_for_conflict(team: Optional[str], jersey_number: Optional[int],
                               names_by_vendor: dict[str, str]) -> Optional[dict]:
    """Optional upgrade path. Only reached when ANTHROPIC_API_KEY is set and
    AI_ESCALATION_ENABLED_DEFAULT is True. Asks Claude whether the names seen
    across vendors for one (team, jersey_number) cluster plausibly refer to
    the same person (e.g. 'K. De Bruyne' vs 'Kevin De Bruyne') or are a real
    data conflict (two different players wrongly sharing a jersey record).

    Returns a dict like {"same_player": bool, "reasoning": str} or None if
    the call fails for any reason -- callers must treat None exactly like
    "no key configured" and fall through to the deterministic tie-break.
    Nothing about the deterministic path depends on this function succeeding.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic  # local import: keeps this an optional dependency path

        client = anthropic.Anthropic(api_key=api_key)
        names_desc = "\n".join(f"- {vendor}: '{name}'" for vendor, name in names_by_vendor.items())
        prompt = (
            f"Football data reconciliation task. Team '{team}', shirt number "
            f"{jersey_number}, has been reported under these names by different "
            f"data vendors for the same match:\n{names_desc}\n\n"
            "Are these plausibly the same player (name formatting/abbreviation "
            "differences, nicknames, transliteration), or are they genuinely "
            "different players incorrectly sharing a shirt-number record? "
            "Reply with exactly one line: SAME or DIFFERENT, then a dash, then "
            "a one-sentence reason."
        )
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        verdict = text.split("-", 1)[0].strip().upper()
        reasoning = text.split("-", 1)[1].strip() if "-" in text else text
        return {"same_player": verdict == "SAME", "reasoning": reasoning}
    except Exception as exc:  # network error, bad key, rate limit, SDK not installed, etc.
        return {"_error": str(exc)}


class EntityResolver:
    """Agent 4. Resolves vendor-local player references into canonical,
    cross-vendor player identities.

    Call resolve() once per match, passing in every event from every vendor
    feed being reconciled for that match (e.g. Opta's events + StatsBomb's
    events, both already through Format Detective and Schema Mapper).
    """

    def __init__(self, ai_escalation_enabled: bool = AI_ESCALATION_ENABLED_DEFAULT):
        self.ai_escalation_enabled = ai_escalation_enabled

    def resolve(self, events: list[MappedEvent]) -> tuple[list[ResolvedEvent], dict[str, PlayerIdentity]]:
        clusters: dict[tuple[Optional[str], int], PlayerIdentity] = {}
        name_lookup: dict[tuple[Optional[str], str], str] = {}  # (team, name) -> canonical_id
        resolved: list[ResolvedEvent] = []

        # Pass 1: build jersey-keyed clusters from every event that has one.
        for e in events:
            ref = e.raw_player_ref
            jersey = ref.get("jersey_number")
            name = ref.get("name")
            vendor_id = ref.get("vendor_id")
            if jersey is None:
                continue

            key = (e.team, jersey)
            identity = clusters.get(key)
            if identity is None:
                identity = PlayerIdentity(
                    canonical_id=f"player::{e.team or 'unknown_team'}::{jersey}",
                    team=e.team,
                    jersey_number=jersey,
                )
                clusters[key] = identity

            if name:
                existing_name = identity.names_seen.get(e.vendor_source)
                if existing_name and existing_name != name:
                    identity.flags.append(
                        f"vendor-internal name inconsistency on vendor={e.vendor_source}: "
                        f"'{existing_name}' vs '{name}'"
                    )
                identity.names_seen[e.vendor_source] = name
                name_lookup[(e.team, name)] = identity.canonical_id
            if vendor_id is not None:
                identity.vendor_refs.setdefault(e.vendor_source, set()).add(vendor_id)

        # Cross-vendor name conflicts: same jersey+team, different names
        # reported by *different* vendors (not the same vendor disagreeing
        # with itself -- that's a Schema Mapper-level data quality issue).
        for identity in clusters.values():
            distinct_names = set(identity.names_seen.values())
            if len(distinct_names) > 1:
                self._resolve_name_conflict(identity)

        # Pass 2: assign every event to a canonical id.
        for idx, e in enumerate(events):
            ref = e.raw_player_ref
            jersey = ref.get("jersey_number")
            name = ref.get("name")
            log: list[str] = []

            if jersey is not None:
                identity = clusters[(e.team, jersey)]
                conflict = any("name conflict" in f for f in identity.flags)
                method = identity.resolution_method if conflict else "deterministic_jersey_team"
                confidence = identity.confidence if conflict else 1.0
                if conflict:
                    log.append("resolved via jersey+team cluster with an unresolved/AI-adjudicated name conflict")
                resolved.append(ResolvedEvent(
                    event=e, canonical_player_id=identity.canonical_id,
                    resolution_confidence=confidence, resolution_method=method,
                    resolution_log=log,
                ))
                continue

            # Attempt 2: jersey missing on this event -- try corroborating via
            # a name this vendor also reported, if some other vendor's
            # jersey-keyed cluster already claimed that (team, name).
            if name and (e.team, name) in name_lookup:
                canonical_id = name_lookup[(e.team, name)]
                log.append(f"jersey_number missing; recovered via name match ('{name}') to existing cluster")
                resolved.append(ResolvedEvent(
                    event=e, canonical_player_id=canonical_id,
                    resolution_confidence=0.75, resolution_method="deterministic_name_fallback",
                    resolution_log=log,
                ))
                continue

            # Fallback: no cross-vendor signal at all. Vendor-scoped synthetic
            # identity -- explicitly NOT claimed to match anything else.
            vendor_id = ref.get("vendor_id")
            scoped_key = vendor_id if vendor_id is not None else f"idx{idx}"
            canonical_id = f"unresolved::{e.vendor_source}::{e.team or 'unknown_team'}::{scoped_key}"
            log.append("no jersey_number and no name match available; vendor-scoped fallback identity assigned")
            resolved.append(ResolvedEvent(
                event=e, canonical_player_id=canonical_id,
                resolution_confidence=0.2, resolution_method="vendor_scoped_fallback",
                resolution_log=log,
            ))

        registry = {identity.canonical_id: identity for identity in clusters.values()}
        return resolved, registry

    def _resolve_name_conflict(self, identity: PlayerIdentity) -> None:
        """Mutates identity in place: sets resolution_method/confidence/flags
        once a jersey+team cluster has genuinely conflicting names across
        vendors. Tries the optional Claude escalation first; always has a
        deterministic answer ready regardless of whether that succeeds."""
        identity.flags.append(
            f"name conflict across vendors for team={identity.team} jersey={identity.jersey_number}: "
            f"{identity.names_seen}"
        )
        ai_result = None
        if self.ai_escalation_enabled:
            ai_result = _call_claude_for_conflict(identity.team, identity.jersey_number, identity.names_seen)

        if ai_result is None:
            identity.flags.append("AI escalation skipped: ANTHROPIC_API_KEY not configured")
            identity.resolution_method = "deterministic_jersey_team_unresolved_conflict"
            identity.confidence = 0.6
            return

        if "_error" in ai_result:
            identity.flags.append(f"AI escalation attempted but failed: {ai_result['_error']}")
            identity.resolution_method = "deterministic_jersey_team_unresolved_conflict"
            identity.confidence = 0.6
            return

        identity.flags.append(f"AI escalation verdict: {'SAME' if ai_result['same_player'] else 'DIFFERENT'} -- {ai_result['reasoning']}")
        if ai_result["same_player"]:
            identity.resolution_method = "ai_escalation_confirmed_same"
            identity.confidence = 0.9
        else:
            identity.resolution_method = "ai_escalation_flagged_different"
            identity.confidence = 0.4


if __name__ == "__main__":
    import json
    from pathlib import Path
    from format_detective import FormatDetective
    from schema_mapper import SchemaMapper

    detective = FormatDetective()
    mapper = SchemaMapper()
    resolver = EntityResolver()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Entity Resolver -- test run (merging Opta + StatsBomb + Wyscout per match)")
    print("=" * 78)

    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        all_events = []
        for vendor_file in sorted(match_dir.glob("*.json")):
            data = json.loads(vendor_file.read_text())
            detection = detective.detect(data)
            mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
            all_events.extend(mapped)

        resolved, registry = resolver.resolve(all_events)
        methods = {}
        for r in resolved:
            methods[r.resolution_method] = methods.get(r.resolution_method, 0) + 1

        print(f"\n{match_dir.name}: {len(all_events)} events across vendors -> {len(registry)} canonical players")
        for method, count in sorted(methods.items()):
            print(f"  {method}: {count}")
