#!/usr/bin/env python3
"""Prepare a SWE-bench Pro Docker-backed command helper for a workspace."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


DEFAULT_DOCKERHUB_USERNAME = "jefzda"


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def sanitize(value: str, limit: int = 96) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe[:limit] or "swe-pro"


def docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False


def docker_image(instance: dict) -> str:
    dockerhub_tag = (
        os.environ.get("SWE_DOCKERHUB_TAG")
        or instance.get("dockerhub_tag")
        or ""
    ).strip()
    if not dockerhub_tag:
        raise RuntimeError("SWE-bench Pro instance is missing dockerhub_tag.")
    username = os.environ.get("SWE_PRO_DOCKERHUB_USERNAME", DEFAULT_DOCKERHUB_USERNAME)
    return f"{username}/sweap-images:{dockerhub_tag}"


def write_helper_script(path: Path, instance: dict, image: str, workspace: Path, logs_dir: Path) -> None:
    cmd_log = logs_dir / "swe-pro-docker-commands.log"
    base_commit = instance["base_commit"]
    platform = os.environ.get("SWE_PRO_DOCKER_PLATFORM", "")
    pull = os.environ.get("SWE_PRO_DOCKER_PULL", "missing")
    network = os.environ.get("SWE_PRO_DOCKER_NETWORK", "")
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'if [ "$#" -eq 0 ]; then',
                '  echo "usage: $0 [--reinstall] [--stdin-python] <command> [args...]" >&2',
                "  exit 2",
                "fi",
                'MODE="normal"',
                'STDIN_PYTHON="0"',
                'while [ "$#" -gt 0 ]; do',
                '  case "${1:-}" in',
                '    --reinstall)',
                '      MODE="reinstall"',
                "      shift",
                "      ;;",
                '    --stdin-python)',
                '      STDIN_PYTHON="1"',
                "      shift",
                "      ;;",
                "    *)",
                "      break",
                "      ;;",
                "  esac",
                "done",
                'if [ "$STDIN_PYTHON" = "0" ] && [ "$#" -eq 0 ]; then',
                '  echo "usage: $0 [--reinstall] [--stdin-python] <command> [args...]" >&2',
                "  exit 2",
                "fi",
                f'WORKSPACE={shell_quote(str(workspace))}',
                f'IMAGE={shell_quote(image)}',
                f'BASE_COMMIT={shell_quote(base_commit)}',
                f'CMD_LOG={shell_quote(str(cmd_log))}',
                f'PLATFORM={shell_quote(platform)}',
                f'PULL_POLICY={shell_quote(pull)}',
                f'NETWORK_MODE={shell_quote(network)}',
                'TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swe-pro-docker-XXXXXX")"',
                'cleanup() { rm -rf "$TMP_DIR"; }',
                "trap cleanup EXIT",
                'PATCH_FILE="$TMP_DIR/current.patch"',
                'STDIN_FILE="$TMP_DIR/stdin.py"',
                'git -C "$WORKSPACE" diff --binary >"$PATCH_FILE"',
                'DOCKER_ARGS=(run --rm -v "$TMP_DIR:/workspace:rw" --entrypoint /bin/bash)',
                'DOCKER_ARGS+=(-v "$WORKSPACE:/host_workspace:ro")',
                'if [ -n "$PLATFORM" ]; then DOCKER_ARGS+=(--platform "$PLATFORM"); fi',
                'if [ -n "$NETWORK_MODE" ]; then DOCKER_ARGS+=(--network "$NETWORK_MODE"); fi',
                'if [ "$PULL_POLICY" != "0" ] && [ "$PULL_POLICY" != "false" ]; then',
                '  DOCKER_ARGS+=(--pull "$PULL_POLICY")',
                "fi",
                'if [ "$STDIN_PYTHON" = "1" ]; then',
                '  cat >"$STDIN_FILE"',
                '  USER_CMD=(python /workspace/stdin.py)',
                '  printf "\\n[%s] $ python <stdin>\\n" "$(date -Is)" | tee -a "$CMD_LOG"',
                'elif [ "$#" -ge 2 ] && [ "${1##*/}" = "python" ] && [ "${2:-}" = "-" ] && [ ! -t 0 ]; then',
                '  cat >"$STDIN_FILE"',
                '  USER_CMD=(python /workspace/stdin.py)',
                '  printf "\\n[%s] $ python -\\n" "$(date -Is)" | tee -a "$CMD_LOG"',
                "else",
                '  USER_CMD=("$@")',
                '  printf "\\n[%s] $ %s\\n" "$(date -Is)" "$*" | tee -a "$CMD_LOG"',
                "fi",
                'INNER=$(printf "%q " "${USER_CMD[@]}")',
                'docker "${DOCKER_ARGS[@]}" "$IMAGE" -lc "set -euo pipefail; cd /app; git reset --hard $BASE_COMMIT; git checkout $BASE_COMMIT; if [ -s /workspace/current.patch ]; then git apply --whitespace=nowarn /workspace/current.patch; fi; if [ \"$MODE\" = reinstall ]; then python -m pip install -v -e .; fi; $INNER" | tee -a "$CMD_LOG"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: prepare_swe_local_env.py <instance.json> <workspace> <experiment_dir>", file=sys.stderr)
        return 2

    instance_path = Path(sys.argv[1]).resolve()
    workspace = Path(sys.argv[2]).resolve()
    experiment_dir = Path(sys.argv[3]).resolve()
    logs_dir = experiment_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    instance = json.loads(instance_path.read_text(encoding="utf-8"))
    image = docker_image(instance)
    info_path = experiment_dir / "swe_pro_docker_env.json"
    create_log = logs_dir / "swe-pro-docker-env-prepare.log"

    if not docker_available():
        create_log.write_text("docker info failed; Docker is required for SWE-bench Pro images.\n", encoding="utf-8")
        return 1

    helper_path = workspace / "RUN_IN_SWE_PRO_DOCKER_ENV.sh"
    write_helper_script(helper_path, instance, image, workspace, logs_dir)

    meta = {
        "instance_id": instance.get("instance_id"),
        "repo": instance.get("repo"),
        "base_commit": instance.get("base_commit"),
        "docker_image": image,
        "dockerhub_tag": instance.get("dockerhub_tag"),
        "helper_script": str(helper_path),
        "command_log": str(logs_dir / "swe-pro-docker-commands.log"),
        "mode": "swe_pro_docker_image",
    }
    info_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    create_log.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    env_prefix = f"docker://{image}"
    exports = {
        "SWE_TASK_ENV_MODE": "swe_pro_docker_image",
        "SWE_TASK_DOCKER_IMAGE": image,
        "SWE_TASK_ENV_PREFIX": env_prefix,
        "SWE_TASK_ENV_HELPER": str(helper_path),
        "SWE_TASK_ENV_INFO_JSON": str(info_path),
        "SWE_TASK_ENV_CREATE_LOG": str(create_log),
        "SWE_TASK_ENV_COMMAND_LOG": str(logs_dir / "swe-pro-docker-commands.log"),
    }
    for key, value in exports.items():
        print(f"export {key}={shell_quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
