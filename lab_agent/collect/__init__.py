"""Experiment collection pipeline stages: discover → ingest → summarize (+ dependencies)."""
from .dependencies import resolve_dependencies
from .summarize import summarize_bundle

__all__ = ["resolve_dependencies", "summarize_bundle"]
