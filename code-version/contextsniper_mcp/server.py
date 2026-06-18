"""MCP stdio server exposing ContextSniper code search in the indevelopment style."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP

from contextsniper_mcp.http_client import identity_fields, post_json

app = FastMCP("contextsniper")

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"


def _canonical_code_query(query: str) -> str:
    """Normalize common code-search phrases to the L0 abstract shape."""
    q = (query or "").strip()
    if not q:
        return q

    patterns = (
        (rf"^function\s+({_IDENT})$", "{symbol} function"),
        (rf"^def\s+({_IDENT})$", "{symbol} function"),
        (rf"^async\s+function\s+({_IDENT})$", "{symbol} async function"),
        (rf"^async\s+def\s+({_IDENT})$", "{symbol} async function"),
        (rf"^type\s+({_IDENT})$", "{symbol} type"),
    )
    for pattern, template in patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            return template.format(symbol=match.group(1))
    return q


def _resolve_workspace_path(workspace_root: str, file_path: str) -> Path:
    root = Path(workspace_root).expanduser().resolve()
    target = Path(file_path).expanduser()
    target = target if target.is_absolute() else (root / target)
    target = target.resolve()
    active_raw = os.environ.get("CONTEXTSNIPER_WORKSPACE_ROOT", "") or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if active_raw:
        active_root = Path(active_raw).expanduser().resolve()
        active_target = Path(file_path).expanduser()
        active_target = active_target if active_target.is_absolute() else (active_root / active_target)
        active_target = active_target.resolve()
        if not target.exists() and active_target.exists():
            root = active_root
            target = active_target
    target.relative_to(root)
    return target


def _workspace_root_arg(path: str | None) -> str | None:
    raw = (path or "").strip()
    if raw in {"", "$WORK_DIR", "${WORK_DIR}", "$PWD", "${PWD}"}:
        raw = os.environ.get("CONTEXTSNIPER_WORKSPACE_ROOT", "") or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.environ.get("PWD", "")
    if not raw:
        return None
    return str(Path(os.path.expandvars(raw)).expanduser().resolve())


def _effective_search_limit(requested_limit: int) -> int:
    forced_raw = (os.environ.get("CONTEXTSNIPER_SEARCH_FORCE_LIMIT") or "").strip()
    if forced_raw:
        try:
            return max(1, min(100, int(forced_raw)))
        except ValueError:
            pass
    return int(requested_limit)


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _format_search_snippets(result: dict) -> str:
    """Return only the L2 snippets needed for editing unless search debug is enabled."""
    if _env_flag("CONTEXTSNIPER_SEARCH_DEBUG") or _env_flag("CONTEXTSNIPER_SEARCH_INCLUDE_DEBUG"):
        return json.dumps(result, indent=2)

    lines: list[str] = []
    query = str(result.get("query") or "").strip()
    if query:
        lines.append(f"# search_code query: {query}")
    hits = [hit for hit in (result.get("hits") or []) if isinstance(hit, dict)]
    lines.append(f"# hit_count: {len(hits)}")

    for index, hit in enumerate(hits, start=1):
        excerpt = str(hit.get("content_excerpt") or "").strip()
        if not excerpt:
            excerpt = str(hit.get("abstract") or "").strip()
        if not excerpt:
            continue
        if lines:
            lines.append("")
        lines.append(f"# hit {index}")
        lines.append(excerpt)

    return "\n".join(lines).rstrip() + "\n"


@app.tool(
    description="""\
Reserved no-op for explicit workspace indexing.

`search_code` retrieves from local snippets by default, so this tool only
reports the resolved workspace path.
""",
)
def index_codebase(
    path: Annotated[str, "Absolute path to the workspace directory to index."],
    force: Annotated[bool, "Reserved; re-index override (currently ignored)."] = False,
) -> str:
    return json.dumps(
        {
            "ok": True,
            "skipped": True,
            "reason": "search_code uses local snippets by default; explicit indexing is optional",
            "workspace_root": path,
            "force": force,
            "next_step": "Call search_code with a focused query.",
        },
        indent=2,
    )


@app.tool(
    description="""\
Search indexed code memories using a natural language query.

Returns only the top L2 code snippets by default. Each snippet keeps its
`# repo/path (lines start-end)` header so it can be edited directly. Set
`CONTEXTSNIPER_SEARCH_DEBUG=1` or `CONTEXTSNIPER_SEARCH_INCLUDE_DEBUG=1` to return the raw backend
payload with bootstrap, scores, and graph/debug metadata.

MCP requirement: before editing source/code files, call this tool at least
once with a focused query. Treat this as the preferred replacement for broad
grep and exploratory Read. Use 1-2 focused symbol-level searches when possible.
If snippets already identify the target file and line context, proceed directly
to `mcp__contextsniper__edit_file` or the next concrete verification step instead
of reading many unrelated files.
""",
)
def search_code(
    query: Annotated[str, "Natural language or keyword query."],
    path: Annotated[str, "Workspace root. Leave empty to use CONTEXTSNIPER_WORKSPACE_ROOT, then Claude's launch/project directory."] = "",
    limit: Annotated[int, "Max hits (1-100)."] = 5,
    glob_patterns: Annotated[str, "Optional comma-separated glob patterns to narrow candidate files before ranking."] = "",
    grep_terms: Annotated[str, "Optional comma-separated grep keywords/symbols to narrow candidate files before ranking."] = "",
) -> str:
    workspace_root = _workspace_root_arg(path)
    body = identity_fields(
        {
            "workspaceRoot": workspace_root,
            "query": _canonical_code_query(query),
            "limit": _effective_search_limit(limit),
            "glob_patterns": glob_patterns or None,
            "grep_terms": grep_terms or None,
            "waitForFullWorkspaceIndex": False,
        }
    )
    result = post_json("/api/v1/call/code_semantic_search", body)
    return _format_search_snippets(result)


@app.tool(
    description="""\
Edit a file via exact string replacement without requiring a prior Read tool call.

This is an MCP replacement for Claude's built-in Edit tool:
- validates the target is under `workspace_root`
- replaces `old_string` with `new_string`
- can replace once or all matches

Use this for source/code edits after locating the relevant file with
`mcp__contextsniper__search_code`. Prefer it over Claude's built-in Edit tool for
files found through ContextSniper.
""",
)
def edit_file(
    file_path: Annotated[str, "Absolute or workspace-relative target file path."],
    old_string: Annotated[str, "Exact text to replace."],
    new_string: Annotated[str, "Replacement text."],
    workspace_root: Annotated[str, "Workspace root used to validate the target path. Leave empty to use CONTEXTSNIPER_WORKSPACE_ROOT, then Claude's launch/project directory."] = "",
    replace_all: Annotated[bool, "Replace every occurrence instead of exactly one."] = False,
) -> str:
    workspace_root = _workspace_root_arg(workspace_root) or workspace_root
    target = _resolve_workspace_path(workspace_root, file_path)
    if not target.exists():
        raise FileNotFoundError(f"Target file not found: {target}")
    original = target.read_text(encoding="utf-8")
    if old_string not in original:
        raise ValueError("old_string not found in target file")

    occurrences = original.count(old_string)
    if not replace_all and occurrences != 1:
        raise ValueError(
            f"old_string matched {occurrences} times; pass replace_all=true or provide a more specific old_string"
        )

    updated = original.replace(old_string, new_string) if replace_all else original.replace(old_string, new_string, 1)
    target.write_text(updated, encoding="utf-8")
    refresh_wait_raw = (os.environ.get("CONTEXTSNIPER_EDIT_REFRESH_WAIT") or "0").strip().lower()
    refresh_wait = refresh_wait_raw in {"1", "true", "yes", "on"}
    refresh_timeout_raw = (os.environ.get("CONTEXTSNIPER_EDIT_REFRESH_TIMEOUT_SEC") or "5").strip()
    try:
        refresh_timeout_sec = max(1.0, float(refresh_timeout_raw))
    except ValueError:
        refresh_timeout_sec = 5.0

    refresh_body = identity_fields(
        {
            "workspaceRoot": str(Path(workspace_root).expanduser().resolve()),
            "file_path": str(target),
            "wait_for_index": refresh_wait,
            "refresh_timeout_sec": refresh_timeout_sec,
        }
    )
    try:
        refresh = post_json(
            "/api/v1/call/code_refresh_workspace_path",
            refresh_body,
            timeout_sec=refresh_timeout_sec + 1.0,
        )
    except Exception as exc:
        refresh = {
            "ok": False,
            "background_refresh_dispatched": False,
            "error": str(exc),
        }
    relative_path = target.relative_to(Path(workspace_root).expanduser().resolve()).as_posix()
    edit_debug = (os.environ.get("CONTEXTSNIPER_EDIT_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}
    if edit_debug:
        payload = {
            "ok": True,
            "file_path": str(target),
            "relative_path": relative_path,
            "replace_all": replace_all,
            "occurrences": occurrences,
            "bytes_before": len(original.encode("utf-8")),
            "bytes_after": len(updated.encode("utf-8")),
            "memory_refresh": refresh,
        }
    else:
        payload = {
            "ok": True,
            "relative_path": relative_path,
            "occurrences": occurrences,
            "refresh_ok": bool(refresh.get("ok")) if isinstance(refresh, dict) else False,
        }
    return json.dumps(payload, separators=(",", ":"))


def main() -> None:
    app.run(transport="stdio")
