"""The knowledge-graph build and service must not hide failures or keep stale content.

Each test is a failure found in the 2026-11-06 review:
  * a page LightRAG failed on was counted as indexed; a dead login failed page after page;
  * a delete LightRAG refused was treated as done;
  * a service error was taken for "service down", and the cold retry crashed;
  * two services could share port 8765;
  * a lock left by a killed build blocked every build for six hours;
  * renamed and deleted pages never left the corpus, and a partial crawl wiped the
    figure manifest.

Nothing here touches C:\\lore_kb, the real corpus, or the running service.
Run: python tests/run_all.py kg_robustness
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import lab_agent.rag as rag
import lab_agent.rag.service as svc
from lab_agent.cli import build_kb
from lab_agent.collect import la_crawl
from lab_agent.config import lab_config_value
from lab_agent.rag import build_lock
from lab_agent.rag.incremental import load_manifest, normalized_hash, save_manifest


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def kb_in(tmp: str):
    saved = os.environ.get("KB_STORAGE_DIR")
    os.environ["KB_STORAGE_DIR"] = tmp
    try:
        yield Path(tmp)
    finally:
        if saved is None:
            os.environ.pop("KB_STORAGE_DIR", None)
        else:
            os.environ["KB_STORAGE_DIR"] = saved


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeKG:
    def __init__(self, *a, fail_insert=(), fail_delete=(), stop_on=None):
        self.inserted, self.deleted = [], []
        self.fail_insert, self.fail_delete, self.stop_on = set(fail_insert), set(fail_delete), stop_on

    def delete(self, doc_id):
        if doc_id in self.fail_delete:
            return False
        self.deleted.append(doc_id)
        return True

    def insert(self, doc_id, text):
        if doc_id == self.stop_on:
            raise RuntimeError("claude -p could not answer — Failed to authenticate: OAuth "
                               "session expired")
        if doc_id in self.fail_insert:
            raise RuntimeError("LightRAG marked it failed: extract error")
        self.inserted.append(doc_id)

    def close(self):
        pass


def _run_index(docs, manifest, kg):
    with tempfile.TemporaryDirectory() as tmp, kb_in(tmp):
        save_manifest(manifest)
        reloads = []
        with patched(build_kb, _gather_corpus=lambda: docs), \
             patched(rag, KnowledgeGraph=lambda *a, **k: kg, backend_available=lambda: True), \
             patched(svc, trigger_reload=lambda *a, **k: reloads.append(1) or True):
            code = build_kb._build_index()
        return code, load_manifest(), reloads


def test_a_failed_page_is_not_marked_done_and_a_dead_login_stops_the_run():
    docs = {"a": "page a", "b": "page b", "c": "page c"}
    kg = FakeKG(stop_on="b")
    code, manifest, reloads = _run_index(docs, {}, kg)
    assert kg.inserted == ["a"], "c must not be tried once the login is known to be dead"
    assert set(manifest) == {"a"}, "b and c must be retried next run"
    assert code == build_kb.EXIT_PARTIAL and reloads, (code, reloads)

    kg = FakeKG(fail_insert={"b"})
    code, manifest, _ = _run_index(docs, {}, kg)
    assert kg.inserted == ["a", "c"] and set(manifest) == {"a", "c"}, "one bad page, not all"
    assert code == build_kb.EXIT_PARTIAL


def test_a_delete_lightrag_refused_is_retried_not_dropped():
    docs = {"x": "new text for x"}
    old = {"x": normalized_hash("old text for x"), "gone": normalized_hash("a deleted page")}
    kg = FakeKG(fail_delete={"x", "gone"})
    code, manifest, _ = _run_index(docs, old, kg)
    assert kg.inserted == [], "re-inserting an id LightRAG still holds is silently dropped"
    assert manifest["x"] == old["x"] and "gone" in manifest, manifest
    assert code == build_kb.EXIT_PARTIAL


def test_nothing_to_do_is_a_clean_exit_without_a_reload():
    docs = {"a": "same"}
    code, _, reloads = _run_index(docs, {"a": normalized_hash("same")}, FakeKG())
    assert code == 0 and not reloads


def _stub_service(port, status, body):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            out = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_service_error_is_reported_not_mistaken_for_down():
    port = _free_port()
    saved = os.environ.get("KB_SERVICE_PORT")
    os.environ["KB_SERVICE_PORT"] = str(port)
    try:
        srv = _stub_service(port, 500, {"error": "claude -p could not answer — Failed to authenticate"})
        try:
            svc.query_via_service("q", timeout=5)
        except svc.ServiceError as exc:
            assert "Failed to authenticate" in str(exc)
        else:
            raise AssertionError("a 500 was taken for 'service down'")
        finally:
            srv.shutdown(); srv.server_close()
        port2 = _free_port()
        os.environ["KB_SERVICE_PORT"] = str(port2)
        srv = _stub_service(port2, 503, {"error": "no graph loaded"})
        try:
            assert svc.query_via_service("q", timeout=5) is None, "503 means use the cold path"
        finally:
            srv.shutdown(); srv.server_close()
    finally:
        if saved is None:
            os.environ.pop("KB_SERVICE_PORT", None)
        else:
            os.environ["KB_SERVICE_PORT"] = saved


def test_the_service_port_cannot_be_shared():
    port = _free_port()
    first = svc._ExclusiveHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
    try:
        try:
            second = svc._ExclusiveHTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        except OSError:
            pass
        else:
            second.server_close()
            raise AssertionError("a second service bound the same port")
    finally:
        first.server_close()


def test_a_lock_left_by_a_dead_build_is_reclaimed_at_once():
    with tempfile.TemporaryDirectory() as tmp, kb_in(tmp):
        lock = Path(tmp) / "kb_build.lock"
        lock.write_text("pid=999999 started=1\n", encoding="utf-8")   # no such process
        with build_lock.build_lock():
            pass
        lock.write_text(f"pid={os.getpid()} started=1\n", encoding="utf-8")  # alive: held
        try:
            with build_lock.build_lock():
                pass
        except build_lock.BuildLockHeld:
            pass
        else:
            raise AssertionError("a live build's lock was taken")


class FakeLA:
    """A notebook whose tree is given as {folder: [(display, tree_id, is_page)]}."""

    def __init__(self, tree, broken_folders=()):
        self.tree, self.broken = tree, set(broken_folders)

    def _list_notebooks(self):
        return [{"nbid": "NB1", "name": lab_config_value("Primary notebook") or "NB"}]

    def _get_tree_level(self, nbid, parent="0"):
        if parent in self.broken:
            raise RuntimeError("tree call failed")
        nodes = ET.Element("r")
        for display, tid, is_page in self.tree.get(parent, []):
            n = ET.SubElement(nodes, "level-node")
            ET.SubElement(n, "tree-id").text = tid
            ET.SubElement(n, "display-text").text = display
            ET.SubElement(n, "is-page").text = "true" if is_page else "false"
        return nodes.findall(".//level-node")

    def _get_entries_for_page(self, nbid, tree_id):
        return f"<tree><entry><entry-data>text of {tree_id}</entry-data></entry></tree>"

    @staticmethod
    def _parse_entries_xml(xml):
        return [{"content": ET.fromstring(xml).findtext(".//entry-data")}]


@contextlib.contextmanager
def corpus_in(tmp: str, existing: list[str]):
    root = Path(tmp) / "knowledge"
    (root / "labarchives").mkdir(parents=True)
    for name in existing:
        (root / "labarchives" / f"{name}.md").write_text("old", encoding="utf-8")
    with patched(la_crawl, KNOWLEDGE_ROOT=root, IMAGE_MANIFEST_PATH=root / "images.json"), \
         patched(la_crawl.page_index, record_pages=lambda *a, **k: None):
        yield root / "labarchives"


def test_a_renamed_page_leaves_the_corpus_after_a_complete_crawl():
    tree = {"0": [("New Name", "T1", True), ("Kept", "T2", True)]}
    with tempfile.TemporaryDirectory() as tmp, corpus_in(tmp, ["Old_Name", "Kept"]) as d:
        la_crawl.crawl_to_bundle(adapter=FakeLA(tree))
        assert sorted(p.stem for p in d.glob("*.md")) == ["Kept", "New_Name"]


def test_a_partial_crawl_removes_nothing_and_keeps_figures():
    tree = {"0": [("A", "T1", True), ("Folder", "F1", False)], "F1": [("B", "T2", True)]}
    with tempfile.TemporaryDirectory() as tmp, corpus_in(tmp, ["Old"]) as d:
        la_crawl.IMAGE_MANIFEST_PATH.write_text(json.dumps({"pages": {"Old": {"images": ["u"]}}}))
        la_crawl.crawl_to_bundle(adapter=FakeLA(tree, broken_folders={"F1"}))
        assert "Old" in [p.stem for p in d.glob("*.md")], "a failed folder made the crawl partial"
        la_crawl.crawl_to_bundle(adapter=FakeLA(tree), max_pages=1)
        assert "Old" in [p.stem for p in d.glob("*.md")], "a capped crawl must prune nothing"
        pages = json.loads(la_crawl.IMAGE_MANIFEST_PATH.read_text())["pages"]
        assert "Old" in pages, "a partial crawl wiped the figure manifest"


def test_a_crawl_that_suddenly_sees_few_pages_prunes_nothing():
    tree = {"0": [("Only", "T1", True)]}
    with tempfile.TemporaryDirectory() as tmp, corpus_in(tmp, [f"P{i}" for i in range(30)]) as d:
        la_crawl.crawl_to_bundle(adapter=FakeLA(tree))
        assert len(list(d.glob("*.md"))) == 31, "29 removals at once must be refused"


if __name__ == "__main__":
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
