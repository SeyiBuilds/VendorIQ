"""Tests for Agent 3: Coordinate Translator.

Core invariant this agent owns: every event's x/y/end_x/end_y must be in
[0, 100] after translation, regardless of vendor or input corruption. Every
test here either proves that invariant holds or proves the math is exact.
"""

import json
import pytest
from conftest import SAMPLE_MATCHES_DIR
from format_detective import FormatDetective
from schema_mapper import SchemaMapper, MappedEvent
from coordinate_translator import CoordinateTranslator, _scale_value, _clamp, _translate_coordinate


@pytest.fixture(scope="module")
def detective():
    return FormatDetective()


@pytest.fixture(scope="module")
def mapper():
    return SchemaMapper()


@pytest.fixture(scope="module")
def translator():
    return CoordinateTranslator()


def _pipeline(detective, mapper, translator, path):
    data = json.loads(path.read_text())
    detection = detective.detect(data)
    mapped = mapper.map_events(data, detection.detected_format, detection.event_list_key)
    translated = translator.translate(mapped)
    return data, detection, mapped, translated


def _all_sample_files():
    cases = []
    for match_dir in sorted(SAMPLE_MATCHES_DIR.iterdir()):
        if not match_dir.is_dir():
            continue
        for f in sorted(match_dir.glob("*.json")):
            cases.append((match_dir.name, f.stem, f))
    return cases


ALL_SAMPLES = _all_sample_files()


# --- Unit tests on the pure math functions ---

def test_scale_value_linear():
    assert _scale_value(60.0, 120.0) == 50.0
    assert _scale_value(0.0, 120.0) == 0.0
    assert _scale_value(120.0, 120.0) == 100.0


def test_clamp_in_range_unchanged():
    val, clamped = _clamp(55.0)
    assert val == 55.0
    assert clamped is False


def test_clamp_above_range():
    val, clamped = _clamp(150.0)
    assert val == 100.0
    assert clamped is True


def test_clamp_below_range():
    val, clamped = _clamp(-20.0)
    assert val == 0.0
    assert clamped is True


def test_translate_coordinate_none_passes_through():
    log = []
    value, attempts, penalty = _translate_coordinate(None, "opta", "x", log)
    assert value is None
    assert penalty == 0.0
    assert log == []  # a missing value is not this agent's problem to log


def test_translate_coordinate_unknown_vendor_falls_back():
    log = []
    value, attempts, penalty = _translate_coordinate(50.0, "totally_unknown_vendor", "x", log)
    assert attempts == 3
    assert penalty > 0.0
    assert value is not None
    assert any("fell back to StatsBomb-standard" in line for line in log)


# --- Invariant: output is ALWAYS in [0, 100], across every sample file ---

@pytest.mark.parametrize("match_name,vendor,filepath", ALL_SAMPLES,
                          ids=[f"{m}/{v}" for m, v, _ in ALL_SAMPLES])
def test_all_coordinates_in_valid_range_after_translation(detective, mapper, translator, match_name, vendor, filepath):
    _, _, _, translated = _pipeline(detective, mapper, translator, filepath)
    for e in translated:
        for attr in ("x", "y", "end_x", "end_y"):
            val = getattr(e, attr)
            if val is not None:
                assert 0.0 <= val <= 100.0, (
                    f"{match_name}/{vendor}.json event {e.event_id}: {attr}={val} out of range after translation"
                )


def test_opta_scaling_is_mathematically_exact(detective, mapper, translator):
    """0-120 x 0-80 -> 0-100 x 0-100 linear scale, spot-checked against the
    raw source value, not just 'is it in range'."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json"
    data, detection, mapped, translated = _pipeline(detective, mapper, translator, path)
    raw_events = data["Events"]

    checked = 0
    for raw, t in zip(raw_events, translated):
        if raw.get("X") is None:
            continue
        expected_x = raw["X"] / 120.0 * 100.0
        expected_y = raw["Y"] / 80.0 * 100.0
        assert t.x == pytest.approx(expected_x, abs=0.01)
        assert t.y == pytest.approx(expected_y, abs=0.01)
        checked += 1
    assert checked > 0, "no events had coordinates to check"


def test_statsbomb_coordinates_pass_through_when_already_valid(detective, mapper, translator):
    """StatsBomb is already 0-100 x 0-100 — clean-tier values should be
    unchanged by translation (not re-scaled, since native_max == 100)."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "statsbomb.json"
    data, detection, mapped, translated = _pipeline(detective, mapper, translator, path)
    for m, t in zip(mapped, translated):
        if m.x is not None:
            assert t.x == pytest.approx(m.x, abs=0.001)
        if m.y is not None:
            assert t.y == pytest.approx(m.y, abs=0.001)


def test_messy_wyscout_out_of_range_values_get_clamped(detective, mapper, translator):
    path = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001" / "wyscout.json"
    _, _, mapped, translated = _pipeline(detective, mapper, translator, path)

    had_out_of_range = any(
        (e.x is not None and e.x > 100) or (e.y is not None and e.y > 100)
        for e in mapped
    )
    assert had_out_of_range, "test fixture assumption broken: expected out-of-range values in messy wyscout data"

    for e in translated:
        if e.x is not None:
            assert e.x <= 100.0
        if e.y is not None:
            assert e.y <= 100.0


def test_clamping_reduces_confidence(detective, mapper, translator):
    """An event that needed clamping should end up with lower confidence
    than it had going in — translation isn't free when the input was bad."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_MESSY_001" / "statsbomb.json"
    _, _, mapped, translated = _pipeline(detective, mapper, translator, path)
    for m, t in zip(mapped, translated):
        if (m.x is not None and m.x > 100):
            assert t.mapping_confidence < m.mapping_confidence, (
                f"event {m.event_id} was clamped but confidence didn't drop"
            )


def test_clean_tier_never_needs_fallback_vendor(detective, mapper, translator):
    """All 3 MVP vendors (opta/statsbomb/wyscout) are in VENDOR_PITCH_DIMENSIONS,
    so clean data should never hit the 'unknown vendor' fallback path."""
    for match_name, vendor, filepath in ALL_SAMPLES:
        if "CLEAN" not in match_name:
            continue
        _, _, _, translated = _pipeline(detective, mapper, translator, filepath)
        for e in translated:
            assert not any("fell back to StatsBomb-standard" in line for line in e.mapping_log)


def test_translate_does_not_mutate_input_events(detective, mapper, translator):
    """translate() should return new objects, not mutate the MappedEvents
    passed in — downstream code (or a retry) shouldn't be surprised by
    silent mutation of something it still holds a reference to."""
    path = SAMPLE_MATCHES_DIR / "PL_2025_CLEAN_001" / "opta.json"
    data, detection, mapped, _ = _pipeline(detective, mapper, translator, path)
    original_x_values = [e.x for e in mapped]
    translator.translate(mapped)  # discard result
    assert [e.x for e in mapped] == original_x_values, "translate() mutated its input"
