"""Tests for the Stage 5c RAG layer (lab_agent.rag.graph).

Covers the dependency-injected build/query flow, source-tag provenance, and graceful
degradation — all without the optional LightRAG/sentence-transformers deps. The real
LightRAG backend is validated on install against a live corpus, not here.

Run: python -m pytest tests/test_rag_graph.py  (or: python tests/test_rag_graph.py)
"""
from lab_agent.rag.graph import KnowledgeGraph, backend_available, _format_doc


class FakeBackend:
    def __init__(self):
        self.inserted = []      # (doc_id, text)
        self.deleted = []
        self.last_query = None

    def insert(self, doc_id, text):
        self.inserted.append((doc_id, text))

    def delete(self, doc_id):
        self.deleted.append(doc_id)

    def query(self, question, mode="hybrid"):
        self.last_query = (question, mode)
        return f"answer[{mode}]"


def test_backend_available_returns_bool():
    # False in a clean env (no lightrag); True only after `pip install .[rag]`.
    assert isinstance(backend_available(), bool)


def test_format_doc_keeps_source_provenance():
    out = _format_doc({"source": "la_page:SQUAT_Cooldown", "text": "base temp 12 mK"})
    assert "[source: la_page:SQUAT_Cooldown]" in out and "12 mK" in out


def test_build_inserts_nonempty_docs_with_source_tags():
    fb = FakeBackend()
    kg = KnowledgeGraph("kb_unused", backend=fb)
    n = kg.build([
        {"source": "a", "text": "hello"},
        {"source": "b", "text": "   "},        # empty → skipped
        {"source": "c", "text": "world"},
    ])
    assert n == 2
    assert [i[0] for i in fb.inserted] == ["a", "c"]     # stable doc ids
    assert fb.inserted[0][1].startswith("[source: a]")   # provenance tag in text


def test_insert_and_delete_delegate_with_doc_id():
    fb = FakeBackend()
    kg = KnowledgeGraph("kb_unused", backend=fb)
    kg.insert("la_page:X", "base temp 12 mK")
    assert fb.inserted[0][0] == "la_page:X" and "12 mK" in fb.inserted[0][1]
    kg.delete("la_page:X")
    assert fb.deleted == ["la_page:X"]


def test_query_delegates_to_backend_with_mode():
    fb = FakeBackend()
    kg = KnowledgeGraph("kb_unused", backend=fb)
    assert kg.query("has f0 drifted?", mode="local") == "answer[local]"
    assert fb.last_query == ("has f0 drifted?", "local")


def test_available_true_with_injected_backend():
    assert KnowledgeGraph("kb_unused", backend=FakeBackend()).available() is True


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
