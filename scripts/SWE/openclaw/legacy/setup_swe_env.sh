#!/usr/bin/env bash
# Source this file to load safe defaults for the plain OpenClaw SWE Lite runner.
#
# IMPORTANT: When invoking OpenClaw manually after sourcing this file, cd to the
# generated SWE workspace first. Starting OpenClaw from the ContextSniper repo root makes
# native read/exec tools resolve paths against the wrong project.

export OPENCLAW_MODEL="${OPENCLAW_MODEL:-deepseek/deepseek-v4-flash}"
export SWE_LITE_INSTANCE_ID="${SWE_LITE_INSTANCE_ID:-pallets__flask-4045}"
export CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"
export REPO_BASE="${REPO_BASE:-$CONTEXTSNIPER_CACHE_HOME/swe/openclaw/plain/cache/repo}"
_SWE_OPENCLAW_LEGACY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SWE_OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-$_SWE_OPENCLAW_LEGACY_DIR/output_logs}"
export SWE_VALIDATION_FORCE_LOCAL="${SWE_VALIDATION_FORCE_LOCAL:-1}"
export SWE_SKIP_VALIDATION="${SWE_SKIP_VALIDATION:-1}"

echo "Loaded scripts/SWE/openclaw legacy SWE defaults."
echo "SWE_LITE_INSTANCE_ID=$SWE_LITE_INSTANCE_ID"
echo "OPENCLAW_MODEL=$OPENCLAW_MODEL"
echo "SWE_VALIDATION_FORCE_LOCAL=$SWE_VALIDATION_FORCE_LOCAL"
echo "SWE_SKIP_VALIDATION=$SWE_SKIP_VALIDATION"
