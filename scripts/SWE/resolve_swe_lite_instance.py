#!/usr/bin/env python3
"""Resolve a SWE-bench Lite instance row and write shell exports for runners."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DATASET = "princeton-nlp/SWE-bench_Lite"
SPLIT = "test"
ROWS_API = os.environ.get("SWE_ROWS_API", "https://datasets-server.huggingface.co/rows")
PARQUET_URL = os.environ.get(
    "SWE_PARQUET_URL",
    "https://huggingface.co/datasets/"
    f"{DATASET}/resolve/main/data/{SPLIT}-00000-of-00001.parquet",
)


def log(message: str) -> None:
    print(f"[swe-resolve] {message}", file=sys.stderr)


def request_bytes(url: str, *, timeout: int = 120, attempts: int = 5) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = min(2 ** (attempt - 1), 8)
            log(f"{url} failed on attempt {attempt}/{attempts}: {exc}; retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"{url} failed after {attempts} attempts: {last_error}") from last_error


def fetch_rows(offset: int, length: int = 100) -> dict:
    query = urllib.parse.urlencode(
        {
            "dataset": DATASET,
            "config": "default",
            "split": SPLIT,
            "offset": str(offset),
            "length": str(length),
        }
    )
    return json.loads(request_bytes(f"{ROWS_API}?{query}").decode("utf-8"))


def find_via_rows_api(target: str) -> dict:
    first = fetch_rows(0, 1)
    total = int(first.get("num_rows_total", 0))
    if not target:
        return first["rows"][0]["row"]
    for offset in range(0, max(total, 0), 100):
        block = fetch_rows(offset, 100)
        for item in block.get("rows", []):
            row = item["row"]
            if row.get("instance_id") == target:
                return row
    raise KeyError(f"No instance_id={target!r} in {SPLIT!r} split of {DATASET} (n={total}).")


def ensure_parquet(repo_base: Path) -> Path:
    parquet_path = repo_base / "_datasets" / "SWE-bench_Lite" / "data" / f"{SPLIT}.parquet"
    if parquet_path.is_file() and parquet_path.stat().st_size > 0:
        return parquet_path
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = parquet_path.with_suffix(".parquet.tmp")
    tmp_path.write_bytes(request_bytes(PARQUET_URL))
    tmp_path.replace(parquet_path)
    return parquet_path


def load_parquet_rows(parquet_path: Path) -> list[dict]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError(
            "pyarrow is required for SWE metadata parquet fallback; "
            "run ./bootstrap.sh --install-swe-deps"
        ) from exc
    return pq.read_table(parquet_path).to_pylist()


def find_via_parquet(repo_base: Path, target: str) -> dict:
    parquet_path = ensure_parquet(repo_base)
    rows = load_parquet_rows(parquet_path)
    if not target:
        return rows[0]
    for row in rows:
        if row.get("instance_id") == target:
            return row
    raise KeyError(f"No instance_id={target!r} in local parquet copy of {DATASET}.")


def esc(value: str) -> str:
    return json.dumps(value)


def build_prompt(row: dict, prompt_kind: str) -> tuple[str, str]:
    repo = row["repo"]
    base_commit = row["base_commit"]
    problem = (row.get("problem_statement") or "").strip()
    hints = (row.get("hints_text") or "").strip()

    if prompt_kind == "openclaw_plain":
        prompt = f"""You are working on a real open-source project as in the SWE-bench Lite benchmark.

Repository: {repo}
Checkout: parent commit (state before the fix) is {base_commit}. The codebase is already checked out in this directory.
Do not look up or apply the original solution PR or patch from the web.

Official issue text (`problem_statement`):

---
{problem}
---
"""
        if hints:
            prompt += f"""
Optional prior discussion (`hints_text`):
---
{hints}
---
"""
        prompt += """
Important SWE-bench rule:
- Do not edit benchmark tests, test files, or test fixtures.
- Make the minimal production source-code change needed to satisfy the issue.
- You may run existing tests to reproduce and verify, but the final patch should
  be source-only unless the issue explicitly asks for test changes.
- If the issue text mentions behavior that was already added for a related code
  path, search for that related behavior and keep the public exception semantics
  consistent. Do not use Python `assert` for runtime user-input validation.
- For Flask blueprint dot-name tasks, validate both sides of the issue text:
  dotted blueprint names must raise `ValueError`, and the existing dotted
  endpoint / view-function-name checks must raise `ValueError` too. A patch
  that leaves those endpoint checks as `AssertionError` is incomplete and will
  fail validation; do not preserve that assertion behavior.
"""
        return prompt, "PROMPT_OPENCLAW_PLAIN.txt"

    prompt = f"""{problem}

Generate a patch that resolves the issue.
"""
    if prompt_kind == "openclaw_contextsniper":
        prompt += """
Important SWE-bench rule:
- Do not edit benchmark tests, test files, or test fixtures.
- Make the minimal production source-code change needed to satisfy the issue.
- You may run existing tests to reproduce and verify, but the final patch should
  be source-only unless the issue explicitly asks for test changes.
"""
        return prompt, "PROMPT_OPENCLAW_CONTEXTSNIPER.txt"
    return prompt, "PROMPT_CONTEXTSNIPER.txt"


def main() -> int:
    target = (os.environ.get("SWE_LITE_INSTANCE_ID") or "").strip()
    repo_base = Path(os.environ["REPO_BASE"])
    prompt_kind = os.environ.get("SWE_PROMPT_KIND", "basic")
    cached_inst_path = repo_base / target / "instance.json" if target else None

    if cached_inst_path and cached_inst_path.is_file():
        row = json.loads(cached_inst_path.read_text(encoding="utf-8"))
    else:
        try:
            row = find_via_rows_api(target)
        except Exception as api_error:
            log(f"datasets-server path failed; trying direct parquet fallback: {api_error}")
            row = find_via_parquet(repo_base, target)

    iid = row["instance_id"]
    inst_dir = repo_base / iid
    inst_dir.mkdir(parents=True, exist_ok=True)
    inst_path = inst_dir / "instance.json"
    inst_path.write_text(json.dumps(row, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    prompt, prompt_name = build_prompt(row, prompt_kind)
    prompt_path = inst_dir / prompt_name
    prompt_path.write_text(prompt, encoding="utf-8")

    print(f"export SWE_INSTANCE_ID={esc(iid)}")
    print(f"export SWE_REPO={esc(row['repo'])}")
    print(f"export SWE_BASE_COMMIT={esc(row['base_commit'])}")
    print(f"export SWE_INSTANCE_JSON={esc(str(inst_path.resolve()))}")
    print(f"export SWE_PROMPT_FILE={esc(str(prompt_path.resolve()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
