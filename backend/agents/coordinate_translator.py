"""
Agent 3: Coordinate Translator

Takes MappedEvents (Schema Mapper's output — field names aligned to CDF,
but coordinates still in each vendor's native scale) and normalizes every
coordinate into the CDF invariant: 0-100 x 0-100, origin at the pitch
corner, consistent across all vendors.

This agent OWNS the 0-100 guarantee. Nothing downstream (Entity Resolver,
Validator, Narrative Generator) should ever see an out-of-range or
vendor-native coordinate once it's passed through here — if it does, that's
a bug in this agent, not something downstream should have to defend against.

Known vendor coordinate systems (per PRD Section 4.1, Agent 3):
  - Opta:      0-120 x 0-80   (linear scale)
  - StatsBomb: 0-100 x 0-100  (already normalized — pass-through)
  - Wyscout:   0-100 x 0-100  nominally, but real-world (and our synthetic
               messy tier) data frequently has out-of-range values from
               sloppy export pipelines — needs clamping, not just scaling

Per PRD Section 4.1 fallback logic:
  Attempt 1: use the vendor-specific conversion formula
  Attempt 2: check metadata for pitch dimensions (not available in this
             synthetic dataset, so attempt 2 degrades to a documented no-op
             — see _attempt_metadata_dimensions below)
  Fallback:  assume StatsBomb standard (0-100), clamp into range, confidence
             drops accordingly
"""

from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Optional

from schema_mapper import MappedEvent

# Pitch dimensions per vendor's native coordinate system (width, height)
VENDOR_PITCH_DIMENSIONS: dict[str, tuple[float, float]] = {
    "opta": (120.0, 80.0),
    "statsbomb": (100.0, 100.0),
    "wyscout": (100.0, 100.0),
    "genius_sports": (100.0, 100.0),  # not in MVP scope, documented for extensibility
}

DEFAULT_PITCH_DIMENSIONS = (100.0, 100.0)  # StatsBomb standard, used as last-resort fallback


@dataclass
class TranslationReport:
    """Per-event record of what happened during translation, for the
    confidence-scoring and demo-transparency purposes the PRD calls for."""
    attempts: int
    confidence_penalty: float
    log: list[str]


def _scale_value(value: float, native_max: float) -> float:
    """Linear scale from [0, native_max] to [0, 100]."""
    return (value / native_max) * 100.0


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> tuple[float, bool]:
    """Returns (clamped_value, was_clamped)."""
    if value < lo:
        return lo, True
    if value > hi:
        return hi, True
    return value, False


def _translate_coordinate(value: Optional[float], vendor: str, dim: str,
                           log: list[str]) -> tuple[Optional[float], int, float]:
    """Translates a single coordinate value. Returns (translated_value,
    attempts_used, confidence_penalty).

    dim is 'x' or 'y' — needed because Opta's x/y scales differ (120 vs 80),
    unlike StatsBomb/Wyscout where both axes share one scale.
    """
    if value is None:
        return None, 1, 0.0  # nothing to translate; not this agent's failure to report

    # --- Attempt 1: vendor-specific conversion formula ---
    dims = VENDOR_PITCH_DIMENSIONS.get(vendor)
    if dims is not None:
        native_max = dims[0] if dim == "x" else dims[1]
        scaled = _scale_value(value, native_max)
        clamped, was_clamped = _clamp(scaled)
        if not was_clamped:
            return clamped, 1, 0.0
        # In range after vendor-correct scaling but still out of bounds ->
        # source data itself is corrupted (e.g. Wyscout messy tier), not a
        # wrong formula. Record it and apply a confidence penalty, but this
        # still counts as attempt 1 succeeding at its job (correct formula,
        # bad input) rather than needing to fall through to attempt 2.
        log.append(
            f"{dim}={value} vendor={vendor}: scaled to {scaled:.2f}, out of 0-100 range, clamped to {clamped:.2f}"
        )
        return clamped, 1, 0.08

    # --- Attempt 2: check metadata for pitch dimensions ---
    # Our synthetic vendor files don't carry a metadata pitch-dimensions
    # block (real Opta/StatsBomb exports sometimes do, e.g. a PitchDimensions
    # field). Documented honestly as a no-op here rather than faking a success.
    log.append(f"{dim}={value} vendor='{vendor}' unrecognized: attempt 2 (metadata lookup) found no pitch dimensions")

    # --- Fallback: assume StatsBomb standard (0-100), clamp, penalize ---
    native_max = DEFAULT_PITCH_DIMENSIONS[0] if dim == "x" else DEFAULT_PITCH_DIMENSIONS[1]
    scaled = _scale_value(value, native_max) if native_max != 100.0 else value
    clamped, was_clamped = _clamp(scaled)
    log.append(
        f"{dim}={value}: fell back to StatsBomb-standard (0-100) assumption, "
        f"result={clamped:.2f}{' (clamped)' if was_clamped else ''}"
    )
    return clamped, 3, 0.15


class CoordinateTranslator:
    """Agent 3. Pure function over MappedEvent -> MappedEvent with normalized
    coordinates. Does not mutate the input; returns new MappedEvent instances
    (coordinates replaced, mapping_confidence adjusted, log extended)."""

    def translate(self, events: list[MappedEvent]) -> list[MappedEvent]:
        return [self._translate_one(e) for e in events]

    def _translate_one(self, event: MappedEvent) -> MappedEvent:
        log: list[str] = []
        total_attempts = 1
        total_penalty = 0.0

        new_x, a, p = _translate_coordinate(event.x, event.vendor_source, "x", log)
        total_attempts = max(total_attempts, a)
        total_penalty += p

        new_y, a, p = _translate_coordinate(event.y, event.vendor_source, "y", log)
        total_attempts = max(total_attempts, a)
        total_penalty += p

        new_end_x, a, p = _translate_coordinate(event.end_x, event.vendor_source, "x", log)
        total_attempts = max(total_attempts, a)
        total_penalty += p

        new_end_y, a, p = _translate_coordinate(event.end_y, event.vendor_source, "y", log)
        total_attempts = max(total_attempts, a)
        total_penalty += p

        new_confidence = max(0.1, round(event.mapping_confidence - total_penalty, 3))
        new_log = event.mapping_log + log
        new_attempts = max(event.mapping_attempts, total_attempts)

        return replace(
            event,
            x=new_x, y=new_y, end_x=new_end_x, end_y=new_end_y,
            mapping_confidence=new_confidence,
            mapping_attempts=new_attempts,
            mapping_log=new_log,
        )


if __name__ == "__main__":
    import json
    from pathlib import Path
    from format_detective import FormatDetective
    from schema_mapper import SchemaMapper

    detective = FormatDetective()
    mapper = SchemaMapper()
    translator = CoordinateTranslator()
    sample_root = Path("backend/data/sample_matches")

    print("=" * 78)
    print("Coordinate Translator — test run (Detective -> Mapper -> Translator)")
    print("=" * 78)

    for match_dir in sorted(sample_root.iterdir()):
        if not match_dir.is_dir():
            continue
        for vendor_file in sorted(match_dir.glob("*.json")):
            data = json.loads(vendor_file.read_text())
            detection = detective.detect(data)
            mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
            translated = translator.translate(mapped)

            out_of_range_before = sum(
                1 for e in mapped
                if (e.x is not None and (e.x > 100 or e.x < 0)) or (e.y is not None and (e.y > 100 or e.y < 0))
            )
            out_of_range_after = sum(
                1 for e in translated
                if (e.x is not None and (e.x > 100 or e.x < 0)) or (e.y is not None and (e.y > 100 or e.y < 0))
            )
            avg_conf = sum(e.mapping_confidence for e in translated) / len(translated)
            clamped_count = sum(1 for e in translated if any("clamped" in line for line in e.mapping_log))

            print(f"\n{match_dir.name}/{vendor_file.name}  (vendor={detection.detected_format})")
            print(f"  out-of-range coords before: {out_of_range_before} | after: {out_of_range_after}")
            print(f"  events needing clamp: {clamped_count} | avg confidence after translation: {avg_conf:.3f}")

    print("\n" + "=" * 78)
    print("Spot check: Opta clean-tier event, coordinate scaling correctness")
    print("=" * 78)
    data = json.loads((sample_root / "PL_2025_CLEAN_001" / "opta.json").read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)
    raw_event = data["Events"][0]
    mapped_event = mapped[0]
    translated_event = translated[0]
    print(f"  raw Opta:        X={raw_event['X']} Y={raw_event['Y']}  (scale 0-120 x 0-80)")
    print(f"  after mapper:    x={mapped_event.x} y={mapped_event.y}  (still native scale)")
    print(f"  after translate: x={translated_event.x:.2f} y={translated_event.y:.2f}  (normalized 0-100)")
    expected_x = raw_event["X"] / 120 * 100
    expected_y = raw_event["Y"] / 80 * 100
    print(f"  expected:        x={expected_x:.2f} y={expected_y:.2f}")
