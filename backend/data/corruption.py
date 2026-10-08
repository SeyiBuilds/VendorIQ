"""
Live, on-demand feed corruption, for demonstrating graceful degradation.

This is deliberately separate from synthetic_match_generator.py's
clean/messy/partial tiers -- those are fixed, reproducible fixtures used
by the test suite. This module exists so a *clean* feed can be corrupted
live, on demand, and show the exact same pipeline absorb it -- rather
than only ever pointing at pre-baked "messy" files and asking the
audience to trust that they're representative.

Every mutation here targets a SPECIFIC, already-documented fallback path
in an upstream agent (see the docstring comment next to each one). This
is intentional: the point of this feature is "watch the system recover
from a known failure mode," not "watch it survive random noise." Each
mutation operates on Opta's raw event shape (EventId/TypeName/Min/Sec/
PeriodId/PlayerRef/JerseyNumber/TeamName/X/Y/...) since Opta is this
app's default single-vendor ingest path (see orchestrator.py and
backend/api/main.py). Extending to StatsBomb/Wyscout shapes is future
work, not attempted here -- same "registry, not a rewrite" pattern as
every other vendor-keyed dict in this codebase, so it's a drop-in
extension when needed.
"""

from __future__ import annotations

import copy
import random
from typing import Callable, Optional

Mutation = Callable[[dict], None]


def _strip_player_identity(event: dict) -> None:
    # schema_mapper._map_opta_event reads JerseyNumber via
    # _get_first_present(["JerseyNumber", "ShirtNumber"]); with it gone and
    # no player name anywhere in this dataset, entity_resolver.py's Pass 1
    # clustering skips the event ("if jersey is None: continue") and Pass 2
    # falls all the way to vendor_scoped_fallback (resolution_confidence
    # 0.2) -- a real, already-tested degraded path, not a crash.
    event.pop("JerseyNumber", None)


def _null_coordinates(event: dict) -> None:
    # coordinate_translator.py passes None straight through with no
    # penalty (it's not this agent's failure to report); validator.py's
    # _build_event then drops the event outright ("missing coordinates
    # after Coordinate Translator") rather than fabricating a position.
    # Visible as an events_dropped count in the final document's warnings.
    event["X"] = None
    event["Y"] = None


def _drop_period(event: dict) -> None:
    # schema_mapper logs "period missing"; validator._repair_period then
    # infers the period from the timestamp using the same 45-minute
    # boundary rule the generator itself uses -- usually a silent, correct
    # recovery, which is exactly the point: not every corruption should
    # look dramatic, some should just quietly work.
    event.pop("PeriodId", None)


def _drop_timestamps(event: dict) -> None:
    # schema_mapper: "if minute is not None and second is not None" fails,
    # so timestamp defaults to 0.0 with "Min/Sec missing, timestamp
    # defaulted to 0.0" logged. Deliberately NOT setting Min/Sec to a
    # non-numeric string -- that hits an unguarded float() call in
    # _map_opta_event and would crash Schema Mapper rather than degrade
    # gracefully, which is a real bug worth fixing someday but is not what
    # this "graceful degradation" feature is meant to trigger.
    event.pop("Min", None)
    event.pop("Sec", None)


def _garble_event_type(event: dict) -> None:
    # schema_mapper: unrecognized TypeName buckets into event_type="other"
    # with "unrecognized event type '...' mapped to 'other'" logged,
    # instead of guessing. event_type_raw is preserved for lossless
    # export-back (Agent 7), so nothing is actually lost.
    event["TypeName"] = "ZZZ_UNKNOWN_VENDOR_CODE"


MUTATIONS: dict[str, Mutation] = {
    "strip_player_identity": _strip_player_identity,
    "null_coordinates": _null_coordinates,
    "drop_period": _drop_period,
    "drop_timestamps": _drop_timestamps,
    "garble_event_types": _garble_event_type,
}

MUTATION_DESCRIPTIONS: dict[str, str] = {
    "strip_player_identity": "removes jersey numbers -- forces Entity Resolver's lowest-confidence fallback identity",
    "null_coordinates": "nulls out X/Y -- Validator drops these events rather than fabricating a position",
    "drop_period": "removes the period marker -- Validator re-derives it from the timestamp (usually silent, correct recovery)",
    "drop_timestamps": "removes the clock reading -- Schema Mapper defaults it to 0.0 and logs the gap",
    "garble_event_types": "replaces the event type with an unrecognized vendor code -- mapped honestly to 'other', not guessed",
}


def corrupt_opta_payload(
    vendor_data: dict,
    mutation_names: list[str],
    rate: float = 0.2,
    seed: Optional[int] = None,
) -> tuple[dict, list[str]]:
    """Returns (corrupted_vendor_data, human_readable_log). Never mutates
    vendor_data in place -- callers (e.g. the sample match files on disk)
    keep their clean copy regardless of what happens here."""
    unknown = [m for m in mutation_names if m not in MUTATIONS]
    if unknown:
        raise ValueError(f"unknown mutation(s) {unknown}; supported: {sorted(MUTATIONS)}")
    if not 0.0 < rate <= 1.0:
        raise ValueError(f"rate must be in (0.0, 1.0], got {rate}")

    data = copy.deepcopy(vendor_data)
    events = data.get("Events", [])
    rng = random.Random(seed)
    log: list[str] = []

    n = len(events)
    sample_size = max(1, round(rate * n)) if n else 0

    for name in mutation_names:
        fn = MUTATIONS[name]
        indices = rng.sample(range(n), min(sample_size, n)) if n else []
        for i in indices:
            fn(events[i])
        log.append(
            f"{name}: corrupted {len(indices)} of {n} events (~{rate * 100:.0f}% target) -- {MUTATION_DESCRIPTIONS[name]}"
        )

    return data, log
