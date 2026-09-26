#!/usr/bin/env python3
"""Revalidate saved ContextSniper-Codex patches without invoking Codex again."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from evaluator import (  # noqa: E402
    Project,
    ProjectValidator,
    classify_validation_result,
)
from contextsniper_codex import (  # noqa: E402
    CONTEXTSNIPER_ROOT,
    groundtruth_access_audit,
)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def stale_run_dirs(output_root: Path) -> list[Path]:
    discovered: dict[Path, Path] = {}
    for result_path in sorted(output_root.expanduser().resolve().glob("*/result.json")):
        run_dir = result_path.parent
        if run_dir.is_symlink():
            continue
        result = load_json(result_path)
        stale_fail_fast_validation = (
            result.get("target_status") == "failing"
            and result.get("regression_executed") is False
        )
        stale_access_audit = result.get("validation_error") in {
            "groundtruth_access_attempt_detected",
            "benchmark_answer_lookup_attempt_detected",
        }
        if stale_fail_fast_validation or stale_access_audit:
            discovered[run_dir.resolve()] = run_dir.resolve()
    return list(discovered.values())


def saved_input(run_dir: Path, case_id: str, suffix: str) -> Path:
    path = run_dir / "input" / f"{case_id}{suffix}"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def patch_paths_from_diff(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            fields = shlex.split(line)
        except ValueError as exc:
            raise ValueError(f"Saved patch has an invalid header: {line!r}") from exc
        if len(fields) != 4 or not fields[3].startswith("b/"):
            raise ValueError(f"Saved patch has an invalid file header: {line!r}")
        path = fields[3][2:]
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"Saved patch contains an unsafe path: {path!r}")
        paths.append(path)
    return sorted(dict.fromkeys(paths))


def revalidate(
    run_dir: Path,
    *,
    command_timeout: int,
    jobs: int,
    force: bool,
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    old_result = load_json(run_dir / "result.json")
    run_metadata = load_json(run_dir / "run.json")
    case_id = str(old_result.get("case_id") or run_metadata.get("case_id") or "")
    if not case_id:
        raise ValueError(f"Missing case_id in {run_dir}")

    output_path = run_dir / "revalidated-result.json"
    if output_path.exists() and not force:
        return load_json(output_path)

    patch_path = run_dir / "patch.diff"
    diff = patch_path.read_text(encoding="utf-8")
    config_path = saved_input(run_dir, case_id, ".debugging-framework.json")
    failure_log = saved_input(run_dir, case_id, ".failure.log")
    failure_output = failure_log.read_text(encoding="utf-8", errors="replace")
    old_baseline = old_result.get("baseline") or {}
    failing_tests = tuple(
        str(value)
        for value in (
            old_baseline.get("target_tests")
            or old_baseline.get("failed_test_ids")
            or old_result.get("initial_failed_test_ids")
            or []
        )
        if str(value).strip()
    )
    if not failing_tests:
        raise ValueError(f"Saved result has no failing test IDs: {run_dir}")

    events_path = run_dir / "events.jsonl"
    evaluated_workspace = Path(
        str(old_result.get("evaluated_workspace") or run_dir / "workspace")
    )
    project_path = Path(str(old_result["project"])).expanduser().resolve()
    sensitive_roots = tuple(
        dict.fromkeys(
            (
                CONTEXTSNIPER_ROOT.resolve(),
                project_path.parent,
                project_path.parent.parent,
            )
        )
    )
    leakage_audit = groundtruth_access_audit(
        events_path,
        evaluated_workspace,
        case_id=case_id,
        failing_tests=failing_tests,
        guard_log_path=run_dir / "logs" / "benchmark-lookup-guard.jsonl",
        sensitive_roots=sensitive_roots,
    )
    if not leakage_audit["passed"]:
        benchmark_lookup = any(
            violation.get("kind") == "benchmark_identifier_in_network_lookup"
            for violation in leakage_audit["violations"]
        )
        result = {
            **old_result,
            "status": "invalid",
            "post_validation_status": "not-run",
            "validation_error": (
                "benchmark_answer_lookup_attempt_detected"
                if benchmark_lookup
                else "groundtruth_access_attempt_detected"
            ),
            "patch_validation_passed": False,
            "classification_basis_valid": False,
            "groundtruth_access_audit": leakage_audit,
            "original_result": str(run_dir / "result.json"),
            "original_status": old_result.get("status"),
            "revalidation": {
                "implementation": "bundled-validation-v2",
                "codex_invoked": False,
                "saved_patch_reused": False,
                "validation_skipped": "groundtruth_or_lookup_audit_failed",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        }
        atomic_json(output_path, result)
        return result

    codex_returncode = old_result.get("codex_returncode")
    codex_timed_out = bool(old_result.get("codex_timed_out"))
    compatibility_error = str(old_result.get("codex_compatibility_error") or "")
    if compatibility_error or codex_timed_out or codex_returncode not in {0, "0"}:
        result = {
            **old_result,
            "status": "invalid",
            "post_validation_status": "not-run",
            "validation_error": (
                "codex_tool_schema_mismatch"
                if compatibility_error
                else (
                    "codex_execution_timed_out"
                    if codex_timed_out
                    else "codex_execution_failed"
                )
            ),
            "patch_validation_passed": False,
            "classification_basis_valid": False,
            "groundtruth_access_audit": leakage_audit,
            "original_result": str(run_dir / "result.json"),
            "original_status": old_result.get("status"),
            "revalidation": {
                "implementation": "bundled-validation-v2",
                "codex_invoked": False,
                "saved_patch_reused": False,
                "validation_skipped": "codex_execution_incomplete",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        }
        atomic_json(output_path, result)
        return result

    if not diff.strip():
        raise ValueError(f"Saved patch is empty: {patch_path}")
    patch_paths = [str(value) for value in (old_result.get("patch_paths") or [])]
    if not patch_paths:
        patch_paths = patch_paths_from_diff(diff)
    if not patch_paths:
        raise ValueError(f"Saved patch changes no paths: {patch_path}")

    environment = run_metadata.get("environment") or {}
    project = Project(
        path=project_path,
        project_id=case_id,
        config_path=config_path,
    )
    validator = ProjectValidator(
        command_timeout=command_timeout,
        jobs=jobs,
        environment_backend=str(environment.get("mode") or "auto"),
        environment_runtime=str(environment.get("runtime") or "auto"),
        environment_image=str(environment.get("image") or ""),
    )
    artifact_dir = run_dir / "validation" / "revalidated-v2"
    baseline = validator.external_baseline(
        project,
        artifact_dir / "baseline",
        failing_tests=failing_tests,
        failure_output=failure_output,
    )
    validation = validator.validate_diff(
        project=project,
        diff=diff,
        patch_paths=patch_paths,
        artifact_dir=artifact_dir / "patched",
        failing_tests=failing_tests,
        expected_plan_digest=str(baseline.get("plan_digest") or ""),
        expected_environment_digest=str(baseline.get("environment_digest") or ""),
        expected_image_digest=str(baseline.get("provisioned_image_digest") or ""),
    )
    classified = classify_validation_result(baseline, validation)
    result = {
        **old_result,
        **classified,
        "status": classified.get("status", "invalid"),
        "patch_validation_passed": classified.get("status") == "plausible",
        "baseline": baseline,
        "groundtruth_access_audit": leakage_audit,
        "original_result": str(run_dir / "result.json"),
        "original_status": old_result.get("status"),
        "revalidation": {
            "implementation": "bundled-validation-v2",
            "codex_invoked": False,
            "saved_patch_reused": True,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "artifact_dir": str(artifact_dir),
        },
    }
    atomic_json(artifact_dir / "baseline.json", baseline)
    atomic_json(artifact_dir / "validation.json", validation)
    atomic_json(output_path, result)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run bundled validation for saved patches; Codex and "
            "ContextSniper are not started."
        )
    )
    parser.add_argument("run_dirs", nargs="*", type=Path)
    parser.add_argument("--stale-in", type=Path)
    parser.add_argument("--command-timeout", type=int, default=1800)
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dirs = [path.expanduser().resolve() for path in args.run_dirs]
    if args.stale_in:
        run_dirs.extend(stale_run_dirs(args.stale_in))
    run_dirs = list(dict.fromkeys(run_dirs))
    if not run_dirs:
        raise SystemExit("No run directories were selected")

    summaries = []
    exit_code = 0
    for run_dir in run_dirs:
        try:
            result = revalidate(
                run_dir,
                command_timeout=args.command_timeout,
                jobs=args.jobs,
                force=args.force,
            )
            summaries.append(
                {
                    "case_id": result.get("case_id"),
                    "original_status": result.get("original_status"),
                    "status": result.get("status"),
                    "target_status": result.get("target_status"),
                    "regression_status": result.get("regression_status"),
                    "regression_executed": result.get("regression_executed"),
                    "regression_test_ids": result.get("regression_test_ids", []),
                    "result": str(run_dir / "revalidated-result.json"),
                }
            )
        except Exception as exc:
            exit_code = 1
            summaries.append(
                {
                    "run_dir": str(run_dir),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    print(json.dumps(summaries, indent=2, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
