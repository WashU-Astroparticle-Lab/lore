"""Run every unit test with outbound network access denied.

This is the supported way to run the suite:

    python tests/run_all.py            # all of tests/test_*.py
    python tests/run_all.py verify     # only files matching *verify*

Why the network guard: `tests/test_la_upload_limit.py` was not a unit test. It
posted real entries to the lab's real LabArchives notebook, created a fresh page
per run, and matched `tests/test_*.py` — so every routine `for t in
tests/test_*.py` sweep filed another page. 27 of them accumulated against 6 real
reports before anyone noticed, because the test exited 0 every time. A test that
also written to Slack (`chat.postMessage`) slipped in the same way.

Grepping for it is not proof. Loopback is allowed (one test binds a free local
port); anything else raises NetworkAttempt and fails the run, naming the host.
So a test that acquires a live side effect cannot pass here, and cannot hide.
"""
from __future__ import annotations

import runpy
import socket
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

LOOPBACK = ("127.0.0.1", "::1", "localhost", "")


class NetworkAttempt(RuntimeError):
    """A unit test tried to reach something outside this machine."""


def _install_guard() -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_gai = socket.getaddrinfo

    def guard(real):
        def wrapper(*args, **kwargs):
            addr = next((a for a in reversed(args) if isinstance(a, (tuple, list))), None)
            if addr:
                host = addr[0] if addr else ""
                if host not in LOOPBACK:
                    raise NetworkAttempt(
                        f"a unit test tried to connect to {host!r}. Unit tests must not "
                        "touch Slack, GitHub or LabArchives — stub the call, or rename the "
                        "file to live_*.py and gate it behind an env var."
                    )
            return real(*args, **kwargs)
        return wrapper

    def gai(host, *args, **kwargs):
        if host not in LOOPBACK:
            raise NetworkAttempt(f"a unit test tried to resolve {host!r}.")
        return real_gai(host, *args, **kwargs)

    socket.socket.connect = guard(real_connect)
    socket.socket.connect_ex = guard(real_connect_ex)
    socket.create_connection = guard(real_create)
    socket.getaddrinfo = gai


def _run_one(path: Path) -> int:
    """Child mode: install the guard, run exactly one test file."""
    _install_guard()
    sys.argv = [str(path)]
    try:
        runpy.run_path(str(path), run_name="__main__")
    except NetworkAttempt as exc:
        print(f"NETWORK: {exc}")
        return 3
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def main() -> int:
    # Each file runs in its own process. Sharing one interpreter let an earlier
    # file's os.environ and imported module state change a later file's result:
    # test_qa_resolution passed alone and failed after test_kg_service had set
    # KB_SERVICE_PORT. Isolation makes a pass mean the same thing every time.
    if len(sys.argv) > 2 and sys.argv[1] == "--one":
        return _run_one(Path(sys.argv[2]))

    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(f for f in TESTS.glob("test_*.py") if pattern in f.name)
    if not files:
        print(f"[tests] no test files match {pattern!r}")
        return 1

    passed, failed, netfail = [], [], []
    for f in files:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--one", str(f)],
            capture_output=True, text=True, cwd=str(TESTS.parent),
        )
        if proc.returncode == 0:
            passed.append(f.name)
            print(f"  ok    {f.name}")
        elif proc.returncode == 3:
            netfail.append(f.name)
            tail = [l for l in proc.stdout.splitlines() if l.startswith("NETWORK:")]
            print(f"  NET   {f.name}: {tail[-1] if tail else 'network access'}")
        else:
            failed.append(f.name)
            last = (proc.stdout.strip().splitlines() or [""])[-1]
            print(f"  FAIL  {f.name}: {last[:160]}")

    print(f"\n[tests] {len(passed)} passed, {len(failed)} failed, "
          f"{len(netfail)} reached for the network (denied except loopback)")
    if netfail:
        print("  A unit test must not touch Slack, GitHub or LabArchives. Stub the call,")
        print("  or rename the file to live_*.py and gate it behind an env var.")
    return 1 if (failed or netfail) else 0


if __name__ == "__main__":
    sys.exit(main())
