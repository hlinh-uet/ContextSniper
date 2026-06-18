#!/usr/bin/env bash
# Source this file to load safe defaults for the plain OpenClaw SWE Pro runner.
#
# IMPORTANT: When invoking OpenClaw manually after sourcing this file, cd to the
# generated SWE workspace first. Starting OpenClaw from the ContextSniper repo root makes
# native read/exec tools resolve paths against the wrong project.

export OPENCLAW_MODEL="${OPENCLAW_MODEL:-deepseek/deepseek-v4-flash}"
export SWE_PRO_INSTANCE_ID="${SWE_PRO_INSTANCE_ID:-instance_qutebrowser__qutebrowser-f91ace96223cac8161c16dd061907e138fe85111-v059c6fdc75567943479b23ebca7c07b5e9a7f34c}"
export CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
export REPO_BASE="${REPO_BASE:-$CONTEXTSNIPER_CACHE_HOME/swe-pro/openclaw/plain/cache/repo}"
_SWE_OPENCLAW_LEGACY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SWE_OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-$_SWE_OPENCLAW_LEGACY_DIR/output_logs}"
export SWE_VALIDATION_FORCE_LOCAL="${SWE_VALIDATION_FORCE_LOCAL:-1}"
export SWE_SKIP_VALIDATION="${SWE_SKIP_VALIDATION:-1}"

echo "Loaded scripts/SWE-pro/openclaw legacy SWE defaults."
echo "SWE_PRO_INSTANCE_ID=$SWE_PRO_INSTANCE_ID"
echo "OPENCLAW_MODEL=$OPENCLAW_MODEL"
echo "SWE_VALIDATION_FORCE_LOCAL=$SWE_VALIDATION_FORCE_LOCAL"
echo "SWE_SKIP_VALIDATION=$SWE_SKIP_VALIDATION"
