"""Search Odoo's source: plain functions that the agents also use as tools.

The source is a read-only checkout (`ODOO_SOURCE`, default `vendor/odoo`):

    git clone --depth 1 -b 18.0 https://github.com/odoo/odoo vendor/odoo
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from ..config import settings
from ..contracts import CodeRef

# Directories inside a module that hold Python worth searching.
CODE_DIRS = ("models", "wizard", "wizards", "report", "controllers")
SKIP_PARTS = {"tests", "static", "node_modules", "__pycache__", ".git"}

MAX_READ_LINES = 400


class SourceUnavailable(RuntimeError):
    """The Odoo source checkout is missing."""


def source_root() -> Path:
    """The source checkout; raises SourceUnavailable if it is missing."""
    root = settings().odoo_source_abs
    if not root.exists():
        raise SourceUnavailable(
            f"Odoo source not found at {root}. Clone it with:\n"
            f"  git clone --depth 1 -b 18.0 https://github.com/odoo/odoo {settings().odoo_source}"
        )
    return root


def source_available() -> bool:
    """Whether the source checkout exists."""
    return settings().odoo_source_abs.exists()


def module_of(path: Path, root: Path) -> str:
    """The Odoo module a file belongs to, from its path."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return ""
    if "addons" in parts:
        i = parts.index("addons")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[0] if parts else ""


def relative(path: Path, root: Path) -> str:
    """`path` relative to `root`, or unchanged when it is outside it."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _grep_files(pattern: str, root: Path, include: str = "*.py") -> list[Path]:
    """Files containing a literal string, via grep. Falls back to a walk."""
    try:
        result = subprocess.run(
            ["grep", "-rl", "--include", include, "-F", pattern, str(root)],
            capture_output=True, text=True, timeout=120,
        )
        paths = [Path(p) for p in result.stdout.splitlines() if p]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        paths = [p for p in root.rglob(include) if pattern in _safe_read(p)]
    return [p for p in paths if not SKIP_PARTS & set(p.parts)]


def _safe_read(path: Path) -> str:
    """A file's text, or "" when it cannot be read."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _string_values(node: ast.AST) -> list[str]:
    """String constants in a node, whether it is a string or a list of them."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for element in node.elts:
            out += _string_values(element)
        return out
    return []


def model_names(cls: ast.ClassDef) -> set[str]:
    """Models a class defines or extends, from `_name` and `_inherit`."""
    names: set[str] = set()
    for stmt in cls.body:
        if not isinstance(stmt, ast.Assign):
            continue
        targets = {t.id for t in stmt.targets if isinstance(t, ast.Name)}
        if targets & {"_name", "_inherit"}:
            names.update(_string_values(stmt.value))
    return names


def _methods_in(cls: ast.ClassDef, method: str) -> Iterable[ast.FunctionDef]:
    """The definitions of `method` in a class body."""
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == method:
            yield stmt


def find_method(model: str, method: str, installed_only: bool = True) -> list[CodeRef]:
    """Find every definition of `method` on Odoo model `model`.

    Odoo modules override each other, so this returns the whole chain - all of
    them are the real behaviour. Results are filtered to installed modules
    when that list is available, which keeps uninstalled lookalikes out (for
    example `purchase_requisition` also defines `button_confirm` on
    `purchase.order`).

    Args:
        model: Odoo model name, e.g. "purchase.order".
        method: Python method name, e.g. "button_confirm".
        installed_only: drop modules that are not installed in the demo database.

    Returns:
        A CodeRef per definition found, module then file order.
    """
    root = source_root()
    refs: list[CodeRef] = []

    for path in _grep_files(f"def {method}", root):
        if not any(part in CODE_DIRS for part in path.parts):
            continue
        text = _safe_read(path)
        if model not in text:            # cheap reject before parsing
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or model not in model_names(node):
                continue
            for fn in _methods_in(node, method):
                refs.append(
                    CodeRef(
                        module=module_of(path, root),
                        file=relative(path, root),
                        line=fn.lineno,
                        symbol=f"{node.name}.{method}",
                    )
                )

    if installed_only:
        allowed = installed_modules()
        if allowed:
            refs = [r for r in refs if r.module in allowed] or refs
    return sorted(refs, key=lambda r: (r.module, r.file, r.line))


_BUTTON_RE = re.compile(r"<button\b[^>]*>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def find_button(name: str, model: str | None = None) -> list[CodeRef]:
    """Find view buttons whose `name` attribute is `name`.

    A button's `type` says what the name means: `object` is a Python method on
    the record's model, `action` is a window action id.

    Args:
        name: the button's name attribute, e.g. "button_confirm".
        model: optional Odoo model, used to prefer buttons in matching files.

    Returns:
        A CodeRef per button; `symbol` carries the button type.
    """
    root = source_root()
    refs: list[CodeRef] = []

    for path in _grep_files(f'name="{name}"', root, include="*.xml"):
        text = _safe_read(path)
        for match in _BUTTON_RE.finditer(text):
            attrs = dict(_ATTR_RE.findall(match.group(0)))
            if attrs.get("name") != name:
                continue
            line = text.count("\n", 0, match.start()) + 1
            refs.append(
                CodeRef(
                    module=module_of(path, root),
                    file=relative(path, root),
                    line=line,
                    symbol=f"button[type={attrs.get('type', 'object')}] {name}",
                )
            )

    allowed = installed_modules()
    if allowed:
        refs = [r for r in refs if r.module in allowed] or refs
    if model:
        stem = model.replace(".", "_")
        refs.sort(key=lambda r: (stem not in r.file, r.module, r.line))
    return refs


def read_source(file: str, start: int = 1, end: int | None = None) -> str:
    """Read a bounded range of lines from Odoo's source or this project's addons.

    Args:
        file: path as returned by find_method/find_button, relative to the
            Odoo source root (or to the project, for addons/).
        start: first line, 1-indexed.
        end: last line. Defaults to 400 lines after `start`.

    Returns:
        The lines, each prefixed with its number.
    """
    end = end or start + MAX_READ_LINES - 1
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start}).")
    end = min(end, start + MAX_READ_LINES - 1)

    candidate = _resolve_readable(file)
    lines = _safe_read(candidate).splitlines()
    chunk = lines[max(start - 1, 0):end]
    width = len(str(min(end, len(lines))))
    return "\n".join(f"{start + i:>{width}} {line}" for i, line in enumerate(chunk))


def _resolve_readable(file: str) -> Path:
    """Resolve a path, refusing anything outside the Odoo source or addons/."""
    raw = Path(file)
    roots = []
    if source_available():
        roots.append(settings().odoo_source_abs.resolve())
    addons = (Path.cwd() / settings().addons_dir).resolve()
    roots.append(addons)

    candidates = [raw] if raw.is_absolute() else [root / raw for root in roots]
    # "addons/purchase/..." may be relative to the Odoo root or to the project.
    candidates += [Path.cwd() / raw]

    missing_inside = False
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not any(resolved == root or root in resolved.parents for root in roots):
            continue
        if resolved.is_file():
            return resolved
        missing_inside = True

    if missing_inside:
        raise FileNotFoundError(
            f"No such file: {file}. Paths are relative to the Odoo source root, e.g. "
            "addons/purchase/models/purchase_order.py; find_method and find_button return exact paths."
        )
    raise PermissionError(
        f"{file} is outside the Odoo source checkout and addons/; refusing to read it."
    )


_FIELD_RE = re.compile(r'<field\b[^>]*\bname="([^"]+)"[^>]*>?', re.DOTALL)
_RECORD_SPLIT_RE = re.compile(r"(?=<record\b)")
_PAGE_RE = re.compile(r'<page\b[^>]*\bstring="([^"]*)"')


def find_view_fields(model: str, field: str | None = None) -> list[dict]:
    """Where a model's views put its fields, and what hides them.

    Used to tell whether a field the user edited was behind a notebook page or
    conditionally invisible.

    Args:
        model: Odoo model name, e.g. "account.move".
        field: optional field name to restrict the answer to.

    Returns:
        One dict per field occurrence: field, string (its label, when the view
        sets one), file, line, page (the notebook page's label, if any) and
        invisible (the modifier, if any).
    """
    root = source_root()
    # Views declare their model as element text, and one file often holds views
    # for several models, so each <record> is scanned on its own.
    declaration = f'name="model">{model}<'
    out: list[dict] = []

    for path in _grep_files(f">{model}<", root, include="*.xml"):
        text = _safe_read(path)
        if declaration not in text:
            continue
        offset = 0
        for record in _RECORD_SPLIT_RE.split(text):
            length = len(record)
            if declaration in record:
                out += _fields_in_record(record, path, root, offset, text, field)
            offset += length
    return out


def _fields_in_record(
    record: str, path: Path, root: Path, offset: int, whole: str, wanted: str | None
) -> list[dict]:
    """Fields inside one <record>, with the notebook page each one sits in."""
    base_line = whole.count("\n", 0, offset) + 1
    out: list[dict] = []
    page: str | None = None

    for number, line in enumerate(record.splitlines()):
        if "<page" in line:
            label = _PAGE_RE.search(line)
            if label:
                page = label.group(1)
        if "</notebook>" in line:
            page = None

        match = _FIELD_RE.search(line)
        if not match:
            continue
        name = match.group(1)
        if name == "model" or (wanted and name != wanted):
            continue
        label = re.search(r'string="([^"]*)"', line)
        invisible = re.search(r'invisible="([^"]*)"', line)
        out.append({
            "field": name,
            "string": label.group(1) if label else None,
            "file": relative(path, root),
            "line": base_line + number,
            "page": page,
            "invisible": invisible.group(1) if invisible else None,
        })
    return out


# ---------------------------------------------------------------- installed modules


# How long a fetched module list is trusted before asking Odoo again.
INSTALLED_MODULES_TTL = 6 * 3600


def _modules_cache() -> Path:
    """Where this database's installed-module list is cached."""
    return Path("out") / ".cache" / f"installed_modules.{settings().odoo_db}.json"


@lru_cache(maxsize=1)
def installed_modules() -> frozenset[str]:
    """Modules installed in the demo database, asked over JSON-RPC.

    Cached on disk for INSTALLED_MODULES_TTL. When Odoo cannot be reached, an
    expired cache is used, or else an empty set, which callers take as "do not filter".
    """
    cache = _modules_cache()
    cached: frozenset[str] = frozenset()
    if cache.exists():
        try:
            cached = frozenset(json.loads(cache.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            cached = frozenset()
        if cached and time.time() - cache.stat().st_mtime < INSTALLED_MODULES_TTL:
            return cached

    names = _fetch_installed_modules()
    if not names:
        return cached
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")
    return frozenset(names)


def _fetch_installed_modules() -> set[str]:
    """Ask Odoo for its installed modules; empty when it cannot be reached."""
    import urllib.error
    import urllib.request

    s = settings()

    def call(service: str, method: str, args: list):
        """One JSON-RPC call to Odoo."""
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }).encode()
        request = urllib.request.Request(
            f"{s.odoo_url}/jsonrpc", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read())
        if "error" in body:
            raise RuntimeError(body["error"])
        return body["result"]

    try:
        uid = call("common", "login", [s.odoo_db, s.odoo_user, s.odoo_password])
        if not uid:
            return set()
        records = call("object", "execute_kw", [
            s.odoo_db, uid, s.odoo_password,
            "ir.module.module", "search_read",
            [[["state", "=", "installed"]]], {"fields": ["name"]},
        ])
    except (urllib.error.URLError, OSError, RuntimeError, TimeoutError):
        return set()
    return {r["name"] for r in records if r.get("name")}


def find_view_pages(model: str) -> list[str]:
    """The notebook page labels a model's views define, e.g. "General Information".

    A click on one of these is a tab switch; Odoo 18's tabs have no class to
    match on, so the label is the signal.

    Args:
        model: Odoo model name, e.g. "product.template".

    Returns:
        Page labels, de-duplicated.
    """
    root = source_root()
    declaration = f'name="model">{model}<'
    pages: set[str] = set()

    for path in _grep_files(f">{model}<", root, include="*.xml"):
        text = _safe_read(path)
        if declaration not in text:
            continue
        for record in _RECORD_SPLIT_RE.split(text):
            if declaration in record:
                pages.update(_PAGE_RE.findall(record))
    return sorted(pages)


_ALL = (find_method, find_button, read_source, find_view_fields, find_view_pages)


def agent_tools(*names: str) -> list:
    """The named functions as agent tools, all of them by default.

    Their docstrings are the descriptions the model reads; errors come back as text.
    """
    from .errors import reports_errors

    chosen = [fn for fn in _ALL if not names or fn.__name__ in names]
    unknown = set(names) - {fn.__name__ for fn in chosen}
    if unknown:
        raise ValueError(f"Unknown tools: {sorted(unknown)}")
    return [reports_errors(fn) for fn in chosen]


TOOLS = agent_tools()
