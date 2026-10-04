"""Pull Odoo context (model, record, action, view) out of web client URLs.

Odoo has used two URL styles:

* Legacy (up to 17.0): everything in the hash fragment
  /web#id=1042&model=account.move&view_type=form&action=245&menu_id=115
* Newer (17.2+ / 18): readable paths that encode the breadcrumb stack
  /odoo/action-245/1042                 bill 1042 opened from action 245
  /odoo/action-245/1042/action-388/77   ...then drilled into record 77 of action 388
  /odoo/purchase/7                      action path ("purchase") with record 7
  /odoo/account.move/1042  /odoo/m-website/3   model-based screens

The path is parsed the way Odoo 18's router does it (web/static/src/core/browser/
router.js): "action-<id|xmlid>" is an action, "m-<model>" or a segment with a dot
is a model, any other word is an action path/tag, and a number (or "new") is the
record of the screen before it. The *last* screen in the stack is the one the
user is looking at, so that is what ends up in the context.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlparse

from .models import OdooContext


def _to_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _first(params: dict[str, list[str]], key: str) -> Optional[str]:
    values = params.get(key)
    return values[0] if values else None


def parse_odoo_url(url: Optional[str]) -> OdooContext:
    if not url:
        return OdooContext()

    parsed = urlparse(url)
    ctx = OdooContext()

    # Legacy hash style, also tolerate the same keys in the query string.
    params = parse_qs(parsed.fragment)
    for key, vals in parse_qs(parsed.query).items():
        params.setdefault(key, vals)

    if params:
        ctx.model = _first(params, "model")
        ctx.record_id = _to_int(_first(params, "id"))
        ctx.action = _first(params, "action")
        ctx.view_type = _first(params, "view_type")
        ctx.menu_id = _to_int(_first(params, "menu_id"))

    # Newer path style.
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[0] in ("odoo", "scoped_app"):
        rest = segments[1:]
        ctx.path_slugs = rest

        action = model = None
        record: Optional[int] = None
        is_new = False
        for seg in rest:
            if seg.isdigit():
                record, is_new = int(seg), False
            elif seg == "new":
                record, is_new = None, True
            else:
                # A new screen in the breadcrumb stack: forget the previous one.
                record, is_new, action, model = None, False, None, None
                if seg.startswith("action-"):
                    action = seg[len("action-"):]
                elif seg.startswith("m-"):
                    model = seg[len("m-"):]
                elif "." in seg:
                    model = seg
                else:
                    action = seg  # action path or client action tag, e.g. "purchase"

        ctx.action = ctx.action or action
        ctx.model = ctx.model or model
        if ctx.record_id is None:
            ctx.record_id = record
        if ctx.view_type is None and (record is not None or is_new):
            ctx.view_type = "form"  # a record (or "new") in the URL means a form view

    return ctx
