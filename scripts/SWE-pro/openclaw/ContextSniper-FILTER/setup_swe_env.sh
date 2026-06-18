#!/usr/bin/env bash
# Source this file to load safe defaults for the OpenClaw ContextSniper-FILTER SWE Pro runner.
# This file intentionally does NOT set secrets or provider base URLs.
#
# Usage:
#   source scripts/SWE-pro/openclaw/ContextSniper-FILTER/setup_swe_env.sh
#
# IMPORTANT: When invoking OpenClaw manually after sourcing this file, cd to the
# generated SWE workspace first. Starting OpenClaw from the ContextSniper repo root makes
# native read/exec tools resolve paths against the wrong project.

_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXTSNIPER_CACHE_HOME="${CONTEXTSNIPER_CACHE_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/contextsniper}"

export SWE_CACHE_DIR="${SWE_CACHE_DIR:-$CONTEXTSNIPER_CACHE_HOME/swe-pro/openclaw/contextsniper-filter/cache}"
export REPO_BASE="${REPO_BASE:-$SWE_CACHE_DIR/repo}"
export SWE_OUTPUT_ROOT="${SWE_OUTPUT_ROOT:-$_HERE/output_logs}"

export CONTEXTSNIPER_INJECT_FILTERING_PROMPT=1
export CONTEXTSNIPER_FILTER_ENABLED=1
export CONTEXTSNIPER_FILTER_NATIVE_READ=1
export CONTEXTSNIPER_FILTER_NATIVE_BASH=1

# Reuse the OpenClaw ContextSniper defaults after setting this mode's cache/output roots.
# shellcheck disable=SC1091
source "$_HERE/../ContextSniper/setup_swe_env.sh"

echo "Loaded scripts/SWE-pro/openclaw ContextSniper-FILTER defaults."
echo "SWE_OUTPUT_ROOT=$SWE_OUTPUT_ROOT"
echo "SWE_CACHE_DIR=$SWE_CACHE_DIR"
echo "CONTEXTSNIPER_INJECT_FILTERING_PROMPT=$CONTEXTSNIPER_INJECT_FILTERING_PROMPT"
echo "CONTEXTSNIPER_FILTER_ENABLED=$CONTEXTSNIPER_FILTER_ENABLED"
echo "CONTEXTSNIPER_FILTER_NATIVE_READ=$CONTEXTSNIPER_FILTER_NATIVE_READ"
echo "CONTEXTSNIPER_FILTER_NATIVE_BASH=$CONTEXTSNIPER_FILTER_NATIVE_BASH"
