"""Report a tool's failure to the model instead of ending the run.

LangGraph's control-flow signals are re-raised untouched: an approval pause is
a GraphInterrupt, which subclasses Exception, and swallowing it would remove a
human gate.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp


def reports_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap `fn` to return "error: ..." instead of raising, keeping its name and docstring."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        """Call `fn`, returning an exception as an error message."""
        try:
            return fn(*args, **kwargs)
        except GraphBubbleUp:
            raise
        except Exception as exc:
            return f"error: {fn.__name__} failed - {type(exc).__name__}: {exc}"

    return wrapper
