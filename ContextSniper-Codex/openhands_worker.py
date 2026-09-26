"""OpenHands SDK worker; stdout is the shared evaluator event protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from benchmark_lookup_guard import (
    LOG_ENV, MARKERS_ENV, SENSITIVE_ROOTS_ENV,
    command_lookup_violation, command_sensitive_path_violation,
)


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def guarded_command(command: str, config: dict, env: dict) -> dict | None:
    markers = json.loads(env[MARKERS_ENV])
    roots = [Path(p) for p in json.loads(env[SENSITIVE_ROOTS_ENV])]
    if not markers or not roots:
        raise RuntimeError("Missing benchmark guard configuration")
    violation = command_sensitive_path_violation(command, Path(config["workspace"]), roots)
    return violation or command_lookup_violation(command, markers)


def execute_shell(command: str, config: dict, env: dict) -> tuple[str, int]:
    violation = guarded_command(command, config, env)
    if violation:
        with Path(env[LOG_ENV]).open("a") as f:
            f.write(json.dumps({**violation, "decision": "deny", "timestamp": time.time()}) + "\n")
        return "Command blocked by benchmark PreToolUse policy. Use only permitted workspace evidence.", 126
    item = {"id": uuid.uuid4().hex, "type": "command_execution", "command": command,
            "status": "in_progress"}
    emit({"type": "item.started", "item": item})
    # timeout runs inside the container, so killing the client does not leave
    # an unlimited command behind. The parent always removes the container.
    completed = subprocess.run(
        ["docker", "exec", "--workdir", config["workspace"], config["container"],
         "timeout", str(config["command_timeout"]), "/bin/sh", "-lc", command],
        capture_output=True, text=True, errors="replace",
        timeout=config["command_timeout"] + 10,
    )
    output = (completed.stdout + completed.stderr)[-40000:]
    emit({"type": "item.completed", "item": {**item, "status": "completed",
          "exit_code": completed.returncode, "aggregated_output": output}})
    return output, completed.returncode


class EventBridge:
    def __init__(self, raw_path: Path):
        self.raw_path = raw_path
        self.actions: dict[str, dict] = {}
        self.last_response = ""

    def __call__(self, event):
        data = event.model_dump(mode="json")
        with self.raw_path.open("a") as f:
            f.write(json.dumps(data) + "\n")
        kind = type(event).__name__
        name = data.get("tool_name", "")
        tool = next((t for t in ("search_code", "edit_file")
                     if name == t or name.endswith("_" + t)), None)
        call_id = data.get("tool_call_id", "")
        if kind == "ActionEvent" and name == "finish":
            self.last_response = (data.get("action") or {}).get("message", "")
        if kind == "ActionEvent" and tool:
            arguments = (data.get("action") or {}).get("data", data.get("action") or {})
            arguments.pop("kind", None)
            self.actions[call_id] = {"id": call_id, "type": "mcp_tool_call",
                "server": "contextsniper", "tool": tool, "arguments": arguments}
            emit({"type": "item.started", "item": {**self.actions[call_id], "status": "in_progress"}})
        elif tool and kind in {"ObservationEvent", "AgentErrorEvent", "UserRejectObservation"}:
            if call_id not in self.actions:
                raise RuntimeError("MCP observation without matching action")
            obs = data.get("observation") or {}
            failed = kind != "ObservationEvent" or bool(obs.get("is_error") or obs.get("isError"))
            emit({"type": "item.completed", "item": {**self.actions.pop(call_id),
                "status": "failed" if failed else "completed",
                "result": obs, "error": {"message": str(data.get("error") or obs)} if failed else None}})
        elif kind == "MessageEvent" and data.get("source") == "agent":
            message = data.get("llm_message") or {}
            self.last_response = "\n".join(c.get("text", "") for c in message.get("content", []))
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": self.last_response}})


def main() -> int:
    from pydantic import Field, SecretStr
    from openhands.sdk import LLM, Agent, Action, Observation, TextContent, ToolDefinition
    from openhands.sdk.tool import Tool, ToolExecutor, register_tool

    config = json.loads(Path(sys.argv[1]).read_text())
    prompt = sys.stdin.read()
    env = dict(os.environ)

    class ShellAction(Action):
        command: str = Field(description="Shell command to execute in the isolated case workspace.")

    class ShellObservation(Observation):
        output: str
        exit_code: int

        @property
        def to_llm_content(self):
            return [TextContent(text=f"Exit code: {self.exit_code}\n{self.output}")]

    class ShellExecutor(ToolExecutor):
        def __call__(self, action, conversation=None):
            output, code = execute_shell(action.command, config, env)
            return ShellObservation(output=output, exit_code=code)

    class WorkspaceShellTool(ToolDefinition[ShellAction, ShellObservation]):
        @classmethod
        def create(cls, conv_state, **kwargs):
            return [cls(description="Run a shell command in the case workspace. Parent/benchmark access is forbidden.",
                        action_type=ShellAction, observation_type=ShellObservation,
                        executor=ShellExecutor())]

    register_tool("WorkspaceShellTool", WorkspaceShellTool)
    if config["auth"] == "subscription":
        llm = LLM.subscription_login(vendor="openai", model=config["model"])
        if config["reasoning_effort"]:
            llm.reasoning_effort = config["reasoning_effort"]
    else:
        key_name = "OPENROUTER_API_KEY" if config["provider"] == "openrouter" else "OPENAI_API_KEY"
        kwargs = {"model": config["provider"] + "/" + config["model"],
                  "api_key": SecretStr(env[key_name]), "usage_id": "repair", "api_mode": "responses"}
        if config["reasoning_effort"]:
            kwargs["reasoning_effort"] = config["reasoning_effort"]
        llm = LLM(**kwargs)
    launcher = Path(__file__).resolve().parent.parent / "claude-plugin/bin/contextsniper-mcp"
    # Same explicit environment allowlist used by the Codex MCP configuration.
    from contextsniper_codex import build_codex_command
    command = build_codex_command(codex_bin=Path("codex"), workspace=Path(config["workspace"]),
                                 response_path=Path(config["response"]), model=config["model"], reasoning_effort=None)
    env_setting = next(x for x in command if x.startswith("mcp_servers.contextsniper.env_vars="))
    names = json.loads(env_setting.split("=", 1)[1])
    mcp_env = {name: env[name] for name in names if name in env}
    agent = Agent(llm=llm, tools=[Tool(name="WorkspaceShellTool")],
                  mcp_config={"contextsniper": {"command": str(launcher), "env": mcp_env}},
                  filter_tools_regex=r"^(?:workspace_shell|(?:contextsniper[_:]+)?(?:search_code|edit_file))$")
    # The benchmark must not load host/project plugins that can add unguarded
    # tools or execute hooks. SDK 1.49.5 has no public discovery-off switch.
    import openhands.sdk.conversation.impl.local_conversation as local_impl
    import openhands.sdk.agent.base as agent_base
    local_impl.load_available_plugins = lambda **kwargs: {}
    local_impl.load_available_skills = lambda **kwargs: {}
    agent_base.has_vision_profile_available = lambda: False
    bridge = EventBridge(Path(config["raw_events"]))
    emit({"type": "thread.started", "thread_id": "openhands-" + uuid.uuid4().hex})
    state_dir = Path(config["raw_events"]).parent / "openhands-state"
    conversation = local_impl.LocalConversation(agent=agent, workspace=config["workspace"], callbacks=[bridge],
                                visualizer=None, persistence_dir=str(state_dir),
                                profile_store_dir=str(state_dir / "profiles"))
    try:
        conversation.send_message(prompt)
        conversation.run()
        if str(conversation.state.execution_status.value) != "finished":
            raise RuntimeError(f"OpenHands did not finish: {conversation.state.execution_status}")
        Path(config["response"]).write_text(bridge.last_response)
        metrics = llm.metrics.model_dump(mode="json")
        Path(config["raw_events"]).with_name("openhands-metrics.json").write_text(json.dumps(metrics, indent=2))
        usage = (metrics.get("accumulated_token_usage") or {})
        emit({"type": "turn.completed", "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cached_input_tokens": usage.get("cache_read_tokens", 0),
        }})
    finally:
        conversation.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
