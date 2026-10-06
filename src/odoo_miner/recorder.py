"""Parse Chrome DevTools Recorder exports (JSON) into a flat list of clicks.

Recorder format reference: https://github.com/puppeteer/replay (src/Schema.ts).
The format records the order of steps but no timestamps.
"""

from __future__ import annotations

import copy
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

# Values typed into these fields are redacted at ingest, because the analysis
# files are sent to the model. Replay reads the original recording.
SECRET_FIELD = re.compile(r"passw(or)?d|\bpwd\b|passcode|secret|token|api[\s_-]?key|\botp\b|totp", re.I)
REDACTED = "<redacted>"


def is_secret_field(target: Target | None) -> bool:
    """Whether a step targets a password, token or similar field."""
    return target is not None and bool(SECRET_FIELD.search(" ".join(target.selectors)))


class RecordingError(ValueError):
    """A file that is not a usable Chrome Recorder export."""


def _flatten_selector(selector: Any) -> str:
    """One selector as a string; a chain through shadow roots or frames is joined with " >> "."""
    if isinstance(selector, list):
        return " >> ".join(str(s) for s in selector)
    return str(selector)


_ROLE_SUFFIX = re.compile(r"\[role=.*\]$")
# Icon fonts draw their glyphs from the Unicode Private Use Area; they are not words.
_ICON_GLYPHS = re.compile("[\ue000-\uf8ff]")


def _aria_label(parts: list[str]) -> str | None:
    """The nearest readable accessible name in an aria selector chain.

    The Recorder often targets an icon inside a button, and the icon has no name.
    """
    for part in reversed(parts):
        if not part.startswith("aria/"):
            continue
        label = _ICON_GLYPHS.sub("", _ROLE_SUFFIX.sub("", part[len("aria/"):])).strip()
        if label:
            return label
    return None


def parse_target(raw_selectors: list | None) -> Target | None:
    """The element a step acted on, from the Recorder's selectors."""
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
    """The URL a step navigated to, from its asserted events."""
    for event in step.get("assertedEvents") or []:
        if event.get("type") == "navigation" and event.get("url"):
            return event["url"]
    return None


def parse_recording(data: dict, source: str = "<memory>", keep_noise: bool = False) -> ClickLog:
    """Turn a Recorder export into a click log, dropping setup and noise steps."""
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
    """Read and parse a Recorder export file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecordingError(f"{path} is not valid JSON: {exc}") from exc
    return parse_recording(data, source=str(path), keep_noise=keep_noise)


def _first_selector(step: dict) -> str | None:
    """A step's first selector, used to match steps across copies of a recording."""
    selectors = step.get("selectors") or []
    return _flatten_selector(selectors[0]) if selectors else None


def redact_recording(data: dict) -> dict:
    """A copy of a recording with secret values redacted, safe to give an agent."""
    out = copy.deepcopy(data)
    for step in out.get("steps", []):
        if step.get("value") is not None and is_secret_field(parse_target(step.get("selectors"))):
            step["value"] = REDACTED
    return out


def rehydrate_secrets(recording: dict, original: dict) -> dict:
    """Put redacted values back from the original recording, matching steps by first selector.

    Raises RecordingError if a redacted value has no match.
    """
    secrets = {
        _first_selector(step): step["value"]
        for step in original.get("steps", [])
        if step.get("value") not in (None, REDACTED) and _first_selector(step)
        and is_secret_field(parse_target(step.get("selectors")))
    }
    out = copy.deepcopy(recording)
    for step in out.get("steps", []):
        if step.get("value") == REDACTED:
            key = _first_selector(step)
            if key not in secrets:
                raise RecordingError(f"No original value for the redacted field {key!r}.")
            step["value"] = secrets[key]
    return out
