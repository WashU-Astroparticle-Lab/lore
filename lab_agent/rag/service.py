"""Warm, resident knowledge-graph query service (speed).

Loading the KG costs ~25-40s per cold start (sentence-transformers ~17s + torch + the
graph/vecdb ~7s). The Slack Q&A path spawns a fresh subprocess per question, so it re-pays
that every time. This module keeps ONE warm copy: the always-on listener starts ``serve()``
in a daemon thread, which loads the KG once and answers queries over a **localhost-only**
HTTP endpoint. ``query_kb`` becomes a thin client (``query_via_service``) that hits this
endpoint and falls back to the cold path if the service isn't up.

Single-threaded on purpose: the LightRAG backend owns an asyncio loop bound to the thread
that created it, so load + serve + every query all run on the one serve thread (concurrent
requests simply queue — fine for Q&A volume). Bind is 127.0.0.1 only — on-prem, no external
exposure, honoring the local-only rule.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_PORT = 8765


def service_port() -> int:
    try:
        return int(os.environ.get("KB_SERVICE_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


# ── client (used by query_kb) ─────────────────────────────────────────────────

def query_via_service(question: str, mode: str = "hybrid", timeout: float = 600.0) -> str | None:
    """Return the warm service's answer, or None if it's unreachable/erroring (caller falls back).

    Connection-refused (service not started or still warming up) fails fast, so the fallback is
    quick; the long timeout only matters once connected, since the answer includes LLM synthesis.
    """
    import urllib.request

    payload = json.dumps({"question": question, "mode": mode}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{service_port()}/query",
                                 data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("answer")
    except Exception:
        return None


def trigger_reload(timeout: float = 5.0) -> bool:
    """Ask a running service to reload the graph (e.g. after a nightly rebuild). Best-effort."""
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{service_port()}/reload",
                                 data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


# ── server (hosted by the listener) ───────────────────────────────────────────

_stop = threading.Event()


def serve() -> None:
    """Load the KG once, then answer /query and /reload on 127.0.0.1 — all on ONE thread.

    Meant to run in a daemon thread started by the listener. If the RAG deps or the built graph
    are absent, it logs and returns, and clients transparently use the cold/keyword path.
    """
    # Avoid a hard crash from a duplicate OpenMP runtime (conda + torch/mkl) when torch loads.
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

    from ..config import kb_dir, load_env
    from . import KnowledgeGraph, backend_available

    load_env()   # so KB_STORAGE_DIR (KB lives outside the repo) is honored regardless of launcher

    if not backend_available():
        print("[kg-service] RAG deps not installed — service not started (clients use cold path).",
              flush=True)
        return

    kbdir = kb_dir()
    if not (kbdir.is_dir() and any(kbdir.iterdir())):
        print("[kg-service] no built graph — service not started (clients use keyword fallback).",
              flush=True)
        return

    print("[kg-service] loading knowledge graph (one-time ~30s)...", flush=True)
    state = {"kg": KnowledgeGraph(kbdir)}
    state["kg"]._ensure_backend()   # warm: heavy import + model + graph load, on THIS thread
    reload_flag = threading.Event()
    print(f"[kg-service] ready on 127.0.0.1:{service_port()} — Q&A is now warm.", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):        # silence default per-request stderr logging
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/reload":
                reload_flag.set()
                self._send(200, {"status": "reloading"})
                return
            if self.path != "/query":
                self._send(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n).decode("utf-8"))
                answer = state["kg"].query(req["question"], mode=req.get("mode", "hybrid"))
                self._send(200, {"answer": answer})
            except Exception as exc:      # noqa: BLE001 — report, don't kill the service
                self._send(500, {"error": str(exc)})

        def do_GET(self):
            self._send(200 if self.path == "/health" else 404,
                       {"status": "ready"} if self.path == "/health" else {"error": "not found"})

    server = HTTPServer(("127.0.0.1", service_port()), Handler)
    server.timeout = 1.0
    while not _stop.is_set():
        server.handle_request()           # one request, or return after 1s if idle
        if reload_flag.is_set():
            reload_flag.clear()
            print("[kg-service] reloading graph after refresh...", flush=True)
            try:
                old = state["kg"]
                fresh = KnowledgeGraph(kbdir)
                fresh._ensure_backend()
                state["kg"] = fresh
                try:
                    old.close()
                except Exception:
                    pass
                print("[kg-service] reload complete.", flush=True)
            except Exception as exc:       # noqa: BLE001 — keep serving the old graph
                print(f"[kg-service] reload failed (keeping current graph): {exc}", flush=True)


def serve_in_background() -> threading.Thread:
    """Start serve() in a daemon thread and return it."""
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t
