"""Minimal JSON HTTP client for ContextSniper MCP bridge."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


def contextsniper_base_url() -> str:
    return os.environ.get("CONTEXTSNIPER_URL", "http://127.0.0.1:8090").rstrip("/")


def post_json(path: str, body: dict[str, Any], *, timeout_sec: float = 600.0) -> dict[str, Any]:
    url = contextsniper_base_url() + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    wait_until = time.monotonic() + float(os.environ.get("CONTEXTSNIPER_BACKEND_WAIT_ON_TOOL", "60"))
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {url}: {err_body}") from exc
        except urllib.error.URLError:
            if time.monotonic() >= wait_until:
                raise
            time.sleep(1)
    if not raw.strip():
        return {}
    return json.loads(raw)


def identity_fields(extra: dict[str, Any]) -> dict[str, Any]:
    base = {
        "accountId": os.environ.get("CONTEXTSNIPER_ACCOUNT_ID", os.environ.get("CONTEXTSNIPER_ACCOUNT_ID", "acct-demo")),
        "userId": os.environ.get("CONTEXTSNIPER_USER_ID", os.environ.get("CONTEXTSNIPER_USER_ID", "u-mcp")),
        "agentId": os.environ.get("CONTEXTSNIPER_AGENT_ID", os.environ.get("CONTEXTSNIPER_AGENT_ID", "mcp-smoke")),
        "sessionId": os.environ.get("CONTEXTSNIPER_SESSION_ID", "mcp-session"),
    }
    out = {**base, **extra}
    return {k: v for k, v in out.items() if v is not None}
