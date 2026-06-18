#!/usr/bin/env bash
set -euo pipefail

# IMPORTANT: OpenClaw must run from the generated SWE workspace, not the
# ContextSniper repo root. If you edit this runner or invoke OpenClaw
# manually, cd to "$WORK_DIR" before `openclaw agent`/`openclaw chat`;
# otherwise native read/exec tools can resolve paths against the wrong project.

_SCRIPTS_MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
CACHE_DIR="${SWE_CACHE_DIR:-${CONTEXTSNIPER_SWE_CACHE_DIR:-$CONTEXTSNIPER_CACHE_HOME/swe-pro/openclaw/plain/cache}}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CONTEXTSNIPER_DIR="${CONTEXTSNIPER_DIR:-$PROJECT_ROOT}"
CLAUDE_PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$CONTEXTSNIPER_DIR/claude-plugin}"
export OPENCLAW_MODEL="${OPENCLAW_MODEL:-deepseek/deepseek-v4-flash}"
PY_BIN="${PY_BIN:-python3}"

PARENT_ENV_SH="$PROJECT_ROOT/source_contextsniper_env.sh"
if [ -f "$PARENT_ENV_SH" ]; then
  # shellcheck disable=SC1090
  . "$PARENT_ENV_SH" >/dev/null
fi

if [ -f "$PROJECT_ROOT/setup_env.sh" ]; then
  # shellcheck disable=SC1091
  . "$PROJECT_ROOT/setup_env.sh" >/dev/null
  PY_BIN="${PY_BIN:-python3}"
  CLAUDE_PLUGIN_DIR="${CONTEXTSNIPER_CLAUDE_PLUGIN_DIR:-$CLAUDE_PLUGIN_DIR}"
fi

# Priority: first CLI arg > env > default.
# Default to a qutebrowser SWE-bench Pro task for Python-centric debugging.
export SWE_PRO_INSTANCE_ID="${1:-${SWE_PRO_INSTANCE_ID:-instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c}}"
export SWE_USE_DERIVED_LOCAL_ENV="${SWE_USE_DERIVED_LOCAL_ENV:-1}"
export SWE_VALIDATION_FORCE_LOCAL="${SWE_VALIDATION_FORCE_LOCAL:-1}"
export SWE_SKIP_VALIDATION="${SWE_SKIP_VALIDATION:-1}"

# Parallel-run isolation knobs.
# RUN_IDX lets callers run multiple jobs concurrently with deterministic offsets.
RUN_IDX="${RUN_IDX:-0}"
export RUN_IDX

REPO_BASE="${REPO_BASE:-$CACHE_DIR/repo}"
mkdir -p "$REPO_BASE"

ensure_python_runtime() {
  if "$PY_BIN" - <<'PY' >/dev/null 2>&1
import flask
import mcp
PY
  then
    return 0
  fi

  if ! command -v uv >/dev/null 2>&1; then
    echo "Missing Python deps (flask/mcp), and uv is not installed." >&2
    echo "Install uv or pre-install deps, e.g. pip install -e '.[mcp]'." >&2
    exit 1
  fi

    echo "[setup] Installing runtime deps with uv sync --extra mcp --extra swe ..." >&2
  (cd "$CONTEXTSNIPER_DIR" && uv sync --extra mcp --extra swe)
  PY_BIN="$CONTEXTSNIPER_DIR/.venv/bin/python"

  if ! "$PY_BIN" - <<'PY' >/dev/null 2>&1
import flask
import mcp
PY
  then
    echo "Dependency bootstrap failed. Please inspect uv sync output above." >&2
    exit 1
  fi
}

ensure_python_runtime

# Resolve SWE-bench Pro row and generate prompt text.
_swe_prompt_exports="$(
  REPO_BASE="$REPO_BASE" \
  SWE_PROMPT_KIND="openclaw_plain" \
  "$PY_BIN" "$_SCRIPTS_MCP_DIR/../../resolve_swe_pro_instance.py"
)" || {
  echo "[setup] Failed to resolve SWE-bench instance metadata for ${SWE_PRO_INSTANCE_ID:-<unset>}." >&2
  exit 1
}
eval "$_swe_prompt_exports"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-${CONTEXTSNIPER_SWE_OUTPUT_ROOT:-$_SCRIPTS_MCP_DIR/output_logs}}"
EXPERIMENT_DIR="$OUTPUT_ROOT/${STAMP}-swe-pro-openclaw-plain-r${RUN_IDX}-p$$"
LOGS_DIR="$EXPERIMENT_DIR/logs"
WORK_DIR="$EXPERIMENT_DIR/workspace"
mkdir -p "$LOGS_DIR" "$EXPERIMENT_DIR"
ln -sfn "$EXPERIMENT_DIR" "$OUTPUT_ROOT/latest"

CANON_ROOT="$REPO_BASE/$SWE_INSTANCE_ID"
CANON_DIR="$CANON_ROOT/src"
LOCKS_DIR="$CACHE_DIR/locks"
mkdir -p "$CANON_ROOT" "$LOCKS_DIR"
CANON_LOCK="$LOCKS_DIR/${SWE_INSTANCE_ID}.canon.lock"

{
  flock 9

  canon_repo_ok() {
    [ -d "$1/.git" ] || return 1
    git -C "$1" rev-parse --verify -q HEAD >/dev/null 2>&1 || return 1
    git -C "$1" remote get-url origin >/dev/null 2>&1 || return 1
  }

  if [ -d "$CANON_DIR" ] && ! canon_repo_ok "$CANON_DIR"; then
    echo "[setup] Removing incomplete canonical repo cache: $CANON_DIR" >&2
    rm -rf "$CANON_DIR"
  fi

  if [ ! -d "$CANON_DIR/.git" ]; then
    _canon_tmp="${CANON_DIR}.tmp.$$"
    rm -rf "$_canon_tmp"
    echo "Cloning https://github.com/${SWE_REPO}.git ..." >&2
    git clone "https://github.com/${SWE_REPO}.git" "$_canon_tmp"
    if ! canon_repo_ok "$_canon_tmp"; then
      echo "[setup] Canonical repo clone did not produce a valid HEAD: $_canon_tmp" >&2
      rm -rf "$_canon_tmp"
      exit 1
    fi
    mv "$_canon_tmp" "$CANON_DIR"
  fi

  if [ "${SWE_SKIP_CANON_FETCH:-0}" != "1" ]; then
    git -C "$CANON_DIR" fetch --all --prune
  fi
  if ! git -C "$CANON_DIR" rev-parse --verify -q "$SWE_BASE_COMMIT^{commit}" >/dev/null 2>&1; then
    if [ "${SWE_SKIP_CANON_FETCH:-0}" = "1" ]; then
      echo "[setup] Missing base commit $SWE_BASE_COMMIT and SWE_SKIP_CANON_FETCH=1 prevents fetching." >&2
      exit 1
    fi
    git -C "$CANON_DIR" fetch origin
  fi
  git -C "$CANON_DIR" checkout -f "$SWE_BASE_COMMIT" --
  git -C "$CANON_DIR" clean -fdx

  rm -rf "$WORK_DIR"
  git clone --quiet "$CANON_DIR" "$WORK_DIR"
} 9>"$CANON_LOCK"
cp -a "$SWE_INSTANCE_JSON" "$EXPERIMENT_DIR/instance.json"
cp -a "$SWE_PROMPT_FILE" "$WORK_DIR/TASK.md"
cat >>"$WORK_DIR/TASK.md" <<EOF2

## Workspace Paths
- OpenClaw is launched against the SWE task checkout: \`$WORK_DIR\`.
- Edits for this SWE task should be made in the task checkout: \`$WORK_DIR\`.
EOF2

OPENCLAW_STDOUT="$LOGS_DIR/openclaw-stdout.log"
OPENCLAW_JSON="$LOGS_DIR/openclaw-agent.json"

if [ "$SWE_USE_DERIVED_LOCAL_ENV" = "1" ]; then
  echo "[setup] Deriving local SWE-bench env ..." >&2
  LOCAL_ENV_PREP_LOG="$LOGS_DIR/swe-local-env-prepare.log"
  LOCAL_ENV_EXPORTS_FILE="$LOGS_DIR/swe-local-env-exports.sh"
  if ! "$PY_BIN" "$_SCRIPTS_MCP_DIR/prepare_swe_local_env.py" "$SWE_INSTANCE_JSON" "$WORK_DIR" "$EXPERIMENT_DIR" \
      > "$LOCAL_ENV_EXPORTS_FILE" 2>> "$LOCAL_ENV_PREP_LOG"; then
    cat "$LOCAL_ENV_EXPORTS_FILE" >> "$LOCAL_ENV_PREP_LOG" 2>/dev/null || true
    echo "[setup] Failed to derive local SWE-bench env; see $LOCAL_ENV_PREP_LOG" >&2
    exit 1
  fi
  cat "$LOCAL_ENV_EXPORTS_FILE" >> "$LOCAL_ENV_PREP_LOG"
  _local_env_exports="$(grep '^export ' "$LOCAL_ENV_EXPORTS_FILE" || true)"
  if [ -z "$_local_env_exports" ]; then
    echo "[setup] Local SWE-bench env derivation produced no exports; see $LOCAL_ENV_PREP_LOG" >&2
    exit 1
  fi
  eval "$_local_env_exports"
  echo "[setup] Refreshing editable install for current SWE workspace ..." >&2
  if ! (
    cd "$WORK_DIR"
    "$SWE_TASK_ENV_HELPER" --reinstall python - <<'PY'
import sys
print(sys.executable)
PY
  ) >> "$LOCAL_ENV_PREP_LOG" 2>&1; then
    echo "[setup] Failed to refresh current workspace install; see $LOCAL_ENV_PREP_LOG" >&2
    exit 1
  fi
  cat >>"$WORK_DIR/TASK.md" <<EOF2

	## Local SWE-bench Environment
	- This workspace has a task-specific environment derived from the official SWE-bench \`TestSpec\`.
	- Use \`$SWE_TASK_ENV_HELPER\` for every reproduction and verification command.
	- Do not use raw \`python\`, raw \`python -m pytest\`, or raw \`pytest\`; those may hit the host environment.
	- Use:
	  - \`./RUN_IN_SWE_LOCAL_ENV.sh pytest -q <target>\`
	  - \`./RUN_IN_SWE_LOCAL_ENV.sh python -m pytest -q <target>\`
	- For inline Python snippets, prefer:
	  - \`./RUN_IN_SWE_LOCAL_ENV.sh --stdin-python <<'PY'\`
	  - \`...\`
	  - \`PY\`
	- If editable-install state needs refreshing after a structural change, use:
	  - \`./RUN_IN_SWE_LOCAL_ENV.sh --reinstall pytest -q <target>\`
	- Avoid shared temp files like \`/tmp/build.log\`; keep per-run logs under the workspace or \`logs/\`.
	- Local env prefix: \`$SWE_TASK_ENV_PREFIX\`
	- Local env create log: \`$SWE_TASK_ENV_CREATE_LOG\`
- Local env command log: \`$SWE_TASK_ENV_COMMAND_LOG\`
EOF2
fi

WORKSPACE_GIT_MOVED=0
OPENCLAW_PLUGINS_BACKUP="$LOGS_DIR/openclaw-plugins-config.before.json"
OPENCLAW_PLUGIN_PATCH="$LOGS_DIR/openclaw-disable-contextsniper-plugin.patch.json"
disable_generated_workspace_git() {
  [ "${WORKSPACE_GIT_MOVED:-0}" = "0" ] || return 0
  [ -n "${WORK_DIR:-}" ] || return 0
  [ -n "${EXPERIMENT_DIR:-}" ] || return 0
  if [ -d "$WORK_DIR/.git" ]; then
    rm -rf "$EXPERIMENT_DIR/workspace.git"
    mv "$WORK_DIR/.git" "$EXPERIMENT_DIR/workspace.git"
    cat > "$WORK_DIR/.git-disabled.txt" <<EOF2
Git metadata was moved from workspace/.git to ../workspace.git after this run.
This keeps the generated SWE checkout analyzable while preventing editors from
showing output_logs/workspace as a nested Git repository.
EOF2
    WORKSPACE_GIT_MOVED=1
  fi
}
cleanup_generated_workspace() {
  disable_generated_workspace_git
  restore_openclaw_plugin_config
}

json_string() {
  "$PY_BIN" - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}

restore_openclaw_plugin_config() {
  [ -s "$OPENCLAW_PLUGINS_BACKUP" ] || return 0
  "$PY_BIN" - "$OPENCLAW_PLUGINS_BACKUP" "$OPENCLAW_PLUGIN_PATCH" <<'PY'
import json
import sys
from pathlib import Path

plugins = json.loads(Path(sys.argv[1]).read_text())
if "allow" not in plugins:
    plugins["allow"] = None
Path(sys.argv[2]).write_text(json.dumps({"plugins": plugins}, indent=2) + "\n")
PY
  openclaw config patch --file "$OPENCLAW_PLUGIN_PATCH" \
    > "$LOGS_DIR/openclaw-config-restore.log" 2>&1 || true
}

trap cleanup_generated_workspace EXIT

OPENCLAW_AGENT_ID="${OPENCLAW_AGENT_ID:-swe-openclaw-plain-r${RUN_IDX}-p$$}"
OPENCLAW_SESSION_ID="${OPENCLAW_SESSION_ID:-swe-openclaw-plain-r${RUN_IDX}-p$$}"
OPENCLAW_TIMEOUT="${OPENCLAW_TIMEOUT:-900}"

echo "[setup] Disabling ContextSniper plugin for plain OpenClaw SWE run" >&2
openclaw config get plugins > "$OPENCLAW_PLUGINS_BACKUP" 2> "$LOGS_DIR/openclaw-config-get-plugins.stderr" || echo '{}' > "$OPENCLAW_PLUGINS_BACKUP"
openclaw config set plugins.entries.contextsniper.enabled false --strict-json \
  > "$LOGS_DIR/openclaw-config-disable-contextsniper.log" 2>&1 || true
openclaw config set plugins.allow null --strict-json \
  > "$LOGS_DIR/openclaw-config-allow-default-tools.log" 2>&1 || true

openclaw gateway restart \
  > "$LOGS_DIR/openclaw-gateway-restart.log" 2>&1 || true

echo "[setup] Creating OpenClaw agent $OPENCLAW_AGENT_ID for $WORK_DIR" >&2
openclaw agents add "$OPENCLAW_AGENT_ID" \
  --workspace "$WORK_DIR" \
  --model "$OPENCLAW_MODEL" \
  --non-interactive \
  --json > "$LOGS_DIR/openclaw-agent-add.json" 2> "$LOGS_DIR/openclaw-agent-add.stderr" || true

set +e
(
  cd "$WORK_DIR"
  export PYTHONPATH="$WORK_DIR${PYTHONPATH:+:$PYTHONPATH}"
  openclaw agent --local \
    --agent "$OPENCLAW_AGENT_ID" \
    --session-id "$OPENCLAW_SESSION_ID" \
    --model "$OPENCLAW_MODEL" \
    --timeout "$OPENCLAW_TIMEOUT" \
    --message "$(cat "$WORK_DIR/TASK.md")" \
    --json
) 2>&1 | tee "$OPENCLAW_STDOUT" "$OPENCLAW_JSON"
RC=${PIPESTATUS[0]}
set -e

SESSION_JSONL="${HOME}/.openclaw/agents/${OPENCLAW_AGENT_ID}/sessions/${OPENCLAW_SESSION_ID}.jsonl"
SESSION_TRAJECTORY_JSONL="${HOME}/.openclaw/agents/${OPENCLAW_AGENT_ID}/sessions/${OPENCLAW_SESSION_ID}.trajectory.jsonl"
SESSION_TRAJECTORY_PATH_JSON="${HOME}/.openclaw/agents/${OPENCLAW_AGENT_ID}/sessions/${OPENCLAW_SESSION_ID}.trajectory-path.json"
for _jsonl_retry in 1 2 3 4 5; do
  if [ -f "$SESSION_JSONL" ]; then
    break
  fi
  sleep 1
done
if [ -n "$SESSION_JSONL" ] && [ -f "$SESSION_JSONL" ]; then
  cp -f "$SESSION_JSONL" "$LOGS_DIR/"
  [ -f "$SESSION_TRAJECTORY_JSONL" ] && cp -f "$SESSION_TRAJECTORY_JSONL" "$LOGS_DIR/"
  [ -f "$SESSION_TRAJECTORY_PATH_JSON" ] && cp -f "$SESSION_TRAJECTORY_PATH_JSON" "$LOGS_DIR/"
  "$PY_BIN" "$_SCRIPTS_MCP_DIR/render_jsonl_turns.py" "$SESSION_JSONL" --stdout-log "$OPENCLAW_STDOUT" > "$EXPERIMENT_DIR/latest_session_render.txt" || true
	  "$PY_BIN" - "$SESSION_JSONL" "$EXPERIMENT_DIR/openclaw_tool_summary.json" <<'PY' || true
import json
import sys
from pathlib import Path

session = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
tool_calls = []
tool_results = []
for idx, raw in enumerate(session.read_text(errors="replace").splitlines(), 1):
    try:
        obj = json.loads(raw)
    except Exception:
        continue
    if obj.get("type") != "message":
        continue
    msg = obj.get("message") or {}
    content = msg.get("content") or []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "toolCall":
                tool_calls.append(
                    {
                        "line": idx,
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )
    if msg.get("role") == "toolResult":
        tool_results.append(
            {
                "line": idx,
                "name": msg.get("toolName"),
                "isError": msg.get("isError"),
            }
        )

summary = {
    "session_jsonl": str(session),
    "tool_calls": tool_calls,
    "tool_results": tool_results,
    "tool_names": sorted({str(t.get("name")) for t in tool_calls if t.get("name")}),
    "has_contextsniper_search_code": any(t.get("name") == "contextsniper_search_code" for t in tool_calls),
    "has_contextsniper_edit_file": any(t.get("name") == "contextsniper_edit_file" for t in tool_calls),
    "has_successful_contextsniper_edit_file": any(
        t.get("name") == "contextsniper_edit_file" and not t.get("isError") for t in tool_results
    ),
    "used_builtin_edit": any(t.get("name") == "edit" for t in tool_calls),
    "edited_test_file": any(
        t.get("name") in {"contextsniper_edit_file", "edit", "write", "file_write"}
        and (
            "/test" in str((t.get("arguments") or {}).get("file_path") or (t.get("arguments") or {}).get("path") or "")
            or str((t.get("arguments") or {}).get("file_path") or (t.get("arguments") or {}).get("path") or "").split("/")[-1].startswith("test_")
        )
        for t in tool_calls
    ),
}
summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
else
  echo "[warn] OpenClaw session JSONL not found: $SESSION_JSONL" >&2
fi

VALIDATION_RC=0
if [ "${SWE_SKIP_VALIDATION:-0}" = "1" ]; then
  echo "[validate] Skipped validation for $SWE_INSTANCE_ID (SWE_SKIP_VALIDATION=1)." >&2
  "$PY_BIN" - "$SWE_INSTANCE_ID" "$EXPERIMENT_DIR" <<'PY'
import json
import sys
from pathlib import Path

instance_id, exp_dir = sys.argv[1], Path(sys.argv[2])
payload = {
    "instance_id": instance_id,
    "status": "skipped",
    "validation_mode": "skipped",
    "reason": "SWE_SKIP_VALIDATION=1",
}
(exp_dir / "validation.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
(exp_dir / "validation.md").write_text("# Validation skipped\n\nSWE_SKIP_VALIDATION=1\n", encoding="utf-8")
PY
else
  echo "[validate] Starting attached validation for $SWE_INSTANCE_ID ..." >&2
  set +e
  "$PY_BIN" "$_SCRIPTS_MCP_DIR/validate_swe_run.py" "$SWE_INSTANCE_JSON" "$WORK_DIR" "$EXPERIMENT_DIR" \
    > "$LOGS_DIR/validation-summary.json" 2> "$LOGS_DIR/validation-stderr.log"
  VALIDATION_RC=$?
  set -e
  if [ "$VALIDATION_RC" -ne 0 ]; then
    echo "[validate] Validation failed for $SWE_INSTANCE_ID; see $EXPERIMENT_DIR/validation.md" >&2
  fi
fi

disable_generated_workspace_git

echo "Instance: $SWE_INSTANCE_ID ($SWE_REPO @ $SWE_BASE_COMMIT)"
echo "Experiment dir: $EXPERIMENT_DIR"
echo "Workspace: $WORK_DIR"
echo "Logs: $LOGS_DIR"
echo "OpenClaw agent: ${OPENCLAW_AGENT_ID:-<unset>}"
echo "OpenClaw session: ${OPENCLAW_SESSION_ID:-<unset>}"
echo "OpenClaw session jsonl: ${SESSION_JSONL:-<unset>}"
echo "Latest session render: $EXPERIMENT_DIR/latest_session_render.txt"
echo "OpenClaw tool summary: $EXPERIMENT_DIR/openclaw_tool_summary.json"
echo "Validation markdown: $EXPERIMENT_DIR/validation.md"
echo "Validation json: $EXPERIMENT_DIR/validation.json"
echo "Validation rc: $VALIDATION_RC"
if [ "$RC" -ne 0 ]; then
  exit "$RC"
fi
exit "$VALIDATION_RC"
