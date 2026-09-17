"""Knowledge-graph retrieval over the LORE knowledge bundle (Stage 5c — LightRAG).

Wraps LightRAG (a KG + vector index in one) with **local** sentence-transformer
embeddings (nothing leaves the machine) and a pluggable build-LLM (Claude via
``ANTHROPIC_API_KEY``, or a local Ollama model). The index lives under
``knowledge/kb/`` (gitignored).

Retrieval is a *finding* aid: it resolves *what to fetch* and cites its sources; it
never supplies report numbers — the live pull still produces every value (the trust
invariant that keeps LORE correct even as LabArchives pages change).

STATUS / IMPORTANT
------------------
The LightRAG-specific glue lives entirely in ``_LightRAGBackend`` and is
**version-sensitive** — validate it against your installed LightRAG on the first real
build (it could not be executed in the authoring environment). Everything degrades
gracefully: if LightRAG / sentence-transformers are absent, ``backend_available()`` is
False and callers fall back to the keyword search (``lab_agent.cli.ask``). The rest of
this module (formatting, DI wiring, build/query flow) is unit-tested with a fake backend.

Install:  pip install -e '.[rag]'      Build:  python -m lab_agent.cli.build_kb --index
Query:    python -m lab_agent.cli.query_kb "your question"
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


def backend_available() -> bool:
    """True if the optional RAG dependencies are INSTALLED.

    Uses ``importlib.util.find_spec`` (a metadata lookup) instead of importing the packages,
    so a bare availability check does NOT pay the ~15-20s cost of importing
    sentence-transformers/torch. That heavy import is deferred to ``_ensure_backend`` — i.e.
    it happens once, only when a backend is actually created to run a query or build.
    """
    import importlib.util

    try:
        return bool(importlib.util.find_spec("lightrag")
                    and importlib.util.find_spec("sentence_transformers"))
    except Exception:
        return False


class Backend(Protocol):
    """Minimal interface a KG backend must provide (real or fake)."""

    def insert(self, doc_id: str, text: str) -> None: ...
    def delete(self, doc_id: str) -> None: ...
    def query(self, question: str, mode: str = "hybrid") -> str: ...


def _format_doc(doc: dict) -> str:
    """One indexable chunk with an explicit source tag so retrieval keeps provenance."""
    src = doc.get("source") or doc.get("id") or "unknown"
    return f"[source: {src}]\n{doc.get('text', '').strip()}"


class KnowledgeGraph:
    """Build/query a LightRAG index over the knowledge bundle.

    A *backend* may be injected (for tests); otherwise a real LightRAG backend is
    created lazily when the optional deps are installed.
    """

    def __init__(self, working_dir: str | Path, backend: Backend | None = None) -> None:
        self.working_dir = Path(working_dir)
        self._backend = backend

    # ---- lifecycle -------------------------------------------------------
    def available(self) -> bool:
        return self._backend is not None or backend_available()

    def is_built(self) -> bool:
        return self.working_dir.is_dir() and any(self.working_dir.iterdir())

    def _ensure_backend(self) -> Backend:
        if self._backend is None:
            self.working_dir.mkdir(parents=True, exist_ok=True)
            self._backend = _LightRAGBackend(self.working_dir)
        return self._backend

    # ---- API -------------------------------------------------------------
    def insert(self, doc_id: str, text: str) -> None:
        """Insert/replace one doc under a stable id (so it can be updated/deleted)."""
        self._ensure_backend().insert(doc_id, _format_doc({"source": doc_id, "text": text}))

    def delete(self, doc_id: str) -> None:
        """Best-effort removal of a doc from the graph (older-version-tolerant)."""
        try:
            self._ensure_backend().delete(doc_id)
        except Exception as exc:  # noqa: BLE001 — deletion support is version-dependent
            print(f"[rag] delete({doc_id}) unsupported/failed: {exc}")

    def build(self, docs: list[dict]) -> int:
        """Index a list of {id, text, source} docs. Returns the count inserted."""
        n = 0
        for doc in docs:
            if doc.get("text", "").strip():
                self.insert(doc.get("source") or doc.get("id") or "unknown", doc["text"])
                n += 1
        return n

    def query(self, question: str, mode: str = "hybrid") -> str:
        """Answer a question over the graph (hybrid = KG + vector). Provenance in-text."""
        return self._ensure_backend().query(question, mode=mode)

    def close(self) -> None:
        """Release backend resources (event loop + LightRAG background tasks). Safe once."""
        b = self._backend
        if b is not None and hasattr(b, "close"):
            try:
                b.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Real LightRAG backend — isolated + version-sensitive (validate on install)
# ---------------------------------------------------------------------------

def _local_embed_func():
    """Local sentence-transformer embedding function (no data leaves the machine).

    ``max_token_size`` is **read from the model, never hardcoded.** LightRAG uses it
    to truncate over-long content *visibly* (with a warning) before embedding;
    declaring more than the model accepts disables that guard, and
    sentence-transformers then truncates silently instead.

    This was a real, measured defect: a hardcoded 8192 against MiniLM's actual
    256-token window meant 201 of 235 chunks were embedded from their opening
    fifth, discarding a mean 56% of each. The dropped tails were not redundant —
    median cosine to their own head was 0.51 — so the semantic index was missing
    content nobody could see was missing. Reading the limit off the model means a
    model swap updates it automatically and the two can never drift apart again.
    """
    from lightrag.utils import EmbeddingFunc
    from sentence_transformers import SentenceTransformer

    model_name = os.environ.get("KB_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    model = SentenceTransformer(model_name)
    dim = model.get_sentence_embedding_dimension()
    limit = int(model.max_seq_length)
    print(f"[kb] embeddings: {model_name} — {dim} dims, {limit}-token window", flush=True)
    if limit < 512:
        print(f"[kb] WARNING: a {limit}-token window truncates typical notebook chunks. "
              "Set KB_EMBEDDING_MODEL to a long-context model (e.g. BAAI/bge-m3) "
              "and rebuild with --full.", flush=True)

    async def _embed(texts: list[str]):
        return model.encode(texts, normalize_embeddings=True)

    return EmbeddingFunc(embedding_dim=dim, max_token_size=limit, func=_embed)


async def _claude_code_llm_func(prompt, system_prompt=None, history_messages=None, **kwargs) -> str:
    """Build-time LLM = the Claude Code **plan** via headless ``claude -p`` (no API, no Ollama).

    Every entity/relation-extraction call is routed through the logged-in Claude Code plan,
    so the KG build costs no pay-per-token API. ANTHROPIC_API_KEY is scrubbed so the CLI
    uses the plan login (not the API), matching how the Slack listener spawns sessions.
    Prompt goes in via stdin (no argv length limit).
    """
    import asyncio

    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt)
    for h in history_messages or []:
        parts.append(f"{h.get('role', '')}: {h.get('content', '')}")
    parts.append(prompt)
    full = "\n\n".join(parts)

    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    cmd = "claude -p --dangerously-skip-permissions"
    model = os.environ.get("KB_BUILD_MODEL")
    if model:
        cmd += f" --model {model}"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate(full.encode("utf-8"))
    text = out.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError(f"claude -p returned nothing: {err.decode('utf-8', errors='replace')[:300]}")
    if cli_error_message(text):
        raise RuntimeError(
            f"claude -p could not answer — {cli_error_message(text)}. "
            "The knowledge graph's LLM runs on the Claude Code login; re-authenticate "
            "(`claude` in a terminal) and retry."
        )
    return text


# `claude -p` reports auth/usage failures on STDOUT and still exits 0, so the
# error text comes back looking exactly like a model answer. LightRAG then feeds
# "Failed to authenticate. API Error: 401 …" into keyword extraction, logs a JSON
# repair warning, and returns an empty answer — the caller sees a thin result and
# no sign that the system is broken. Detect it and fail loudly instead.
_CLI_ERROR_MARKERS = (
    "failed to authenticate",
    "oauth access token has expired",
    "re-authenticate to continue",
    "invalid api key",
    "credit balance is too low",
    "usage limit reached",
)


def cli_error_message(text: str) -> str | None:
    """Return the CLI error if *text* is an error report rather than an answer.

    Only short outputs are considered: a genuine answer that happens to quote one
    of these phrases will be long, so length keeps the check from firing on real
    content.
    """
    if len(text) > 600:
        return None
    low = text.lower()
    for marker in _CLI_ERROR_MARKERS:
        if marker in low:
            return text.strip().splitlines()[0][:200]
    return None


class _LightRAGBackend:
    """Thin adapter over LightRAG 1.5.x. All version-sensitive calls are here.

    LightRAG 1.x requires an async init (initialize_storages + pipeline status) before
    any insert/query, and exposes delete only as ``adelete_by_doc_id``. We drive its
    async API through a dedicated event loop so the sync Backend interface holds.
    """

    def __init__(self, working_dir: Path) -> None:
        import asyncio
        import sys

        from lightrag import LightRAG, QueryParam
        from lightrag.kg.shared_storage import initialize_pipeline_status

        self._QueryParam = QueryParam
        # asyncio subprocesses (our `claude -p` LLM) require a Proactor loop on Windows.
        self._loop = (asyncio.ProactorEventLoop() if sys.platform == "win32"
                      else asyncio.new_event_loop())
        # Low concurrency: each LLM call spawns a `claude -p` process, so cap parallelism.
        self._rag = LightRAG(
            working_dir=str(working_dir),
            llm_model_func=_claude_code_llm_func,
            llm_model_max_async=int(os.environ.get("KB_LLM_CONCURRENCY", "2")),
            embedding_func=_local_embed_func(),
        )
        self._loop.run_until_complete(self._rag.initialize_storages())
        self._loop.run_until_complete(initialize_pipeline_status())

    def insert(self, doc_id: str, text: str) -> None:
        # ids= pins a stable doc id so a changed doc replaces (not duplicates) the old one.
        self._loop.run_until_complete(self._rag.ainsert(text, ids=doc_id))

    def delete(self, doc_id: str) -> None:
        self._loop.run_until_complete(self._rag.adelete_by_doc_id(doc_id))

    def query(self, question: str, mode: str = "hybrid") -> str:
        return self._loop.run_until_complete(
            self._rag.aquery(question, param=self._QueryParam(mode=mode)))

    def close(self) -> None:
        import asyncio

        try:
            self._loop.run_until_complete(self._rag.finalize_storages())
        except Exception:
            pass
        try:
            pending = asyncio.all_tasks(self._loop)
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        self._loop.close()
