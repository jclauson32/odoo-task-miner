"""Shared state for the analysis pipeline.

The state carries file paths rather than documents, so checkpoints stay small
and every stage's output stays on disk.
"""

from __future__ import annotations

from typing import Annotated, TypedDict


def _extend(left: list | None, right: list | None) -> list:
    """Reducer that appends errors from each node instead of replacing them."""
    return (left or []) + (right or [])


class PipelineState(TypedDict, total=False):
    """What the pipeline passes from one node to the next."""

    run: str                      # run name, used in LangSmith metadata
    run_dir: str
    session_path: str
    segments_path: str
    traces_path: str
    assessment_path: str
    plan_path: str
    decision: str                 # the plan's decision; only "customize" is built
    approval: dict                # {"approved": bool, "notes": str}
    review_notes: str             # notes from a reviewer who sent the plan back
    plan_revisions: int           # how many times the plan has been sent back
    build_path: str
    errors: Annotated[list[str], _extend]


# The pipeline's stages, in order. `--until` slices this list.
STAGES = ["segment", "trace", "assess", "plan", "approve", "build"]
