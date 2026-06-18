#!/usr/bin/env python3
"""Render a Claude session JSONL as readable turns.

Usage:
  python render_jsonl_turns.py /path/to/session.jsonl

Behavior:
  - Prints user and assistant turns in order.
  - Groups assistant text/thinking/tool calls together.
  - If a turn calls `mcp__contextsniper__search_code`, prints the MCP result inline.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path
from typing import Any


def _shorten(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if limit <= 0:
        return text
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... [truncated]"


def _json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _decode_nested_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value
        return _decode_nested_json(parsed)
    if isinstance(value, list):
        return [_decode_nested_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_nested_json(item) for key, item in value.items()}
    return value


def _maybe_pretty_json_string(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return value
    parsed = _decode_nested_json(parsed)
    if isinstance(parsed, (dict, list)):
        return _json_pretty(parsed)
    return value


def _indent_block(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _format_search_code_result(text: str) -> str:
    """Render search_code results with multiline snippet/uri blocks."""
    normalized = _maybe_pretty_json_string(text)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return normalized

    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return normalized

    lines: list[str] = []
    lines.append("result:")
    for key in ("ok", "query", "hit_count"):
        if key in result:
            lines.append(f"  {key}: {result[key]!r}")

    hits = result.get("hits")
    if isinstance(hits, list):
        lines.append("  hits:")
        for idx, hit in enumerate(hits, start=1):
            if not isinstance(hit, dict):
                lines.append(f"    - {hit!r}")
                continue
            lines.append(f"    - hit {idx}:")
            for key in ("score",):
                if key in hit:
                    lines.append(f"        {key}: {hit[key]!r}")
            for key in ("uri", "snippet", "content_excerpt", "overview", "abstract"):
                if key in hit:
                    value = str(hit.get(key) or "")
                    lines.append(f"        {key}: |")
                    lines.append(_indent_block(value, "          "))

            for key, value in hit.items():
                if key in {"score", "uri", "snippet", "content_excerpt", "overview", "abstract"}:
                    continue
                lines.append(f"        {key}: {value!r}")

    for key, value in payload.items():
        if key == "result":
            continue
        lines.append(f"{key}: {value!r}")

    return "\n".join(lines)


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "tool_result":
                    parts.append(str(item.get("content") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p).strip()
    return str(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", type=Path, help="Path to Claude session JSONL")
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = args.jsonl
    if not path.is_file():
        raise SystemExit(f"Not found: {path}")

    tool_results: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            events.append(obj)

            if obj.get("type") != "user":
                continue
            message = obj.get("message") or {}
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "tool_result":
                    continue
                tool_use_id = item.get("tool_use_id")
                if tool_use_id:
                    tool_results[str(tool_use_id)] = item

    turn_no = 0
    in_assistant_block = False

    for obj in events:
        typ = obj.get("type")
        if typ == "user":
            message = obj.get("message") or {}
            content = message.get("content")

            # Skip tool-result pseudo-user entries; they are shown under the tool call instead.
            if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "tool_result" for item in content
            ):
                continue

            turn_no += 1
            in_assistant_block = False
            text = _extract_text_content(content)
            print(f"\n=== Turn {turn_no} | User ===")
            print(_shorten(text, args.max_chars))

        elif typ == "assistant":
            message = obj.get("message") or {}
            content = message.get("content") or []
            if not isinstance(content, list):
                continue

            printed_header = False
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_uses: list[dict[str, Any]] = []

            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "text":
                    text_parts.append(str(item.get("text") or ""))
                elif item_type == "thinking":
                    thinking_parts.append(str(item.get("thinking") or ""))
                elif item_type == "tool_use":
                    tool_uses.append(item)

            if text_parts or thinking_parts or tool_uses:
                if not in_assistant_block:
                    print(f"\n=== Turn {turn_no} | Assistant ===")
                    in_assistant_block = True
                printed_header = True

            if args.show_thinking and thinking_parts:
                print("[thinking]")
                print(_shorten("\n\n".join(thinking_parts), args.max_chars))

            if text_parts:
                print("[text]")
                print(_shorten("\n\n".join(text_parts), args.max_chars))

            for tool in tool_uses:
                name = str(tool.get("name") or "")
                tool_input = tool.get("input")
                tool_use_id = str(tool.get("id") or "")
                if not printed_header:
                    if not in_assistant_block:
                        print(f"\n=== Turn {turn_no} | Assistant ===")
                        in_assistant_block = True
                    printed_header = True
                print(f"[tool_use] {name}")
                print(_json_pretty(tool_input))

                result = tool_results.get(tool_use_id)
                if result is None:
                    print("[tool_result]")
                    print("(no recorded result found)")
                    continue

                content = result.get("content")
                is_error = bool(result.get("is_error"))
                label = "[tool_result:error]" if is_error else "[tool_result]"
                print(label)
                if name == "mcp__contextsniper__search_code":
                    text = _extract_text_content(content)
                    text = _format_search_code_result(text)
                    print(_shorten(text, args.max_chars))
                else:
                    if isinstance(content, (dict, list)):
                        print(_shorten(_json_pretty(content), args.max_chars))
                    else:
                        text = str(content or "")
                        text = _maybe_pretty_json_string(text)
                        print(_shorten(text, args.max_chars))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
