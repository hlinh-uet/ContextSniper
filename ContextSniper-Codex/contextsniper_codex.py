#!/usr/bin/env python3
"""Run the plain ContextSniper baseline with Codex and bundled validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONTEXTSNIPER_ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from evaluator import (  # noqa: E402
    Project,
    ProjectValidator,
    ProjectWorkspace,
    classify_validation_result,
    event_usage,
)
from benchmark_lookup_guard import (  # noqa: E402
    LOG_ENV as LOOKUP_GUARD_LOG_ENV,
    MARKERS_ENV as LOOKUP_MARKERS_ENV,
    SENSITIVE_ROOTS_ENV as LOOKUP_SENSITIVE_ROOTS_ENV,
    benchmark_lookup_markers,
    command_sensitive_path_violation,
    event_lookup_violation,
    path_value_may_exist,
)


DEFAULT_DATASET_ROOT = CONTEXTSNIPER_ROOT.parent / "defects4c"
DEFAULT_OUTPUT_ROOT = HERE / "output_logs"
DEFAULT_AGENT = "codex"
DEFAULT_MODEL_PROVIDER = "openai"
OPENROUTER_MODEL_PROVIDER = "openrouter"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_REASONING_EFFORT = "low"
AGENT_HARNESS_PROFILE = "contextsniper-codex-parity-v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_MODEL_CATALOG = HERE / "models" / "deepseek-v4-flash-0731.json"
OPENROUTER_MODEL_CONTEXT_WINDOW = 1_310_720
BENCHMARK_LOOKUP_GUARD = HERE / "benchmark_lookup_guard.py"
PLAIN_SETTINGS_PATH = CONTEXTSNIPER_ROOT / "scripts" / "SWE" / "contextsniper_plain_defaults.sh"
CASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
TOOL_COMPATIBILITY_ERROR_RES = (
    re.compile(r"unsupported call:\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE),
    re.compile(r"unknown MCP server ['\"]([^'\"]+)", re.IGNORECASE),
    re.compile(r"unsupported tool[^A-Za-z0-9_.:-]+([A-Za-z0-9_.:-]+)", re.IGNORECASE),
    re.compile(r"`?justification`? requires (?:an )?explicit `?sandbox_permissions`?", re.IGNORECASE),
    re.compile(r"approval policy is Never", re.IGNORECASE),
)
UNSUPPORTED_TOOL_CALL_LIMIT = 5


from runner_errors import RunnerError


def _progress(message: str) -> None:
    print(f"[ContextSniper-Codex] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class CaseInputs:
    case_id: str
    project: Path
    config: Path
    failure_log: Path
    failing_tests: tuple[str, ...]
    environment_mode: str
    environment_runtime: str
    environment_image: str
    raw_config: dict[str, Any]


@dataclass(frozen=True)
class RunPaths:
    root: Path
    workspace: Path
    input_dir: Path
    logs: Path
    runtime: Path
    validation: Path
    prompt: Path
    events: Path
    stderr: Path
    response: Path
    patch: Path
    result: Path


@dataclass(frozen=True)
class CodexRun:
    returncode: int
    timed_out: bool
    elapsed_seconds: float
    compatibility_error: str | None = None
    policy_violation: dict[str, str] | None = None


@dataclass(frozen=True)
class RepairAgentConfig:
    agent: str
    model_provider: str
    model: str
    reasoning_effort: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one prepared benchmark case with plain ContextSniper, "
            "Codex, and the bundled benchmark validator."
        )
    )
    parser.add_argument("case_id", help="Prepared case id, for example CVE-2016-3132__28a6ed9f9a36")
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument(
        "--inputs-dir",
        type=Path,
        help=(
            "Directory containing either the flat Defects4C triplet or SWE-style "
            "<case-id>/{<case-id>/,config.json,failure.log}; overrides the default."
        ),
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--agent",
        choices=(DEFAULT_AGENT, "openhands"),
        default=DEFAULT_AGENT,
        help="Repair harness: codex (default) or openhands.",
    )
    parser.add_argument(
        "--model",
        help=(
            f"Repair model (default: {DEFAULT_MODEL}). Passing "
            f"{DEFAULT_OPENROUTER_MODEL} keeps Codex as the agent and routes "
            "model inference through OpenRouter."
        ),
    )
    parser.add_argument(
        "--model-provider",
        choices=(DEFAULT_MODEL_PROVIDER, OPENROUTER_MODEL_PROVIDER),
        help=(
            "Optional provider override. By default, slash-qualified model "
            "names such as deepseek/deepseek-v4-flash-0731 use OpenRouter; other models "
            "use Codex's built-in OpenAI provider."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        help=(
            f"Optional reasoning-effort override. {DEFAULT_MODEL} defaults to "
            f"{DEFAULT_REASONING_EFFORT}; {DEFAULT_OPENROUTER_MODEL} also defaults to "
            f"{DEFAULT_REASONING_EFFORT}."
        ),
    )
    parser.add_argument("--openhands-python", default=str(HERE / ".venv-openhands" / "bin" / "python"))
    parser.add_argument("--openhands-auth", choices=("api-key", "subscription"), default="subscription")
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument("--contextsniper-python", default=os.environ.get("CONTEXTSNIPER_PYTHON", ""))
    parser.add_argument("--codex-timeout", "--agent-timeout", dest="codex_timeout", type=int, default=1800)
    parser.add_argument("--command-timeout", type=int, default=1800)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--contextsniper-port", type=int)
    parser.add_argument("--agfs-port", type=int)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve inputs and print the execution plan without creating artifacts or starting services.",
    )
    return parser.parse_args(argv)


def resolve_agent_config(args: argparse.Namespace) -> RepairAgentConfig:
    agent = str(args.agent or DEFAULT_AGENT).strip()
    if agent not in {DEFAULT_AGENT, "openhands"}:
        raise RunnerError(f"Unsupported repair agent: {agent!r}")

    requested_provider = str(args.model_provider or "").strip()
    requested_model = str(args.model or "").strip()
    if requested_model:
        model = requested_model
    elif requested_provider == OPENROUTER_MODEL_PROVIDER:
        model = DEFAULT_OPENROUTER_MODEL
    else:
        model = DEFAULT_MODEL

    model_provider = requested_provider or (
        OPENROUTER_MODEL_PROVIDER if "/" in model else DEFAULT_MODEL_PROVIDER
    )
    requested_effort = str(args.reasoning_effort or "").strip() or None
    reasoning_effort = requested_effort
    if reasoning_effort is None and (
        model_provider == DEFAULT_MODEL_PROVIDER or model == DEFAULT_OPENROUTER_MODEL
    ):
        reasoning_effort = DEFAULT_REASONING_EFFORT
    return RepairAgentConfig(
        agent=agent,
        model_provider=model_provider,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def _resolved_directory(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise RunnerError(f"{label} does not exist or is not a directory: {resolved}")
    return resolved


def resolve_case(
    dataset_root: Path, case_id: str, inputs_dir: Path | None = None
) -> CaseInputs:
    if not CASE_NAME_RE.fullmatch(case_id):
        raise RunnerError(f"Unsafe case id: {case_id!r}")
    if inputs_dir is not None:
        inputs = _resolved_directory(inputs_dir, "Prepared inputs directory")
    else:
        dataset = _resolved_directory(dataset_root, "Dataset root")
        inputs = dataset / "out_tmp_dirs" / "debugging_framework" / "php" / "inputs"
    flat = (
        inputs / case_id,
        inputs / f"{case_id}.debugging-framework.json",
        inputs / f"{case_id}.failure.log",
    )
    bundle_root = inputs / case_id
    bundled = (
        bundle_root / case_id,
        bundle_root / "config.json",
        bundle_root / "failure.log",
    )
    if flat[0].is_dir() and flat[1].is_file() and flat[2].is_file():
        project, config, failure = flat
    elif bundled[0].is_dir() and bundled[1].is_file() and bundled[2].is_file():
        project, config, failure = bundled
    else:
        raise RunnerError(
            "Prepared case is neither a flat contract triplet nor a SWE case bundle: "
            f"{inputs / case_id}"
        )
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"Could not read project contract {config}: {exc}") from exc
    repair = raw.get("repair") if isinstance(raw.get("repair"), dict) else {}
    raw_tests = repair.get("failing_tests")
    if not isinstance(raw_tests, list):
        raise RunnerError("Project contract repair.failing_tests must be a non-empty list")
    failing_tests = tuple(str(value).strip() for value in raw_tests if str(value).strip())
    if not failing_tests or len(failing_tests) != len(raw_tests):
        raise RunnerError("Project contract repair.failing_tests contains an empty value")
    environment = raw.get("environment") if isinstance(raw.get("environment"), dict) else {}
    mode = str(environment.get("mode") or "").strip()
    runtime = str(environment.get("runtime") or "auto").strip() or "auto"
    image = str(environment.get("image") or "").strip()
    if mode not in {"host", "image"}:
        raise RunnerError("Project contract environment.mode must be host or image")
    if mode == "image" and not image:
        raise RunnerError("Project contract environment.image is required in image mode")
    return CaseInputs(
        case_id=case_id,
        project=project.resolve(),
        config=config.resolve(),
        failure_log=failure.resolve(),
        failing_tests=failing_tests,
        environment_mode=mode,
        environment_runtime=runtime,
        environment_image=image,
        raw_config=raw,
    )


def read_code_policy() -> str:
    path = CONTEXTSNIPER_ROOT / "claude-plugin" / "prompts" / "code_policy_injection.txt"
    if not path.is_file():
        raise RunnerError(f"ContextSniper code policy is missing: {path}")
    policy = path.read_text(encoding="utf-8").strip()
    if not policy:
        raise RunnerError(f"ContextSniper code policy is empty: {path}")
    return policy


def render_code_policy(
    *, env: dict[str, str], paths: RunPaths, python_bin: Path
) -> str:
    """Render the same policy text used by the existing plain Claude runner."""
    terminal = CONTEXTSNIPER_ROOT / "claude-plugin" / "scripts" / "contextsniper_terminal.py"
    completed = _run_logged(
        [str(python_bin), str(terminal), "render-code-policy"],
        env=env,
        cwd=CONTEXTSNIPER_ROOT,
        stdout_path=paths.logs / "contextsniper-code-policy.txt",
        stderr_path=paths.logs / "contextsniper-code-policy.stderr.log",
        timeout=20,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RunnerError(
            "Could not render the ContextSniper code policy: "
            + (detail[-1] if detail else "empty output")
        )
    return completed.stdout.strip()


def build_prompt(
    case: CaseInputs,
    failure_output: str,
    policy: str,
) -> str:
    tests = "\n".join(f"- {test_id}" for test_id in case.failing_tests)
    host_mapping = """This run uses Codex instead of Claude Code. Read the reference above to Claude's built-in Edit tool as a reference to Codex's native patch/edit mechanisms. Follow the same substitution: use the ContextSniper MCP tool named `edit_file` for source edits. Before editing source code, call the ContextSniper MCP tool named `search_code` at least once with a focused query."""
    return f"""Fix the defect in the current workspace.

Case: {case.case_id}

The following failing tests and failure output were observed by the benchmark adapter. Treat them as the failure baseline. Do not modify tests, fixtures, build scripts, benchmark configuration, or validation infrastructure. Make only the production-source changes needed to fix the defect.

Use only files inside the current workspace. Do not inspect parent or sibling directories, benchmark metadata, ground-truth/reference/gold patches, or any other copy of this project outside the current workspace.

General web research about programming concepts, APIs, and language semantics is allowed. Do not look up this benchmark case, its known fix, or its answer online. Never put the case/CVE identifier, failing bug or test identifier/name, or benchmark commit hash into a web search, URL, curl/wget command, network request, or external search tool. Derive the repair from local workspace evidence. ContextSniper `search_code` is local repository retrieval and remains allowed.

Failing tests:
{tests}

Observed failure output:
<failure_output>
{failure_output.rstrip()}
</failure_output>

{policy}

## Codex host mapping

{host_mapping}
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def make_run_paths(output_root: Path, case_id: str) -> RunPaths:
    output = output_root.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = output / f"{stamp}-{case_id}-p{os.getpid()}"
    root.mkdir(parents=False, exist_ok=False)
    latest = output / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(root.name)
    input_dir = root / "input"
    logs = root / "logs"
    validation = root / "validation"
    runtime = root / "runtime"
    for directory in (input_dir, logs, validation, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    return RunPaths(
        root=root,
        workspace=root / "workspace",
        input_dir=input_dir,
        logs=logs,
        runtime=runtime,
        validation=validation,
        prompt=root / "prompt.txt",
        events=root / "events.jsonl",
        stderr=logs / "codex-stderr.log",
        response=root / "response.txt",
        patch=root / "patch.diff",
        result=root / "result.json",
    )


PLAIN_SETTING_NAMES = (
    "CONTEXTSNIPER_CODE_TOGGLE_FORCE",
    "CONTEXTSNIPER_SEARCH_LIMIT",
    "CONTEXTSNIPER_SEARCH_FORCE_LIMIT",
    "VECTOR_DB_TYPE",
    "CONTEXTSNIPER_CODE_TOGGLE",
    "EMBEDDING_PROVIDER",
    "CONTEXTSNIPER_EMBEDDING_MODEL",
    "CONTEXTSNIPER_EMBEDDING_BASE_URL",
    "CONTEXTSNIPER_EMBEDDING_PROBE_REQUIRED",
    "CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED",
    "CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED",
    "CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED",
    "CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED",
    "CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES",
    "CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES",
    "CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS",
    "CONTEXTSNIPER_CODE_FUSE_MODE",
    "CONTEXTSNIPER_CODE_FUSE_W_EMBED",
    "CONTEXTSNIPER_CODE_FUSE_W_BM25",
    "CONTEXTSNIPER_CODE_FUSE_W_CTAGS",
    "CONTEXTSNIPER_CODE_FUSE_W_GRAPH",
    "CONTEXTSNIPER_BOOTSTRAP_MAX_FILES",
    "CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES",
    "CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION",
    "CONTEXTSNIPER_START_LOCAL_EMBED_SERVER",
    "CONTEXTSNIPER_PLUGIN_AUTO_START",
    "CONTEXTSNIPER_PLUGIN_AUTO_STOP",
    "CONTEXTSNIPER_PLUGIN_START_WAIT",
    "CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME",
    "CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT",
    "CONTEXTSNIPER_APPEND_CODE_POLICY_TO_TASK",
    "CONTEXTSNIPER_FILTER_ENABLED",
    "CONTEXTSNIPER_FILTER_NATIVE_READ",
    "CONTEXTSNIPER_FILTER_NATIVE_BASH",
    "CONTEXTSNIPER_INJECT_FILTERING_PROMPT",
    "CONTEXTSNIPER_FORCE_EMBED_DIM_ALIGN",
    "OPENGAUSS_DIMENSION",
)


def load_plain_contextsniper_settings(base: dict[str, str]) -> dict[str, str]:
    """Source the same canonical settings file as the original Claude runner."""
    if not PLAIN_SETTINGS_PATH.is_file():
        raise RunnerError(f"Plain ContextSniper settings are missing: {PLAIN_SETTINGS_PATH}")
    seed = {str(key): str(value) for key, value in base.items()}
    seed["CONTEXTSNIPER_DIR"] = str(CONTEXTSNIPER_ROOT)
    completed = subprocess.run(
        ["bash", "-c", '. "$1"; /usr/bin/env -0', "contextsniper-settings", str(PLAIN_SETTINGS_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=seed,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RunnerError(f"Could not load plain ContextSniper settings: {detail}")
    resolved: dict[str, str] = {}
    for item in completed.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        resolved[key.decode("utf-8", errors="surrogateescape")] = value.decode(
            "utf-8", errors="surrogateescape"
        )
    return resolved


def public_plain_settings(env: dict[str, str]) -> dict[str, Any]:
    settings = {name: str(env.get(name) or "") for name in PLAIN_SETTING_NAMES}
    settings["CONTEXTSNIPER_EMBEDDING_API_KEY_CONFIGURED"] = bool(
        str(env.get("CONTEXTSNIPER_EMBEDDING_API_KEY") or "").strip()
    )
    settings["settings_file"] = str(PLAIN_SETTINGS_PATH.relative_to(CONTEXTSNIPER_ROOT))
    return settings


def build_contextsniper_env(
    base: dict[str, str],
    *,
    workspace: Path,
    runtime_dir: Path,
    python_bin: Path,
    contextsniper_port: int,
    agfs_port: int,
    run_id: str,
) -> dict[str, str]:
    env = dict(base)
    env["PY_BIN"] = str(python_bin)
    env["CONTEXTSNIPER_DIR"] = str(CONTEXTSNIPER_ROOT)
    env["CONTEXTSNIPER_WORKSPACE_ROOT"] = str(workspace)
    # Evaluation isolation only: keep caller-supplied MCP roots inside the
    # copied per-run workspace without changing ContextSniper retrieval knobs.
    env["CONTEXTSNIPER_ENFORCE_WORKSPACE_ROOT"] = "1"
    prior_pythonpath = str(env.get("PYTHONPATH") or "")
    env["PYTHONPATH"] = str(workspace) + (os.pathsep + prior_pythonpath if prior_pythonpath else "")
    env["CONTEXTSNIPER_RUNTIME_DIR"] = str(runtime_dir)
    env["CONTEXTSNIPER_HTTP_PORT"] = str(contextsniper_port)
    env["AGFS_HTTP_PORT"] = str(agfs_port)
    env["CONTEXTSNIPER_URL"] = f"http://127.0.0.1:{contextsniper_port}"
    env["AGFS_BASE_URL"] = f"http://127.0.0.1:{agfs_port}"
    env["CONTEXTSNIPER_ACCOUNT_ID"] = f"acct-defects4c-{run_id}"
    env["CONTEXTSNIPER_USER_ID"] = f"u-codex-{run_id}"
    env["CONTEXTSNIPER_AGENT_ID"] = f"codex-{run_id}"
    env["CONTEXTSNIPER_SESSION_ID"] = f"defects4c-{run_id}"
    no_proxy = str(env.get("NO_PROXY") or env.get("no_proxy") or "")
    loopback = "127.0.0.1,localhost,::1"
    env["NO_PROXY"] = f"{loopback},{no_proxy}" if no_proxy else loopback
    env["no_proxy"] = env["NO_PROXY"]
    return env


def _python_has_contextsniper_deps(candidate: Path) -> bool:
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return False
    completed = subprocess.run(
        [str(candidate), "-c", "import flask, mcp, openai, pyagfs"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    return completed.returncode == 0


def choose_contextsniper_python(explicit: str) -> Path:
    candidates = [
        explicit,
        os.environ.get("PY_BIN", ""),
        str(CONTEXTSNIPER_ROOT / ".venv" / "bin" / "python"),
        str(CONTEXTSNIPER_ROOT / ".venv" / "bin" / "python3"),
    ]
    for raw in candidates:
        if not str(raw or "").strip():
            continue
        # Do not resolve the final symlink here. Virtual-environment Python
        # executables are commonly symlinks to the base interpreter; invoking
        # the resolved target bypasses the venv and loses its site-packages.
        candidate = Path(os.path.abspath(os.path.expanduser(str(raw))))
        if _python_has_contextsniper_deps(candidate):
            return candidate
    raise RunnerError(
        "No ContextSniper Python runtime has flask, mcp, openai, and pyagfs. "
        "Run ./bootstrap.sh or pass --contextsniper-python /path/to/python."
    )


def _command_path(value: str, label: str) -> Path:
    raw = str(value or "").strip()
    found = shutil.which(raw) if raw else None
    candidate = Path(found or raw).expanduser().resolve() if raw else Path()
    if not raw or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RunnerError(f"{label} executable was not found: {value!r}")
    return candidate


def ensure_embedding_backend(env: dict[str, str]) -> None:
    """Match the original runner's opt-in embedding health probe."""
    if str(env.get("CONTEXTSNIPER_EMBEDDING_PROBE_REQUIRED") or "0") != "1":
        return
    provider = str(env.get("EMBEDDING_PROVIDER") or "openai").strip().lower()
    if provider != "openai":
        return
    key = str(env.get("CONTEXTSNIPER_EMBEDDING_API_KEY") or "").strip()
    if not key:
        raise RunnerError(
            "Embedding key is empty; export OPENROUTER_API_KEY (or the lower-level "
            "CONTEXTSNIPER_EMBEDDING_API_KEY) before probing the "
            "OpenAI-compatible embedding backend."
        )
    base = str(
        env.get("CONTEXTSNIPER_EMBEDDING_BASE_URL") or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    url = base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")
    body = json.dumps(
        {
            "model": str(
                env.get("CONTEXTSNIPER_EMBEDDING_MODEL")
                or "openai/text-embedding-3-small"
            ),
            "input": ["embedding health probe"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if not 200 <= int(getattr(response, "status", 200)) < 300:
                raise RunnerError(f"Embedding probe returned HTTP {response.status}")
    except Exception as exc:
        if isinstance(exc, RunnerError):
            raise
        raise RunnerError(
            "Embedding probe failed; check OPENROUTER_API_KEY, "
            "CONTEXTSNIPER_EMBEDDING_API_KEY, "
            "CONTEXTSNIPER_EMBEDDING_BASE_URL, and CONTEXTSNIPER_EMBEDDING_MODEL."
        ) from exc


def ensure_agent_credentials(
    agent_config: RepairAgentConfig, env: dict[str, str]
) -> None:
    if agent_config.model_provider != OPENROUTER_MODEL_PROVIDER:
        return
    if (
        agent_config.agent == "codex"
        and agent_config.model == DEFAULT_OPENROUTER_MODEL
        and not OPENROUTER_MODEL_CATALOG.is_file()
    ):
        raise RunnerError(
            f"Bundled DeepSeek model catalog is missing: {OPENROUTER_MODEL_CATALOG}"
        )
    if not str(env.get(OPENROUTER_API_KEY_ENV) or "").strip():
        raise RunnerError(
            f"{OPENROUTER_API_KEY_ENV} is required for "
            f"--model {agent_config.model}"
        )


def preflight(
    args: argparse.Namespace,
    plain_env: dict[str, str],
    agent_config: RepairAgentConfig,
) -> tuple[Path, Path]:
    if args.codex_timeout < 1 or args.command_timeout < 1:
        raise RunnerError("Timeouts must be positive")
    if args.jobs < 0:
        raise RunnerError("--jobs must be non-negative")
    ensure_agent_credentials(agent_config, plain_env)
    if agent_config.agent == "openhands":
        from openhands_adapter import preflight_openhands
        codex = preflight_openhands(args, agent_config, plain_env)
    else:
        codex = _command_path(args.codex_bin, "Codex")
    python_bin = choose_contextsniper_python(args.contextsniper_python)
    agfs = plain_env.get("AGFS_BIN", "")
    agfs_candidate = (
        Path(agfs).expanduser().resolve()
        if agfs
        else CONTEXTSNIPER_ROOT / "agfs" / "build" / "agfs-server"
    )
    if not agfs_candidate.is_file() or not os.access(agfs_candidate, os.X_OK):
        if not shutil.which("agfs-server"):
            raise RunnerError("agfs-server is missing. Run ./bootstrap.sh to build agfs/build/agfs-server.")
    ensure_embedding_backend(plain_env)
    return codex, python_bin


def find_port_pair(requested_contextsniper: int | None, requested_agfs: int | None) -> tuple[int, int]:
    requested = [requested_contextsniper, requested_agfs]
    if any(value is not None and not 1 <= value <= 65535 for value in requested):
        raise RunnerError("Ports must be between 1 and 65535")
    if requested_contextsniper and requested_agfs:
        if requested_contextsniper == requested_agfs:
            raise RunnerError("ContextSniper and AGFS ports must differ")
        return requested_contextsniper, requested_agfs

    reserved: list[socket.socket] = []
    try:
        values: list[int] = []
        for requested_value in requested:
            if requested_value:
                values.append(requested_value)
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            reserved.append(sock)
            values.append(int(sock.getsockname()[1]))
        if values[0] == values[1]:
            raise RunnerError("Could not allocate distinct ContextSniper and AGFS ports")
        return values[0], values[1]
    finally:
        for sock in reserved:
            sock.close()


def build_codex_command(
    *,
    codex_bin: Path,
    workspace: Path,
    response_path: Path,
    model: str,
    reasoning_effort: str | None,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    hook_python: Path | None = None,
    lookup_guard_path: Path = BENCHMARK_LOOKUP_GUARD,
) -> list[str]:
    if model_provider not in {
        DEFAULT_MODEL_PROVIDER,
        OPENROUTER_MODEL_PROVIDER,
    }:
        raise RunnerError(f"Unsupported model provider: {model_provider!r}")
    launcher = CONTEXTSNIPER_ROOT / "claude-plugin" / "bin" / "contextsniper-mcp"
    env_names = [
        "PY_BIN",
        "PYTHONPATH",
        "CONTEXTSNIPER_DIR",
        "CONTEXTSNIPER_WORKSPACE_ROOT",
        "CONTEXTSNIPER_ENFORCE_WORKSPACE_ROOT",
        "CONTEXTSNIPER_RUNTIME_DIR",
        "CONTEXTSNIPER_HTTP_PORT",
        "CONTEXTSNIPER_URL",
        "CONTEXTSNIPER_ACCOUNT_ID",
        "CONTEXTSNIPER_USER_ID",
        "CONTEXTSNIPER_AGENT_ID",
        "CONTEXTSNIPER_SESSION_ID",
        "CONTEXTSNIPER_CODE_TOGGLE_FORCE",
        "CONTEXTSNIPER_SEARCH_LIMIT",
        "CONTEXTSNIPER_FILTER_ENABLED",
        "CONTEXTSNIPER_FILTER_NATIVE_READ",
        "CONTEXTSNIPER_FILTER_NATIVE_BASH",
        "CONTEXTSNIPER_INJECT_FILTERING_PROMPT",
        "CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT",
        "CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION",
        "CONTEXTSNIPER_PLUGIN_AUTO_START",
        "CONTEXTSNIPER_PLUGIN_AUTO_STOP",
        "CONTEXTSNIPER_PLUGIN_START_WAIT",
        "CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME",
        "CONTEXTSNIPER_CODE_TOGGLE",
        "CONTEXTSNIPER_SEARCH_FORCE_LIMIT",
        "CONTEXTSNIPER_EMBEDDING_API_KEY",
        "CONTEXTSNIPER_EMBEDDING_BASE_URL",
        "CONTEXTSNIPER_EMBEDDING_MODEL",
        "CONTEXTSNIPER_EMBEDDING_PROBE_REQUIRED",
        "CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED",
        "CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED",
        "CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED",
        "CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED",
        "CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES",
        "CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES",
        "CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS",
        "CONTEXTSNIPER_CODE_FUSE_MODE",
        "CONTEXTSNIPER_CODE_FUSE_W_EMBED",
        "CONTEXTSNIPER_CODE_FUSE_W_BM25",
        "CONTEXTSNIPER_CODE_FUSE_W_CTAGS",
        "CONTEXTSNIPER_CODE_FUSE_W_GRAPH",
        "CONTEXTSNIPER_BOOTSTRAP_MAX_FILES",
        "CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES",
        "CONTEXTSNIPER_START_LOCAL_EMBED_SERVER",
        "CONTEXTSNIPER_APPEND_CODE_POLICY_TO_TASK",
        "CONTEXTSNIPER_FORCE_EMBED_DIM_ALIGN",
        "VECTOR_DB_TYPE",
        "EMBEDDING_PROVIDER",
        "OPENGAUSS_DIMENSION",
        "AGFS_HTTP_PORT",
        "AGFS_BASE_URL",
        "AGFS_BIN",
        "NO_PROXY",
        "no_proxy",
    ]
    env_vars = json.dumps(env_names, separators=(",", ":"))
    provider_config = []
    if model_provider == OPENROUTER_MODEL_PROVIDER:
        provider_config = [
            "-c",
            f"model_provider={json.dumps(OPENROUTER_MODEL_PROVIDER)}",
            "-c",
            f"model_providers.{OPENROUTER_MODEL_PROVIDER}.name=\"OpenRouter\"",
            "-c",
            (
                f"model_providers.{OPENROUTER_MODEL_PROVIDER}.base_url="
                f"{json.dumps(OPENROUTER_BASE_URL)}"
            ),
            "-c",
            (
                f"model_providers.{OPENROUTER_MODEL_PROVIDER}.env_key="
                f"{json.dumps(OPENROUTER_API_KEY_ENV)}"
            ),
            "-c",
            f"model_providers.{OPENROUTER_MODEL_PROVIDER}.wire_api=\"responses\"",
            "-c",
            (
                f"model_providers.{OPENROUTER_MODEL_PROVIDER}."
                "requires_openai_auth=false"
            ),
            # Keep hosted web search disabled for OpenRouter model parity.
            # General network research remains possible through the sandboxed
            # shell; exact benchmark lookup is independently blocked by the hook.
            "-c",
            'web_search="disabled"',
        ]
        if model == DEFAULT_OPENROUTER_MODEL:
            provider_config.extend(
                [
                    "-c",
                    f"model_catalog_json={json.dumps(str(OPENROUTER_MODEL_CATALOG))}",
                    "-c",
                    f"model_context_window={OPENROUTER_MODEL_CONTEXT_WINDOW}",
                ]
            )

    reasoning_config = (
        ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
        if reasoning_effort
        else []
    )
    guard_python = hook_python or Path(sys.executable)
    guard_command = shlex.join([str(guard_python), str(lookup_guard_path)])
    hook_config = (
        "hooks.PreToolUse=[{matcher=\"^Bash$\",hooks=[{type=\"command\","
        f"command={json.dumps(guard_command)},timeout=5,"
        'statusMessage="Checking benchmark lookup policy"}]}]'
    )
    return [
        str(codex_bin),
        "exec",
        "--cd",
        str(workspace),
        "--color",
        "never",
        "--json",
        "--output-last-message",
        str(response_path),
        "-c",
        'approval_policy="never"',
        "-c",
        'default_permissions="contextsniper_eval"',
        "-c",
        'permissions.contextsniper_eval.filesystem={":minimal"="read",":workspace_roots"={"."="write"}}',
        "-c",
        "agents.enabled=false",
        "-c",
        hook_config,
        "--enable",
        "hooks",
        "--dangerously-bypass-hook-trust",
        *provider_config,
        *reasoning_config,
        "--disable",
        "multi_agent",
        "-c",
        f"mcp_servers.contextsniper.command={json.dumps(str(launcher))}",
        "-c",
        f"mcp_servers.contextsniper.env_vars={env_vars}",
        "-c",
        "mcp_servers.contextsniper.required=true",
        "-c",
        'mcp_servers.contextsniper.enabled_tools=["search_code","edit_file"]',
        "-c",
        'mcp_servers.contextsniper.default_tools_approval_mode="approve"',
        "-c",
        "mcp_servers.contextsniper.startup_timeout_sec=90",
        "-c",
        "mcp_servers.contextsniper.tool_timeout_sec=180",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model,
        "-",
    ]


def run_codex(
    command: list[str],
    *,
    prompt: str,
    workspace: Path,
    env: dict[str, str],
    events_path: Path,
    stderr_path: Path,
    timeout: int,
    lookup_markers: tuple[str, ...] = (),
    agent_label: str = "Codex",
) -> CodexRun:
    started = time.monotonic()
    timed_out = False
    compatibility_error: str | None = None
    policy_violation: dict[str, str] | None = None
    audited_event_lines = 0
    next_heartbeat = started + 30

    def stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    with events_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(workspace),
            env=env,
            start_new_session=True,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
            deadline = started + timeout
            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    stop_process(process)
                    break
                if now >= next_heartbeat:
                    _progress(
                        f"{agent_label} is still running ({int(now - started)} seconds elapsed)"
                    )
                    next_heartbeat = now + 30
                stderr_handle.flush()
                stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
                compatibility_failures: list[str] = []
                for pattern in TOOL_COMPATIBILITY_ERROR_RES:
                    for match in pattern.finditer(stderr_text):
                        label = match.group(1) if match.lastindex else match.group(0)
                        compatibility_failures.append(label.strip())
                if len(compatibility_failures) >= UNSUPPORTED_TOOL_CALL_LIMIT:
                    counts: dict[str, int] = {}
                    for name in compatibility_failures:
                        counts[name] = counts.get(name, 0) + 1
                    detail = ", ".join(
                        f"{name} x{count}"
                        for name, count in sorted(
                            counts.items(), key=lambda item: (-item[1], item[0])
                        )
                    )
                    compatibility_error = (
                        "Codex/model tool-schema mismatch; repeated unsupported "
                        f"tool calls detected ({detail})"
                    )
                    stop_process(process)
                    break
                if lookup_markers:
                    stdout_handle.flush()
                    event_lines = events_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    for raw_line in event_lines[audited_event_lines:]:
                        try:
                            event = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict) or event.get("type") not in {
                            "item.started",
                            "item.completed",
                        }:
                            continue
                        item = event.get("item")
                        if not isinstance(item, dict):
                            continue
                        policy_violation = event_lookup_violation(
                            item, lookup_markers
                        )
                        if policy_violation is not None:
                            _progress(
                                "Stopping Codex after a prohibited exact "
                                "benchmark network lookup attempt"
                            )
                            stop_process(process)
                            break
                    audited_event_lines = len(event_lines)
                    if policy_violation is not None:
                        break
                time.sleep(0.5)
            if process.poll() is None:
                process.wait(timeout=5)
        except BaseException:
            stop_process(process)
            raise
    return CodexRun(
        returncode=int(process.returncode if process.returncode is not None else -1),
        timed_out=timed_out,
        elapsed_seconds=round(time.monotonic() - started, 3),
        compatibility_error=compatibility_error,
        policy_violation=policy_violation,
    )


def _run_logged(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
        env=env,
        timeout=timeout,
        check=False,
    )
    stdout_path.write_text(completed.stdout or "", encoding="utf-8")
    stderr_path.write_text(completed.stderr or "", encoding="utf-8")
    return completed


def _api_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    value = json.loads(raw) if raw.strip() else {}
    return value if isinstance(value, dict) else {"value": value}


def start_contextsniper(env: dict[str, str], paths: RunPaths, python_bin: Path) -> None:
    terminal = CONTEXTSNIPER_ROOT / "claude-plugin" / "scripts" / "contextsniper_terminal.py"
    completed = _run_logged(
        [str(python_bin), str(terminal), "start", "--runtime-dir", str(paths.runtime), "--wait", "60"],
        env=env,
        cwd=CONTEXTSNIPER_ROOT,
        stdout_path=paths.logs / "contextsniper-start.stdout.log",
        stderr_path=paths.logs / "contextsniper-start.stderr.log",
        timeout=90,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise RunnerError(
            "ContextSniper service failed to start: " + (detail[-1] if detail else "unknown error")
        )


def stop_contextsniper(env: dict[str, str], paths: RunPaths, python_bin: Path) -> None:
    terminal = CONTEXTSNIPER_ROOT / "claude-plugin" / "scripts" / "contextsniper_terminal.py"
    try:
        _run_logged(
            [str(python_bin), str(terminal), "stop", "--runtime-dir", str(paths.runtime)],
            env=env,
            cwd=CONTEXTSNIPER_ROOT,
            stdout_path=paths.logs / "contextsniper-stop.stdout.log",
            stderr_path=paths.logs / "contextsniper-stop.stderr.log",
            timeout=20,
        )
    except Exception as exc:
        (paths.logs / "contextsniper-stop.stderr.log").write_text(str(exc) + "\n", encoding="utf-8")


def compose_prompt_context(
    prompt: str,
    *,
    env: dict[str, str],
    paths: RunPaths,
    python_bin: Path,
) -> str:
    hook = CONTEXTSNIPER_ROOT / "claude-plugin" / "scripts" / "hook_compose.py"
    payload = {
        "prompt": prompt,
        "session_id": env["CONTEXTSNIPER_SESSION_ID"],
        "hook_event_name": "UserPromptSubmit",
    }
    completed = _run_logged(
        [str(python_bin), str(hook)],
        env=env,
        cwd=CONTEXTSNIPER_ROOT,
        stdout_path=paths.logs / "contextsniper-compose.json",
        stderr_path=paths.logs / "contextsniper-compose.stderr.log",
        input_text=json.dumps(payload),
        timeout=45,
    )
    if completed.returncode != 0:
        raise RunnerError(f"ContextSniper compose hook exited with code {completed.returncode}")
    if not completed.stdout.strip():
        return ""
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"ContextSniper compose hook returned invalid JSON: {exc}") from exc
    hook_output = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    if not isinstance(hook_output, dict):
        return ""
    return str(hook_output.get("additionalContext") or "").strip()


def contextsniper_mcp_usage(events_path: Path) -> dict[str, Any]:
    """Summarize successful/failed calls to the two baseline MCP tools."""
    tools = {
        name: {"successful_calls": 0, "failed_calls": 0}
        for name in ("search_code", "edit_file")
    }
    if events_path.is_file():
        for raw_line in events_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if (
                event.get("type") != "item.completed"
                or not isinstance(item, dict)
                or item.get("type") != "mcp_tool_call"
                or item.get("server") != "contextsniper"
            ):
                continue
            tool = str(item.get("tool") or "")
            if tool not in tools:
                continue
            if item.get("status") == "completed" and not item.get("error"):
                tools[tool]["successful_calls"] += 1
            else:
                tools[tool]["failed_calls"] += 1
    return {
        "tools": tools,
        "search_succeeded": tools["search_code"]["successful_calls"] > 0,
        "edit_succeeded": tools["edit_file"]["successful_calls"] > 0,
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _shell_payload(command: str) -> str:
    """Extract the script passed to the shell from a Codex event."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) >= 3 and parts[1] in {"-c", "-lc"}:
        return parts[2]
    return command


def benchmark_sensitive_roots(case: CaseInputs) -> tuple[Path, ...]:
    """Roots containing runner code, prepared cases, or benchmark metadata."""
    candidates = [
        CONTEXTSNIPER_ROOT,
        case.project.parent,
        case.config.parent,
        case.failure_log.parent,
        case.config.parent.parent,
    ]
    roots: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve(strict=False)
        if resolved == Path(resolved.anchor) or resolved in roots:
            continue
        roots.append(resolved)
    return tuple(roots)


def groundtruth_access_audit(
    events_path: Path,
    workspace: Path,
    *,
    case_id: str = "",
    failing_tests: tuple[str, ...] = (),
    guard_log_path: Path | None = None,
    sensitive_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Record local ground-truth access or exact online-answer lookup attempts.

    The Codex filesystem profile and MCP root guard are the enforcement
    boundaries. Confirmed pre-tool denials are recorded separately; observed
    execution or unverified access still invalidates the run.
    """
    root = workspace.resolve()
    protected_roots = sensitive_roots or (root.parent,)
    lookup_markers = benchmark_lookup_markers(case_id, failing_tests)
    violations: list[dict[str, str]] = []
    blocked_attempts: list[dict[str, str]] = []
    # This runner-owned log is written only by the PreToolUse denial handler.
    # Legacy records predate the explicit decision field, but have the same
    # provenance. Never use a denial to suppress a separate execution event.
    if guard_log_path is not None and guard_log_path.is_file():
        for raw_line in guard_log_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                record = None
            if not isinstance(record, dict):
                violations.append({"kind": "lookup_guard_log_invalid", "value": raw_line})
                continue
            entry = {
                key: str(value) for key, value in record.items()
                if key in {"kind", "marker", "value"}
            }
            if (
                record.get("kind") in {
                    "sensitive_path_outside_workspace",
                    "benchmark_identifier_in_network_lookup",
                }
                and isinstance(record.get("value"), str)
                and record["value"].strip()
                and record.get("decision", "deny") == "deny"
            ):
                blocked_attempts.append({**entry, "decision": "deny", "source": "pre_tool_hook"})
            else:
                violations.append({"kind": "lookup_guard_log_invalid", "value": raw_line})
    if not events_path.is_file():
        return {
            "passed": False,
            "policy": "blocked-attempts-allowed-v1",
            "blocked_attempts": blocked_attempts,
            "violations": [
                *violations,
                {"kind": "missing_events", "value": ""},
            ],
        }

    for raw_line in events_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in {
            "item.started",
            "item.completed",
        }:
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue

        lookup_violation = event_lookup_violation(item, lookup_markers)
        if lookup_violation is not None:
            if lookup_violation not in violations:
                violations.append(lookup_violation)

        if event.get("type") != "item.completed":
            continue

        if item.get("type") == "mcp_tool_call" and item.get("server") == "contextsniper":
            arguments = item.get("arguments")
            if not isinstance(arguments, dict):
                continue
            candidates: list[tuple[str, str]] = []
            if item.get("tool") == "search_code":
                candidates.append(("path", str(arguments.get("path") or "")))
            elif item.get("tool") == "edit_file":
                candidates.extend(
                    [
                        ("workspace_root", str(arguments.get("workspace_root") or "")),
                        ("file_path", str(arguments.get("file_path") or "")),
                    ]
                )
            for field, raw_path in candidates:
                value = raw_path.strip()
                if not value or value in {
                    "$WORK_DIR",
                    "${WORK_DIR}",
                    "$PWD",
                    "${PWD}",
                    "$CONTEXTSNIPER_WORKSPACE_ROOT",
                    "${CONTEXTSNIPER_WORKSPACE_ROOT}",
                }:
                    continue
                candidate = Path(os.path.expandvars(value)).expanduser()
                candidate = candidate if candidate.is_absolute() else root / candidate
                if not _is_within(candidate, root) and path_value_may_exist(
                    value, root
                ):
                    violations.append(
                        {
                            "kind": (
                                f"contextsniper_{item.get('tool')}_"
                                f"{field}_outside_workspace"
                            ),
                            "value": value,
                        }
                    )

        if item.get("type") == "command_execution":
            command = str(item.get("command") or "")
            path_violation = command_sensitive_path_violation(
                command, root, protected_roots, require_existing=True
            )
            if path_violation is not None and path_violation not in violations:
                violations.append(
                    {
                        key: str(value)
                        for key, value in path_violation.items()
                        if key in {"kind", "value"}
                    }
                )

    return {
        "passed": not violations,
        "policy": "blocked-attempts-allowed-v1",
        "blocked_attempts": blocked_attempts,
        "violations": violations,
    }


def _copy_inputs(case: CaseInputs, paths: RunPaths) -> Path:
    # Prepared repositories may retain generated navigation artifacts from a
    # different benchmark run. They are neither source input nor ground truth,
    # and copying them would both dirty Git and leak prior-run context.
    shutil.copytree(
        case.project,
        paths.workspace,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".debugging-framework",
            ".codegraph",
            ".codex",
            "codegraph.json",
        ),
    )
    runtime_config = {
        **case.raw_config,
        "workspace": {
            **(
                case.raw_config.get("workspace")
                if isinstance(case.raw_config.get("workspace"), dict)
                else {}
            ),
            # The adapter always evaluates its own per-run copy, so temporary
            # Git initialization is safe even when a prepared source export
            # does not include repository metadata (the SWE bundle layout).
            "disposable": True,
            "initialize_git_if_missing": True,
        },
    }
    config_path = paths.input_dir / f"{case.case_id}.debugging-framework.json"
    failure_path = paths.input_dir / f"{case.case_id}.failure.log"
    _atomic_json(config_path, runtime_config)
    shutil.copy2(case.failure_log, failure_path)
    return config_path


def _plan(
    args: argparse.Namespace,
    case: CaseInputs,
    plain_env: dict[str, str],
    agent_config: RepairAgentConfig,
) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "project": str(case.project),
        "config": str(case.config),
        "failure_log": str(case.failure_log),
        "failing_tests": list(case.failing_tests),
        "environment": {
            "mode": case.environment_mode,
            "runtime": case.environment_runtime,
            "image": case.environment_image,
        },
        "contextsniper_mode": "plain-search-edit-no-filter",
        "attempts": 1,
        "agent_harness_profile": (AGENT_HARNESS_PROFILE if agent_config.agent == "codex" else "contextsniper-openhands-v1"),
        "agent": agent_config.agent,
        "model_provider": agent_config.model_provider,
        "model": agent_config.model,
        "reasoning_effort": agent_config.reasoning_effort,
        "model_catalog": (
            str(OPENROUTER_MODEL_CATALOG.relative_to(HERE))
            if agent_config.agent == "codex"
            and agent_config.model_provider == OPENROUTER_MODEL_PROVIDER
            and agent_config.model == DEFAULT_OPENROUTER_MODEL
            else None
        ),
        "validation_implementation": "bundled",
        "groundtruth_isolation": {
            "shell_filesystem": ("minimal-system-plus-copied-workspace" if agent_config.agent == "codex"
                                 else "read-only-case-image-plus-copied-workspace"),
            "contextsniper_workspace_root_enforced": True,
            "exact_benchmark_network_lookup": "pre-tool-block-and-event-audit",
            "general_web_research_allowed": True,
            "event_audit": "fail-closed",
        },
        "contextsniper_settings": public_plain_settings(plain_env),
        "output_root": str(args.output_root.expanduser().resolve()),
    }


def execute(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    agent_config = resolve_agent_config(args)
    case = resolve_case(args.dataset_root, args.case_id, args.inputs_dir)
    read_code_policy()
    plain_env = load_plain_contextsniper_settings(os.environ)
    plan = _plan(args, case, plain_env, agent_config)
    if args.dry_run:
        return 0, {"status": "dry-run", **plan}

    if agent_config.agent == "openhands" and (case.environment_mode != "image" or not case.environment_image):
        raise RunnerError("OpenHands currently requires a prepared image-mode case")
    codex_bin, contextsniper_python = preflight(args, plain_env, agent_config)
    paths = make_run_paths(args.output_root, case.case_id)
    _progress(f"{case.case_id}: preparing workspace and validating the baseline")
    runtime_config = _copy_inputs(case, paths)
    failure_output = case.failure_log.read_text(encoding="utf-8", errors="replace")
    if not failure_output.strip():
        raise RunnerError(f"Failure log is empty: {case.failure_log}")
    contextsniper_port, agfs_port = find_port_pair(args.contextsniper_port, args.agfs_port)
    run_id = f"{case.case_id}-p{os.getpid()}"
    env = build_contextsniper_env(
        plain_env,
        workspace=paths.workspace,
        runtime_dir=paths.runtime,
        python_bin=contextsniper_python,
        contextsniper_port=contextsniper_port,
        agfs_port=agfs_port,
        run_id=run_id,
    )
    lookup_markers = benchmark_lookup_markers(case.case_id, case.failing_tests)
    if not lookup_markers:
        raise RunnerError("Could not derive benchmark lookup guard identifiers")
    sensitive_roots = benchmark_sensitive_roots(case)
    env[LOOKUP_MARKERS_ENV] = json.dumps(lookup_markers, separators=(",", ":"))
    env[LOOKUP_GUARD_LOG_ENV] = str(paths.logs / "benchmark-lookup-guard.jsonl")
    env[LOOKUP_SENSITIVE_ROOTS_ENV] = json.dumps(
        [str(path) for path in sensitive_roots], separators=(",", ":")
    )
    base_prompt = build_prompt(
        case,
        failure_output,
        render_code_policy(env=env, paths=paths, python_bin=contextsniper_python),
    )
    paths.prompt.write_text(base_prompt, encoding="utf-8")
    metadata = {
        **plan,
        "config_schema_version": case.raw_config.get("schema_version"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "input_sha256": {
            "config": _sha256(case.config),
            "failure_log": _sha256(case.failure_log),
        },
        "ports": {"contextsniper": contextsniper_port, "agfs": agfs_port},
        "artifacts": {
            "prompt": paths.prompt.name,
            "events": paths.events.name,
            "stderr": str(paths.stderr.relative_to(paths.root)),
            "response": paths.response.name,
            "patch": paths.patch.name,
        },
    }
    _atomic_json(paths.root / "run.json", metadata)

    project = Project(
        path=paths.workspace,
        project_id=case.case_id,
        config_path=runtime_config,
    )
    validator = ProjectValidator(
        command_timeout=args.command_timeout,
        jobs=args.jobs,
        environment_backend=case.environment_mode,
        environment_runtime=case.environment_runtime,
        environment_image=case.environment_image,
    )
    result: dict[str, Any]
    service_started = False
    try:
        workspace_parent = paths.validation / "workspace-state"
        with ProjectWorkspace(project, workspace_parent) as workspace:
            baseline = validator.external_baseline(
                project,
                paths.validation / "baseline",
                failing_tests=case.failing_tests,
                failure_output=failure_output,
            )
            _atomic_json(paths.validation / "baseline.json", baseline)
            if baseline.get("status") != "failing" or baseline.get("validation_error"):
                raise RunnerError(
                    "The bundled validator rejected the supplied failure baseline: "
                    + str(baseline.get("validation_error") or baseline.get("status"))
                )

            _progress(f"{case.case_id}: starting ContextSniper retrieval services")
            start_contextsniper(env, paths, contextsniper_python)
            service_started = True
            base_url = env["CONTEXTSNIPER_URL"]
            try:
                _api_json(base_url, "/api/v1/token_stats", method="POST", body={"reset": True})
            except Exception as exc:
                (paths.logs / "contextsniper-token-reset.warning.log").write_text(
                    str(exc) + "\n", encoding="utf-8"
                )
            additional_context = compose_prompt_context(
                base_prompt,
                env=env,
                paths=paths,
                python_bin=contextsniper_python,
            )
            prompt = base_prompt
            if additional_context:
                prompt += "\n\n" + additional_context + "\n"
            paths.prompt.write_text(prompt, encoding="utf-8")

            _progress(
                f"{case.case_id}: running agent={agent_config.agent} "
                f"model={agent_config.model}"
            )
            if agent_config.agent == "openhands":
                from openhands_adapter import run_openhands
                codex_run = run_openhands(
                    python_bin=codex_bin, args=args, agent_config=agent_config,
                    case=case, paths=paths, prompt=prompt, env=env,
                )
            else:
                command = build_codex_command(
                    codex_bin=codex_bin,
                    workspace=paths.workspace,
                    response_path=paths.response,
                    model=agent_config.model,
                    reasoning_effort=agent_config.reasoning_effort,
                    model_provider=agent_config.model_provider,
                    hook_python=contextsniper_python,
                )
                codex_run = run_codex(
                    command,
                    prompt=prompt,
                    workspace=paths.workspace,
                    env=env,
                    events_path=paths.events,
                    stderr_path=paths.stderr,
                    timeout=args.codex_timeout,
                    lookup_markers=tuple(lookup_markers),
                )
            contextsniper_usage: dict[str, Any] = {}
            try:
                contextsniper_usage = _api_json(base_url, "/api/v1/token_stats", timeout=10)
            except Exception as exc:
                contextsniper_usage = {"error": str(exc)}

            diff = workspace.canonical_diff()
            paths.patch.write_text(diff, encoding="utf-8")
            repair_usage = event_usage(paths.events)
            mcp_usage = contextsniper_mcp_usage(paths.events)
            leakage_audit = groundtruth_access_audit(
                paths.events,
                paths.workspace,
                case_id=case.case_id,
                failing_tests=case.failing_tests,
                guard_log_path=paths.logs / "benchmark-lookup-guard.jsonl",
                sensitive_roots=sensitive_roots,
            )
            common = {
                "project": str(case.project),
                "evaluated_workspace": str(paths.workspace),
                "case_id": case.case_id,
                "agent_harness_profile": (AGENT_HARNESS_PROFILE if agent_config.agent == "codex" else "contextsniper-openhands-v1"),
                "agent": agent_config.agent,
                "model_provider": agent_config.model_provider,
                "model": agent_config.model,
                "reasoning_effort": agent_config.reasoning_effort,
                "attempts": 1,
                "codex_returncode": codex_run.returncode,
                "codex_timed_out": codex_run.timed_out,
                "codex_elapsed_seconds": codex_run.elapsed_seconds,
                "codex_compatibility_error": codex_run.compatibility_error,
                "codex_policy_violation": codex_run.policy_violation,
                "agent_returncode": codex_run.returncode,
                "agent_timed_out": codex_run.timed_out,
                "agent_elapsed_seconds": codex_run.elapsed_seconds,
                "output_patch": str(paths.patch),
                "agent_usage": {
                    "retrieval": {},
                    "repair": repair_usage,
                    "complete_for_recorded_workers": bool(repair_usage),
                    "total": dict(repair_usage),
                },
                "contextsniper_usage": contextsniper_usage,
                "contextsniper_mcp_usage": mcp_usage,
                "groundtruth_access_audit": leakage_audit,
                "baseline": baseline,
                "input_project_untouched": True,
                "contextsniper_mode": "plain-search-edit-no-filter",
            }
            mcp_validation_error = ""
            if not leakage_audit["passed"]:
                if any(
                    violation.get("kind")
                    == "benchmark_identifier_in_network_lookup"
                    for violation in leakage_audit["violations"]
                ):
                    mcp_validation_error = "benchmark_answer_lookup_attempt_detected"
                else:
                    mcp_validation_error = "groundtruth_access_attempt_detected"
            elif codex_run.compatibility_error:
                mcp_validation_error = "codex_tool_schema_mismatch"
            elif codex_run.timed_out:
                mcp_validation_error = "codex_execution_timed_out"
            elif codex_run.returncode != 0:
                mcp_validation_error = "codex_execution_failed"
            elif not mcp_usage["search_succeeded"]:
                mcp_validation_error = "contextsniper_search_code_not_successful"
            elif diff.strip() and not mcp_usage["edit_succeeded"]:
                mcp_validation_error = "contextsniper_edit_file_not_successful"

            if mcp_validation_error:
                result = {
                    **common,
                    "status": "invalid",
                    "post_validation_status": "not-run",
                    "validation_error": mcp_validation_error,
                    "patch_validation_passed": False,
                    "initial_failed_test_ids": list(case.failing_tests),
                    "post_failed_test_ids": [],
                    "fixed_test_ids": [],
                    "regression_test_ids": [],
                    "classification_basis_valid": False,
                }
            elif not diff.strip():
                result = {
                    **common,
                    "status": "invalid",
                    "post_validation_status": "not-run",
                    "validation_error": "codex_did_not_produce_a_patch",
                    "patch_validation_passed": False,
                    "initial_failed_test_ids": list(case.failing_tests),
                    "post_failed_test_ids": [],
                    "fixed_test_ids": [],
                    "regression_test_ids": [],
                    "classification_basis_valid": False,
                }
            else:
                _progress(f"{case.case_id}: validating the generated patch")
                patch_paths = workspace.unified_diff_paths(diff)
                snapshot_hashes = workspace.snapshot_sha256s(patch_paths)
                # The bundled validator compares the clean checkout with the
                # captured snapshot hashes before it reapplies the diff. This
                # mirrors Debugging-Framework's pipeline and prevents the
                # agent-edited working tree from being mistaken for external
                # input drift.
                workspace.reset_to_snapshot()
                validation = validator.validate_diff(
                    project=project,
                    diff=diff,
                    patch_paths=patch_paths,
                    artifact_dir=paths.validation / "patched",
                    expected_sha256s=snapshot_hashes,
                    failing_tests=case.failing_tests,
                    expected_plan_digest=str(baseline.get("plan_digest") or ""),
                    expected_environment_digest=str(baseline.get("environment_digest") or ""),
                    expected_image_digest=str(baseline.get("provisioned_image_digest") or ""),
                    reusable_workspace=workspace,
                )
                classified = classify_validation_result(baseline, validation)
                result = {
                    **classified,
                    **common,
                    "status": classified.get("status", "invalid"),
                    "patch_validation_passed": classified.get("status") == "plausible",
                    "patch_paths": patch_paths,
                }
        result["input_project_restored"] = True
        _atomic_json(paths.result, result)
        return (0 if result.get("status") == "plausible" else 1), result
    except Exception as exc:
        result = {
            "status": "error",
            "stage": "run",
            "error": f"{type(exc).__name__}: {exc}",
            "case_id": case.case_id,
            "agent": agent_config.agent,
            "model_provider": agent_config.model_provider,
            "model": agent_config.model,
            "reasoning_effort": agent_config.reasoning_effort,
            "patch_validation_passed": False,
            "input_project_untouched": True,
        }
        _atomic_json(paths.result, result)
        return 2, result
    finally:
        if service_started:
            stop_contextsniper(env, paths, contextsniper_python)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        code, result = execute(args)
    except (OSError, RunnerError, subprocess.SubprocessError) as exc:
        result = {
            "status": "error",
            "stage": "preflight",
            "error": f"{type(exc).__name__}: {exc}",
        }
        code = 2
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
