"""Data contracts between the agent stages.

Each stage reads one file and writes one, validated against a model here. The
recording pipeline's contracts are in `odoo_miner/models.py`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CONTRACT_VERSION = "1"

Outcome = Literal["completed", "failed", "abandoned", "recovered"]
SegmentKind = Literal["retrieval", "action", "navigation", "mixed"]
Decision = Literal["customize", "configure", "data_fix", "no_change"]


class RecordRef(BaseModel):
    """An Odoo record a segment worked on."""

    model: str
    record_id: int | None = None


class Segment(BaseModel):
    """A run of consecutive steps that accomplish one thing."""

    segment_id: str                       # "s01", "s02", ...
    step_indexes: list[int]               # Click.step_index values, in order
    label: str                            # "Create RFQ for Apex Guidewire"
    intent: str                           # short verb phrase: "create purchase order"
    record: RecordRef | None = None
    outcome: Outcome = "completed"


class SegmentLog(BaseModel):
    """Output of `odoo-miner segment`."""

    contract_version: str = CONTRACT_VERSION
    session: str
    segments: list[Segment]


class CodeRef(BaseModel):
    """A place in Odoo's source (or an addon) where behaviour lives."""

    module: str                           # "purchase"
    file: str                             # "addons/purchase/models/purchase_order.py"
    line: int
    symbol: str                           # "PurchaseOrder.button_confirm"


class QueryRef(BaseModel):
    """Data the user had to look up."""

    model: str
    method: str
    domain: list | None = None
    fields: list[str] | None = None


class TracedSegment(BaseModel):
    """One segment, explained against the backend code that ran."""

    segment_id: str
    kind: SegmentKind
    actions: list[CodeRef] = Field(default_factory=list)
    retrievals: list[QueryRef] = Field(default_factory=list)
    explanation: str = ""


# Separate from TracedSegment because QueryRef.domain is an untyped list, which
# strict structured output rejects; the retrievals come from the captured calls.
class TracedSegmentDraft(BaseModel):
    """What the tracer agent returns for one segment; code adds the retrievals."""

    segment_id: str
    kind: SegmentKind
    actions: list[CodeRef] = Field(default_factory=list)
    explanation: str = ""


class TraceLog(BaseModel):
    """Output of `odoo-miner trace`."""

    contract_version: str = CONTRACT_VERSION
    session: str
    segments: list[TracedSegment]


class StepDifficulty(BaseModel):
    """How hard one step was, and the signals behind the score."""

    step_index: int
    score: int = Field(ge=1, le=5)
    signals: dict[str, float] = Field(default_factory=dict)
    rationale: str = ""


class SegmentAssessment(BaseModel):
    """Effort and friction for one segment."""

    segment_id: str
    effort: float
    steps: list[StepDifficulty] = Field(default_factory=list)
    friction: list[str] = Field(default_factory=list)


class Assessment(BaseModel):
    """Output of `odoo-miner assess`."""

    contract_version: str = CONTRACT_VERSION
    session: str
    total_effort: float = 0.0
    segments: list[SegmentAssessment] = Field(default_factory=list)


class PlannedChange(BaseModel):
    """One file the plan adds or changes."""

    file: str                             # "addons/odoo_miner_bill_date/views/account_move_views.xml"
    kind: Literal["new_file", "inherit_view", "inherit_model", "data", "config"]
    description: str


class Plan(BaseModel):
    """Output of `odoo-miner plan`, and what a human approves."""

    contract_version: str = CONTRACT_VERSION
    decision: Decision
    target_segments: list[str] = Field(default_factory=list)
    summary: str
    changes: list[PlannedChange] = Field(default_factory=list)
    module_name: str | None = None
    expected_steps_before: int = 0
    expected_steps_after: int = 0
    acceptance_criteria: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    # Set by planner.check_citations after the model answers.
    unverified_citations: list[str] = Field(default_factory=list)


class BuildResult(BaseModel):
    """What the builder returns: tests, replay, effort before and after, and delivery."""

    contract_version: str = CONTRACT_VERSION
    branch: str = ""
    commit: str = ""
    pr_url: str = ""
    tests_passed: bool = False
    test_output_path: str = ""
    replay_completed: bool = False
    effort_before: float = 0.0
    effort_after: float = 0.0
    screenshots: list[str] = Field(default_factory=list)
    email_sent: bool = False
