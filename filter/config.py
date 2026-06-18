"""Runtime configuration for the filter package."""

from __future__ import annotations

import os


def filter_enabled() -> bool:
    value = os.getenv("CONTEXTSNIPER_FILTER_ENABLED", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}

