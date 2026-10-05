"""Turn a tool's failure into a message the model can read and act on.

An exception raised inside a tool propagates out of the agent and ends the
run - for the builder, possibly half an hour of work - over something as
small as a mistyped path. Wrapped tools return the error as text instead, so
the model can correct itself.

LangGraph's control-flow signals are exceptions too (an approval pause is a
GraphInterrupt, which subclasses Exception). They are re-raised untouched:
swallowing one would silently remove a human gate.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp


def reports_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn`, returning "error: ..." instead of raising. Name, signature and docstring are kept."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return f"error: {fn.__name__} failed - {type(exc).__name__}: {exc}"

    return wrapper
