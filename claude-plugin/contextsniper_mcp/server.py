"""MCP stdio server exposing ContextSniper code search for the Claude Code plugin."""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Annotated
import urllib.request

from mcp.server.fastmcp import FastMCP

from contextsniper_mcp.http_client import identity_fields, post_json

app = FastMCP("contextsniper")

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_STARTED_BACKEND = False


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _extend_runtime_pythonpath() -> None:
    plugin = _plugin_root()
    candidates = [
        plugin.parent,
        plugin.parent / "contextsniper-runtime",
    ]
    for env_name in ("CONTEXTSNIPER_DIR",):
        raw = os.environ.get(env_name)
        if raw:
            candidates.append(Path(raw).expanduser())
    runtime_raw = os.environ.get("CONTEXTSNIPER_RUNTIME_DIR")
    if runtime_raw:
        runtime = Path(runtime_raw).expanduser()
        candidates.extend([runtime, runtime / "contextsniper-runtime"])

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if not resolved.exists():
            continue
        path = str(resolved)
        if path not in sys.path:
            sys.path.insert(0, path)


_extend_runtime_pythonpath()


def _backend_healthy() -> bool:
    url = (os.environ.get("CONTEXTSNIPER_URL") or "http://127.0.0.1:8090").rstrip("/") + "/api/v1/health"
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except Exception:
        return False


def _start_backend_if_needed() -> None:
    global _STARTED_BACKEND
    if os.environ.get("CONTEXTSNIPER_PLUGIN_AUTO_START", "1") == "0":
        return
    if _backend_healthy():
        return
    plugin = _plugin_root()
    env = os.environ.copy()
    env.setdefault("PY_BIN", sys.executable)
    wait = os.environ.get("CONTEXTSNIPER_PLUGIN_START_WAIT", "45")
    try:
        timeout = max(5.0, float(wait) + 10.0)
    except ValueError:
        wait = "45"
        timeout = 55.0
    proc = subprocess.run(
        [sys.executable, str(plugin / "scripts" / "contextsniper_terminal.py"), "start", "--wait", wait],
        cwd=str(plugin.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode == 0:
        _STARTED_BACKEND = True
    elif not _backend_healthy():
        print(f"[contextsniper-mcp] backend auto-start failed with exit code {proc.returncode}", file=sys.stderr)


def _stop_backend_if_owned() -> None:
    if not _STARTED_BACKEND or os.environ.get("CONTEXTSNIPER_PLUGIN_AUTO_STOP", "1") == "0":
        return
    plugin = _plugin_root()
    try:
        subprocess.run(
            [sys.executable, str(plugin / "scripts" / "contextsniper_terminal.py"), "stop"],
            cwd=str(plugin.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        pass


def _handle_signal(signum: int, _frame: object) -> None:
    _stop_backend_if_owned()
    raise SystemExit(128 + signum)


_start_backend_if_needed()
atexit.register(_stop_backend_if_owned)
for _sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(_sig, _handle_signal)


def _canonical_code_query(query: str) -> str:
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


def _workspace_root_arg(path: str | None) -> str | None:
    raw = (path or "").strip()
    unresolved = {
        "",
        "$WORK_DIR",
        "${WORK_DIR}",
        "$PWD",
        "${PWD}",
        "$CONTEXTSNIPER_WORKSPACE_ROOT",
        "${CONTEXTSNIPER_WORKSPACE_ROOT}",
    }
    if raw in unresolved:
        raw = os.environ.get("CONTEXTSNIPER_WORKSPACE_ROOT", "") or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()
    if not raw:
        return None
    return str(Path(os.path.expandvars(raw)).expanduser().resolve())


def _search_scope_args(path: str | None, glob_patterns: str | None) -> tuple[str | None, str | None]:
    """Resolve search_code path, accepting either a workspace root or file scope."""
    raw = (path or "").strip()
    workspace_root = _workspace_root_arg(raw)
    patterns = [p.strip() for p in re.split(r"[\n,]", glob_patterns or "") if p.strip()]
    unresolved = {
        "",
        "$WORK_DIR",
        "${WORK_DIR}",
        "$PWD",
        "${PWD}",
        "$CONTEXTSNIPER_WORKSPACE_ROOT",
        "${CONTEXTSNIPER_WORKSPACE_ROOT}",
    }
    if raw in unresolved:
        return workspace_root, ",".join(patterns) or None

    active_raw = os.environ.get("CONTEXTSNIPER_WORKSPACE_ROOT", "") or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()
    active_root = Path(active_raw).expanduser().resolve()
    raw_path = Path(os.path.expandvars(raw)).expanduser()
    active_target = raw_path if raw_path.is_absolute() else active_root / raw_path
    active_target = active_target.resolve()

    if active_target.is_file():
        try:
            rel = active_target.relative_to(active_root).as_posix()
            return str(active_root), ",".join([rel, *patterns])
        except ValueError:
            return str(active_target.parent), ",".join([active_target.name, *patterns])

    resolved = raw_path.resolve()
    if resolved.is_file():
        return str(resolved.parent), ",".join([resolved.name, *patterns])

    return workspace_root, ",".join(patterns) or None


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
            "workspace_root": _workspace_root_arg(path),
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
to the ContextSniper MCP tool named `edit_file` or the next concrete
verification step instead of reading many unrelated files.
""",
)
def search_code(
    query: Annotated[str, "Natural language or keyword query."],
    path: Annotated[str, "Workspace root. If a file path is supplied, search is narrowed to that file. Leave empty to use CONTEXTSNIPER_WORKSPACE_ROOT, then Claude's launch/project directory."] = "",
    limit: Annotated[int, "Max hits (1-100)."] = 5,
    glob_patterns: Annotated[str, "Optional comma-separated glob patterns to narrow candidate files before ranking."] = "",
    grep_terms: Annotated[str, "Optional comma-separated grep keywords/symbols to narrow candidate files before ranking."] = "",
) -> str:
    workspace_root, scope_patterns = _search_scope_args(path, glob_patterns)
    body = identity_fields(
        {
            "workspaceRoot": workspace_root,
            "query": _canonical_code_query(query),
            "limit": _effective_search_limit(limit),
            "glob_patterns": scope_patterns,
            "grep_terms": grep_terms or None,
            "waitForFullWorkspaceIndex": False,
        }
    )
    result = post_json("/api/v1/call/code_semantic_search", body)
    return _format_search_snippets(result)


@app.tool(
    description="""\
Read a workspace file through the filter before returning it.

Use this for long logs, command output captures, traces, and other non-code
text where native Read would return too much raw content. The tool validates
the path under the workspace root, detects the content type, and shortens logs
while preserving error/warning lines and the tail.
""",
)
def read_filtered(
    file_path: Annotated[str, "Absolute or workspace-relative file path to read."],
    workspace_root: Annotated[str, "Workspace root used to validate the target path. Leave empty to use CONTEXTSNIPER_WORKSPACE_ROOT, then Claude's launch/project directory."] = "",
    max_lines: Annotated[int, "Approximate output line budget after filtering."] = 60,
) -> str:
    root = _workspace_root_arg(workspace_root) or str(Path.cwd())
    target = _resolve_workspace_path(root, file_path)
    if not target.exists():
        raise FileNotFoundError(f"Target file not found: {target}")
    if not target.is_file():
        raise IsADirectoryError(f"Target is not a file: {target}")

    original = target.read_text(encoding="utf-8", errors="replace")
    filtered = original
    detected = "unknown"
    filter_active = False

    try:
        from filter.config import filter_enabled
        if filter_enabled():
            from filter import get_plugin

            plugin = get_plugin()
            filtered, stats = plugin.short_adaptive(original, max_lines=max_lines)
            detected = stats.stage.removeprefix("adaptive_")
            filter_active = True
        else:
            detected = "disabled"
    except Exception as exc:
        detected = f"failed:{type(exc).__name__}"
        filtered = original

    root_path = Path(root).expanduser().resolve()
    relative_path = target.relative_to(root_path).as_posix()
    original_lines = len(original.splitlines())
    filtered_lines = len(filtered.splitlines())
    original_chars = len(original)
    filtered_chars = len(filtered)

    header = "\n".join(
        [
            f"# read_filtered: {relative_path}",
            f"# filter: {'enabled' if filter_active else 'disabled'}",
            f"# detected: {detected}",
            f"# chars: {original_chars} -> {filtered_chars}",
            f"# lines: {original_lines} -> {filtered_lines}",
            "",
        ]
    )
    return header + filtered.rstrip() + "\n"


@app.tool(
    description="""\
Return the original unfiltered output for a filtered tool result.

Use this when a tool result begins with `FILTER IS TRIGGERED` and the filtered
or captured output appears to be missing information needed to continue. Pass
the `memory_uri` shown in the output banner.
""",
)
def get_original_tool_output(
    memory_uri: Annotated[str, "The ctx://.../memories/tool_outputs/... URI shown in the filtered output banner."],
    max_chars: Annotated[int, "Maximum characters to return. Use 0 for the full original output."] = 20000,
) -> str:
    body = identity_fields({
        "memory_uri": memory_uri,
        "max_chars": max_chars,
    })
    match = re.match(r"^ctx://([^/]+)/agents/([^/]+)/memories/tool_outputs/", memory_uri or "")
    if match and match.group(1) == body.get("accountId"):
        body["agentId"] = match.group(2)
    result = post_json("/api/v1/tool_output_original", body)
    if not result.get("ok"):
        return json.dumps(result, indent=2)

    original = str(result.get("original") or "")
    header = "\n".join([
        f"# original tool output: {result.get('memory_uri') or memory_uri}",
        f"# chars: {result.get('returned_chars', len(original))} / {result.get('original_chars', len(original))}",
        f"# truncated: {bool(result.get('truncated'))}",
        "",
    ])
    return header + original.rstrip() + "\n"


@app.tool(
    description="""\
Edit a file via exact string replacement without requiring a prior Read tool call.

This is an MCP replacement for Claude's built-in Edit tool:
- validates the target is under `workspace_root`
- replaces `old_string` with `new_string`
- can replace once or all matches

Use this for source/code edits after locating the relevant file with the
ContextSniper MCP tool named `search_code`. Prefer it over Claude's
built-in Edit tool for files found through ContextSniper.
""",
)
def edit_file(
    file_path: Annotated[str, "Absolute or workspace-relative target file path."],
    old_string: Annotated[str, "Exact text to replace."],
    new_string: Annotated[str, "Replacement text."],
    workspace_root: Annotated[str, "Workspace root used to validate the target path. Leave empty to use CONTEXTSNIPER_WORKSPACE_ROOT, then Claude's launch/project directory."] = "",
    replace_all: Annotated[bool, "Replace every occurrence instead of exactly one."] = False,
) -> str:
    root = _workspace_root_arg(workspace_root) or str(Path.cwd())
    target = _resolve_workspace_path(root, file_path)
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

    refresh_wait = (os.environ.get("CONTEXTSNIPER_EDIT_REFRESH_WAIT") or "0").strip().lower() in {"1", "true", "yes", "on"}
    try:
        refresh_timeout_sec = max(1.0, float(os.environ.get("CONTEXTSNIPER_EDIT_REFRESH_TIMEOUT_SEC") or "5"))
    except ValueError:
        refresh_timeout_sec = 5.0

    refresh_body = identity_fields(
        {
            "workspaceRoot": str(Path(root).expanduser().resolve()),
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
        refresh = {"ok": False, "background_refresh_dispatched": False, "error": str(exc)}

    relative_path = target.relative_to(Path(root).expanduser().resolve()).as_posix()
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
