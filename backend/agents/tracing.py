"""Optional TraceRoot tracing for the agent pipeline.

Tracing is off unless the `traceroot` package is installed and
TRACEROOT_API_KEY is set, so the pipeline and its tests still run fully
offline with nothing extra installed. When it is on, a pipeline run is
traced as:

  pipeline_run                  (root span, one per run)
    format_detective            (one span per stage attempt, so a stage
    schema_mapper                that fails and succeeds on retry shows
    ...                          both attempts, the first marked as an error)
      anthropic / groq calls    (auto-instrumented, under the stage that made them)

Inputs and outputs are not captured on agent spans, since a vendor payload
is thousands of events. Run-level details (status, retried and failed
stages, match id) go on the trace instead.
"""

from __future__ import annotations

import functools
import os
import threading
from typing import Any, Callable, Optional, TypeVar

try:
    import traceroot
except ImportError:  # optional dependency
    traceroot = None

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])

_lock = threading.Lock()
_enabled: Optional[bool] = None


def tracing_enabled() -> bool:
    """True once TraceRoot is initialized. Decided on first call, then cached."""
    global _enabled
    if _enabled is None:
        with _lock:
            if _enabled is None:
                _enabled = _initialize()
    return _enabled


def _initialize() -> bool:
    if traceroot is None or not os.getenv("TRACEROOT_API_KEY"):
        return False
    traceroot.initialize(
        integrations=[traceroot.Integration.ANTHROPIC, traceroot.Integration.GROQ],
    )
    return True


def traced(name: str, kind: str = "agent") -> Callable[[F], F]:
    """Give a function or generator a TraceRoot span when tracing is on.

    A passthrough when tracing is off. The span is created lazily so that
    importing this module never initializes TraceRoot.
    """

    def decorator(func: F) -> F:
        observed: Optional[Callable[..., Any]] = None

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal observed
            if not tracing_enabled():
                return func(*args, **kwargs)
            if observed is None:
                observed = traceroot.observe(
                    name=name, type=kind, capture_input=False, capture_output=False,
                )(func)
            return observed(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def run_in_span(name: str, fn: Callable[[], T], metadata: Optional[dict] = None) -> T:
    """Run fn() inside its own agent span. An exception marks the span as an
    error and is re-raised unchanged."""
    if not tracing_enabled():
        return fn()
    return traceroot.observe(
        name=name, type="agent", metadata=metadata, capture_input=False, capture_output=False,
    )(fn)()


def annotate_trace(session_id: Optional[str] = None, **metadata: Any) -> None:
    """Attach run-level details to the current trace. session_id groups every
    run of the same match (a live feed replay is many runs of one match)."""
    if tracing_enabled():
        details = {k: v for k, v in metadata.items() if v is not None}
        traceroot.update_current_trace(session_id=session_id, metadata=details or None)
