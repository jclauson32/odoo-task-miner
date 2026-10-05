"""Shared state for the analysis pipeline.

File paths travel in the state, not whole documents: checkpoints stay small
and every intermediate result stays on disk where it can be inspected or a
single stage re-run.
"""

from __future__ import annotations

from typing import Annotated, Optional, TypedDict


def _extend(left: Optional[list], right: Optional[list]) -> list:
    """Reducer so nodes can append errors without clobbering each other."""
    return (left or []) + (right or [])


class PipelineState(TypedDict, total=False):
    run: str                      # run name, used in LangSmith metadata
    run_dir: str
    session_path: str
    segments_path: str
    traces_path: str
    assessment_path: str
    plan_path: str
    approval: dict                # {"approved": bool, "notes": str}
    build_path: str
    errors: Annotated[list[str], _extend]


# The pipeline's stages, in order. `--until` slices this list.
STAGES = ["segment", "trace", "assess", "plan", "approve", "build"]
