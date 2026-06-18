# contextsniper filter — context shortening tool for ContextSniper
#
# Filter is NOT a compression tool. It SHORTENS context via Filter and format
# conversion, while PRESERVING information density.
from filter.core.filter import MinimalFilter, AggressiveFilter
from filter.core.truncation import smart_truncate
from filter.core.dedup import deduplicate
from filter.core.shorter import PipelineShorter, shorten_text
from filter.plugin import ContextSniperFilterPlugin, get_plugin

__all__ = [
    "MinimalFilter",
    "AggressiveFilter",
    "smart_truncate",
    "deduplicate",
    "PipelineShorter",
    "shorten_text",
    "ContextSniperFilterPlugin",
    "get_plugin",
]