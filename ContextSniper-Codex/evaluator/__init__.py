"""Self-contained benchmark validation support for ContextSniper-Codex."""

from .outcome import classify_validation_result, event_usage
from .project import Project
from .validator import ProjectValidator
from .workspace import ProjectWorkspace

__all__ = [
    "Project",
    "ProjectValidator",
    "ProjectWorkspace",
    "classify_validation_result",
    "event_usage",
]
