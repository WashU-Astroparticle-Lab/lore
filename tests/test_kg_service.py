"""The warm KG service client must read answers when the service is up and fall back (None)
when it is not — so query_kb degrades gracefully to the cold path.

Run: python tests/test_kg_service.py
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import lab_agent.rag.service as svc


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_query_via_service_none_when_down():
    import os
    os.environ["KB_SERVICE_PORT"] = str(_free_port())   # nothing listening there
    assert svc.query_via_service("anything", timeout=1.0) is None


def _run_stub(port: int, hits: list):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode()) if n else {}
            hits.append((self.path, body))
            resp = ({"answer": f"warm answer to: {body.get('question')}"}
                    if self.path == "/query" else {"status": "reloading"})
            out = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", port), H)
    return srv


def test_query_via_service_reads_answer_and_reload():
    import os
    port = _free_port()
    os.environ["KB_SERVICE_PORT"] = str(port)
    hits: list = []
    srv = _run_stub(port, hits)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        ans = svc.query_via_service("mean Qc in BE260416?", mode="hybrid", timeout=5.0)
        assert ans == "warm answer to: mean Qc in BE260416?", ans
        assert svc.trigger_reload(timeout=5.0) is True
        assert hits[0][0] == "/query" and hits[-1][0] == "/reload", hits
    finally:
        srv.shutdown()


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
