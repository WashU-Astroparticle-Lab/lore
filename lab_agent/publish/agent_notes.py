"""Publish a measurement agent's notes to LabArchives, as unreviewed agent notes.

The agent on the DAQ PC submits notes through LORE's MCP server, which only queues
them (``agent_inbox``). The listener calls ``process_inbox`` every half minute; this
module turns each pending note into a NEW page in the configured upload folder
("AI Agent"), headed by a banner that says who wrote it and that nobody reviewed it,
with the raw markdown attached.

Deliberately narrower than ``upload_report``: always a new page (never a revision
appended to an existing page, which could be a person's), only in the upload folder,
no images, and raw HTML in the notes is shown as text rather than rendered.
"""
from __future__ import annotations

import html as _html
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import markdown as _md

from .. import agent_inbox
from . import labarchives as la

PAGE_PREFIX = "[UNSIGNED] Agent notes"
# The most notes published per pass and per hour. MAX_PENDING bounded only the queue: a
# looping agent could keep it full and get every one of them published every 30 s.
MAX_PER_PASS = 3
MAX_PER_HOUR = 20

# Fenced blocks and inline code spans: markdown escapes these itself.
_CODE = re.compile(r"(^(?:```|~~~)[^\n]*\n.*?^(?:```|~~~)[ \t]*$|`[^`\n]+`)", re.M | re.S)
_IMG = re.compile(r'<img\b[^>]*?alt="([^"]*)"[^>]*>|<img\b[^>]*>')
_HREF = re.compile(r'<a href="([^"]*)"')


def page_title(record: dict) -> str:
    stamp = (record.get("submitted_at") or datetime.now().isoformat())[:16].replace("T", " ")
    return f"{PAGE_PREFIX} {stamp} — {record['title']}"


def _escape_outside_code(markdown: str) -> str:
    """Escape "&" and "<" except inside code, which markdown escapes on its own.

    Escaping everything first double-escaped code: `Qi < 1e5` reached the page as
    "Qi &lt; 1e5" written out literally.
    """
    parts = _CODE.split(markdown)
    return "".join(p if i % 2 else p.replace("&", "&amp;").replace("<", "&lt;")
                   for i, p in enumerate(parts))


def _safe_html(html: str) -> str:
    """No images (the notes are text only), and no link that is not http(s) or mailto."""
    html = _IMG.sub(lambda m: f"[image: {m.group(1)}]" if m.group(1) else "[image removed]", html)
    return _HREF.sub(lambda m: m.group(0) if re.match(r"(https?:|mailto:)", m.group(1), re.I)
                     else "<a", html)


def render(record: dict, markdown: str) -> str:
    """The page's HTML: a provenance banner, then the notes."""
    # The notes come from an AI on another machine: raw HTML in them is shown as text,
    # never rendered into the lab's notebook, and the result keeps no images or scripts.
    body = _safe_html(_md.markdown(_escape_outside_code(markdown),
                                   extensions=["tables", "fenced_code", "nl2br"]))
    who = _html.escape(record.get("caller") or "unknown machine")
    run = f" for run <strong>{_html.escape(record['run'])}</strong>" if record.get("run") else ""
    banner = (
        '<p style="border:1px solid #c80;padding:6px;background:#fff6e5">'
        f"<strong>Written by the measurement agent</strong> (AI){run}, submitted from {who} "
        f"at {_html.escape(record.get('submitted_at', ''))}. "
        "<strong>Not reviewed</strong> by LORE's critic or by a person. Values are the "
        "agent's own; check anything that matters against the data before relying on it."
        "</p>"
    )
    return banner + "\n" + body


def publish_note(record: dict, markdown: str, save: Callable[[dict], None] = lambda r: None,
                 ) -> dict:
    """Create the page and post the note. Returns where it landed.

    Each step is recorded on the note (``save``) as it succeeds, and a retry skips what is
    done. Every attempt used to create the page again, so an entry that timed out after
    the page existed left up to five pages behind, empty or duplicate.
    """
    adapter = la._get_adapter()
    nbid, folder_tree_id = la._find_upload_folder(adapter)
    title = record.get("page_title_pending") or page_title(record)
    page_tree_id = record.get("page_tree_id")
    if not page_tree_id:
        page_tree_id = la._insert_page(nbid, folder_tree_id, title)
        record.update(page_tree_id=page_tree_id, page_title_pending=title)
        save(record)
    if not record.get("text_added"):
        la._add_text_entry(nbid, page_tree_id, render(record, markdown))
        record["text_added"] = True
        save(record)
    if not record.get("attachment_added"):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / f"{record['id']}.md"
            raw.write_text(markdown, encoding="utf-8")
            la._add_attachment(nbid, page_tree_id, raw)
        record["attachment_added"] = True
        save(record)
    return {"page_title": title, "folder": la._upload_folder_name(),
            "notebook": la._target_notebook_name(), "notebook_url": la._notebook_url(nbid)}


def _published_last_hour(now: datetime) -> int:
    since = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    return sum(1 for r in agent_inbox.recent(limit=10_000)
               if r.get("state") == "published" and (r.get("published_at") or "") >= since)


def process_inbox(publish: Callable[..., dict] = publish_note) -> list[str]:
    """Publish pending notes, a few per pass. Returns one log line per note handled.

    A failure is counted and retried on the next pass; after MAX_ATTEMPTS the note is
    moved to failed/ with the error, where the agent's status call will find it. A note
    already published whose move to published/ failed is only moved, not published again.
    """
    lines = []
    now = datetime.now()
    budget = min(MAX_PER_PASS, MAX_PER_HOUR - _published_last_hour(now))
    for record, markdown in agent_inbox.pending():
        if record.get("published_where"):
            lines.append(_finish(record, record["published_where"]))
            continue
        if budget <= 0:
            lines.append(f"{len(agent_inbox.pending())} note(s) waiting: publishing limit "
                         f"({MAX_PER_PASS} per pass, {MAX_PER_HOUR} per hour) reached")
            break
        last = record.get("last_attempt_at")
        if last:
            # Back off 2, 4, 8, 16 min, so a LabArchives outage of up to half an hour
            # is ridden out rather than burning all attempts in two minutes.
            wait_s = 60 * 2 ** record.get("attempts", 0)
            if (now - datetime.fromisoformat(last)).total_seconds() < wait_s:
                continue
        budget -= 1
        try:
            if publish is publish_note:
                where = publish(record, markdown, save=agent_inbox.save_pending)
            else:
                where = publish(record, markdown)
        except Exception as exc:  # noqa: BLE001 — one bad note must not stop the others
            state = agent_inbox.mark_failed_attempt(record, f"{type(exc).__name__}: {exc}")
            lines.append(f"{record['id']} attempt failed ({state}): {exc}")
            continue
        # Record success before moving: if the move fails, the next pass only moves it.
        record["published_where"] = where
        agent_inbox.save_pending(record)
        lines.append(_finish(record, where))
    return lines


def _finish(record: dict, where: dict) -> str:
    try:
        agent_inbox.mark_published(record, where)
    except OSError as exc:  # e.g. the MCP server reading the file right now (WinError 32)
        return f"{record['id']} published, move to published/ failed ({exc}); retried next pass"
    return f"{record['id']} published: {where['page_title']}"
