"""Generic, dependency-free serialization for the agent pipeline's return
types.

Every agent in this codebase returns either a plain @dataclass (Narrative
Generator, Explainability Engine, Overlay Renderer, Personalization
Engine, the orchestrator's own log/result types) or a Pydantic model
(CDFDocument and friends, from Validator). Rather than hand-write a
to_dict for each of the ~15 types across the pipeline -- which would need
updating every time a field is added upstream, as happened repeatedly
during this project -- this walks either shape generically and converts
nested dataclasses/models/enums the same way.
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
from typing import Any

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - fastapi pulls pydantic in transitively
    BaseModel = None  # type: ignore


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if BaseModel is not None and isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result = {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        # @property members (e.g. TeamStats.avg_pass_distance_m, .pass_accuracy)
        # are derived values intentionally kept out of the stored fields --
        # dataclasses.fields() never sees them, so without this they'd
        # silently vanish from every API response instead of erroring.
        for name, member in inspect.getmembers(type(obj), lambda m: isinstance(m, property)):
            if name not in result:
                result[name] = to_jsonable(getattr(obj, name))
        return result
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_jsonable(v) for v in obj]
    # Fallback for anything else JSON-ish passed straight through (datetime
    # objects, etc.) -- str() is deliberately the last resort, not the
    # first, since every type above is handled exactly.
    return str(obj)
