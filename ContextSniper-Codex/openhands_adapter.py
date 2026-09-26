"""Host-side OpenHands launcher. No SDK dependency in the baseline interpreter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

HERE = Path(__file__).resolve().parent


def preflight_openhands(args, config, env) -> Path:
    from runner_errors import RunnerError

    python = Path(args.openhands_python).expanduser().absolute()
    if not python.is_file():
        raise RunnerError("OpenHands interpreter missing; install requirements-openhands.lock in .venv-openhands")
    probe = subprocess.run([str(python), "-c", "from importlib.metadata import version; assert version('openhands-sdk') == '1.49.5'; import openhands.sdk"],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode:
        raise RunnerError("OpenHands SDK 1.49.5 is required in --openhands-python; install requirements-openhands.lock")
    if args.openhands_auth == "subscription":
        if config.model_provider != "openai":
            raise RunnerError("OpenHands subscription authentication requires the openai provider")
        auth_probe = subprocess.run([str(python), "-c",
            "from openhands.sdk.llm.auth.credentials import get_credentials_dir; "
            "raise SystemExit(not (get_credentials_dir() / 'openai_oauth.json').is_file())"],
            capture_output=True, text=True, timeout=60, env=env)
        if auth_probe.returncode:
            raise RunnerError("Sign in with your ChatGPT account first: run .venv-openhands/bin/python -c 'from openhands.sdk import LLM; LLM.subscription_login(vendor=\"openai\", model=\"gpt-5.6-sol\")' from ContextSniper-Codex. No OPENAI_API_KEY is needed.")
    else:
        key = "OPENROUTER_API_KEY" if config.model_provider == "openrouter" else "OPENAI_API_KEY"
        if not env.get(key, "").strip():
            raise RunnerError(f"{key} is required for OpenHands API-key authentication")
    if not shutil.which("docker"):
        raise RunnerError("OpenHands shell isolation requires Docker")
    subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=30)
    return python


def container_command(name: str, workspace: Path, image: str) -> list[str]:
    # Only the disposable case is mounted. Neither credentials, runner logs,
    # prepared inputs, the Docker socket nor the host home are exposed.
    return ["docker", "run", "--detach", "--rm", "--pull=never", "--name", name,
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev", "--workdir", str(workspace),
            "--mount", f"type=bind,src={workspace},dst={workspace}",
            "--entrypoint", "/bin/sh", image, "-c", "while :; do sleep 3600; done"]


def run_openhands(*, python_bin, args, agent_config, case, paths, prompt, env):
    from runner_errors import RunnerError
    from contextsniper_codex import run_codex

    if case.environment_mode != "image" or not case.environment_image:
        raise RunnerError("OpenHands currently requires a prepared image-mode case")
    name = "contextsniper-oh-" + uuid.uuid4().hex[:16]
    subprocess.run(["docker", "image", "inspect", case.environment_image],
                   check=True, capture_output=True, timeout=60)
    config = {
        "workspace": str(paths.workspace), "response": str(paths.response),
        "raw_events": str(paths.logs / "openhands-events.jsonl"),
        "container": name, "model": agent_config.model,
        "provider": agent_config.model_provider, "reasoning_effort": agent_config.reasoning_effort,
        "auth": args.openhands_auth,
        "command_timeout": args.command_timeout,
    }
    config_path = paths.logs / "openhands-config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    command = [str(python_bin), str(HERE / "openhands_worker.py"), str(config_path)]
    try:
        subprocess.run(container_command(name, paths.workspace, case.environment_image),
                       check=True, capture_output=True, text=True, timeout=60)
        return run_codex(command, prompt=prompt, workspace=paths.workspace, env=env,
                         events_path=paths.events, stderr_path=paths.stderr,
                         timeout=args.codex_timeout, agent_label="OpenHands")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
