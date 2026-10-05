"""Data contracts shared by every pipeline stage.

Each stage reads and writes JSON that validates against these models, so a
stage can be inspected, re-run, or swapped out on its own.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"


class OdooContext(BaseModel):
    """What the Odoo URL says about the screen the user was on."""

    model: Optional[str] = None          # e.g. "account.move"
    record_id: Optional[int] = None      # e.g. 1042
    action: Optional[str] = None         # action id or xml id
    view_type: Optional[str] = None      # "form", "list", "kanban", ...
    menu_id: Optional[int] = None
    path_slugs: list[str] = Field(default_factory=list)  # Odoo 17.2+/18 path segments


class Target(BaseModel):
    """The element a step acted on, as described by the Recorder's selectors."""

    selectors: list[str] = Field(default_factory=list)  # flattened, best-first
    aria_label: Optional[str] = None     # from "aria/..." selectors
    text: Optional[str] = None           # from "text/..." selectors
    button_name: Optional[str] = None    # from [name="..."] (Odoo method or field name)
    css: Optional[str] = None            # first plain CSS selector


class Click(BaseModel):
    """One user interaction from the recording."""

    index: int                           # position in our output
    step_index: int                      # position in the original recording (replay uses this)
    type: str                            # click, change, navigate, keyDown, scroll, ...
    target: Optional[Target] = None
    value: Optional[str] = None          # typed value for "change" steps
    key: Optional[str] = None            # key for keyDown steps
    url: Optional[str] = None            # destination for "navigate" steps
    page_url: Optional[str] = None       # URL the user was on when the step happened
    page: OdooContext = Field(default_factory=OdooContext)
    navigates_to: Optional[str] = None   # URL the step led to, from assertedEvents


RpcKind = Literal["read", "write", "compute", "action_load", "unknown"]


class NetworkCall(BaseModel):
    """One frontend-to-backend call captured during replay."""

    step_index: int                      # recording step that was running (-1 = before first step)
    timestamp_ms: float                  # wall clock, from the replay (not human timing)
    endpoint: str                        # URL path, e.g. /web/dataset/call_kw/account.move/web_read
    model: Optional[str] = None
    method: Optional[str] = None
    args: Optional[list] = None
    kwargs: Optional[dict] = None
    status: Optional[int] = None
    rpc_error: Optional[str] = None
    kind: RpcKind = "unknown"            # filled in by merge


class ClickLog(BaseModel):
    """Output of `odoo-miner ingest`."""

    schema_version: str = SCHEMA_VERSION
    source: str
    title: Optional[str] = None
    clicks: list[Click]


class NetworkLog(BaseModel):
    """Output of the replay capture script."""

    schema_version: str = SCHEMA_VERSION
    recording: str
    started_at: Optional[str] = None
    completed: bool = True
    failed_step: Optional[int] = None
    error: Optional[str] = None
    failed_url: Optional[str] = None
    failure_screenshot: Optional[str] = None
    # Which of the Recorder's alternative selectors replay used, per step index.
    selectors_used: dict[str, Optional[str]] = Field(default_factory=dict)
    calls: list[NetworkCall]


class SessionClick(Click):
    calls: list[NetworkCall] = Field(default_factory=list)
    has_write: bool = False


class Session(BaseModel):
    """Output of `odoo-miner merge`: clicks with their backend calls attached."""

    schema_version: str = SCHEMA_VERSION
    source: str
    title: Optional[str] = None
    replay_completed: Optional[bool] = None
    clicks: list[SessionClick]
    unattributed_calls: list[NetworkCall] = Field(default_factory=list)
