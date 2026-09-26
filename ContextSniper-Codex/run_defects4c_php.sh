#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTEXTSNIPER_ROOT="$(cd "$HERE/.." && pwd)"

if [ -f "$CONTEXTSNIPER_ROOT/setup_env.sh" ]; then
  # shellcheck disable=SC1091
  . "$CONTEXTSNIPER_ROOT/setup_env.sh" >/dev/null
fi

export CONTEXTSNIPER_PYTHON="${CONTEXTSNIPER_PYTHON:-${PY_BIN:-}}"
DRIVER_PYTHON="${CONTEXTSNIPER_CODEX_PYTHON:-${CONTEXTSNIPER_PYTHON:-}}"
if [ -z "$DRIVER_PYTHON" ] && [ -x "$CONTEXTSNIPER_ROOT/.venv/bin/python" ]; then
  DRIVER_PYTHON="$CONTEXTSNIPER_ROOT/.venv/bin/python"
fi
if [ ! -x "$DRIVER_PYTHON" ]; then
  DRIVER_PYTHON="$(command -v python3 || true)"
fi
if [ -z "$DRIVER_PYTHON" ] || [ ! -x "$DRIVER_PYTHON" ]; then
  echo "No Python interpreter is available for the ContextSniper-Codex adapter." >&2
  exit 2
fi
if ! "$DRIVER_PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "ContextSniper-Codex requires Python 3.11 or newer." >&2
  exit 2
fi

exec "$DRIVER_PYTHON" "$HERE/contextsniper_codex.py" "$@"
