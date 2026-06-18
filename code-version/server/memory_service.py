"""MemoryService — Core ContextSniper business logic, decoupled from transport.

Used by both:
  - server/app.py        (HTTP / Flask mode)
  - bridge/memory_api.py (CLI / subprocess mode)
"""

from __future__ import annotations

import logging
import json
import math
import os
import re
import hashlib
import shutil
import subprocess
import threading
import time
from collections import Counter
from glob import glob
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _dt
from pathlib import Path
from uuid import uuid4

from core.models import RequestContext, RetrievalConfig, TokenBudget, ArchiveRef, ComposedContext, Role
from session.models import ArchiveEntry, SessionWindowState
from session.session_manager import SessionManager
from session.rolling_compressor import RollingCompressor
from providers.config import ProviderConfig
from providers.llm import get_openai_llm
from providers.unified_config import ContextSniperConfig, get_config
from retrieval.pipeline import RetrievalPipeline
from retrieval.query_planner import QueryPlanner, sanitize_query
from retrieval.seed_retriever import SeedRetriever
from retrieval.hierarchical_searcher import HierarchicalSearcher
from retrieval.result_ranker import ResultRanker
from retrieval.context_reader import ContextReader
from retrieval.path_anchor import build_path_anchor_query, prepend_code_location_header, guess_repo_rel_from_file_path
from extraction.code_chunker import chunk_source_code, detect_language
from server.api_keys import APIKeyManager
from server.audit import AuditService
from server.auth import AuthService, ResolvedIdentity
from server.control_plane_store import ControlPlaneStore
from server.tenant_admin import TenantAdminService

try:
    from pyagfs import AGFSClient
    from fs.agfs_adapter import AGFSContextFS
    from providers.relation_store.agfs_relation_store import AGFSRelationStore
    from service.api import MemoryWriteAPI, ReadAPI
    from commit.outbox_store import OutboxStore
    _HAS_AGFS = True
except ImportError:
    _HAS_AGFS = False

logger = logging.getLogger("contextsniper.service")

# Fallback archive-trim safety margin used only when incoming messages cannot
# be aligned with the session buffer.  It preserves a likely in-flight
# user/assistant turn that has not reached after_turn yet.
_ARCHIVE_TRIM_UNMATCHED_TAIL_MARGIN = 2

_CODE_SELECTION_MAX_FILES = 200
_CODE_SELECTION_MAX_PATHS = int(os.environ.get("CONTEXTSNIPER_COMPOSE_MAX_CODE_PATHS", "100"))
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
}
_IGNORED_CODE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", "env", "node_modules", "dist", "build",
    "coverage", ".next", ".turbo",
}
DEFAULT_BOOTSTRAP_INGEST_MAX_FILES = 200
DEFAULT_CODE_SYNC_MAX_FILES = 500
_CODE_QUERY_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "when", "what",
    "where", "which", "how", "does", "dont", "doesnt", "handle", "nicely", "issue",
    "expected", "actual", "behavior", "possible", "file", "hints", "before", "after",
    "function", "method", "class", "module", "call", "calls", "using", "used",
}
_DEFAULT_CODE_SEARCH_CANDIDATE_MAX_FILES = 80
_DEFAULT_CODE_SEARCH_EMBED_MAX_FILES = 80
_DEFAULT_CODE_SEARCH_MAX_SNIPPETS = 500

_CODE_RETRIEVAL_ENV = {
    "semantic": "CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED",
    "embedding": "CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED",
    "frequency": "CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED",
    "bm25": "CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED",
    "symbolic": "CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED",
    "ctags": "CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED",
    "graph": "CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED",
}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _code_retrieval_enabled(route: str) -> bool:
    env_name = _CODE_RETRIEVAL_ENV.get(route.lower())
    if not env_name:
        return True
    return _env_bool(env_name, True)


def _bootstrap_max_files() -> int:
    raw = os.environ.get("CONTEXTSNIPER_BOOTSTRAP_MAX_FILES", str(DEFAULT_BOOTSTRAP_INGEST_MAX_FILES))
    try:
        n = int(raw)
    except ValueError:
        n = DEFAULT_BOOTSTRAP_INGEST_MAX_FILES
    return max(1, min(n, 2000))


def _bootstrap_full_index_cap_files() -> int | None:
    raw = os.environ.get("CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES", "0")
    try:
        n = int(raw)
    except ValueError:
        return None
    if n <= 0:
        return None
    return max(1, min(n, 2000))


def _code_sync_enabled() -> bool:
    return _env_bool("CONTEXTSNIPER_CODE_SYNC_ON_PROMPT", True)


def _code_sync_max_files() -> int:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SYNC_MAX_FILES", str(DEFAULT_CODE_SYNC_MAX_FILES))
    try:
        n = int(raw)
    except ValueError:
        n = DEFAULT_CODE_SYNC_MAX_FILES
    return max(1, min(n, 5000))


def _code_sync_wait_for_index_default() -> bool:
    return _env_bool("CONTEXTSNIPER_CODE_SYNC_WAIT_FOR_INDEX", True)


def _extract_code_query_terms(query: str) -> list[str]:
    raw_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query or "")
    seen: set[str] = set()
    terms: list[str] = []
    for term in raw_terms:
        lowered = term.lower()
        if lowered in _CODE_QUERY_STOPWORDS:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(term)
    return terms


def _code_search_candidate_max_files() -> int:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES", str(_DEFAULT_CODE_SEARCH_CANDIDATE_MAX_FILES))
    try:
        n = int(raw)
    except ValueError:
        n = _DEFAULT_CODE_SEARCH_CANDIDATE_MAX_FILES
    return max(1, min(n, 500))


def _code_search_embed_max_files() -> int:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES", str(_DEFAULT_CODE_SEARCH_EMBED_MAX_FILES))
    try:
        n = int(raw)
    except ValueError:
        n = _DEFAULT_CODE_SEARCH_EMBED_MAX_FILES
    return max(1, min(n, 100))


def _code_search_max_snippets() -> int:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS", str(_DEFAULT_CODE_SEARCH_MAX_SNIPPETS))
    try:
        n = int(raw)
    except ValueError:
        n = _DEFAULT_CODE_SEARCH_MAX_SNIPPETS
    return max(1, min(n, 5000))


def _code_search_ingest_candidates_enabled() -> bool:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _code_search_ingest_async_enabled() -> bool:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _code_search_ingest_max_files() -> int:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES", "3")
    try:
        n = int(raw)
    except ValueError:
        n = 3
    return max(0, min(n, 50))


def _code_search_ingest_max_chunks() -> int:
    raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS", "40")
    try:
        n = int(raw)
    except ValueError:
        n = 40
    return max(0, min(n, 500))


def _forced_code_search_limit() -> int | None:
    raw = (os.environ.get("CONTEXTSNIPER_SEARCH_FORCE_LIMIT") or os.environ.get("CONTEXTSNIPER_SEARCH_LIMIT") or "").strip()
    if not raw:
        return None
    try:
        return max(1, min(100, int(raw)))
    except ValueError:
        return None


def _estimate_text_tokens(text: str) -> int:
    """Cheap token estimate for embedding batch sizing."""
    if not text:
        return 0
    total_chars = len(text)
    cjk_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    cjk_tokens = int(cjk_chars / 1.5)
    en_tokens = max(1, (total_chars - cjk_chars) // 4)
    return cjk_tokens + en_tokens


def _tokenize_for_bm25(text: str) -> list[str]:
    terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", text or "")
    out: list[str] = []
    for term in terms:
        lowered = term.lower()
        if lowered in _CODE_QUERY_STOPWORDS:
            continue
        out.append(lowered)
    return out


def _split_code_identifier(text: str) -> list[str]:
    parts: list[str] = []
    for raw in re.split(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|[_\W]+", text or ""):
        if not raw:
            continue
        parts.extend(_tokenize_for_bm25(raw))
    return parts


# ---------------------------------------------------------------------------
# Helpers (stateless, reusable)
# ---------------------------------------------------------------------------

def extract_content_text(content) -> str:
    """Extract text from message content (string or structured blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts).strip()
    return str(content)


def _message_diag(messages) -> list[dict]:
    """Return compact message fingerprints for extraction diagnostics."""
    import hashlib

    diag = []
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = extract_content_text(msg.get("content", ""))
            msg_id = msg.get("id", "")
            created_at = msg.get("created_at", "")
        else:
            role = getattr(msg, "role", "")
            content = extract_content_text(getattr(msg, "content", ""))
            msg_id = getattr(msg, "id", "")
            created_at = getattr(msg, "created_at", "")

        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:12]
        diag.append({
            "i": idx,
            "role": role,
            "chars": len(content),
            "sha": digest,
            "id": msg_id,
            "created_at": str(created_at) if created_at else "",
        })
    return diag


def extract_content_and_tool_calls(content) -> dict:
    """Extract text AND tool call metadata from message content.

    Returns:
        {"text": str, "tool_calls": list[dict]}
    """
    from extraction.tool_collector import parse_message_parts
    return parse_message_parts(content)


def extract_query(messages: list[dict]) -> str:
    """Build a search query from the last 3 user messages."""
    user_texts = []
    for msg in messages:
        if msg.get("role") == "user":
            text = extract_content_text(msg.get("content", ""))
            if text:
                user_texts.append(text)
    return "\n".join(user_texts[-3:]).strip()


def _message_role_content(msg) -> tuple[str, str]:
    if isinstance(msg, dict):
        role = str(msg.get("role", ""))
        content = extract_content_text(msg.get("content", ""))
    else:
        role = str(getattr(msg, "role", ""))
        content = extract_content_text(getattr(msg, "content", ""))
    return role, content


def _find_last_message_sequence_start(messages: list, sequence: list) -> int | None:
    """Find the last contiguous occurrence of sequence inside messages."""
    if not messages or not sequence or len(sequence) > len(messages):
        return None

    haystack = [_message_role_content(msg) for msg in messages]
    needle = [_message_role_content(msg) for msg in sequence]
    last_start = len(haystack) - len(needle)
    for start in range(last_start, -1, -1):
        if haystack[start:start + len(needle)] == needle:
            return start
    return None


def _bounded_message_count(value, max_count: int) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        count = 0
    return max(0, min(count, max_count))


def _update_extraction_summary(existing_summary: str, write_result: dict, max_chars: int = 2000) -> str:
    """Append new candidate abstracts to the extraction summary.

    Simple concatenation strategy: keep [{category}] {abstract} lines,
    truncate to max_chars (oldest entries dropped when exceeded).

    Args:
        existing_summary: Current summary text from previous extractions
        write_result: Dict from commit_session() with "plans" list
        max_chars: Maximum summary length in chars (~500 tokens)

    Returns:
        Updated summary string
    """
    plans = write_result.get("plans", [])
    new_lines = []
    for plan in plans:
        if plan.get("action") == "skip":
            continue
        uri = plan.get("target_uri", "")
        # Extract category and routing_key from URI for context
        parts = uri.rstrip("/").split("/")
        category = parts[-2] if len(parts) >= 2 else ""
        slug = parts[-1] if parts else ""
        new_lines.append(f"[{category}] {slug}")

    if not new_lines:
        return existing_summary

    combined = existing_summary + "\n" + "\n".join(new_lines) if existing_summary else "\n".join(new_lines)

    # Truncate to max_chars, keeping newest entries
    if len(combined) > max_chars:
        combined = combined[-max_chars:]

    return combined


def _positive_int(value: object, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return resolved if resolved > 0 else default


def _summary_max_chars(config: ContextSniperConfig, params: dict | None = None, default: int = 2000) -> int:
    """Resolve request-level summary trim overrides before falling back to config."""
    config_value = _positive_int(getattr(config, "summary_max_chars", default), default)
    request_value = None if params is None else params.get("summaryMaxChars")
    if request_value is not None:
        return _positive_int(request_value, config_value)
    return config_value


def _trim_summary(summary: str, max_chars: int) -> str:
    """Trim summary text explicitly, keeping the newest tail when oversized."""
    if not summary:
        return ""
    if len(summary) <= max_chars:
        return summary
    return summary[-max_chars:]


def _short_term_index_mode(params: dict | None = None) -> str:
    """Normalize compact short-term index mode for Phase 1 backend handling."""
    raw_value = None if params is None else params.get("shortTermIndexMode")
    if raw_value is None:
        return "sync"
    normalized = str(raw_value).strip().lower()
    if normalized in {"async", "off"}:
        return normalized
    return "sync"


def _parse_session_time(created_at: object) -> object | None:
    if not created_at:
        return None
    try:
        if isinstance(created_at, str):
            return _dt.fromisoformat(created_at.replace("Z", "+00:00"))
        if isinstance(created_at, (int, float)):
            return _dt.fromtimestamp(created_at)
        return created_at
    except Exception as exc:
        logger.warning("failed to parse session_time created_at=%s: %s", created_at, exc)
        return None




# ---------------------------------------------------------------------------
# Archive helpers (stateless, testable)
# ---------------------------------------------------------------------------

def build_archive_refs(
    entries: list[ArchiveEntry],
    budget: TokenBudget,
) -> tuple[list[ArchiveRef], list[ArchiveRef]]:
    """Build distance-tiered archive references within token budget.

    Sorting: newest first (by created_at descending).
    Tier 1: Latest archive gets full overview.
    Tier 2: Remaining archives get abstract only, budget-truncated
            (oldest dropped first when budget exceeded).

    Args:
        entries: List of ArchiveEntry objects from SessionArchiveStore
        budget: Token budget for assembly

    Returns:
        Tuple of (latest_archives, pre_archives)
    """
    if not entries:
        return [], []

    # Sort by created_at descending (newest first)
    sorted_entries = sorted(
        entries,
        key=lambda e: e.created_at or "",
        reverse=True,
    )

    archive_budget = budget.archive_limit

    # Tier 1: Latest archive gets full overview
    latest = sorted_entries[0]
    overview_text = latest.overview or ""
    overview_tokens = len(overview_text) // 4

    latest_ref = ArchiveRef(
        archive_id=latest.archive_id,
        archive_uri=f"archive://{latest.session_id}/{latest.archive_id}",
        abstract=latest.abstract or "",
        overview=overview_text,
        tokens=overview_tokens,
    )
    latest_refs = [latest_ref]
    used_tokens = overview_tokens

    # Tier 2: Remaining archives get abstract only
    # Budget-truncated: iterate newest→oldest, stop when budget exceeded
    # (oldest entries are naturally dropped first)
    pre_refs: list[ArchiveRef] = []
    for entry in sorted_entries[1:]:
        abstract_text = entry.abstract or ""
        abstract_tokens = len(abstract_text) // 4

        if used_tokens + abstract_tokens > archive_budget:
            break

        pre_refs.append(ArchiveRef(
            archive_id=entry.archive_id,
            archive_uri=f"archive://{entry.session_id}/{entry.archive_id}",
            abstract=abstract_text,
            overview=None,
            tokens=abstract_tokens,
        ))
        used_tokens += abstract_tokens

    return latest_refs, pre_refs


# ---------------------------------------------------------------------------
# MemoryService — warm singleton holding initialized resources
# ---------------------------------------------------------------------------

class MemoryService:
    """Transport-agnostic ContextSniper service.

    Holds lazy-initialized LLM, ReadAPI, WriteAPI instances.
    Each handler method accepts a plain dict and returns a plain dict.
    """

    def __init__(self, config: ContextSniperConfig | None = None):
        cfg = config or get_config()
        self._cfg = cfg
        self._provider_cfg = ProviderConfig.from_contextsniper_config(cfg)

        self._agfs_base_url = cfg.agfs_base_url
        self._mount_prefix = cfg.agfs_mount_prefix
        self._default_account_id = cfg.account_id
        self._default_user_id = cfg.user_id
        self._default_agent_id = cfg.agent_id

        self._llm = None
        self._write_api = None
        self._read_api = None
        self._session_mgr: SessionManager | None = None

        # Shared vector index + embedder (used by both read API and outbox worker)
        self._vector_index = None
        self._embedder = None
        self._outbox_thread: threading.Thread | None = None
        self._control_store = None
        self._key_manager = None
        self._auth = None
        self._audit = None
        self._tenant_admin = None
        self._search_bootstrapped_workspaces: set[tuple[str, str]] = set()
        self._search_bootstrap_lock = threading.Lock()
        self._code_sync_lock = threading.Lock()

    # -- RequestContext factory ------------------------------------------------

    def build_context(
        self,
        params: dict,
        identity: ResolvedIdentity | None = None,
    ) -> RequestContext:
        """Build RequestContext, allowing per-request overrides.

        Empty strings from the plugin are treated as absent, so defaults apply.
        """
        session_id = params.get("sessionId") or "unknown"
        explicit_agent_id = params.get("agentId")
        user_id = params.get("userId") or params.get("user_id") or self._default_user_id
        account_id = params.get("accountId") or params.get("account_id") or self._default_account_id
        logger.debug("build_context userId=%s (from params: %s)", user_id, params.get("userId"))

        if identity is not None:
            requested_agent_id = explicit_agent_id
            ctx = self.get_auth_service().build_request_context(
                identity,
                account_id=account_id,
                user_id=user_id,
                agent_id=requested_agent_id or "",
                session_id=session_id,
            )
            visible = [ctx.user_space_name()]
            visible_agent_ids = self.get_tenant_admin_service().list_visible_agent_ids(ctx.account_id, ctx.user_id, self._cfg)
            visible.extend(f"agent:{agent_id}" for agent_id in visible_agent_ids)
            if requested_agent_id and requested_agent_id not in set(visible_agent_ids):
                raise PermissionError(f"agent access denied: {requested_agent_id}")
            return RequestContext(
                account_id=ctx.account_id,
                user_id=ctx.user_id,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
                trace_id=ctx.trace_id,
                role=ctx.role,
                visible_owner_spaces=tuple(dict.fromkeys(v for v in visible if v)),
            )

        requested_agent_id = explicit_agent_id or self._default_agent_id
        return RequestContext(
            account_id=account_id,
            user_id=user_id,
            agent_id=requested_agent_id,
            session_id=session_id,
            trace_id=str(uuid4()),
            role=Role.ROOT,
            visible_owner_spaces=(),
        )

    def get_control_plane_store(self) -> ControlPlaneStore:
        if self._control_store is None:
            if _HAS_AGFS:
                client = AGFSClient(api_base_url=self._agfs_base_url)
                self._control_store = ControlPlaneStore(
                    mount_prefix=self._mount_prefix,
                    client=client,
                )
            else:
                self._control_store = ControlPlaneStore(
                    mount_prefix=self._mount_prefix,
                    local_root=os.path.join(os.getcwd(), ".contextsniper_control"),
                )
        return self._control_store

    def get_key_manager(self) -> APIKeyManager:
        if self._key_manager is None:
            self._key_manager = APIKeyManager(self.get_control_plane_store())
        return self._key_manager

    def get_auth_service(self) -> AuthService:
        if self._auth is None:
            self._auth = AuthService(self._cfg, self.get_key_manager())
        return self._auth

    def get_audit_service(self) -> AuditService:
        if self._audit is None:
            self._audit = AuditService(self.get_control_plane_store())
        return self._audit

    def get_tenant_admin_service(self) -> TenantAdminService:
        if self._tenant_admin is None:
            self._tenant_admin = TenantAdminService(
                self.get_key_manager(),
                self.get_control_plane_store(),
                self.get_audit_service(),
            )
        return self._tenant_admin

    def _read_profile(self, ctx: RequestContext) -> str:
        """Read user profile content directly from AGFS.

        Profile may be stored as a single node or as field-level child nodes
        (e.g. profile/name, profile/location, profile/identity). This method
        handles both: if the profile node has content, use it; otherwise
        enumerate children and concatenate their content.
        """
        if not _HAS_AGFS:
            return ""
        try:
            client = AGFSClient(api_base_url=self._agfs_base_url)
            agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
            profile_uri = f"ctx://{ctx.account_id}/users/{ctx.user_id}/memories/profile"

            # Try reading as a single node first
            if agfs.exists(profile_uri, ctx):
                node = agfs.read_node(profile_uri, ctx)
                if node.content:
                    return node.content

            # Field-level split: enumerate children and read each
            try:
                children = agfs.list_children(profile_uri, ctx)
            except Exception as exc:
                logger.error("list_children failed in _read_profile: %s", exc)
                raise

            if not children:
                return ""

            parts = []
            for child_uri in children:
                if agfs.exists(child_uri, ctx):
                    child_node = agfs.read_node(child_uri, ctx)
                    if child_node.content:
                        # Extract field name from URI for labeling
                        field_name = child_uri.rstrip("/").split("/")[-1]
                        parts.append(f"- {field_name}: {child_node.content.strip()}")

            return "\n".join(parts) if parts else ""
        except Exception as exc:
            logger.error("_read_profile failed for %s: %s", ctx.user_id, exc, exc_info=True)
            raise

    def _collect_archives(self, ctx: RequestContext, token_budget: TokenBudget) -> tuple[list[ArchiveRef], list[ArchiveRef]]:
        """Collect session archives with distance-based graduated compression.

        Tier 1 (latest): full overview.  Tier 2 (older): abstract only.
        """
        if not _HAS_AGFS:
            return [], []

        try:
            from session import SessionArchiveStore

            client = AGFSClient(api_base_url=self._agfs_base_url)
            agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
            store = SessionArchiveStore(fs=agfs)

            entries = store.list_archives(session_id=ctx.session_id, ctx=ctx)
            return build_archive_refs(entries, token_budget)
        except Exception as exc:
            logger.warning("_collect_archives failed: %s", exc, exc_info=True)
            return [], []


    def _search_working_set(self, query: str, ctx: RequestContext) -> list[dict]:
        """Vector search for relevant memories. Recall 15, dynamically truncate by score gaps."""
        try:
            result = self.get_read_api().search_memory(
                query=query, ctx=ctx, top_k=15, fill_content_for_top_k=5, mode="QUICK"
            )
            if not result or not result.hits:
                return []
            items = [
                {
                    "uri": h.uri,
                    "abstract": h.abstract or "",
                    "content": h.content_excerpt or "",
                    "score": h.score,
                    "category": h.category,
                }
                for h in result.hits
            ]
            return self._truncate_by_score_gap(items)
        except Exception as exc:
            logger.warning("Working set search failed: %s", exc)
        return []

    @staticmethod
    def _truncate_by_score_gap(items: list[dict], min_keep: int = 3, gap_ratio: float = 0.15) -> list[dict]:
        """Truncate items at the largest score gap.

        Finds the biggest relative score drop between consecutive items.
        Keeps at least min_keep items. If no significant gap, returns all.
        """
        if len(items) <= min_keep:
            return items

        scores = [it["score"] for it in items]
        max_gap_idx = min_keep - 1
        max_gap = 0.0

        for i in range(min_keep - 1, len(scores) - 1):
            if scores[i] > 0:
                gap = (scores[i] - scores[i + 1]) / scores[i]
                if gap > max_gap:
                    max_gap = gap
                    max_gap_idx = i

        if max_gap >= gap_ratio:
            return items[: max_gap_idx + 1]

        return items

    def _resolve_workspace_root(self, params: dict) -> Path | None:
        """Resolve workspace root supplied by the client/plugin."""
        raw = (
            params.get("workspaceRoot")
            or params.get("workspace_root")
            or params.get("projectRoot")
            or params.get("project_root")
            or ""
        )
        if not raw:
            return None
        try:
            root = Path(raw).expanduser().resolve()
        except Exception:
            return None
        if not root.exists() or not root.is_dir():
            return None
        return root

    def _list_workspace_code_files(
        self,
        workspace_root: Path,
        *,
        max_files: int | None = _CODE_SELECTION_MAX_FILES,
    ) -> list[str]:
        """List repo-relative code files under the workspace root."""
        found: list[str] = []
        try:
            for current_root, dirnames, filenames in os.walk(workspace_root):
                dirnames[:] = sorted(
                    name for name in dirnames
                    if name not in _IGNORED_CODE_DIRS and not name.startswith(".cache")
                )
                for filename in sorted(filenames):
                    if max_files is not None and len(found) >= max_files:
                        return found
                    suffix = Path(filename).suffix.lower()
                    if suffix not in _CODE_EXTENSIONS:
                        continue
                    full_path = Path(current_root) / filename
                    relative_path = full_path.relative_to(workspace_root).as_posix()
                    found.append(relative_path)
        except Exception as exc:
            logger.warning("workspace code listing failed for %s: %s", workspace_root, exc)
        return found

    @staticmethod
    def _is_code_sync_path(relative_path: str) -> bool:
        try:
            rel = Path(str(relative_path or "").replace("\\", "/"))
        except Exception:
            return False
        if rel.is_absolute() or ".." in rel.parts:
            return False
        if any(part in _IGNORED_CODE_DIRS or part.startswith(".cache") for part in rel.parts):
            return False
        return rel.suffix.lower() in _CODE_EXTENSIONS

    def _git_workspace_code_files(
        self,
        workspace_root: Path,
        *,
        max_files: int,
    ) -> tuple[list[str], dict]:
        """List git-visible code files, including untracked non-ignored files."""
        cmd = [
            "git",
            "-C",
            str(workspace_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            return [], {"ok": False, "error": "git_not_found"}
        except subprocess.TimeoutExpired:
            return [], {"ok": False, "error": "git_ls_files_timeout"}

        if proc.returncode != 0:
            return [], {
                "ok": False,
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:400],
            }

        out: list[str] = []
        seen: set[str] = set()
        for raw in proc.stdout.splitlines():
            rel = raw.strip().replace("\\", "/")
            if not rel or rel in seen:
                continue
            if not self._is_code_sync_path(rel):
                continue
            full = (workspace_root / rel).resolve()
            try:
                full.relative_to(workspace_root)
            except ValueError:
                continue
            if not full.is_file():
                continue
            seen.add(rel)
            out.append(rel)
            if len(out) >= max_files:
                break
        return out, {
            "ok": True,
            "source": "git_ls_files",
            "max_files": max_files,
            "truncated": len(out) >= max_files,
        }

    def _current_code_sync_files(self, workspace_root: Path, *, max_files: int) -> tuple[list[str], dict]:
        git_files, git_meta = self._git_workspace_code_files(workspace_root, max_files=max_files)
        if git_meta.get("ok"):
            return git_files, git_meta

        fallback = self._list_workspace_code_files(workspace_root, max_files=max_files)
        return fallback, {
            "ok": True,
            "source": "workspace_walk",
            "git": git_meta,
            "max_files": max_files,
            "truncated": len(fallback) >= max_files,
        }

    def _code_sync_state_dir(self, workspace_root: Path, ctx: RequestContext) -> Path:
        runtime_dir = Path(os.environ.get("CONTEXTSNIPER_RUNTIME_DIR") or ".").expanduser().resolve()
        raw = "\0".join([
            ctx.account_id,
            ctx.user_id,
            ctx.agent_id,
            str(workspace_root.resolve()),
        ])
        key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return runtime_dir / "code-sync-state" / key

    def _write_code_sync_snapshot(
        self,
        *,
        workspace_root: Path,
        snapshot_dir: Path,
        relative_paths: list[str],
    ) -> int:
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        copied = 0
        root = workspace_root.resolve()
        for rel in sorted(dict.fromkeys(relative_paths)):
            if not self._is_code_sync_path(rel):
                continue
            source = (root / rel).resolve()
            try:
                source.relative_to(root)
            except ValueError:
                continue
            if not source.is_file():
                continue
            target = snapshot_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(source, target)
                copied += 1
            except OSError as exc:
                logger.warning("code sync snapshot copy failed for %s: %s", rel, exc)
        return copied

    @staticmethod
    def _relative_from_sync_diff_path(raw_path: str, old_dir: Path, new_dir: Path) -> str:
        candidate = Path(raw_path).expanduser().resolve(strict=False)
        for root in (new_dir.resolve(strict=False), old_dir.resolve(strict=False)):
            try:
                return candidate.relative_to(root).as_posix()
            except ValueError:
                continue
        return ""

    def _git_diff_code_sync_snapshots(
        self,
        *,
        old_dir: Path,
        new_dir: Path,
    ) -> dict:
        cmd = [
            "git",
            "diff",
            "--no-index",
            "--name-status",
            "--no-renames",
            str(old_dir),
            str(new_dir),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            return {"ok": False, "error": "git_not_found", "changed": [], "deleted": []}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "git_diff_timeout", "changed": [], "deleted": []}

        # git diff --no-index returns 1 when differences are found.
        if proc.returncode not in (0, 1):
            return {
                "ok": False,
                "error": "git_diff_failed",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:800],
                "changed": [],
                "deleted": [],
            }

        changed: list[str] = []
        deleted: list[str] = []
        seen_changed: set[str] = set()
        seen_deleted: set[str] = set()
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0].strip()
            path_parts = parts[1:]
            if not status:
                continue
            if status.startswith("D"):
                rel = self._relative_from_sync_diff_path(path_parts[0], old_dir, new_dir)
                if rel and self._is_code_sync_path(rel) and rel not in seen_deleted:
                    seen_deleted.add(rel)
                    deleted.append(rel)
                continue

            raw_path = path_parts[-1] if len(path_parts) > 1 else path_parts[0]
            rel = self._relative_from_sync_diff_path(raw_path, old_dir, new_dir)
            if rel and self._is_code_sync_path(rel) and rel not in seen_changed:
                seen_changed.add(rel)
                changed.append(rel)

        return {
            "ok": True,
            "returncode": proc.returncode,
            "changed": changed,
            "deleted": deleted,
            "raw_line_count": len(proc.stdout.splitlines()),
        }

    @staticmethod
    def _load_code_sync_meta(meta_path: Path) -> dict:
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _write_code_sync_meta(meta_path: Path, payload: dict) -> None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _apply_code_sync_changes(
        self,
        *,
        workspace_root: Path,
        params: dict,
        ctx: RequestContext,
        changed_paths: list[str],
        deleted_paths: list[str],
        wait_for_index: bool,
    ) -> dict:
        if not changed_paths and not deleted_paths:
            return {
                "ok": True,
                "changed_count": 0,
                "deleted_count": 0,
                "ingested_count": 0,
                "deleted_memory_count": 0,
            }

        if self.get_write_api() is None:
            return {"ok": False, "error": "write_api_unavailable"}

        project_id = str(
            params.get("projectId")
            or params.get("project_id")
            or workspace_root.name
        )
        root = workspace_root.resolve()

        delete_totals = {"deleted": 0, "failed": 0, "uris": [], "errors": []}

        def delete_uris(uris: list[str]) -> None:
            out = self._delete_code_memory_uris(uris=uris, ctx=ctx)
            delete_totals["deleted"] += int(out.get("deleted", 0) or 0)
            delete_totals["failed"] += int(out.get("failed", 0) or 0)
            delete_totals["uris"].extend(out.get("uris", []) or [])
            delete_totals["errors"].extend(out.get("errors", []) or [])

        for rel in sorted(dict.fromkeys(deleted_paths)):
            file_path = (root / rel).resolve()
            existing = self._list_code_memory_records_for_file(file_path=file_path, ctx=ctx)
            delete_uris(list(existing.keys()))

        for rel in sorted(dict.fromkeys(changed_paths)):
            file_path = (root / rel).resolve()
            existing = self._list_code_memory_records_for_file(file_path=file_path, ctx=ctx)
            expected = self._expected_code_memory_records_for_file(
                workspace_root=root,
                relative_path=rel,
                ctx=ctx,
            )
            stale_or_dirty: list[str] = []
            for uri, old_meta in existing.items():
                new_meta = expected.get(uri)
                if new_meta is None:
                    stale_or_dirty.append(uri)
                    continue
                old_hash = str((old_meta or {}).get("chunk_hash") or "").strip()
                new_hash = str((new_meta or {}).get("chunk_hash") or "").strip()
                if old_hash and new_hash and old_hash != new_hash:
                    stale_or_dirty.append(uri)
            delete_uris(stale_or_dirty)

        ingested: list[str] = []
        failed: list[dict] = []
        for rel in sorted(dict.fromkeys(changed_paths)):
            full = (root / rel).resolve()
            try:
                full.relative_to(root)
            except ValueError:
                failed.append({"file_path": rel, "error": "outside_workspace"})
                continue
            if not full.is_file():
                continue
            res = self._ingest_workspace_code_path(
                workspace_root=root,
                relative_path=rel,
                ctx=ctx,
                project_id=project_id,
            )
            if res and res.get("ok"):
                ingested.append(rel)
            else:
                failed.append({
                    "file_path": rel,
                    "error": (res or {}).get("error", "ingest_failed") if isinstance(res, dict) else "ingest_failed",
                })

        if wait_for_index:
            try:
                timeout_raw = params.get("sync_timeout_sec", params.get("syncTimeoutSec"))
                timeout_sec = max(1.0, float(timeout_raw or os.environ.get("CONTEXTSNIPER_CODE_SYNC_TIMEOUT_SEC", "30")))
            except (TypeError, ValueError):
                timeout_sec = 30.0
            drain = self._drain_outbox_until_quiet(
                account_id=ctx.account_id,
                deadline=time.time() + timeout_sec,
            )
        else:
            self._async_drain(account_id=ctx.account_id)
            drain = {"background": True, "timed_out": False}

        return {
            "ok": True,
            "changed_count": len(changed_paths),
            "deleted_count": len(deleted_paths),
            "ingested_count": len(ingested),
            "ingested_paths": ingested,
            "failed_paths": failed[:20],
            "failed_count": len(failed),
            "deleted_memory_count": delete_totals["deleted"],
            "delete_failed_count": delete_totals["failed"],
            "delete_errors": delete_totals["errors"][:20],
            "drain": drain,
        }

    @staticmethod
    def _param_bool(raw: object, default: bool) -> bool:
        if raw is None:
            return default
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_hint_list(raw: object) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, list):
            items = raw
        else:
            items = re.split(r"[\n,]", str(raw))
        out: list[str] = []
        seen: set[str] = set()
        for item in items:
            token = str(item or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            out.append(token)
        return out

    @staticmethod
    def _slugify_code_identity(value: str) -> str:
        value = value.lower().strip()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        return value.strip("_") or "code_chunk"

    def _candidate_slug_markers(self, workspace_root: Path, relative_path: str) -> list[str]:
        full_path = str((workspace_root / relative_path).resolve())
        rel_norm = relative_path.replace("\\", "/")
        markers = {
            self._slugify_code_identity(full_path),
            self._slugify_code_identity(rel_norm),
        }
        return [m for m in markers if m]

    def _glob_candidate_paths(
        self,
        workspace_root: Path,
        *,
        patterns: list[str],
        already: set[str],
        limit: int,
    ) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            if len(found) >= limit:
                break
            pattern = pattern.strip()
            if not pattern:
                continue
            full_pattern = str(workspace_root / pattern)
            for raw_path in glob(full_pattern, recursive=True):
                if len(found) >= limit:
                    break
                try:
                    rel = Path(raw_path).resolve().relative_to(workspace_root).as_posix()
                except Exception:
                    continue
                if rel in already or rel in seen:
                    continue
                if Path(rel).suffix.lower() not in _CODE_EXTENSIONS:
                    continue
                if any(part in _IGNORED_CODE_DIRS for part in Path(rel).parts):
                    continue
                seen.add(rel)
                found.append(rel)
        return found

    def _grep_candidate_paths(
        self,
        workspace_root: Path,
        *,
        grep_terms: list[str],
        already: set[str],
        limit: int,
    ) -> list[str]:
        if not grep_terms or limit <= 0:
            return []
        seen: set[str] = set()
        found: list[str] = []
        per_term_limit = max(limit, min(limit * 3, 200))
        glob_args: list[str] = []
        for ext in sorted(_CODE_EXTENSIONS):
            glob_args.extend(["-g", f"*{ext}"])
        for term in grep_terms:
            term_count = 0
            cmd = [
                "rg",
                "-l",
                "-F",
                "-m",
                "1",
                "--hidden",
                *glob_args,
                term,
                str(workspace_root),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                break
            if proc.returncode not in (0, 1):
                continue
            for raw_path in proc.stdout.splitlines():
                if term_count >= per_term_limit:
                    break
                try:
                    rel = Path(raw_path).resolve().relative_to(workspace_root).as_posix()
                except Exception:
                    continue
                if rel in already or rel in seen:
                    continue
                if Path(rel).suffix.lower() not in _CODE_EXTENSIONS:
                    continue
                if any(part in _IGNORED_CODE_DIRS for part in Path(rel).parts):
                    continue
                seen.add(rel)
                found.append(rel)
                term_count += 1
        return found

    def _collect_code_search_candidates(
        self,
        workspace_root: Path,
        *,
        query: str,
        glob_patterns: list[str],
        grep_terms: list[str],
        limit: int,
    ) -> list[str]:
        """Collect candidate files via glob + grep hints before semantic ranking."""
        already: set[str] = set()
        candidates: list[str] = []

        from_glob = self._glob_candidate_paths(
            workspace_root,
            patterns=glob_patterns,
            already=already,
            limit=limit,
        )
        candidates.extend(from_glob)
        already.update(from_glob)

        effective_grep_terms = grep_terms or _extract_code_query_terms(query)
        from_grep = self._grep_candidate_paths(
            workspace_root,
            grep_terms=effective_grep_terms,
            already=already,
            limit=max(0, limit - len(candidates)),
        )
        candidates.extend(from_grep)
        already.update(from_grep)

        if not candidates:
            fallback = self._rg_query_candidate_paths(
                workspace_root,
                query=query,
                already=already,
                limit=limit,
            )
            candidates.extend(fallback)
        # Keep a larger pre-rank pool so later, more specific query terms are
        # not starved by a broad first term such as "check_array".
        pool_limit = max(limit, min(limit * 4, 300))
        return candidates[:pool_limit]

    def _rank_code_search_candidates(
        self,
        workspace_root: Path,
        *,
        candidate_paths: list[str],
        query: str,
        grep_terms: list[str],
        glob_patterns: list[str],
        limit: int,
    ) -> list[str]:
        if len(candidate_paths) <= limit:
            return candidate_paths

        terms = [t.lower() for t in (grep_terms or _extract_code_query_terms(query)) if t]
        scored: list[tuple[tuple[int, int, int, int, str], str]] = []
        for idx, rel in enumerate(candidate_paths):
            rel_l = rel.lower()
            path_score = 0
            if glob_patterns:
                for pattern in glob_patterns:
                    anchor = pattern.split("*", 1)[0].strip("/").lower()
                    if anchor and rel_l.startswith(anchor):
                        path_score += 8
            for term in terms:
                if term in rel_l:
                    path_score += 20

            content_score = 0
            full_path = workspace_root / rel
            try:
                text = full_path.read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                text = ""
            for term in terms[:8]:
                if not term:
                    continue
                hits = text.count(term)
                if hits:
                    content_score += min(hits, 5) * 5
            basename_bonus = 10 if any(term in Path(rel_l).name for term in terms) else 0
            score_key = (path_score + basename_bonus + content_score, path_score, content_score, -idx, rel)
            scored.append((score_key, rel))

        scored.sort(reverse=True)
        return [rel for _, rel in scored[:limit]]

    def _ingest_candidate_paths(
        self,
        workspace_root: Path,
        params: dict,
        ctx: RequestContext,
        *,
        candidate_paths: list[str],
    ) -> list[str]:
        if not candidate_paths or not self.get_write_api():
            return []
        max_ingest = _code_search_ingest_max_files()
        if max_ingest <= 0:
            return []
        max_chunks = _code_search_ingest_max_chunks()
        if max_chunks <= 0:
            return []

        capped_paths: list[str] = []
        chunk_count = 0
        for rel in candidate_paths:
            if len(capped_paths) >= max_ingest:
                break
            full_path = (workspace_root / rel).resolve()
            try:
                text = full_path.read_text(encoding="utf-8")
                chunks = chunk_source_code(text, str(full_path), language=detect_language(str(full_path)))
            except Exception:
                continue
            next_count = len(chunks)
            if next_count <= 0:
                continue
            if chunk_count + next_count > max_chunks:
                continue
            capped_paths.append(rel)
            chunk_count += next_count
        candidate_paths = capped_paths
        if not candidate_paths:
            return []
        project_id = (
            params.get("projectId")
            or params.get("project_id")
            or workspace_root.name
        )
        ingested: list[str] = []
        workers_raw = os.environ.get(
            "CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS",
            os.environ.get("CONTEXTSNIPER_BOOTSTRAP_INGEST_WORKERS", "2"),
        )
        try:
            workers = int(workers_raw)
        except ValueError:
            workers = 2
        workers = max(1, min(workers, 8))
        if workers == 1 or len(candidate_paths) == 1:
            for rel in candidate_paths:
                res = self._ingest_workspace_code_path(
                    workspace_root=workspace_root,
                    relative_path=rel,
                    ctx=ctx,
                    project_id=str(project_id),
                )
                if res and res.get("ok"):
                    ingested.append(rel)
            return ingested
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_to_rel = {
                pool.submit(
                    self._ingest_workspace_code_path,
                    workspace_root=workspace_root,
                    relative_path=rel,
                    ctx=ctx,
                    project_id=str(project_id),
                ): rel
                for rel in candidate_paths
            }
            for fut in as_completed(fut_to_rel):
                rel = fut_to_rel[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    logger.warning("candidate ingest failed for %s: %s", rel, exc)
                    continue
                if res and res.get("ok"):
                    ingested.append(rel)
        return ingested

    def _ingest_candidate_paths_background(
        self,
        workspace_root: Path,
        params: dict,
        ctx: RequestContext,
        *,
        candidate_paths: list[str],
    ) -> dict:
        """Dispatch candidate persistence without blocking the search response."""
        if not candidate_paths or not self.get_write_api():
            return {"background": False, "reason": "no_candidates_or_write_api"}

        def run() -> None:
            try:
                ingested = self._ingest_candidate_paths(
                    workspace_root,
                    params,
                    ctx,
                    candidate_paths=candidate_paths,
                )
                logger.info(
                    "code search background candidate ingest completed: files=%d workspace=%s",
                    len(ingested),
                    workspace_root,
                )
            except Exception as exc:
                logger.warning("code search background candidate ingest failed: %s", exc, exc_info=True)

        thread = threading.Thread(
            target=run,
            name="contextsniper-code-search-candidate-ingest",
            daemon=True,
        )
        thread.start()
        return {
            "background": True,
            "candidate_files_considered": len(candidate_paths),
            "max_files": _code_search_ingest_max_files(),
            "max_chunks": _code_search_ingest_max_chunks(),
        }

    def _filter_hits_to_candidate_paths(
        self,
        workspace_root: Path,
        *,
        candidate_paths: list[str],
        hits: list,
    ) -> list:
        if not candidate_paths:
            return hits
        markers: set[str] = set()
        for rel in candidate_paths:
            markers.update(self._candidate_slug_markers(workspace_root, rel))
        filtered = []
        for hit in hits:
            uri = str(getattr(hit, "uri", "") or "")
            if any(marker and marker in uri for marker in markers):
                filtered.append(hit)
        return filtered

    def _rg_query_candidate_paths(
        self,
        workspace_root: Path,
        *,
        query: str,
        already: set[str],
        limit: int,
    ) -> list[str]:
        """Use ripgrep to find likely candidate files for a code query."""
        terms = _extract_code_query_terms(query)
        if not terms or limit <= 0:
            return []

        seen: set[str] = set()
        ranked: list[str] = []
        glob_args: list[str] = []
        for ext in sorted(_CODE_EXTENSIONS):
            glob_args.extend(["-g", f"*{ext}"])
        for term in terms[:6]:
            cmd = [
                "rg",
                "-l",
                "-F",
                "-m",
                "1",
                "--hidden",
                *glob_args,
                term,
                str(workspace_root),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                break

            if proc.returncode not in (0, 1):
                continue
            for raw_path in proc.stdout.splitlines():
                try:
                    rel = Path(raw_path).resolve().relative_to(workspace_root).as_posix()
                except Exception:
                    continue
                if rel in already or rel in seen:
                    continue
                parts = Path(rel).parts
                if any(part in _IGNORED_CODE_DIRS for part in parts):
                    continue
                if Path(rel).suffix.lower() not in _CODE_EXTENSIONS:
                    continue
                seen.add(rel)
                ranked.append(rel)
                if len(ranked) >= limit:
                    return ranked
        return ranked

    def _rg_semantic_fallback_hits(
        self,
        workspace_root: Path,
        *,
        query: str,
        limit: int,
    ) -> list[dict]:
        """Fast grep-style fallback for the first code search before vectors are ready."""
        terms = _extract_code_query_terms(query)
        if not terms or limit <= 0:
            return []

        glob_args: list[str] = []
        for ext in sorted(_CODE_EXTENSIONS):
            glob_args.extend(["-g", f"*{ext}"])

        hits: list[dict] = []
        seen_paths: set[str] = set()
        for term in terms[:4]:
            cmd = [
                "rg",
                "-n",
                "-F",
                "-m",
                str(max(1, limit)),
                "-C",
                "2",
                "--hidden",
                *glob_args,
                term,
                str(workspace_root),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                break
            if proc.returncode not in (0, 1):
                continue

            blocks = [block.strip() for block in proc.stdout.split("\n--\n") if block.strip()]
            for block in blocks:
                lines = [line for line in block.splitlines() if line.strip()]
                if not lines:
                    continue
                header = lines[0]
                path_part, _, _ = header.partition(":")
                try:
                    rel = Path(path_part).resolve().relative_to(workspace_root).as_posix()
                except Exception:
                    continue
                if rel in seen_paths:
                    continue
                seen_paths.add(rel)
                hit_rank = len(hits) + 1
                hits.append(
                    {
                        "uri": f"rg://{workspace_root}/{rel}",
                        "level_hit": "L2",
                        # Score fallback hits by final rank, not term index, so
                        # top-k results do not all saturate at 1.0.
                        "score": max(0.1, 1.0 - (hit_rank - 1) * 0.1),
                        "category": "code",
                        "abstract": f"{rel} matched {term}",
                        "overview": "",
                        "content_excerpt": "\n".join(lines)[:4000],
                    }
                )
                if len(hits) >= limit:
                    return hits
        return hits

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)

    def _embed_rank_candidate_hits(
        self,
        workspace_root: Path,
        *,
        candidate_paths: list[str],
        query: str,
        limit: int,
        snippets: list[dict] | None = None,
    ) -> list[dict]:
        if not candidate_paths or limit <= 0:
            return []
        if snippets is None:
            snippets = self._build_candidate_snippets(workspace_root, candidate_paths)
        if not snippets:
            return []
        try:
            embedder = self._get_shared_embedder()
        except Exception as exc:
            logger.warning("embedder init failed for code search: %s", exc)
            return []

        docs = [str(item.get("embedding_doc") or "") for item in snippets]
        max_batch_texts_raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_EMBED_BATCH_TEXTS", "512")
        try:
            max_batch_texts = int(max_batch_texts_raw)
        except ValueError:
            max_batch_texts = 512
        # Leave ample headroom below provider hard caps such as OpenAI's 2048 texts/request.
        max_batch_texts = max(1, min(max_batch_texts, 1024))
        max_batch_tokens_raw = os.environ.get("CONTEXTSNIPER_CODE_SEARCH_EMBED_BATCH_TOKENS", "100000")
        try:
            max_batch_tokens = int(max_batch_tokens_raw)
        except ValueError:
            max_batch_tokens = 100000
        # Keep a conservative margin below provider caps; local estimates can
        # undercount code-heavy text compared with provider tokenization.
        max_batch_tokens = max(20000, min(max_batch_tokens, 180000))

        try:
            query_vecs = embedder.embed_texts([query])
        except Exception as exc:
            logger.warning("direct embedding rank failed for query vector: %s", exc)
            return []
        if len(query_vecs) != 1:
            return []

        query_vec = query_vecs[0]
        scored: list[tuple[float, dict]] = []
        token_estimates = [_estimate_text_tokens(doc) for doc in docs]
        batches: list[tuple[int, list[str], list[dict]]] = []
        start = 0
        while start < len(docs):
            end = start
            batch_tokens = 0
            while end < len(docs):
                est = token_estimates[end]
                next_tokens = batch_tokens + est
                if end > start and (end - start) >= max_batch_texts:
                    break
                if end > start and next_tokens > max_batch_tokens:
                    break
                batch_tokens = next_tokens
                end += 1
                if batch_tokens >= max_batch_tokens:
                    break
            if end == start:
                end += 1
                batch_tokens = token_estimates[start]
            batches.append((start, docs[start:end], snippets[start:end]))
            start = end

        for start, doc_batch, meta_batch in batches:
            embedded = self._embed_doc_batch_resilient(
                embedder,
                doc_batch,
                meta_batch,
                token_estimates[start : start + len(doc_batch)],
                batch_start=start,
                total_docs=len(docs),
            )
            for meta, vec in embedded:
                scored.append((self._cosine_similarity(query_vec, vec), meta))
        scored.sort(key=lambda t: t[0], reverse=True)

        hits: list[dict] = []
        for score, meta in scored[:limit]:
            rel = str(meta["rel"])
            symbol = str(meta["symbol"] or "").strip()
            start_line = meta.get("start_line")
            end_line = meta.get("end_line")
            range_text = f"lines {start_line}-{end_line}" if start_line and end_line else "chunk"
            kind = str(meta.get("symbol_kind") or "code")
            abstract = f"{symbol} {kind} in {rel} ({range_text})".strip()
            overview = str(meta.get("graph_doc") or meta.get("signature") or "")
            hits.append(
                {
                    "uri": str(meta["snippet_uri"]),
                    "level_hit": "L2",
                    "score": float(score),
                    "category": "code",
                    "abstract": abstract,
                    "overview": overview,
                    "content_excerpt": str(meta["excerpt"])[:4000],
                    "symbol": symbol,
                    "symbol_kind": kind,
                    "start_line": start_line,
                    "end_line": end_line,
                    "retrieval_source": "embedding",
                }
            )
        return hits

    def _embed_doc_batch_resilient(
        self,
        embedder,
        docs: list[str],
        metas: list[dict],
        token_estimates: list[int],
        *,
        batch_start: int,
        total_docs: int,
    ) -> list[tuple[dict, list[float]]]:
        if not docs:
            return []
        try:
            vectors = embedder.embed_texts(docs)
        except Exception as exc:
            if len(docs) == 1:
                logger.warning(
                    "direct embedding rank skipped doc %d/%d (est_tokens=%d): %s",
                    batch_start,
                    total_docs,
                    token_estimates[0] if token_estimates else 0,
                    exc,
                )
                return []
            mid = len(docs) // 2
            logger.warning(
                "direct embedding rank splitting doc batch %d-%d/%d (texts=%d est_tokens=%d): %s",
                batch_start,
                batch_start + len(docs),
                total_docs,
                len(docs),
                sum(token_estimates),
                exc,
            )
            left = self._embed_doc_batch_resilient(
                embedder,
                docs[:mid],
                metas[:mid],
                token_estimates[:mid],
                batch_start=batch_start,
                total_docs=total_docs,
            )
            right = self._embed_doc_batch_resilient(
                embedder,
                docs[mid:],
                metas[mid:],
                token_estimates[mid:],
                batch_start=batch_start + mid,
                total_docs=total_docs,
            )
            return left + right

        if len(vectors) != len(docs):
            if len(docs) == 1:
                logger.warning(
                    "direct embedding rank skipped doc %d/%d after vector-count mismatch: got=%d expected=1",
                    batch_start,
                    total_docs,
                    len(vectors),
                )
                return []
            mid = len(docs) // 2
            logger.warning(
                "direct embedding rank splitting doc batch %d-%d/%d after vector-count mismatch: got=%d expected=%d",
                batch_start,
                batch_start + len(docs),
                total_docs,
                len(vectors),
                len(docs),
            )
            return (
                self._embed_doc_batch_resilient(
                    embedder,
                    docs[:mid],
                    metas[:mid],
                    token_estimates[:mid],
                    batch_start=batch_start,
                    total_docs=total_docs,
                )
                + self._embed_doc_batch_resilient(
                    embedder,
                    docs[mid:],
                    metas[mid:],
                    token_estimates[mid:],
                    batch_start=batch_start + mid,
                    total_docs=total_docs,
                )
            )
        return list(zip(metas, vectors))

    def _build_candidate_snippets(
        self,
        workspace_root: Path,
        candidate_paths: list[str],
        ctx: RequestContext | None = None,
    ) -> list[dict]:
        snippets: list[dict] = []
        for rel in candidate_paths:
            full_path = workspace_root / rel
            try:
                text = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if not text.strip():
                continue
            file_chunks = chunk_source_code(text, str(full_path))
            if not file_chunks:
                continue
            for chunk in file_chunks:
                snippet = str(chunk.content or "").strip()
                if not snippet:
                    continue
                code_metadata = {
                    "file_path": str(full_path),
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "symbol": chunk.symbol,
                    "language": chunk.language,
                    "signature": chunk.signature,
                }
                excerpt = prepend_code_location_header(snippet, code_metadata)
                rel_display = guess_repo_rel_from_file_path(str(full_path)) or rel
                symbol_kind = chunk.symbol_kind or "code"
                graph_metadata = {}
                try:
                    graph_metadata = dict((getattr(chunk, "metadata", None) or {}).get("graph") or {})
                except Exception:
                    graph_metadata = {}
                agfs_uri = self._code_memory_uri_for_chunk(chunk, ctx) if ctx is not None else ""
                agfs_directory = self._agfs_directory_for_uri(agfs_uri)
                graph_doc = self._format_graph_l1_doc(
                    symbol=str(chunk.symbol or ""),
                    file_path=str(full_path),
                    rel_path=rel_display,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    signature=str(chunk.signature or ""),
                    graph_metadata=graph_metadata,
                    agfs_uri=agfs_uri,
                    agfs_directory=agfs_directory,
                )
                snippet_uri = (
                    f"file://{workspace_root}/{rel}"
                    f"#L{chunk.start_line}-L{chunk.end_line}:{chunk.symbol}"
                )
                doc_parts = [
                    f"path: {rel_display}",
                    f"symbol: {chunk.symbol}",
                    f"kind: {symbol_kind}",
                ]
                if chunk.signature:
                    doc_parts.append(f"signature: {chunk.signature}")
                if graph_doc:
                    doc_parts.extend(["", graph_doc])
                doc_parts.extend(["", excerpt])
                snippets.append(
                    {
                        "snippet_uri": snippet_uri,
                        "agfs_uri": agfs_uri,
                        "file_path": str(full_path),
                        "rel": rel,
                        "symbol": chunk.symbol,
                        "symbol_kind": symbol_kind,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "signature": chunk.signature,
                        "excerpt": excerpt,
                        "graph": graph_metadata,
                        "graph_doc": graph_doc,
                        "embedding_doc": "\n".join(doc_parts),
                        "bm25_doc": " ".join(
                            [
                                f"path {rel_display}",
                                f"symbol {chunk.symbol}",
                                f"kind {symbol_kind}",
                                f"signature {chunk.signature or ''}",
                                graph_doc,
                                snippet,
                            ]
                        ),
                    }
                )
        self._resolve_snippet_graph_relations(snippets)
        for snippet in snippets:
            graph_metadata = snippet.get("graph") if isinstance(snippet.get("graph"), dict) else {}
            graph_doc = self._format_graph_l1_doc(
                symbol=str(snippet.get("symbol") or ""),
                file_path=str(snippet.get("file_path") or ""),
                rel_path=str(snippet.get("rel") or ""),
                start_line=snippet.get("start_line"),
                end_line=snippet.get("end_line"),
                signature=str(snippet.get("signature") or ""),
                graph_metadata=graph_metadata,
                agfs_uri=str(snippet.get("agfs_uri") or ""),
                agfs_directory=self._agfs_directory_for_uri(str(snippet.get("agfs_uri") or "")),
            )
            snippet["graph_doc"] = graph_doc
            snippet["embedding_doc"] = "\n".join(
                part for part in [
                    f"path: {snippet.get('rel') or ''}",
                    f"symbol: {snippet.get('symbol') or ''}",
                    f"kind: {snippet.get('symbol_kind') or 'code'}",
                    f"signature: {snippet.get('signature') or ''}" if snippet.get("signature") else "",
                    "",
                    graph_doc,
                    "",
                    str(snippet.get("excerpt") or ""),
                ] if part != ""
            )
            snippet["bm25_doc"] = " ".join(
                [
                    f"path {snippet.get('rel') or ''}",
                    f"symbol {snippet.get('symbol') or ''}",
                    f"kind {snippet.get('symbol_kind') or 'code'}",
                    f"signature {snippet.get('signature') or ''}",
                    graph_doc,
                    str(snippet.get("excerpt") or ""),
                ]
            )
        return snippets

    def _cap_code_search_snippets(self, snippets: list[dict], *, query: str, max_snippets: int) -> list[dict]:
        if max_snippets <= 0 or len(snippets) <= max_snippets:
            return snippets
        ctags_hits = self._ctags_rank_candidate_hits(snippets, query=query, limit=len(snippets))
        bm25_hits = self._bm25_rank_candidate_hits(snippets, query=query, limit=len(snippets))
        ctags_n = self._normalize_scores(ctags_hits)
        bm25_n = self._normalize_scores(bm25_hits)
        scored: list[tuple[float, int, dict]] = []
        for idx, snippet in enumerate(snippets):
            uri = str(snippet.get("snippet_uri") or "")
            score = (0.65 * ctags_n.get(uri, 0.0)) + (0.35 * bm25_n.get(uri, 0.0))
            scored.append((score, -int(snippet.get("start_line") or 0), snippet))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [snippet for _, _, snippet in scored[:max_snippets]]

    @staticmethod
    def _code_embedding_query(
        query: str,
        *,
        grep_terms: list[str] | None = None,
        glob_patterns: list[str] | None = None,
    ) -> str:
        parts = [query.strip()]
        scope_terms = []
        for value in [*(grep_terms or []), *(glob_patterns or [])]:
            for token in _split_code_identifier(str(value)):
                if token and token not in scope_terms:
                    scope_terms.append(token)
        if scope_terms:
            parts.append("scope: " + " ".join(scope_terms[:24]))
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _resolve_snippet_graph_relations(snippets: list[dict]) -> None:
        """Resolve graph relation names to concrete snippet URIs and locators."""
        symbol_index: dict[str, list[dict]] = {}

        def leaf(value: object) -> str:
            text = str(value or "").strip().lower()
            if not text:
                return ""
            return text.rsplit(".", 1)[-1]

        def add_symbol(symbol: object, snippet: dict) -> None:
            key = leaf(symbol)
            if key:
                symbol_index.setdefault(key, []).append(snippet)

        for snippet in snippets:
            add_symbol(snippet.get("symbol"), snippet)
            graph = snippet.get("graph") if isinstance(snippet.get("graph"), dict) else {}
            add_symbol(graph.get("symbol"), snippet)

        for snippet in snippets:
            graph = dict(snippet.get("graph") or {})
            relations: list[dict] = []
            seen: set[tuple[str, str, str]] = set()
            for relation_type in ("calls", "extends", "contains"):
                values = graph.get(relation_type)
                if not isinstance(values, list):
                    continue
                for raw_name in values:
                    name = str(raw_name or "").strip()
                    target_key = leaf(name)
                    if not target_key:
                        continue
                    for target in symbol_index.get(target_key, []):
                        if target is snippet:
                            continue
                        target_uri = str(target.get("snippet_uri") or "")
                        dedupe_key = (relation_type, name, target_uri)
                        if not target_uri or dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        relations.append(
                            {
                                "type": relation_type,
                                "name": name,
                                "target_uri": target_uri,
                                "target_agfs_uri": target.get("agfs_uri") or "",
                                "target_symbol": target.get("symbol") or "",
                                "target_path": target.get("rel") or "",
                                "target_start_line": target.get("start_line"),
                                "target_end_line": target.get("end_line"),
                                "target_signature": target.get("signature") or "",
                                "confidence": "resolved_symbol",
                            }
                        )
            graph["relations"] = relations
            snippet["graph"] = graph

    @staticmethod
    def _format_graph_l1_doc(
        *,
        symbol: str,
        file_path: str = "",
        rel_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        signature: str,
        graph_metadata: dict,
        agfs_uri: str = "",
        agfs_directory: str = "",
    ) -> str:
        title = symbol or "code"
        lines: list[str] = [f"# {title}", "", "## Location"]
        lines.append(f"- graph.symbol: {title}")
        if agfs_uri:
            lines.append(f"- agfs.uri: {agfs_uri}")
        if agfs_directory:
            lines.append(f"- agfs.directory: {agfs_directory}")
        if file_path:
            file_display = file_path.strip().replace("\\", "/")
            lines.append(f"- source.file: {file_display}")
        if rel_path:
            lines.append(f"- graph.path: {rel_path}")
        if start_line is not None and end_line is not None:
            lines.append(f"- source.lines: {start_line}-{end_line}")
        if signature:
            lines.extend(["", "## Signature", f"```text\n{signature}\n```"])

        def add_list(label: str, values: object, limit: int = 12) -> None:
            if not isinstance(values, list):
                return
            cleaned = [str(v).strip() for v in values if str(v).strip()]
            if cleaned:
                lines.append(f"- {label}: {', '.join(cleaned[:limit])}")

        graph_start = len(lines)
        lines.extend(["", "## Graph"])
        add_list("graph.calls", graph_metadata.get("calls"))
        add_list("graph.imports", graph_metadata.get("imports"))
        add_list("graph.extends", graph_metadata.get("extends"))
        add_list("graph.contains", graph_metadata.get("contains"))
        relations = graph_metadata.get("relations")
        if isinstance(relations, list):
            relation_lines: list[str] = []
            for rel in relations[:12]:
                if not isinstance(rel, dict):
                    continue
                relation_type = str(rel.get("type") or "").strip()
                name = str(rel.get("name") or rel.get("target_symbol") or "").strip()
                target_path = str(rel.get("target_path") or "").strip().replace("\\", "/")
                start = rel.get("target_start_line")
                end = rel.get("target_end_line")
                target_uri = str(rel.get("target_uri") or rel.get("target_agfs_uri") or "").strip()
                location = target_path
                if start is not None and end is not None:
                    location = f"{location}:{start}-{end}" if location else f"{start}-{end}"
                if relation_type and name:
                    suffix = f" -> {location}" if location else ""
                    if target_uri:
                        suffix += f" ({target_uri})"
                    relation_lines.append(f"{relation_type}:{name}{suffix}")
            if relation_lines:
                lines.append(f"- graph.resolved_relations: {'; '.join(relation_lines)}")
        if len(lines) == graph_start + 2:
            lines.append("- graph.relations: none")
        return "\n".join(lines)

    @staticmethod
    def _slugify_code_value(value: str) -> str:
        value = value.lower().strip()
        value = re.sub(r"[^a-z0-9]+", "_", value)
        return value.strip("_") or "code_chunk"

    @classmethod
    def _code_memory_slug_for_chunk(cls, chunk) -> str:
        language = str(getattr(chunk, "language", None) or "unknown")
        file_path = str(getattr(chunk, "file_path", None) or "")
        symbol = str(getattr(chunk, "symbol", None) or getattr(chunk, "routing_key", None) or "symbol")
        start_line = getattr(chunk, "start_line", None)
        end_line = getattr(chunk, "end_line", None)
        line_range_slug = f"{start_line}_{end_line}" if start_line is not None and end_line is not None else "0_0"
        file_name = Path(file_path).name if file_path else "file"
        file_stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        prefix = cls._slugify_code_value(f"{language}_{file_stem}_{symbol}_{line_range_slug}")
        prefix = prefix[:96].rstrip("_") or "code_chunk"
        line_range_identity = f"{start_line}-{end_line}" if start_line is not None and end_line is not None else "0-0"
        identity = f"{language}:{file_path}:{symbol}:{line_range_identity}"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

    @classmethod
    def _code_memory_uri_for_chunk(cls, chunk, ctx: RequestContext) -> str:
        owner_id = ctx.agent_id
        if not owner_id:
            return ""
        slug = cls._code_memory_slug_for_chunk(chunk)
        return f"ctx://{ctx.account_id}/agents/{owner_id}/memories/code/{slug}"

    @staticmethod
    def _agfs_directory_for_uri(uri: str) -> str:
        if not uri.startswith("ctx://"):
            return ""
        rest = uri[len("ctx://"):]
        parts = rest.split("/")
        if not parts:
            return ""
        return "/accounts/" + "/".join(parts)

    def _bm25_rank_candidate_hits(
        self,
        snippets: list[dict],
        *,
        query: str,
        limit: int,
    ) -> list[dict]:
        if not snippets or limit <= 0:
            return []
        query_terms = _tokenize_for_bm25(query)
        if not query_terms:
            return []

        fields = [
            ("symbol", 4.5, 0.05, lambda s: str(s.get("symbol") or "")),
            ("signature", 2.6, 0.20, lambda s: str(s.get("signature") or "")),
            ("path", 1.2, 0.20, lambda s: str(s.get("rel") or "")),
            ("body", 0.45, 0.65, lambda s: str(s.get("excerpt") or "")),
        ]
        field_tokens: dict[str, list[list[str]]] = {}
        for name, _, _, getter in fields:
            field_tokens[name] = [_tokenize_for_bm25(getter(snippet)) for snippet in snippets]
        if not any(any(docs) for docs in field_tokens.values()):
            return []

        n_docs = len(snippets)
        k1 = 1.5
        field_avgdl = {
            name: sum(len(toks) for toks in docs) / max(1, n_docs)
            for name, docs in field_tokens.items()
        }
        field_dfs: dict[str, Counter[str]] = {}
        for name, docs in field_tokens.items():
            dfs: Counter[str] = Counter()
            for toks in docs:
                for term in set(toks):
                    dfs[term] += 1
            field_dfs[name] = dfs

        scored: list[tuple[float, dict]] = []
        for idx, snippet in enumerate(snippets):
            score = 0.0
            for name, weight, b, _ in fields:
                toks = field_tokens[name][idx]
                if not toks:
                    continue
                tf = Counter(toks)
                dl = len(toks)
                avgdl = field_avgdl[name]
                dfs = field_dfs[name]
                for term in query_terms:
                    df = dfs.get(term, 0)
                    freq = tf.get(term, 0)
                    if df <= 0 or freq <= 0:
                        continue
                    idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                    denom = freq + k1 * (1.0 - b + b * (dl / max(1e-9, avgdl)))
                    score += weight * idf * ((freq * (k1 + 1.0)) / max(1e-9, denom))
            if score <= 0.0:
                continue
            scored.append((score, snippet))
        scored.sort(key=lambda item: item[0], reverse=True)

        hits: list[dict] = []
        for score, meta in scored[:limit]:
            rel = str(meta["rel"])
            symbol = str(meta["symbol"] or "").strip()
            start_line = meta.get("start_line")
            end_line = meta.get("end_line")
            range_text = f"lines {start_line}-{end_line}" if start_line and end_line else "chunk"
            kind = str(meta.get("symbol_kind") or "code")
            abstract = f"{symbol} {kind} in {rel} ({range_text})".strip()
            overview = str(meta.get("graph_doc") or meta.get("signature") or "")
            hits.append(
                {
                    "uri": str(meta["snippet_uri"]),
                    "level_hit": "L2",
                    "score": float(score),
                    "category": "code",
                    "abstract": abstract,
                    "overview": overview,
                    "content_excerpt": str(meta["excerpt"])[:4000],
                    "symbol": symbol,
                    "symbol_kind": kind,
                    "start_line": start_line,
                    "end_line": end_line,
                    "retrieval_source": "bm25",
                }
            )
        return hits

    def _ctags_rank_candidate_hits(
        self,
        snippets: list[dict],
        *,
        query: str,
        limit: int,
    ) -> list[dict]:
        if not snippets or limit <= 0:
            return []
        query_terms = [t.lower() for t in _extract_code_query_terms(query)]
        if not query_terms:
            return []
        kind_terms = {
            "function": {"function", "def", "fn", "method"},
            "async_function": {"async", "function", "def", "method"},
            "type": {"class", "type", "struct", "interface", "enum"},
            "code": {"code", "snippet"},
        }
        scored: list[tuple[float, dict]] = []
        for snippet in snippets:
            symbol = str(snippet.get("symbol") or "")
            symbol_l = symbol.lower()
            rel_l = str(snippet.get("rel") or "").lower()
            sig_l = str(snippet.get("signature") or "").lower()
            kind = str(snippet.get("symbol_kind") or "code")
            score = 0.0
            for term in query_terms:
                if term == symbol_l:
                    score += 3.0
                elif term in symbol_l:
                    score += 1.5
                if term in sig_l:
                    score += 1.0
                if term in rel_l:
                    score += 0.6
            query_vocab = set(query_terms)
            if query_vocab & kind_terms.get(kind, set()):
                score += 0.8
            if score <= 0.0:
                continue
            scored.append((score, snippet))
        scored.sort(key=lambda item: item[0], reverse=True)

        hits: list[dict] = []
        for score, meta in scored[:limit]:
            rel = str(meta["rel"])
            symbol = str(meta["symbol"] or "").strip()
            start_line = meta.get("start_line")
            end_line = meta.get("end_line")
            range_text = f"lines {start_line}-{end_line}" if start_line and end_line else "chunk"
            kind = str(meta.get("symbol_kind") or "code")
            abstract = f"{symbol} {kind} in {rel} ({range_text})".strip()
            overview = str(meta.get("graph_doc") or meta.get("signature") or "")
            hits.append(
                {
                    "uri": str(meta["snippet_uri"]),
                    "level_hit": "L2",
                    "score": float(score),
                    "category": "code",
                    "abstract": abstract,
                    "overview": overview,
                    "content_excerpt": str(meta["excerpt"])[:4000],
                    "symbol": symbol,
                    "symbol_kind": kind,
                    "start_line": start_line,
                    "end_line": end_line,
                    "retrieval_source": "ctags",
                }
            )
        return hits

    def _graph_rank_candidate_hits(
        self,
        snippets: list[dict],
        *,
        query: str,
        limit: int,
    ) -> list[dict]:
        """L1 graph-memory route: anchor rank + 1-hop relation walk + small PageRank, fused by RRF."""
        if not snippets or limit <= 0:
            return []
        query_terms = [t.lower() for t in _extract_code_query_terms(query)]
        if not query_terms:
            return []

        def norm(value: object) -> str:
            return str(value or "").strip().lower()

        def leaf(value: object) -> str:
            text = norm(value)
            if not text:
                return ""
            return text.rsplit(".", 1)[-1]

        uri_by_symbol: dict[str, list[str]] = {}
        uri_aliases: dict[str, str] = {}
        snippets_by_uri: dict[str, dict] = {}
        for snippet in snippets:
            uri = str(snippet.get("snippet_uri") or "")
            if not uri:
                continue
            snippets_by_uri[uri] = snippet
            for alias_key in ("agfs_uri", "uri"):
                alias = str(snippet.get(alias_key) or "").strip()
                if alias:
                    uri_aliases[alias] = uri
            symbol = leaf(snippet.get("symbol"))
            if symbol:
                uri_by_symbol.setdefault(symbol, []).append(uri)

        out_edges: dict[str, set[str]] = {uri: set() for uri in snippets_by_uri}
        in_edges: dict[str, set[str]] = {uri: set() for uri in snippets_by_uri}
        for uri, snippet in snippets_by_uri.items():
            graph = snippet.get("graph") if isinstance(snippet.get("graph"), dict) else {}
            relations = graph.get("relations")
            if isinstance(relations, list):
                for relation in relations:
                    if not isinstance(relation, dict):
                        continue
                    target_uri = str(relation.get("target_uri") or "").strip()
                    target_agfs_uri = str(relation.get("target_agfs_uri") or "").strip()
                    resolved_target = (
                        target_uri if target_uri in snippets_by_uri else uri_aliases.get(target_uri)
                    )
                    if not resolved_target and target_agfs_uri:
                        resolved_target = uri_aliases.get(target_agfs_uri)
                    if resolved_target and resolved_target != uri:
                        out_edges[uri].add(resolved_target)
                        in_edges[resolved_target].add(uri)
                        continue

                    # Fallback for stale/incomplete direct edges.
                    target_symbol = leaf(relation.get("target_symbol") or relation.get("name"))
                    if target_symbol:
                        for fallback_uri in uri_by_symbol.get(target_symbol, []):
                            if fallback_uri != uri:
                                out_edges[uri].add(fallback_uri)
                                in_edges[fallback_uri].add(uri)
            relation_names: list[str] = []
            for key in ("calls", "extends", "contains"):
                values = graph.get(key)
                if isinstance(values, list):
                    relation_names.extend(str(v) for v in values)
            for name in relation_names:
                target_symbol = leaf(name)
                if not target_symbol:
                    continue
                for target_uri in uri_by_symbol.get(target_symbol, []):
                    if target_uri == uri:
                        continue
                    out_edges[uri].add(target_uri)
                    in_edges[target_uri].add(uri)

        def match_count(text: str) -> int:
            text_l = text.lower()
            return sum(1 for term in query_terms if term and term in text_l)

        def direct_score(snippet: dict) -> tuple[float, float, bool]:
            symbol = norm(snippet.get("symbol"))
            rel = norm(snippet.get("rel"))
            signature = norm(snippet.get("signature"))
            graph = snippet.get("graph") if isinstance(snippet.get("graph"), dict) else {}
            relation_text = " ".join(
                " ".join(str(v) for v in graph.get(key, []) if str(v).strip())
                for key in ("calls", "imports", "extends", "contains")
                if isinstance(graph.get(key), list)
            ).lower()

            score = 0.0
            exact_symbol_anchor = any(term == symbol for term in query_terms)
            if exact_symbol_anchor:
                score += 6.0
            score += match_count(symbol) * 2.5
            score += match_count(signature) * 1.8
            score += match_count(rel) * 1.4
            anchor_score = score
            score += match_count(relation_text) * 1.6
            degree = len(out_edges.get(str(snippet.get("snippet_uri") or ""), set()))
            degree += len(in_edges.get(str(snippet.get("snippet_uri") or ""), set()))
            if degree:
                score += min(degree, 4) * 0.25
            return score, anchor_score, exact_symbol_anchor

        def relation_neighbors(uri: str, reverse: bool = True) -> list[tuple[str, str, bool]]:
            out = [(target_uri, "out", False) for target_uri in out_edges.get(uri, set())]
            if reverse:
                out.extend((source_uri, "in", True) for source_uri in in_edges.get(uri, set()))
            return out

        def anchor_text_scores() -> dict[str, float]:
            scores: dict[str, float] = {}
            for uri, snippet in snippets_by_uri.items():
                direct, anchor, _ = direct_score(snippet)
                relationish = direct - anchor
                text_l = " ".join(
                    [
                        str(snippet.get("rel") or ""),
                        str(snippet.get("symbol") or ""),
                        str(snippet.get("signature") or ""),
                        str(snippet.get("excerpt") or ""),
                    ]
                ).lower()
                overlap = sum(1 for term in query_terms if term and term in text_l)
                score = max(anchor, overlap * 0.75)
                if relationish > 0:
                    score = max(score, anchor + relationish * 0.25)
                if score > 0:
                    scores[uri] = score
            return scores

        def traverse(seeds: dict[str, float]) -> tuple[dict[str, float], dict[str, int]]:
            weights = {"calls": 0.95, "extends": 0.9, "contains": 0.75, "out": 0.95, "in": 0.72}
            scores = dict(seeds)
            distances = {uri: 0 for uri in seeds}
            frontier = dict(seeds)
            for depth in (1,):
                nxt: dict[str, float] = {}
                for uri, seed_score in frontier.items():
                    for neighbor_uri, rel_type, is_reverse in relation_neighbors(uri, reverse=True):
                        rel_weight = weights.get(rel_type, 0.8)
                        direction_weight = 0.72 if is_reverse else 1.0
                        gain = seed_score * (0.7 ** depth) * rel_weight * direction_weight
                        if gain <= 0:
                            continue
                        nxt[neighbor_uri] = max(nxt.get(neighbor_uri, 0.0), gain)
                        scores[neighbor_uri] = max(scores.get(neighbor_uri, 0.0), gain)
                        distances.setdefault(neighbor_uri, depth)
                frontier = nxt
                if not frontier:
                    break
            return scores, distances

        def pagerank(seeds: dict[str, float]) -> dict[str, float]:
            seeds_n = self._normalize_score_map(seeds)
            if not seeds_n:
                return {}
            uris = list(snippets_by_uri)
            scores = {uri: seeds_n.get(uri, 0.0) for uri in uris}
            alpha = 0.35
            for _ in range(14):
                nxt = {uri: alpha * seeds_n.get(uri, 0.0) for uri in uris}
                for uri, value in scores.items():
                    edges = list(out_edges.get(uri, set()))
                    if not edges:
                        continue
                    share = (1.0 - alpha) * value / max(1, len(edges))
                    for target_uri in edges:
                        nxt[target_uri] = nxt.get(target_uri, 0.0) + share
                scores = nxt
            return {uri: value for uri, value in scores.items() if value > 0}

        def rrf_score_maps(score_maps: list[dict[str, float]]) -> dict[str, float]:
            out: dict[str, float] = {}
            for scores in score_maps:
                ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
                for rank, (uri, _) in enumerate(ranked, start=1):
                    out[uri] = out.get(uri, 0.0) + 1.0 / (60.0 + rank)
            return out

        anchor = anchor_text_scores()
        if not anchor:
            return []
        walk, distances = traverse(anchor)
        ppr = pagerank(anchor)
        graph_scores = rrf_score_maps([anchor, walk, ppr])

        scored: list[tuple[float, dict]] = []
        for uri, score in graph_scores.items():
            snippet = snippets_by_uri.get(uri)
            if not snippet or score <= 0:
                continue
            dist = distances.get(uri, 0 if uri in anchor else 99)
            out_degree = len(out_edges.get(uri, set()))
            in_degree = len(in_edges.get(uri, set()))

            rel = str(snippet["rel"])
            symbol = str(snippet.get("symbol") or "").strip()
            start_line = snippet.get("start_line")
            end_line = snippet.get("end_line")
            range_text = f"lines {start_line}-{end_line}" if start_line and end_line else "chunk"
            kind = str(snippet.get("symbol_kind") or "code")
            graph_doc = str(snippet.get("graph_doc") or "")
            hit = {
                "uri": uri,
                "level_hit": "L1",
                "score": float(score),
                "category": "code",
                "abstract": f"{symbol} {kind} in {rel} ({range_text})".strip(),
                "overview": graph_doc,
                "content_excerpt": str(snippet["excerpt"])[:4000],
                "symbol": symbol,
                "symbol_kind": kind,
                "start_line": start_line,
                "end_line": end_line,
                "retrieval_source": "graph",
                "graph_distance": dist,
                "graph_relation_counts": {
                    "out": out_degree,
                    "in": in_degree,
                },
            }
            scored.append((score, hit))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in scored[:limit]]

    @staticmethod
    def _normalize_scores(hits: list[dict]) -> dict[str, float]:
        if not hits:
            return {}
        values = [float(h.get("score") or 0.0) for h in hits]
        min_v = min(values)
        max_v = max(values)
        out: dict[str, float] = {}
        if max_v <= min_v:
            for h in hits:
                out[str(h.get("uri") or "")] = 1.0
            return out
        span = max_v - min_v
        for h in hits:
            uri = str(h.get("uri") or "")
            out[uri] = (float(h.get("score") or 0.0) - min_v) / span
        return out

    @staticmethod
    def _normalize_score_map(scores: dict[str, float]) -> dict[str, float]:
        if not scores:
            return {}
        values = list(scores.values())
        min_v = min(values)
        max_v = max(values)
        if max_v <= min_v:
            return {uri: 1.0 for uri in scores}
        span = max_v - min_v
        return {uri: (float(score) - min_v) / span for uri, score in scores.items()}

    @staticmethod
    def _raw_scores_by_uri(hits: list[dict]) -> dict[str, float]:
        return {
            str(h.get("uri") or ""): float(h.get("score") or 0.0)
            for h in hits
            if h.get("uri")
        }

    def _fuse_code_hits(
        self,
        *,
        embedding_hits: list[dict],
        bm25_hits: list[dict],
        ctags_hits: list[dict],
        graph_hits: list[dict],
        embedding_score_hits: list[dict] | None = None,
        bm25_score_hits: list[dict] | None = None,
        ctags_score_hits: list[dict] | None = None,
        graph_score_hits: list[dict] | None = None,
        limit: int,
    ) -> list[dict]:
        candidates: dict[str, dict] = {}
        for group in (embedding_hits, bm25_hits, ctags_hits, graph_hits):
            for hit in group:
                uri = str(hit.get("uri") or "")
                if not uri:
                    continue
                if uri not in candidates:
                    candidates[uri] = dict(hit)
                elif hit.get("retrieval_source") == "graph":
                    candidates[uri]["graph_distance"] = hit.get("graph_distance")
                    candidates[uri]["graph_relation_counts"] = hit.get("graph_relation_counts")
                    candidates[uri]["graph_overview"] = hit.get("overview")

        embedding_score_hits = embedding_score_hits if embedding_score_hits is not None else embedding_hits
        bm25_score_hits = bm25_score_hits if bm25_score_hits is not None else bm25_hits
        ctags_score_hits = ctags_score_hits if ctags_score_hits is not None else ctags_hits
        graph_score_hits = graph_score_hits if graph_score_hits is not None else graph_hits

        norm_bm25 = self._normalize_scores(bm25_score_hits)
        norm_ctags = self._normalize_scores(ctags_score_hits)
        norm_graph = self._normalize_scores(graph_score_hits)
        raw_embed = self._raw_scores_by_uri(embedding_score_hits)
        raw_bm25 = self._raw_scores_by_uri(bm25_score_hits)
        raw_ctags = self._raw_scores_by_uri(ctags_score_hits)
        raw_graph = self._raw_scores_by_uri(graph_score_hits)

        fuse_mode = os.environ.get("CONTEXTSNIPER_CODE_FUSE_MODE", "weighted_rrf").strip().lower()
        w_embed = float(os.environ.get("CONTEXTSNIPER_CODE_FUSE_W_EMBED", "0.33")) if _code_retrieval_enabled("semantic") else 0.0
        w_bm25 = float(os.environ.get("CONTEXTSNIPER_CODE_FUSE_W_BM25", "0.17")) if _code_retrieval_enabled("frequency") else 0.0
        w_ctags = float(os.environ.get("CONTEXTSNIPER_CODE_FUSE_W_CTAGS", "0.17")) if _code_retrieval_enabled("symbolic") else 0.0
        w_graph = float(os.environ.get("CONTEXTSNIPER_CODE_FUSE_W_GRAPH", "0.33")) if _code_retrieval_enabled("graph") else 0.0
        route_weights = {
            "embedding": w_embed,
            "bm25": w_bm25,
            "ctags": w_ctags,
            "graph": w_graph,
        }
        route_hit_lists = {
            "embedding": embedding_hits,
            "bm25": bm25_hits,
            "ctags": ctags_hits,
            "graph": graph_hits,
        }
        route_ranks: dict[str, dict[str, int]] = {}
        for route, hits in route_hit_lists.items():
            route_ranks[route] = {
                str(hit.get("uri") or ""): rank
                for rank, hit in enumerate(hits, start=1)
                if hit.get("uri")
            }

        fused: list[tuple[float, dict]] = []
        for uri, hit in candidates.items():
            s_embed = raw_embed.get(uri, 0.0)
            s_bm25 = norm_bm25.get(uri, 0.0)
            s_ctags = norm_ctags.get(uri, 0.0)
            s_graph = norm_graph.get(uri, 0.0)
            if fuse_mode in {"weighted_sum", "sum"}:
                score = (
                    (w_embed * s_embed)
                    + (w_bm25 * s_bm25)
                    + (w_ctags * s_ctags)
                    + (w_graph * s_graph)
                )
                score_kind = "weighted_sum_embedding_raw_bm25_ctags_graph_normalized"
            else:
                score = 0.0
                for route, ranks in route_ranks.items():
                    rank = ranks.get(uri)
                    if rank is not None:
                        score += route_weights.get(route, 0.0) / (60.0 + rank)
                score_kind = "weighted_rrf_embedding_bm25_ctags_graph_topk"
            item = dict(hit)
            item["score"] = float(score)
            item["retrieval_source"] = "hybrid"
            item["score_kind"] = score_kind
            item["score_weights"] = route_weights
            item["route_ranks"] = {
                route: ranks.get(uri)
                for route, ranks in route_ranks.items()
                if ranks.get(uri) is not None
            }
            item["raw_scores"] = {
                "embedding": raw_embed.get(uri, 0.0),
                "bm25": raw_bm25.get(uri, 0.0),
                "ctags": raw_ctags.get(uri, 0.0),
                "graph": raw_graph.get(uri, 0.0),
            }
            item["normalized_scores"] = {
                "embedding": raw_embed.get(uri, 0.0),
                "bm25": norm_bm25.get(uri, 0.0),
                "ctags": norm_ctags.get(uri, 0.0),
                "graph": norm_graph.get(uri, 0.0),
            }
            # Backward compatibility: older clients read fused_scores. These
            # are now the component scores used by the final fusion formula.
            item["fused_scores"] = {
                "embedding": s_embed,
                "bm25": s_bm25,
                "ctags": s_ctags,
                "graph": s_graph,
            }
            fused.append((score, item))
        fused.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in fused[:limit]]

    def _ingest_workspace_code_path(
        self,
        *,
        workspace_root: Path,
        relative_path: str,
        ctx: RequestContext,
        project_id: str,
    ) -> dict | None:
        """Read and ingest one workspace file into code memory."""
        write_api = self.get_write_api()
        if write_api is None:
            return None

        full_path = (workspace_root / relative_path).resolve()
        try:
            full_path.relative_to(workspace_root)
        except ValueError:
            logger.warning("Skipping path outside workspace root: %s", relative_path)
            return {"ok": False, "file_path": relative_path, "error": "outside_workspace"}
        if not full_path.is_file():
            return {"ok": False, "file_path": relative_path, "error": "not_found"}

        try:
            source_code = full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.info("Skipping non-utf8 source file %s", full_path)
            return {"ok": False, "file_path": relative_path, "error": "non_utf8"}
        except Exception as exc:
            logger.warning("Failed reading source file %s: %s", full_path, exc)
            return {"ok": False, "file_path": relative_path, "error": str(exc)}

        if not source_code.strip():
            return {"ok": False, "file_path": relative_path, "error": "empty"}

        canonical_file_path = str(full_path)
        result = write_api.ingest_code_file(
            file_path=canonical_file_path,
            source_code=source_code,
            ctx=ctx,
            project_id=project_id,
            parallel=True,
        )
        return {"ok": True, "file_path": canonical_file_path, **result}

    def _list_code_memory_uris_for_file(
        self,
        *,
        file_path: Path,
        ctx: RequestContext,
    ) -> list[str]:
        """Return active code-memory URIs whose stored metadata belongs to one file."""
        return list(self._list_code_memory_records_for_file(file_path=file_path, ctx=ctx).keys())

    def _list_code_memory_records_for_file(
        self,
        *,
        file_path: Path,
        ctx: RequestContext,
    ) -> dict[str, dict]:
        """Return active code-memory raw metadata records for one source file."""
        if not _HAS_AGFS:
            return {}

        client = AGFSClient(api_base_url=self._agfs_base_url)
        agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
        code_root_uri = f"ctx://{ctx.account_id}/agents/{ctx.agent_id}/memories/code"
        target_norm = str(file_path.resolve()).replace("\\", "/")

        hits: dict[str, dict] = {}
        for child_uri in agfs.list_children(code_root_uri, ctx):
            raw_meta: dict = {}
            node = None
            try:
                node_path = agfs._uri_to_agfs_path(child_uri)
                meta_content = agfs._read_file(node_path + agfs.FILE_META)
                raw_meta = json.loads(meta_content) if meta_content else {}
            except Exception:
                raw_meta = {}

            stored = str(raw_meta.get("file_path") or "").strip()
            try:
                stored_norm = str(Path(stored).expanduser().resolve()).replace("\\", "/")
            except Exception:
                stored_norm = stored.replace("\\", "/")
            if stored_norm == target_norm:
                hits[child_uri] = raw_meta
                continue

            ctags = raw_meta.get("ctags") if isinstance(raw_meta, dict) else {}
            ctags_path = str((ctags or {}).get("path") or "").strip().replace("\\", "/")
            if ctags_path and (
                target_norm.endswith(f"/{ctags_path}") or target_norm == ctags_path
            ):
                hits[child_uri] = raw_meta
                continue

            blob_parts = [
                str(raw_meta.get("code_identity") or ""),
                str(raw_meta.get("graph_document") or ""),
                str(raw_meta.get("bm25_document") or ""),
            ]
            if not any(blob_parts):
                try:
                    node = node or agfs.read_node(child_uri, ctx)
                    blob_parts.extend([
                        getattr(node, "content", "") or "",
                        getattr(node, "overview", "") or "",
                        getattr(node, "abstract", "") or "",
                    ])
                except Exception:
                    pass
            blob = "\n".join(blob_parts).replace("\\", "/")
            if target_norm in blob:
                hits[child_uri] = raw_meta
        return hits

    @staticmethod
    def _should_reject_code_chunk(file_path: str, symbol: str) -> bool:
        """Reject synthetic/internal diagnostic chunks from code memory."""
        path = str(file_path or "").strip().lower()
        base = os.path.basename(path)
        sym = str(symbol or "").strip().lower()

        if base.startswith("turn_"):
            return True
        if "diag_many_funcs.py" in path:
            return True
        if re.fullmatch(r"ast_h\d+", sym) and "diag" in path:
            return True
        return False

    def _expected_code_memory_records_for_file(
        self,
        *,
        workspace_root: Path,
        relative_path: str,
        ctx: RequestContext,
    ) -> dict[str, dict]:
        """Return expected current code-memory URI/hash records for a workspace file."""
        full_path = (workspace_root / relative_path).resolve()
        try:
            full_path.relative_to(workspace_root.resolve())
        except ValueError:
            return {}
        if not full_path.is_file():
            return {}
        try:
            source_code = full_path.read_text(encoding="utf-8")
        except Exception:
            return {}
        if not source_code.strip():
            return {}

        lang = detect_language(str(full_path))
        try:
            chunks = chunk_source_code(source_code, str(full_path), language=lang)
        except Exception as exc:
            logger.warning("code sync chunk planning failed for %s: %s", relative_path, exc)
            return {}

        expected: dict[str, dict] = {}
        for chunk in chunks:
            if self._should_reject_code_chunk(chunk.file_path, chunk.symbol):
                continue
            uri = self._code_memory_uri_for_chunk(chunk, ctx)
            if not uri:
                continue
            expected[uri] = {
                "chunk_hash": getattr(chunk, "chunk_hash", ""),
                "file_path": str(full_path),
                "symbol": getattr(chunk, "symbol", ""),
                "start_line": getattr(chunk, "start_line", None),
                "end_line": getattr(chunk, "end_line", None),
            }
        return expected

    def _delete_code_memory_uris(
        self,
        *,
        uris: list[str],
        ctx: RequestContext,
    ) -> dict:
        """Delete code-memory nodes and enqueue vector-index delete events."""
        if not _HAS_AGFS:
            return {"deleted": 0, "failed": 0, "uris": [], "errors": ["agfs_unavailable"]}
        if not uris:
            return {"deleted": 0, "failed": 0, "uris": [], "errors": []}

        client = AGFSClient(api_base_url=self._agfs_base_url)
        agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
        outbox = OutboxStore(client=client, fs=agfs, mount_prefix=self._mount_prefix)

        deleted = 0
        failed = 0
        errors: list[str] = []
        deleted_uris: list[str] = []
        for uri in uris:
            try:
                outbox.register_delete(uri, ctx)
                agfs.delete_node(uri, ctx)
                deleted += 1
                deleted_uris.append(uri)
            except Exception as exc:
                failed += 1
                errors.append(f"{uri}: {exc}")
                logger.warning("Failed deleting code memory uri %s: %s", uri, exc)
        return {"deleted": deleted, "failed": failed, "uris": deleted_uris, "errors": errors}

    def code_refresh_workspace_path(self, params: dict) -> dict:
        """Rebuild code memory for one workspace file after an edit."""
        ctx = params.get("_ctx") or self.build_context(params)
        workspace_root = self._resolve_workspace_root(params)
        if workspace_root is None:
            return {"ok": False, "error": "no_workspace"}

        raw = (params.get("file_path") or params.get("filePath") or "").strip()
        if not raw:
            return {"ok": False, "error": "missing_file_path"}

        p = Path(raw)
        if not p.is_absolute():
            p = (workspace_root / p).resolve()
        try:
            rel = str(p.relative_to(workspace_root.resolve()))
        except ValueError:
            return {"ok": False, "error": "outside_workspace"}

        project_id = (
            params.get("projectId")
            or params.get("project_id")
            or workspace_root.name
        )
        wait_for_index = bool(params.get("wait_for_index", params.get("waitForIndex", False)))
        timeout_raw = params.get("refresh_timeout_sec", params.get("refreshTimeoutSec"))
        if timeout_raw is None:
            timeout_raw = os.environ.get("CONTEXTSNIPER_EDIT_REFRESH_TIMEOUT_SEC", "5")
        try:
            timeout_sec = max(1, int(float(timeout_raw)))
        except (TypeError, ValueError):
            timeout_sec = 5
        deadline = time.time() + timeout_sec if wait_for_index else None

        stale_uris = self._list_code_memory_uris_for_file(file_path=p, ctx=ctx)
        delete_out = self._delete_code_memory_uris(uris=stale_uris, ctx=ctx)
        if wait_for_index:
            delete_drain = self._drain_outbox_until_quiet(
                account_id=ctx.account_id,
                deadline=deadline,
            )
        else:
            self._async_drain(account_id=ctx.account_id)
            delete_drain = {"background": True, "timed_out": False}

        ingest_out = self._ingest_workspace_code_path(
            workspace_root=workspace_root,
            relative_path=rel,
            ctx=ctx,
            project_id=str(project_id),
        )
        if ingest_out is None:
            return {
                "ok": False,
                "error": "write_api_unavailable",
                "relative_path": rel,
                "deleted": delete_out,
                "delete_drain": delete_drain,
            }

        if wait_for_index:
            ingest_drain = self._drain_outbox_until_quiet(
                account_id=ctx.account_id,
                deadline=deadline,
            )
        else:
            self._async_drain(account_id=ctx.account_id)
            ingest_drain = {"background": True, "timed_out": False}
        return {
            "ok": bool(ingest_out.get("ok")),
            "relative_path": rel.replace("\\", "/"),
            "deleted": delete_out,
            "delete_drain": delete_drain,
            "ingested": ingest_out,
            "ingest_drain": ingest_drain,
            "wait_for_index": wait_for_index,
            "background_refresh_dispatched": not wait_for_index,
            "refresh_timeout_sec": timeout_sec,
        }

    def _ingest_bootstrap_file_batch(
        self,
        workspace_root: Path,
        params: dict,
        ctx: RequestContext,
        *,
        already: set[str],
        max_new_paths: int | None = None,
        query: str = "",
    ) -> list[str]:
        """Ingest additional repo files not yet seen for bootstrap/search."""
        if not self.get_write_api():
            return []
        if max_new_paths is None:
            max_new_paths = _bootstrap_max_files()
        new_paths: list[str] = []
        project_id = (
            params.get("projectId")
            or params.get("project_id")
            or workspace_root.name
        )
        candidates: list[str] = []
        if query:
            prioritized = self._rg_query_candidate_paths(
                workspace_root,
                query=query,
                already=already,
                limit=max_new_paths or _bootstrap_max_files(),
            )
            candidates.extend(prioritized)

        for rel in self._list_workspace_code_files(workspace_root, max_files=None):
            if rel in already or rel in candidates:
                continue
            candidates.append(rel)
            if max_new_paths is not None and len(candidates) >= max_new_paths:
                break
        if not candidates:
            return []

        already.update(candidates)

        workers_raw = os.environ.get("CONTEXTSNIPER_BOOTSTRAP_INGEST_WORKERS", "8")
        try:
            workers = int(workers_raw)
        except ValueError:
            workers = 8
        workers = max(1, min(workers, 32))

        if workers == 1 or len(candidates) == 1:
            for rel in candidates:
                res = self._ingest_workspace_code_path(
                    workspace_root=workspace_root,
                    relative_path=rel,
                    ctx=ctx,
                    project_id=str(project_id),
                )
                if res and res.get("ok"):
                    new_paths.append(rel)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                fut_to_rel = {
                    pool.submit(
                        self._ingest_workspace_code_path,
                        workspace_root=workspace_root,
                        relative_path=rel,
                        ctx=ctx,
                        project_id=str(project_id),
                    ): rel
                    for rel in candidates
                }
                for fut in as_completed(fut_to_rel):
                    rel = fut_to_rel[fut]
                    try:
                        res = fut.result()
                    except Exception as exc:
                        logger.warning("bootstrap ingest failed for %s: %s", rel, exc)
                        continue
                    if res and res.get("ok"):
                        new_paths.append(rel)
        if new_paths:
            logger.info(
                "code bootstrap ingested %d file(s) under %s",
                len(new_paths),
                workspace_root,
            )
        return new_paths


    # -- System prompt builder (KV-cache friendly) ----------------------------

    def _format_profile(
        self,
        profile: str,
        budget: dict[str, int],
    ) -> str:
        """Build stable identity context (Profile only) → systemPromptAddition.

        This is the MOST stable layer — changes only when user profile updates.
        Fully KV-cacheable across all turns and sessions.
        """
        if not profile:
            return ""

        max_tokens = budget.get("identity", 5000)
        text = f"## Profile\n{profile}"
        max_chars = max_tokens * 4
        if len(text) > max_chars:
            text = text[:max_chars]
        return text

    def _format_archives(
        self,
        latest_archives: list[ArchiveRef],
        pre_archives: list[ArchiveRef],
        budget: dict[str, int],
    ) -> str:
        """Build episodic history context (Archives only) → systemPromptSuffix prefix.

        Goes into systemPromptSuffix BEFORE session state. Semi-stable:
        only changes when sessions are archived (not every turn).
        """
        max_tokens = budget.get("archive", 40000)

        archive_parts: list[str] = []
        if latest_archives:
            latest = latest_archives[0]
            if latest.overview:
                archive_parts.append(f"### Latest Session\n{latest.overview}")
        if pre_archives:
            lines = ["### Previous Sessions"]
            for ref in pre_archives:
                lines.append(f"- {ref.archive_id}: {ref.abstract}")
            archive_parts.append("\n".join(lines))

        if not archive_parts:
            return ""

        text = "## Archive History\n" + "\n".join(archive_parts)
        max_chars = max_tokens * 4
        if len(text) > max_chars:
            text = text[:max_chars]
        return text

    def _format_working_set(self, working_set: list[dict], budget: dict[str, int]) -> str:
        """Build DYNAMIC working set as user message content.

        This goes into memoryUserMessage and changes every turn.
        Placed in user message so KV cache for stable system prompt
        prefix is preserved across turns.

        Args:
            working_set: List of retrieved memory items
            budget: dict allocation from allocate()
        """
        if not working_set:
            return ""
        ws_lines = [
            "## Retrieved Memories",
            "The following memories were retrieved from the user's long-term memory. "
            "Use them to answer the user's question. If the answer is found in these memories, respond based on them.",
            "",
        ]
        for item in working_set:
            cat = item.get("category", "memory")
            abstract = item.get("abstract", "")
            content = item.get("content", "")
            text_to_show = content if content else abstract

            # Add temporal context if available
            when = (item.get("metadata") or {}).get("when")
            if when:
                text_to_show = f"[{when}] {text_to_show}"

            ws_lines.append(f"- [{cat}] {text_to_show}")
        ws_text = "\n".join(ws_lines)
        ws_tokens = len(ws_text) // 4

        working_set_budget = budget.get("working_set", 20000)

        if ws_tokens > working_set_budget and working_set_budget > 100:
            ws_text = ws_text[: working_set_budget * 4]
        return ws_text

    def _get_session_state(
        self,
        session_id: str,
        ctx: RequestContext,
    ) -> SessionWindowState:
        """Get or compress session window state (Layer 2)."""
        mgr = self.get_session_manager()
        buf = mgr.get_or_create(session_id)
        window_state = buf.window_state

        if not getattr(self._cfg, "rolling_compress_enabled", False):
            return window_state

        if not buf.should_compress():
            return window_state

        # Need to compress — use RollingCompressor
        try:
            compressor = RollingCompressor(llm=self.get_llm())
            window_state = compressor.compress(buf.messages, window_state)
            mgr.update_window_state(session_id, window_state)
            logger.info(
                "Session window compressed: session=%s turns=%d tokens=%d",
                session_id,
                window_state.turn_count_at_last_compress,
                window_state.token_count_at_last_compress,
            )
        except Exception as exc:
            logger.warning("Session window compression failed: %s", exc)

        return window_state

    def _format_session_state(
        self,
        window_state: SessionWindowState,
        budget: dict[str, int],
    ) -> str:
        """Build Layer 2 session state suffix for system prompt.

        Goes into systemPromptSuffix — updated atomically every N turns.
        Placed at end of system prompt so KV cache for stable prefix
        (Layer 1) is preserved; only this suffix chunk changes.

        Args:
            window_state: Current session window state
            budget: dict allocation from allocate()
        """
        sections: list[str] = []

        if window_state.active_task:
            sections.append(f"## Active Task\n{window_state.active_task}")

        if window_state.confirmed_constraints:
            items = "\n".join(f"- {c}" for c in window_state.confirmed_constraints[:5])
            sections.append(f"## Confirmed Constraints\n{items}")

        if window_state.recent_decisions:
            items = "\n".join(f"- {d}" for d in window_state.recent_decisions[:5])
            sections.append(f"## Recent Decisions\n{items}")

        if window_state.open_loops:
            items = "\n".join(f"- {l}" for l in window_state.open_loops[:5])
            sections.append(f"## Open Loops\n{items}")

        if window_state.compressed_text:
            sections.append(f"## Recent Session Summary\n{window_state.compressed_text}")

        if not sections:
            return ""

        combined = "\n\n".join(sections)

        session_state_budget = budget.get("session_state", 10000)

        # Truncate to budget
        max_chars = session_state_budget * 4
        if len(combined) > max_chars:
            combined = combined[:max_chars]
        return combined

    # -- Token estimation -----------------------------------------------------

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """Estimate token count for a list of messages.

        Uses CJK-aware estimation: ~1.5 chars/token for CJK, ~4 chars/token for English.
        """
        total_chars = 0
        cjk_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
                cjk_chars += sum(1 for c in content if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
            elif isinstance(content, list):
                for block in content:
                    text = block.get("text", "") if isinstance(block, dict) else (block if isinstance(block, str) else "")
                    total_chars += len(text)
                    cjk_chars += sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
        # CJK: ~1.5 chars/token, English: ~4 chars/token
        cjk_tokens = int(cjk_chars / 1.5)
        en_tokens = (total_chars - cjk_chars) // 4
        return cjk_tokens + en_tokens

    # -- Lazy resource getters ------------------------------------------------

    def _get_shared_vector_index(self):
        """Lazy-init shared vector index (same instance for read + outbox worker)."""
        if self._vector_index is None:
            self._vector_index = self._provider_cfg.create_vector_index()
            logger.info("Shared vector_index ready: %s", type(self._vector_index).__name__)
        return self._vector_index

    def _get_shared_embedder(self):
        """Lazy-init shared embedder (same instance for read + outbox worker)."""
        if self._embedder is None:
            self._embedder = self._provider_cfg.create_embedder()
            logger.info("Shared embedder ready: %s", type(self._embedder).__name__)
        return self._embedder

    def _start_outbox_worker(self):
        """Start OutboxWorker background thread (once, idempotent)."""
        if self._outbox_thread is not None:
            return
        if not _HAS_AGFS:
            logger.info("OutboxWorker skipped: AGFS not available")
            return
        try:
            from index.outbox_worker import OutboxWorker
            from commit.outbox_store import OutboxStore

            vector_index = self._get_shared_vector_index()
            embedder = self._get_shared_embedder()

            client = AGFSClient(api_base_url=self._agfs_base_url)
            agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
            outbox_store = OutboxStore(client=client, fs=agfs, mount_prefix=self._mount_prefix)

            worker = OutboxWorker(
                vector_index=vector_index,
                embedder=embedder,
                fs=agfs,
                llm=self.get_llm(),
                directory_summary_enabled=getattr(self._cfg, 'directory_summary_enabled', False),
            )

            def _get_account_ids_to_scan():
                if self._cfg.role_control_enabled:
                    try:
                        accounts = self.get_key_manager().get_accounts()
                        return [a["account_id"] for a in accounts if a.get("account_id")]
                    except Exception as exc:
                        logger.warning("Failed to list accounts for outbox scan: %s", exc)
                return [self._default_account_id]

            def _outbox_loop():
                import time
                logger.info("OutboxWorker thread started (polling every 5s)")
                while True:
                    try:
                        account_ids = _get_account_ids_to_scan()
                        worker.run_once(
                            outbox_store=outbox_store,
                            account_ids=account_ids,
                        )
                    except Exception as exc:
                        logger.warning("OutboxWorker poll error: %s", exc)
                    time.sleep(5)

            self._outbox_thread = threading.Thread(
                target=_outbox_loop, daemon=True, name="outbox-worker"
            )
            self._outbox_thread.start()
            logger.info("OutboxWorker background thread launched")
        except Exception as exc:
            logger.warning("Failed to start OutboxWorker: %s", exc, exc_info=True)

    def _async_drain(self, account_id: str | None = None):
        """Background thread wrapper for outbox drain with logging."""
        try:
            drain_stats = self.drain_outbox_sync(account_id=account_id)
            logger.info(
                "outbox_drain processed=%d succeeded=%d failed=%d",
                drain_stats.get("processed", 0),
                drain_stats.get("succeeded", 0),
                drain_stats.get("failed", 0),
            )
        except Exception as exc:
            logger.error("outbox_drain failed in background: %s", exc, exc_info=True)

    def drain_outbox_sync(self, account_id: str | None = None) -> dict:
        """Synchronously process pending OutboxEvents: embed → upsert to vector index.

        In subprocess mode the background OutboxWorker thread dies with the process,
        so we must drain inline before returning to ensure data is indexed.
        ChromaDB persists to disk, so the next subprocess call will find the data.
        """
        if not _HAS_AGFS:
            return {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0}
        try:
            from index.outbox_worker import OutboxWorker
            from commit.outbox_store import OutboxStore

            vector_index = self._get_shared_vector_index()
            embedder = self._get_shared_embedder()

            client = AGFSClient(api_base_url=self._agfs_base_url)
            agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
            outbox_store = OutboxStore(client=client, fs=agfs, mount_prefix=self._mount_prefix)

            worker = OutboxWorker(
                vector_index=vector_index,
                embedder=embedder,
                fs=agfs,
                llm=self.get_llm(),
                directory_summary_enabled=getattr(self._cfg, 'directory_summary_enabled', False),
            )

            effective_account_id = account_id or self._default_account_id
            stats = worker.run_once(
                outbox_store=outbox_store,
                account_ids=[effective_account_id],
            )
            logger.info("drain_outbox_sync: %s", stats)
            return stats
        except Exception as exc:
            logger.warning("drain_outbox_sync failed: %s", exc)
            return {"processed": 0, "succeeded": 0, "failed": 1, "error": str(exc)}

    def _drain_outbox_until_quiet(
        self,
        account_id: str | None = None,
        *,
        max_rounds: int | None = None,
        deadline: float | None = None,
    ) -> dict:
        """Drain repeatedly until pending events are quiet."""
        if max_rounds is None:
            raw = os.environ.get("CONTEXTSNIPER_SEARCH_BOOTSTRAP_DRAIN_ROUNDS", "30")
            try:
                max_rounds = int(raw)
            except ValueError:
                max_rounds = 30
        max_rounds = max(1, min(max_rounds, 100))

        total = {
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "rounds": 0,
            "timed_out": False,
        }
        last: dict = {}
        for _ in range(max_rounds):
            if deadline is not None and time.time() >= deadline:
                total["timed_out"] = True
                break
            last = self.drain_outbox_sync(account_id=account_id) or {}
            total["rounds"] += 1
            for key in ("processed", "succeeded", "failed", "skipped"):
                try:
                    total[key] += int(last.get(key, 0))
                except (TypeError, ValueError):
                    pass
            if int(last.get("processed", 0) or 0) == 0 and int(last.get("skipped", 0) or 0) == 0:
                break
            if deadline is not None and time.time() >= deadline:
                total["timed_out"] = True
                break
            time.sleep(0.2)
        total["last"] = last
        return total

    def _poll_outbox_until_quiet(
        self,
        account_id: str | None = None,
        *,
        deadline: float | None = None,
        poll_interval_sec: float = 0.2,
    ) -> dict:
        """Wait for the background outbox worker to quiesce without doing inline work."""
        if not _HAS_AGFS:
            return {"pending": 0, "rounds": 0, "timed_out": False}

        effective_account_id = account_id or self._default_account_id
        total = {"pending": 0, "rounds": 0, "timed_out": False}

        try:
            client = AGFSClient(api_base_url=self._agfs_base_url)
            agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
            outbox_store = OutboxStore(client=client, fs=agfs, mount_prefix=self._mount_prefix)
        except Exception as exc:
            logger.warning("outbox quiet poll unavailable: %s", exc)
            return {"pending": -1, "rounds": 0, "timed_out": False, "error": str(exc)}

        while True:
            if deadline is not None and time.time() >= deadline:
                total["timed_out"] = True
                break
            try:
                pending = len(outbox_store.list_pending(effective_account_id))
            except Exception as exc:
                logger.warning("outbox pending check failed: %s", exc)
                return {
                    "pending": -1,
                    "rounds": total["rounds"],
                    "timed_out": False,
                    "error": str(exc),
                }
            total["rounds"] += 1
            total["pending"] = pending
            if pending <= 0:
                break
            time.sleep(max(0.05, poll_interval_sec))
        return total

    def get_llm(self):
        if self._llm is None:
            cfg = self._provider_cfg
            if cfg.provider == "mock" and cfg.openai_api_key:
                cfg = ProviderConfig(
                    provider="openai",
                    openai_api_key=cfg.openai_api_key,
                    openai_base_url=cfg.openai_base_url,
                    openai_llm_model=cfg.openai_llm_model,
                    llm_temperature=cfg.llm_temperature,
                    llm_max_tokens=cfg.llm_max_tokens,
                )
            if cfg.provider == "mock":
                from providers.llm import MockLLM
                self._llm = MockLLM()
            else:
                OpenAILLM, _ = get_openai_llm()
                self._llm = OpenAILLM(
                    api_key=cfg.effective_openai_api_key(),
                    base_url=cfg.openai_base_url,
                    model=cfg.openai_llm_model,
                )
            logger.info("LLM ready: %s", type(self._llm).__name__)
        return self._llm

    def get_write_api(self):
        if self._write_api is None:
            if not _HAS_AGFS:
                return None
            client = AGFSClient(api_base_url=self._agfs_base_url)
            agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
            outbox = OutboxStore(client=client, fs=agfs, mount_prefix=self._mount_prefix)

            # Create SchemaRegistry for dynamic tool generation
            schema_registry = None
            try:
                from extraction.schemas.registry import SchemaRegistry
                schema_registry = SchemaRegistry()
                logger.info("SchemaRegistry initialized for dynamic tool generation")
            except Exception as exc:
                logger.warning("SchemaRegistry initialization failed, using legacy tools: %s", exc)

            # Create URIResolver for prefetch
            uri_resolver = None
            try:
                from core.uri_resolver import URIResolver
                if schema_registry:
                    uri_resolver = URIResolver(schema_registry)
                    logger.info("URIResolver initialized for prefetch")
            except Exception as exc:
                logger.warning("URIResolver initialization failed, prefetch disabled: %s", exc)

            # Get shared vector_index and embedder for prefetch
            vector_index = self._get_shared_vector_index()
            embedder = self._get_shared_embedder()

            self._write_api = MemoryWriteAPI(
                fs=agfs,
                llm=self.get_llm(),
                outbox_store=outbox,
                schema_registry=schema_registry,
                vector_index=vector_index,
                embedder=embedder,
                uri_resolver=uri_resolver,
            )
            logger.info("WriteAPI ready")
            # Start outbox worker so written memories get indexed
            self._start_outbox_worker()
        return self._write_api

    def get_read_api(self):
        if self._read_api is None:
            embedder = self._get_shared_embedder()
            vector_index = self._get_shared_vector_index()

            cfg = RetrievalConfig()

            # Create relation_store if AGFS is available
            relation_store = None
            if _HAS_AGFS:
                try:
                    rs_client = AGFSClient(api_base_url=self._agfs_base_url)
                    relation_store = AGFSRelationStore(client=rs_client, mount_prefix=self._mount_prefix)
                except Exception:
                    pass

            # Create context_reader for content filling (shared by pipeline and read_service)
            context_reader = None
            if _HAS_AGFS:
                try:
                    client = AGFSClient(api_base_url=self._agfs_base_url)
                    agfs = AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
                    context_reader = ContextReader(fs=agfs)
                except Exception as exc:
                    logger.warning("AGFS unavailable for read_memory: %s", exc)

            pipeline = RetrievalPipeline(
                planner=QueryPlanner(cfg),
                seed_retriever=SeedRetriever(vector_index, embedder, cfg),
                hierarchical_searcher=HierarchicalSearcher(vector_index, cfg),
                assembly=ResultRanker(cfg, relation_store=relation_store),
                config=cfg,
                context_reader=context_reader,
            )

            self._read_api = ReadAPI(
                pipeline=pipeline,
                read_service=context_reader,
                config=cfg,
            )
            logger.info(
                "ReadAPI ready: embedder=%s, index=%s, agfs=%s",
                type(embedder).__name__,
                type(vector_index).__name__,
                "yes" if context_reader else "no",
            )
            # Start outbox worker so pending events from ingest get indexed
            self._start_outbox_worker()
        return self._read_api

    # -- Lifecycle handlers ---------------------------------------------------

    def get_session_manager(self) -> SessionManager:
        """Lazy-init SessionManager with current service dependencies."""
        if self._session_mgr is None:
            self._session_mgr = SessionManager(
                get_llm=self.get_llm,
                get_write_api=self.get_write_api,
                get_agfs=self._get_agfs_fs,
            )
            logger.info("SessionManager ready")
        return self._session_mgr

    def _get_agfs_fs(self):
        """Create a fresh AGFSContextFS instance for session operations."""
        if not _HAS_AGFS:
            return None
        try:
            client = AGFSClient(api_base_url=self._agfs_base_url)
            return AGFSContextFS(client=client, mount_prefix=self._mount_prefix)
        except Exception as exc:
            logger.warning("_get_agfs_fs failed: %s", exc)
            return None

    def compose(self, params: dict) -> dict:
        """Assemble memory context for the current turn.

        Pipeline with 3-layer architecture:
          1. Extract query + budget
          2. Read profile          (Layer 1 — stable identity, rarely changes)
          3. Collect archives       (Layer 1b — episodic history, semi-stable)
          4. Search working set     (Layer 3 — dynamic, changes every turn)
          4b. Get/compress session state (Layer 2 — updates every N turns)
          5. Build stable identity  (Layer 1 output → system message)
          5b. Build episodic + session state (→ system messages)
          6. Return (all context injected into messages as system messages)

        Memory context is injected into messages as system messages,
        ordered by stability (less dynamic first, more dynamic last):

          Before original messages:
            ## Profile            ← changes only on profile update
            ## Archive History    ← changes when sessions end
            ## Session State      ← updates every N turns
          After original messages:
            ## Working Set        ← changes every turn

        systemPromptAddition is empty for KV-cache efficiency.

        Args:
            params: Dict with optional keys:
                - messages: list[dict] - Current conversation messages
                - prompt: str - Direct query prompt (optional)
                - tokenBudget: int - Token budget (default: 128_000)

        Returns:
            Dict with:
                - messages: list[dict] - Messages with injected system messages
                - systemPromptAddition: str - Empty (content in messages)
                - systemPromptSuffix: str - Empty (content in messages)
                - memoryUserMessage: str - Empty (content in messages)
                - estimatedTokens: int - Total estimated tokens
                - archiveCount: int - Number of archives found
                - archiveIncluded: bool - Whether archives were injected
                - identityContext: str - Semantic slot: profile only
                - episodicContext: str - Semantic slot: archive history
                - sessionContext: str - Semantic slot: structured session state
        """
        messages = params.get("messages", [])
        prompt = params.get("prompt", "")

        # Strip previous synthetic memory messages to prevent cumulative duplication.
        # Synthetic messages are tagged with _contextsniper=True by _to_response().
        messages = [
            m for m in messages
            if not (isinstance(m, dict) and m.get("_contextsniper"))
        ]
        logger.info(
            "assemble entry: msgs=%d prompt_len=%d keys=%s",
            len(messages), len(prompt), sorted(params.keys()),
        )
        raw = prompt.strip() if prompt else extract_query(messages)
        query = sanitize_query(raw)

        # Step 1: Budget
        token_budget_value = params.get("tokenBudget", 128_000)
        token_budget = TokenBudget(total=token_budget_value)
        budget_allocation = token_budget.allocate()

        # Empty query: return messages unchanged
        if not query:
            result = ComposedContext(
                messages=messages,
                estimated_tokens=self._estimate_tokens(messages),
                archive_count=0,
                archive_included=False,
            )
            return self._to_response(result)

        try:
            ctx = params.get("_ctx") or self.build_context(params)
        except Exception as exc:
            logger.warning("assemble build_context failed: %s", exc, exc_info=True)
            result = ComposedContext(
                messages=messages,
                estimated_tokens=self._estimate_tokens(messages),
                archive_count=0,
                archive_included=False,
            )
            return self._to_response(result)

        # Each step degrades independently — a failure in one doesn't kill the rest
        profile = ""
        try:
            profile = self._read_profile(ctx)
        except Exception as exc:
            logger.warning("assemble profile read failed: %s", exc)

        latest_archives, pre_archives = [], []
        try:
            latest_archives, pre_archives = self._collect_archives(ctx, token_budget)
        except Exception as exc:
            logger.warning("assemble archive collection failed: %s", exc)

        working_set = []
        try:
            try:
                from filter.config import filter_enabled
                if not filter_enabled():
                    raise ImportError("filter disabled")
                from filter import get_plugin
                plugin = get_plugin()
                query, _ = plugin.short_query(query)
            except Exception:
                pass  # Filter failure should not block search
        except Exception as exc:
            logger.warning("assemble working set search failed: %s", exc)
            working_set = []
        else:
            try:
                working_set = self._search_working_set(query, ctx)
            except Exception as exc:
                logger.warning("assemble working set search failed: %s", exc)
                working_set = []
            else:
                if working_set:
                    try:
                        from filter.config import filter_enabled
                        if not filter_enabled():
                            raise ImportError("filter disabled")
                        from filter import get_plugin
                        plugin = get_plugin()
                        working_set = plugin.short_retrieved_blocks(working_set)
                    except Exception:
                        pass  # Filter failure should not block formatting

        # Each step degrades independently — a failure in one doesn't kill the rest

        # Step 5a: Build stable identity (Profile only) → systemPromptAddition
        identity_context = ""
        try:
            identity_context = self._format_profile(
                profile=profile,
                budget=budget_allocation,
            )
        except Exception as exc:
            logger.warning("assemble identity_context build failed: %s", exc)

        # Step 5b: Build episodic history (Archives only) → systemPromptSuffix prefix
        episodic_context = ""
        try:
            episodic_context = self._format_archives(
                latest_archives=latest_archives,
                pre_archives=pre_archives,
                budget=budget_allocation,
            )
        except Exception as exc:
            logger.warning("assemble episodic_context build failed: %s", exc)

        # Step 5c: Build dynamic working set → user message (preserves KV cache)
        working_set_msg = ""
        try:
            working_set_msg = self._format_working_set(working_set, budget_allocation)
        except Exception as exc:
            logger.warning("assemble working_set_msg build failed: %s", exc)

        # Step 4b: Get/compress session state → Layer 2 (updates every N turns)
        session_state_suffix = ""
        window_state = SessionWindowState()  # Default empty state
        try:
            session_id = params.get("sessionId") or ctx.session_id
            window_state = self._get_session_state(session_id, ctx)
            session_state_suffix = self._format_session_state(
                window_state, budget_allocation,
            )
        except Exception as exc:
            logger.warning("assemble session_state_suffix build failed: %s", exc)

        # Step 5d: Trim archived messages from the message list.
        # If archives exist, the archived content is already represented in
        # episodic_context (position 2). Only keep unarchived messages
        # (session buffer) plus the current turn's new messages.
        trimmed_messages = messages
        try:
            session_id_for_buf = params.get("sessionId") or ctx.session_id
            if episodic_context and session_id_for_buf:
                mgr = self.get_session_manager()
                buf = mgr.get_or_create(session_id_for_buf)
                buf_count = len(buf.messages)

                # buf.messages are the unarchived messages recorded by
                # after_turn.  Prefer finding that exact suffix in the caller's
                # message list so current-turn messages after the buffer are
                # also preserved.  If matching fails, fall back to the named
                # tail margin above so this heuristic is explicit.
                pre_prompt_count = _bounded_message_count(
                    params.get("prePromptMessageCount", 0),
                    len(messages),
                )
                pre_prompt_messages = messages[:pre_prompt_count]
                conversation_messages = messages[pre_prompt_count:]

                if buf_count == 0:
                    keep_from = 0
                    trim_reason = "empty_buffer"
                    if conversation_messages:
                        logger.warning(
                            "compose archive trim skipped because session buffer "
                            "is empty; preserving messages: session=%s trace=%s "
                            "total=%d pre_prompt=%d",
                            getattr(ctx, "session_id", ""),
                            getattr(ctx, "trace_id", ""),
                            len(messages),
                            pre_prompt_count,
                        )
                else:
                    keep_from = _find_last_message_sequence_start(
                        conversation_messages,
                        buf.messages,
                    )
                    trim_reason = "matched_buffer"
                    if keep_from is None:
                        if "prePromptMessageCount" not in params:
                            logger.warning(
                                "compose archive trim using tail margin without "
                                "prePromptMessageCount: session=%s trace=%s "
                                "total=%d buf=%d margin=%d",
                                getattr(ctx, "session_id", ""),
                                getattr(ctx, "trace_id", ""),
                                len(messages),
                                buf_count,
                                _ARCHIVE_TRIM_UNMATCHED_TAIL_MARGIN,
                            )
                        keep_count = min(
                            len(conversation_messages),
                            buf_count + _ARCHIVE_TRIM_UNMATCHED_TAIL_MARGIN,
                        )
                        keep_from = len(conversation_messages) - keep_count
                        trim_reason = "tail_margin"

                if keep_from > 0:
                    kept_messages = conversation_messages[keep_from:]
                    trimmed_messages = pre_prompt_messages + kept_messages
                    logger.info(
                        "compose trimmed archived messages: total=%d buf=%d "
                        "pre_prompt=%d dropped=%d kept=%d reason=%s",
                        len(messages), buf_count, pre_prompt_count, keep_from,
                        len(trimmed_messages), trim_reason,
                    )
        except Exception as exc:
            logger.warning("compose archive trim failed, using full messages: %s", exc)

        # Also trim messages compressed by RollingCompressor if session_state exists
        if session_state_suffix and window_state.turn_count_at_last_compress > 0:
            pre_prompt_count = _bounded_message_count(
                params.get("prePromptMessageCount", 0),
                len(trimmed_messages),
            )
            compressed = window_state.turn_count_at_last_compress
            end_of_compressed = pre_prompt_count + compressed
            if end_of_compressed < len(trimmed_messages):
                trimmed_messages = trimmed_messages[:pre_prompt_count] + trimmed_messages[end_of_compressed:]
                logger.info(
                    "compose trimmed %d compressed messages (pre_prompt=%d, kept=%d)",
                    compressed, pre_prompt_count, len(trimmed_messages),
                )
            elif pre_prompt_count < len(trimmed_messages):
                trimmed_messages = trimmed_messages[:pre_prompt_count]
                logger.info(
                    "compose trimmed to pre_prompt only (pre_prompt=%d, compressed=%d)",
                    pre_prompt_count, compressed,
                )

        # Step 6: Assemble final output
        try:
            system_prompt_suffix = "\n\n".join(
                p for p in [episodic_context, session_state_suffix] if p
            )

            archive_count = len(latest_archives) + len(pre_archives)
            prompt_tokens = (
                len(identity_context) // 4
                + len(system_prompt_suffix) // 4
                + len(working_set_msg) // 4
            )
            msg_tokens = self._estimate_tokens(trimmed_messages)

            # Extract open_loops and uncertainties from window_state
            open_loops = window_state.open_loops if window_state else []
            uncertainties = window_state.uncertainties if window_state else []

            # Build budget metadata by slot
            budget_used_by_slot = {
                "identity": len(identity_context) // 4,
                "episodic": len(episodic_context) // 4,
                "session": len(session_state_suffix) // 4,
                "retrieved": len(working_set_msg) // 4,
            }

            # Create ComposedContext with all semantic slots populated
            result = ComposedContext(
                # Semantic slots
                identity_context=identity_context,
                episodic_context=episodic_context,
                session_context=session_state_suffix,
                task_context="",
                retrieved_evidence=working_set_msg,
                open_loops=open_loops,
                uncertainties=uncertainties,
                # Budget metadata
                estimated_tokens=msg_tokens + prompt_tokens,
                budget_used_by_slot=budget_used_by_slot,
                # Legacy compat
                messages=trimmed_messages,
                archive_count=archive_count,
                archive_included=archive_count > 0,
                system_prompt_suffix=system_prompt_suffix,
            )

            return self._to_response(result)
        except Exception as exc:
            logger.warning("assemble final result construction failed: %s", exc, exc_info=True)
            result = ComposedContext(
                messages=trimmed_messages,
                estimated_tokens=self._estimate_tokens(messages),
                archive_count=0,
                archive_included=False,
            )
            return self._to_response(result)

    def _to_response(self, result: ComposedContext) -> dict:
        """Convert ComposedContext to dict for the OpenClaw plugin.

        Message injection order (all go into messages as user role for KV-cache):
          1. Profile (identity_context) — stable prefix
          2. Episodic (archive history) — rarely changes
          3. Working Set (retrieved_evidence) — before question to avoid confirmation bias
          4. Original messages — grows each turn
          5. Session state — dynamic suffix

        systemPromptAddition/systemPromptSuffix/memoryUserMessage are empty —
        all context is in messages.  The plugin only passes messages through.
        """
        injected_messages = []

        if result.identity_context:
            injected_messages.append({"role": "user", "content": result.identity_context, "_contextsniper": True})

        if result.episodic_context:
            injected_messages.append({"role": "user", "content": result.episodic_context, "_contextsniper": True})

        injected_messages.extend(result.messages)

        if result.session_context:
            injected_messages.append({"role": "user", "content": result.session_context, "_contextsniper": True})

        if result.retrieved_evidence:
            injected_messages.append({"role": "user", "content": result.retrieved_evidence, "_contextsniper": True})

        return {
            "messages": injected_messages,
            "systemPromptAddition": "",
            "systemPromptSuffix": "",
            "memoryUserMessage": "",
            "estimatedTokens": result.estimated_tokens,
            "archiveCount": result.archive_count,
            "archiveIncluded": result.archive_included,
            "stats": result.stats,

            "identityContext": result.identity_context,
            "episodicContext": result.episodic_context or "",
            "sessionContext": result.session_context or "",
            "taskContext": result.task_context,
            "retrievedEvidence": result.retrieved_evidence,
            "openLoops": result.open_loops,
            "uncertainties": result.uncertainties,
            "budgetUsedBySlot": result.budget_used_by_slot,
        }

    def after_turn(self, params: dict) -> dict:
        """Unified write entry point: accumulate session buffer + extract memories.

        Step 1 (every turn): add new messages to session buffer (lightweight).
        Step 2 (threshold reached): extract memories via LLM → write to AGFS →
              drain outbox (embed + upsert vector index) → commit session archive.
        """
        session_id = params.get("sessionId", "unknown")
        messages = params.get("messages", [])
        pre_prompt_count = params.get("prePromptMessageCount", 0)
        if not messages:
            return {"ok": True}

        ctx = params.get("_ctx") or self.build_context(params)

        # Extract new messages only (skip pre-prompt system messages)
        new_messages = messages[pre_prompt_count:]
        if not new_messages:
            return {"ok": True, "status": "no_new_messages"}

        request_session_time = None
        for msg in new_messages:
            created_at = msg.get("created_at") if isinstance(msg, dict) else getattr(msg, "created_at", None)
            request_session_time = _parse_session_time(created_at)
            if request_session_time is not None:
                break

        # Threshold controlled by env var CONTEXTSNIPER_AFTER_TURN_THRESHOLD.
        # Default 5000 tokens (~5-10 turns). Set to 1 for ingest (always extract), 999999 for QA (never extract).
        import os as _os
        threshold = int(_os.environ.get("CONTEXTSNIPER_AFTER_TURN_THRESHOLD", "200"))
        disable_after_turn_extraction = _os.environ.get(
            "CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION", ""
        ).strip().lower() in ("1", "true", "yes", "on")

        # Step 1: Accumulate in session buffer (lightweight, every turn)
        mgr = self.get_session_manager()
        from extraction.tool_collector import update_buffer_stats
        for msg in new_messages:
            role = msg.get("role", "user")
            content_raw = msg.get("content", "")
            parsed = extract_content_and_tool_calls(content_raw)
            content = parsed["text"]
            created_at = msg.get("created_at") if isinstance(msg, dict) else getattr(msg, "created_at", None)
            if content:
                mgr.add_message(session_id, role, content, ctx, created_at=created_at)
            # Accumulate tool call metadata into buffer stats
            if parsed["tool_calls"]:
                buf = mgr.get_or_create(session_id)
                update_buffer_stats(buf.tool_usage_stats, parsed["tool_calls"])

        # Check pending_tokens
        session = mgr.get_session(session_id, ctx)
        pending_tokens = session.get("pending_tokens", 0)

        if pending_tokens < threshold:
            return {"ok": True, "status": "accumulating", "pending_tokens": pending_tokens}

        if disable_after_turn_extraction:
            logger.info(
                "after_turn extraction disabled: session=%s pending_tokens=%d threshold=%d",
                session_id[:8],
                pending_tokens,
                threshold,
            )
            result = mgr.commit(session_id, ctx, wait=False)
            return {
                **result,
                "ok": True,
                "status": "extraction_disabled",
                "pending_tokens": pending_tokens,
            }

        # Step 2: Threshold reached → extract memories + write + index + archive
        write_api = self.get_write_api()
        if write_api is None:
            logger.warning("after_turn: write API unavailable, session commit only")
            result = mgr.commit(session_id, ctx, wait=False)
            return {"ok": True, **result}

        # Extract candidates → filter → write to AGFS → create OutboxEvents
        # Use INCREMENTAL messages (after watermark) + extraction summary
        buf = mgr.get_or_create(session_id)
        extraction_run_id = uuid4().hex[:12]
        extraction_in_progress_before = buf.extraction_in_progress
        extraction_state = self._build_incremental_extraction_state(buf)
        incremental = extraction_state["incremental"]
        archive_snapshot = list(incremental)
        archive_snapshot_ids = {m.id for m in archive_snapshot}
        extraction_messages = extraction_state["messages"]

        if extraction_messages:
            try:
                from filter.config import filter_enabled
                if not filter_enabled():
                    raise ImportError("filter disabled")
                from filter import get_plugin
                plugin = get_plugin()
                shortened = plugin.short_session_messages(
                    extraction_messages, mode="generic", max_lines=60
                )
                original_len = sum(len(m.get("content", "") or "") for m in extraction_messages)
                shortened_len = sum(len(m.get("content", "") or "") for m in shortened)
                if shortened_len < original_len * 0.95:
                    extraction_messages = shortened
                    logger.info(
                        "after_turn filter shortening: %d -> %d chars (%.1f%%)",
                        original_len,
                        shortened_len,
                        shortened_len / original_len * 100 if original_len > 0 else 100,
                    )
            except ImportError:
                pass
            except Exception as exc:
                logger.warning("after_turn filter shortening failed: %s", exc)

        logger.info(
            "after_turn extraction scheduled: run=%s session=%s "
            "pending_tokens=%d buffer_len=%d watermark=%d incremental=%d "
            "extractable=%d active_extractions=%d",
            extraction_run_id,
            session_id[:8],
            pending_tokens,
            len(buf.messages),
            buf.extraction_watermark,
            len(incremental),
            len(extraction_messages),
            getattr(buf, "extraction_active_count", 0),
        )
        if extraction_in_progress_before:
            logger.warning(
                "after_turn overlapping extraction detected: run=%s session=%s "
                "buffer_len=%d watermark=%d active_extractions=%d",
                extraction_run_id,
                session_id[:8],
                len(buf.messages),
                buf.extraction_watermark,
                getattr(buf, "extraction_active_count", 0),
            )
        if not extraction_messages:
            buf.extraction_watermark = len(buf.messages)
            drain_stats = self.drain_outbox_sync(account_id=ctx.account_id)
            commit_result = mgr.commit_snapshot(session_id, archive_snapshot, ctx, wait=True)
            removed = 0
            if commit_result.get("archived"):
                removed = buf.remove_messages_by_id(archive_snapshot_ids)
            logger.info(
                "after_turn no-extractable snapshot commit: run=%s session=%s "
                "archived=%s removed=%d reason=%s",
                extraction_run_id,
                session_id[:8],
                commit_result.get("archived"),
                removed,
                commit_result.get("reason", ""),
            )
            return {
                "ok": True,
                "status": "no_extractable_messages",
                "drain": drain_stats,
                "commit": commit_result,
                "extraction_run_id": extraction_run_id,
            }

        # Keep the upstream async after_turn behavior, but source extraction
        # inputs from the shared incremental-state helper so after_turn and
        # prepare_compaction operate on the same delta semantics.
        tool_stats_text = extraction_state["tool_stats_text"]
        session_time = extraction_state["session_time"] or request_session_time
        extraction_watermark = buf.extraction_watermark
        extraction_summary = buf.extraction_summary
        buf.extraction_watermark = len(buf.messages)

        import time as _time
        buf.begin_extraction()

        def _background_extract_write():
            start_time = _time.monotonic()
            try:
                write_result = write_api.commit_session(
                    messages=extraction_messages,
                    ctx=ctx,
                    confidence_threshold=0.5,
                    wait=True,
                    session_time=session_time,
                    session_summary=extraction_summary,
                    tool_stats_text=tool_stats_text,
                )
                elapsed_ms = int((_time.monotonic() - start_time) * 1000)
                logger.info(
                    "after_turn background extract done: run=%s session=%s "
                    "extracted=%d filtered=%d writes=%d skipped=%d failed=%d "
                    "watermark=%d->%d buffer_len_now=%d elapsed_ms=%d",
                    extraction_run_id,
                    session_id[:8],
                    write_result.get("candidates_extracted", 0),
                    write_result.get("candidates_filtered", 0),
                    write_result.get("writes_completed", 0),
                    write_result.get("writes_skipped", 0),
                    write_result.get("writes_failed", 0),
                    extraction_watermark,
                    buf.extraction_watermark,
                    len(buf.messages),
                    elapsed_ms,
                )
                if write_result.get("candidates_extracted", 0) == 0:
                    logger.warning(
                        "after_turn zero-candidate diagnostic: run=%s session=%s "
                        "extractable=%d extraction_diag=%s write_result_keys=%s",
                        extraction_run_id,
                        session_id[:8],
                        len(extraction_messages),
                        _message_diag(extraction_messages),
                        sorted(write_result.keys()),
                    )

                if write_result.get("candidates_extracted", 0) > 0:
                    buf.extraction_summary = _update_extraction_summary(
                        buf.extraction_summary, write_result
                    )

                # Async drain: embed → upsert to vector index
                try:
                    self._async_drain()
                except Exception as exc:
                    logger.warning("outbox_drain failed after background extract: %s", exc)

                # Commit session archive
                commit_result = mgr.commit_snapshot(session_id, archive_snapshot, ctx, wait=True)
                removed = 0
                if commit_result.get("archived"):
                    removed = buf.remove_messages_by_id(archive_snapshot_ids)
                logger.info(
                    "after_turn background snapshot commit done: run=%s session=%s "
                    "archived=%s reason=%s removed=%d buffer_len=%d watermark=%d",
                    extraction_run_id,
                    session_id[:8],
                    commit_result.get("archived"),
                    commit_result.get("reason", ""),
                    removed,
                    len(buf.messages),
                    buf.extraction_watermark,
                )
            except Exception as exc:
                rewound = buf.rewind_watermark_to_ids(archive_snapshot_ids)
                logger.error(
                    "after_turn background extract failed for run=%s session=%s "
                    "rewound=%s: %s",
                    extraction_run_id, session_id[:8], rewound, exc, exc_info=True,
                )
            finally:
                buf.end_extraction()

        try:
            t = threading.Thread(
                target=_background_extract_write, daemon=True,
                name=f"extract-{session_id[:8]}",
            )
            t.start()
        except Exception as exc:
            # Thread spawn failed — fall back to synchronous (better than losing data)
            buf.end_extraction()
            logger.error(
                "Failed to spawn background extract thread: run=%s session=%s %s",
                extraction_run_id,
                session_id[:8],
                exc,
                exc_info=True,
            )
            write_result = write_api.commit_session(
                messages=extraction_messages, ctx=ctx,
                confidence_threshold=0.5, wait=True,
                session_time=session_time,
                session_summary=extraction_summary,
                tool_stats_text=tool_stats_text,
            )
            buf.extraction_watermark = len(buf.messages)
            if write_result.get("candidates_extracted", 0) > 0:
                buf.extraction_summary = _update_extraction_summary(
                    buf.extraction_summary, write_result
                )
            try:
                self._async_drain()
            except Exception:
                pass
            commit_result = mgr.commit_snapshot(session_id, archive_snapshot, ctx, wait=True)
            removed = 0
            if commit_result.get("archived"):
                removed = buf.remove_messages_by_id(archive_snapshot_ids)
            logger.info(
                "after_turn sync snapshot commit done: run=%s session=%s "
                "archived=%s removed=%d reason=%s",
                extraction_run_id,
                session_id[:8],
                commit_result.get("archived"),
                removed,
                commit_result.get("reason", ""),
            )
            return {
                "ok": True,
                "status": "completed",
                "extraction_run_id": extraction_run_id,
                **write_result,
            }

        return {"ok": True, "status": "processing", "extraction_run_id": extraction_run_id}

    def ingest(self, params: dict) -> dict:
        """Lightweight ack — OpenClaw calls this every turn, but actual write
        logic lives in after_turn().  Intentionally a no-op to avoid
        double-processing since OpenClaw also calls afterTurn().
        """
        return {"ingested": True}

    def ingest_batch(self, params: dict) -> dict:
        return {"ingested": True, "count": len(params.get("messages", []))}

    def compact(self, params: dict) -> dict:
        """Compact session: commit synchronously, return compressed context."""
        prepare_token = params.get("prepareToken")
        prepared_flag = bool(params.get("prepared"))
        if prepare_token:
            prepared = self._consume_prepared_compaction_state(params, str(prepare_token))
        elif prepared_flag:
            raise ValueError("prepare token required when prepared=true")
        else:
            prepared = self.prepare_compaction(params)
        session_id = prepared["session_id"]
        ctx = prepared["ctx"]
        token_budget = params.get("tokenBudget", 128_000)
        mgr = prepared["mgr"]

        # Estimate tokensBefore
        session = mgr.get_session(session_id, ctx)
        tokens_before = session.get("pending_tokens", 0)

        # Commit synchronously (wait=true)
        commit_result = mgr.commit(session_id, ctx, wait=True)

        if not commit_result.get("archived"):
            return {
                "ok": True,
                "compacted": False,
                "reason": commit_result.get("reason", "no_archive"),
                "result": {
                    "summary": "",
                    "tokensBefore": tokens_before,
                },
            }

        # Get compressed context (archive overview as summary)
        context = mgr.get_context(session_id, token_budget, ctx)
        summary = _trim_summary(
            context.get("latest_archive_overview", ""),
            _summary_max_chars(self._cfg, params),
        )
        tokens_after = context.get("estimatedTokens", 0)
        self._apply_compact_short_term_index_mode(params)

        result = {
            "ok": True,
            "compacted": True,
            "result": {
                "summary": summary,
                "tokensBefore": tokens_before,
                "tokensAfter": tokens_after,
            },
        }
        first_kept_entry_id = (
            commit_result.get("firstKeptEntryId")
            or commit_result.get("first_kept_entry_id")
        )
        if first_kept_entry_id:
            result["result"]["firstKeptEntryId"] = first_kept_entry_id
        return result

    def _apply_compact_short_term_index_mode(self, params: dict) -> None:
        """Apply Phase 1 short-term index semantics without blocking compact success."""
        mode = _short_term_index_mode(params)
        if mode == "off":
            logger.info("compact short-term index update skipped: mode=off")
            return
        if mode == "async":
            logger.info("compact short-term index update deferred: mode=async")
            return

        try:
            drain_stats = self.drain_outbox_sync()
            logger.info(
                "compact short-term index sync processed=%d succeeded=%d failed=%d",
                drain_stats.get("processed", 0),
                drain_stats.get("succeeded", 0),
                drain_stats.get("failed", 0),
            )
        except Exception as exc:
            logger.warning("compact short-term index sync skipped: %s", exc)

    def _build_compaction_state(self, params: dict) -> dict:
        """Build transport-agnostic compact state without mutating extraction progress."""
        session_id = params.get("sessionId", "unknown")
        ctx = params.get("_ctx") or self.build_context(params)
        mgr = self.get_session_manager()
        buf = mgr.get_or_create(session_id)
        extraction_state = self._build_incremental_extraction_state(buf)

        result = {
            "session_id": session_id,
            "ctx": ctx,
            "mgr": mgr,
            "buffer": buf,
            **extraction_state,
        }

        return result

    def _build_incremental_extraction_state(self, buf) -> dict:
        incremental = buf.messages[buf.extraction_watermark:]
        extraction_messages = [
            {"role": message.role, "content": message.content}
            for message in incremental
            if message.role != "assistant"
        ]
        from extraction.tool_collector import format_tool_stats_text

        session_time = None
        time_source_messages = [
            message for message in incremental
            if message.role != "assistant"
        ] or incremental or buf.messages
        for message in time_source_messages:
            session_time = _parse_session_time(getattr(message, "created_at", None))
            if session_time is not None:
                break

        return {
            "incremental": incremental,
            "messages": extraction_messages,
            "session_time": session_time,
            "tool_stats_text": format_tool_stats_text(buf.tool_usage_stats),
        }

    def _consume_prepared_compaction_state(self, params: dict, prepare_token: str) -> dict:
        result = self._build_compaction_state(params)
        if not result["mgr"].consume_compaction_prepare_token(result["session_id"], prepare_token):
            raise ValueError("invalid prepare token")
        result["prepareToken"] = prepare_token
        return result

    def prepare_compaction(self, params: dict) -> dict:
        """Prepare incremental extraction state before compact commit."""
        result = self._build_compaction_state(params)
        buf = result["buffer"]
        incremental = result["incremental"]

        if not incremental:
            result["prepareToken"] = result["mgr"].issue_compaction_prepare_token(result["session_id"])
            return result

        if not result["messages"]:
            buf.extraction_watermark = len(buf.messages)
            result["prepareToken"] = result["mgr"].issue_compaction_prepare_token(result["session_id"])
            return result

        write_api = self.get_write_api()
        if write_api is None:
            raise RuntimeError("write API unavailable for compaction")

        write_result = write_api.commit_session(
            messages=result["messages"],
            ctx=result["ctx"],
            confidence_threshold=0.5,
            wait=True,
            session_time=result["session_time"],
            session_summary=buf.extraction_summary,
            tool_stats_text=result["tool_stats_text"],
        )
        buf.extraction_watermark = len(buf.messages)
        if write_result.get("candidates_extracted", 0) > 0:
            buf.extraction_summary = _update_extraction_summary(
                buf.extraction_summary,
                write_result,
            )
        result["prepareToken"] = result["mgr"].issue_compaction_prepare_token(result["session_id"])
        result["write_result"] = write_result
        return result

    def bootstrap(self, params: dict) -> dict:
        return {"bootstrapped": True}

    def _ensure_workspace_index_ready_for_search(
        self,
        workspace_root: Path,
        params: dict,
        ctx: RequestContext,
    ) -> dict:
        """Ensure workspace code has indexed vectors before code search."""
        key = (ctx.account_id, str(workspace_root))
        want_full = bool(params.get("waitForFullWorkspaceIndex", False))
        timeout_raw = params.get("waitForFullWorkspaceIndexTimeoutSec")
        if timeout_raw is None:
            timeout_raw = os.environ.get("CONTEXTSNIPER_SEARCH_BOOTSTRAP_WAIT_TIMEOUT_SEC", "180")
        try:
            timeout_sec = max(1, int(timeout_raw))
        except (TypeError, ValueError):
            timeout_sec = 180
        deadline = time.time() + timeout_sec
        with self._search_bootstrap_lock:
            already_bootstrapped = key in self._search_bootstrapped_workspaces
            new_paths: list[str] = []
            rounds = 0
            full_index_completed = True
            full_index_cap_files = _bootstrap_full_index_cap_files()
            already: set[str] = set()
            if self.get_write_api():
                while True:
                    if time.time() >= deadline:
                        full_index_completed = False
                        break
                    if want_full and full_index_cap_files is not None and len(already) >= full_index_cap_files:
                        full_index_completed = False
                        break
                    rounds += 1
                    cap_remaining = None
                    if want_full and full_index_cap_files is not None:
                        cap_remaining = max(0, full_index_cap_files - len(already))
                    batch = self._ingest_bootstrap_file_batch(
                        workspace_root,
                        params,
                        ctx,
                        already=already,
                        query=str(params.get("query") or ""),
                        max_new_paths=(
                            cap_remaining
                            if want_full and full_index_cap_files is not None
                            else (None if want_full else _bootstrap_max_files())
                        ),
                    )
                    if batch:
                        new_paths.extend(batch)
                    if not want_full:
                        break
                    if not batch:
                        break
            if not already_bootstrapped:
                self._search_bootstrapped_workspaces.add(key)
            if want_full:
                if self._outbox_thread is not None and self._outbox_thread.is_alive():
                    drain = self._poll_outbox_until_quiet(
                        account_id=ctx.account_id,
                        deadline=deadline,
                    )
                else:
                    drain = self._drain_outbox_until_quiet(
                        account_id=ctx.account_id,
                        deadline=deadline,
                    )
                if drain.get("timed_out"):
                    full_index_completed = False
            else:
                drain = {"skipped_wait": True}
        return {
            "workspace_root": str(workspace_root),
            "already_bootstrapped": already_bootstrapped,
            "ingested_paths": new_paths,
            "ingested_count": len(new_paths),
            "drain": drain,
            "full_index_requested": want_full,
            "full_index_completed": full_index_completed,
            "full_index_cap_files": full_index_cap_files,
            "ingest_rounds": rounds,
            "timeout_sec": timeout_sec,
        }

    def code_workspace_bootstrap(self, params: dict) -> dict:
        """Ingest an initial batch of workspace source files and drain outbox."""
        ctx = params.get("_ctx") or self.build_context(params)
        workspace_root = self._resolve_workspace_root(params)
        if workspace_root is None:
            return {"ok": False, "reason": "no_workspace"}
        if not self.get_write_api():
            return {"ok": False, "reason": "no_write_api"}
        already: set[str] = set()
        try:
            new_paths = self._ingest_bootstrap_file_batch(
                workspace_root, params, ctx, already=already
            )
        except Exception as exc:
            logger.warning("code_workspace_bootstrap ingest failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}
        try:
            if self._outbox_thread is not None and self._outbox_thread.is_alive():
                drain = {"skipped_wait": True}
            else:
                drain = self._drain_outbox_until_quiet(account_id=ctx.account_id) or {}
        except Exception as exc:
            logger.warning("code_workspace_bootstrap drain failed: %s", exc)
            drain = {"error": str(exc)}
        return {
            "ok": True,
            "ingested_paths": new_paths,
            "ingested_count": len(new_paths),
            "drain": drain,
        }

    def code_sync_workspace(self, params: dict) -> dict:
        """Sync code memory from a git-diff baseline for prompt-time freshness.

        The first sync compares an empty snapshot to the current workspace, so
        every git-visible code file is treated as an addition. Later syncs run
        `git diff --no-index --name-status` between the last prompt snapshot
        and a fresh snapshot, then rebuild only changed code memories and delete
        memories for removed files.
        """
        if not _code_sync_enabled() and not self._param_bool(params.get("force"), False):
            return {"ok": True, "skipped": True, "reason": "disabled"}

        ctx = params.get("_ctx") or self.build_context(params)
        workspace_root = self._resolve_workspace_root(params)
        if workspace_root is None:
            return {"ok": False, "error": "no_workspace"}
        workspace_root = workspace_root.resolve()

        if not self.get_write_api():
            return {"ok": False, "error": "write_api_unavailable"}

        max_raw = params.get("max_files", params.get("maxFiles"))
        try:
            max_files = int(max_raw) if max_raw is not None else _code_sync_max_files()
        except (TypeError, ValueError):
            max_files = _code_sync_max_files()
        max_files = max(1, min(max_files, 5000))

        wait_for_index = self._param_bool(
            params.get("wait_for_index", params.get("waitForIndex")),
            _code_sync_wait_for_index_default(),
        )
        force = self._param_bool(params.get("force"), False)

        with self._code_sync_lock:
            state_dir = self._code_sync_state_dir(workspace_root, ctx)
            snapshot_dir = state_dir / "snapshot"
            current_dir = state_dir / "current"
            empty_dir = state_dir / "empty"
            meta_path = state_dir / "meta.json"
            meta = self._load_code_sync_meta(meta_path)
            first_sync = force or not bool(meta.get("initialized")) or not snapshot_dir.exists()

            current_paths, listing = self._current_code_sync_files(workspace_root, max_files=max_files)
            copied = self._write_code_sync_snapshot(
                workspace_root=workspace_root,
                snapshot_dir=current_dir,
                relative_paths=current_paths,
            )

            if first_sync:
                if empty_dir.exists():
                    shutil.rmtree(empty_dir)
                empty_dir.mkdir(parents=True, exist_ok=True)
                old_dir = empty_dir
            else:
                old_dir = snapshot_dir

            diff = self._git_diff_code_sync_snapshots(old_dir=old_dir, new_dir=current_dir)
            if not diff.get("ok"):
                try:
                    if current_dir.exists():
                        shutil.rmtree(current_dir)
                except OSError:
                    pass
                return {
                    "ok": False,
                    "error": diff.get("error", "git_diff_failed"),
                    "workspace_root": str(workspace_root),
                    "listing": listing,
                    "diff": diff,
                }

            changed_paths = diff.get("changed", []) or []
            deleted_paths = [] if first_sync else (diff.get("deleted", []) or [])
            if first_sync and not changed_paths:
                changed_paths = list(current_paths)

            apply_out = self._apply_code_sync_changes(
                workspace_root=workspace_root,
                params=params,
                ctx=ctx,
                changed_paths=changed_paths,
                deleted_paths=deleted_paths,
                wait_for_index=wait_for_index,
            )
            if not apply_out.get("ok"):
                try:
                    if current_dir.exists():
                        shutil.rmtree(current_dir)
                except OSError:
                    pass
                return {
                    **apply_out,
                    "workspace_root": str(workspace_root),
                    "mode": "initial_full" if first_sync else "incremental_diff",
                    "listing": listing,
                    "diff": diff,
                }

            if snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            current_dir.rename(snapshot_dir)
            now = _dt.utcnow().isoformat() + "Z"
            self._write_code_sync_meta(
                meta_path,
                {
                    "initialized": True,
                    "workspace_root": str(workspace_root),
                    "account_id": ctx.account_id,
                    "user_id": ctx.user_id,
                    "agent_id": ctx.agent_id,
                    "last_synced_at": now,
                    "last_mode": "initial_full" if first_sync else "incremental_diff",
                    "last_changed_count": len(changed_paths),
                    "last_deleted_count": len(deleted_paths),
                    "tracked_file_count": copied,
                    "listing": listing,
                },
            )

            return {
                "ok": True,
                "workspace_root": str(workspace_root),
                "mode": "initial_full" if first_sync else "incremental_diff",
                "first_sync": first_sync,
                "changed_paths": changed_paths[:50],
                "deleted_paths": deleted_paths[:50],
                "changed_count": len(changed_paths),
                "deleted_count": len(deleted_paths),
                "snapshot_file_count": copied,
                "listing": listing,
                "diff": {
                    "ok": diff.get("ok"),
                    "raw_line_count": diff.get("raw_line_count", 0),
                    "returncode": diff.get("returncode", 0),
                },
                "apply": apply_out,
                "wait_for_index": wait_for_index,
                "state_dir": str(state_dir),
            }

    def code_semantic_search(self, params: dict) -> dict:
        """Code-mode semantic search over grep/glob-selected candidates."""
        ctx = params.get("_ctx") or self.build_context(params)
        query = str(params.get("query") or "").strip()
        if not query:
            return {"ok": False, "reason": "missing_query"}
        tk = params.get("limit") if params.get("limit") is not None else params.get("top_k", 5)
        try:
            top_k = int(tk)
        except (TypeError, ValueError):
            top_k = 5
        forced_top_k = _forced_code_search_limit()
        if forced_top_k is not None:
            top_k = forced_top_k
        workspace_root = self._resolve_workspace_root(params)
        bootstrap: dict = {}
        candidate_paths: list[str] = []
        candidate_limit = _code_search_candidate_max_files()
        embed_limit = min(candidate_limit, _code_search_embed_max_files())
        max_snippets = _code_search_max_snippets()
        glob_patterns = self._parse_hint_list(params.get("glob_patterns") or params.get("globPatterns"))
        grep_terms = self._parse_hint_list(params.get("grep_terms") or params.get("grepTerms"))
        timings: dict[str, float] = {}
        if workspace_root is not None:
            t0 = time.perf_counter()
            candidate_paths_all = self._collect_code_search_candidates(
                workspace_root,
                query=query,
                glob_patterns=glob_patterns,
                grep_terms=grep_terms,
                limit=candidate_limit,
            )
            timings["collect_candidates_sec"] = round(time.perf_counter() - t0, 3)
            t1 = time.perf_counter()
            candidate_paths = self._rank_code_search_candidates(
                workspace_root,
                candidate_paths=candidate_paths_all,
                query=query,
                grep_terms=grep_terms,
                glob_patterns=glob_patterns,
                limit=embed_limit,
            )
            timings["rank_candidates_sec"] = round(time.perf_counter() - t1, 3)
            bootstrap = {
                "workspace_root": str(workspace_root),
                "candidate_paths": candidate_paths,
                "candidate_count": len(candidate_paths),
                "candidate_paths_all": candidate_paths_all,
                "candidate_count_all": len(candidate_paths_all),
                "glob_patterns": glob_patterns,
                "grep_terms": grep_terms or _extract_code_query_terms(query),
                "ingested_paths": [],
                "ingested_count": 0,
                "mode": "grep_glob_candidates",
                "embed_candidate_limit": embed_limit,
                "max_snippets": max_snippets,
                "timings": timings,
            }
            try:
                topn_each = int(os.environ.get("CONTEXTSNIPER_CODE_RETRIEVE_EACH", "5"))
            except ValueError:
                topn_each = 5
            topn_each = max(1, min(topn_each, 5))
            snippets = self._build_candidate_snippets(workspace_root, candidate_paths, ctx=ctx)
            snippets = self._cap_code_search_snippets(snippets, query=query, max_snippets=max_snippets)
            bootstrap["snippet_count"] = len(snippets)
            retrieval_enabled = {
                "semantic": _code_retrieval_enabled("semantic"),
                "frequency": _code_retrieval_enabled("frequency"),
                "symbolic": _code_retrieval_enabled("symbolic"),
                "graph": _code_retrieval_enabled("graph"),
            }
            bootstrap["retrieval_enabled"] = retrieval_enabled
            if _code_search_ingest_candidates_enabled():
                bootstrap["candidate_ingest_enabled"] = True
                if _code_search_ingest_async_enabled():
                    timings["candidate_ingest_sec"] = 0.0
                    bootstrap["candidate_ingest_async"] = True
                    bootstrap["candidate_ingest_dispatch"] = self._ingest_candidate_paths_background(
                        workspace_root,
                        params,
                        ctx,
                        candidate_paths=candidate_paths,
                    )
                else:
                    t_ingest = time.perf_counter()
                    ingested = self._ingest_candidate_paths(
                        workspace_root,
                        params,
                        ctx,
                        candidate_paths=candidate_paths,
                    )
                    timings["candidate_ingest_sec"] = round(time.perf_counter() - t_ingest, 3)
                    bootstrap["ingested_paths"] = ingested
                    bootstrap["ingested_count"] = len(ingested)
                    bootstrap["candidate_ingest_async"] = False
            else:
                timings["candidate_ingest_sec"] = 0.0
                bootstrap["candidate_ingest_enabled"] = False
            if retrieval_enabled["semantic"]:
                t2 = time.perf_counter()
                embed_score_hits = self._embed_rank_candidate_hits(
                    workspace_root,
                    candidate_paths=candidate_paths,
                    query=self._code_embedding_query(query, grep_terms=grep_terms, glob_patterns=glob_patterns),
                    limit=len(snippets),
                    snippets=snippets,
                )
                timings["embedding_sec"] = round(time.perf_counter() - t2, 3)
            else:
                embed_score_hits = []
                timings["embedding_sec"] = 0.0
            embed_hits = embed_score_hits[:topn_each]
            if retrieval_enabled["frequency"]:
                t_bm25 = time.perf_counter()
                bm25_score_hits = self._bm25_rank_candidate_hits(
                    snippets,
                    query=query,
                    limit=len(snippets),
                )
                timings["bm25_sec"] = round(time.perf_counter() - t_bm25, 3)
            else:
                bm25_score_hits = []
                timings["bm25_sec"] = 0.0
            bm25_hits = bm25_score_hits[:topn_each]
            if retrieval_enabled["symbolic"]:
                t_ctags = time.perf_counter()
                ctags_score_hits = self._ctags_rank_candidate_hits(
                    snippets,
                    query=query,
                    limit=len(snippets),
                )
                timings["ctags_sec"] = round(time.perf_counter() - t_ctags, 3)
            else:
                ctags_score_hits = []
                timings["ctags_sec"] = 0.0
            ctags_hits = ctags_score_hits[:topn_each]
            if retrieval_enabled["graph"]:
                t_graph = time.perf_counter()
                graph_score_hits = self._graph_rank_candidate_hits(
                    snippets,
                    query=query,
                    limit=len(snippets),
                )
                timings["graph_sec"] = round(time.perf_counter() - t_graph, 3)
            else:
                graph_score_hits = []
                timings["graph_sec"] = 0.0
            graph_hits = graph_score_hits[:topn_each]
            t_fuse = time.perf_counter()
            fused_hits = self._fuse_code_hits(
                embedding_hits=embed_hits,
                bm25_hits=bm25_hits,
                ctags_hits=ctags_hits,
                graph_hits=graph_hits,
                embedding_score_hits=embed_score_hits,
                bm25_score_hits=bm25_score_hits,
                ctags_score_hits=ctags_score_hits,
                graph_score_hits=graph_score_hits,
                limit=top_k,
            )
            timings["hybrid_fuse_sec"] = round(time.perf_counter() - t_fuse, 3)
            if len(fused_hits) < top_k:
                seen_uris = {str(hit.get("uri") or "") for hit in fused_hits if hit.get("uri")}
                route_fillers: list[dict] = []
                for route, route_hits in (
                    ("embedding", embed_score_hits),
                    ("bm25", bm25_score_hits),
                    ("ctags", ctags_score_hits),
                    ("graph", graph_score_hits),
                ):
                    for hit in route_hits:
                        uri = str(hit.get("uri") or "")
                        if not uri or uri in seen_uris:
                            continue
                        if not (hit.get("content_excerpt") or hit.get("abstract")):
                            continue
                        filler = dict(hit)
                        filler["retrieval_source"] = "hybrid"
                        filler["score_kind"] = "route_fill_after_weighted_rrf"
                        filler["route_ranks"] = {route: len(route_fillers) + 1}
                        seen_uris.add(uri)
                        route_fillers.append(filler)
                        if len(fused_hits) + len(route_fillers) >= top_k:
                            break
                    if len(fused_hits) + len(route_fillers) >= top_k:
                        break
                fused_hits.extend(route_fillers)
            logger.info(
                "code_semantic_search hybrid path: query=%r candidates=%d enabled=%s embed=%d bm25=%d ctags=%d graph=%d fused=%d timings=%s",
                query,
                len(candidate_paths),
                retrieval_enabled,
                len(embed_hits),
                len(bm25_hits),
                len(ctags_hits),
                len(graph_hits),
                len(fused_hits),
                timings,
            )
            if fused_hits:
                union_uris: set[str] = set()
                for group in (embed_hits, bm25_hits, ctags_hits, graph_hits):
                    for item in group:
                        uri = str(item.get("uri") or "")
                        if uri:
                            union_uris.add(uri)
                return {
                    "ok": True,
                    "request_id": f"hybrid-{uuid4()}",
                    "query": query,
                    "hits": fused_hits,
                    "hit_count": len(fused_hits),
                    "bootstrap": {
                        **bootstrap,
                        "fallback": "hybrid_direct",
                        "hybrid_counts": {
                            "semantic": len(embed_hits),
                            "frequency": len(bm25_hits),
                            "symbolic": len(ctags_hits),
                            "embedding": len(embed_hits),
                            "bm25": len(bm25_hits),
                            "ctags": len(ctags_hits),
                            "graph": len(graph_hits),
                            "union_candidates": len(union_uris),
                        },
                    },
                }
            if not any(retrieval_enabled.values()):
                return {
                    "ok": True,
                    "request_id": f"hybrid-{uuid4()}",
                    "query": query,
                    "hits": [],
                    "hit_count": 0,
                    "bootstrap": {
                        **bootstrap,
                        "fallback": "hybrid_direct",
                        "hybrid_counts": {
                            "semantic": 0,
                            "frequency": 0,
                            "symbolic": 0,
                            "embedding": 0,
                            "bm25": 0,
                            "ctags": 0,
                            "graph": 0,
                            "union_candidates": 0,
                        },
                    },
                }
            if not retrieval_enabled["semantic"]:
                return {
                    "ok": True,
                    "request_id": f"hybrid-{uuid4()}",
                    "query": query,
                    "hits": [],
                    "hit_count": 0,
                    "bootstrap": {
                        **bootstrap,
                        "fallback": "hybrid_direct",
                        "hybrid_counts": {
                            "semantic": 0,
                            "frequency": len(bm25_hits),
                            "symbolic": len(ctags_hits),
                            "embedding": 0,
                            "bm25": len(bm25_hits),
                            "ctags": len(ctags_hits),
                            "graph": len(graph_hits),
                            "union_candidates": 0,
                        },
                    },
                }
        api = self.get_read_api()
        if api is None:
            return {"ok": False, "reason": "no_read_api"}
        max_k = api._cfg.max_top_k
        top_k = max(1, min(top_k, max_k))
        fill_raw = params.get("fill_content_for_top_k")
        try:
            fill_k = int(fill_raw) if fill_raw is not None else min(5, top_k)
        except (TypeError, ValueError):
            fill_k = min(5, top_k)
        categories = params.get("categories")
        if categories is not None and not isinstance(categories, list):
            categories = None
        if categories is None:
            categories = ["code"]
        target_uri = params.get("target_uri")
        if target_uri is not None:
            target_uri = str(target_uri).strip() or None
        try:
            result = api.search_memory(
                query=query,
                ctx=ctx,
                top_k=max(top_k, min(20, max_k)),
                categories=categories,
                target_uri=target_uri,
                mode="QUICK",
                fill_content_for_top_k=fill_k,
            )
        except Exception as exc:
            logger.warning("code_semantic_search failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}
        filtered_hits = self._filter_hits_to_candidate_paths(
            workspace_root,
            candidate_paths=candidate_paths,
            hits=result.hits or [],
        ) if workspace_root is not None else (result.hits or [])
        if workspace_root is not None and candidate_paths and not filtered_hits:
            rg_hits = self._rg_semantic_fallback_hits(
                workspace_root,
                query=query,
                limit=max(1, min(top_k, 5)),
            )
            return {
                "ok": True,
                "request_id": f"rg-{uuid4()}",
                "query": query,
                "hits": rg_hits,
                "hit_count": len(rg_hits),
                "bootstrap": {
                    **bootstrap,
                    "fallback": "ripgrep",
                },
            }
        hits_out: list[dict] = []
        for h in filtered_hits[:top_k]:
            hits_out.append(
                {
                    "uri": h.uri,
                    "level_hit": getattr(h, "level_hit", "L2"),
                    "score": h.score,
                    "category": h.category or "",
                    "abstract": h.abstract or "",
                    "overview": (h.overview or "")[:2000] if h.overview else "",
                    "content_excerpt": (h.content_excerpt or "")[:4000] if h.content_excerpt else "",
                }
            )
        return {
            "ok": True,
            "request_id": result.request_id,
            "query": result.query,
            "hits": hits_out,
            "hit_count": len(hits_out),
            "bootstrap": bootstrap,
        }

    def code_read_snippet_for_hook(self, params: dict) -> dict:
        """Vector search for excerpts tied to a Read tool path."""
        ctx = params.get("_ctx") or self.build_context(params)
        workspace_root = self._resolve_workspace_root(params)
        raw = (params.get("file_path") or params.get("filePath") or "").strip()
        if not raw or workspace_root is None:
            return {"hit": False, "reason": "missing_path_or_workspace"}
        path = Path(raw)
        if not path.is_absolute():
            path = (workspace_root / path).resolve()
        else:
            path = path.resolve()
        try:
            rel = str(path.relative_to(workspace_root.resolve()))
        except ValueError:
            return {"hit": False, "reason": "outside_workspace"}

        is_dir = path.is_dir()
        is_file = path.is_file()
        if not is_file and not is_dir:
            return {"hit": False, "reason": "not_a_file"}

        api = self.get_read_api()
        if api is None:
            return {"hit": False, "reason": "no_read_api"}

        q = rel.replace("\\", "/")
        intent_raw = params.get("readIntent") or params.get("read_intent") or params.get("query")
        intent = str(intent_raw).strip() if intent_raw is not None else ""

        if is_dir:
            default_q = (q if q and q not in (".", "") else (workspace_root.name or "workspace")).strip()
            search_query = intent or default_q
            needle = ""
        else:
            needle = Path(q).name
            search_query = build_path_anchor_query(q, intent=intent)

        anchor_query = search_query.strip()
        grep_ctx = str(params.get("grepContext") or params.get("grep_context") or "").strip()
        if grep_ctx:
            search_query = f"{grep_ctx} {search_query}".strip()
            if len(search_query) > 4000:
                search_query = search_query[:4000]

        tk_raw = params.get("top_k", params.get("topK", 40))
        try:
            top_k = int(tk_raw)
        except (TypeError, ValueError):
            top_k = 40
        top_k = max(1, min(top_k, 50))
        wide_k = min(50, top_k * 2) if (grep_ctx and is_file) else top_k

        def _hit_blob(hit) -> str:
            return "\n".join(
                x
                for x in (
                    hit.abstract or "",
                    hit.overview or "",
                    hit.content_excerpt or "",
                    hit.uri or "",
                )
                if x
            )

        def _filter_hits_for_path(raw_hits: list, qnorm: str, ndl: str) -> list:
            qf = qnorm.replace("\\", "/")
            nf = ndl.replace("\\", "/")
            path_slug = qf.replace("/", "_").replace(".", "_") if qf else ""
            base_slug = nf.replace(".", "_") if nf else ""
            out = []
            for hit in raw_hits or []:
                blob = _hit_blob(hit).replace("\\", "/")
                blob_slug = blob.replace("/", "_").replace(".", "_")
                if (
                    qf in blob
                    or nf in blob
                    or (path_slug and path_slug in blob_slug)
                    or (base_slug and base_slug in blob_slug)
                ):
                    out.append(hit)
            return out

        def _search_file_hits(query_text: str, k: int) -> list:
            result = api.search_memory(
                query=query_text,
                ctx=ctx,
                top_k=k,
                fill_content_for_top_k=15,
                mode="QUICK",
                score_threshold=0.0,
                categories=["code"],
            )
            return _filter_hits_for_path(result.hits, q, needle)

        k_initial = wide_k if (is_file and grep_ctx) else top_k
        try:
            result = api.search_memory(
                query=search_query,
                ctx=ctx,
                top_k=k_initial,
                fill_content_for_top_k=15,
                mode="QUICK",
                score_threshold=0.0,
                categories=["code"] if is_file else None,
            )
        except Exception as exc:
            logger.warning("code_read_snippet search failed: %s", exc)
            return {"hit": False, "reason": "search_failed", "query": search_query}

        if is_dir:
            hits = []
            for hit in result.hits or []:
                chunk = (hit.content_excerpt or hit.abstract or "").strip()
                if chunk:
                    hits.append(hit)
                if len(hits) >= 5:
                    break
        else:
            hits = _filter_hits_for_path(result.hits, q, needle)
            if not hits and grep_ctx and anchor_query != search_query:
                try:
                    hits = _search_file_hits(anchor_query, wide_k)
                except Exception as exc:
                    logger.warning("code_read_snippet anchor-query search failed: %s", exc)
            if not hits and self.get_write_api():
                project_id = str(
                    params.get("projectId")
                    or params.get("project_id")
                    or workspace_root.name
                )
                ing = self._ingest_workspace_code_path(
                    workspace_root=workspace_root,
                    relative_path=q,
                    ctx=ctx,
                    project_id=project_id,
                )
                if ing and ing.get("ok"):
                    try:
                        self.drain_outbox_sync(account_id=ctx.account_id)
                    except Exception as dre:
                        logger.warning("code_read_snippet ingest drain failed: %s", dre)
                    try:
                        hits = _search_file_hits(search_query, wide_k)
                        if not hits and grep_ctx and anchor_query != search_query:
                            hits = _search_file_hits(anchor_query, wide_k)
                    except Exception as exc:
                        logger.warning("code_read_snippet retry search failed: %s", exc)
            if not hits:
                return {
                    "hit": False,
                    "reason": "no_matching_chunks",
                    "query": search_query,
                    "relative_path": q,
                }

        if not hits and is_dir:
            guide = (
                f"`{q or '.'}` is a directory, not a file.\n\n"
                f"A memory search was run using query `{search_query!r}`; nothing relevant was retrieved yet. "
                "Use Glob or Read on a concrete file under it."
            )
            return {
                "hit": True,
                "file_path": str(path),
                "relative_path": q or ".",
                "snippet": guide,
                "intercept": "directory_read",
                "query": search_query,
                "top_k": top_k,
            }

        parts: list[str] = []
        total = 0
        max_chars = 10_000
        for hit in hits[:5]:
            chunk = (hit.content_excerpt or hit.abstract or "").strip()
            if not chunk:
                continue
            line = f"--- {hit.uri} (score={hit.score:.3f}) ---\n{chunk}"
            if total + len(line) > max_chars:
                break
            parts.append(line)
            total += len(line)
        text = "\n\n".join(parts).strip()
        if not text:
            return {
                "hit": False,
                "reason": "empty_excerpts",
                "query": search_query,
                "relative_path": q or ".",
            }
        out = {
            "hit": True,
            "file_path": str(path),
            "relative_path": q,
            "snippet": text,
            "query": search_query,
            "top_k": top_k,
        }
        if is_dir:
            out["intercept"] = "directory_read"
        return out

    def code_ingest_workspace_path(self, params: dict) -> dict:
        """Ingest a single workspace file into code memory."""
        ctx = params.get("_ctx") or self.build_context(params)
        workspace_root = self._resolve_workspace_root(params)
        if workspace_root is None:
            return {"ok": False, "error": "no_workspace"}
        raw = (params.get("file_path") or params.get("filePath") or "").strip()
        if not raw:
            return {"ok": False, "error": "missing_file_path"}
        p = Path(raw)
        if not p.is_absolute():
            p = (workspace_root / p).resolve()
        try:
            rel = str(p.relative_to(workspace_root.resolve()))
        except ValueError:
            return {"ok": False, "error": "outside_workspace"}
        project_id = (
            params.get("projectId")
            or params.get("project_id")
            or workspace_root.name
        )

        edit_search: dict = {"queried": False, "path_filtered_hits": 0, "uris": []}
        old_s = (params.get("old_string") or params.get("oldString") or "").strip()
        read_api = self.get_read_api()
        if old_s and read_api is not None:
            edit_search["queried"] = True
            rel_norm = rel.replace("\\", "/")
            base_name = Path(rel_norm).name
            try:
                result = read_api.search_memory(
                    query=old_s[:2000],
                    ctx=ctx,
                    top_k=25,
                    categories=["code"],
                    mode="QUICK",
                    fill_content_for_top_k=8,
                )
                for hit in result.hits or []:
                    blob = " ".join(
                        x
                        for x in (
                            hit.abstract or "",
                            hit.overview or "",
                            hit.content_excerpt or "",
                        )
                        if x
                    )
                    blob_norm = blob.replace("\\", "/")
                    if rel_norm in blob_norm or base_name in blob_norm:
                        edit_search["path_filtered_hits"] += 1
                        if len(edit_search["uris"]) < 8:
                            edit_search["uris"].append(hit.uri)
            except Exception as exc:
                edit_search["error"] = str(exc)
                logger.warning("code_ingest_workspace_path edit_search failed: %s", exc)

        ingest_out = self._ingest_workspace_code_path(
            workspace_root=workspace_root,
            relative_path=rel,
            ctx=ctx,
            project_id=str(project_id),
        )
        if ingest_out is None:
            return {"ok": False, "error": "write_api_unavailable", "edit_search": edit_search}
        return {**ingest_out, "edit_search": edit_search}

    def write_natural_language(self, params: dict) -> dict:
        """Write prepared filtered natural-language memory entries."""
        ctx = params.get("_ctx") or self.build_context(params)
        memories = params.get("memories", [])
        if not memories:
            return {"ok": False, "error": "no memories provided"}

        write_api = self.get_write_api()
        if write_api is None:
            return {"ok": False, "error": "write_api_unavailable"}

        try:
            from filter.config import filter_enabled
            if not filter_enabled():
                raise ImportError("filter disabled")
            from filter import get_plugin
            plugin = get_plugin()
            for mem in memories:
                content = mem.get("content", "")
                if not content:
                    continue
                try:
                    max_lines = int(os.environ.get("CONTEXTSNIPER_FILTER_MEMORY_MAX_LINES") or "60")
                except ValueError:
                    max_lines = 60
                shortened, stats = plugin.short_adaptive(content, max_lines=max_lines)
                detected = stats.stage.removeprefix("adaptive_")
                mem["detected"] = detected
                mem["strategy"] = detected
                metadata = mem.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.update({
                    "filter_enabled": True,
                    "filter_stage": stats.stage,
                    "original_chars": stats.original_chars,
                    "shortened_chars": stats.shortened_chars,
                    "tokens_saved_estimate": stats.tokens_saved_estimate,
                    "shortening_ratio": stats.shortening_ratio,
                })
                mem["metadata"] = metadata
                if stats.shortening_ratio < 0.95:
                    # Default natural-language writes keep L2 raw and use
                    # filtered text for missing L0/L1 fields.
                    mem.setdefault("abstract", shortened[:200])
                    mem.setdefault("overview", shortened)
                else:
                    mem.setdefault("abstract", content[:200])
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("write_natural_language filter shortening failed: %s", exc)

        try:
            results = write_api.write_natural_language(memories, ctx)
            return {"ok": True, "writes": results}
        except Exception as exc:
            logger.error("write_natural_language failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}

    def filter_read(self, params: dict) -> dict:
        """Filter a native Read target, store non-code levels, and return an L0 file.

        Level contract for non-code tool output:
        - L0 / .abstract.md: filtered content returned to the agent.
        - L1 / .overview.md: structured properties for retrieval/routing.
        - L2 / content.md: original unfiltered tool output.
        """
        ctx = params.get("_ctx") or self.build_context(params)
        workspace_root = self._resolve_workspace_root(params)
        raw = (params.get("file_path") or params.get("filePath") or "").strip()
        if not raw or workspace_root is None:
            return {"ok": False, "reason": "missing_path_or_workspace"}

        target = Path(raw).expanduser()
        target = target.resolve() if target.is_absolute() else (workspace_root / target).resolve()
        try:
            rel = target.relative_to(workspace_root.resolve()).as_posix()
        except ValueError:
            return {"ok": False, "reason": "outside_workspace"}
        if not target.is_file():
            return {"ok": False, "reason": "not_a_file"}

        try:
            min_chars = int(os.environ.get("CONTEXTSNIPER_FILTER_READ_MIN_CHARS") or "8000")
        except ValueError:
            min_chars = 8000
        original = target.read_text(encoding="utf-8", errors="replace")
        if len(original) < min_chars:
            return {"ok": False, "reason": "below_min_chars", "original_chars": len(original)}

        try:
            from filter.config import filter_enabled
            if not filter_enabled():
                return {"ok": False, "reason": "filter_disabled"}
            from filter import get_plugin
        except ImportError:
            return {"ok": False, "reason": "filter_unavailable"}

        try:
            max_lines = int(os.environ.get("CONTEXTSNIPER_FILTER_READ_MAX_LINES") or os.environ.get("CONTEXTSNIPER_FILTER_MEMORY_MAX_LINES") or "60")
        except ValueError:
            max_lines = 60
        plugin = get_plugin()
        filtered, stats = plugin.short_adaptive(original, max_lines=max_lines)
        if filtered == original:
            return {"ok": False, "reason": "unchanged", "original_chars": len(original)}

        detected = stats.stage.removeprefix("adaptive_")
        original_lines = len(original.splitlines())
        filtered_lines = len(filtered.splitlines())
        digest = hashlib.sha256(f"{target}\0{original}".encode("utf-8", errors="replace")).hexdigest()
        routing_key = f"tool_output_read_{digest[:16]}"
        properties = "\n".join([
            "source: pre_tool_use",
            "tool_name: Read",
            f"file_path: {target}",
            f"workspace_relative_path: {rel}",
            f"detected: {detected}",
            f"strategy: {detected}",
            f"filter_stage: {stats.stage}",
            f"original_chars: {stats.original_chars}",
            f"filtered_chars: {stats.shortened_chars}",
            f"original_lines: {original_lines}",
            f"filtered_lines: {filtered_lines}",
            f"tokens_saved_estimate: {stats.tokens_saved_estimate}",
        ])

        memory_result = self.write_natural_language({
            **params,
            "memories": [{
                "type": "l1",
                "role": "tool",
                "source": "pre_tool_use",
                "tool_name": "Read",
                "file_path": str(target),
                "category": "tool_outputs",
                "owner_scope": "agent",
                "routing_key": routing_key,
                "abstract": filtered,
                "overview": properties,
                "content": original,
                "detected": detected,
                "strategy": detected,
                "confidence": 0.8,
                "metadata": {
                    "filter_enabled": True,
                    "filter_stage": stats.stage,
                    "original_chars": stats.original_chars,
                    "shortened_chars": stats.shortened_chars,
                    "tokens_saved_estimate": stats.tokens_saved_estimate,
                    "shortening_ratio": stats.shortening_ratio,
                    "original_lines": original_lines,
                    "filtered_lines": filtered_lines,
                },
            }],
        })

        memory_uri = ""
        if isinstance(memory_result, dict):
            writes = memory_result.get("writes") or []
            if writes and isinstance(writes[0], dict):
                memory_uri = str(writes[0].get("target_uri") or "")

        banner = "\n".join([
            "### FILTER IS TRIGGERED",
            "This tool output was shortened before it reached you.",
            "If useful information appears missing, call `get_original_tool_output` with:",
            f"`memory_uri`: `{memory_uri}`",
            "",
        ])
        filtered_for_agent = banner + filtered.rstrip() + "\n"

        runtime_dir = Path(os.environ.get("CONTEXTSNIPER_RUNTIME_DIR") or ".").expanduser().resolve()
        out_dir = runtime_dir / "filtered-read"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{target.name}.{digest[:16]}.l0.txt"
        out_path.write_text(filtered_for_agent, encoding="utf-8")

        return {
            "ok": True,
            "filtered_file_path": str(out_path),
            "memory_uri": memory_uri,
            "detected": detected,
            "strategy": detected,
            "original_chars": stats.original_chars,
            "filtered_chars": len(filtered_for_agent),
            "original_lines": original_lines,
            "filtered_lines": len(filtered_for_agent.splitlines()),
        }

    def read_tool_output_original(self, params: dict) -> dict:
        """Return the L2 original for a filtered tool-output memory node."""
        ctx = params.get("_ctx") or self.build_context(params)
        memory_uri = str(
            params.get("memory_uri")
            or params.get("target_uri")
            or params.get("uri")
            or ""
        ).strip()
        if not memory_uri:
            return {"ok": False, "error": "memory_uri is required"}
        if "/memories/tool_outputs/" not in memory_uri:
            return {"ok": False, "error": "memory_uri is not a tool_outputs node"}

        read_api = self.get_read_api()
        if read_api is None:
            return {"ok": False, "error": "read_api_unavailable"}

        block = read_api.read_memory(memory_uri, ctx)
        original = block.content_excerpt or ""
        if block.category and block.category != "tool_outputs":
            return {"ok": False, "error": f"memory category is {block.category}, not tool_outputs"}
        if not original:
            return {"ok": False, "error": "original output not found", "memory_uri": memory_uri}

        try:
            max_chars = int(params.get("max_chars") or 0)
        except (TypeError, ValueError):
            max_chars = 0
        truncated = False
        returned = original
        if max_chars > 0 and len(original) > max_chars:
            head = max_chars // 2
            tail = max_chars - head
            omitted = len(original) - head - tail
            returned = (
                original[:head]
                + f"\n\n... [original output clipped: {omitted} chars omitted] ...\n\n"
                + original[-tail:]
            )
            truncated = True

        return {
            "ok": True,
            "memory_uri": memory_uri,
            "original": returned,
            "original_chars": len(original),
            "returned_chars": len(returned),
            "truncated": truncated,
        }

    @staticmethod
    def _classify_bash_output(command: str, output: str, pre_category: str) -> str:
        lower = (command or "").lower()
        if pre_category == "test_runner":
            return "test_runner"
        if pre_category == "diff":
            return "diff"
        if pre_category == "diff_maybe":
            if re.search(r"^diff --git a/", output, re.MULTILINE) or re.search(
                r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@", output, re.MULTILINE
            ):
                return "diff"
            return ""
        if pre_category == "python_maybe":
            if re.search(r"Traceback \(most recent call last\)|\b\w*(Error|Exception):", output):
                return "python_error"
            return ""
        if re.search(r"(^|[\s;&|()])git\s+diff\b", lower):
            return "diff"
        return ""

    @staticmethod
    def _shorten_bash_output(command: str, output: str, category: str) -> tuple[str, str]:
        if category == "test_runner":
            from filter.adapters.strategies.test_runner import TestRunnerStrategy

            result = TestRunnerStrategy().shorten(output, {
                "noise_lines": [
                    r"^plugins: ",
                    r"^collecting \.\.\.",
                    r"^cachedir:",
                    r"^rootdir:",
                    r"^platform ",
                    r"^Creating test database",
                    r"^Cloning test database",
                    r"^Destroying test database",
                    r"^Synchronizing apps without migrations",
                    r"^Operations to perform",
                    r"^\s+Apply all migrations",
                    r"^\s+Synchronize unmigrated apps",
                    r"^Importing application ",
                    r"^Skipping setup of unused database",
                ],
            })
            if result.text != output:
                return result.text, "bash_test_runner"
            from filter import get_plugin

            try:
                max_lines = int(os.environ.get("CONTEXTSNIPER_FILTER_BASH_MAX_LINES") or os.environ.get("CONTEXTSNIPER_FILTER_MEMORY_MAX_LINES") or "80")
            except ValueError:
                max_lines = 80
            plugin = get_plugin()
            filtered, stats = plugin.short_adaptive(output, max_lines=max_lines)
            return filtered, f"bash_test_runner_{stats.stage}"
        if category == "diff":
            from filter.adapters.strategies.diff_hunk import DiffHunkStrategy

            result = DiffHunkStrategy().shorten(output, {"context_window": 3})
            return result.text, "bash_diff"
        if category == "python_error":
            from filter import get_plugin

            try:
                max_lines = int(os.environ.get("CONTEXTSNIPER_FILTER_BASH_MAX_LINES") or os.environ.get("CONTEXTSNIPER_FILTER_MEMORY_MAX_LINES") or "80")
            except ValueError:
                max_lines = 80
            plugin = get_plugin()
            filtered, stats = plugin.short_adaptive(output, max_lines=max_lines)
            return filtered, stats.stage
        return output, "bash_unknown"

    def filter_bash(self, params: dict) -> dict:
        """Filter whitelisted native Bash output and store L0/L1/L2 tool output."""
        ctx = params.get("_ctx") or self.build_context(params)
        command = str(params.get("command") or "").strip()
        output = str(params.get("output") or "")
        pre_category = str(params.get("pre_category") or "").strip()
        if not command or not output:
            return {"ok": False, "reason": "missing_command_or_output"}

        try:
            from filter.config import filter_enabled
            if not filter_enabled():
                return {"ok": False, "reason": "filter_disabled"}
        except ImportError:
            return {"ok": False, "reason": "filter_unavailable"}

        category = self._classify_bash_output(command, output, pre_category)
        if category not in {"test_runner", "python_error", "diff"}:
            return {"ok": False, "reason": "not_whitelisted", "category": category or pre_category}

        try:
            min_chars = int(os.environ.get("CONTEXTSNIPER_FILTER_BASH_MIN_CHARS") or "8000")
        except ValueError:
            min_chars = 8000
        try:
            min_lines = int(os.environ.get("CONTEXTSNIPER_FILTER_BASH_MIN_LINES") or "100")
        except ValueError:
            min_lines = 100
        original_lines = len(output.splitlines())
        should_shorten = len(output) >= min_chars or original_lines >= min_lines
        if should_shorten:
            filtered, strategy = self._shorten_bash_output(command, output, category)
            if not filtered:
                filtered = output
                strategy = "bash_passthrough_empty_filter"
        else:
            filtered = output
            strategy = "bash_passthrough_below_min_size"

        filtered_lines = len(filtered.splitlines())
        was_shortened = filtered != output
        digest = hashlib.sha256(f"{command}\0{output}".encode("utf-8", errors="replace")).hexdigest()
        routing_key = f"tool_output_bash_{digest[:16]}"
        exit_code = params.get("exit_code")
        properties = "\n".join([
            "source: pre_tool_use",
            "tool_name: Bash",
            f"command: {command}",
            f"category: {category}",
            f"strategy: {strategy}",
            f"exit_code: {exit_code}",
            f"was_shortened: {was_shortened}",
            f"original_chars: {len(output)}",
            f"filtered_chars: {len(filtered)}",
            f"original_lines: {original_lines}",
            f"filtered_lines: {filtered_lines}",
            f"tokens_saved_estimate: {max(0, len(output) - len(filtered)) // 4}",
        ])
        memory_result = self.write_natural_language({
            **params,
            "memories": [{
                "type": "l1",
                "role": "tool",
                "source": "pre_tool_use",
                "tool_name": "Bash",
                "category": "tool_outputs",
                "owner_scope": "agent",
                "routing_key": routing_key,
                "abstract": filtered,
                "overview": properties,
                "content": output,
                "detected": category,
                "strategy": strategy,
                "confidence": 0.8,
                "metadata": {
                    "filter_enabled": True,
                    "filter_stage": strategy,
                    "command": command,
                    "bash_category": category,
                    "exit_code": exit_code,
                    "was_shortened": was_shortened,
                    "original_chars": len(output),
                    "shortened_chars": len(filtered),
                    "tokens_saved_estimate": max(0, len(output) - len(filtered)) // 4,
                    "shortening_ratio": len(filtered) / len(output) if output else 1.0,
                    "original_lines": original_lines,
                    "filtered_lines": filtered_lines,
                },
            }],
        })

        memory_uri = ""
        if isinstance(memory_result, dict):
            writes = memory_result.get("writes") or []
            if writes and isinstance(writes[0], dict):
                memory_uri = str(writes[0].get("target_uri") or "")

        banner = "\n".join([
            "### FILTER IS TRIGGERED",
            (
                "This Bash output was shortened before it reached you."
                if was_shortened
                else "This Bash output was captured before it reached you."
            ),
            "If useful information appears missing, call `get_original_tool_output` with:",
            f"`memory_uri`: `{memory_uri}`",
            f"`category`: `{category}`",
            "",
        ])
        filtered_output = banner + filtered.rstrip() + "\n"
        return {
            "ok": True,
            "filtered_output": filtered_output,
            "memory_uri": memory_uri,
            "category": category,
            "strategy": strategy,
            "was_shortened": was_shortened,
            "original_chars": len(output),
            "filtered_chars": len(filtered_output),
            "original_lines": original_lines,
            "filtered_lines": len(filtered_output.splitlines()),
        }

    def search_memory(self, params: dict) -> dict:
        """Search long-term natural-language memory for L0/L1 entries."""
        ctx = params.get("_ctx") or self.build_context(params)
        query = params.get("query", "")
        limit = min(int(params.get("limit", 10)), 50)
        categories_param = params.get("categories")

        if not query:
            return {"ok": False, "error": "query must not be empty"}

        read_api = self.get_read_api()
        if read_api is None:
            return {"ok": False, "error": "read_api_unavailable"}

        short_query = query
        try:
            from filter.config import filter_enabled
            if not filter_enabled():
                raise ImportError("filter disabled")
            from filter import get_plugin
            plugin = get_plugin()
            short_query, _ = plugin.short_query(query)
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("search_memory filter short_query failed: %s", exc)

        categories = None
        if categories_param:
            if isinstance(categories_param, list):
                categories = categories_param
            elif categories_param in ("l0", "l1", "l2", "non-code", "natural_language"):
                categories = ["natural_language"]

        try:
            result = read_api.search_memory(
                query=short_query,
                ctx=ctx,
                top_k=limit,
                categories=categories,
                mode="QUICK",
                fill_content_for_top_k=min(limit, 5),
            )
            hits = []
            for hit in (result.hits or []):
                hits.append({
                    "uri": hit.uri,
                    "abstract": hit.abstract or "",
                    "content": hit.content_excerpt or "",
                    "score": hit.score,
                    "category": hit.category,
                })
            try:
                from filter.config import filter_enabled
                if not filter_enabled():
                    raise ImportError("filter disabled")
                from filter import get_plugin
                plugin = get_plugin()
                hits = plugin.short_retrieved_blocks(hits, budget_chars=2000)
            except Exception:
                pass
            return {"ok": True, "hits": hits, "query": short_query}
        except Exception as exc:
            logger.error("search_memory failed: %s", exc, exc_info=True)
            return {"ok": False, "error": str(exc)}

    def prepare_subagent_spawn(self, params: dict) -> dict:
        return {"prepared": True}

    def on_subagent_ended(self, params: dict) -> dict:
        return {"cleaned": True}

    def dispose(self, params: dict | None = None) -> dict:
        """Session end: dispatch background flush for pending memories."""
        params = params or {}
        session_id = params.get("sessionId") or params.get("session_id") or ""
        if not session_id:
            return {"disposed": True, "flushed": False, "reason": "no_session_id"}

        mgr = self.get_session_manager()
        if not mgr.has_session(session_id):
            return {"disposed": True, "flushed": False, "reason": "session_not_found"}

        buf = mgr.get_or_create(session_id)

        # Wait for any in-progress background extraction to finish before
        # touching the session.  This prevents the race where after_turn()
        # has optimistically advanced extraction_watermark but the background
        # thread hasn't committed yet — removing the session here would cause
        # the background commit to fail and lose data.
        if buf.extraction_in_progress and buf.extraction_done_event is not None:
            logger.info("dispose: waiting for background extraction on session=%s", session_id[:8])
            completed = buf.extraction_done_event.wait(timeout=30)
            if not completed and buf.extraction_in_progress:
                logger.warning(
                    "dispose: background extraction still in progress after timeout; "
                    "keeping session=%s active_extractions=%d watermark=%d total=%d",
                    session_id[:8],
                    getattr(buf, "extraction_active_count", 0),
                    buf.extraction_watermark,
                    len(buf.messages),
                )
                return {
                    "disposed": True,
                    "flushed": False,
                    "reason": "extraction_in_progress_timeout",
                    "extraction_in_progress": True,
                    "active_extractions": getattr(buf, "extraction_active_count", 0),
                }

        unextracted = buf.messages[buf.extraction_watermark:]
        if not unextracted:
            mgr.remove_session(session_id)
            return {"disposed": True, "flushed": False, "reason": "no_pending_messages"}

        pending_tokens = sum(len(m.content) // 4 for m in unextracted)
        _MIN_FLUSH_TOKENS = 200
        if pending_tokens < _MIN_FLUSH_TOKENS:
            logger.info(
                "dispose flush skipped: session=%s pending_tokens=%d < %d",
                session_id[:8], pending_tokens, _MIN_FLUSH_TOKENS,
            )
            mgr.remove_session(session_id)
            return {"disposed": True, "flushed": False, "reason": "below_token_threshold",
                    "pending_tokens": pending_tokens}

        extraction_messages = [
            {"role": m.role, "content": m.content}
            for m in unextracted if m.role != "assistant"
        ]
        if not extraction_messages:
            mgr.remove_session(session_id)
            return {"disposed": True, "flushed": False, "reason": "no_user_messages"}

        # Check write_api BEFORE removing the session so that we can bail
        # out safely without losing pending data.
        write_api = self.get_write_api()
        if write_api is None:
            logger.warning("dispose: write API unavailable, keeping session for retry session=%s", session_id[:8])
            # Do NOT remove the session — a subsequent dispose or flush can retry.
            return {"disposed": True, "flushed": False, "reason": "no_write_api"}

        ctx = params.get("_ctx") or self.build_context(params)
        extraction_summary = buf.extraction_summary
        tool_stats_text = ""
        try:
            from extraction.tool_collector import format_tool_stats_text
            tool_stats_text = format_tool_stats_text(buf.tool_usage_stats)
        except Exception:
            pass

        buf.extraction_watermark = len(buf.messages)
        mgr.remove_session(session_id)

        logger.info(
            "dispose: dispatching background flush for session=%s msgs=%d tokens=%d",
            session_id[:8], len(extraction_messages), pending_tokens,
        )

        svc = self

        def _background_flush():
            try:
                write_result = write_api.commit_session(
                    messages=extraction_messages,
                    ctx=ctx,
                    confidence_threshold=0.5,
                    wait=True,
                    session_time=None,
                    session_summary=extraction_summary,
                    tool_stats_text=tool_stats_text,
                )
                logger.info(
                    "dispose background flush done: session=%s extracted=%d writes=%d",
                    session_id[:8],
                    write_result.get("candidates_extracted", 0),
                    write_result.get("writes_completed", 0),
                )
                try:
                    svc.drain_outbox_sync()
                except Exception:
                    pass
            except Exception:
                logger.exception("dispose background flush failed: session=%s", session_id[:8])

        import threading
        threading.Thread(target=_background_flush, daemon=True).start()
        return {"disposed": True, "flushed": True, "status": "background_flush_dispatched",
                "pending_tokens": pending_tokens}

    def get_cumulative_token_usage(self, reset: bool = False) -> dict:
        result: dict = {}
        llm = self._llm
        if llm and hasattr(llm, "token_tracker"):
            tracker = llm.token_tracker
            snap = tracker.snapshot_and_reset() if reset else tracker.snapshot()
            result["llm"] = {
                "input_tokens": snap.input_tokens,
                "output_tokens": snap.output_tokens,
                "cache_read": snap.cache_read,
                "cache_write": snap.cache_write,
                "total_tokens": snap.input_tokens + snap.output_tokens,
                "calls": snap.llm_calls,
            }
        if self._embedder and hasattr(self._embedder, "token_tracker"):
            tracker = self._embedder.token_tracker
            snap = tracker.snapshot_and_reset() if reset else tracker.snapshot()
            result["embedding"] = {"total_tokens": snap.embed_tokens, "calls": snap.embed_calls}
        if result:
            result["total_tokens"] = (
                result.get("llm", {}).get("total_tokens", 0)
                + result.get("embedding", {}).get("total_tokens", 0)
            )
        return result

    def health(self) -> dict:
        info: dict = {"backend": "contextsniper", "agfs": _HAS_AGFS}
        try:
            if _HAS_AGFS:
                AGFSClient(api_base_url=self._agfs_base_url).ls("/")
                info["agfs_url"] = self._agfs_base_url
            llm = self.get_llm()
            info["llm"] = getattr(llm, "model", "unknown")
            info["status"] = "ok"
        except Exception as exc:
            info["status"] = "error"
            info["error"] = str(exc)
        return info
