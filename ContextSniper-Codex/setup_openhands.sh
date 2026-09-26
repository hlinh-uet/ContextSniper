#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${1:-$here/../.venv/bin/python}"
"$python_bin" -m venv "$here/.venv-openhands"
"$here/.venv-openhands/bin/python" -m pip install -r "$here/requirements-openhands.lock"
"$here/.venv-openhands/bin/python" -m pip check
