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


def test_cli_error_detection():
    """`claude -p` reports auth failure on stdout with exit 0.

    Found live on 2026-09-05: query_kb returned "Failed to authenticate. API
    Error: 401 OAuth access token has expired." as if it were the model's answer,
    LightRAG fed it into keyword extraction, and the user got an empty answer with
    no indication the graph's LLM was down.
    """
    from lab_agent.rag.graph import cli_error_message

    real = "Failed to authenticate. API Error: 401 OAuth access token has expired. Re-authenticate to continue."
    assert cli_error_message(real), "did not detect the real auth failure"
    assert "Failed to authenticate" in cli_error_message(real)

    for txt in ("Invalid API key", "Credit balance is too low", "Usage limit reached"):
        assert cli_error_message(txt), f"missed: {txt}"

    assert cli_error_message("The QPD devices were measured at 2.7-2.9 GHz.") is None
    assert cli_error_message("") is None

    # A long, genuine answer that mentions authentication must not be swallowed.
    long_answer = ("The cryostat wiring notes discuss how to authenticate to the "
                   "instrument server. " * 20)
    assert cli_error_message(long_answer) is None, "false positive on a long answer"


def test_embedding_limit_is_read_from_the_model_not_hardcoded():
    """The declared max_token_size must equal the model's real input window.

    Regression guard for a measured defect: graph.py declared 8192 while
    all-MiniLM-L6-v2 accepts 256, which disabled LightRAG's own truncation
    warning and let sentence-transformers drop a mean 56% of 201 of 235 chunks
    in silence. A literal number here is the bug; the value has to come off the
    model so a model swap cannot reintroduce the mismatch.
    """
    import inspect

    from lab_agent.rag import graph as g

    src = inspect.getsource(g._local_embed_func)
    assert "max_token_size=limit" in src, (
        "max_token_size is not wired to the model's own limit"
    )
    assert "model.max_seq_length" in src, (
        "the limit is not read from the model"
    )
    for bad in ("max_token_size=8192", "max_token_size=1024", "max_token_size=512"):
        assert bad not in src, f"hardcoded embedding window is back: {bad}"


def test_the_graph_llm_is_text_only_and_outside_the_repo():
    """The graph's LLM reads LabArchives page text, so it must not be an agent.

    It ran with --dangerously-skip-permissions from the LORE checkout, which loaded
    CLAUDE.md, skills and MCP servers, and once answered a query by running query_kb
    itself. Page text that read like an instruction could have run a command.
    """
    import os
    import tempfile
    from pathlib import Path

    from lab_agent.config import PROJECT_ROOT
    from lab_agent.rag import graph as g

    args = g.claude_text_only_args("claude-sonnet-4-6")
    assert "--dangerously-skip-permissions" not in args
    assert args[args.index("--tools") + 1] == "", "tools must be disabled with --tools \"\""
    assert "--strict-mcp-config" in args and "--mcp-config" not in args, "no MCP servers"
    assert "--disable-slash-commands" in args and "--no-session-persistence" in args
    assert args[-2:] == ["--model", "claude-sonnet-4-6"]
    assert "--model" not in g.claude_text_only_args(None)

    saved = os.environ.get("KB_LLM_SANDBOX")
    try:
        os.environ.pop("KB_LLM_SANDBOX", None)
        default = g.llm_sandbox_dir().resolve()
        root = Path(PROJECT_ROOT).resolve()
        assert root not in default.parents and default != root, (
            "the sandbox is inside the checkout, so LORE's CLAUDE.md would load")
        assert "appdata" not in str(default).lower(), (
            "Store Python redirects AppData writes; claude.exe would not find the folder")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["KB_LLM_SANDBOX"] = str(Path(tmp) / "box")
            assert g.llm_sandbox_dir() == Path(tmp) / "box" and (Path(tmp) / "box").is_dir()
    finally:
        if saved is None:
            os.environ.pop("KB_LLM_SANDBOX", None)
        else:
            os.environ["KB_LLM_SANDBOX"] = saved


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
