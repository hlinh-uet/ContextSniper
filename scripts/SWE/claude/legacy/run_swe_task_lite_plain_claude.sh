#!/usr/bin/env bash
set -euo pipefail

# IMPORTANT: Agent commands must run from the generated SWE workspace, not the
# ContextSniper repo root. If you edit this runner or invoke the agent
# manually, cd to "$WORK_DIR" first; otherwise native file tools can resolve
# paths against the wrong project.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
CACHE_DIR="${SWE_CACHE_DIR:-${CONTEXTSNIPER_SWE_CACHE_DIR:-$CONTEXTSNIPER_CACHE_HOME/swe/plain/cache}}"
PY_BIN="${PY_BIN:-python3}"
export CLAUDE_MODEL="${CLAUDE_MODEL:-claude-haiku-4-5-20251001}"
export SWE_LITE_INSTANCE_ID="${1:-${SWE_LITE_INSTANCE_ID:-pallets__flask-4045}}"
export SWE_USE_DERIVED_LOCAL_ENV="${SWE_USE_DERIVED_LOCAL_ENV:-1}"
export SWE_VALIDATION_FORCE_LOCAL="${SWE_VALIDATION_FORCE_LOCAL:-1}"
export RUN_IDX="${RUN_IDX:-0}"

REPO_BASE="${REPO_BASE:-$CACHE_DIR/repo}"
mkdir -p "$REPO_BASE"

ensure_python_runtime() {
  if "$PY_BIN" - <<'PY' >/dev/null 2>&1
import json
import urllib.request
PY
  then
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "No usable Python interpreter found." >&2
    exit 1
  fi
  PY_BIN="$(command -v python3)"
}

ensure_python_runtime

prompt_exports="$(
  REPO_BASE="$REPO_BASE" \
  SWE_PROMPT_KIND="basic" \
  "$PY_BIN" "$SCRIPT_DIR/../../resolve_swe_lite_instance.py"
)" || {
  echo "[setup] Failed to resolve SWE-bench instance metadata for ${SWE_LITE_INSTANCE_ID:-<unset>}." >&2
  exit 1
}
eval "$prompt_exports"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-${CONTEXTSNIPER_SWE_OUTPUT_ROOT:-$SCRIPT_DIR/output_logs}}"
EXPERIMENT_DIR="$OUTPUT_ROOT/${STAMP}-swe-lite-plain-r${RUN_IDX}-p$$"
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
    tmp_dir="${CANON_DIR}.tmp.$$"
    rm -rf "$tmp_dir"
    echo "Cloning https://github.com/${SWE_REPO}.git ..." >&2
    git clone "https://github.com/${SWE_REPO}.git" "$tmp_dir"
    if ! canon_repo_ok "$tmp_dir"; then
      echo "[setup] Canonical repo clone did not produce a valid HEAD: $tmp_dir" >&2
      rm -rf "$tmp_dir"
      exit 1
    fi
    mv "$tmp_dir" "$CANON_DIR"
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

CLAUDE_LOG="$LOGS_DIR/claude-code-debug.log"
CLAUDE_STDOUT="$LOGS_DIR/claude-stdout.log"
export CLAUDE_CODE_DEBUG_LOGS_DIR="${CLAUDE_CODE_DEBUG_LOGS_DIR:-$LOGS_DIR}"
export CLAUDE_CODE_DEBUG_LOG_LEVEL="${CLAUDE_CODE_DEBUG_LOG_LEVEL:-debug}"

if [ "$SWE_USE_DERIVED_LOCAL_ENV" = "1" ]; then
  echo "[setup] Deriving local SWE-bench env ..." >&2
  LOCAL_ENV_PREP_LOG="$LOGS_DIR/swe-local-env-prepare.log"
  LOCAL_ENV_EXPORTS_FILE="$LOGS_DIR/swe-local-env-exports.sh"
  if ! "$PY_BIN" "$SCRIPT_DIR/prepare_swe_local_env.py" "$SWE_INSTANCE_JSON" "$WORK_DIR" "$EXPERIMENT_DIR" \
      > "$LOCAL_ENV_EXPORTS_FILE" 2>> "$LOCAL_ENV_PREP_LOG"; then
    cat "$LOCAL_ENV_EXPORTS_FILE" >> "$LOCAL_ENV_PREP_LOG" 2>/dev/null || true
    echo "[setup] Failed to derive local SWE-bench env; see $LOCAL_ENV_PREP_LOG" >&2
    exit 1
  fi
  cat "$LOCAL_ENV_EXPORTS_FILE" >> "$LOCAL_ENV_PREP_LOG"
  local_env_exports="$(grep '^export ' "$LOCAL_ENV_EXPORTS_FILE" || true)"
  if [ -z "$local_env_exports" ]; then
    echo "[setup] Local SWE-bench env derivation produced no exports; see $LOCAL_ENV_PREP_LOG" >&2
    exit 1
  fi
  eval "$local_env_exports"
fi

set +e
(
  cd "$WORK_DIR"
  export PYTHONPATH="$WORK_DIR${PYTHONPATH:+:$PYTHONPATH}"
  claude --model "$CLAUDE_MODEL" \
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
  "$PY_BIN" "$SCRIPT_DIR/render_jsonl_turns.py" "$SESSION_JSONL" > "$EXPERIMENT_DIR/latest_session_render.txt" || true
else
  echo "[warn] Claude session JSONL not found under $PROJ_DIR" >&2
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
cleanup_generated_workspace() {
  disable_generated_workspace_git
}
trap cleanup_generated_workspace EXIT

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
  "$PY_BIN" "$SCRIPT_DIR/validate_swe_run.py" "$SWE_INSTANCE_JSON" "$WORK_DIR" "$EXPERIMENT_DIR" \
    > "$LOGS_DIR/validation-summary.json" 2> "$LOGS_DIR/validation-stderr.log"
  VALIDATION_RC=$?
  set -e
fi

disable_generated_workspace_git

echo "Instance: $SWE_INSTANCE_ID ($SWE_REPO @ $SWE_BASE_COMMIT)"
echo "Experiment dir: $EXPERIMENT_DIR"
echo "Workspace: $WORK_DIR"
echo "Logs: $LOGS_DIR"
echo "Claude project dir: ${PROJ_DIR:-<unset>}"
echo "Claude session jsonl: ${SESSION_JSONL:-<unset>}"
echo "Latest session render: $EXPERIMENT_DIR/latest_session_render.txt"
echo "Validation markdown: $EXPERIMENT_DIR/validation.md"
echo "Validation json: $EXPERIMENT_DIR/validation.json"
echo "Validation rc: $VALIDATION_RC"
if [ "$RC" -ne 0 ]; then
  exit "$RC"
fi
exit "$VALIDATION_RC"
