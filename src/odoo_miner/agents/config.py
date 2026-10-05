"""Settings for the agent stages, driven by the environment.

Model names are never hard-coded at a call site; they come from here, which
reads `.env` (see `.env.example`) and falls back to sensible defaults.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

# Default models. `MODEL` does the judgement work; `FAST_MODEL` is for
# subagents and cheap checks. These are the current model ids - do not append
# a date suffix, the plain id is complete.
DEFAULT_MODEL = "anthropic:claude-sonnet-5"
DEFAULT_FAST_MODEL = "anthropic:claude-haiku-4-5"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Per-request timeout. Measured over 278 calls in the live runs: median 2.4 s,
# 95th percentile 13 s, slowest 59 s (the planner). Without a timeout the SDK
# waits 10 minutes per attempt, so one stalled connection froze a run for
# over five minutes before it was noticed. Two retries bound the worst case.
REQUEST_TIMEOUT_S = 120
MAX_RETRIES = 2


def _load_dotenv_once() -> None:
    """Load .env from the project root, if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:          # the library is optional for deterministic use
        return
    root = Path(__file__).resolve().parents[3]
    load_dotenv(root / ".env", override=False)


class Settings(BaseModel):
    """Everything the agent stages read from the environment."""

    model: str = DEFAULT_MODEL
    fast_model: str = DEFAULT_FAST_MODEL
    odoo_source: Path = Path("vendor/odoo")
    addons_dir: Path = Path("addons")
    odoo_url: str = "http://localhost:8069"
    odoo_db: str = "demo"
    odoo_user: str = "admin"
    odoo_password: str = "admin"
    # Difficulty weights; see assessor.py for how each signal is detected.
    weights: dict[str, float] = {
        "typing": 0.5,
        "lookup": 0.7,
        "tab_switch": 0.6,
        "screen_change": 0.8,
        "modal": 0.7,
        "error": 2.0,
        "backtrack": 1.0,
        "hidden_field": 1.0,
        "wasted_click": 0.4,
    }

    @property
    def odoo_source_abs(self) -> Path:
        return self.odoo_source if self.odoo_source.is_absolute() else Path.cwd() / self.odoo_source

    def prompt(self, name: str) -> str:
        """Read a prompt by name from agents/prompts/<name>.md."""
        path = PROMPTS_DIR / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"No prompt named {name!r} at {path}")
        return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Process-wide settings, read from the environment once."""
    _load_dotenv_once()
    env = os.environ
    return Settings(
        model=env.get("ODOO_MINER_MODEL", DEFAULT_MODEL),
        fast_model=env.get("ODOO_MINER_FAST_MODEL", DEFAULT_FAST_MODEL),
        odoo_source=Path(env.get("ODOO_SOURCE", "vendor/odoo")),
        addons_dir=Path(env.get("ODOO_MINER_ADDONS", "addons")),
        odoo_url=env.get("ODOO_URL", "http://localhost:8069"),
        odoo_db=env.get("ODOO_DB", "demo"),
        odoo_user=env.get("ODOO_USER", "admin"),
        odoo_password=env.get("ODOO_PASSWORD", "admin"),
    )


def load_prompt(name: str) -> str:
    return settings().prompt(name)


def trace_config(run: str, stage: str, **extra) -> dict:
    """LangSmith metadata so a stage's trace is findable.

    Passed as `config=` to any agent invoke; with LANGSMITH_TRACING=true the
    tracing itself needs no code.
    """
    return {
        "metadata": {"run": run, "stage": stage, **extra},
        "tags": ["odoo-miner", stage],
    }


def chat_model(stage: str):
    """The chat model for a stage, with a request timeout and bounded retries.

    Every agent builds its model here rather than passing a model string, so
    no stage can hang on a stalled connection.
    """
    from langchain.chat_models import init_chat_model

    return init_chat_model(model_for(stage), timeout=REQUEST_TIMEOUT_S, max_retries=MAX_RETRIES)


def model_for(stage: str) -> str:
    """The model a stage should use. Subagents and cheap checks get the fast one."""
    fast_stages = {"researcher", "fast"}
    s = settings()
    return s.fast_model if stage in fast_stages else s.model
