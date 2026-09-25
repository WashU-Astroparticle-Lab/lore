"""The measurement agent's notes: queued by the MCP server, published by the listener.

No test here reaches LabArchives: publishing is exercised with a stand-in, because a
probe that wrote to the real notebook once rode along in the test glob 27 times.

Run: python tests/run_all.py agent_notes   (or: python tests/test_agent_notes.py)
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from lab_agent import agent_inbox
from lab_agent.publish import agent_notes

NOTES = "# Night 1\n\n| P (dBm) | Qi |\n|---|---|\n| -40 | 1.2e5 |\n\nQi measured; Qc inferred."


@contextlib.contextmanager
def _inbox():
    saved = os.environ.get("LORE_AGENT_INBOX")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LORE_AGENT_INBOX"] = tmp
        try:
            yield Path(tmp)
        finally:
            if saved is None:
                os.environ.pop("LORE_AGENT_INBOX", None)
            else:
                os.environ["LORE_AGENT_INBOX"] = saved


def test_a_note_is_queued_once_and_checked_first():
    with _inbox() as box:
        r = agent_inbox.submit("  JPL QPD\nnight 1 ", NOTES, run="PRIMA_20260831", caller="10.232.129.236")
        assert r["state"] == "pending" and r["title"] == "JPL QPD night 1", r
        assert (box / "pending" / f"{r['id']}.md").read_text(encoding="utf-8") == NOTES
        again = agent_inbox.submit("JPL QPD night 1", NOTES)
        assert again.get("duplicate") and again["id"] == r["id"], "a retry must not double-post"
        assert len(list((box / "pending").glob("*.json"))) == 1
        assert "error" in agent_inbox.submit("", NOTES)
        assert "error" in agent_inbox.submit("t", "   ")
        assert "error" in agent_inbox.submit("t", "x" * (agent_inbox.MAX_NOTE_CHARS + 1))
        assert agent_inbox.read("../../etc/passwd") is None, "ids are never paths"


def test_a_full_queue_refuses_rather_than_piling_up():
    with _inbox():
        for i in range(agent_inbox.MAX_PENDING):
            assert "error" not in agent_inbox.submit(f"note {i}", f"text {i}")
        refused = agent_inbox.submit("one more", "text")
        assert "error" in refused and "not running" in refused["error"], refused


def test_the_listener_publishes_pending_notes_and_records_where():
    posted = []

    def fake_publish(record, markdown):
        posted.append((record["id"], markdown))
        return {"page_title": agent_notes.page_title(record), "folder": "AI Agent",
                "notebook": "Qubit & KID", "notebook_url": "https://example.invalid/nb"}

    with _inbox():
        r = agent_inbox.submit("night 1", NOTES, run="PRIMA_20260831")
        lines = agent_notes.process_inbox(publish=fake_publish)
        assert posted == [(r["id"], NOTES)] and "published" in lines[0], lines
        done = agent_inbox.read(r["id"])
        assert done["state"] == "published" and done["folder"] == "AI Agent", done
        assert done["page_title"].startswith("[UNSIGNED] Agent notes ") and done["page_title"].endswith("— night 1")
        assert agent_notes.process_inbox(publish=fake_publish) == [], "published twice"


def test_a_failing_upload_backs_off_then_gives_up_visibly():
    def broken(record, markdown):
        raise RuntimeError("LabArchives 503")

    with _inbox() as box:
        r = agent_inbox.submit("night 1", NOTES)
        agent_notes.process_inbox(publish=broken)
        rec = agent_inbox.read(r["id"])
        assert rec["state"] == "pending" and rec["attempts"] == 1 and "503" in rec["last_error"]
        assert agent_notes.process_inbox(publish=broken) == [], "retried without backing off"
        for _ in range(agent_inbox.MAX_ATTEMPTS):
            meta = box / "pending" / f"{r['id']}.json"
            if not meta.exists():
                break
            m = json.loads(meta.read_text(encoding="utf-8"))
            m["last_attempt_at"] = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
            meta.write_text(json.dumps(m), encoding="utf-8")
            agent_notes.process_inbox(publish=broken)
        rec = agent_inbox.read(r["id"])
        assert rec["state"] == "failed" and rec["attempts"] == agent_inbox.MAX_ATTEMPTS, rec


def test_the_page_says_who_wrote_it_and_cannot_carry_html():
    record = {"id": "note-0123456789ab", "title": "night 1", "run": "PRIMA_20260831",
              "caller": "10.232.129.236", "submitted_at": "2026-09-25T02:10:00"}
    html = agent_notes.render(record, NOTES + "\n\n<script>alert(1)</script> <img src=x>")
    assert "Written by the measurement agent" in html and "Not reviewed" in html
    assert "10.232.129.236" in html and "PRIMA_20260831" in html
    assert "<script>" not in html and "<img" not in html, "raw HTML from the agent was rendered"
    assert "&lt;script&gt;" in html
    assert "<table>" in html, "markdown tables should still render"
    assert agent_notes.page_title(record) == "[UNSIGNED] Agent notes 2026-09-25 02:10 — night 1"


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
