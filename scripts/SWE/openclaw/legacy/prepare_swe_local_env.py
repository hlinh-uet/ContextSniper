#!/usr/bin/env python3
import json
import os
import re
import shlex
import subprocess
import sys
import fcntl
import shutil
from pathlib import Path

from swebench.harness.test_spec.test_spec import make_test_spec


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def sanitize(value: str, limit: int = 80) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe[:limit] or "env"


def conda_base() -> str:
    candidates = [
        os.environ.get("CONDA_EXE"),
        shutil.which("conda"),
        str(Path.home() / "miniconda3" / "bin" / "conda"),
        str(Path.home() / "anaconda3" / "bin" / "conda"),
    ]
    conda = next((item for item in candidates if item and Path(item).is_file()), "")
    if not conda:
        raise FileNotFoundError(
            "conda executable not found. Set CONDA_EXE or add conda to PATH."
        )
    proc = subprocess.run(
        [conda, "info", "--base"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def adapt_env_commands(commands: list[str], base: str, prefix: Path, pkgs_dir: Path) -> list[str]:
    out: list[str] = []
    prefix_q = shell_quote(str(prefix))
    pkgs_q = shell_quote(str(pkgs_dir))
    requirements_q = shell_quote(str(pkgs_dir / "swebench-requirements.txt"))
    for cmd in commands:
        stripped = cmd.strip()
        stripped = stripped.replace("$HOME/requirements.txt", requirements_q)
        stripped = stripped.replace("${HOME}/requirements.txt", requirements_q)
        if re.fullmatch(r"source\s+\S+/bin/activate", stripped):
            out.append(f"source {shell_quote(base + '/bin/activate')}")
            continue
        if stripped == "conda activate testbed":
            out.append(f"conda activate {prefix_q}")
            continue
        if "conda activate testbed" in stripped:
            out.append(stripped.replace("conda activate testbed", f"conda activate {prefix_q}"))
            continue
        if stripped.startswith("conda create -n testbed "):
            out.append(
                stripped.replace(
                    "conda create -n testbed",
                    f"CONDA_PKGS_DIRS={pkgs_q} conda create -p {prefix_q}",
                    1,
                )
            )
            continue
        out.append(stripped)
    return out


def run_create_script(script: str, log_path: Path) -> int:
    proc = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
    )
    log_path.write_text(
        f"$ bash -lc {shell_quote(script)}\n\n[stdout]\n{proc.stdout}\n\n[stderr]\n{proc.stderr}\n"
    )
    return proc.returncode


def _read_local_requirements(path: Path, seen: set[Path] | None = None) -> str:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return ""
    seen.add(path)
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("-r"):
            nested = stripped[len("-r") :].strip()
            if nested:
                nested_path = (path.parent / nested).resolve()
                if nested_path.is_file():
                    lines.append(_read_local_requirements(nested_path, seen))
            continue
        lines.append(line)
    return "\n".join(lines)


def install_local_requirements_fallback(workspace: Path) -> None:
    import swebench.harness.test_spec.python as swe_python

    original = swe_python.get_requirements_by_commit

    def get_requirements_by_commit(repo: str, commit: str) -> str:
        try:
            return original(repo, commit)
        except Exception as exc:
            for req_path in swe_python.MAP_REPO_TO_REQS_PATHS.get(repo, []):
                candidate = workspace / req_path
                if candidate.is_file():
                    print(
                        f"[setup] Remote requirements lookup failed ({exc}); "
                        f"using local {candidate}",
                        file=sys.stderr,
                    )
                    return _read_local_requirements(candidate)
            raise

    swe_python.get_requirements_by_commit = get_requirements_by_commit


def write_helper_script(path: Path, env_prefix: Path, base: str, cmd_log: Path) -> None:
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
                'if [ "$#" -eq 0 ]; then',
                '  if [ "$STDIN_PYTHON" != "1" ]; then',
                '    echo "usage: $0 [--reinstall] [--stdin-python] <command> [args...]" >&2',
                "    exit 2",
                "  fi",
                "fi",
                f'CMD_LOG={shell_quote(str(cmd_log))}',
                f'ENV_PREFIX={shell_quote(str(env_prefix))}',
                f'CONDA_BASE={shell_quote(base)}',
                'TMP_STDIN_PY=""',
                'cleanup() {',
                '  if [ -n "$TMP_STDIN_PY" ] && [ -f "$TMP_STDIN_PY" ]; then',
                '    rm -f "$TMP_STDIN_PY"',
                "  fi",
                "}",
                "trap cleanup EXIT",
                'REMAINING_ARGS=("$@")',
                "set --",
                'source "$CONDA_BASE/bin/activate"',
                'conda activate "$ENV_PREFIX"',
                'set -- "${REMAINING_ARGS[@]}"',
                'if [ "$MODE" = "reinstall" ]; then',
                "  python -m pip install -v -e .",
                "fi",
                'if [ "$STDIN_PYTHON" = "1" ]; then',
                '  TMP_STDIN_PY="$(mktemp "${TMPDIR:-/tmp}/swe-local-env-stdin-XXXXXX.py")"',
                '  cat >"$TMP_STDIN_PY"',
                '  USER_ARGS=(python "$TMP_STDIN_PY")',
                '  printf "\\n[%s] $ python <stdin>\\n" "$(date -Is)" | tee -a "$CMD_LOG"',
                "else",
                '  USER_ARGS=("$@")',
                '  printf "\\n[%s] $ %s\\n" "$(date -Is)" "$*" | tee -a "$CMD_LOG"',
                "fi",
                '"${USER_ARGS[@]}" | tee -a "$CMD_LOG"',
                "",
            ]
        )
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
    cache_root = Path(__file__).resolve().parent / ".cache"
    envs_root = cache_root / "swebench_envs"
    pkgs_root = cache_root / "conda_pkgs"
    locks_root = cache_root / "locks"
    envs_root.mkdir(parents=True, exist_ok=True)
    pkgs_root.mkdir(parents=True, exist_ok=True)
    locks_root.mkdir(parents=True, exist_ok=True)
    run_idx = (os.environ.get("RUN_IDX") or "").strip()
    use_per_run_env = os.environ.get("SWE_LOCAL_ENV_PER_RUN", "1") != "0"

    instance = json.loads(instance_path.read_text())
    install_local_requirements_fallback(workspace)
    test_spec = make_test_spec(instance, instance_image_tag="local")
    base = conda_base()
    base_env_key = sanitize(f"{instance.get('repo','repo').replace('/','__')}__{instance.get('version','unknown')}")
    env_key = base_env_key
    if use_per_run_env and run_idx:
        env_key = sanitize(f"{env_key}__r{run_idx}")
    env_prefix = envs_root / env_key
    base_env_prefix = envs_root / base_env_key
    pkgs_dir = pkgs_root / env_key
    base_pkgs_dir = pkgs_root / base_env_key
    env_meta = envs_root / f"{env_key}.json"
    base_activate = f"source {shell_quote(base + '/bin/activate')}"
    adapted_env_cmds = adapt_env_commands(test_spec.env_script_list, base, env_prefix, pkgs_dir)
    base_adapted_env_cmds = adapt_env_commands(test_spec.env_script_list, base, base_env_prefix, base_pkgs_dir)
    create_script = "\n".join(["set -euo pipefail", base_activate] + adapted_env_cmds) + "\n"
    base_create_script = "\n".join(["set -euo pipefail", base_activate] + base_adapted_env_cmds) + "\n"

    create_log = logs_dir / "swe-local-env-create.log"
    force_recreate = os.environ.get("SWE_LOCAL_ENV_RECREATE", "0") == "1"
    needs_create = force_recreate or not (env_prefix / "bin" / "python").exists()
    if needs_create:
        lock_path = locks_root / f"{base_env_key}.env.lock"
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)

            if use_per_run_env and run_idx:
                base_needs_create = force_recreate or not (base_env_prefix / "bin" / "python").exists()
                if base_needs_create:
                    if base_env_prefix.exists():
                        subprocess.run(["rm", "-rf", str(base_env_prefix)], check=True)
                    base_pkgs_dir.mkdir(parents=True, exist_ok=True)
                    rc = run_create_script(base_create_script, create_log)
                    if rc != 0:
                        return rc

                if env_prefix.exists():
                    subprocess.run(["rm", "-rf", str(env_prefix)], check=True)
                clone_script = "\n".join(
                    [
                        "set -euo pipefail",
                        base_activate,
                        f"conda create -p {shell_quote(str(env_prefix))} --clone {shell_quote(str(base_env_prefix))} -y",
                    ]
                ) + "\n"
                rc = run_create_script(clone_script, create_log)
                if rc != 0:
                    return rc
            else:
                if env_prefix.exists():
                    subprocess.run(["rm", "-rf", str(env_prefix)], check=True)
                pkgs_dir.mkdir(parents=True, exist_ok=True)
                rc = run_create_script(create_script, create_log)
                if rc != 0:
                    return rc

    helper_path = workspace / "RUN_IN_SWE_LOCAL_ENV.sh"
    cmd_log = logs_dir / "swe-local-env-commands.log"
    write_helper_script(helper_path, env_prefix, base, cmd_log)

    meta = {
        "instance_id": instance.get("instance_id"),
        "repo": instance.get("repo"),
        "version": instance.get("version"),
        "run_idx": run_idx or None,
        "per_run_env": use_per_run_env,
        "conda_base": base,
        "conda_pkgs_dir": str(pkgs_dir),
        "base_env_prefix": str(base_env_prefix),
        "base_conda_pkgs_dir": str(base_pkgs_dir),
        "env_prefix": str(env_prefix),
        "python": str(env_prefix / "bin" / "python"),
        "helper_script": str(helper_path),
        "create_log": str(create_log),
        "command_log": str(cmd_log),
        "derived_env_script_list": test_spec.env_script_list,
        "adapted_env_commands": adapted_env_cmds,
    }
    env_meta.write_text(json.dumps(meta, indent=2))
    info_path = experiment_dir / "swe_local_env.json"
    info_path.write_text(json.dumps(meta, indent=2))

    exports = {
        "SWE_TASK_PYTHON": str(env_prefix / "bin" / "python"),
        "SWE_TASK_ENV_PREFIX": str(env_prefix),
        "SWE_TASK_ENV_HELPER": str(helper_path),
        "SWE_TASK_ENV_INFO_JSON": str(info_path),
        "SWE_TASK_ENV_CREATE_LOG": str(create_log),
        "SWE_TASK_ENV_COMMAND_LOG": str(cmd_log),
    }
    for key, value in exports.items():
        print(f"export {key}={shell_quote(value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
