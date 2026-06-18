#!/usr/bin/env bash
# Source this file before starting Claude with the local ContextSniper plugin.
#
# Usage:
#   source /path/to/source_tree/setup_env.sh

# Let developer machines provide local secrets/base URLs from their shell setup.
# This file intentionally does not set API keys.
if [ -f "$HOME/.bashrc" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.bashrc" >/dev/null 2>&1 || true
fi

# Backward compatibility for machines configured before the ContextSniper
# rename. Prefer the new names when both are present.
export CONTEXTSNIPER_EMBEDDING_API_KEY="${CONTEXTSNIPER_EMBEDDING_API_KEY:-${RTC_EMBEDDING_API_KEY:-}}"
export CONTEXTSNIPER_EMBEDDING_BASE_URL="${CONTEXTSNIPER_EMBEDDING_BASE_URL:-${RTC_EMBEDDING_BASE_URL:-}}"
export CONTEXTSNIPER_EMBEDDING_MODEL="${CONTEXTSNIPER_EMBEDDING_MODEL:-${RTC_EMBEDDING_MODEL:-}}"

# User-editable settings.
# Keep real API keys in your shell profile when possible; this empty default is
# here to show the required variable name for semantic code search.
export CONTEXTSNIPER_EMBEDDING_API_KEY="${CONTEXTSNIPER_EMBEDDING_API_KEY:-}"
export CONTEXTSNIPER_EMBEDDING_BASE_URL="${CONTEXTSNIPER_EMBEDDING_BASE_URL:-https://api.openai-proxy.org}"
export CONTEXTSNIPER_EMBEDDING_MODEL="${CONTEXTSNIPER_EMBEDDING_MODEL:-text-embedding-3-small}"
export EMBEDDING_PROVIDER="${EMBEDDING_PROVIDER:-openai}"
export CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED="${CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED:-1}"
export CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED="${CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED:-1}"
export CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED="${CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED:-1}"
export CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED="${CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED:-1}"

export CONTEXTSNIPER_HTTP_PORT="${CONTEXTSNIPER_HTTP_PORT:-8090}"
export AGFS_HTTP_PORT="${AGFS_HTTP_PORT:-1833}"
export CONTEXTSNIPER_PLUGIN_AUTO_START="${CONTEXTSNIPER_PLUGIN_AUTO_START:-1}"
export CONTEXTSNIPER_PLUGIN_AUTO_STOP="${CONTEXTSNIPER_PLUGIN_AUTO_STOP:-1}"
export CONTEXTSNIPER_INJECT_FILTERING_PROMPT="${CONTEXTSNIPER_INJECT_FILTERING_PROMPT:-0}"
export OPENCLAW_MODEL="${OPENCLAW_MODEL:-}"

export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
export GOSUMDB="${GOSUMDB:-sum.golang.google.cn}"
export NPM_CONFIG_REGISTRY="${NPM_CONFIG_REGISTRY:-https://registry.npmmirror.com/}"
export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"
export PATH="$HOME/.local/bin:$HOME/.openclaw/bin:$PATH"

export CONTEXTSNIPER_SOURCE_TREE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CONTEXTSNIPER_DIR="$CONTEXTSNIPER_SOURCE_TREE"
export CONTEXTSNIPER_CLAUDE_PLUGIN_DIR="$CONTEXTSNIPER_SOURCE_TREE/claude-plugin"

contextsniper_python_has_runtime_deps() {
  [ -n "${1:-}" ] || return 1
  [ -x "$1" ] || return 1
  "$1" - <<'PY' >/dev/null 2>&1
import flask
import mcp
import openai
import pyagfs
PY
}

contextsniper_pick_python() {
  local candidate
  for candidate in \
    "${PY_BIN:-}" \
    "$CONTEXTSNIPER_SOURCE_TREE/.venv/bin/python" \
    "$CONTEXTSNIPER_SOURCE_TREE/.venv/bin/python3" \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python 2>/dev/null || true)"
  do
    if contextsniper_python_has_runtime_deps "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PY_BIN="$(contextsniper_pick_python)"; then
  unset PY_BIN
fi
export PY_BIN="${PY_BIN:-}"

export CONTEXTSNIPER_URL="${CONTEXTSNIPER_URL:-http://127.0.0.1:${CONTEXTSNIPER_HTTP_PORT}}"
export AGFS_BASE_URL="${AGFS_BASE_URL:-http://127.0.0.1:${AGFS_HTTP_PORT}}"
# Local ContextSniper/AGFS traffic must not be routed through HTTP(S)/SOCKS proxies.
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="$NO_PROXY"

echo "Loaded ContextSniper Claude plugin defaults."
echo "CONTEXTSNIPER_CLAUDE_PLUGIN_DIR=$CONTEXTSNIPER_CLAUDE_PLUGIN_DIR"
echo "PY_BIN=${PY_BIN:-<unset>}"
if [ -z "${PY_BIN:-}" ]; then
  echo "PY_BIN warning: no Python with required runtime packages was found."
  echo "Install with: cd $CONTEXTSNIPER_SOURCE_TREE && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
fi
if [ -n "${CONTEXTSNIPER_EMBEDDING_API_KEY:-}" ]; then
  echo "CONTEXTSNIPER_EMBEDDING_API_KEY=set"
else
  echo "CONTEXTSNIPER_EMBEDDING_API_KEY=<empty> (set this before real code search)"
fi
