"""Tests for backend/api/serialization.py's generic to_jsonable().

The bug this guards against: @property members on a dataclass (e.g.
TeamStats.avg_pass_distance_m, .pass_accuracy in narrative_generator.py)
are derived values, not stored fields -- dataclasses.fields() never sees
them. A naive dataclasses.asdict()-style walk silently drops them from
every API response instead of erroring, which is exactly what happened
the first time this serializer was wired into the frontend: the "avg
pass distance" column came back blank with no error anywhere.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "api"))
from serialization import to_jsonable  # noqa: E402


@dataclass
class Inner:
    value: int = 0

    @property
    def doubled(self) -> int:
        return self.value * 2


@dataclass
class Outer:
    name: str
    total: float = 0.0
    count: int = 0
    children: list = field(default_factory=list)

    @property
    def average(self) -> Optional[float]:
        if self.count == 0:
            return None
        return round(self.total / self.count, 3)


def test_plain_fields_survive():
    obj = Outer(name="a", total=10.0, count=4)
    assert to_jsonable(obj)["name"] == "a"
    assert to_jsonable(obj)["total"] == 10.0
    assert to_jsonable(obj)["count"] == 4


def test_property_included_alongside_fields():
    obj = Outer(name="a", total=10.0, count=4)
    result = to_jsonable(obj)
    assert "average" in result
    assert result["average"] == 2.5


def test_property_returning_none_is_preserved_not_dropped():
    obj = Outer(name="a", total=0.0, count=0)
    result = to_jsonable(obj)
    assert "average" in result
    assert result["average"] is None


def test_nested_dataclass_properties_also_included():
    obj = Outer(name="a", children=[Inner(value=3), Inner(value=5)])
    result = to_jsonable(obj)
    assert result["children"] == [{"value": 3, "doubled": 6}, {"value": 5, "doubled": 10}]


def test_dict_and_list_pass_through_recursively():
    assert to_jsonable({"a": Inner(value=2)}) == {"a": {"value": 2, "doubled": 4}}
    assert to_jsonable([Inner(value=1), 2, "x"]) == [{"value": 1, "doubled": 2}, 2, "x"]


def test_scalars_and_none_unchanged():
    assert to_jsonable(None) is None
    assert to_jsonable(5) == 5
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable("x") == "x"
    assert to_jsonable(True) is True
