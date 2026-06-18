#!/usr/bin/env python3
"""Log OpenAI-compatible LLM requests while forwarding them upstream.

This is a small debugging proxy for OpenClaw/OpenAI-compatible providers.
Point a provider baseUrl at this server, and set the upstream base URL with
--upstream or LLM_LISTENER_UPSTREAM_BASE_URL.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOP_BY_HOP_HEADERS = {
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def try_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"_non_json_body": raw.decode("utf-8", errors="replace")}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


def strip_hop_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


def make_handler(upstream: str, log_path: Path, redact: bool) -> type[BaseHTTPRequestHandler]:
    upstream_url = urllib.parse.urlparse(upstream.rstrip("/"))
    if upstream_url.scheme not in {"http", "https"} or not upstream_url.netloc:
        raise ValueError(f"Invalid upstream URL: {upstream!r}")

    class LlmProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("[%s] %s\n" % (now_iso(), fmt % args))

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._forward()

        def _write_log(self, record: dict[str, Any]) -> None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json_dumps(record) + "\n")

        def _forward(self) -> None:
            request_id = uuid.uuid4().hex
            started = time.time()
            length = int(self.headers.get("content-length") or "0")
            request_body = self.rfile.read(length) if length else b""
            request_headers = {k: v for k, v in self.headers.items()}
            upstream_path = (upstream_url.path.rstrip("/") + self.path) if upstream_url.path else self.path

            outgoing_headers = strip_hop_headers(request_headers)
            outgoing_headers["Host"] = upstream_url.netloc
            if request_body and "Content-Length" not in outgoing_headers:
                outgoing_headers["Content-Length"] = str(len(request_body))

            log_record: dict[str, Any] = {
                "id": request_id,
                "ts": now_iso(),
                "method": self.command,
                "path": self.path,
                "upstream": urllib.parse.urlunparse(
                    (upstream_url.scheme, upstream_url.netloc, upstream_path, "", "", "")
                ),
                "request_headers": redact_headers(request_headers) if redact else request_headers,
                "request_json": try_json(request_body),
            }

            conn_cls = http.client.HTTPSConnection if upstream_url.scheme == "https" else http.client.HTTPConnection
            conn_kwargs: dict[str, Any] = {"timeout": 300}
            if upstream_url.scheme == "https":
                conn_kwargs["context"] = ssl.create_default_context()
            conn = conn_cls(upstream_url.netloc, **conn_kwargs)

            try:
                conn.request(self.command, upstream_path, body=request_body or None, headers=outgoing_headers)
                resp = conn.getresponse()
                response_headers = {k: v for k, v in resp.getheaders()}
                self.send_response(resp.status, resp.reason)
                for key, value in strip_hop_headers(response_headers).items():
                    if key.lower() == "content-length":
                        continue
                    self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()

                captured = bytearray()
                captured_limit = int(os.environ.get("LLM_LISTENER_CAPTURE_RESPONSE_BYTES", "2000000"))
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    if len(captured) < captured_limit:
                        remaining = captured_limit - len(captured)
                        captured.extend(chunk[:remaining])
                    self.wfile.write(chunk)
                    self.wfile.flush()
                response_body = bytes(captured)
                elapsed_ms = int((time.time() - started) * 1000)

                log_record.update(
                    {
                        "elapsed_ms": elapsed_ms,
                        "status": resp.status,
                        "response_headers": redact_headers(response_headers) if redact else response_headers,
                        "response_json": try_json(response_body),
                        "response_captured_bytes": len(response_body),
                        "response_capture_truncated": len(response_body) >= captured_limit,
                    }
                )
            except Exception as exc:
                elapsed_ms = int((time.time() - started) * 1000)
                body = json_dumps({"error": f"llm listener proxy error: {exc}"}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log_record.update({"elapsed_ms": elapsed_ms, "status": 502, "proxy_error": repr(exc)})
            finally:
                conn.close()
                self._write_log(log_record)

    return LlmProxyHandler


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-compatible LLM request logging proxy.")
    parser.add_argument("--host", default=os.environ.get("LLM_LISTENER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("LLM_LISTENER_PORT", "8787")))
    parser.add_argument(
        "--upstream",
        default=os.environ.get("LLM_LISTENER_UPSTREAM_BASE_URL", "https://api.deepseek.com"),
        help="Upstream provider base URL, e.g. https://api.deepseek.com",
    )
    parser.add_argument(
        "--log",
        default=os.environ.get("LLM_LISTENER_LOG", str(Path.home() / ".cache/contextsniper-llm-listener/llm_calls.jsonl")),
        help="JSONL log path.",
    )
    parser.add_argument("--no-redact", action="store_true", help="Do not redact auth-like headers in logs.")
    args = parser.parse_args()

    log_path = Path(args.log).expanduser()
    handler = make_handler(args.upstream, log_path, redact=not args.no_redact)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"LLM listener on http://{args.host}:{args.port}", flush=True)
    print(f"Forwarding to {args.upstream.rstrip('/')}", flush=True)
    print(f"Logging JSONL to {log_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping listener.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
