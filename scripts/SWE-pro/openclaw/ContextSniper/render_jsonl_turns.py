#!/usr/bin/env python3
"""Render an OpenClaw session JSONL as readable turns.

Usage:
  python render_jsonl_turns.py /path/to/session.jsonl

The renderer understands OpenClaw's session JSONL shape, including user and
assistant messages, toolCall blocks, and toolResult messages.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _shorten(text: str, limit: int = 0) -> str:
    text = str(text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... [truncated]"


def _json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            parts.append(str(item.get("text") or ""))
        elif item_type == "thinking":
            parts.append(str(item.get("thinking") or ""))
        elif item_type == "toolResult":
            parts.append(str(item.get("content") or ""))
    return "\n".join(p for p in parts if p).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="Path to OpenClaw session JSONL")
    parser.add_argument(
        "--show-thinking",
        action="store_true",
        help="Include assistant thinking blocks",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=0,
        help="Max chars to print for large text blocks; 0 disables truncation",
    )
    parser.add_argument(
        "--stdout-log",
        type=Path,
        help="Optional OpenClaw stdout log to append as run status.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.jsonl
    if not path.is_file():
        raise SystemExit(f"Not found: {path}")

    turn_no = 0
    in_assistant_block = False

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            typ = obj.get("type")
            if typ == "session":
                print("=== Session ===")
                print(_json_pretty({k: v for k, v in obj.items() if k != "type"}))
                continue
            if typ == "model_change":
                print("\n=== Model ===")
                print(_json_pretty({k: v for k, v in obj.items() if k not in {"type", "parentId"}}))
                continue

            if typ != "message":
                continue

            message = obj.get("message") or {}
            role = message.get("role")
            content = message.get("content") or []

            if role == "user":
                turn_no += 1
                in_assistant_block = False
                print(f"\n=== Turn {turn_no} | User ===")
                print(_shorten(_text_from_content(content), args.max_chars))
                continue

            if role == "toolResult":
                name = message.get("toolName") or "(unknown)"
                print(f"\n[tool_result] {name} line={line_no} isError={message.get('isError')}")
                text = _text_from_content(content)
                if text:
                    print(_shorten(text, args.max_chars))
                details = message.get("details")
                if details:
                    print("[details]")
                    print(_shorten(_json_pretty(details), args.max_chars))
                continue

            if role != "assistant" or not isinstance(content, list):
                continue

            if not in_assistant_block:
                print(f"\n=== Turn {turn_no} | Assistant ===")
                in_assistant_block = True

            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "thinking" and args.show_thinking:
                    print("[thinking]")
                    print(_shorten(str(item.get("thinking") or ""), args.max_chars))
                elif item_type == "text":
                    print("[text]")
                    print(_shorten(str(item.get("text") or ""), args.max_chars))
                elif item_type == "toolCall":
                    print(f"[tool_call] {item.get('name') or '(unknown)'} line={line_no}")
                    print(_shorten(_json_pretty(item.get("arguments")), args.max_chars))

            if message.get("stopReason") == "error" or message.get("errorMessage"):
                print("[error]")
                print(_shorten(str(message.get("errorMessage") or "assistant stopped with error"), args.max_chars))

    if args.stdout_log and args.stdout_log.is_file():
        text = args.stdout_log.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            print("\n=== Run Status ===")
            print(_shorten(text, args.max_chars))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
