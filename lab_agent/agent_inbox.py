"""The queue between a measurement agent's notes and LORE's LabArchives upload.

The MCP server (``mcp_server``) accepts notes from the agent on another machine but
holds no credentials and cannot upload, by design. It writes each note here. The Slack
listener, which runs on LORE's machine with the LabArchives keys, publishes what is
pending (``publish/agent_notes.py``) and records the outcome here, where the agent can
read it back through the server.

Standard library only, no network, no credentials: both sides import it.

    <inbox>/pending/<id>.md + <id>.json     submitted, not yet published
    <inbox>/published/<id>.md + <id>.json   published; the json says where
    <inbox>/failed/<id>.md + <id>.json      gave up after MAX_ATTEMPTS; the json says why

    LORE_AGENT_INBOX   path of the inbox (default: agent_inbox/ in the project, gitignored)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT

STATES = ("pending", "published", "failed")
MAX_NOTE_CHARS = 200_000
MAX_TITLE_CHARS = 120
MAX_PENDING = 20        # an agent in a loop must not be able to fill the notebook
MAX_ATTEMPTS = 5


def inbox() -> Path:
    return Path(os.environ.get("LORE_AGENT_INBOX") or PROJECT_ROOT / "agent_inbox")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def clean_title(title: str) -> str:
    """One line, no control characters, bounded length."""
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"[\x00-\x1f\x7f]", "", t)
    return t[:MAX_TITLE_CHARS].rstrip()


def note_id(markdown: str, title: str) -> str:
    """Stable for the same notes, so an agent retrying a submit does not double-post."""
    digest = hashlib.sha256(f"{title}\n{markdown}".encode("utf-8")).hexdigest()[:12]
    return f"note-{digest}"


def _paths(state: str, nid: str) -> tuple[Path, Path]:
    folder = inbox() / state
    return folder / f"{nid}.md", folder / f"{nid}.json"


def read(nid: str) -> dict | None:
    """A note's record, whichever state it is in, or None."""
    if not re.fullmatch(r"note-[0-9a-f]{12}", nid or ""):
        return None
    for state in STATES:
        md, meta = _paths(state, nid)
        if meta.exists():
            record = json.loads(meta.read_text(encoding="utf-8"))
            record["state"] = state
            return record
    return None


def submit(title: str, markdown: str, run: str = "", caller: str = "") -> dict:
    """Queue a note for publication. Returns its record, or {"error": ...}."""
    title = clean_title(title)
    if not title:
        return {"error": "A title is required."}
    if not (markdown or "").strip():
        return {"error": "The notes are empty."}
    if len(markdown) > MAX_NOTE_CHARS:
        return {"error": f"The notes are {len(markdown)} characters; the limit is "
                         f"{MAX_NOTE_CHARS}. Split them or trim raw output."}

    nid = note_id(markdown, title)
    existing = read(nid)
    if existing is not None and existing["state"] == "failed":
        # A note that gave up can be sent again: it used to be "Already submitted" forever.
        _requeue_failed(existing)
        existing = read(nid)
        existing["requeued"] = True
        return existing
    if existing is not None:
        existing["duplicate"] = True
        return existing

    pending = inbox() / "pending"
    if pending.is_dir() and len(list(pending.glob("*.json"))) >= MAX_PENDING:
        return {"error": f"{MAX_PENDING} notes are already waiting to be published, so "
                         "LORE's uploader is probably not running. Nothing was queued; "
                         "keep your notes locally and try again later."}

    record = {"id": nid, "title": title, "run": clean_title(run), "caller": caller,
              "submitted_at": _now(), "chars": len(markdown), "attempts": 0}
    pending.mkdir(parents=True, exist_ok=True)
    md, meta = _paths("pending", nid)
    md.write_text(markdown, encoding="utf-8")
    _write_json(meta, record)
    record["state"] = "pending"
    return record


def _write_json(path: Path, record: dict) -> None:
    """Write a record atomically, so a reader never sees half a file."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: v for k, v in record.items() if k != "state"}, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def save_pending(record: dict) -> None:
    """Persist a pending note's progress (which publishing steps are done)."""
    _write_json(_paths("pending", record["id"])[1], record)


def pending() -> list[tuple[dict, str]]:
    """(record, markdown) for every note waiting, oldest first.

    A record whose notes are gone is what an interrupted move leaves behind: if the note
    reached published/ or failed/, the stray record is removed; otherwise it is skipped.
    It used to raise FileNotFoundError on every pass, and no note was published again.
    """
    folder = inbox() / "pending"
    out = []
    for meta in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # being written, or unreadable: next pass
        md = meta.with_suffix(".md")
        if not md.exists():
            if any(_paths(s, record.get("id", ""))[0].exists() for s in ("published", "failed")):
                meta.unlink(missing_ok=True)
            continue
        out.append((record, md.read_text(encoding="utf-8")))
    return sorted(out, key=lambda rm: rm[0].get("submitted_at", ""))


def _move(record: dict, to_state: str) -> None:
    """Move a pending note to ``to_state``: record first, then notes, then the old record.

    In that order an interruption at any point leaves a state pending() can finish, not
    a record without its notes.
    """
    src_md, src_meta = _paths("pending", record["id"])
    dst_md, dst_meta = _paths(to_state, record["id"])
    dst_meta.parent.mkdir(parents=True, exist_ok=True)
    _write_json(dst_meta, record)
    if src_md.exists():
        os.replace(src_md, dst_md)
    src_meta.unlink(missing_ok=True)


def _requeue_failed(record: dict) -> None:
    src_md, src_meta = _paths("failed", record["id"])
    dst_md, dst_meta = _paths("pending", record["id"])
    dst_meta.parent.mkdir(parents=True, exist_ok=True)
    fresh = {k: v for k, v in record.items()
             if k not in ("state", "attempts", "last_error", "last_attempt_at")}
    fresh.update(attempts=0, requeued_at=_now())
    _write_json(dst_meta, fresh)
    if src_md.exists():
        os.replace(src_md, dst_md)
    src_meta.unlink(missing_ok=True)


def mark_published(record: dict, where: dict) -> None:
    record = dict(record, published_at=record.get("published_at") or _now(), **where)
    for k in ("last_error", "published_where", "page_title_pending"):
        record.pop(k, None)
    _move(record, "published")


def mark_failed_attempt(record: dict, error: str) -> str:
    """Count a failed upload; after MAX_ATTEMPTS the note moves to failed/. Returns its state."""
    record = dict(record, attempts=record.get("attempts", 0) + 1, last_error=error[:500],
                  last_attempt_at=_now())
    if record["attempts"] >= MAX_ATTEMPTS:
        _move(record, "failed")
        return "failed"
    save_pending(record)
    return "pending"


def recent(limit: int = 10) -> list[dict]:
    """The latest notes in any state, newest first."""
    records = []
    for state in STATES:
        folder = inbox() / state
        for meta in folder.glob("*.json") if folder.is_dir() else []:
            record = json.loads(meta.read_text(encoding="utf-8"))
            record["state"] = state
            records.append(record)
    return sorted(records, key=lambda r: r.get("submitted_at", ""), reverse=True)[:limit]
