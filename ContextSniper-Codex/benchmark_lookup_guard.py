#!/usr/bin/env python3
"""Block exact benchmark-answer lookups while allowing general web research."""

from __future__ import annotations

import json
import glob
import os
import re
import shlex
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable


MARKERS_ENV = "CONTEXTSNIPER_BENCHMARK_LOOKUP_MARKERS_JSON"
LOG_ENV = "CONTEXTSNIPER_BENCHMARK_LOOKUP_GUARD_LOG"
SENSITIVE_ROOTS_ENV = "CONTEXTSNIPER_BENCHMARK_SENSITIVE_ROOTS_JSON"
WORKSPACE_ROOT_ENV = "CONTEXTSNIPER_WORKSPACE_ROOT"

_NETWORK_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|()`])(?:[^\s;&|()]+/)?(?:"
    r"curl|wget|aria2c|http|https|lynx|w3m|links|elinks|"
    r"ddgr|googler|gh|hub"
    r")(?=\s|$)",
    re.IGNORECASE,
)
_GIT_NETWORK_RE = re.compile(
    r"(?:^|[\s;&|()`])git\s+(?:clone|fetch|pull|ls-remote)(?:\s|$)",
    re.IGNORECASE,
)
_PROGRAMMATIC_NETWORK_RE = re.compile(
    r"(?:requests\.(?:get|post|request)|urllib(?:\.request)?\.urlopen|"
    r"aiohttp\.|httpx\.|fetch\s*\(|axios\.|new\s+URL\s*\()",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?:https?|ftp)://", re.IGNORECASE)
_BUG_NUMBER_RE = re.compile(r"\bbug[-_#\s]*(\d{4,})\b", re.IGNORECASE)
_TICKET_RE = re.compile(r"\b[a-z][a-z0-9_.]*[-_](\d{3,})\b", re.IGNORECASE)
_CVE_RE = re.compile(r"(?<![A-Z0-9])CVE-\d{4}-\d{4,}(?!\d)", re.IGNORECASE)
_HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
_HEREDOC_RE = re.compile(
    r"<<-?\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=quote)"
)
_QUOTED_PATH_RE = re.compile(r"['\"](?P<path>(?:/|\.\./)[^'\"\n]+)['\"]")


def _marker_forms(value: str) -> tuple[str, str]:
    decoded = str(value)
    for _ in range(2):
        decoded = urllib.parse.unquote_plus(decoded)
    lowered = decoded.casefold()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    return lowered, compact


def benchmark_lookup_markers(
    case_id: str, failing_tests: Iterable[str]
) -> list[str]:
    """Derive benchmark identifiers that must never be used in online lookups."""
    markers: set[str] = set()

    def add(value: str) -> None:
        cleaned = str(value or "").strip().strip("/\\")
        if len(cleaned) >= 5:
            markers.add(cleaned)

    add(case_id)
    case_parts = [part for part in re.split(r"__+", case_id) if part]
    if case_parts:
        # The suffix normally carries the benchmark issue ID or source commit.
        add(case_parts[-1])
    for match in _CVE_RE.findall(case_id):
        add(match)
    for match in _HEX_RE.findall(case_id):
        add(match)
    for match in _TICKET_RE.finditer(case_id):
        if not match.group(0).casefold().startswith("cve-"):
            add(match.group(0))

    for raw_test in failing_tests:
        test_id = str(raw_test or "").strip()
        if not test_id:
            continue
        add(test_id)
        name = Path(test_id).name
        stem = Path(name).stem
        # A basename/stem with a number or structured test name is specific
        # enough to identify a public benchmark case. Avoid generic names such
        # as "failure" to keep ordinary technical search usable.
        if any(character.isdigit() for character in name) or "." in stem:
            add(name)
            add(stem)
        for match in _BUG_NUMBER_RE.finditer(test_id):
            add(match.group(0))
            add(match.group(1))
        for match in _TICKET_RE.finditer(test_id):
            add(match.group(0))

    return sorted(markers, key=lambda value: (-len(value), value.casefold()))


def command_uses_network(command: str) -> bool:
    value = str(command or "")
    return bool(
        _URL_RE.search(value)
        or _NETWORK_COMMAND_RE.search(value)
        or _GIT_NETWORK_RE.search(value)
        or _PROGRAMMATIC_NETWORK_RE.search(value)
    )


def matched_lookup_marker(text: str, markers: Iterable[str]) -> str | None:
    lowered, compact = _marker_forms(text)
    for marker in markers:
        marker_lowered, marker_compact = _marker_forms(str(marker))
        if marker_lowered and marker_lowered in lowered:
            return str(marker)
        if len(marker_compact) >= 5 and marker_compact in compact:
            return str(marker)
    return None


def command_lookup_violation(
    command: str, markers: Iterable[str]
) -> dict[str, str] | None:
    if not command_uses_network(command):
        return None
    marker = matched_lookup_marker(command, markers)
    if marker is None:
        return None
    return {
        "kind": "benchmark_identifier_in_network_lookup",
        "marker": marker,
        "value": str(command),
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return True


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def shell_payload(command: str) -> str:
    """Extract the script passed through ``sh -c`` from a Codex event."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) >= 3 and parts[1] in {"-c", "-lc"}:
        return parts[2]
    return command


def _split_heredoc_bodies(script: str) -> tuple[str, list[str]]:
    """Keep shell command headers while omitting embedded language bodies.

    Treating a Python ``//`` operator or a string in a heredoc as a shell path
    created false ground-truth alarms. Quoted paths are inspected separately.
    """
    kept: list[str] = []
    bodies: list[str] = []
    body: list[str] = []
    delimiter = ""
    for line in script.splitlines():
        if delimiter:
            if line.strip() == delimiter:
                bodies.append("\n".join(body))
                body = []
                delimiter = ""
            else:
                body.append(line)
            continue
        kept.append(line)
        match = _HEREDOC_RE.search(line)
        if match:
            delimiter = match.group("delimiter")
    if body:
        bodies.append("\n".join(body))
    return "\n".join(kept), bodies


def _path_values(command: str) -> list[str]:
    script = shell_payload(command)
    shell_only, heredoc_bodies = _split_heredoc_bodies(script)
    try:
        tokens = shlex.split(shell_only)
    except ValueError:
        tokens = shell_only.split()

    values: list[str] = []
    for token in tokens:
        value = token.strip("'\"(){}[];,<>")
        if not value or value == "//":
            continue
        # Peel off common redirection/option prefixes without interpreting the
        # rest of the shell grammar.
        value = re.sub(r"^\d*(?:>>?|<)", "", value)
        if value.startswith("-I/"):
            value = value[2:]
        elif "=" in value:
            possible = value.split("=", 1)[1]
            if possible.startswith(("/", "../", "./")):
                value = possible
        if value.startswith(("/", "../", "./")) or ".." in Path(value).parts:
            values.append(value)

    # A heredoc can still deliberately open a ground-truth file. Inspect quoted
    # literals in embedded code, not the normal shell script: e.g. the shell
    # pattern in ``grep -v "//"`` is not a filesystem path.
    for body in heredoc_bodies:
        values.extend(
            match.group("path")
            for match in _QUOTED_PATH_RE.finditer(body)
            if match.group("path") != "//"
        )
    return list(dict.fromkeys(values))


def path_value_may_exist(raw_value: str, workspace: Path) -> bool:
    """Whether a recorded path could have reached an existing local object."""
    value = os.path.expandvars(str(raw_value or "")).strip()
    if not value or value in {"//", "/dev/null"}:
        return False
    candidate = Path(value).expanduser()
    candidate = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (workspace.expanduser().resolve(strict=False) / candidate).resolve(
            strict=False
        )
    )
    if glob.has_magic(str(candidate)):
        rendered = str(candidate)
        prefix = rendered[: min(rendered.index(char) for char in "*?[" if char in rendered)]
        prefix_path = Path(prefix.rstrip(os.sep) or os.sep)
        return prefix_path.exists()
    return candidate.exists()


def command_sensitive_path_violation(
    command: str,
    workspace: Path,
    sensitive_roots: Iterable[Path],
    *,
    require_existing: bool = False,
) -> dict[str, Any] | None:
    """Reject workspace escapes that can reach prepared benchmark inputs.

    System runtimes such as ``/usr/bin/python3`` and unrelated temporary paths
    are not ground truth. A path is sensitive when it is inside a configured
    benchmark root *or* is an ancestor whose recursive scan could reach it.
    Relative ``..`` escapes are always rejected.
    """
    root = workspace.expanduser().resolve(strict=False)
    protected = tuple(
        candidate.expanduser().resolve(strict=False) for candidate in sensitive_roots
    )
    for raw_value in _path_values(command):
        value = os.path.expandvars(raw_value).strip()
        if not value or value == "/dev/null":
            continue
        candidate = Path(value).expanduser()
        parent_escape = ".." in candidate.parts
        candidate = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (root / candidate).resolve(strict=False)
        )
        if _is_within(candidate, root):
            continue
        if parent_escape or any(_paths_overlap(candidate, item) for item in protected):
            path_exists = path_value_may_exist(raw_value, root)
            if require_existing and not path_exists:
                continue
            return {
                "kind": "sensitive_path_outside_workspace",
                "value": raw_value,
                "path_exists": path_exists,
            }
    return None


def event_lookup_violation(
    item: dict[str, Any], markers: Iterable[str]
) -> dict[str, str] | None:
    """Audit shell and hosted/network-tool events from Codex JSONL."""
    item_type = str(item.get("type") or "").casefold()
    if item_type == "command_execution":
        return command_lookup_violation(str(item.get("command") or ""), markers)

    # ContextSniper's local repository search is the baseline operation and is
    # allowed to use the failing test ID. It is never an online lookup.
    if item_type == "mcp_tool_call" and item.get("server") == "contextsniper":
        return None

    descriptor = " ".join(
        str(item.get(field) or "").casefold()
        for field in ("type", "server", "tool", "name")
    )
    network_tool = any(
        label in descriptor
        for label in (
            "web_search",
            "search_query",
            "browser",
            "fetch_url",
            "open_url",
            "http_request",
        )
    )
    if not network_tool:
        return None
    rendered = json.dumps(item, ensure_ascii=False, sort_keys=True)
    marker = matched_lookup_marker(rendered, markers)
    if marker is None:
        return None
    return {
        "kind": "benchmark_identifier_in_network_lookup",
        "marker": marker,
        "value": rendered,
    }


def _append_log(record: dict[str, Any]) -> None:
    raw_path = str(os.environ.get(LOG_ENV) or "").strip()
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # The denial still takes effect even if its auxiliary log is unwritable.
        pass


def _deny(reason: str, *, record: dict[str, Any]) -> int:
    _append_log({"timestamp": time.time(), **record, "decision": "deny"})
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        return _deny(
            "Benchmark lookup guard could not parse the pending tool call.",
            record={"kind": "guard_input_error", "value": str(exc)},
        )
    try:
        markers_value = json.loads(str(os.environ.get(MARKERS_ENV) or ""))
    except json.JSONDecodeError as exc:
        markers_value = []
        marker_error = str(exc)
    else:
        marker_error = ""
    markers = (
        [str(value) for value in markers_value if str(value).strip()]
        if isinstance(markers_value, list)
        else []
    )
    if not markers:
        return _deny(
            "Benchmark lookup guard is missing its forbidden identifiers.",
            record={"kind": "guard_configuration_error", "value": marker_error},
        )

    try:
        sensitive_value = json.loads(str(os.environ.get(SENSITIVE_ROOTS_ENV) or "[]"))
    except json.JSONDecodeError as exc:
        return _deny(
            "Benchmark lookup guard has invalid sensitive-root configuration.",
            record={"kind": "guard_configuration_error", "value": str(exc)},
        )
    sensitive_roots = (
        [Path(str(value)) for value in sensitive_value if str(value).strip()]
        if isinstance(sensitive_value, list)
        else []
    )
    workspace_value = str(os.environ.get(WORKSPACE_ROOT_ENV) or "").strip()

    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    command = ""
    if isinstance(tool_input, dict):
        command = tool_input.get("command") or tool_input.get("cmd") or ""
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if workspace_value and sensitive_roots:
        path_violation = command_sensitive_path_violation(
            str(command or ""), Path(workspace_value), sensitive_roots
        )
        if path_violation is not None:
            return _deny(
                "Commands may not inspect parent/sibling benchmark data or any "
                "prepared project copy outside the current workspace.",
                record=path_violation,
            )
    violation = command_lookup_violation(str(command or ""), markers)
    if violation is None:
        return 0
    return _deny(
        "Exact benchmark identifiers may not be used in network or web lookups. "
        "Use local evidence or a general technical search without the case, CVE, "
        "bug/test ID, or commit hash.",
        record=violation,
    )


if __name__ == "__main__":
    raise SystemExit(main())
