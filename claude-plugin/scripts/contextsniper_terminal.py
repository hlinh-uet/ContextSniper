#!/usr/bin/env python3
"""Terminal and hook entrypoints for the ContextSniper Claude plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8090"
SIDE_EFFECT_TOOLS = {"Write", "Edit", "MultiEdit", "Bash", "NotebookEdit"}
MAX_TOOL_CHARS = 10000
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_POLICY_PROMPT_PATH = PLUGIN_ROOT / "prompts" / "code_policy_injection.txt"
FILTERING_PROMPT_HEADING = "## Filtering Strategy"
FILTERING_PROMPT_END_MARKER = "Keep the workflow compact:"
FILTERING_PROMPT_BULLETS = (
    "- Native `Read` on long logs/traces may be filtered through ContextSniper before content is returned. "
    "The backend stores L0 as filtered content, L1 as properties, and L2 as the original output.",
)


def log(prefix: str, message: str) -> None:
    print(f"[{prefix}] {message}", file=sys.stderr)


def api_url() -> str:
    return (os.environ.get("CONTEXTSNIPER_URL") or DEFAULT_URL).rstrip("/")


def identity(session_id: str | None = None) -> dict[str, str]:
    out = {
        "accountId": os.environ.get("CONTEXTSNIPER_ACCOUNT_ID")
        or os.environ.get("CONTEXTSNIPER_ACCOUNT_ID", "acct-demo"),
        "userId": os.environ.get("CONTEXTSNIPER_USER_ID")
        or os.environ.get("CONTEXTSNIPER_USER_ID", "u-claude"),
        "agentId": os.environ.get("CONTEXTSNIPER_AGENT_ID")
        or os.environ.get("CONTEXTSNIPER_AGENT_ID", "claude-code"),
    }
    sid = session_id or os.environ.get("CONTEXTSNIPER_SESSION_ID")
    if sid:
        out["sessionId"] = sid
    return out


def headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = (os.environ.get("CONTEXTSNIPER_AUTH_API_KEY") or "").strip()
    if key:
        h["X-API-Key"] = key
        h["X-Account-ID"] = identity().get("accountId", "acct-demo")
        h["X-User-ID"] = identity().get("userId", "u-claude")
    return h


def post_json(path: str, body: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    url = api_url() + path
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def backend_healthy(timeout: float = 1.0) -> bool:
    try:
        get_json("/api/v1/health", timeout=timeout)
        return True
    except Exception:
        return False


def ensure_backend_for_hook() -> bool:
    if os.environ.get("CONTEXTSNIPER_PLUGIN_AUTO_START", "1").strip().lower() in {"0", "false", "no", "off"}:
        return backend_healthy(timeout=1)
    if backend_healthy(timeout=1):
        return True

    wait = os.environ.get("CONTEXTSNIPER_PLUGIN_HOOK_START_WAIT") or "8"
    try:
        timeout = max(2.0, float(wait) + 3.0)
    except ValueError:
        wait = "8"
        timeout = 11.0
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "scripts" / "contextsniper_terminal.py"),
                "start",
                "--wait",
                wait,
            ],
            cwd=str(PLUGIN_ROOT.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        log("ensure_backend", f"auto-start failed: {exc}")
        return False
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().replace("\n", " ")
        log("ensure_backend", f"auto-start exited {proc.returncode}: {err[:240]}")
    return backend_healthy(timeout=1)


def get_json(path: str, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(api_url() + path, headers=headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def compact_json(obj: Any, limit: int = MAX_TOOL_CHARS) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except TypeError:
        text = str(obj)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more chars omitted]"


def truthy_env(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def looks_like_code_prompt(prompt: str) -> bool:
    lowered = prompt.lower()
    keywords = (
        "bug",
        "fix",
        "test",
        "function",
        "class",
        "method",
        "source",
        "code",
        "repo",
        "implementation",
        "traceback",
        "error",
    )
    return any(keyword in lowered for keyword in keywords)


def filtering_prompt_enabled() -> bool:
    return truthy_env("CONTEXTSNIPER_INJECT_FILTERING_PROMPT", "0")


def render_code_policy_prompt(text: str, include_filtering: bool) -> str:
    if include_filtering:
        return text.strip()

    for bullet in FILTERING_PROMPT_BULLETS:
        text = text.replace(bullet + "\n", "")

    start = text.find(FILTERING_PROMPT_HEADING)
    if start == -1:
        return text.strip()

    end = text.find(FILTERING_PROMPT_END_MARKER, start)
    if end == -1:
        return (text[:start]).rstrip()

    return (text[:start].rstrip() + "\n\n" + text[end:].lstrip()).strip()


def code_policy_prompt() -> str:
    try:
        text = CODE_POLICY_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        log("call_compose", f"failed to read code policy injection: {exc}")
        return ""
    return render_code_policy_prompt(text, include_filtering=filtering_prompt_enabled())


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts).strip()
    return ""


def parse_transcript_chunk(text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isSidechain") or entry.get("isApiErrorMessage"):
            continue
        if entry.get("type") not in {"user", "assistant"}:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = extract_text(msg.get("content", ""))
        if content:
            messages.append({"role": role, "content": content})
    return messages


def project_transcript_dir(project_dir: str | None = None) -> Path:
    cwd = Path(project_dir or os.getcwd()).resolve()
    slug = str(cwd).replace("/", "-").replace("_", "-")
    candidates = [
        Path.home() / ".claude" / "projects" / slug,
        Path.home() / ".claude" / "projects" / f"-{slug}",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def format_compose(data: dict[str, Any]) -> str:
    sections = [
        ("identityContext", "Profile"),
        ("episodicContext", "Archives"),
        ("sessionContext", "Session"),
        ("retrievedEvidence", "Working Set"),
        ("memoryUserMessage", "Memory Message"),
    ]
    parts = []
    for key, label in sections:
        value = str(data.get(key) or "").strip()
        if value:
            parts.append(f"## {label}\n{value}")
    return "\n\n".join(parts) if parts else "No relevant context found."


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_root_from_plugin() -> Path:
    return plugin_root().parent


def runtime_dir(args: argparse.Namespace | None = None) -> Path:
    raw = getattr(args, "runtime_dir", None) if args else None
    raw = raw or os.environ.get("CONTEXTSNIPER_RUNTIME_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".cache" / "contextsniper-claude-plugin"


def env_truthy(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except Exception:
        return None


def find_agfs_bin(repo_root: Path) -> str:
    env_bin = os.environ.get("AGFS_BIN")
    if env_bin:
        try:
            candidate = Path(env_bin).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            pass
    local = repo_root / "agfs" / "build" / "agfs-server"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    found = shutil.which("agfs-server")
    if found:
        return found
    raise RuntimeError("agfs-server not found. Set AGFS_BIN or put agfs-server on PATH.")


def write_agfs_config(path: Path, data_dir: Path, port: int) -> None:
    path.write_text(
        "\n".join(
            [
                "server:",
                f'  address: ":{port}"',
                "  log_level: info",
                "plugins:",
                "  serverinfofs:",
                "    enabled: true",
                "    path: /serverinfo",
                "    config:",
                '      version: "1.0.0"',
                "  localfs:",
                "    enabled: true",
                "    path: /local",
                "    config:",
                f'      local_dir: "{data_dir}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def build_runtime_tree(repo_root: Path, run: Path) -> Path:
    runtime = run / "contextsniper-runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    run_resolved = run.resolve()
    copy_agfs_runtime = env_truthy("CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME", True)

    def ignore(dir_name: str, names: list[str]) -> set[str]:
        ignored = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        current = Path(dir_name).resolve()
        ignored.update({".cache", "output_logs", "include", "repo"})
        if current == repo_root.resolve():
            ignored.update({".git", ".venv", runtime.name})
            if not copy_agfs_runtime:
                ignored.add("agfs")
        if current == run_resolved:
            ignored.update(names)
        if current == (repo_root / "scripts" / "SWE" / "claude").resolve():
            ignored.update({".cache", "output_logs", "repo"})
        return ignored.intersection(names)

    shutil.copytree(repo_root, runtime, ignore=ignore)
    overlay = repo_root / "code-version"
    if overlay.is_dir():
        shutil.copytree(overlay, runtime, dirs_exist_ok=True)
    return runtime


def command_start(args: argparse.Namespace) -> int:
    root = repo_root_from_plugin()
    run = runtime_dir(args)
    logs = run / "logs"
    data_dir = run / "agfs-data"
    logs.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    contextsniper_pid_file = run / "contextsniper.pid"
    agfs_pid_file = run / "agfs.pid"
    contextsniper_pid = read_pid(contextsniper_pid_file)
    agfs_pid = read_pid(agfs_pid_file)
    if contextsniper_pid and agfs_pid and pid_alive(contextsniper_pid) and pid_alive(agfs_pid):
        print(f"Already running: {api_url()}")
        print(f"Runtime dir: {run}")
        return 0

    agfs_port = int(os.environ.get("AGFS_HTTP_PORT") or "1833")
    contextsniper_url = api_url()
    agfs_url = (os.environ.get("AGFS_BASE_URL") or f"http://127.0.0.1:{agfs_port}").rstrip("/")
    env = os.environ.copy()
    env.setdefault("CONTEXTSNIPER_URL", contextsniper_url)
    env.setdefault("AGFS_BASE_URL", agfs_url)
    env.setdefault("VECTOR_DB_TYPE", "memory")
    env.setdefault("CONTEXTSNIPER_CODE_TOGGLE", "true")
    env.setdefault("EMBEDDING_PROVIDER", "openai")
    env.setdefault("CONTEXTSNIPER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
    env.setdefault("CONTEXTSNIPER_EMBEDDING_BASE_URL", "https://openrouter.ai/api/v1")
    env.setdefault("CONTEXTSNIPER_START_LOCAL_EMBED_SERVER", "0")

    agfs_config = run / "agfs-config.yaml"
    write_agfs_config(agfs_config, data_dir, agfs_port)
    runtime_root = build_runtime_tree(root, run)
    env["PYTHONPATH"] = f"{runtime_root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)

    agfs_bin = find_agfs_bin(root)
    with (logs / "agfs-server.log").open("ab") as agfs_log:
        agfs_proc = subprocess.Popen(
            [agfs_bin, "-c", str(agfs_config)],
            stdout=agfs_log,
            stderr=subprocess.STDOUT,
            cwd=str(root),
            env=env,
            start_new_session=True,
        )
    agfs_pid_file.write_text(str(agfs_proc.pid), encoding="utf-8")

    py_bin = os.environ.get("PY_BIN") or sys.executable
    with (logs / "contextsniper-server.log").open("ab") as server_log:
        server_proc = subprocess.Popen(
            [py_bin, str(runtime_root / "server" / "app.py")],
            stdout=server_log,
            stderr=subprocess.STDOUT,
            cwd=str(runtime_root),
            env=env,
            start_new_session=True,
        )
    contextsniper_pid_file.write_text(str(server_proc.pid), encoding="utf-8")

    time.sleep(0.5)
    failed = []
    if agfs_proc.poll() is not None:
        failed.append(f"AGFS exited with code {agfs_proc.returncode}; see {logs / 'agfs-server.log'}")
    if server_proc.poll() is not None:
        failed.append(
            f"ContextSniper exited with code {server_proc.returncode}; "
            f"see {logs / 'contextsniper-server.log'}"
        )
    if failed:
        print("Failed to start local ContextSniper services:", file=sys.stderr)
        for item in failed:
            print(f"- {item}", file=sys.stderr)
        return 1

    for _ in range(max(1, int(args.wait))):
        try:
            get_json("/api/v1/health", timeout=2)
            print(f"Started ContextSniper: {contextsniper_url}")
            print(f"Runtime dir: {run}")
            print(f"Logs: {logs}")
            return 0
        except Exception:
            time.sleep(1)

    print(f"Started processes, but health check did not pass yet: {contextsniper_url}", file=sys.stderr)
    print(f"Check logs: {logs}", file=sys.stderr)
    return 1


def command_stop(args: argparse.Namespace) -> int:
    run = runtime_dir(args)
    stopped = 0
    for name in ("contextsniper", "agfs"):
        pid_file = run / f"{name}.pid"
        pid = read_pid(pid_file)
        if not pid:
            continue
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                stopped += 1
            except ProcessLookupError:
                pass
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
    print(f"Stopped {stopped} process(es). Runtime dir: {run}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    run = runtime_dir(args)
    status = {}
    for name in ("contextsniper", "agfs"):
        pid = read_pid(run / f"{name}.pid")
        status[name] = {"pid": pid, "alive": bool(pid and pid_alive(pid))}
    try:
        status["health"] = get_json("/api/v1/health", timeout=3)
    except Exception as exc:
        status["health_error"] = str(exc)
    print(json.dumps(status, indent=2, ensure_ascii=False))
    return 0 if status.get("contextsniper", {}).get("alive") else 1


def command_health(_: argparse.Namespace) -> int:
    try:
        print(json.dumps(get_json("/api/v1/health"), indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"health failed: {exc}", file=sys.stderr)
        return 1


def command_compose(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print("usage: contextsniper-compose <query>", file=sys.stderr)
        return 2
    body = {**identity(args.session_id), "prompt": query}
    try:
        data = post_json("/api/v1/compose", body, timeout=args.timeout)
    except Exception as exc:
        print(f"compose failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(format_compose(data))
    return 0


def command_render_code_policy(args: argparse.Namespace) -> int:
    prompt = code_policy_prompt()
    if prompt:
        print(prompt)
    return 0


def command_after_turn(args: argparse.Namespace) -> int:
    if args.transcript:
        path = Path(args.transcript)
        if not path.is_file():
            print(f"transcript not found: {path}", file=sys.stderr)
            return 1
        messages = parse_transcript_chunk(path.read_text(encoding="utf-8", errors="replace"))
        session_id = args.session_id or path.stem
    else:
        raw = sys.stdin.read()
        messages = parse_transcript_chunk(raw)
        session_id = args.session_id or "terminal-session"
    if not messages:
        print("No valid transcript messages found.")
        return 0
    body = {**identity(session_id), "messages": messages, "hook_event_name": "terminal"}
    try:
        data = post_json("/api/v1/after_turn", body, timeout=args.timeout)
    except Exception as exc:
        print(f"after-turn failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def command_add_history(args: argparse.Namespace) -> int:
    base = project_transcript_dir(args.project_dir)
    if not base.is_dir():
        print(f"No Claude transcript directory found: {base}")
        return 0
    transcripts = sorted(
        p for p in base.rglob("*.jsonl")
        if "subagents" not in p.parts and not p.name.endswith(".ingest-offset")
    )
    pending = []
    skipped = 0
    total_size = 0
    for path in transcripts:
        off = Path(str(path) + ".ingest-offset")
        if off.is_file():
            try:
                if int(off.read_text().strip()) >= path.stat().st_size:
                    skipped += 1
                    continue
            except ValueError:
                pass
        pending.append(path)
        total_size += path.stat().st_size
    print(f"Project transcript dir: {base}")
    print(f"Pending transcripts: {len(pending)}")
    print(f"Skipped already ingested: {skipped}")
    print(f"Total pending size: {total_size / 1024 / 1024:.1f} MB")
    if args.dry_run:
        return 0
    if not args.yes:
        print("Refusing to import without --yes. Run with --dry-run first if unsure.")
        return 2
    ok = fail = empty = 0
    for path in pending:
        messages = parse_transcript_chunk(path.read_text(encoding="utf-8", errors="replace"))
        if not messages:
            empty += 1
            continue
        body = {**identity(path.stem), "messages": messages, "hook_event_name": "terminal-add-history"}
        try:
            post_json("/api/v1/after_turn", body, timeout=args.timeout)
            Path(str(path) + ".ingest-offset").write_text(str(path.stat().st_size))
            ok += 1
            print(f"OK {path.name}: {len(messages)} messages")
        except Exception as exc:
            fail += 1
            print(f"FAIL {path.name}: {exc}", file=sys.stderr)
    print(f"Done: ok={ok} fail={fail} empty={empty} skipped={skipped}")
    return 1 if fail else 0


def hook_compose() -> int:
    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log("call_compose", "invalid hook JSON")
        return 0
    prompt = str(hook.get("prompt") or "").strip()
    session_id = str(hook.get("session_id") or "unknown")
    if len(prompt) < 4 or prompt.startswith("/"):
        return 0
    additions: list[str] = []
    if os.environ.get("CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT", "1") != "0" and looks_like_code_prompt(prompt):
        policy = code_policy_prompt()
        if policy:
            additions.append(policy)
    data = {}
    if ensure_backend_for_hook():
        _sync_workspace_on_prompt(session_id)
        try:
            data = post_json("/api/v1/compose", {**identity(session_id), "prompt": prompt}, timeout=30)
            log("call_compose", f"POST {api_url()}/api/v1/compose session={session_id} prompt_len={len(prompt)}")
        except Exception as exc:
            log("call_compose", f"failed: {exc}")
    else:
        log("call_compose", "backend unavailable")
    if data:
        additional = format_compose(data)
        if additional == "No relevant context found.":
            log("call_compose", "No relevant context returned")
        else:
            additions.append(f"[ContextSniper]\n{additional[:9500]}")
    if not additions:
        return 0
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(additions),
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


def hook_add_session_message() -> int:
    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log("call_add_session_message", "invalid hook JSON")
        return 0
    session_id = str(hook.get("session_id") or "unknown")
    tool = str(hook.get("tool_name") or "unknown_tool")
    if session_id == "unknown" or tool not in SIDE_EFFECT_TOOLS:
        return 0
    content = "\n".join([
        f"[PostToolUse] {tool}",
        f"tool_input: {compact_json(hook.get('tool_input'))}",
        f"tool_response: {compact_json(hook.get('tool_response'))}",
    ])
    if not ensure_backend_for_hook():
        log("call_add_session_message", "backend unavailable")
        return 0
    body = {**identity(session_id), "role": "tool", "content": content}
    try:
        data = post_json(f"/api/v1/sessions/{session_id}/messages", body, timeout=8)
        log("call_add_session_message", f"tool={tool} POST session message ok={data.get('ok', True)}")
    except Exception as exc:
        log("call_add_session_message", f"failed: {exc}")
    return 0


def _workspace_root() -> str:
    return os.environ.get("CONTEXTSNIPER_WORKSPACE_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _sync_workspace_on_prompt(session_id: str) -> None:
    if not truthy_env("CONTEXTSNIPER_CODE_SYNC_ON_PROMPT", "1"):
        return

    workspace_root = _workspace_root()
    try:
        timeout = max(1.0, float(os.environ.get("CONTEXTSNIPER_CODE_SYNC_HOOK_TIMEOUT", "30")))
    except ValueError:
        timeout = 30.0
    wait_for_index = truthy_env("CONTEXTSNIPER_CODE_SYNC_WAIT_FOR_INDEX", "1")
    body = {
        **identity(session_id),
        "workspaceRoot": workspace_root,
        "reason": "user_prompt_submit",
        "wait_for_index": wait_for_index,
    }
    try:
        data = post_json("/api/v1/call/code_sync_workspace", body, timeout=timeout)
    except Exception as exc:
        log("code_sync", f"prompt sync failed: {exc}")
        return

    if data.get("ok"):
        log(
            "code_sync",
            "mode=%s changed=%s deleted=%s ingested=%s"
            % (
                data.get("mode", ""),
                data.get("changed_count", ""),
                data.get("deleted_count", ""),
                (data.get("apply") or {}).get("ingested_count", ""),
            ),
        )
    else:
        log("code_sync", f"prompt sync skipped/failed: {compact_json(data, limit=800)}")


def _canonicalize_read_file_path(file_path: str, workspace_root: str) -> tuple[str, str]:
    """Map invented absolute repo paths back into the active workspace."""
    raw = str(file_path or "").strip()
    if not raw:
        return raw, ""

    root = Path(workspace_root or os.getcwd()).expanduser().resolve()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        return raw, ""

    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root)
        if resolved.is_dir() and (resolved / "TASK.md").exists():
            return str(resolved / "TASK.md"), "rewrote workspace directory Read to TASK.md"
        return raw, ""
    except ValueError:
        pass

    if target.is_dir() and (root / "TASK.md").exists():
        return str(root / "TASK.md"), "rewrote external directory Read to workspace TASK.md"

    parts = target.parts
    # Prefer meaningful repo-relative suffixes over basename-only matches.
    for start in range(1, max(len(parts) - 1, 1)):
        suffix = Path(*parts[start:])
        if len(suffix.parts) < 2:
            continue
        candidate = (root / suffix).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.exists():
            return str(candidate), f"rewrote missing absolute path to workspace-relative suffix {suffix.as_posix()}"

    if target.name:
        candidate = (root / target.name).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            pass
        else:
            if candidate.exists():
                return str(candidate), f"rewrote missing absolute path to workspace root file {target.name}"

    if target.exists():
        return raw, ""

    return raw, ""


def _canonicalize_command_path_arg(raw_path: str, workspace_root: str) -> tuple[str, str]:
    root = Path(workspace_root or os.getcwd()).expanduser().resolve()
    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        return raw_path, ""

    try:
        target.resolve(strict=False).relative_to(root)
        return raw_path, ""
    except ValueError:
        pass

    if target.name == "source_tree" and (root / "TASK.md").exists():
        return str(root), "rewrote source_tree project root to active workspace root"

    parts = target.parts
    for start in range(1, max(len(parts) - 1, 1)):
        suffix = Path(*parts[start:])
        candidate = (root / suffix).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.exists():
            return str(candidate), f"rewrote missing absolute path to workspace-relative suffix {suffix.as_posix()}"

    project_root = Path(os.environ.get("CONTEXTSNIPER_DIR") or PLUGIN_ROOT.parent).expanduser().resolve()
    try:
        target.resolve(strict=False).relative_to(project_root)
    except ValueError:
        return raw_path, ""
    return str(root), "rewrote project-root path to active workspace root"


def _canonicalize_command_paths(command: str, workspace_root: str) -> tuple[str, list[str]]:
    corrected = str(command or "")
    reasons: list[str] = []
    if not corrected.strip():
        return corrected, reasons

    # Handle common unquoted absolute path arguments. Keep this conservative:
    # only rewrite paths that do not exist but whose repo-relative suffix exists
    # inside the active workspace.
    for match in sorted(set(re.findall(r"/[^\s'\"<>|&;]+", corrected)), key=len, reverse=True):
        raw_path = match
        trailing = ""
        while raw_path and raw_path[-1] in ",:)]}":
            trailing = raw_path[-1] + trailing
            raw_path = raw_path[:-1]
        if not raw_path:
            continue
        canonical, reason = _canonicalize_command_path_arg(raw_path, workspace_root)
        if not reason or canonical == raw_path:
            continue
        replacement = shlex.quote(canonical) + trailing
        corrected = corrected.replace(match, replacement)
        reasons.append(reason)
    return corrected, reasons


def _django_pytest_target_to_runtests(target: str) -> str:
    target = str(target or "").strip()
    if not target:
        return ""
    path_part, _, selector = target.partition("::")
    target = path_part
    if target.startswith("tests/"):
        target = target[len("tests/"):]
    if target.endswith(".py"):
        target = target[:-3]
    target = target.replace("/", ".")
    if target.startswith("tests."):
        target = target[len("tests."):]
    if selector:
        target = target + "." + selector.replace("::", ".")
    return target


def _rewrite_known_test_commands(command: str, workspace_root: str) -> tuple[str, str]:
    root = Path(workspace_root or os.getcwd()).expanduser().resolve()
    if not (root / "django").is_dir() or not (root / "tests" / "runtests.py").exists():
        return command, ""

    compact = " ".join(str(command or "").strip().split())
    if not compact:
        return command, ""

    pytest_match = re.search(
        r"(?:^|&&|;)\s*(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+pytest|pytest)\s+([^\s|;&]+)",
        compact,
    )
    if pytest_match:
        module = _django_pytest_target_to_runtests(pytest_match.group(1))
        if module:
            return (
                f"cd {shlex.quote(str(root / 'tests'))} && python runtests.py {shlex.quote(module)} -v 2 2>&1",
                "rewrote Django pytest command to tests/runtests.py",
            )

    runtests_root = re.search(r"(?:^|&&|;)\s*python(?:\d+(?:\.\d+)?)?\s+runtests\.py\s+([^\s|;&]+)", compact)
    if runtests_root and "cd tests" not in compact and f"cd {root / 'tests'}" not in compact:
        module = runtests_root.group(1)
        if module.startswith("tests."):
            module = module[len("tests."):]
        return (
            f"cd {shlex.quote(str(root / 'tests'))} && python runtests.py {shlex.quote(module)} -v 2 2>&1",
            "rewrote Django root runtests.py command to tests/runtests.py",
        )

    return command, ""


def hook_filter_read() -> int:
    if not truthy_env("CONTEXTSNIPER_FILTER_ENABLED", "1") or not truthy_env("CONTEXTSNIPER_FILTER_NATIVE_READ", "1"):
        return 0

    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log("call_filter_read", "invalid hook JSON")
        return 0

    session_id = str(hook.get("session_id") or "unknown")
    if session_id == "unknown" or str(hook.get("tool_name") or "") != "Read":
        return 0

    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    file_path = str(tool_input.get("file_path") or "").strip()
    if not file_path:
        return 0

    workspace_root = _workspace_root()
    canonical_file_path, canonical_reason = _canonicalize_read_file_path(file_path, workspace_root)
    if canonical_reason:
        log("call_filter_read", canonical_reason)

    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        if canonical_file_path == file_path:
            return 0
        updated = dict(tool_input)
        updated["file_path"] = canonical_file_path
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "ContextSniper corrected Read path to active workspace",
                "updatedInput": updated,
            }
        }
        sys.stdout.write(json.dumps(response, ensure_ascii=False))
        return 0

    if not ensure_backend_for_hook():
        log("call_filter_read", "backend unavailable")
        return 0

    body = {
        **identity(session_id),
        "file_path": canonical_file_path,
        "workspaceRoot": workspace_root,
        "tool_name": "Read",
    }
    try:
        data = post_json("/api/v1/filter_read", body, timeout=10)
    except Exception as exc:
        log("call_filter_read", f"failed: {exc}")
        return 0

    if not data.get("ok") or not data.get("filtered_file_path"):
        log("call_filter_read", f"skip reason={data.get('reason') or data.get('error') or 'unknown'}")
        return 0

    updated = dict(tool_input)
    updated["file_path"] = str(data["filtered_file_path"])
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "ContextSniper returned filtered L0 for native Read",
            "updatedInput": updated,
        }
    }
    log(
        "call_filter_read",
        "stored=%s detected=%s chars=%s->%s"
        % (
            data.get("memory_uri", ""),
            data.get("detected", ""),
            data.get("original_chars", ""),
            data.get("filtered_chars", ""),
        ),
    )
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0


def hook_filter_grep() -> int:
    if not truthy_env("CONTEXTSNIPER_FILTER_ENABLED", "1"):
        return 0

    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log("call_filter_grep", "invalid hook JSON")
        return 0

    if str(hook.get("tool_name") or "") != "Grep":
        return 0
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    raw_path = str(tool_input.get("path") or "").strip()
    if not raw_path:
        return 0
    canonical_path, reason = _canonicalize_command_path_arg(raw_path, _workspace_root())
    if not reason or canonical_path == raw_path:
        return 0

    updated = dict(tool_input)
    updated["path"] = canonical_path
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "ContextSniper corrected Grep path to active workspace",
            "updatedInput": updated,
        }
    }
    log("call_filter_grep", reason)
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0


def _bash_filter_candidate(command: str) -> str:
    cmd = " ".join((command or "").strip().split())
    lower = cmd.lower()
    if not cmd:
        return ""
    if re.search(r"(^|[\s;&|()])(?:python(?:\d+(?:\.\d+)?)?\s+-m\s+)?pytest\b", lower):
        return "test_runner"
    if re.search(r"(^|[\s;&|()])py\.test\b", lower):
        return "test_runner"
    if "tests/runtests.py" in lower or re.search(r"(^|[\s;&|()])tox\b", lower):
        return "test_runner"
    if re.search(r"(^|[\s;&|()])npm\s+(run\s+)?test\b", lower):
        return "test_runner"
    if re.search(r"(^|[\s;&|()])(?:python|python\d+(?:\.\d+)?)\s+(?:\./)?run_(smoke|filter_probe)\.py\b", lower):
        return "test_runner"
    if re.search(r"(^|[\s;&|()])git\s+diff\b", lower):
        return "diff"
    if re.search(r"(^|[\s;&|()])git\s+show\b", lower):
        return "diff_maybe"
    if re.search(r"(^|[\s;&|()])cat\s+/tmp/[^;&|]*\.(patch|diff)\b", lower):
        return "diff"
    if re.search(r"(^|[\s;&|()])(?:python|python\d+(?:\.\d+)?)\b", lower):
        return "python_maybe"
    return ""


def hook_filter_bash() -> int:
    if not truthy_env("CONTEXTSNIPER_FILTER_ENABLED", "1") or not truthy_env("CONTEXTSNIPER_FILTER_NATIVE_BASH", "1"):
        return 0

    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log("call_filter_bash", "invalid hook JSON")
        return 0

    session_id = str(hook.get("session_id") or "unknown")
    if session_id == "unknown" or str(hook.get("tool_name") or "") != "Bash":
        return 0

    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = str(tool_input.get("command") or "").strip()
    workspace_root = _workspace_root()
    command, path_rewrite_reasons = _canonicalize_command_paths(command, workspace_root)
    for reason in path_rewrite_reasons:
        log("call_filter_bash", reason)
    command, test_rewrite_reason = _rewrite_known_test_commands(command, workspace_root)
    if test_rewrite_reason:
        log("call_filter_bash", test_rewrite_reason)

    candidate = _bash_filter_candidate(command)
    if not candidate:
        if path_rewrite_reasons or test_rewrite_reason:
            updated = dict(tool_input)
            updated["command"] = command
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "ContextSniper corrected Bash command for active workspace",
                    "updatedInput": updated,
                }
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False))
        return 0

    if not ensure_backend_for_hook():
        log("call_filter_bash", "backend unavailable")
        return 0

    run = runtime_dir(None)
    hook_dir = run / "bash-filter-hook"
    hook_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    payload_path = hook_dir / f"{run_id}.json"
    wrapper_path = hook_dir / f"{run_id}.py"
    payload = {
        **identity(session_id),
        "command": command,
        "pre_category": candidate,
        "workspaceRoot": workspace_root,
        "cwd": str(hook.get("cwd") or os.getcwd()),
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    wrapper_path.write_text(
        '''#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request


def post_json(path: str, body: dict, timeout: float = 30.0) -> dict:
    url = (os.environ.get("CONTEXTSNIPER_URL") or "http://127.0.0.1:8090").rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def main() -> int:
    payload = json.loads(open(sys.argv[1], "r", encoding="utf-8").read())
    command = payload.get("command") or ""
    proc = subprocess.run(
        command,
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = proc.stdout or ""
    body = dict(payload)
    body["output"] = output
    body["exit_code"] = proc.returncode
    try:
        result = post_json("/api/v1/filter_bash", body, timeout=float(os.environ.get("CONTEXTSNIPER_FILTER_BASH_TIMEOUT", "30")))
    except Exception:
        sys.stdout.write(output)
        return proc.returncode
    if result.get("ok") and result.get("filtered_output"):
        sys.stdout.write(str(result["filtered_output"]))
    else:
        sys.stdout.write(output)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)

    updated = dict(tool_input)
    updated["command"] = f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper_path))} {shlex.quote(str(payload_path))}"
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"ContextSniper wrapped whitelisted Bash command for filter category {candidate}",
            "updatedInput": updated,
        }
    }
    log("call_filter_bash", f"wrapped category={candidate} command={command[:120]!r}")
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0


def hook_after_turn() -> int:
    raw = sys.stdin.read()
    try:
        hook = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log("call_after_turn", "invalid hook JSON")
        return 0
    session_id = str(hook.get("session_id") or "unknown")
    transcript = str(hook.get("transcript_path") or "")
    event = str(hook.get("hook_event_name") or "unknown")
    if session_id == "unknown" or not transcript or not os.path.isfile(transcript):
        return 0
    path = Path(transcript)
    offset_path = Path(transcript + ".ingest-offset")
    try:
        offset = int(offset_path.read_text().strip()) if offset_path.is_file() else 0
    except ValueError:
        offset = 0
    size = path.stat().st_size
    if size <= offset:
        return 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        chunk = f.read()
    messages = parse_transcript_chunk(chunk)
    if not messages:
        offset_path.write_text(str(size))
        return 0
    if not ensure_backend_for_hook():
        log("call_after_turn", "backend unavailable")
        return 0
    body = {**identity(session_id), "messages": messages, "hook_event_name": event}
    try:
        post_json("/api/v1/after_turn", body, timeout=10)
        offset_path.write_text(str(size))
        log("call_after_turn", f"POST after_turn session={session_id} msgs={len(messages)}")
    except Exception as exc:
        log("call_after_turn", f"failed: {exc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contextsniper", description="ContextSniper terminal plugin commands")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Check ContextSniper health")
    health.set_defaults(func=command_health)

    start = sub.add_parser("start", help="Start local AGFS and ContextSniper services")
    start.add_argument("--runtime-dir")
    start.add_argument("--wait", type=int, default=30)
    start.set_defaults(func=command_start)

    stop = sub.add_parser("stop", help="Stop services started by contextsniper start")
    stop.add_argument("--runtime-dir")
    stop.set_defaults(func=command_stop)

    status = sub.add_parser("status", help="Show service status")
    status.add_argument("--runtime-dir")
    status.set_defaults(func=command_status)

    compose = sub.add_parser("compose", help="Compose memory context for a query")
    compose.add_argument("query", nargs="*")
    compose.add_argument("--session-id")
    compose.add_argument("--json", action="store_true")
    compose.add_argument("--timeout", type=float, default=30)
    compose.set_defaults(func=command_compose)

    policy = sub.add_parser("render-code-policy", help="Render the code policy prompt for hooks or SWE tasks")
    policy.set_defaults(func=command_render_code_policy)

    after = sub.add_parser("after-turn", help="Ingest one transcript or stdin JSONL")
    after.add_argument("--transcript")
    after.add_argument("--session-id")
    after.add_argument("--timeout", type=float, default=60)
    after.set_defaults(func=command_after_turn)

    hist = sub.add_parser("add-history", help="Import current project's Claude transcript history")
    hist.add_argument("--project-dir")
    hist.add_argument("--dry-run", action="store_true")
    hist.add_argument("--yes", action="store_true")
    hist.add_argument("--timeout", type=float, default=60)
    hist.set_defaults(func=command_add_history)

    hook = sub.add_parser("_hook", help=argparse.SUPPRESS)
    hook.add_argument("name", choices=["compose", "add-session-message", "after-turn", "filter-read", "filter-bash", "filter-grep"])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "_hook":
        if args.name == "compose":
            return hook_compose()
        if args.name == "add-session-message":
            return hook_add_session_message()
        if args.name == "after-turn":
            return hook_after_turn()
        if args.name == "filter-read":
            return hook_filter_read()
        if args.name == "filter-bash":
            return hook_filter_bash()
        if args.name == "filter-grep":
            return hook_filter_grep()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
