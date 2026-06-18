#!/usr/bin/env python3
import ast
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CACHE_ROOT = ROOT / ".cache"
OFFICIAL_HARNESS_ROOT = CACHE_ROOT / "official_harness"
OFFICIAL_PRO_ROOT = CACHE_ROOT / "SWE-bench_Pro-os"
OFFICIAL_MODEL_NAME = "plain-claude-validator"
SWEBENCH_SITECUSTOMIZE = ROOT / "swebench_sitecustomize"


def load_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value, str):
        value = value.strip()
        try:
            out = json.loads(value)
            if isinstance(out, list):
                return out
        except Exception:
            pass
        try:
            out = ast.literal_eval(value)
            if isinstance(out, list):
                return out
        except Exception:
            pass
    return []


def instance_tests(instance, key):
    return load_list(instance.get(key) or instance.get(key.lower()))


def run_cmd(cmd, cwd, env, timeout_sec):
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        status = "passed" if proc.returncode == 0 else "failed"
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "status": status,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_sec": round(time.time() - start, 3),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": cmd,
            "returncode": None,
            "status": "timeout",
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
            "duration_sec": round(time.time() - start, 3),
        }


def workspace_git_env(workspace, experiment_dir, env):
    out = env.copy()
    moved_git = experiment_dir / "workspace.git"
    if not (workspace / ".git").exists() and moved_git.exists():
        out["GIT_DIR"] = str(moved_git)
        out["GIT_WORK_TREE"] = str(workspace)
    return out


def should_retry_official_harness(result, report_path, aggregate_report_path, instance_log_path, build_image_log_path):
    if Path(report_path).exists():
        return False
    texts = []
    for path in [aggregate_report_path, instance_log_path, build_image_log_path]:
        p = Path(path)
        if p.exists():
            try:
                texts.append(p.read_text(errors="replace"))
            except Exception:
                pass
    hay = "\n".join(texts)
    transient_markers = [
        "gnutls_handshake() failed",
        "GnuTLS recv error",
        "RPC failed",
        "unexpected disconnect",
        "early EOF",
        "invalid index-pack output",
        "HTTP/2 stream",
        "TLS connection was non-properly terminated",
        "pack-objects died of signal 9",
        "fatal: failed to run repack",
        "Could not parse object",
        '"error_instances": 1',
    ]
    return any(marker in hay for marker in transient_markers)


def run_with_lock(lock_path, fn):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def collect_tests_for_file(runner, cwd, env, test_file, timeout_sec):
    if "tests/runtests.py" in runner:
        return []
    collect_runner = [x for x in runner if x != "-q"]
    res = run_cmd(collect_runner + ["--collect-only", test_file], cwd, env, timeout_sec)
    if res["status"] == "timeout":
        return []
    out = (res.get("stdout") or "") + "\n" + (res.get("stderr") or "")
    tests = []
    tree_funcs = []
    for line in out.splitlines():
        s = line.strip()
        if "::" in s and not s.startswith(("ERROR", "no tests ran", "collected ")):
            tests.append(s)
            continue
        m = re.search(r"<Function\s+([^>]+)>", s)
        if m:
            tree_funcs.append(m.group(1).strip())
    if not tests and tree_funcs:
        tests = [f"{test_file}::{name}" for name in tree_funcs]
    return tests


def resolve_test_node(runner, cwd, env, test, timeout_sec):
    if "tests/runtests.py" in runner:
        resolved = django_test_label(test)
        if resolved == "test_utils" or "." not in resolved:
            return resolved
        probe = run_cmd(runner + [resolved], cwd, env, timeout_sec)
        probe_text = (probe.get("stdout") or "") + "\n" + (probe.get("stderr") or "")
        if probe["status"] == "passed" or not any(
            marker in probe_text
            for marker in [
                "ModuleNotFoundError",
                "Failed to import test module",
                "ImportError:",
                "AttributeError:",
            ]
        ):
            return resolved
        module_path = resolved.rsplit(".", 2)[0]
        if any(part.startswith("test_") for part in module_path.split(".")[1:]):
            return module_path.split(".", 1)[0]
        return resolved

    file_part, _, fn_part = test.partition("::")
    if not file_part or not fn_part:
        return test

    exact = run_cmd(runner + ["--collect-only", "-q", test], cwd, env, timeout_sec)
    if exact.get("returncode") == 0:
        return test

    collected = collect_tests_for_file(runner, cwd, env, file_part, timeout_sec)
    if not collected:
        return test

    needle = re.sub(r"[^a-z0-9]+", " ", fn_part.lower()).strip()
    needle_tokens = [t for t in needle.split() if t and t != "test"]
    if not needle_tokens:
        return test

    scored = []
    for candidate in collected:
        _, _, cand_fn = candidate.partition("::")
        cand_norm = re.sub(r"[^a-z0-9]+", " ", cand_fn.lower()).strip()
        score = sum(1 for tok in needle_tokens if tok in cand_norm)
        scored.append((score, candidate))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return test


def django_test_label(test):
    match = re.match(r"^(?P<method>[A-Za-z_][A-Za-z0-9_]*)\s+\((?P<class>[^)]+)\)$", test)
    if match:
        class_path = match.group("class")
        module_path = class_path.rsplit(".", 1)[0]
        return f"{class_path}.{match.group('method')}"
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$", test):
        # Some older Django unittest descriptions in SWE-bench are human
        # docstrings rather than importable labels. In older Django-style tasks
        # these can come from test_utils; run the containing app instead of
        # reporting a false import failure.
        return "test_utils"
    return test


def choose_runner(instance):
    python_bin = (
        os.environ.get("SWE_VALIDATION_PYTHON")
        or os.environ.get("SWE_TASK_PYTHON")
        or sys.executable
    )
    if (instance.get("repo") or "").lower() == "django/django":
        return [python_bin, "tests/runtests.py", "--verbosity=1"]
    return [python_bin, "-m", "pytest", "-q", "-o", "filterwarnings=ignore"]


def pre_validation_steps(instance, workspace, env, timeout_sec):
    python_bin = (
        os.environ.get("SWE_VALIDATION_PYTHON")
        or os.environ.get("SWE_TASK_PYTHON")
        or sys.executable
    )
    repo = (instance.get("repo") or "").lower()
    steps = [
        run_cmd(
            [
                python_bin,
                "-m",
                "pip",
                "install",
                "-v",
                "-e",
                ".",
            ],
            workspace,
            env,
            timeout_sec,
        )
    ]
    if "scikit-learn/scikit-learn" in repo:
        steps.append(
            run_cmd(
                [python_bin, "setup.py", "build_ext", "--inplace"],
                workspace,
                env,
                timeout_sec,
            )
        )
    return steps


def docker_available():
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except Exception:
        return False


def docker_image_for_pro(instance):
    tag = (os.environ.get("SWE_DOCKERHUB_TAG") or instance.get("dockerhub_tag") or "").strip()
    if not tag:
        return ""
    username = os.environ.get("SWE_PRO_DOCKERHUB_USERNAME", "jefzda")
    return f"{username}/sweap-images:{tag}"


def ensure_official_pro_repo():
    if (OFFICIAL_PRO_ROOT / "swe_bench_pro_eval.py").exists():
        return OFFICIAL_PRO_ROOT
    OFFICIAL_PRO_ROOT.parent.mkdir(parents=True, exist_ok=True)
    if OFFICIAL_PRO_ROOT.exists():
        shutil.rmtree(OFFICIAL_PRO_ROOT)
    url = os.environ.get("SWE_PRO_OFFICIAL_REPO_URL", "https://github.com/scaleapi/SWE-bench_Pro-os.git")
    subprocess.run(["git", "clone", "--depth=1", url, str(OFFICIAL_PRO_ROOT)], check=True, timeout=900)
    return OFFICIAL_PRO_ROOT


def pro_csv_value(value):
    if isinstance(value, list):
        return repr(value)
    if value is None:
        return ""
    return str(value)


def write_pro_raw_sample(instance, path):
    import csv

    fields = [
        "instance_id",
        "repo",
        "base_commit",
        "before_repo_set_cmd",
        "selected_test_files_to_run",
        "fail_to_pass",
        "pass_to_pass",
        "dockerhub_tag",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow({field: pro_csv_value(instance.get(field)) for field in fields})


def run_swe_pro_docker_validation(instance, workspace, experiment_dir, timeout_sec):
    image = docker_image_for_pro(instance)
    if not image:
        return {
            "mode": "swe_pro_docker",
            "infrastructure_error": "missing_dockerhub_tag",
            "overall_ok": False,
        }
    if not docker_available():
        return {
            "mode": "swe_pro_docker",
            "docker_image": image,
            "infrastructure_error": "docker_unavailable",
            "overall_ok": False,
        }

    repo = ensure_official_pro_repo()
    patch = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=str(workspace),
        env=workspace_git_env(workspace, experiment_dir, os.environ.copy()),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    patch_path = experiment_dir / "swe_pro_predictions.json"
    raw_sample_path = experiment_dir / "swe_pro_raw_sample.csv"
    output_dir = experiment_dir / "swe_pro_eval"
    prefix = OFFICIAL_MODEL_NAME
    patch_path.write_text(
        json.dumps(
            [{"instance_id": instance["instance_id"], "patch": patch, "prefix": prefix}],
            indent=2,
        ),
        encoding="utf-8",
    )
    write_pro_raw_sample(instance, raw_sample_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(repo / "swe_bench_pro_eval.py"),
        "--raw_sample_path",
        str(raw_sample_path),
        "--patch_path",
        str(patch_path),
        "--output_dir",
        str(output_dir),
        "--scripts_dir",
        str(repo / "run_scripts"),
        "--num_workers",
        "1",
        "--dockerhub_username",
        os.environ.get("SWE_PRO_DOCKERHUB_USERNAME", "jefzda"),
        "--use_local_docker",
        "--redo",
    ]
    platform = os.environ.get("SWE_PRO_DOCKER_PLATFORM")
    if platform:
        cmd.extend(["--docker_platform", platform])
    if os.environ.get("SWE_PRO_BLOCK_NETWORK") == "1":
        cmd.append("--block_network")

    run = run_cmd(cmd, repo, os.environ.copy(), max(timeout_sec, 3600))
    iid = instance["instance_id"]
    output_json = output_dir / iid / f"{prefix}_output.json"
    stdout_log = output_dir / iid / f"{prefix}_stdout.log"
    stderr_log = output_dir / iid / f"{prefix}_stderr.log"
    details = {
        "mode": "swe_pro_docker",
        "cmd": cmd,
        "run": run,
        "docker_image": image,
        "raw_sample_path": str(raw_sample_path),
        "patch_path": str(patch_path),
        "output_dir": str(output_dir),
        "output_json": str(output_json),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }
    if output_json.exists():
        output = json.loads(output_json.read_text(encoding="utf-8"))
        details["output"] = output
        passed_tests = {test["name"] for test in output.get("tests", []) if test.get("status") == "PASSED"}
        fail_to_pass = set(instance_tests(instance, "FAIL_TO_PASS"))
        pass_to_pass = set(instance_tests(instance, "PASS_TO_PASS"))
        details["fail_ok"] = fail_to_pass <= passed_tests if fail_to_pass else None
        details["pass_ok"] = pass_to_pass <= passed_tests if pass_to_pass else None
        details["overall_ok"] = bool((fail_to_pass | pass_to_pass) <= passed_tests)
    else:
        details["infrastructure_error"] = (
            "swe_pro_eval_failed" if run["status"] != "passed" else "swe_pro_output_missing"
        )
        details["fail_ok"] = None
        details["pass_ok"] = None
        details["overall_ok"] = False
    return details


def docker_bridge_gateway():
    proc = subprocess.run(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return "172.17.0.1"


def ensure_local_git_mirror(repo):
    if repo != "scikit-learn/scikit-learn":
        return None

    source = CACHE_ROOT / "repo" / "scikit-learn__scikit-learn-10297" / "src"
    mirror = CACHE_ROOT / "git-daemon" / "scikit-learn" / "scikit-learn.git"
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--mirror", str(source), str(mirror)],
            check=True,
            timeout=600,
        )
    else:
        subprocess.run(
            ["git", "remote", "update", "--prune"],
            cwd=str(mirror),
            check=False,
            timeout=600,
        )

    pid_file = CACHE_ROOT / "git-daemon" / "git-daemon.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
        except Exception:
            pid_file.unlink(missing_ok=True)
    if not pid_file.exists():
        subprocess.run(
            [
                "git",
                "daemon",
                "--reuseaddr",
                "--export-all",
                f"--base-path={CACHE_ROOT / 'git-daemon'}",
                "--listen=0.0.0.0",
                "--port=9418",
                "--detach",
                f"--pid-file={pid_file}",
            ],
            check=True,
            timeout=30,
        )

    return f"git://{docker_bridge_gateway()}/scikit-learn/scikit-learn.git"


def has_official_harness():
    return importlib.util.find_spec("swebench") is not None


def should_use_official_harness(instance):
    if os.environ.get("SWE_VALIDATION_FORCE_LOCAL") == "1":
        return False
    if instance.get("dockerhub_tag"):
        return docker_available()
    required = {"instance_id", "repo", "version", "base_commit", "test_patch"}
    if not required.issubset(instance.keys()):
        return False
    return has_official_harness() and docker_available()


def build_local_prediction(instance, workspace, experiment_dir):
    patch = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=str(workspace),
        env=workspace_git_env(workspace, experiment_dir, os.environ.copy()),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pred = [
        {
            "instance_id": instance["instance_id"],
            "model_name_or_path": OFFICIAL_MODEL_NAME,
            "model_patch": patch,
        }
    ]
    dataset_path = experiment_dir / "official_dataset.json"
    pred_path = experiment_dir / "official_predictions.json"
    dataset_path.write_text(json.dumps([instance], indent=2))
    pred_path.write_text(json.dumps(pred, indent=2))
    return dataset_path, pred_path


def official_run_id(experiment_dir, instance=None):
    m = re.search(r"-r(\d+)-", experiment_dir.name)
    instance_slug = ""
    if instance:
        instance_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", instance.get("instance_id", "")).strip("-")
    if m:
        if instance_slug:
            return f"official-r{m.group(1)}-{instance_slug}"
        return f"official-r{m.group(1)}"
    if instance_slug:
        return f"official-{experiment_dir.name}-{instance_slug}"
    return f"official-{experiment_dir.name}"


def official_instance_image_tag(experiment_dir, instance=None):
    override = os.environ.get("SWE_OFFICIAL_INSTANCE_IMAGE_TAG")
    if override:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", override).strip("-")
    if instance and os.environ.get("SWE_OFFICIAL_STABLE_INSTANCE_IMAGE_TAG") == "1":
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", instance["instance_id"]).strip("-")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", experiment_dir.name).strip("-")


def parse_official_results(instance, report):
    instance_report = report[instance["instance_id"]]
    tests_status = instance_report.get("tests_status", {})
    fail_to_pass = instance_tests(instance, "FAIL_TO_PASS")
    pass_to_pass = instance_tests(instance, "PASS_TO_PASS")

    def make_group(group_name, tests):
        success = set(tests_status.get(group_name, {}).get("success", []))
        failure = set(tests_status.get(group_name, {}).get("failure", []))
        out = []
        for test in tests:
            if test in success:
                status = "passed"
                rc = 0
            elif test in failure:
                status = "failed"
                rc = 1
            else:
                status = "unknown"
                rc = None
            out.append(
                {
                    "test": test,
                    "resolved_test": test,
                    "status": status,
                    "returncode": rc,
                    "stdout": "",
                    "stderr": "",
                    "duration_sec": None,
                }
            )
        return out

    results = {
        "FAIL_TO_PASS": make_group("FAIL_TO_PASS", fail_to_pass),
        "PASS_TO_PASS": make_group("PASS_TO_PASS", pass_to_pass),
    }
    fail_ok = all(r["status"] == "passed" for r in results["FAIL_TO_PASS"]) if fail_to_pass else None
    pass_ok = all(r["status"] == "passed" for r in results["PASS_TO_PASS"]) if pass_to_pass else None
    overall_ok = bool(instance_report.get("resolved")) and (pass_ok is not False)
    return results, fail_ok, pass_ok, overall_ok


def run_official_harness(instance, workspace, experiment_dir, timeout_sec):
    if instance.get("dockerhub_tag"):
        return run_swe_pro_docker_validation(instance, workspace, experiment_dir, timeout_sec)

    dataset_path, pred_path = build_local_prediction(instance, workspace, experiment_dir)
    local_repo_url = ensure_local_git_mirror(instance.get("repo"))
    run_id = official_run_id(experiment_dir, instance)
    OFFICIAL_HARNESS_ROOT.mkdir(parents=True, exist_ok=True)
    harness_log_dir = OFFICIAL_HARNESS_ROOT / "logs" / "run_evaluation" / run_id
    report_path = harness_log_dir / OFFICIAL_MODEL_NAME / instance["instance_id"] / "report.json"
    test_output_path = harness_log_dir / OFFICIAL_MODEL_NAME / instance["instance_id"] / "test_output.txt"
    instance_log_path = harness_log_dir / OFFICIAL_MODEL_NAME / instance["instance_id"] / "run_instance.log"
    build_image_log_path = (
        OFFICIAL_HARNESS_ROOT
        / "logs"
        / "build_images"
        / "instances"
        / f"sweb.eval.x86_64.{instance['instance_id']}__{official_instance_image_tag(experiment_dir, instance)}"
        / "build_image.log"
    )
    aggregate_report_path = OFFICIAL_HARNESS_ROOT / f"{OFFICIAL_MODEL_NAME}.{run_id}.json"
    reuse_reports = os.environ.get("SWE_VALIDATION_REUSE_REPORTS", "1") != "0"
    max_retries = int(os.environ.get("SWE_OFFICIAL_RETRIES", "3"))
    if not (reuse_reports and report_path.exists()):
        shutil.rmtree(harness_log_dir, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "-d",
        str(dataset_path),
        "-i",
        instance["instance_id"],
        "-p",
        str(pred_path),
        "--max_workers",
        "1",
        "-t",
        str(max(timeout_sec, 1800)),
        "-id",
        run_id,
        "-n",
        "none",
        "--instance_image_tag",
        official_instance_image_tag(experiment_dir, instance),
        "--report_dir",
        str(experiment_dir),
    ]
    if reuse_reports and report_path.exists():
        harness = {
            "cmd": cmd,
            "returncode": 0,
            "status": "passed",
            "stdout": "",
            "stderr": "",
            "duration_sec": 0.0,
        }
    else:
        harness = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                shutil.rmtree(harness_log_dir, ignore_errors=True)
                shutil.rmtree(build_image_log_path.parent, ignore_errors=True)
                aggregate_report_path.unlink(missing_ok=True)
                time.sleep(min(10, 2 * attempt))
            harness_env = os.environ.copy()
            if SWEBENCH_SITECUSTOMIZE.exists():
                harness_env["PYTHONPATH"] = (
                    f"{SWEBENCH_SITECUSTOMIZE}{os.pathsep}{harness_env.get('PYTHONPATH', '')}"
                ).rstrip(os.pathsep)
            if local_repo_url:
                repo_key = "SWEBENCH_LOCAL_REPO_URL_" + re.sub(
                    r"[^A-Za-z0-9]", "_", instance["repo"]
                )
                harness_env[repo_key] = local_repo_url
            harness = run_cmd(cmd, OFFICIAL_HARNESS_ROOT, harness_env, max(timeout_sec * 3, 7200))
            if report_path.exists():
                break
            if not should_retry_official_harness(
                harness,
                report_path,
                aggregate_report_path,
                instance_log_path,
                build_image_log_path,
            ):
                break
        assert harness is not None

    result = {
        "mode": "official_harness",
        "cmd": cmd,
        "run": harness,
        "report_path": str(report_path),
        "aggregate_report_path": str(aggregate_report_path),
        "test_output_path": str(test_output_path),
        "instance_log_path": str(instance_log_path),
        "build_image_log_path": str(build_image_log_path),
    }
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if instance["instance_id"] in report:
            result["report"] = report
    elif aggregate_report_path.exists():
        report = json.loads(aggregate_report_path.read_text())
        if instance["instance_id"] in report:
            result["report"] = report
    return result


def write_group_logs(logs_dir, group_name, results):
    for res in results:
        safe = res["test"].replace("/", "_").replace(":", "_")
        (logs_dir / f"validation-{group_name.lower()}-{safe}.log").write_text(
            f"original_test: {res['test']}\n"
            f"resolved_test: {res['resolved_test']}\n"
            f"status: {res['status']}\n"
            f"returncode: {res['returncode']}\n"
        )


def run_local_validation(instance, workspace, experiment_dir, logs_dir, timeout_sec):
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{workspace}{os.pathsep}{env.get('PYTHONPATH','')}".rstrip(os.pathsep)
    runner = choose_runner(instance)
    prep_results = pre_validation_steps(instance, workspace, env, timeout_sec)
    prep_ok = all(step["status"] == "passed" for step in prep_results)

    results = {"FAIL_TO_PASS": [], "PASS_TO_PASS": []}
    infrastructure_error = None
    if not prep_ok:
        infrastructure_error = "pre_validation_failed"
    else:
        test_patch_path = None
        test_patch_applied = False
        test_patch = instance.get("test_patch") or ""
        if test_patch.strip():
            test_patch_path = experiment_dir / "test.patch"
            test_patch_path.write_text(test_patch)
            apply_res = run_cmd(
                ["git", "apply", "--whitespace=nowarn", str(test_patch_path)],
                workspace,
                workspace_git_env(workspace, experiment_dir, env),
                timeout_sec,
            )
            prep_results.append(apply_res)
            prep_ok = prep_ok and apply_res["status"] == "passed"
            test_patch_applied = apply_res["status"] == "passed"
            if not test_patch_applied:
                infrastructure_error = "test_patch_apply_failed"

        try:
            if test_patch_applied or not test_patch.strip():
                for group_name, tests in [
                    ("FAIL_TO_PASS", instance_tests(instance, "FAIL_TO_PASS")),
                    ("PASS_TO_PASS", instance_tests(instance, "PASS_TO_PASS")),
                ]:
                    for test in tests:
                        resolved_test = resolve_test_node(runner, workspace, env, test, timeout_sec)
                        res = run_cmd(runner + [resolved_test], workspace, env, timeout_sec)
                        res["test"] = test
                        res["resolved_test"] = resolved_test
                        results[group_name].append(res)
                        safe = test.replace("/", "_").replace(":", "_")
                        (logs_dir / f"validation-{group_name.lower()}-{safe}.log").write_text(
                            f"$ {' '.join(res['cmd'])}\n"
                            f"original_test: {test}\n"
                            f"resolved_test: {resolved_test}\n\n"
                            f"[stdout]\n{res['stdout']}\n\n[stderr]\n{res['stderr']}\n"
                        )
        finally:
            if test_patch_applied and test_patch_path is not None:
                run_cmd(
                    ["git", "apply", "-R", "--whitespace=nowarn", str(test_patch_path)],
                    workspace,
                    workspace_git_env(workspace, experiment_dir, env),
                    timeout_sec,
                )

    fail_to_pass = instance_tests(instance, "FAIL_TO_PASS")
    pass_to_pass = instance_tests(instance, "PASS_TO_PASS")
    fail_ok = all(r["status"] == "passed" for r in results["FAIL_TO_PASS"]) if fail_to_pass and prep_ok else None
    pass_ok = all(r["status"] == "passed" for r in results["PASS_TO_PASS"]) if pass_to_pass and prep_ok else None
    overall_ok = (fail_ok is not False) and (pass_ok is not False)
    if not prep_ok:
        overall_ok = False

    return {
        "mode": "local_fallback",
        "runner": runner,
        "pre_validation_steps": prep_results,
        "prep_ok": prep_ok,
        "infrastructure_error": infrastructure_error,
        "results": results,
        "fail_ok": fail_ok,
        "pass_ok": pass_ok,
        "overall_ok": overall_ok,
    }


def main():
    if len(sys.argv) != 4:
        print("usage: validate_swe_run.py <instance.json> <workspace> <experiment_dir>", file=sys.stderr)
        return 2

    instance_path = Path(sys.argv[1]).resolve()
    workspace = Path(sys.argv[2]).resolve()
    experiment_dir = Path(sys.argv[3]).resolve()
    logs_dir = experiment_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    validation_json = experiment_dir / "validation.json"
    validation_md = experiment_dir / "validation.md"
    produced_patch = experiment_dir / "produced.patch"

    instance = json.loads(instance_path.read_text())
    fail_to_pass = instance_tests(instance, "FAIL_TO_PASS")
    pass_to_pass = instance_tests(instance, "PASS_TO_PASS")
    timeout_sec = int(os.environ.get("SWE_VALIDATION_TIMEOUT_SEC", "600"))

    patch_proc = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=str(workspace),
        env=workspace_git_env(workspace, experiment_dir, os.environ.copy()),
        capture_output=True,
        text=True,
    )
    produced_patch.write_text(patch_proc.stdout)

    # Write an early stub so attached validation is visible while the official
    # harness is still running.
    running_payload = {
        "instance_id": instance.get("instance_id"),
        "repo": instance.get("repo"),
        "version": instance.get("version"),
        "base_commit": instance.get("base_commit"),
        "workspace": str(workspace),
        "produced_patch": str(produced_patch),
        "timeout_sec": timeout_sec,
        "status": "running",
        "validation_mode": "pending",
        "summary": None,
        "results": None,
    }
    validation_json.write_text(json.dumps(running_payload, indent=2))
    validation_md.write_text(
        "\n".join(
            [
                "# SWE Validation",
                "",
                f"- Instance: `{instance.get('instance_id')}`",
                f"- Repo: `{instance.get('repo')}`",
                f"- Version: `{instance.get('version')}`",
                f"- Base commit: `{instance.get('base_commit')}`",
                f"- Workspace: `{workspace}`",
                f"- Produced patch: `{produced_patch}`",
                "- Status: `running`",
                "",
            ]
        )
        + "\n"
    )

    runner = None
    prep_results = []
    prep_ok = None
    infrastructure_error = None
    harness_details = None

    if should_use_official_harness(instance):
        harness_details = run_with_lock(
            CACHE_ROOT / "locks" / "official-harness.lock",
            lambda: run_official_harness(instance, workspace, experiment_dir, timeout_sec),
        )
        if "report" in harness_details:
            results, fail_ok, pass_ok, overall_ok = parse_official_results(
                instance, harness_details["report"]
            )
            write_group_logs(logs_dir, "FAIL_TO_PASS", results["FAIL_TO_PASS"])
            write_group_logs(logs_dir, "PASS_TO_PASS", results["PASS_TO_PASS"])
            prep_ok = bool(
                harness_details["report"][instance["instance_id"]].get("patch_successfully_applied")
            )
            if not prep_ok:
                infrastructure_error = "official_patch_apply_failed"
        elif harness_details.get("mode") == "swe_pro_docker":
            results = {"FAIL_TO_PASS": [], "PASS_TO_PASS": []}
            fail_ok = harness_details.get("fail_ok")
            pass_ok = harness_details.get("pass_ok")
            overall_ok = bool(harness_details.get("overall_ok"))
            prep_ok = harness_details.get("infrastructure_error") is None
            infrastructure_error = harness_details.get("infrastructure_error")
        else:
            results = {"FAIL_TO_PASS": [], "PASS_TO_PASS": []}
            fail_ok = None
            pass_ok = None
            overall_ok = False
            prep_ok = False
            infrastructure_error = "official_report_missing"
    else:
        local = run_local_validation(instance, workspace, experiment_dir, logs_dir, timeout_sec)
        runner = local["runner"]
        prep_results = local["pre_validation_steps"]
        prep_ok = local["prep_ok"]
        infrastructure_error = local["infrastructure_error"]
        results = local["results"]
        fail_ok = local["fail_ok"]
        pass_ok = local["pass_ok"]
        overall_ok = local["overall_ok"]

    payload = {
        "instance_id": instance.get("instance_id"),
        "repo": instance.get("repo"),
        "version": instance.get("version"),
        "base_commit": instance.get("base_commit"),
        "workspace": str(workspace),
        "produced_patch": str(produced_patch),
        "status": "completed",
        "runner": runner,
        "timeout_sec": timeout_sec,
        "pre_validation_steps": prep_results,
        "prep_ok": prep_ok,
        "infrastructure_error": infrastructure_error,
        "validation_mode": "official_harness" if harness_details else "local_fallback",
        "official_harness": harness_details,
        "summary": {
            "fail_to_pass_total": len(fail_to_pass),
            "pass_to_pass_total": len(pass_to_pass),
            "fail_to_pass_all_passed": fail_ok,
            "pass_to_pass_all_passed": pass_ok,
            "overall_ok": overall_ok,
        },
        "results": results,
    }
    validation_json.write_text(json.dumps(payload, indent=2))

    lines = []
    lines.append("# SWE Validation\n")
    lines.append(f"- Instance: `{instance.get('instance_id')}`")
    lines.append(f"- Repo: `{instance.get('repo')}`")
    lines.append(f"- Version: `{instance.get('version')}`")
    lines.append(f"- Base commit: `{instance.get('base_commit')}`")
    lines.append(f"- Workspace: `{workspace}`")
    lines.append(f"- Produced patch: `{produced_patch}`")
    lines.append(f"- Validation mode: `{payload['validation_mode']}`")
    if runner:
        lines.append(f"- Runner: `{' '.join(runner)}`")
    lines.append(f"- Timeout: `{timeout_sec}s`\n")
    if infrastructure_error:
        lines.append(f"- Infrastructure error: `{infrastructure_error}`\n")
    if harness_details:
        lines.append("## Official Harness\n")
        lines.append(f"- Report path: `{harness_details.get('report_path')}`")
        lines.append(f"- Test output path: `{harness_details.get('test_output_path')}`")
        lines.append(f"- Instance log path: `{harness_details.get('instance_log_path')}`")
        lines.append(f"- Harness status: `{harness_details['run']['status']}`")
        lines.append(f"- Harness return code: `{harness_details['run']['returncode']}`\n")
    if prep_results:
        lines.append("## Pre-validation Steps\n")
        lines.append("| Command | Status | Seconds | Return code |")
        lines.append("|---|---|---:|---:|")
        for r in prep_results:
            rc = "" if r["returncode"] is None else str(r["returncode"])
            lines.append(f"| `{' '.join(r['cmd'])}` | `{r['status']}` | {r['duration_sec']} | {rc} |")
        lines.append("")
    lines.append("## Summary\n")
    lines.append(f"- `FAIL_TO_PASS` all passed: `{fail_ok}`")
    lines.append(f"- `PASS_TO_PASS` all passed: `{pass_ok}`")
    lines.append(f"- Overall validation ok: `{overall_ok}`\n")
    for group_name in ["FAIL_TO_PASS", "PASS_TO_PASS"]:
        lines.append(f"## {group_name}\n")
        lines.append("| Test | Status | Return code |")
        lines.append("|---|---|---:|")
        for r in results[group_name]:
            rc = "" if r["returncode"] is None else str(r["returncode"])
            lines.append(f"| `{r['test']}` | `{r['status']}` | {rc} |")
        lines.append("")
    validation_md.write_text("\n".join(lines) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
