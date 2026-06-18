#!/usr/bin/env bash
set -euo pipefail

# IMPORTANT: Agent commands must run from the generated SWE workspace, not the
# ContextSniper repo root. If you edit this runner or invoke the agent
# manually, cd to "$WORK_DIR" first; otherwise native file tools can resolve
# paths against the wrong project.

_SCRIPTS_MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Loopback must not use HTTP(S) proxy; AGFS and ContextSniper run on localhost.
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="$NO_PROXY"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CONTEXTSNIPER_DIR="${CONTEXTSNIPER_DIR:-$PROJECT_ROOT}"
CLAUDE_PLUGIN_DIR="${CLAUDE_PLUGIN_DIR:-$CONTEXTSNIPER_DIR/claude-plugin}"
export CLAUDE_MODEL="${CLAUDE_MODEL:-claude-haiku-4-5-20251001}"
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

# SWE runner defaults. Keep them in this runner so it can be executed directly.
export CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
CACHE_DIR="${SWE_CACHE_DIR:-${CONTEXTSNIPER_SWE_CACHE_DIR:-$CONTEXTSNIPER_CACHE_HOME/swe-pro/contextsniper/cache}}"
export REPO_BASE="${REPO_BASE:-$CACHE_DIR/repo}"
export SWE_OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-$_SCRIPTS_MCP_DIR/output_logs}"
export CONTEXTSNIPER_CODE_TOGGLE_FORCE="${CONTEXTSNIPER_CODE_TOGGLE_FORCE:-true}"
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-openai}"
export CONTEXTSNIPER_EMBEDDING_MODEL="${CONTEXTSNIPER_EMBEDDING_MODEL:-text-embedding-3-small}"
export CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES:-80}"
export CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES:-80}"
export CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS="${CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS:-500}"
export CONTEXTSNIPER_CODE_FUSE_MODE="${CONTEXTSNIPER_CODE_FUSE_MODE:-weighted_rrf}"
export CONTEXTSNIPER_CODE_FUSE_W_EMBED="${CONTEXTSNIPER_CODE_FUSE_W_EMBED:-0.33}"
export CONTEXTSNIPER_CODE_FUSE_W_BM25="${CONTEXTSNIPER_CODE_FUSE_W_BM25:-0.17}"
export CONTEXTSNIPER_CODE_FUSE_W_CTAGS="${CONTEXTSNIPER_CODE_FUSE_W_CTAGS:-0.17}"
export CONTEXTSNIPER_CODE_FUSE_W_GRAPH="${CONTEXTSNIPER_CODE_FUSE_W_GRAPH:-0.33}"
export CONTEXTSNIPER_BOOTSTRAP_MAX_FILES="${CONTEXTSNIPER_BOOTSTRAP_MAX_FILES:-40}"
export CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES="${CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES:-40}"
export CONTEXTSNIPER_START_LOCAL_EMBED_SERVER="${CONTEXTSNIPER_START_LOCAL_EMBED_SERVER:-0}"

# Priority: first CLI arg > env > default.
# Default to a qutebrowser SWE-bench Pro task for Python-centric debugging.
export SWE_PRO_INSTANCE_ID="${1:-${SWE_PRO_INSTANCE_ID:-instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c}}"
export CONTEXTSNIPER_SEARCH_LIMIT="${CONTEXTSNIPER_SEARCH_LIMIT:-}"
export SWE_USE_DERIVED_LOCAL_ENV="${SWE_USE_DERIVED_LOCAL_ENV:-1}"
export SWE_VALIDATION_FORCE_LOCAL="${SWE_VALIDATION_FORCE_LOCAL:-1}"

# Parallel-run isolation knobs.
# RUN_IDX lets callers run multiple jobs concurrently with deterministic offsets.
RUN_IDX="${RUN_IDX:-0}"
export RUN_IDX
CONTEXTSNIPER_BASE_PORT="${CONTEXTSNIPER_BASE_PORT:-8090}"
AGFS_BASE_PORT="${AGFS_BASE_PORT:-1833}"

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
  SWE_PROMPT_KIND="basic" \
  "$PY_BIN" "$_SCRIPTS_MCP_DIR/../../resolve_swe_pro_instance.py"
)" || {
  echo "[setup] Failed to resolve SWE-bench instance metadata for ${SWE_PRO_INSTANCE_ID:-<unset>}." >&2
  exit 1
}
eval "$_swe_prompt_exports"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-${CONTEXTSNIPER_SWE_OUTPUT_ROOT:-$_SCRIPTS_MCP_DIR/output_logs}}"
EXPERIMENT_DIR="$OUTPUT_ROOT/${STAMP}-swe-pro-contextsniper-r${RUN_IDX}-p$$"
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

  git -C "$CANON_DIR" fetch --all --prune
  if ! git -C "$CANON_DIR" rev-parse --verify -q "$SWE_BASE_COMMIT^{commit}" >/dev/null 2>&1; then
    git -C "$CANON_DIR" fetch origin
  fi
  git -C "$CANON_DIR" checkout -f "$SWE_BASE_COMMIT" --
  git -C "$CANON_DIR" clean -fdx

  rm -rf "$WORK_DIR"
  git clone --quiet "$CANON_DIR" "$WORK_DIR"
} 9>"$CANON_LOCK"
cp -a "$SWE_INSTANCE_JSON" "$EXPERIMENT_DIR/instance.json"
cp -a "$SWE_PROMPT_FILE" "$WORK_DIR/TASK.md"
export CONTEXTSNIPER_WORKSPACE_ROOT="${CONTEXTSNIPER_WORKSPACE_ROOT:-$WORK_DIR}"

# ContextSniper-FILTER mode intentionally exercises read/bash output filtering.
export CONTEXTSNIPER_FILTER_ENABLED=1
export CONTEXTSNIPER_FILTER_NATIVE_READ=1
export CONTEXTSNIPER_FILTER_NATIVE_BASH=1
export CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1

CODE_POLICY_RENDERER="$CLAUDE_PLUGIN_DIR/bin/contextsniper-render-code-policy"
if [ "${CONTEXTSNIPER_APPEND_CODE_POLICY_TO_TASK:-1}" = "1" ] && [ -x "$CODE_POLICY_RENDERER" ]; then
  {
    printf '\n'
    "$CODE_POLICY_RENDERER"
  } >> "$WORK_DIR/TASK.md"
fi

CLAUDE_LOG="$LOGS_DIR/claude-code-debug.log"
CLAUDE_STDOUT="$LOGS_DIR/claude-stdout.log"

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
export CONTEXTSNIPER_USER_ID="${CONTEXTSNIPER_USER_ID:-u-claude-r${RUN_IDX}}"
export CONTEXTSNIPER_AGENT_ID="${CONTEXTSNIPER_AGENT_ID:-claude-code-r${RUN_IDX}}"
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
export CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES:-80}"
export CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES="${CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES:-80}"
export CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS="${CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS:-500}"
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
export CONTEXTSNIPER_PLUGIN_AUTO_START="${CONTEXTSNIPER_PLUGIN_AUTO_START:-1}"
export CONTEXTSNIPER_PLUGIN_AUTO_STOP="${CONTEXTSNIPER_PLUGIN_AUTO_STOP:-1}"
export CONTEXTSNIPER_PLUGIN_START_WAIT="${CONTEXTSNIPER_PLUGIN_START_WAIT:-60}"
export CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME="${CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME:-1}"
if [ -z "${AGFS_BIN:-}" ] && [ -x "$CONTEXTSNIPER_DIR/agfs/build/agfs-server" ]; then
  export AGFS_BIN="$CONTEXTSNIPER_DIR/agfs/build/agfs-server"
fi
export CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT="${CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT:-0}"
export CLAUDE_CODE_DEBUG_LOGS_DIR="${CLAUDE_CODE_DEBUG_LOGS_DIR:-$LOGS_DIR}"
export CLAUDE_CODE_DEBUG_LOG_LEVEL="${CLAUDE_CODE_DEBUG_LOG_LEVEL:-debug}"

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
cleanup_contextsniper_backend() {
  disable_generated_workspace_git
}
trap cleanup_contextsniper_backend EXIT

set +e
(
  cd "$WORK_DIR"
  export PYTHONPATH="$WORK_DIR${PYTHONPATH:+:$PYTHONPATH}"
  claude --model "$CLAUDE_MODEL" --plugin-dir "$CLAUDE_PLUGIN_DIR" \
    --dangerously-skip-permissions \
    --permission-mode bypassPermissions \
    --print --debug-file "$CLAUDE_LOG" < "$WORK_DIR/TASK.md"
) 2>&1 | tee "$CLAUDE_STDOUT"
RC=${PIPESTATUS[0]}
set -e

WORK_SLUG="$("$PY_BIN" - "$WORK_DIR" <<'PY'
import re
import sys
print(re.sub(r'[^A-Za-z0-9]+', '-', sys.argv[1]).rstrip('-'))
PY
)"
PROJ_DIR="${HOME}/.claude/projects/${WORK_SLUG}"
SESSION_JSONL=""
for _jsonl_retry in 1 2 3 4 5; do
  if [ -d "$PROJ_DIR" ]; then
    SESSION_JSONL="$(find "$PROJ_DIR" -maxdepth 1 -type f -name '*.jsonl' | sort | tail -n 1 || true)"
  fi
  if [ -n "$SESSION_JSONL" ] && [ -f "$SESSION_JSONL" ]; then
    break
  fi
  sleep 1
done
if [ -n "$SESSION_JSONL" ] && [ -f "$SESSION_JSONL" ]; then
  cp -f "$SESSION_JSONL" "$LOGS_DIR/"
  "$PY_BIN" "$_SCRIPTS_MCP_DIR/render_jsonl_turns.py" "$SESSION_JSONL" > "$EXPERIMENT_DIR/latest_session_render.txt" || true
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
echo "Validation markdown: $EXPERIMENT_DIR/validation.md"
echo "Validation json: $EXPERIMENT_DIR/validation.json"
echo "Validation rc: $VALIDATION_RC"
if [ "$RC" -ne 0 ]; then
  exit "$RC"
fi
exit "$VALIDATION_RC"
