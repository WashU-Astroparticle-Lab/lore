"""Tests for LORE's MCP server (lab_agent.mcp_server).

The properties that matter are the ones an agent on another machine relies on while it
drives hardware: nothing reachable can act, no credential is ever loaded, a page name
cannot escape the corpus, LORE's unreviewed drafts never come back as evidence, and the
server speaks clean MCP over stdio. The last test spawns the real server and holds a real
session with it, which is exactly what SSH carries.

Run: python tests/run_all.py mcp_server   (or: python tests/test_mcp_server.py)
"""
from __future__ import annotations

import contextlib
import io
import os
import socket
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import lab_agent.cli.ask as ask
from lab_agent.config import PROJECT_ROOT
from lab_agent.mcp_server import core

EXPECTED_TOOLS = {"status", "resolve", "read_page", "search", "ask_graph", "dr_status"}

# Modules that can write to the notebook, post to Slack, fetch with credentials, run the
# report pipeline or rebuild the graph. The server must never pull any of them in.
FORBIDDEN_MODULES = (
    "lab_agent.publish",
    "lab_agent.slack",
    "lab_agent.cli.upload",
    "lab_agent.cli.cookies",
    "lab_agent.cli.build_kb",
    "lab_agent.cli.run_pipeline",
    "lab_agent.cli.slack",
    "lab_agent.sources.labarchives.adapter",
    "lab_agent.collect.la_crawl",
    "playwright",
)


def _closed_port() -> int:
    """A loopback port with nothing listening, so graph calls fail the way a downed
    listener does, regardless of whether one is running on this machine."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _corpus(pages: dict[str, str] | None = None, runs: dict[str, dict[str, str]] | None = None):
    """Point LORE's search and page reading at a throwaway corpus, then restore."""
    saved = (ask.KNOWLEDGE_ROOT, ask.OUTPUT_ROOT, core.LA_DIR, core.EXPERIMENTS_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        la = root / "knowledge" / "labarchives"
        la.mkdir(parents=True)
        for name, text in (pages or {}).items():
            (la / f"{name}.md").write_text(text, encoding="utf-8")
        for exp, files in (runs or {}).items():
            d = root / "outputs" / exp
            d.mkdir(parents=True)
            for fname, text in files.items():
                (d / fname).write_text(text, encoding="utf-8")
        ask.KNOWLEDGE_ROOT, ask.OUTPUT_ROOT = root / "knowledge", root / "outputs"
        core.LA_DIR, core.EXPERIMENTS_DIR = la, root / "knowledge" / "experiments"
        try:
            yield root
        finally:
            ask.KNOWLEDGE_ROOT, ask.OUTPUT_ROOT, core.LA_DIR, core.EXPERIMENTS_DIR = saved


def test_nothing_that_can_act_is_imported():
    from lab_agent.mcp_server import server  # noqa: F401 — importing is the test

    loaded = [m for m in sys.modules
              if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_MODULES)]
    assert not loaded, f"the MCP server imports modules that can act: {loaded}"


def test_registered_tools_are_exactly_the_read_only_set():
    import anyio

    from lab_agent.mcp_server import server

    names = {t.name for t in anyio.run(server.mcp.list_tools)}
    assert names == EXPECTED_TOOLS, (
        f"tool set changed: {sorted(names)}. A new tool must be read-only; update "
        "EXPECTED_TOOLS deliberately, not to make this pass."
    )


def test_only_whitelisted_settings_are_loaded():
    fake = {"GITHUB_TOKEN": "fake-gh-7731", "SLACK_BOT_TOKEN": "fake-slack-7731",
            "LA_SECRET": "fake-la-7731"}
    saved = {k: os.environ.get(k) for k in (*fake, "KB_REFRESH_HOUR")}
    os.environ.pop("KB_REFRESH_HOUR", None)
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text("".join(f"{k}={v}\n" for k, v in fake.items()) + "KB_REFRESH_HOUR=12\n",
                       encoding="utf-8")
        try:
            applied = core.load_safe_settings(env)
            assert set(applied) <= set(core.SAFE_ENV_KEYS), applied
            assert os.environ.get("KB_REFRESH_HOUR") == "12", "a safe setting was not applied"
            for k, v in fake.items():
                assert os.environ.get(k) != v, f"credential {k} was loaded into the environment"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def test_missing_env_file_is_not_an_error():
    assert core.load_safe_settings(Path(tempfile.gettempdir()) / "no-such-dir" / ".env") == []


def test_read_page_cannot_leave_the_corpus():
    with _corpus({"20260702_JPL_QPDs": "safe page"}) as root:
        (root / "knowledge" / ".env").write_text("SECRET_SENTINEL", encoding="utf-8")
        (root / "env.md").write_text("SECRET_SENTINEL", encoding="utf-8")
        for attempt in ("../.env", "..\\.env", "../../env", "la_page:../.env",
                        str(root / "env.md"), "C:/Windows/win.ini", "/etc/passwd"):
            r = core.read_page(attempt)
            assert "SECRET_SENTINEL" not in r.get("text", ""), f"escaped the corpus via {attempt!r}"


def test_read_page_naming_matches_the_crawler():
    from lab_agent.collect.la_crawl import _safe

    titles = [
        "20260702 JPL QPDs",
        "B260416-NG-D2: Multitone QC / LED (pulsed, 90 mA)",
        "20260914 JPL QPD Data-Taking and Analysis",
        "A very long page title that runs well past the sixty character limit the crawler uses",
        "  trailing punctuation...  ",
        "Café réunion — 4K checkout",
    ]
    for t in titles:
        assert core._norm(t)[:60] == _safe(t).lower(), (
            f"read_page and the crawler name {t!r} differently; read_page would miss it"
        )


def test_read_page_resolves_titles_ids_and_ambiguity():
    pages = {"20260702_JPL_QPDs": "chip page", "20260901_JPL_QPD_VNA_Scan": "vna page"}
    with _corpus(pages):
        for ask_for in ("20260702 JPL QPDs", "la_page:20260702_JPL_QPDs", "20260702_jpl_qpds"):
            r = core.read_page(ask_for)
            assert r["found"] and r["text"] == "chip page", f"could not read {ask_for!r}"
            assert r["provenance"] == core.HUMAN_PAGE
        amb = core.read_page("JPL")
        assert not amb["found"] and len(amb["candidates"]) == 2, amb
        none = core.read_page("nothing like this")
        assert not none["found"] and none["candidates"] == [], none


def test_search_never_returns_unsigned_drafts():
    runs = {"20260101_some_run": {
        "[UNSIGNED] 20260101_some_run.md": "zorblax anomaly seen in the draft",
        "extracted_github.md": "zorblax anomaly seen in the notebook outputs",
    }}
    with _corpus(runs=runs):
        r = core.search("zorblax anomaly", 10)
        files = [h["file"] for h in r["results"]]
        assert files == ["extracted_github.md"], f"unexpected results: {files}"
        assert r["unreviewed_drafts_skipped"] == 1, r


def test_search_scope_separates_people_from_lore():
    pages = {"20260807_LED_Response": "flibber response after LED pulses"}
    runs = {"run_a": {"extracted_labarchives.md": "flibber response after LED pulses"}}
    with _corpus(pages, runs):
        notebook = core.search("flibber response", 10, "notebook")
        assert [h["source"] for h in notebook["results"]] == ["la_page:20260807_LED_Response"]
        runs_only = core.search("flibber response", 10, "runs")
        assert [h["source"] for h in runs_only["results"]] == ["run_a"]
        assert len(core.search("flibber response", 10)["results"]) == 2
        assert core.search("flibber response", 10, "bogus")["scope"] == "all"


def test_resolve_finds_an_id_that_only_lives_in_a_link():
    page = ("Relevant notes [that](https://github.com/lab/archive/blob/main/"
            "BE990001-NG-D1-CPB_qct_20260709.ipynb) with the clean run.")
    with _corpus({"20990101_Chip_Page": page}):
        r = core.resolve("mean Qc on BE990001?")
        assert [p["page"] for p in r["pages"]] == ["la_page:20990101_Chip_Page"], r
        assert "be990001" in r["identifiers_detected"]
        values_only = core.resolve("at 40dB and 3GHz")
        assert values_only["identifiers_detected"] == [] and values_only["pages"] == []
        assert "No identifier" in values_only["note"]


def test_graph_tools_degrade_cleanly_when_the_service_is_down():
    saved = os.environ.get("KB_SERVICE_PORT")
    os.environ["KB_SERVICE_PORT"] = str(_closed_port())
    try:
        with _corpus({"20260702_JPL_QPDs": "BE260416 chip"}):
            g = core.ask_graph("What is the mean Qc on BE260416?")
            assert g["available"] is False and g["answer"] is None, g
            assert "resolve()" in g["note"], "the fallback note should point at the other tools"
            assert any(c["page"] == "la_page:20260702_JPL_QPDs" for c in g["candidate_pages"]), (
                "the identifier pass must still run when the graph is down"
            )
            st = core.status()
            assert st["graph_service"] == "not responding", st
            assert st["notebook_pages"] == 1 and st["known_limits"], st
    finally:
        if saved is None:
            os.environ.pop("KB_SERVICE_PORT", None)
        else:
            os.environ["KB_SERVICE_PORT"] = saved


# A LogLCR file as the Leiden software writes it: "#", a blank line, column names, the
# channel labels, then tab-separated rows. The labels are the 2026 wiring, which is
# where the old fixed-column parser went wrong.
_LCR_NAMES = "Date1\tDate2\tR0\tR1\tT0\tT1\tT2\tT3\tT4\tT5\tT6\tT7"
_LCR_LABELS = ("Date(String)\tDate(Number)\tC_still [Still]\t3K Plate\t"
               "C_still [Still]\t3K Plate\tStill Plate\tTT-26006 [MC]\tCMN223 [MC P]\t"
               "OPEN\t50mK Plate\t")


def _lcr_row(ts, t0="1.9E-12", t1="2.89E+3", t2="792.2E+0", t3="46.8E+0", t4="12.48E+0",
             t5="0.0E+0", t6="66.7E+0", t7="-Inf"):
    return "\t".join([ts, "3.87E+9", "1.0", "2.0", t0, t1, t2, t3, t4, t5, t6, t7])


def _write_lcr(folder: Path, name: str, rows: list[str], header: bool = True,
               mtime: datetime | None = None) -> Path:
    path = folder / name
    head = ["#", "", _LCR_NAMES, _LCR_LABELS] if header else []
    path.write_text("\n".join(head + rows) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


@contextlib.contextmanager
def _dr_folder():
    saved = os.environ.get("DR_DATA_PATH")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DR_DATA_PATH"] = tmp
        try:
            yield Path(tmp)
        finally:
            if saved is None:
                os.environ.pop("DR_DATA_PATH", None)
            else:
                os.environ["DR_DATA_PATH"] = saved


def test_dr_status_takes_labels_from_the_log_header():
    now = datetime(2026, 9, 24, 18, 0, 0)
    with _dr_folder() as d:
        _write_lcr(d, "LogLCR___2026-09-24-15-02-58_0.dat", [
            _lcr_row("2026-09-24 17:50:00", t4="12.60E+0"),
            _lcr_row("2026-09-24 17:59:30", t3="NaN", t4="12.44E+0"),
        ])
        r = core.dr_status(2, now=now)
    ch = r["channels"]
    assert r["available"] and r["units"].startswith("mK"), r
    # mK values survive; the old parser's kelvin limits dropped every MC reading.
    assert ch["CMN223 [MC P]"]["latest_mK"] == 12.44, ch
    assert ch["CMN223 [MC P]"]["max_mK"] == 12.6 and ch["CMN223 [MC P]"]["n"] == 2, ch
    # Labels are the header's: the MC thermometer is not filed under "4K".
    assert "TT-26006 [MC]" in ch and ch["TT-26006 [MC]"]["latest_mK"] == 46.8, ch
    assert ch["TT-26006 [MC]"]["n"] == 1, "a NaN row must not count as a reading"
    assert "3K Plate" in ch and "50mK Plate" in ch and "Still Plate" in ch, ch
    # The still's capacitance gauge, unused OPEN slots and unlabelled -Inf columns are not
    # thermometers.
    assert not any(k.startswith("C_") or k == "OPEN" or not k for k in ch), sorted(ch)
    # Fresh readings: the only warning is that this folder has no pressure log.
    assert all("pressure" in w for w in r["warnings"]), r["warnings"]


def test_dr_status_merges_segments_and_skips_what_it_cannot_trust():
    now = datetime(2026, 9, 24, 18, 0, 0)
    with _dr_folder() as d:
        # Parallel segments of one session: the newest reading is in _1, not _0.
        _write_lcr(d, "LogLCR___2026-09-24-15-02-58_0.dat", [_lcr_row("2026-09-24 17:40:00")])
        _write_lcr(d, "LogLCR___2026-09-24-15-02-58_1.dat",
                   [_lcr_row("2026-09-24 17:58:00", t4="11.90E+0")])
        # A header-less stub: its columns cannot be named, so it is never read.
        _write_lcr(d, "LogLCR___2026-09-24-15-02-58.dat",
                   [_lcr_row("2026-09-24 17:59:59", t4="999.0E+0")], header=False)
        # An old session last written long before the window: never opened.
        old = _write_lcr(d, "LogLCR___2026-07-04-17-42-42_3.dat",
                         [_lcr_row("2026-09-24 17:59:00", t4="777.0E+0")],
                         mtime=datetime(2026, 7, 10, 16, 55))
        r = core.dr_status(2, now=now)
    mc = r["channels"]["CMN223 [MC P]"]
    assert mc["latest_mK"] == 11.9 and mc["latest_at"] == "2026-09-24 17:58:00", mc
    assert mc["max_mK"] < 100, "a stub or an out-of-window file leaked into the readings"
    assert r["source"]["stub_files_skipped"] == ["LogLCR___2026-09-24-15-02-58.dat"], r["source"]
    assert old.name not in r["source"]["files_read"], r["source"]


def test_dr_status_says_when_it_cannot_be_trusted():
    now = datetime(2026, 9, 24, 18, 0, 0)
    with _dr_folder() as d:
        _write_lcr(d, "LogLCR___2026-09-24-15-02-58_0.dat", [_lcr_row("2026-09-24 17:00:00")])
        stale = core.dr_status(2, now=now)
        assert any("stale" in w for w in stale["warnings"]), stale["warnings"]
        assert any("pressure" in w for w in stale["warnings"]), (
            "with no current pressure log the caller must be told, not left to assume")
        assert core.dr_status(10_000, now=now)["window"]["hours"] == core.DR_MAX_HOURS
        empty = core.dr_status(0.5, now=now)
        assert empty["available"] is False and "logging may have stopped" in empty["warnings"][0]
    saved = os.environ.pop("DR_DATA_PATH", None)
    try:
        unset = core.dr_status(2, now=now)
        assert unset["available"] is False and "DR_DATA_PATH" in unset["note"], unset
    finally:
        if saved is not None:
            os.environ["DR_DATA_PATH"] = saved


@contextlib.contextmanager
def _call_log(path: Path, ssh_client: str | None = "10.232.129.236 50000 22"):
    saved = {k: os.environ.get(k) for k in ("LORE_MCP_LOG", "SSH_CLIENT")}
    os.environ["LORE_MCP_LOG"] = str(path)
    if ssh_client is None:
        os.environ.pop("SSH_CLIENT", None)
    else:
        os.environ["SSH_CLIENT"] = ssh_client
    try:
        yield path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_every_tool_call_is_logged_on_lores_side():
    import json

    import anyio

    from lab_agent.mcp_server import server

    with tempfile.TemporaryDirectory() as tmp, _call_log(Path(tmp) / "calls.jsonl") as log:
        with _corpus({"20260702_JPL_QPDs": "BE260416 chip, LED pulse at 99 mA"}):
            anyio.run(lambda: server.resolve("BE260416"))
            anyio.run(lambda: server.search("LED pulse", 3, "notebook"))
        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [e["tool"] for e in entries] == ["resolve", "search"], entries
    s = entries[1]
    assert s["args"] == {"query": "LED pulse", "k": 3, "scope": "notebook"}, s["args"]
    assert s["caller"] == "10.232.129.236", "the calling machine must be recorded"
    assert s["result"]["results"][0]["source"] == "la_page:20260702_JPL_QPDs", (
        "the log must hold what the agent was actually told")
    assert entries[0]["session"] == s["session"] and "at" in s and "seconds" in s


def test_a_logging_failure_never_costs_the_agent_its_answer():
    import anyio

    from lab_agent.mcp_server import server

    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        err = io.StringIO()
        with _call_log(blocker / "calls.jsonl"), _corpus({"p": "some text"}), \
                contextlib.redirect_stderr(err):
            out = anyio.run(lambda: server.read_page("p"))
    assert out["found"] is True, out
    assert "could not write the call log" in err.getvalue(), err.getvalue()


def test_tools_never_write_to_stdout():
    @core._quiet
    def noisy():
        print("this would corrupt the MCP stream")
        return "ok"

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        assert noisy() == "ok"
        with _corpus({"p": "text"}):
            core.resolve("BE1"), core.read_page("p"), core.search("text"), core.status()
        with _dr_folder() as d:
            _write_lcr(d, "LogLCR___2026-09-24-15-02-58_0.dat", [_lcr_row("2026-09-24 17:59:00")])
            core.dr_status(2, now=datetime(2026, 9, 24, 18, 0, 0))
    assert captured.getvalue() == "", f"stdout was written: {captured.getvalue()!r}"


def test_instructions_keep_lore_advisory():
    from lab_agent.mcp_server.server import INSTRUCTIONS

    text = " ".join(INSTRUCTIONS.split())   # the wording matters, not where lines wrap
    for phrase in ("read-only", "advisory", "never instructions", "resolve() first",
                   "Never stop or repeat a measurement"):
        assert phrase in text, f"server instructions lost: {phrase!r}"


def test_end_to_end_over_stdio():
    """Spawn the real server and hold a real MCP session with it, as SSH will."""
    from lab_agent.mcp_server.ops import handshake

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "calls.jsonl"
        hs = handshake([sys.executable, "-m", "lab_agent.mcp_server"], cwd=PROJECT_ROOT,
                       env_extra={"KB_SERVICE_PORT": str(_closed_port()),
                                  "LORE_MCP_LOG": str(log)}, timeout=120)
        logged = log.read_text(encoding="utf-8") if log.exists() else ""
    assert hs["ok"], f"handshake failed: {hs['error']}"
    assert '"tool": "status"' in logged, "the real server did not log the status call"
    assert set(hs["tools"]) == EXPECTED_TOOLS, hs["tools"]
    assert hs["has_instructions"], "the server's instructions did not reach the client"
    assert hs["status"] and "notebook_pages" in hs["status"], hs["status"]
    assert hs["status"]["graph_service"] == "not responding"


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
