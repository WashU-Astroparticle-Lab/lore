"""Knowledge-graph retrieval layer (Stage 5c). Optional — install with `.[rag]`."""
from .graph import KnowledgeGraph, backend_available

__all__ = ["KnowledgeGraph", "backend_available"]
