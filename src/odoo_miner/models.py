"""Data contracts shared by every pipeline stage.

Each stage reads and writes JSON that validates against these models, so a
stage can be inspected, re-run, or swapped out on its own.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"


class OdooContext(BaseModel):
    """What the Odoo URL says about the screen the user was on."""

    model: str | None = None          # e.g. "account.move"
    record_id: int | None = None      # e.g. 1042
    action: str | None = None         # action id or xml id
    view_type: str | None = None      # "form", "list", "kanban", ...
    menu_id: int | None = None
    path_slugs: list[str] = Field(default_factory=list)  # Odoo 17.2+/18 path segments


class Target(BaseModel):
    """The element a step acted on, as described by the Recorder's selectors."""

    selectors: list[str] = Field(default_factory=list)  # flattened, best-first
    aria_label: str | None = None     # from "aria/..." selectors
    text: str | None = None           # from "text/..." selectors
    button_name: str | None = None    # from [name="..."] (Odoo method or field name)
    css: str | None = None            # first plain CSS selector


class Click(BaseModel):
    """One user interaction from the recording."""

    index: int                           # position in our output
    step_index: int                      # position in the original recording (replay uses this)
    type: str                            # click, change, navigate, keyDown, scroll, ...
    target: Target | None = None
    value: str | None = None          # typed value for "change" steps
    key: str | None = None            # key for keyDown steps
    url: str | None = None            # destination for "navigate" steps
    page_url: str | None = None       # URL the user was on when the step happened
    page: OdooContext = Field(default_factory=OdooContext)
    navigates_to: str | None = None   # URL the step led to, from assertedEvents

    def target_label(self) -> str:
        """The most readable name for what the step acted on, or its URL."""
        t = self.target
        if not t:
            return self.url or ""
        return t.aria_label or t.text or t.button_name or t.css or ""


RpcKind = Literal["read", "write", "compute", "action_load", "unknown"]


class NetworkCall(BaseModel):
    """One frontend-to-backend call captured during replay."""

    step_index: int                      # recording step that was running (-1 = before first step)
    timestamp_ms: float                  # wall clock, from the replay (not human timing)
    endpoint: str                        # URL path, e.g. /web/dataset/call_kw/account.move/web_read
    model: str | None = None
    method: str | None = None
    args: list | None = None
    kwargs: dict | None = None
    status: int | None = None
    rpc_error: str | None = None
    kind: RpcKind = "unknown"            # filled in by merge


class ClickLog(BaseModel):
    """Output of `odoo-miner ingest`."""

    schema_version: str = SCHEMA_VERSION
    source: str
    title: str | None = None
    clicks: list[Click]


class NetworkLog(BaseModel):
    """Output of the replay capture script."""

    schema_version: str = SCHEMA_VERSION
    recording: str
    started_at: str | None = None
    completed: bool = True
    failed_step: int | None = None
    error: str | None = None
    failed_url: str | None = None
    failure_screenshot: str | None = None
    # Which of the Recorder's alternative selectors replay used, per step index.
    selectors_used: dict[str, str | None] = Field(default_factory=dict)
    calls: list[NetworkCall]


class SessionClick(Click):
    """A click with the backend calls it triggered."""

    calls: list[NetworkCall] = Field(default_factory=list)
    has_write: bool = False


class Session(BaseModel):
    """Output of `odoo-miner merge`: clicks with their backend calls attached."""

    schema_version: str = SCHEMA_VERSION
    source: str
    title: str | None = None
    replay_completed: bool | None = None
    clicks: list[SessionClick]
    unattributed_calls: list[NetworkCall] = Field(default_factory=list)
