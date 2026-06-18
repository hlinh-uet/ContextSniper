"""ContextSniperFilterPlugin — Filter context shortening for ContextSniper pipeline.

This module provides the integration layer between Filter shortening primitives
and the ContextSniper (ContextSniper) pipeline. It mirrors the FilterMemoryPlugin
interface from memory but is adapted for ContextSniper's data formats:
- list[dict] instead of list[SessionMessage]
- ContextSniper's MemoryService / SessionManager / ArchiveStore integration points

Filter is NOT a compression tool — it SHORTENS context via Filter and format
conversion while PRESERVING information density.

Integration points:
  - session/session_manager.py       — short_session_messages before LLM compress
  - session/archive_store.py         — short_archive_overview before archive write
  - code-version/server/memory_service.py — short_retrieved_blocks, short_query in compose
"""

from __future__ import annotations

from dataclasses import dataclass

from filter.core.shorter import (
    ShortenResult,
    PipelineShorter,
    shorten_text as filter_shorten_text,
    make_code_shorter,
    make_log_shorter,
    make_generic_shorter,
    make_memory_shorter,
)
from filter.core.adaptive import adaptive_shorten, detect_content_type, ContentType


@dataclass
class MemoryShorteningStats:
    """Stats from a context shortening operation for tracking."""
    original_chars: int
    shortened_chars: int
    tokens_saved_estimate: int
    shortening_ratio: float
    stage: str  # where in the pipeline


class ContextSniperFilterPlugin:
    """Filter context shortening plugin for ContextSniper pipeline.

    Provides context shortening at key pipeline stages to reduce token/size
    cost without losing semantic signal.

    Usage:
        from filter import get_plugin
        plugin = get_plugin()
        shortened = plugin.short_session_messages(messages)
        shortened = plugin.short_retrieved_blocks(items)
    """

    def __init__(self):
        self._memory_shorter = make_memory_shorter()
        self._generic_shorter = make_generic_shorter()
        self._code_shorter = make_code_shorter()
        self._log_shorter = make_log_shorter()
        self._stats: list[MemoryShorteningStats] = []

    # ─── Session-level shortening ──────────────────────────────────────────────

    def short_session_messages(
        self,
        messages: list[dict],
        mode: str = "generic",
        max_lines: int = 60,
    ) -> list[dict]:
        """Shorten session message contents before LLM processing.

        Works with list[dict] ({"role": str, "content": str}) not SessionMessage objects.

        This runs BEFORE the SessionCompressor to reduce input size to the LLM
        (fewer tokens = cheaper processing).

        Args:
            messages: Session message dicts to shorten
            mode: Shortening mode (generic, code, log)
            max_lines: Max lines per message

        Returns:
            Messages with shortened content (new dicts, original unchanged)
        """
        shorter = self._get_shorter(mode, max_lines)
        shortened_messages = []

        for msg in messages:
            content = msg.get("content", "")
            if not content:
                shortened_messages.append(msg)
                continue

            result = shorter.shorten(content)
            # Only replace if shortening saved >5%
            if result.shortening_ratio < 0.95:
                shortened_messages.append(
                    {**msg, "content": result.text}
                )
                self._record_stats(result, "session_messages")
            else:
                shortened_messages.append(msg)

        return shortened_messages

    def short_session_window_text(self, text: str) -> tuple[str, MemoryShorteningStats]:
        """Shorten the compressed_text field in SessionWindowState.

        Args:
            text: The compressed window text

        Returns:
            Tuple of (shortened_text, stats)
        """
        result = self._generic_shorter.shorten(text)
        stats = MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=result.shortened_chars,
            tokens_saved_estimate=result.tokens_saved_estimate,
            shortening_ratio=result.shortening_ratio,
            stage="session_window",
        )
        return result.text, stats

    # ─── Archive-level shortening ─────────────────────────────────────────────

    def short_archive_overview(self, overview: str, max_chars: int = 1500) -> tuple[str, MemoryShorteningStats]:
        """Shorten archive overview to fit budget.

        Applied in archive_store.py before writing ContextNode.

        Args:
            overview: Archive overview text
            max_chars: Character budget

        Returns:
            Tuple of (shortened_overview, stats)
        """
        result = self._generic_shorter.shorten(overview)
        # If still over budget, truncate
        if len(result.text) > max_chars:
            result.text = result.text[:max_chars] + f"\n// ... truncated from {len(result.text)} chars ..."
        stats = MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=len(result.text),
            tokens_saved_estimate=result.tokens_saved_estimate,
            shortening_ratio=len(result.text) / result.original_chars if result.original_chars > 0 else 1.0,
            stage="archive_overview",
        )
        return result.text, stats

    # Alias for backward compatibility
    compress_archive_overview = short_archive_overview

    # ─── Retrieval-level shortening ──────────────────────────────────────────

    def short_retrieved_block(self, text: str, budget_chars: int = 2000) -> tuple[str, MemoryShorteningStats]:
        """Shorten a single retrieved evidence block to fit token budget.

        Applied in compose() when assembling retrieved_evidence.

        Args:
            text: Retrieved block text
            budget_chars: Character budget for the block

        Returns:
            Tuple of (shortened_text, stats)
        """
        result = self._memory_shorter.shorten(text)
        if len(result.text) > budget_chars:
            result.text = result.text[:budget_chars]
        stats = MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=len(result.text),
            tokens_saved_estimate=result.tokens_saved_estimate,
            shortening_ratio=len(result.text) / result.original_chars if result.original_chars > 0 else 1.0,
            stage="retrieved_block",
        )
        return result.text, stats

    def short_retrieved_blocks(self, items: list[dict], budget_chars: int = 2000) -> list[dict]:
        """Shorten all retrieved blocks in a working_set item list.

        Applied in compose() between _truncate_by_score_gap and _format_working_set.

        Args:
            items: List of working_set items with "content" or "abstract" fields
            budget_chars: Maximum characters per item after shortening

        Returns:
            Items with shortened content (new dicts, original unchanged)
        """
        shortened_items = []
        for item in items:
            content = item.get("content", "")
            abstract = item.get("abstract", "")

            if content:
                result = self._memory_shorter.shorten(content)
                # Enforce budget cap
                if len(result.text) > budget_chars:
                    result.text = result.text[:budget_chars]
                if result.shortening_ratio < 0.95:
                    item = {**item, "content": result.text}
                    self._record_stats(result, "retrieved_block")
            elif abstract:
                result = self._memory_shorter.shorten(abstract)
                if len(result.text) > budget_chars:
                    result.text = result.text[:budget_chars]
                if result.shortening_ratio < 0.95:
                    item = {**item, "abstract": result.text}
                    self._record_stats(result, "retrieved_block")

            shortened_items.append(item)
        return shortened_items

    def short_query(self, query: str) -> tuple[str, MemoryShorteningStats]:
        """Shorten the compose() query before retrieval.

        Applied in compose() to shorten the query extracted from messages.

        Args:
            query: Raw query text

        Returns:
            Tuple of (shortened_query, stats)
        """
        result = self._generic_shorter.shorten(query)
        stats = MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=result.shortened_chars,
            tokens_saved_estimate=result.tokens_saved_estimate,
            shortening_ratio=result.shortening_ratio,
            stage="query",
        )
        return result.text, stats

    # ─── LLM output shortening ────────────────────────────────────────────────

    def short_llm_output(self, text: str) -> tuple[str, MemoryShorteningStats]:
        """Shorten LLM response before storing in memory.

        Applied when storing assistant responses into memory.

        Args:
            text: LLM output text

        Returns:
            Tuple of (shortened_text, stats)
        """
        result = self._generic_shorter.shorten(text)
        stats = MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=result.shortened_chars,
            tokens_saved_estimate=result.tokens_saved_estimate,
            shortening_ratio=result.shortening_ratio,
            stage="llm_output",
        )
        return result.text, stats

    # Alias for backward compatibility
    compress_llm_output = short_llm_output

    # ─── Adaptive Shortening ───────────────────────────────────────────────────

    def short_adaptive(self, text: str, max_lines: int | None = None) -> tuple[str, MemoryShorteningStats]:
        """Adaptively shorten text by detecting content type and applying optimal strategy.

        Args:
            text: Input text to shorten
            max_lines: Optional max lines override

        Returns:
            Tuple of (shortened_text, stats)
        """
        result = adaptive_shorten(text, max_lines=max_lines)
        stats = MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=result.shortened_chars,
            tokens_saved_estimate=max(0, result.original_chars - result.shortened_chars) // 4,
            shortening_ratio=result.shortening_ratio,
            stage=f"adaptive_{result.content_type.name.lower()}",
        )
        return result.text, stats

    # ─── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> list[MemoryShorteningStats]:
        """Return all shortening stats recorded this session."""
        return self._stats.copy()

    def reset_stats(self):
        """Clear recorded stats."""
        self._stats.clear()

    def total_savings(self) -> dict:
        """Aggregate savings across all recorded shortenings."""
        if not self._stats:
            return {"total_original_chars": 0, "total_shortened_chars": 0, "total_tokens_saved": 0}
        total_original = sum(s.original_chars for s in self._stats)
        total_shortened = sum(s.shortened_chars for s in self._stats)
        return {
            "total_original_chars": total_original,
            "total_shortened_chars": total_shortened,
            "total_tokens_saved": sum(s.tokens_saved_estimate for s in self._stats),
            "operation_count": len(self._stats),
        }

    # ─── Internal ─────────────────────────────────────────────────────────────

    def _get_shorter(self, mode: str, max_lines: int) -> PipelineShorter:
        if mode == "code":
            return make_code_shorter(max_lines)
        elif mode == "log":
            return make_log_shorter(max_lines)
        else:
            return make_generic_shorter(max_lines)

    def _record_stats(self, result: ShortenResult, stage: str):
        self._stats.append(MemoryShorteningStats(
            original_chars=result.original_chars,
            shortened_chars=result.shortened_chars,
            tokens_saved_estimate=result.tokens_saved_estimate,
            shortening_ratio=result.shortening_ratio,
            stage=stage,
        ))


# Global singleton for ContextSniper pipeline to reuse
_plugin: ContextSniperFilterPlugin | None = None


def get_plugin() -> ContextSniperFilterPlugin:
    """Get the global ContextSniperFilterPlugin instance."""
    global _plugin
    if _plugin is None:
        _plugin = ContextSniperFilterPlugin()
    return _plugin