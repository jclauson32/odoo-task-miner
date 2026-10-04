"""Parse Chrome DevTools Recorder exports (JSON) into a flat list of clicks.

Recorder format reference: https://github.com/puppeteer/replay (src/Schema.ts).
Note the format records the order of steps but no timestamps.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .models import Click, ClickLog, Target
from .odoo_urls import parse_odoo_url

# Steps that describe the browser setup rather than user behavior.
SETUP_STEPS = {"setViewport", "emulateNetworkConditions"}
# Steps that duplicate information already in another step.
NOISE_STEPS = {"keyUp"}

_NAME_ATTR = re.compile(r"\[name=['\"]?([\w.\-]+)['\"]?\]")


class RecordingError(ValueError):
    pass


def _flatten_selector(selector: Any) -> str:
    """A selector is a string, or a list of strings (ancestor chain through shadow roots/frames)."""
    if isinstance(selector, list):
        return " >> ".join(str(s) for s in selector)
    return str(selector)


def parse_target(raw_selectors: Optional[list]) -> Optional[Target]:
    if not raw_selectors:
        return None

    flat = [_flatten_selector(s) for s in raw_selectors]
    target = Target(selectors=flat)

    for sel in flat:
        last = sel.split(" >> ")[-1]
        if last.startswith("aria/") and target.aria_label is None:
            target.aria_label = last[len("aria/"):]
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


def _navigation_url(step: dict) -> Optional[str]:
    for event in step.get("assertedEvents") or []:
        if event.get("type") == "navigation" and event.get("url"):
            return event["url"]
    return None


def parse_recording(data: dict, source: str = "<memory>", keep_noise: bool = False) -> ClickLog:
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise RecordingError("Not a Chrome Recorder export: expected an object with a 'steps' list.")

    clicks: list[Click] = []
    current_url: Optional[str] = None

    for step_index, step in enumerate(data["steps"]):
        step_type = step.get("type")
        if not step_type:
            raise RecordingError(f"Step {step_index} has no 'type'.")

        if step_type == "navigate":
            current_url = step.get("url") or current_url

        skip = step_type in SETUP_STEPS or (not keep_noise and step_type in NOISE_STEPS)
        nav_url = _navigation_url(step)

        if not skip:
            page_url = step.get("url") if step_type == "navigate" else current_url
            clicks.append(
                Click(
                    index=len(clicks),
                    step_index=step_index,
                    type=step_type,
                    target=parse_target(step.get("selectors")),
                    value=step.get("value"),
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
