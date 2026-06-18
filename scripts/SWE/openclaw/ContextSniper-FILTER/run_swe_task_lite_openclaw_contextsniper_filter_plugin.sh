#!/usr/bin/env bash
set -euo pipefail

# IMPORTANT: OpenClaw must run from the generated SWE workspace, not the
# ContextSniper repo root. This wrapper delegates to ../ContextSniper, whose
# runner must cd to "$WORK_DIR" before `openclaw agent`/`openclaw chat`;
# otherwise native read/exec tools can resolve paths against the wrong project.

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CONTEXTSNIPER_RUNNER_DIR="$(cd "$_HERE/../ContextSniper" && pwd)"

CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
export SWE_CACHE_DIR="${SWE_CACHE_DIR:-$CONTEXTSNIPER_CACHE_HOME/swe/openclaw/contextsniper-filter/cache}"
export SWE_OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-$_HERE/output_logs}"

export CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1
export CONTEXTSNIPER_FILTER_ENABLED=1
export CONTEXTSNIPER_FILTER_NATIVE_READ=1
export CONTEXTSNIPER_FILTER_NATIVE_BASH=1

exec "$_CONTEXTSNIPER_RUNNER_DIR/run_swe_task_lite_openclaw_contextsniper_plugin.sh" "$@"
