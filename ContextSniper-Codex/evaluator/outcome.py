"""APR outcome and Codex token accounting used by the benchmark adapter."""

from __future__ import annotations

import json
from pathlib import Path


AUTHORITATIVE_TEST_ID_SOURCES = frozenset({
    "caller-supplied",
    "configured-pattern",
    "framework-marker",
    "structured-report",
})


def validation_failed_test_ids(snapshot: dict | None) -> list[str]:
    snapshot = snapshot or {}
    values = snapshot.get("failed_test_ids")
    if not isinstance(values, list):
        values = snapshot.get("post_failed_tests")
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def classify_patch_outcome(baseline: dict | None, patched: dict | None) -> str:
    """Classify a patch by comparing the same validation scope before and after it."""
    baseline = baseline or {}
    patched = patched or {}
    baseline_status = str(baseline.get("status") or "invalid")
    patched_status = str(patched.get("status") or "invalid")
    if baseline_status not in {"plausible", "failing"} or patched_status not in {
        "plausible", "failing",
    }:
        return "invalid"
    if str(baseline.get("validation_error") or "").strip() or str(
        patched.get("validation_error") or ""
    ).strip():
        return "invalid"
    if baseline_status == "failing" and str(
        baseline.get("test_id_source") or ""
    ) not in AUTHORITATIVE_TEST_ID_SOURCES:
        return "invalid"
    if patched_status == "failing" and str(
        patched.get("test_id_source") or ""
    ) not in AUTHORITATIVE_TEST_ID_SOURCES:
        return "invalid"

    initial = set(validation_failed_test_ids(baseline))
    post = set(validation_failed_test_ids(patched))
    if (baseline_status == "failing") != bool(initial):
        return "invalid"
    if (patched_status == "failing") != bool(post):
        return "invalid"
    if not post:
        return "plausible" if initial else "nonefix"
    fixed = initial - post
    regressions = post - initial
    if fixed and regressions:
        return "noisefix"
    if fixed:
        return "cleanfix"
    if regressions:
        return "negfix"
    return "nonefix"


def classify_validation_result(baseline: dict | None, patched: dict | None) -> dict:
    """Attach the public APR outcome and its auditable test-set comparison."""
    result = dict(patched or {})
    post_validation_status = str(result.get("status") or "invalid")
    outcome = classify_patch_outcome(baseline, result)
    initial = validation_failed_test_ids(baseline)
    post = validation_failed_test_ids(result)
    fixed: list[str] = []
    regressions: list[str] = []
    if outcome != "invalid":
        fixed = sorted(set(initial) - set(post))
        regressions = sorted(set(post) - set(initial))
    result.update(
        {
            "post_validation_status": post_validation_status,
            "status": outcome,
            "initial_failed_test_ids": initial,
            "post_failed_test_ids": post,
            "fixed_test_ids": fixed,
            "regression_test_ids": regressions,
            "classification_basis_valid": outcome != "invalid",
        }
    )
    return result


def event_usage(path: Path) -> dict:
    totals: dict[str, int] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "turn.completed":
                for key, value in event.get("usage", {}).items():
                    if type(value) is int:
                        totals[key] = totals.get(key, 0) + value
    return totals
