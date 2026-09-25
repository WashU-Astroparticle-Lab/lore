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
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

import markdown as _md

from .. import agent_inbox
from . import labarchives as la

PAGE_PREFIX = "[UNSIGNED] Agent notes"


def page_title(record: dict) -> str:
    stamp = (record.get("submitted_at") or datetime.now().isoformat())[:16].replace("T", " ")
    return f"{PAGE_PREFIX} {stamp} — {record['title']}"


def render(record: dict, markdown: str) -> str:
    """The page's HTML: a provenance banner, then the notes."""
    # The notes come from an AI on another machine: escape "<" so raw HTML in them is
    # shown as text, never rendered into the lab's notebook. Markdown needs no "<".
    safe = markdown.replace("&", "&amp;").replace("<", "&lt;")
    body = _md.markdown(safe, extensions=["tables", "fenced_code", "nl2br"])
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


def publish_note(record: dict, markdown: str) -> dict:
    """Create the page and post the note. Returns where it landed."""
    adapter = la._get_adapter()
    nbid, folder_tree_id = la._find_upload_folder(adapter)
    title = page_title(record)
    page_tree_id = la._insert_page(nbid, folder_tree_id, title)
    la._add_text_entry(nbid, page_tree_id, render(record, markdown))
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / f"{record['id']}.md"
        raw.write_text(markdown, encoding="utf-8")
        la._add_attachment(nbid, page_tree_id, raw)
    return {"page_title": title, "folder": la._upload_folder_name(),
            "notebook": la._target_notebook_name(), "notebook_url": la._notebook_url(nbid)}


def process_inbox(publish: Callable[[dict, str], dict] = publish_note) -> list[str]:
    """Publish every pending note once. Returns one log line per note handled.

    A failure is counted and retried on the next pass; after MAX_ATTEMPTS the note is
    moved to failed/ with the error, where the agent's status call will find it.
    """
    lines = []
    now = datetime.now()
    for record, markdown in agent_inbox.pending():
        last = record.get("last_attempt_at")
        if last:
            # Back off 2, 4, 8, 16 min, so a LabArchives outage of up to half an hour
            # is ridden out rather than burning all attempts in two minutes.
            wait_s = 60 * 2 ** record.get("attempts", 0)
            if (now - datetime.fromisoformat(last)).total_seconds() < wait_s:
                continue
        try:
            where = publish(record, markdown)
        except Exception as exc:  # noqa: BLE001 — one bad note must not stop the others
            state = agent_inbox.mark_failed_attempt(record, f"{type(exc).__name__}: {exc}")
            lines.append(f"{record['id']} attempt failed ({state}): {exc}")
            continue
        agent_inbox.mark_published(record, where)
        lines.append(f"{record['id']} published: {where['page_title']}")
    return lines
