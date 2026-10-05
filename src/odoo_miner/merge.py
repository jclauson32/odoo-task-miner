"""Attach captured backend calls to the clicks that triggered them.

Also gives each call a first-pass `kind` (read / write / compute / action_load).
This is a deterministic hint for tracer_agent, not a final answer: Odoo modules
can define any method name, so anything unrecognized is left as "unknown".
"""

from __future__ import annotations

from collections import defaultdict

from .models import ClickLog, NetworkCall, NetworkLog, Session, SessionClick

READ_METHODS = {
    "read", "search", "search_read", "search_count", "name_search", "name_get",
    "web_read", "web_search_read", "web_name_search", "web_read_group", "read_group",
    "read_progress_bar", "fields_get", "get_views", "load_views", "default_get",
    "check_access_rights", "has_group", "get_formview_action", "get_formview_id",
}
COMPUTE_METHODS = {"onchange", "web_override_translations"}
WRITE_METHODS = {
    "create", "write", "unlink", "copy", "web_save", "toggle_active",
    "action_archive", "action_unarchive", "message_post",
}
WRITE_PREFIXES = ("action_", "button_")
# Odoo's convention for smart buttons: action_view_* opens related records and
# returns an action; it changes nothing. Checked before the write rules, which
# would otherwise count every such hop as a write.
NAVIGATION_PREFIXES = ("action_view_",)


def classify(call: NetworkCall) -> str:
    endpoint = call.endpoint or ""
    method = call.method or ""

    if endpoint.endswith("/web/action/load") or method.startswith(NAVIGATION_PREFIXES):
        return "action_load"
    if "/web/dataset/call_button" in endpoint:
        return "write"
    if method in READ_METHODS:
        return "read"
    if method in COMPUTE_METHODS:
        return "compute"
    if method in WRITE_METHODS or method.startswith(WRITE_PREFIXES):
        return "write"
    return "unknown"


def merge(clicks: ClickLog, network: NetworkLog | None) -> Session:
    by_step: dict[int, list[NetworkCall]] = defaultdict(list)
    unattributed: list[NetworkCall] = []
    step_indexes = {c.step_index for c in clicks.clicks}

    if network:
        for call in network.calls:
            call = call.model_copy(update={"kind": classify(call)})
            if call.step_index in step_indexes:
                by_step[call.step_index].append(call)
            else:
                # Before the first step, or during a step ingest dropped (e.g. setViewport).
                unattributed.append(call)

    session_clicks = []
    for click in clicks.clicks:
        calls = by_step.get(click.step_index, [])
        session_clicks.append(
            SessionClick(
                **click.model_dump(),
                calls=calls,
                has_write=any(c.kind == "write" for c in calls),
            )
        )

    return Session(
        source=clicks.source,
        title=clicks.title,
        replay_completed=network.completed if network else None,
        clicks=session_clicks,
        unattributed_calls=unattributed,
    )
