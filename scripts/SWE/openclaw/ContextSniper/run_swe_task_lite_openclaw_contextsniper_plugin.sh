#!/usr/bin/env bash
set -euo pipefail

# IMPORTANT: OpenClaw must run from the generated SWE workspace, not the
# ContextSniper repo root. If you edit this runner or invoke OpenClaw
# manually, cd to "$WORK_DIR" before `openclaw agent`/`openclaw chat`;
# otherwise native read/exec tools can resolve paths against the wrong project.

_SCRIPTS_MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
CACHE_DIR="${SWE_CACHE_DIR:-${CONTEXTSNIPER_SWE_CACHE_DIR:-$CONTEXTSNIPER_CACHE_HOME/swe/openclaw/contextsniper/cache}}"
# Loopback must not use HTTP(S) proxy; AGFS and ContextSniper run on localhost.
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="$NO_PROXY"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CONTEXTSNIPER_DIR="${CONTEXTSNIPER_DIR:-$PROJECT_ROOT}"
CLAUDE_PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$CONTEXTSNIPER_DIR/claude-plugin}"
OPENCLAW_PLUGIN_DIR="${OPENCLAW_PLUGIN_DIR:-$CONTEXTSNIPER_DIR/openclaw-plugin}"
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
# Default to a Flask SWE-bench Lite task for faster Python-centric debugging.
export SWE_LITE_INSTANCE_ID="${1:-${SWE_LITE_INSTANCE_ID:-pallets__flask-4045}}"
export CONTEXTSNIPER_SEARCH_LIMIT="${CONTEXTSNIPER_SEARCH_LIMIT:-5}"
export SWE_USE_DERIVED_LOCAL_ENV="${SWE_USE_DERIVED_LOCAL_ENV:-1}"
export SWE_VALIDATION_FORCE_LOCAL="${SWE_VALIDATION_FORCE_LOCAL:-1}"
export SWE_SKIP_VALIDATION="${SWE_SKIP_VALIDATION:-1}"

# Parallel-run isolation knobs.
# RUN_IDX lets callers run multiple jobs concurrently with deterministic offsets.
RUN_IDX="${RUN_IDX:-0}"
export RUN_IDX
CONTEXTSNIPER_BASE_PORT="${CONTEXTSNIPER_BASE_PORT:-8090}"
AGFS_BASE_PORT="${AGFS_BASE_PORT:-1833}"

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

# Resolve SWE-bench Lite row and generate prompt text.
_swe_prompt_exports="$(
  REPO_BASE="$REPO_BASE" \
  SWE_PROMPT_KIND="openclaw_contextsniper" \
  "$PY_BIN" "$_SCRIPTS_MCP_DIR/../../resolve_swe_lite_instance.py"
)" || {
  echo "[setup] Failed to resolve SWE-bench instance metadata for ${SWE_LITE_INSTANCE_ID:-<unset>}." >&2
  exit 1
}
eval "$_swe_prompt_exports"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-${CONTEXTSNIPER_SWE_OUTPUT_ROOT:-$_SCRIPTS_MCP_DIR/output_logs}}"
EXPERIMENT_DIR="$OUTPUT_ROOT/${STAMP}-swe-lite-openclaw-contextsniper-r${RUN_IDX}-p$$"
LOGS_DIR="$EXPERIMENT_DIR/logs"
WORK_DIR="$EXPERIMENT_DIR/workspace"
mkdir -p "$LOGS_DIR" "$EXPERIMENT_DIR"
ln -sfn "$EXPERIMENT_DIR" "$OUTPUT_ROOT/latest"
export CONTEXTSNIPER_RUNTIME_DIR="${CONTEXTSNIPER_RUNTIME_DIR:-$EXPERIMENT_DIR}"

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
CODE_POLICY_PROMPT="$OPENCLAW_PLUGIN_DIR/prompts/code_policy_injection.txt"
if [ "${CONTEXTSNIPER_APPEND_CODE_POLICY_TO_TASK:-1}" = "1" ] && [ -f "$CODE_POLICY_PROMPT" ]; then
  CODE_POLICY_RENDERED="$LOGS_DIR/openclaw-code-policy-rendered.txt"
  if OPENCLAW_PLUGIN_DIR="$OPENCLAW_PLUGIN_DIR" node --experimental-strip-types --input-type=module > "$CODE_POLICY_RENDERED" <<'JS'
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const pluginDir = process.env.OPENCLAW_PLUGIN_DIR;
if (!pluginDir) throw new Error("OPENCLAW_PLUGIN_DIR is not set");
const promptPath = path.join(pluginDir, "prompts", "code_policy_injection.txt");
const modulePath = pathToFileURL(path.join(pluginDir, "src", "policy.ts")).href;
const { renderPolicyPrompt } = await import(modulePath);
process.stdout.write(`${renderPolicyPrompt(fs.readFileSync(promptPath, "utf8"))}\n`);
JS
  then
    CODE_POLICY_PROMPT="$CODE_POLICY_RENDERED"
  else
    echo "[setup] Failed to render OpenClaw ContextSniper code policy; appending raw prompt." >&2
  fi
  {
    printf '\n'
    cat "$CODE_POLICY_PROMPT"
  } >> "$WORK_DIR/TASK.md"
fi

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
fi

if ! [[ "$RUN_IDX" =~ ^[0-9]+$ ]]; then
  echo "RUN_IDX must be a non-negative integer, got: $RUN_IDX" >&2
  exit 1
fi
find_free_port_pair() {
  "$PY_BIN" - "$CONTEXTSNIPER_BASE_PORT" "$AGFS_BASE_PORT" "$RUN_IDX" <<'PY'
import socket
import sys

contextsniper_base, agfs_base, run_idx = map(int, sys.argv[1:4])

def available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True

for offset in range(200):
    contextsniper = contextsniper_base + run_idx + offset
    agfs = agfs_base + run_idx + offset
    if contextsniper != agfs and available(contextsniper) and available(agfs):
        print(contextsniper, agfs)
        raise SystemExit(0)
raise SystemExit("No free ContextSniper/AGFS port pair found")
PY
}
if [ "${SWE_PLUGIN_ISOLATE_PORTS:-1}" = "1" ]; then
  read -r CONTEXTSNIPER_HTTP_PORT AGFS_HTTP_PORT < <(find_free_port_pair)
  export CONTEXTSNIPER_HTTP_PORT AGFS_HTTP_PORT
  export CONTEXTSNIPER_URL="http://127.0.0.1:${CONTEXTSNIPER_HTTP_PORT}"
  export AGFS_BASE_URL="http://127.0.0.1:${AGFS_HTTP_PORT}"
else
  export CONTEXTSNIPER_HTTP_PORT="${CONTEXTSNIPER_HTTP_PORT:-$((CONTEXTSNIPER_BASE_PORT + RUN_IDX))}"
  export AGFS_HTTP_PORT="${AGFS_HTTP_PORT:-$((AGFS_BASE_PORT + RUN_IDX))}"
  export CONTEXTSNIPER_URL="${CONTEXTSNIPER_URL:-http://127.0.0.1:${CONTEXTSNIPER_HTTP_PORT}}"
  export AGFS_BASE_URL="${AGFS_BASE_URL:-http://127.0.0.1:${AGFS_HTTP_PORT}}"
fi
export CONTEXTSNIPER_ACCOUNT_ID="${CONTEXTSNIPER_ACCOUNT_ID:-acct-demo-r${RUN_IDX}}"
export CONTEXTSNIPER_USER_ID="${CONTEXTSNIPER_USER_ID:-u-openclaw-r${RUN_IDX}}"
export CONTEXTSNIPER_AGENT_ID="${CONTEXTSNIPER_AGENT_ID:-openclaw-swe-r${RUN_IDX}}"
export CONTEXTSNIPER_SESSION_ID="${CONTEXTSNIPER_SESSION_ID:-swe-r${RUN_IDX}-p$$}"
export CONTEXTSNIPER_SEARCH_FORCE_LIMIT="${CONTEXTSNIPER_SEARCH_FORCE_LIMIT:-${CONTEXTSNIPER_SEARCH_LIMIT:-}}"
export VECTOR_DB_TYPE="${VECTOR_DB_TYPE:-memory}"
# ContextSniper runner requires code mode endpoints; force enabled unless explicitly
# overridden by CONTEXTSNIPER_CODE_TOGGLE_FORCE.
export CONTEXTSNIPER_CODE_TOGGLE="${CONTEXTSNIPER_CODE_TOGGLE_FORCE:-true}"
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-openai}"
export CONTEXTSNIPER_EMBEDDING_MODEL="${CONTEXTSNIPER_EMBEDDING_MODEL:-text-embedding-3-small}"
export CONTEXTSNIPER_EMBEDDING_BASE_URL="${CONTEXTSNIPER_EMBEDDING_BASE_URL:-https://api.openai-proxy.org}"
export CONTEXTSNIPER_EMBEDDING_API_KEY="${CONTEXTSNIPER_EMBEDDING_API_KEY:-}"
export CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES:-40}"
export CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES:-8}"
export CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS="${CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS:-120}"
export CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES="${CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES:-1}"
export CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC="${CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC:-1}"
export CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES:-3}"
export CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS="${CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS:-40}"
export CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS="${CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS:-2}"
export CONTEXTSNIPER_CODE_FUSE_MODE="${CONTEXTSNIPER_CODE_FUSE_MODE:-weighted_rrf}"
export CONTEXTSNIPER_CODE_FUSE_W_EMBED="${CONTEXTSNIPER_CODE_FUSE_W_EMBED:-0.33}"
export CONTEXTSNIPER_CODE_FUSE_W_BM25="${CONTEXTSNIPER_CODE_FUSE_W_BM25:-0.17}"
export CONTEXTSNIPER_CODE_FUSE_W_CTAGS="${CONTEXTSNIPER_CODE_FUSE_W_CTAGS:-0.17}"
export CONTEXTSNIPER_CODE_FUSE_W_GRAPH="${CONTEXTSNIPER_CODE_FUSE_W_GRAPH:-0.33}"
export CONTEXTSNIPER_BOOTSTRAP_MAX_FILES="${CONTEXTSNIPER_BOOTSTRAP_MAX_FILES:-40}"
export CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES="${CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES:-40}"
export CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION="${CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION:-1}"
export CONTEXTSNIPER_START_LOCAL_EMBED_SERVER="${CONTEXTSNIPER_START_LOCAL_EMBED_SERVER:-0}"
export CONTEXTSNIPER_OPENCLAW_AUTO_START="${CONTEXTSNIPER_OPENCLAW_AUTO_START:-1}"
export CONTEXTSNIPER_OPENCLAW_AUTO_STOP="${CONTEXTSNIPER_OPENCLAW_AUTO_STOP:-1}"
export CONTEXTSNIPER_OPENCLAW_READ_TOOL_POLICY="${CONTEXTSNIPER_OPENCLAW_READ_TOOL_POLICY:-advisory}"
export CONTEXTSNIPER_OPENCLAW_SOUL_POLICY="${CONTEXTSNIPER_OPENCLAW_SOUL_POLICY:-none}"
export CONTEXTSNIPER_PLUGIN_START_WAIT="${CONTEXTSNIPER_PLUGIN_START_WAIT:-60}"
export CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME="${CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME:-1}"
if [ -z "${AGFS_BIN:-}" ] && [ -x "$CONTEXTSNIPER_DIR/agfs/build/agfs-server" ]; then
  export AGFS_BIN="$CONTEXTSNIPER_DIR/agfs/build/agfs-server"
fi
export CONTEXTSNIPER_FILTER_ENABLED="${CONTEXTSNIPER_FILTER_ENABLED:-0}"
export CONTEXTSNIPER_FILTER_NATIVE_READ="${CONTEXTSNIPER_FILTER_NATIVE_READ:-0}"
export CONTEXTSNIPER_FILTER_NATIVE_BASH="${CONTEXTSNIPER_FILTER_NATIVE_BASH:-0}"

ensure_embedding_backend() {
  [ "${CONTEXTSNIPER_EMBEDDING_PROBE_REQUIRED:-0}" = "1" ] || return 0

  local provider_lc
  provider_lc="$(printf '%s' "${EMBEDDING_PROVIDER:-}" | tr '[:upper:]' '[:lower:]')"
  [ "$provider_lc" = "openai" ] || return 0

  # Fail fast when required embedding credentials are missing.
  if [ -z "${CONTEXTSNIPER_EMBEDDING_API_KEY:-}" ]; then
    echo "[setup] CONTEXTSNIPER_EMBEDDING_API_KEY is empty; cannot run ContextSniper code search with EMBEDDING_PROVIDER=openai." >&2
    exit 1
  fi

  # Probe embedding endpoint quickly; fail fast on auth/network issues.
  if ! "$PY_BIN" - <<'PY'
import json, os, urllib.request, urllib.error, sys
base = (os.environ.get("CONTEXTSNIPER_EMBEDDING_BASE_URL") or "https://api.openai-proxy.org").rstrip("/")
if base.endswith("/v1"):
    url = base + "/embeddings"
else:
    url = base + "/v1/embeddings"
model = os.environ.get("CONTEXTSNIPER_EMBEDDING_MODEL") or "text-embedding-3-small"
key = os.environ.get("CONTEXTSNIPER_EMBEDDING_API_KEY") or ""
body = json.dumps({"model": model, "input": ["embedding health probe"]}).encode("utf-8")
req = urllib.request.Request(
    url,
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        ok = 200 <= getattr(resp, "status", 200) < 300
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
PY
  then
    echo "[setup] Embedding probe failed for EMBEDDING_PROVIDER=openai." >&2
    echo "[setup] Check CONTEXTSNIPER_EMBEDDING_API_KEY / CONTEXTSNIPER_EMBEDDING_BASE_URL / CONTEXTSNIPER_EMBEDDING_MODEL." >&2
    exit 1
  fi
}

ensure_embedding_backend

if [ "${CONTEXTSNIPER_FORCE_EMBED_DIM_ALIGN:-1}" = "1" ]; then
  _m="$(printf '%s' "${CONTEXTSNIPER_EMBEDDING_MODEL:-}" | tr '[:upper:]' '[:lower:]')"
  case "$_m" in
    *text-embedding-3-large*) export OPENGAUSS_DIMENSION=3072 ;;
    *text-embedding-3-small*|*text-embedding-ada-002*) export OPENGAUSS_DIMENSION=1536 ;;
    *) export OPENGAUSS_DIMENSION=384 ;;
  esac
fi

WORKSPACE_GIT_MOVED=0
CONTEXTSNIPER_BACKEND_MANAGED=0
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
  if [ "$CONTEXTSNIPER_BACKEND_MANAGED" = "1" ]; then
    "$PY_BIN" "$CLAUDE_PLUGIN_DIR/scripts/contextsniper_terminal.py" stop --runtime-dir "$CONTEXTSNIPER_RUNTIME_DIR" >/dev/null 2>&1 || true
  fi
}
trap cleanup_generated_workspace EXIT

if [ ! -d "$OPENCLAW_PLUGIN_DIR" ]; then
  echo "[setup] OpenClaw plugin directory not found: $OPENCLAW_PLUGIN_DIR" >&2
  exit 1
fi

export CONTEXTSNIPER_WORKSPACE_ROOT="$WORK_DIR"
export CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG=1
export CONTEXTSNIPER_OPENCLAW_RESPECT_CONFIG_WORKSPACE=1
OPENCLAW_AGENT_ID="${OPENCLAW_AGENT_ID:-swe-openclaw-contextsniper-r${RUN_IDX}-p$$}"
OPENCLAW_SESSION_ID="${OPENCLAW_SESSION_ID:-swe-openclaw-contextsniper-r${RUN_IDX}-p$$}"
OPENCLAW_TIMEOUT="${OPENCLAW_TIMEOUT:-900}"

echo "[setup] Starting ContextSniper backend for this OpenClaw SWE run: $CONTEXTSNIPER_RUNTIME_DIR" >&2
"$PY_BIN" "$CLAUDE_PLUGIN_DIR/scripts/contextsniper_terminal.py" start --runtime-dir "$CONTEXTSNIPER_RUNTIME_DIR" --wait "${CONTEXTSNIPER_PLUGIN_START_WAIT:-60}" >&2
CONTEXTSNIPER_BACKEND_MANAGED=1

# The runner starts the isolated backend before OpenClaw begins. Keep plugin
# auto-start disabled inside OpenClaw so tools use this instance instead of
# racing a second startup path with stale gateway environment.
export CONTEXTSNIPER_OPENCLAW_AUTO_START=0
export CONTEXTSNIPER_OPENCLAW_AUTO_STOP=0

echo "[setup] Installing OpenClaw ContextSniper plugin from $OPENCLAW_PLUGIN_DIR" >&2
openclaw plugins uninstall contextsniper --force \
  > "$LOGS_DIR/openclaw-plugin-uninstall.log" 2>&1 || true
openclaw plugins install --link "$OPENCLAW_PLUGIN_DIR" --dangerously-force-unsafe-install \
  > "$LOGS_DIR/openclaw-plugin-install.log" 2>&1
openclaw plugins enable contextsniper \
  > "$LOGS_DIR/openclaw-plugin-enable.log" 2>&1 || true

json_string() {
  "$PY_BIN" - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}
json_bool() {
  case "${1:-0}" in
    1|true|TRUE|yes|YES|on|ON) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

echo "[setup] Writing per-run OpenClaw ContextSniper plugin config" >&2
openclaw config set plugins.load.paths "[$(json_string "$OPENCLAW_PLUGIN_DIR")]" --strict-json \
  > "$LOGS_DIR/openclaw-config-load-paths.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.workspaceRoot "$(json_string "$WORK_DIR")" --strict-json \
  > "$LOGS_DIR/openclaw-config-workspace.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.runtimeDir "$(json_string "$CONTEXTSNIPER_RUNTIME_DIR")" --strict-json \
  > "$LOGS_DIR/openclaw-config-runtime.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.contextsniperUrl "$(json_string "$CONTEXTSNIPER_URL")" --strict-json \
  > "$LOGS_DIR/openclaw-config-contextsniper-url.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.autoStart false --strict-json \
  > "$LOGS_DIR/openclaw-config-autostart.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.autoStop false --strict-json \
  > "$LOGS_DIR/openclaw-config-autostop.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.injectCodePolicy false --strict-json \
  > "$LOGS_DIR/openclaw-config-inject.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.readToolPolicy "$(json_string "$CONTEXTSNIPER_OPENCLAW_READ_TOOL_POLICY")" --strict-json \
  > "$LOGS_DIR/openclaw-config-read-tool-policy.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.filterEnabled "$(json_bool "$CONTEXTSNIPER_FILTER_ENABLED")" --strict-json \
  > "$LOGS_DIR/openclaw-config-filter-enabled.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.filterNativeRead "$(json_bool "$CONTEXTSNIPER_FILTER_NATIVE_READ")" --strict-json \
  > "$LOGS_DIR/openclaw-config-filter-read.log" 2>&1
openclaw config set plugins.entries.contextsniper.config.filterNativeExec "$(json_bool "$CONTEXTSNIPER_FILTER_NATIVE_BASH")" --strict-json \
  > "$LOGS_DIR/openclaw-config-filter-exec.log" 2>&1

openclaw gateway restart \
  > "$LOGS_DIR/openclaw-gateway-restart.log" 2>&1 || true

openclaw plugins inspect contextsniper --runtime --json \
  > "$LOGS_DIR/openclaw-plugin-runtime.json" 2> "$LOGS_DIR/openclaw-plugin-runtime.stderr" || true

echo "[setup] Creating OpenClaw agent $OPENCLAW_AGENT_ID for $WORK_DIR" >&2
openclaw agents add "$OPENCLAW_AGENT_ID" \
  --workspace "$WORK_DIR" \
  --model "$OPENCLAW_MODEL" \
  --non-interactive \
  --json > "$LOGS_DIR/openclaw-agent-add.json" 2> "$LOGS_DIR/openclaw-agent-add.stderr" || true

if [ "$CONTEXTSNIPER_OPENCLAW_SOUL_POLICY" = "contextsniper" ]; then
cat > "$WORK_DIR/SOUL.md" <<'EOF2'
# SOUL.md - ContextSniper SWE Run

This workspace is running under the ContextSniper policy injected by
the OpenClaw plugin. For repository source, test, documentation, and release
note investigation, treat `contextsniper_search_code` results as the file context.

If ContextSniper returns a usable `content_excerpt`, use it directly for reasoning and
patch construction. Avoid broad native `read` exploration after same-file ContextSniper
snippets exist; a narrow native `read` is acceptable to verify an applied edit.
EOF2
fi

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
  if ! "$PY_BIN" - "$EXPERIMENT_DIR/openclaw_tool_summary.json" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
if not data.get("has_contextsniper_search_code"):
    print("[verify] OpenClaw run did not call contextsniper_search_code.", file=sys.stderr)
    sys.exit(2)
if not data.get("has_contextsniper_edit_file"):
    print("[verify] OpenClaw run did not call contextsniper_edit_file.", file=sys.stderr)
    sys.exit(3)
if not data.get("has_successful_contextsniper_edit_file"):
    print("[verify] OpenClaw run did not have a successful contextsniper_edit_file call.", file=sys.stderr)
    sys.exit(4)
if data.get("edited_test_file"):
    print("[verify] OpenClaw edited a test file; SWE runner expects source-only patches.", file=sys.stderr)
    sys.exit(5)
PY
  then
    RC=${RC:-1}
    [ "$RC" -eq 0 ] && RC=4
  fi
else
  echo "[verify] OpenClaw session JSONL not found: $SESSION_JSONL" >&2
  [ "$RC" -eq 0 ] && RC=5
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
echo "OpenClaw tool summary: $EXPERIMENT_DIR/openclaw_tool_summary.json"
echo "Validation markdown: $EXPERIMENT_DIR/validation.md"
echo "Validation json: $EXPERIMENT_DIR/validation.json"
echo "Validation rc: $VALIDATION_RC"
if [ "$RC" -ne 0 ]; then
  exit "$RC"
fi
exit "$VALIDATION_RC"
