from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ADAPTER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ADAPTER_ROOT.parent
MODULE_PATH = ADAPTER_ROOT / "contextsniper_codex.py"
SPEC = importlib.util.spec_from_file_location("contextsniper_codex", MODULE_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

from evaluator.validator import BuildPlan, CommandResult, CommandSpec  # noqa: E402


EXPECTED_PLAIN_SETTINGS = {
    "CONTEXTSNIPER_CODE_TOGGLE_FORCE": "true",
    "CONTEXTSNIPER_SEARCH_LIMIT": "",
    "CONTEXTSNIPER_SEARCH_FORCE_LIMIT": "",
    "VECTOR_DB_TYPE": "memory",
    "CONTEXTSNIPER_CODE_TOGGLE": "true",
    "EMBEDDING_PROVIDER": "openai",
    "CONTEXTSNIPER_EMBEDDING_MODEL": "openai/text-embedding-3-small",
    "CONTEXTSNIPER_EMBEDDING_BASE_URL": "https://openrouter.ai/api/v1",
    "CONTEXTSNIPER_EMBEDDING_PROBE_REQUIRED": "0",
    "CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED": "1",
    "CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED": "1",
    "CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED": "1",
    "CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED": "1",
    "CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES": "80",
    "CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES": "80",
    "CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS": "500",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES": "1",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC": "1",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES": "3",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS": "40",
    "CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS": "2",
    "CONTEXTSNIPER_CODE_FUSE_MODE": "weighted_rrf",
    "CONTEXTSNIPER_CODE_FUSE_W_EMBED": "0.33",
    "CONTEXTSNIPER_CODE_FUSE_W_BM25": "0.17",
    "CONTEXTSNIPER_CODE_FUSE_W_CTAGS": "0.17",
    "CONTEXTSNIPER_CODE_FUSE_W_GRAPH": "0.33",
    "CONTEXTSNIPER_BOOTSTRAP_MAX_FILES": "40",
    "CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES": "40",
    "CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION": "1",
    "CONTEXTSNIPER_START_LOCAL_EMBED_SERVER": "0",
    "CONTEXTSNIPER_PLUGIN_AUTO_START": "1",
    "CONTEXTSNIPER_PLUGIN_AUTO_STOP": "1",
    "CONTEXTSNIPER_PLUGIN_START_WAIT": "60",
    "CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME": "1",
    "CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT": "0",
    "CONTEXTSNIPER_APPEND_CODE_POLICY_TO_TASK": "1",
    "CONTEXTSNIPER_FILTER_ENABLED": "0",
    "CONTEXTSNIPER_FILTER_NATIVE_READ": "0",
    "CONTEXTSNIPER_FILTER_NATIVE_BASH": "0",
    "CONTEXTSNIPER_INJECT_FILTERING_PROMPT": "0",
    "CONTEXTSNIPER_FORCE_EMBED_DIM_ALIGN": "1",
    "OPENGAUSS_DIMENSION": "1536",
}


class ContextSniperCodexTests(unittest.TestCase):
    def _prepared_case(self, root: Path, case_id: str = "CVE-TEST__abc123") -> Path:
        inputs = root / "out_tmp_dirs" / "debugging_framework" / "php" / "inputs"
        project = inputs / case_id
        project.mkdir(parents=True)
        (project / "sample.c").write_text("int value = 0;\n", encoding="utf-8")
        config = {
            "schema_version": 6,
            "repair": {"failing_tests": ["ext/example/failure.phpt"]},
            "environment": {"mode": "image", "runtime": "docker", "image": "sha256:test"},
            "workspace": {"disposable": True, "initialize_git_if_missing": True},
        }
        (inputs / f"{case_id}.debugging-framework.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        (inputs / f"{case_id}.failure.log").write_text(
            "FAILED ext/example/failure.phpt\n", encoding="utf-8"
        )
        return inputs

    def test_resolve_case_uses_prepared_triplet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepared_case(root)
            case = runner.resolve_case(root, "CVE-TEST__abc123")
            self.assertEqual(case.failing_tests, ("ext/example/failure.phpt",))
            self.assertEqual(case.environment_runtime, "docker")
            self.assertTrue(case.project.is_dir())

    def test_inputs_dir_supports_other_prepared_dataset_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = self._prepared_case(root)
            case = runner.resolve_case(
                root / "dataset-root-is-not-used",
                "CVE-TEST__abc123",
                inputs,
            )
            self.assertEqual(case.case_id, "CVE-TEST__abc123")
            self.assertEqual(case.project.parent, inputs.resolve())

    def test_inputs_dir_supports_swe_case_bundle_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "debugging" / "out" / "fmtlib"
            case_id = "fmtlib__fmt-1683"
            bundle = inputs / case_id
            project = bundle / case_id
            project.mkdir(parents=True)
            (project / "sample.cc").write_text("int value = 0;\n", encoding="utf-8")
            generated = project / ".debugging-framework"
            generated.mkdir()
            (generated / "repo-map.md").write_text(
                "prior-run context", encoding="utf-8"
            )
            (bundle / "config.json").write_text(
                json.dumps(
                    {
                        "schema_version": 6,
                        "repair": {"failing_tests": ["PrintfTest.MinusFlag"]},
                        "environment": {
                            "mode": "image",
                            "runtime": "docker",
                            "image": "swebench/fmt:latest",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (bundle / "failure.log").write_text(
                "FAILED PrintfTest.MinusFlag\n", encoding="utf-8"
            )

            case = runner.resolve_case(
                root / "unused", case_id, inputs
            )
            self.assertEqual(case.project, project.resolve())
            self.assertEqual(case.config, (bundle / "config.json").resolve())
            self.assertEqual(case.failing_tests, ("PrintfTest.MinusFlag",))

            paths = runner.make_run_paths(root / "output", case_id)
            runtime_config = runner._copy_inputs(case, paths)
            copied = json.loads(runtime_config.read_text(encoding="utf-8"))
            self.assertTrue(copied["workspace"]["disposable"])
            self.assertTrue(copied["workspace"]["initialize_git_if_missing"])
            self.assertTrue((paths.workspace / "sample.cc").is_file())
            self.assertFalse((paths.workspace / ".debugging-framework").exists())
            self.assertTrue(
                (paths.input_dir / f"{case_id}.failure.log").is_file()
            )

    def test_prompt_contains_failure_and_existing_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepared_case(root)
            case = runner.resolve_case(root, "CVE-TEST__abc123")
            prompt = runner.build_prompt(case, "FAILED ext/example/failure.phpt\n", "POLICY")
            self.assertIn("FAILED ext/example/failure.phpt", prompt)
            self.assertIn("POLICY", prompt)
            self.assertIn("search_code", prompt)
            self.assertIn("edit_file", prompt)
            self.assertIn("Do not inspect parent or sibling directories", prompt)
            self.assertIn("General web research", prompt)
            self.assertIn("Never put the case/CVE identifier", prompt)
            self.assertNotIn("run_shell_command", prompt)
            self.assertNotIn("mcp__contextsniper__search_code", prompt)
            self.assertNotIn("Final Codex runtime contract", prompt)

    def test_shared_plain_preset_matches_original_runner_settings(self) -> None:
        env = runner.load_plain_contextsniper_settings({})
        actual = {name: env.get(name, "") for name in runner.PLAIN_SETTING_NAMES}
        self.assertEqual(actual, EXPECTED_PLAIN_SETTINGS)
        original_runner = (
            REPOSITORY_ROOT
            / "scripts/SWE/claude/ContextSniper/run_swe_task_lite_contextsniper_plugin.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("contextsniper_plain_defaults.sh", original_runner)

    def test_plain_environment_keeps_shared_values_and_adds_run_identity(self) -> None:
        plain = runner.load_plain_contextsniper_settings(
            {"CONTEXTSNIPER_EMBEDDING_API_KEY": "secret", "PYTHONPATH": "/existing"}
        )
        env = runner.build_contextsniper_env(
            plain,
            workspace=Path("/tmp/workspace"),
            runtime_dir=Path("/tmp/runtime"),
            python_bin=Path("/tmp/python"),
            contextsniper_port=18090,
            agfs_port=11833,
            run_id="run-1",
        )
        for name, expected in EXPECTED_PLAIN_SETTINGS.items():
            self.assertEqual(env.get(name, ""), expected, name)
        self.assertEqual(env["PYTHONPATH"], "/tmp/workspace:/existing")
        self.assertEqual(env["CONTEXTSNIPER_PLUGIN_AUTO_START"], "1")
        self.assertEqual(env["CONTEXTSNIPER_PLUGIN_AUTO_STOP"], "1")
        self.assertEqual(env["CONTEXTSNIPER_SESSION_ID"], "defects4c-run-1")
        self.assertEqual(env["CONTEXTSNIPER_ENFORCE_WORKSPACE_ROOT"], "1")

    def test_public_settings_never_expose_embedding_key(self) -> None:
        plain = runner.load_plain_contextsniper_settings(
            {"CONTEXTSNIPER_EMBEDDING_API_KEY": "example-secret-value"}
        )
        public = runner.public_plain_settings(plain)
        self.assertTrue(public["CONTEXTSNIPER_EMBEDDING_API_KEY_CONFIGURED"])
        self.assertNotIn("example-secret-value", json.dumps(public))

    def test_openrouter_key_is_mapped_without_being_exposed(self) -> None:
        plain = runner.load_plain_contextsniper_settings(
            {"OPENROUTER_API_KEY": "openrouter-secret-value"}
        )
        self.assertEqual(
            plain["CONTEXTSNIPER_EMBEDDING_API_KEY"], "openrouter-secret-value"
        )
        public = runner.public_plain_settings(plain)
        self.assertTrue(public["CONTEXTSNIPER_EMBEDDING_API_KEY_CONFIGURED"])
        self.assertNotIn("openrouter-secret-value", json.dumps(public))

    def test_python_selection_preserves_virtualenv_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_python = root / "base-python"
            base_python.write_text("", encoding="utf-8")
            venv_python = root / "venv-python"
            venv_python.symlink_to(base_python)
            expected = Path(os.path.abspath(venv_python))

            with mock.patch.object(
                runner,
                "_python_has_contextsniper_deps",
                side_effect=lambda candidate: candidate == expected,
            ):
                selected = runner.choose_contextsniper_python(str(venv_python))

            self.assertEqual(selected, expected)
            self.assertTrue(selected.is_symlink())

    def test_codex_command_uses_sol_low_and_only_contextsniper_mcp(self) -> None:
        args = runner.parse_args(["CVE-TEST__abc123"])
        agent_config = runner.resolve_agent_config(args)
        command = runner.build_codex_command(
            codex_bin=Path("/usr/local/bin/codex"),
            workspace=Path("/tmp/workspace"),
            response_path=Path("/tmp/response.txt"),
            model=agent_config.model,
            reasoning_effort=agent_config.reasoning_effort,
            model_provider=agent_config.model_provider,
        )
        rendered = "\n".join(command)
        self.assertEqual(agent_config.agent, "codex")
        self.assertEqual(agent_config.model_provider, "openai")
        self.assertEqual(runner.DEFAULT_MODEL, "gpt-5.6-sol")
        self.assertEqual(runner.DEFAULT_REASONING_EFFORT, "low")
        self.assertIn("mcp_servers.contextsniper.required=true", rendered)
        self.assertIn(
            'mcp_servers.contextsniper.enabled_tools=["search_code","edit_file"]',
            rendered,
        )
        self.assertIn(
            'mcp_servers.contextsniper.default_tools_approval_mode="approve"',
            rendered,
        )
        self.assertIn('model_reasoning_effort="low"', rendered)
        self.assertIn("gpt-5.6-sol", command)
        self.assertIn("agents.enabled=false", rendered)
        self.assertIn("multi_agent", command)
        self.assertIn('default_permissions="contextsniper_eval"', rendered)
        self.assertIn(
            'permissions.contextsniper_eval.filesystem={":minimal"="read",'
            '":workspace_roots"={"."="write"}}',
            rendered,
        )
        self.assertNotIn('sandbox_mode="workspace-write"', rendered)
        self.assertIn("CONTEXTSNIPER_EMBEDDING_API_KEY", rendered)
        self.assertIn("CONTEXTSNIPER_ENFORCE_WORKSPACE_ROOT", rendered)
        self.assertIn("hooks.PreToolUse=", rendered)
        self.assertIn("benchmark_lookup_guard.py", rendered)
        self.assertIn("--dangerously-bypass-hook-trust", command)
        self.assertNotIn("--strict-config", command)
        self.assertNotIn("model_providers.openrouter", rendered)
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--disable"
            ],
            ["multi_agent"],
        )
        self.assertNotIn("example-secret-value", rendered)

    def test_codex_uses_deepseek_via_openrouter_with_low_reasoning(self) -> None:
        args = runner.parse_args(
            [
                "CVE-TEST__abc123",
                "--agent",
                "codex",
                "--model",
                "deepseek/deepseek-v4-flash-0731",
            ]
        )
        agent_config = runner.resolve_agent_config(args)
        command = runner.build_codex_command(
            codex_bin=Path("/usr/local/bin/codex"),
            workspace=Path("/tmp/workspace"),
            response_path=Path("/tmp/response.txt"),
            model=agent_config.model,
            reasoning_effort=agent_config.reasoning_effort,
            model_provider=agent_config.model_provider,
        )
        rendered = "\n".join(command)

        self.assertEqual(agent_config.agent, "codex")
        self.assertEqual(agent_config.model_provider, "openrouter")
        self.assertEqual(agent_config.model, "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(agent_config.reasoning_effort, "low")
        self.assertIn('model_provider="openrouter"', rendered)
        self.assertIn(
            'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
            rendered,
        )
        self.assertIn(
            'model_providers.openrouter.env_key="OPENROUTER_API_KEY"', rendered
        )
        self.assertIn('model_providers.openrouter.wire_api="responses"', rendered)
        self.assertIn('web_search="disabled"', rendered)
        self.assertIn("model_catalog_json=", rendered)
        self.assertIn("deepseek-v4-flash-0731.json", rendered)
        self.assertIn(
            f"model_context_window={runner.OPENROUTER_MODEL_CONTEXT_WINDOW}", rendered
        )
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--disable"
            ],
            ["multi_agent"],
        )
        self.assertNotIn("model_supports_reasoning_summaries", rendered)
        self.assertIn('model_reasoning_effort="low"', rendered)
        self.assertIn("deepseek/deepseek-v4-flash-0731", command)
        catalog = json.loads(runner.OPENROUTER_MODEL_CATALOG.read_text())
        self.assertEqual(catalog["models"][0]["slug"], agent_config.model)
        self.assertEqual(catalog["models"][0]["default_reasoning_level"], "low")
        self.assertEqual(
            [level["effort"] for level in catalog["models"][0]["supported_reasoning_levels"]],
            ["low", "high", "max"],
        )
        self.assertEqual(catalog["models"][0]["tool_mode"], "direct")
        self.assertNotIn("web_search_tool_type", catalog["models"][0])
        self.assertFalse(catalog["models"][0]["supports_search_tool"])
        instructions = catalog["models"][0]["base_instructions"]
        self.assertIn("omit justification, sandbox_permissions, and prefix_rule", instructions)
        self.assertIn("not arbitrary file paths", instructions)
        self.assertTrue(catalog["models"][0]["include_plugin_usage_instructions"])
        self.assertTrue(catalog["models"][0]["include_apps_usage_instructions"])

    def test_agent_model_and_reasoning_can_be_overridden(self) -> None:
        args = runner.parse_args(
            [
                "CVE-TEST__abc123",
                "--model",
                "vendor/custom-model",
                "--reasoning-effort",
                "high",
            ]
        )
        agent_config = runner.resolve_agent_config(args)
        self.assertEqual(agent_config.model, "vendor/custom-model")
        self.assertEqual(agent_config.reasoning_effort, "high")

    def test_deepseek_preserves_gpt_contextsniper_and_permission_configuration(self) -> None:
        common = dict(
            codex_bin=Path("/usr/local/bin/codex"),
            workspace=Path("/tmp/workspace"),
            response_path=Path("/tmp/response.txt"),
        )
        gpt = runner.build_codex_command(
            **common, model=runner.DEFAULT_MODEL, reasoning_effort="low"
        )
        deepseek = runner.build_codex_command(
            **common, model=runner.DEFAULT_OPENROUTER_MODEL,
            model_provider="openrouter", reasoning_effort="low",
        )

        def harness_arguments(command: list[str]) -> list[str]:
            result = []
            index = 0
            while index < len(command):
                flag = command[index]
                if flag == "--model":
                    index += 2
                    continue
                if flag == "-c":
                    setting = command[index + 1]
                    if setting.startswith((
                        "model_provider=", "model_providers.", "model_catalog_json=",
                        "model_context_window=", "model_reasoning_effort=", "web_search=",
                    )):
                        index += 2
                        continue
                result.append(flag)
                index += 1
            return result

        self.assertEqual(harness_arguments(gpt), harness_arguments(deepseek))

    def test_deepseek_model_requires_openrouter_key_for_real_runs(self) -> None:
        args = runner.parse_args(
            [
                "CVE-TEST__abc123",
                "--model",
                "deepseek/deepseek-v4-flash-0731",
            ]
        )
        agent_config = runner.resolve_agent_config(args)
        with self.assertRaisesRegex(runner.RunnerError, "OPENROUTER_API_KEY"):
            runner.ensure_agent_credentials(agent_config, {})
        runner.ensure_agent_credentials(
            agent_config, {"OPENROUTER_API_KEY": "secret-not-logged"}
        )

    def test_repeated_unsupported_tool_calls_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = runner.run_codex(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time;"
                        "sys.stderr.write('unsupported call: run_shell_command\\n' * 5);"
                        "sys.stderr.flush();time.sleep(30)"
                    ),
                ],
                prompt="test",
                workspace=root,
                env=os.environ.copy(),
                events_path=root / "events.jsonl",
                stderr_path=root / "stderr.log",
                timeout=20,
            )
        self.assertFalse(result.timed_out)
        self.assertIsNotNone(result.compatibility_error)
        self.assertIn("run_shell_command x5", result.compatibility_error or "")
        self.assertLess(result.elapsed_seconds, 5)

    def test_repeated_permission_schema_errors_fail_fast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = runner.run_codex(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time;"
                        "sys.stderr.write("
                        "'`justification` requires an explicit `sandbox_permissions`\\n' * 5);"
                        "sys.stderr.flush();time.sleep(30)"
                    ),
                ],
                prompt="test",
                workspace=root,
                env=os.environ.copy(),
                events_path=root / "events.jsonl",
                stderr_path=root / "stderr.log",
                timeout=20,
            )
        self.assertIsNotNone(result.compatibility_error)
        self.assertIn("schema mismatch", result.compatibility_error or "")
        self.assertLess(result.elapsed_seconds, 5)

    def test_live_audit_stops_exact_benchmark_network_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event = json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": (
                            'curl -s "https://bugs.php.net/'
                            'bug.php?id=71735"'
                        ),
                    },
                }
            )
            result = runner.run_codex(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time;"
                        f"sys.stdout.write({event!r} + '\\n');"
                        "sys.stdout.flush();time.sleep(30)"
                    ),
                ],
                prompt="test",
                workspace=root,
                env=os.environ.copy(),
                events_path=root / "events.jsonl",
                stderr_path=root / "stderr.log",
                timeout=20,
                lookup_markers=("71735",),
            )
        self.assertIsNotNone(result.policy_violation)
        self.assertEqual(result.policy_violation["marker"], "71735")
        self.assertLess(result.elapsed_seconds, 5)

    def test_openrouter_provider_without_model_defaults_to_deepseek(self) -> None:
        config = runner.resolve_agent_config(
            runner.parse_args(
                ["CVE-TEST__abc123", "--model-provider", "openrouter"]
            )
        )
        self.assertEqual(config.agent, "codex")
        self.assertEqual(config.model_provider, "openrouter")
        self.assertEqual(config.model, "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(config.reasoning_effort, "low")

    def test_contextsniper_mcp_usage_rejects_failed_native_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "mcp_tool_call",
                                    "server": "contextsniper",
                                    "tool": "search_code",
                                    "status": "failed",
                                    "error": {"message": "approval required"},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "mcp_tool_call",
                                    "server": "contextsniper",
                                    "tool": "edit_file",
                                    "status": "completed",
                                    "error": None,
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            usage = runner.contextsniper_mcp_usage(events)
            self.assertFalse(usage["search_succeeded"])
            self.assertTrue(usage["edit_succeeded"])
            self.assertEqual(usage["tools"]["search_code"]["failed_calls"], 1)

    def test_benchmark_lookup_markers_include_case_bug_test_and_commit(self) -> None:
        markers = runner.benchmark_lookup_markers(
            "CVE-2016-3132__28a6ed9f9a36",
            ("ext/spl/tests/bug71735.phpt",),
        )
        self.assertIn("CVE-2016-3132__28a6ed9f9a36", markers)
        self.assertIn("CVE-2016-3132", markers)
        self.assertIn("28a6ed9f9a36", markers)
        self.assertIn("bug71735", markers)
        self.assertIn("71735", markers)
        self.assertNotIn("CVE-2016", markers)

    def test_lookup_guard_allows_general_web_and_local_identifier_search(self) -> None:
        markers = runner.benchmark_lookup_markers(
            "CVE-2016-3132__28a6ed9f9a36",
            ("ext/spl/tests/bug71735.phpt",),
        )
        guard = sys.modules[runner.event_lookup_violation.__module__]
        self.assertIsNone(
            guard.command_lookup_violation(
                'curl -s "https://www.php.net/manual/en/features.gc.php"',
                markers,
            )
        )
        self.assertIsNone(
            guard.command_lookup_violation('grep -R "71735" ext/spl', markers)
        )
        violation = guard.command_lookup_violation(
            '/usr/bin/curl -s "https://bugs.php.net/bug.php?id=71735"',
            markers,
        )
        self.assertIsNotNone(violation)
        self.assertEqual(violation["marker"], "71735")

    def test_pretool_hook_denies_exact_bug_lookup_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "guard.jsonl"
            env = {
                **os.environ,
                "CONTEXTSNIPER_BENCHMARK_LOOKUP_MARKERS_JSON": json.dumps(
                    ["CVE-2016-3132", "71735"]
                ),
                "CONTEXTSNIPER_BENCHMARK_LOOKUP_GUARD_LOG": str(log),
            }
            completed = subprocess.run(
                [sys.executable, str(runner.BENCHMARK_LOOKUP_GUARD)],
                input=json.dumps(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {
                            "command": (
                                'curl -s "https://bugs.php.net/'
                                'bug.php?id=71735"'
                            )
                        },
                    }
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                check=False,
            )
            output = json.loads(completed.stdout)
            decision = output["hookSpecificOutput"]
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertTrue(log.is_file())
            self.assertIn("71735", log.read_text(encoding="utf-8"))
            events = root / "events.jsonl"
            events.write_text("", encoding="utf-8")
            audit = runner.groundtruth_access_audit(
                events,
                root,
                case_id="CVE-2016-3132__28a6ed9f9a36",
                failing_tests=("ext/spl/tests/bug71735.phpt",),
                guard_log_path=log,
            )
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["violations"], [])
            self.assertEqual(
                audit["blocked_attempts"][0]["kind"],
                "benchmark_identifier_in_network_lookup",
            )

    def test_denial_log_does_not_excuse_executed_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            events = root / "events.jsonl"
            events.write_text("")
            log = root / "guard.jsonl"
            record = {"kind": "sensitive_path_outside_workspace", "value": str(root), "path_exists": True}
            for explicit_decision in (False, True):
                if explicit_decision:
                    record["decision"] = "deny"
                log.write_text(json.dumps(record) + "\n")
                audit = runner.groundtruth_access_audit(events, workspace, guard_log_path=log)
                self.assertTrue(audit["passed"])
                self.assertEqual(len(audit["blocked_attempts"]), 1)
            events.write_text(json.dumps({"type": "item.completed", "item": {
                "type": "command_execution", "command": f"ls {root}",
                "exit_code": 0, "status": "completed",
            }}) + "\n")
            audit = runner.groundtruth_access_audit(events, workspace, guard_log_path=log)
            self.assertFalse(audit["passed"])
            self.assertEqual(len(audit["violations"]), 1)
            self.assertEqual(len(audit["blocked_attempts"]), 1)

    def test_invalid_guard_evidence_remains_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events.jsonl"
            events.write_text("")
            log = root / "guard.jsonl"
            for record in ("broken json", "[]", '{"kind":"guard_configuration_error"}',
                           '{"kind":"sensitive_path_outside_workspace","value":"..","decision":"allow"}'):
                log.write_text(record + "\n")
                audit = runner.groundtruth_access_audit(events, root, guard_log_path=log)
                self.assertFalse(audit["passed"])
                self.assertEqual(audit["blocked_attempts"], [])
            events.unlink()
            self.assertFalse(runner.groundtruth_access_audit(events, root)["passed"])

    def test_groundtruth_audit_accepts_workspace_only_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            events = Path(temporary) / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "mcp_tool_call",
                                    "server": "contextsniper",
                                    "tool": "search_code",
                                    "arguments": {"query": "symbol", "path": "src"},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "/bin/zsh -lc 'sed -n 1,20p src/file.c'",
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(events, workspace)
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["violations"], [])

    def test_groundtruth_audit_ignores_system_python_and_heredoc_operators(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "output" / "workspace"
            workspace.mkdir(parents=True)
            sensitive = root / "prepared-inputs"
            sensitive.mkdir()
            command = (
                "/bin/zsh -lc \"/usr/bin/python3 << 'EOF'\\n"
                "print((20 - 10) // 5)\\nEOF\""
            )
            events = root / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "command_execution", "command": command},
                    }
                ),
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(
                events, workspace, sensitive_roots=(sensitive,)
            )
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["violations"], [])

    def test_groundtruth_audit_ignores_nonexistent_workspace_path_typo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "runs" / "20260924-case" / "workspace"
            workspace.mkdir(parents=True)
            sensitive = root / "runs"
            typo = root / "runs" / "20160924-case" / "workspace" / "src.c"
            events = root / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": f"sed -n 1,20p {typo}",
                        },
                    }
                ),
                encoding="utf-8",
            )
            log = root / "guard.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "kind": "sensitive_path_outside_workspace",
                        "value": str(typo),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(
                events,
                workspace,
                guard_log_path=log,
                sensitive_roots=(sensitive,),
            )
            self.assertTrue(audit["passed"])
            self.assertEqual(audit["violations"], [])

    def test_groundtruth_audit_ignores_quoted_double_slash_shell_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            events = root / "events.jsonl"
            events.write_text(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": 'grep -v "//" src/file.c',
                        },
                    }
                ),
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(events, workspace)
            self.assertTrue(audit["passed"])

    def test_groundtruth_audit_allows_unrelated_tmp_but_rejects_sensitive_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "runs" / "workspace"
            workspace.mkdir(parents=True)
            sensitive = root / "dataset" / "inputs"
            sensitive.mkdir(parents=True)
            events = root / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "find /tmp -name php -type f",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": f"find {root} -name '*.patch'",
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(
                events, workspace, sensitive_roots=(sensitive,)
            )
            self.assertFalse(audit["passed"])
            self.assertEqual(len(audit["violations"]), 1)
            self.assertEqual(
                audit["violations"][0]["kind"],
                "sensitive_path_outside_workspace",
            )

    def test_pretool_hook_denies_sensitive_parent_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "runs" / "workspace"
            workspace.mkdir(parents=True)
            sensitive = root / "dataset" / "inputs"
            sensitive.mkdir(parents=True)
            log = root / "guard.jsonl"
            env = {
                **os.environ,
                "CONTEXTSNIPER_BENCHMARK_LOOKUP_MARKERS_JSON": json.dumps(
                    ["CVE-2016-3132"]
                ),
                "CONTEXTSNIPER_BENCHMARK_LOOKUP_GUARD_LOG": str(log),
                "CONTEXTSNIPER_BENCHMARK_SENSITIVE_ROOTS_JSON": json.dumps(
                    [str(sensitive)]
                ),
                "CONTEXTSNIPER_WORKSPACE_ROOT": str(workspace),
            }
            completed = subprocess.run(
                [sys.executable, str(runner.BENCHMARK_LOOKUP_GUARD)],
                input=json.dumps(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {
                            "cmd": f"find {root} -name '*.patch'"
                        },
                    }
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                check=False,
            )
            decision = json.loads(completed.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn(
                "sensitive_path_outside_workspace", log.read_text(encoding="utf-8")
            )

    def test_groundtruth_audit_rejects_mcp_and_shell_escape_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (root / "groundtruth").mkdir()
            events = root / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "mcp_tool_call",
                                    "server": "contextsniper",
                                    "tool": "search_code",
                                    "arguments": {
                                        "query": "fix",
                                        "path": str(root / "groundtruth"),
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": "/bin/zsh -lc 'rg fix ../groundtruth'",
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(events, workspace)
            self.assertFalse(audit["passed"])
            self.assertEqual(len(audit["violations"]), 2)

    def test_groundtruth_audit_rejects_exact_online_bug_lookup_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            events = root / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": 'grep -R "71735" ext/spl',
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": (
                                        'curl -s "https://www.php.net/manual/'
                                        'en/features.gc.php"'
                                    ),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "command_execution",
                                    "command": (
                                        'curl -s "https://bugs.php.net/'
                                        'bug.php?id=71735"'
                                    ),
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            audit = runner.groundtruth_access_audit(
                events,
                workspace,
                case_id="CVE-2016-3132__28a6ed9f9a36",
                failing_tests=("ext/spl/tests/bug71735.phpt",),
            )
            self.assertFalse(audit["passed"])
            lookup_violations = [
                violation
                for violation in audit["violations"]
                if violation["kind"]
                == "benchmark_identifier_in_network_lookup"
            ]
            self.assertEqual(len(lookup_violations), 1)
            self.assertEqual(lookup_violations[0]["marker"], "71735")

    def test_contextsniper_mcp_guard_rejects_paths_outside_active_workspace(self) -> None:
        server_path = (
            REPOSITORY_ROOT / "claude-plugin" / "contextsniper_mcp" / "server.py"
        )
        plugin_root = str(REPOSITORY_ROOT / "claude-plugin")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            spec = importlib.util.spec_from_file_location(
                "guarded_contextsniper_server", server_path
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            with mock.patch.dict(
                os.environ,
                {
                    "CONTEXTSNIPER_PLUGIN_AUTO_START": "0",
                    "CONTEXTSNIPER_ENFORCE_WORKSPACE_ROOT": "1",
                    "CONTEXTSNIPER_WORKSPACE_ROOT": str(workspace),
                },
                clear=False,
            ), mock.patch.object(sys, "path", [plugin_root, *sys.path]):
                spec.loader.exec_module(module)
                self.assertEqual(
                    module._workspace_root_arg("src"),
                    str((workspace / "src").resolve()),
                )
                with self.assertRaisesRegex(ValueError, "outside"):
                    module._workspace_root_arg(str(root / "groundtruth"))

    def test_all_requested_apr_outcomes(self) -> None:
        baseline = {
            "status": "failing",
            "failed_test_ids": ["a", "b"],
            "test_id_source": "caller-supplied",
        }
        cases = {
            "plausible": {"status": "plausible", "failed_test_ids": []},
            "cleanfix": {
                "status": "failing",
                "failed_test_ids": ["b"],
                "test_id_source": "configured-pattern",
            },
            "noisefix": {
                "status": "failing",
                "failed_test_ids": ["b", "c"],
                "test_id_source": "configured-pattern",
            },
            "nonefix": {
                "status": "failing",
                "failed_test_ids": ["a", "b"],
                "test_id_source": "configured-pattern",
            },
            "negfix": {
                "status": "failing",
                "failed_test_ids": ["a", "b", "c"],
                "test_id_source": "configured-pattern",
            },
        }
        for expected, patched in cases.items():
            with self.subTest(expected=expected):
                classified = runner.classify_validation_result(baseline, patched)
                self.assertEqual(classified["status"], expected)

        invalid = runner.classify_validation_result(
            baseline, {"status": "invalid", "validation_error": "runtime failed"}
        )
        self.assertEqual(invalid["status"], "invalid")
        self.assertFalse(invalid["classification_basis_valid"])

    def test_target_failure_still_runs_regression_and_detects_negfix(self) -> None:
        def snapshot(failed: list[str], passed: list[str]) -> dict:
            return {
                "status": "failing" if failed else "plausible",
                "validation_error": "",
                "failed_test_ids": failed,
                "passed_test_ids": passed,
                "tests_executed": True,
                "test_commands": [{"output": "test output"}],
                "test_id_source": "configured-pattern",
            }

        plan = BuildPlan(
            system="custom",
            setup=(),
            build=(),
            target_test=(CommandSpec("target", ("run-target", "{test_id}")),),
            regression_test=(CommandSpec("regression", ("run-regression",)),),
        )
        validator = runner.ProjectValidator(environment_backend="host")
        validator._run_plan = mock.Mock(
            side_effect=[
                snapshot(["bug"], []),
                snapshot(["bug", "stable"], []),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            patched = validator._run_target_and_regression(
                root, plan, root / "artifacts", ("bug",)
            )
        result = runner.classify_validation_result(
            {
                "status": "failing",
                "failed_test_ids": ["bug"],
                "test_id_source": "caller-supplied",
            },
            patched,
        )
        self.assertEqual(validator._run_plan.call_count, 2)
        self.assertFalse(patched["target_passed"])
        self.assertTrue(patched["regression_executed"])
        self.assertEqual(result["status"], "negfix")
        self.assertEqual(result["regression_test_ids"], ["stable"])

    def test_run_plan_collects_every_multi_target_result_after_failure(self) -> None:
        target_tests = (
            CommandSpec(
                "target-first",
                ("run-target", "first"),
                evidence_pattern=r"^(?:PASSED|FAILED)\s+\S+",
                failure_pattern=r"^FAILED\s+\S+",
            ),
            CommandSpec(
                "target-second",
                ("run-target", "second"),
                evidence_pattern=r"^(?:PASSED|FAILED)\s+\S+",
                failure_pattern=r"^FAILED\s+\S+",
            ),
        )
        plan = BuildPlan(
            system="custom",
            setup=(),
            build=(),
            target_test=target_tests,
            regression_test=target_tests,
        )
        validator = runner.ProjectValidator(environment_backend="host")
        executed: list[str] = []

        def run_one(spec, cwd, root):
            executed.append(spec.label)
            return CommandResult(
                label=spec.label,
                argv=list(spec.argv),
                cwd=str(cwd),
                returncode=1,
                output=f"FAILED {spec.argv[-1]}\n",
                elapsed_seconds=0.1,
            )

        validator._run_one = mock.Mock(side_effect=run_one)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            snapshot = validator._run_plan(
                root,
                plan,
                root / "artifacts",
                prefix="patched-target",
                test_commands=target_tests,
                test_scope="target",
            )

        self.assertEqual(executed, ["target-first", "target-second"])
        self.assertEqual(snapshot["status"], "failing")
        self.assertEqual(snapshot["failed_test_ids"], ["first", "second"])
        self.assertEqual(len(snapshot["test_commands"]), 2)

    def test_non_test_command_lists_remain_fail_fast(self) -> None:
        validator = runner.ProjectValidator(environment_backend="host")
        commands = (
            CommandSpec("first", ("run-setup", "first")),
            CommandSpec("second", ("run-setup", "second")),
        )
        failed = CommandResult(
            label="first",
            argv=["run-setup", "first"],
            cwd=".",
            returncode=1,
            output="setup failed\n",
            elapsed_seconds=0.1,
        )
        validator._run_one = mock.Mock(return_value=failed)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            results = validator._run_commands(
                root, commands, root / "artifacts", "setup"
            )

        self.assertEqual(len(results), 1)
        validator._run_one.assert_called_once()

    def test_untrusted_test_ids_cannot_produce_negfix(self) -> None:
        result = runner.classify_validation_result(
            {
                "status": "failing",
                "failed_test_ids": ["bug"],
                "test_id_source": "caller-supplied",
            },
            {
                "status": "failing",
                "validation_error": "",
                "failed_test_ids": ["bug", "guessed-suite"],
                "test_id_source": "output-heuristic",
            },
        )
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["classification_basis_valid"])

    def test_configured_test_markers_do_not_mix_heuristic_suite_ids(self) -> None:
        spec = CommandSpec(
            "regression",
            ("run-tests",),
            evidence_pattern=r"^(?:PASSED|FAILED)\s+\S+",
            failure_pattern=r"^FAILED\s+\S+",
        )
        command = CommandResult(
            label="regression",
            argv=["run-tests"],
            cwd=".",
            returncode=1,
            elapsed_seconds=0.1,
            output=(
                "The following tests FAILED:\n"
                "  13 - ranges-test (Failed)\n"
                "FAILED ranges_test.format_vector\n"
                "PASSED ranges_test.format_array\n"
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            cases, source = runner.ProjectValidator._test_case_results(
                Path(temporary), {}, (spec,), [command]
            )
        self.assertEqual(source, "configured-pattern")
        self.assertEqual(
            cases,
            {
                "ranges_test.format_vector": False,
                "ranges_test.format_array": True,
            },
        )

    def test_codex_event_usage_aggregates_completed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            events.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": 10, "output_tokens": 3},
                            }
                        ),
                        json.dumps({"type": "item.completed", "usage": {"input_tokens": 999}}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": 7, "output_tokens": 2},
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                runner.event_usage(events), {"input_tokens": 17, "output_tokens": 5}
            )

    def test_bundled_validator_loads_contract_without_external_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "project"
            project_dir.mkdir()
            (project_dir / "sample.c").write_text("int value = 0;\n", encoding="utf-8")
            config_path = root / "contract.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 6,
                        "system": "custom",
                        "setup": [],
                        "build": [],
                        "target_test": [
                            {
                                "command": ["sh", "-c", "echo FAILED {test_id}; exit 1"],
                                "evidence_pattern": "^(?:PASSED|FAILED)\\s+\\S+",
                                "failure_pattern": "^FAILED\\s+\\S+",
                            }
                        ],
                        "regression_test": [
                            {
                                "command": ["sh", "-c", "echo PASSED smoke"],
                                "evidence_pattern": "^PASSED\\s+\\S+",
                                "failure_pattern": "^FAILED\\s+\\S+",
                            }
                        ],
                        "repair": {
                            "failing_tests": ["failure-test"],
                            "source_extensions": [".c"],
                        },
                        "workspace": {
                            "disposable": True,
                            "initialize_git_if_missing": True,
                        },
                        "environment": {"mode": "host", "runtime": "auto"},
                    }
                ),
                encoding="utf-8",
            )
            project = runner.Project(
                path=project_dir, project_id="standalone-test", config_path=config_path
            )
            validator = runner.ProjectValidator(
                environment_backend="host", environment_runtime="auto"
            )
            baseline = validator.external_baseline(
                project,
                root / "artifacts",
                failing_tests=("failure-test",),
                failure_output="FAILED failure-test\n",
            )
            self.assertEqual(baseline["status"], "failing")
            self.assertEqual(baseline["failed_test_ids"], ["failure-test"])
            self.assertTrue(baseline["baseline_external"])
            self.assertTrue(baseline["environment_digest"])

            with runner.ProjectWorkspace(
                project, root / "workspace-state"
            ) as workspace:
                (workspace.path / "sample.c").write_text(
                    "int value = 1;\n", encoding="utf-8"
                )
                diff = workspace.canonical_diff()
                patch_paths = workspace.unified_diff_paths(diff)
                snapshot_hashes = workspace.snapshot_sha256s(patch_paths)
                workspace.reset_to_snapshot()
                validation = validator.validate_diff(
                    project=project,
                    diff=diff,
                    patch_paths=patch_paths,
                    artifact_dir=root / "validation",
                    expected_sha256s=snapshot_hashes,
                    reusable_workspace=workspace,
                )

            self.assertEqual(validation["status"], "plausible")
            self.assertTrue(validation["validation_executed"])

    def test_adapter_has_no_sibling_framework_import_or_cli_option(self) -> None:
        runner_source = MODULE_PATH.read_text(encoding="utf-8")
        entrypoint_source = (ADAPTER_ROOT / "run_defects4c_php.sh").read_text(encoding="utf-8")
        self.assertNotIn("--debugging-framework-root", runner_source + entrypoint_source)
        self.assertNotIn("DEFAULT_FRAMEWORK_ROOT", runner_source)
        for path in (ADAPTER_ROOT / "evaluator").glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from src.", source, path.name)
            self.assertNotIn("import src.", source, path.name)

    def test_dry_run_needs_no_runtime_key_or_framework_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepared_case(root)
            args = runner.parse_args(
                ["CVE-TEST__abc123", "--dataset-root", str(root), "--dry-run"]
            )
            with mock.patch.dict(
                os.environ,
                {"CONTEXTSNIPER_EMBEDDING_API_KEY": ""},
                clear=False,
            ):
                code, result = runner.execute(args)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "dry-run")
            self.assertEqual(result["attempts"], 1)
            self.assertEqual(result["agent"], "codex")
            self.assertEqual(
                result["agent_harness_profile"], runner.AGENT_HARNESS_PROFILE
            )
            self.assertEqual(result["model_provider"], "openai")
            self.assertEqual(result["model"], "gpt-5.6-sol")
            self.assertEqual(result["reasoning_effort"], "low")
            self.assertEqual(result["validation_implementation"], "bundled")
            self.assertNotIn("CONTEXTSNIPER_EMBEDDING_API_KEY", result["contextsniper_settings"])

    def test_openrouter_dry_run_resolves_deepseek_without_exposing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._prepared_case(root)
            args = runner.parse_args(
                [
                    "CVE-TEST__abc123",
                    "--dataset-root",
                    str(root),
                    "--model",
                    "deepseek/deepseek-v4-flash-0731",
                    "--dry-run",
                ]
            )
            with mock.patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "secret-not-logged"},
                clear=False,
            ):
                code, result = runner.execute(args)

        self.assertEqual(code, 0)
        self.assertEqual(result["agent"], "codex")
        self.assertEqual(
            result["agent_harness_profile"], runner.AGENT_HARNESS_PROFILE
        )
        self.assertEqual(result["model_provider"], "openrouter")
        self.assertEqual(result["model"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(result["reasoning_effort"], "low")
        self.assertEqual(result["model_catalog"], "models/deepseek-v4-flash-0731.json")
        self.assertNotIn("secret-not-logged", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
