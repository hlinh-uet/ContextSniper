#!/usr/bin/env bash
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PY_BIN:-python3}"
INPUT="$(cat)"
TMPFILE="$(mktemp)"
printf '%s' "$INPUT" >"$TMPFILE"

# Ingest the final transcript before shutting down the local backend. Keep this
# bounded so Claude exit is not held hostage by a slow final ingest.
if command -v timeout >/dev/null 2>&1; then
  timeout 8s "$PYTHON_BIN" "$HERE/hook_after_turn.py" <"$TMPFILE" >/dev/null 2>&1 || true
else
  "$PYTHON_BIN" "$HERE/hook_after_turn.py" <"$TMPFILE" >/dev/null 2>&1 || true
fi
rm -f "$TMPFILE"

if [ "${CONTEXTSNIPER_PLUGIN_AUTO_STOP:-1}" = "0" ]; then
  exit 0
fi

"$PYTHON_BIN" "$HERE/contextsniper_terminal.py" stop >/dev/null 2>&1 || true
exit 0
