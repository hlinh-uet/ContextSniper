"""IndexRecordBuilder: Expand ContextNode into level-based IndexRecords.

Each ContextNode expands to up to 3 IndexRecords (L0/L1/L2) for layered retrieval.
CRITICAL: All IndexRecords MUST contain account_id + owner_space in filters.
This is the LAST layer of tenant isolation at the vector level.

Missing filters cause SILENT cross-tenant leakage (no runtime error).
"""

import os
import re
from typing import Any, Final

from core.models import ContextNode, IndexRecord
from retrieval.path_anchor import guess_repo_rel_from_file_path, prepend_code_location_header


# URI pattern to extract account_id: ctx://{account}/...
_URI_ACCOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^ctx://([^/]+)/"
)

# Required filter keys for tenant isolation
_REQUIRED_FILTER_KEYS: Final[tuple[str, ...]] = ("account_id", "owner_space")


class FilterValidationError(Exception):
    """Raised when IndexRecord filters fail validation."""

    def __init__(self, missing_keys: tuple[str, ...]) -> None:
        self.missing_keys = missing_keys
        super().__init__(
            f"IndexRecord.filters missing required keys: {missing_keys}. "
            "This causes SILENT cross-tenant leakage!"
        )


def _extract_account_id(uri: str) -> str:
    """Extract account_id from ContextNode URI.

    Args:
        uri: ContextNode URI like "ctx://acme/users/alice/memories/profile"

    Returns:
        Account ID (e.g., "acme")

    Raises:
        ValueError: If URI format is invalid
    """
    match = _URI_ACCOUNT_PATTERN.match(uri)
    if not match:
        raise ValueError(f"Invalid URI format, cannot extract account_id: {uri}")
    return match.group(1)


def _parse_enabled_index_levels() -> tuple[int, ...]:
    """Parse enabled index levels from `CONTEXTSNIPER_INDEX_LEVELS`."""
    raw = str(os.environ.get("CONTEXTSNIPER_INDEX_LEVELS", "") or "").strip()
    if not raw:
        return (0, 1, 2)

    levels: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            level = int(token)
        except ValueError:
            continue
        if level in (0, 1, 2) and level not in levels:
            levels.append(level)

    if not levels:
        return (0, 1, 2)
    return tuple(levels)


def _l1_code_index_metadata(node: ContextNode) -> dict[str, Any]:
    """Structured L1 code metadata for programmatic use."""
    if node.category != "code":
        return {}
    md = node.metadata or {}
    fp = str(md.get("file_path") or "").strip().replace("\\", "/")
    out: dict[str, Any] = {}
    if fp:
        out["l1_location"] = fp
        out["l1_source_file"] = fp
    rel = guess_repo_rel_from_file_path(fp) if fp else ""
    if rel:
        out["l1_repo_rel_path"] = rel
    sl, el = md.get("start_line"), md.get("end_line")
    if sl is not None and el is not None:
        try:
            sl_i, el_i = int(sl), int(el)
            out["l1_start_line"] = sl_i
            out["l1_end_line"] = el_i
            out["l1_lines"] = f"{sl_i}-{el_i}"
        except (TypeError, ValueError):
            pass
    agfs_uri = str(md.get("agfs_uri") or node.uri or "").strip()
    if agfs_uri:
        out["l1_agfs_uri"] = agfs_uri
    agfs_directory = str(md.get("agfs_directory") or "").strip()
    if agfs_directory:
        out["l1_agfs_directory"] = agfs_directory
    ctags = md.get("ctags")
    if isinstance(ctags, dict):
        name = str(ctags.get("name") or "").strip()
        kind = str(ctags.get("kind") or "").strip()
        if name:
            out["l1_ctags_name"] = name
        if kind:
            out["l1_ctags_kind"] = kind
    bm25_document = str(md.get("bm25_document") or "").strip()
    if bm25_document:
        # Keep metadata compact while preserving lexical hints.
        out["l1_bm25_document"] = bm25_document[:2000]
    graph = md.get("graph")
    if isinstance(graph, dict):
        symbol = str(graph.get("symbol") or md.get("symbol") or "").strip()
        if symbol:
            out["l1_graph_symbol"] = symbol
        for key in ("calls", "imports", "extends", "contains"):
            values = graph.get(key)
            if isinstance(values, list) and values:
                out[f"l1_graph_{key}"] = ", ".join(str(v) for v in values[:20])[:2000]
        relations = graph.get("relations")
        if isinstance(relations, list) and relations:
            relation_bits: list[str] = []
            for rel in relations[:20]:
                if not isinstance(rel, dict):
                    continue
                relation_type = str(rel.get("type") or "").strip()
                name = str(rel.get("name") or rel.get("target_symbol") or "").strip()
                target_uri = str(rel.get("target_uri") or rel.get("target_agfs_uri") or "").strip()
                target_path = str(rel.get("target_path") or "").strip()
                start = rel.get("target_start_line")
                end = rel.get("target_end_line")
                loc = target_path
                if start is not None and end is not None:
                    loc = f"{loc}:{start}-{end}" if loc else f"{start}-{end}"
                bit = f"{relation_type}:{name}".strip(":")
                if loc:
                    bit += f"->{loc}"
                if target_uri:
                    bit += f"({target_uri})"
                if bit:
                    relation_bits.append(bit)
            if relation_bits:
                out["l1_graph_relations"] = "; ".join(relation_bits)[:2000]
    graph_document = str(md.get("graph_document") or "").strip()
    if graph_document:
        out["l1_graph_document"] = graph_document[:2000]
    return out


def _l2_embed_text(node: ContextNode) -> str:
    """L2 text, with code location header when indexing code chunks."""
    if not (node.content or "").strip():
        return ""
    if node.category == "code":
        return prepend_code_location_header(node.content, node.metadata)
    return node.content or ""


def build_index_records(node: ContextNode) -> list[IndexRecord]:
    """Expand ContextNode into up to 3 IndexRecords (L0/L1/L2)."""
    account_id = _extract_account_id(node.uri)
    base_filters: dict[str, str | int | float | bool] = {
        "account_id": account_id,
        "owner_space": node.owner_space,
        "category": node.category,
        "context_type": node.context_type,
    }
    base_metadata: dict[str, str | bool] = {
        "category": node.category,
        "context_type": node.context_type,
        "parent_uri": node.parent_uri,
        "has_overview": bool(node.overview),
        "has_content": bool(node.content),
    }

    records: list[IndexRecord] = []
    enabled_levels = _parse_enabled_index_levels()

    l0_text = (node.abstract or "").strip()
    if 0 in enabled_levels and l0_text:
        records.append(IndexRecord(
            id=IndexRecord.generate_id(node.uri, 0),
            uri=node.uri,
            level=0,
            text=l0_text,
            filters=dict(base_filters),
            metadata={"level": "abstract", **base_metadata},
        ))

    l1_text = (node.overview or "").strip()
    if 1 in enabled_levels and l1_text:
        l1_meta = {**base_metadata, "level": "overview", **_l1_code_index_metadata(node)}
        records.append(IndexRecord(
            id=IndexRecord.generate_id(node.uri, 1),
            uri=node.uri,
            level=1,
            text=l1_text,
            filters=dict(base_filters),
            metadata=l1_meta,
        ))

    l2_text = _l2_embed_text(node)
    if 2 in enabled_levels and l2_text.strip():
        l2_uri = node.uri.rstrip("/") + "/content.md"
        l2_metadata = {
            **base_metadata,
            "parent_uri": node.uri,
        }
        records.append(IndexRecord(
            id=IndexRecord.generate_id(l2_uri, 2),
            uri=l2_uri,
            level=2,
            text=l2_text,
            filters=dict(base_filters),
            metadata={"level": "content", **l2_metadata},
        ))

    _validate_filters(records)
    return records


def _validate_filters(records: list[IndexRecord]) -> None:
    """Validate that all IndexRecords have required filter keys.

    This is the LAST line of defense against cross-tenant leakage.
    Missing filters cause SILENT leakage (no runtime error in vector search).

    Args:
        records: IndexRecords to validate

    Raises:
        FilterValidationError: If any record is missing required keys
    """
    for record in records:
        missing_keys = tuple(
            key for key in _REQUIRED_FILTER_KEYS
            if key not in record.filters
        )
        if missing_keys:
            raise FilterValidationError(missing_keys)


def build_single_record(
    uri: str,
    level: int,
    text: str,
    account_id: str,
    owner_space: str,
    category: str,
    context_type: str,
) -> IndexRecord:
    """Build a single IndexRecord with explicit parameters.

    Utility function for building individual records.
    Used by OutboxWorker when processing events.

    Args:
        uri: ContextNode URI
        level: Index level (0, 1, or 2)
        text: Text content for embedding
        account_id: Tenant ID (REQUIRED for isolation)
        owner_space: user_space or agent_space (REQUIRED for isolation)
        category: Memory category (profile, preference, etc.)
        context_type: MEMORY | SKILL | RESOURCE

    Returns:
        IndexRecord with mandatory filters

    Raises:
        FilterValidationError: If account_id or owner_space is empty
    """
    if not account_id:
        raise FilterValidationError(("account_id",))
    if not owner_space:
        raise FilterValidationError(("owner_space",))

    return IndexRecord(
        id=IndexRecord.generate_id(uri, level),
        uri=uri,
        level=level,
        text=text,
        filters={
            "account_id": account_id,
            "owner_space": owner_space,
            "category": category,
            "context_type": context_type,
        },
        metadata={
            "level": str(level),
            "category": category,
            "context_type": context_type,
        },
    )


def build_record_id(uri: str, level: int) -> str:
    """Build IndexRecord ID from URI and level.

    Args:
        uri: ContextNode URI
        level: Index level (0, 1, or 2)

    Returns:
        IndexRecord ID (sha256 hash)
    """
    return IndexRecord.generate_id(uri, level)
