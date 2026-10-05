"""Parse Chrome DevTools Recorder exports (JSON) into a flat list of clicks.

Recorder format reference: https://github.com/puppeteer/replay (src/Schema.ts).
Note the format records the order of steps but no timestamps.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Click, ClickLog, Target
from .odoo_urls import parse_odoo_url

# Steps that describe the browser setup rather than user behavior.
SETUP_STEPS = {"setViewport", "emulateNetworkConditions"}
# Steps that duplicate information already in another step.
NOISE_STEPS = {"keyUp"}
# Browser-internal pages, e.g. the new tab a recording was started from.
BROWSER_PAGE = re.compile(r"^(chrome|about|edge|brave|chrome-search):")

_NAME_ATTR = re.compile(r"\[name=['\"]?([\w.\-]+)['\"]?\]")

# Values typed into fields like these are replaced at ingest. The analysis never
# needs them, and clicks.json / session.json are sent to the model and stored in
# traces. Replay reads the original recording, so it can still log in.
SECRET_FIELD = re.compile(r"passw(or)?d|\bpwd\b|passcode|secret|token|api[\s_-]?key|\botp\b|totp", re.I)
REDACTED = "<redacted>"


def is_secret_field(target: Target | None) -> bool:
    return target is not None and bool(SECRET_FIELD.search(" ".join(target.selectors)))


class RecordingError(ValueError):
    pass


def _flatten_selector(selector: Any) -> str:
    """A selector is a string, or a list of strings (ancestor chain through shadow roots/frames)."""
    if isinstance(selector, list):
        return " >> ".join(str(s) for s in selector)
    return str(selector)


_ROLE_SUFFIX = re.compile(r"\[role=.*\]$")
# Icon fonts draw their glyphs from the Unicode Private Use Area; they are not words.
_ICON_GLYPHS = re.compile("[\ue000-\uf8ff]")


def _aria_label(parts: list[str]) -> str | None:
    """Readable accessible name from an aria selector chain.

    The Recorder often targets an icon inside a button, giving chains like
    "aria/Save manually >> aria/[role="generic"]". The inner part has no name,
    so walk outwards to the nearest part that does.
    """
    for part in reversed(parts):
        if not part.startswith("aria/"):
            continue
        label = _ICON_GLYPHS.sub("", _ROLE_SUFFIX.sub("", part[len("aria/"):])).strip()
        if label:
            return label
    return None


def parse_target(raw_selectors: list | None) -> Target | None:
    if not raw_selectors:
        return None

    flat = [_flatten_selector(s) for s in raw_selectors]
    target = Target(selectors=flat)

    for sel in flat:
        parts = sel.split(" >> ")
        last = parts[-1]
        if last.startswith("aria/"):
            if target.aria_label is None:
                target.aria_label = _aria_label(parts)
        elif last.startswith("text/") and target.text is None:
            target.text = last[len("text/"):]
        elif not last.startswith(("xpath/", "pierce/", "aria/", "text/")) and target.css is None:
            target.css = sel

        if target.button_name is None:
            # The rightmost [name=...] is closest to the clicked element.
            names = _NAME_ATTR.findall(last)
            if names:
                target.button_name = names[-1]

    return target


def _navigation_url(step: dict) -> str | None:
    for event in step.get("assertedEvents") or []:
        if event.get("type") == "navigation" and event.get("url"):
            return event["url"]
    return None


def parse_recording(data: dict, source: str = "<memory>", keep_noise: bool = False) -> ClickLog:
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise RecordingError("Not a Chrome Recorder export: expected an object with a 'steps' list.")

    clicks: list[Click] = []
    current_url: str | None = None

    for step_index, step in enumerate(data["steps"]):
        step_type = step.get("type")
        if not step_type:
            raise RecordingError(f"Step {step_index} has no 'type'.")

        browser_page = step_type == "navigate" and bool(BROWSER_PAGE.match(step.get("url") or ""))
        if step_type == "navigate" and not browser_page:
            current_url = step.get("url") or current_url

        skip = (
            step_type in SETUP_STEPS
            or browser_page
            or (not keep_noise and step_type in NOISE_STEPS)
        )
        nav_url = _navigation_url(step)
        if nav_url and BROWSER_PAGE.match(nav_url):
            nav_url = None

        if not skip:
            page_url = step.get("url") if step_type == "navigate" else current_url
            target = parse_target(step.get("selectors"))
            value = step.get("value")
            if value is not None and is_secret_field(target):
                value = REDACTED
            clicks.append(
                Click(
                    index=len(clicks),
                    step_index=step_index,
                    type=step_type,
                    target=target,
                    value=value,
                    key=step.get("key"),
                    url=step.get("url") if step_type == "navigate" else None,
                    page_url=page_url,
                    page=parse_odoo_url(page_url),
                    navigates_to=nav_url,
                )
            )

        if nav_url:
            current_url = nav_url

    return ClickLog(source=source, title=data.get("title"), clicks=clicks)


def load_recording(path: Path, keep_noise: bool = False) -> ClickLog:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecordingError(f"{path} is not valid JSON: {exc}") from exc
    return parse_recording(data, source=str(path), keep_noise=keep_noise)
