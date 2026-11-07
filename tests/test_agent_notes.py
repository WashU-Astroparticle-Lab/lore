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


def test_code_is_not_double_escaped_and_links_and_images_are_defused():
    record = {"id": "note-0123456789ab", "title": "t", "submitted_at": "2026-09-25T02:10:00"}
    notes = ("Cut at `Qi < 1e5 & Qc > 1e4`.\n\n```\nif a < b && c:\n```\n\n"
             "[click](javascript:alert(1)) [docs](https://example.org) ![plot](http://ext/i.png?d=x)")
    html = agent_notes.render(record, notes)
    assert "<code>Qi &lt; 1e5 &amp; Qc &gt; 1e4</code>" in html, html
    assert "&amp;lt;" not in html and "&amp;amp;" not in html, "code was escaped twice"
    assert "javascript:" not in html and 'href="https://example.org"' in html
    assert "<img" not in html and "[image: plot]" in html


def _fake_la(calls, fail_attachment_times=0):
    """A stand-in for the LabArchives calls publish_note makes, failing the attachment n times."""
    left = {"n": fail_attachment_times}

    class LA:
        @staticmethod
        def _get_adapter():
            return None

        @staticmethod
        def _find_upload_folder(adapter):
            return "NB", "FOLDER"

        @staticmethod
        def _insert_page(nbid, folder, title):
            calls.append("page")
            return f"PAGE{len([c for c in calls if c == 'page'])}"

        @staticmethod
        def _add_text_entry(nbid, page, html):
            calls.append(("text", page))

        @staticmethod
        def _add_attachment(nbid, page, path):
            if left["n"] > 0:
                left["n"] -= 1
                raise TimeoutError("LabArchives 30 s timeout")
            calls.append(("attachment", page))

        _upload_folder_name = staticmethod(lambda: "AI Agent")
        _target_notebook_name = staticmethod(lambda: "Qubit & KID")
        _notebook_url = staticmethod(lambda nbid: "https://example/nb")

    return LA


def test_a_retry_finishes_the_same_page_instead_of_making_another():
    calls: list = []
    saved = agent_notes.la
    with _inbox():
        agent_notes.la = _fake_la(calls, fail_attachment_times=1)
        try:
            r = agent_inbox.submit("night 1", NOTES)
            agent_notes.process_inbox()                      # page + text, attachment times out
            rec = agent_inbox.read(r["id"])
            rec["last_attempt_at"] = (datetime.now() - timedelta(hours=1)).isoformat()
            agent_inbox.save_pending(rec)
            agent_notes.process_inbox()                      # retry: attachment only
        finally:
            agent_notes.la = saved
        assert calls.count("page") == 1, f"a retry created another page: {calls}"
        assert calls.count(("text", "PAGE1")) == 1 and ("attachment", "PAGE1") in calls, calls
        assert agent_inbox.read(r["id"])["state"] == "published"


def test_an_interrupted_move_is_finished_not_republished():
    published = []

    def fake_publish(record, markdown):
        published.append(record["id"])
        return {"page_title": "p", "folder": "AI Agent", "notebook": "nb", "notebook_url": "u"}

    with _inbox() as box:
        r = agent_inbox.submit("night 1", NOTES)
        saved_move = agent_inbox._move

        def broken_move(*a, **k):
            raise PermissionError("[WinError 32] being used by another process")

        agent_inbox._move = broken_move
        try:
            lines = agent_notes.process_inbox(publish=fake_publish)
        finally:
            agent_inbox._move = saved_move
        assert "retried next pass" in lines[0], lines
        agent_notes.process_inbox(publish=fake_publish)
        assert published == [r["id"]], "the note was published twice"
        assert agent_inbox.read(r["id"])["state"] == "published"
        # A record left without its notes (the move got halfway) must not stall the queue.
        (box / "pending").mkdir(exist_ok=True)
        (box / "pending" / f"{r['id']}.json").write_text(json.dumps({"id": r["id"]}))
        assert agent_inbox.pending() == []
        assert not (box / "pending" / f"{r['id']}.json").exists(), "the stray record stays"


def test_a_looping_agent_cannot_publish_everything_at_once():
    published = []

    def fake_publish(record, markdown):
        published.append(record["id"])
        return {"page_title": "p", "folder": "f", "notebook": "n", "notebook_url": "u"}

    with _inbox():
        for i in range(10):
            agent_inbox.submit(f"note {i}", f"{NOTES}\n{i}")
        agent_notes.process_inbox(publish=fake_publish)
        assert len(published) == agent_notes.MAX_PER_PASS, published


def test_a_failed_note_can_be_sent_again():
    with _inbox():
        r = agent_inbox.submit("night 1", NOTES)
        rec = agent_inbox.read(r["id"])
        rec["attempts"] = agent_inbox.MAX_ATTEMPTS - 1
        agent_inbox.mark_failed_attempt(rec, "LabArchives down")
        assert agent_inbox.read(r["id"])["state"] == "failed"
        again = agent_inbox.submit("night 1", NOTES)
        assert again.get("requeued") and again["state"] == "pending" and again["attempts"] == 0


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
