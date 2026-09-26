from __future__ import annotations
import contextlib
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import contextsniper_codex as runner
import openhands_adapter as adapter
import openhands_worker as worker


class OpenHandsTests(unittest.TestCase):
    def test_subscription_is_default(self):
        args = runner.parse_args(["CVE-2016-3132__28a6ed9f9a36", "--agent", "openhands"])
        self.assertEqual(args.openhands_auth, "subscription")

    def test_cli_preflight_error_is_json(self):
        completed = subprocess.run([sys.executable, str(ROOT / "contextsniper_codex.py"),
            "CVE-2016-3132__28a6ed9f9a36", "--agent", "openhands",
            "--openhands-python", "/nonexistent/openhands-python"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["stage"], "preflight")
        self.assertNotIn("Traceback", completed.stderr)

    def test_shared_plan(self):
        args = runner.parse_args(["CVE-2016-3132__28a6ed9f9a36", "--agent", "openhands", "--dry-run"])
        config = runner.resolve_agent_config(args)
        self.assertEqual(config.model, runner.DEFAULT_MODEL)
        self.assertEqual(config.reasoning_effort, runner.DEFAULT_REASONING_EFFORT)
        _, oh = runner.execute(args)
        args.agent = "codex"
        _, codex = runner.execute(args)
        self.assertEqual(oh["contextsniper_settings"], codex["contextsniper_settings"])
        self.assertNotEqual(oh["agent_harness_profile"], codex["agent_harness_profile"])

    def test_denied_shell_never_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); workspace = root / "workspace"; workspace.mkdir()
            config = {"workspace": str(workspace)}
            env = {worker.MARKERS_ENV: '["CVE-2016-3132"]',
                   worker.SENSITIVE_ROOTS_ENV: json.dumps([str(root)]),
                   worker.LOG_ENV: str(root / "guard.jsonl")}
            with mock.patch.object(worker.subprocess, "run") as run:
                _, code = worker.execute_shell(f"find {root} -name '*.patch'", config, env)
                self.assertEqual(code, 126); run.assert_not_called()
            events = root / "events.jsonl"; events.write_text("")
            audit = runner.groundtruth_access_audit(events, workspace, guard_log_path=Path(env[worker.LOG_ENV]))
            self.assertTrue(audit["passed"])
            self.assertEqual(len(audit["blocked_attempts"]), 1)

    def test_only_workspace_is_mounted(self):
        cmd = adapter.container_command("test", Path("/tmp/workspace"), "image")
        self.assertEqual(cmd.count("--mount"), 1)
        self.assertIn("--read-only", cmd)
        self.assertIn("--pull=never", cmd)
        self.assertNotIn("docker.sock", " ".join(cmd))

    @unittest.skipUnless(os.environ.get("CONTEXTSNIPER_TEST_DOCKER_IMAGE"), "Opt-in Docker integration")
    def test_docker_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); workspace = root / "workspace"; workspace.mkdir()
            (root / "outside.txt").write_text("private")
            name = "contextsniper-oh-test-" + uuid.uuid4().hex[:12]
            config = {"workspace": str(workspace), "container": name, "command_timeout": 2}
            env = {worker.MARKERS_ENV: '["CVE-2016-3132"]',
                   worker.SENSITIVE_ROOTS_ENV: json.dumps([str(root)]),
                   worker.LOG_ENV: str(root / "guard.jsonl")}
            try:
                subprocess.run(adapter.container_command(name, workspace, os.environ["CONTEXTSNIPER_TEST_DOCKER_IMAGE"]),
                               check=True, capture_output=True, timeout=60)
                with contextlib.redirect_stdout(io.StringIO()):
                    _, code = worker.execute_shell("echo changed > sample.txt; test -z \"$OPENAI_API_KEY\"", config, env)
                    self.assertEqual(code, 0)
                    self.assertEqual((workspace / "sample.txt").read_text(), "changed\n")
                    _, code = worker.execute_shell("sleep 10", config, env)
                    self.assertEqual(code, 124)
                check = subprocess.run(["docker", "exec", name, "test", "!", "-e", str(root / "outside.txt")])
                self.assertEqual(check.returncode, 0)
            finally:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)

    def test_sdk_tool_cycle(self):
        try:
            from openhands.sdk import Agent, LLM
            from openhands.sdk.llm import Message, MessageToolCall, TextContent
            from openhands.sdk.testing import TestLLM
        except ImportError:
            self.skipTest("Run with .venv-openhands for SDK integration")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); workspace = root / "workspace"; workspace.mkdir()
            fixture = root / "mcp_fixture.py"
            fixture.write_text('''from mcp.server.fastmcp import FastMCP
from pathlib import Path
import os
m = FastMCP("contextsniper")
@m.tool()
def search_code(query: str) -> str:
    return "sample.py: value = 1"
@m.tool()
def edit_file(file_path: str, content: str) -> str:
    Path(os.environ["FIXTURE_WORKSPACE"], file_path).write_text(content)
    return "edited"
m.run()
''')
            config = {"workspace": str(workspace), "response": str(root / "response.txt"),
                      "raw_events": str(root / "raw.jsonl"), "container": "unused",
                      "model": runner.DEFAULT_MODEL, "provider": "openai", "auth": "api-key",
                      "reasoning_effort": "low", "command_timeout": 10}
            cfg = root / "config.json"; cfg.write_text(json.dumps(config))
            messages = []
            for i, (name, args) in enumerate([
                ("search_code", {"query": "sample"}),
                ("edit_file", {"file_path": "sample.py", "content": "value = 2\n"}),
                ("finish", {"message": "Done"}),
            ]):
                messages.append(Message(role="assistant", content=[TextContent(text="")],
                    tool_calls=[MessageToolCall(id=f"call_{i}", name=name,
                        arguments=json.dumps(args), origin="completion")]))
            llm = TestLLM.from_messages(messages)
            def make_agent(**kwargs):
                kwargs["mcp_config"] = {"contextsniper": {"command": sys.executable,
                    "args": [str(fixture)], "env": {"FIXTURE_WORKSPACE": str(workspace)}}}
                return Agent(**kwargs)
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-real-key"}), \
                 mock.patch("openhands.sdk.LLM", return_value=llm), \
                 mock.patch("openhands.sdk.Agent", side_effect=make_agent), \
                 mock.patch.object(sys, "argv", ["worker", str(cfg)]), \
                 mock.patch.object(sys, "stdin", io.StringIO("Fix sample.py")), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(worker.main(), 0)
            events = root / "events.jsonl"; events.write_text(output.getvalue())
            usage = runner.contextsniper_mcp_usage(events)
            self.assertTrue(usage["search_succeeded"])
            self.assertTrue(usage["edit_succeeded"])
            self.assertEqual((workspace / "sample.py").read_text(), "value = 2\n")
            self.assertEqual((root / "response.txt").read_text(), "Done")

    def test_real_contextsniper_mcp_discovery(self):
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.skipTest("MCP package required")
        async def check():
            params = StdioServerParameters(
                command=str(ROOT.parent / "claude-plugin/bin/contextsniper-mcp"),
                env={"CONTEXTSNIPER_PLUGIN_AUTO_START": "0", "CONTEXTSNIPER_PLUGIN_AUTO_STOP": "0",
                     "PY_BIN": str(ROOT.parent / ".venv/bin/python")})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self.assertTrue({"search_code", "edit_file"}.issubset({t.name for t in listing.tools}))
        asyncio.run(asyncio.wait_for(check(), timeout=30))


if __name__ == "__main__":
    unittest.main()
