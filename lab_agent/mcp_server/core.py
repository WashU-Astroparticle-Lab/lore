"""LORE's knowledge tools as plain functions; ``server.py`` exposes them over MCP.

An agent on another machine calls these while it drives hardware overnight, so three
properties are structural here rather than policy:

* **Read-only, with one queue.** Nothing in this package uploads, posts to Slack,
  fetches from LabArchives with cookies, or rebuilds the graph, and
  ``tests/test_mcp_server.py`` fails if any module that can do those things gets
  imported. The one write the agent can cause is ``publish_notes``, which drops its
  notes into ``agent_inbox`` on LORE's machine; LORE's listener, not this process,
  uploads them, only ever as a new, unreviewed page in the AI Agent folder. (The call
  log, ``calllog.py``, is the server's other write, to its own file.)
* **No credentials.** Only a whitelist of non-secret knowledge-base settings is read
  from ``.env``. This process never holds a GitHub token, LabArchives key or Slack token.
* **Never prints.** Over the stdio transport, stdout *is* the protocol channel, and a
  stray ``print`` corrupts it. Every tool runs with stdout pointed at stderr.

LORE's own unreviewed drafts (``[UNSIGNED]`` reports) are never returned. Prior AI
drafts can contain errors, and an agent that read them back as evidence would be citing
LORE's guesses as the lab's record.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from ..cli import ask
from ..config import ENV_PATH, KNOWLEDGE_ROOT, kb_dir

LA_DIR = KNOWLEDGE_ROOT / "labarchives"
EXPERIMENTS_DIR = KNOWLEDGE_ROOT / "experiments"

# The only settings this process may take from .env. Every credential is deliberately
# absent: nothing here needs one, so nothing here should be able to leak one.
SAFE_ENV_KEYS = ("KB_STORAGE_DIR", "KB_SERVICE_PORT", "KB_EMBEDDING_MODEL", "KB_REFRESH_HOUR",
                 "DR_DATA_PATH")

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
PAGE_TEXT_LIMIT = 60_000     # characters read_page returns before truncating
GRAPH_TIMEOUT_S = 180.0      # a graph answer makes a model call on LORE's side
MAX_K = 20
DR_MAX_HOURS = 72.0          # a window this long reads a few MB; longer is a report's job
DR_STALE_MINUTES = 15        # the log normally gains a row every few seconds

HUMAN_PAGE = "lab notebook page (written by people)"
MACHINE_SUMMARY = "LORE's summary of a past run (machine-written)"

_UNSIGNED = "[UNSIGNED]"
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")


def _quiet(fn):
    """Run ``fn`` with stdout pointed at stderr.

    The stdio transport captures the real stdout buffer when it starts, so pointing
    ``sys.stdout`` elsewhere here only diverts stray prints; it cannot break framing.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with contextlib.redirect_stdout(sys.stderr):
            return fn(*args, **kwargs)
    return wrapper


def load_safe_settings(env_path: Path | None = None) -> list[str]:
    """Copy the whitelisted non-secret settings from ``.env`` into the environment.

    Returns the keys it set. A value already in the environment wins, and a missing or
    unreadable ``.env`` is not an error: every setting has a working default.
    """
    try:
        from dotenv import dotenv_values
        values = dotenv_values(env_path or ENV_PATH)
    except Exception:  # noqa: BLE001 — no file, no permission, no python-dotenv: defaults
        return []
    applied = []
    for key in SAFE_ENV_KEYS:
        value = values.get(key)
        if value and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


def provenance(source: str, filename: str = "") -> str:
    """Who wrote a result, so the calling agent can weigh it."""
    if source.startswith("la_page:"):
        return HUMAN_PAGE
    if source.startswith("knowledge:"):
        return MACHINE_SUMMARY
    name = filename.lower()
    if name == "labarchives.md":
        return "notebook text fetched for a past run (written by people)"
    if name == "dr_conditions.md":
        return "dilution-refrigerator log summary for a past run (parsed data)"
    return "LORE's extraction for a past run (machine-written)"


def _norm(name: str) -> str:
    """The crawler's page-file naming rule, ``collect/la_crawl._safe``, case-folded.

    Duplicated rather than imported because ``la_crawl`` pulls in the LabArchives
    client. ``tests/test_mcp_server.py`` fails if the two ever disagree.
    """
    return _NON_ALNUM.sub("_", name).strip("_").lower()


def _clamp(k: int) -> int:
    return max(1, min(int(k), MAX_K))


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")


def _health(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 — refused, timed out, busy reloading: all "not responding"
        return False


@_quiet
def resolve(identifier: str, k: int = 6) -> dict:
    """Notebook pages that literally contain a chip, device, run or sample identifier."""
    text = identifier or ""
    tokens = ask._identifier_tokens(text)
    hits = ask.resolve_identifiers(text, _clamp(k))
    result = {
        "query": identifier,
        "identifiers_detected": tokens,
        "pages": [
            {"page": src, "provenance": provenance(src),
             "distinct_ids_matched": n, "occurrences": occ}
            for n, occ, src in hits
        ],
    }
    if not tokens:
        result["note"] = (
            "No identifier-shaped token found. Identifiers mix letters and digits "
            "(BE260416, JKID5x, WH2) or are six or more digits (20260702); measurement "
            "values like 40dB are ignored. Use search() for words."
        )
    elif not hits:
        result["note"] = "No notebook page contains that identifier."
    else:
        result["note"] = "Pass a page id to read_page() to read it."
    return result


@_quiet
def read_page(page: str) -> dict:
    """One crawled notebook page (or one of LORE's run summaries), verbatim."""
    raw = (page or "").strip()
    folder, label, prefix = LA_DIR, HUMAN_PAGE, "la_page:"
    if raw.startswith("knowledge:"):
        folder, label, prefix = EXPERIMENTS_DIR, MACHINE_SUMMARY, "knowledge:"
        raw = raw[len("knowledge:"):]
    elif raw.startswith("la_page:"):
        raw = raw[len("la_page:"):]

    key = _norm(raw)[:60]
    files = sorted(folder.glob("*.md")) if folder.is_dir() else []
    matches = [f for f in files if key and _norm(f.stem) == key]
    if not matches:
        partial = [f for f in files if key and key in _norm(f.stem)]
        if len(partial) != 1:
            return {
                "found": False,
                "requested": page,
                "candidates": [prefix + f.stem for f in partial[:15]],
                "note": ("Several pages match; call read_page() with one of these."
                         if partial else
                         "No page matches. Use resolve() for a chip or run ID, "
                         "or search() for words."),
            }
        matches = partial

    path = matches[0]
    # Only a file sitting directly in the corpus folder can ever be returned. Names are
    # matched against files that exist there, so this is a second line, not the first.
    if path.resolve().parent != folder.resolve():
        return {"found": False, "requested": page, "candidates": [],
                "note": "Refused: that path is outside LORE's corpus."}

    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "found": True,
        "page": prefix + path.stem,
        "provenance": label,
        "last_crawled": _mtime(path),
        "chars": len(text),
        "truncated": len(text) > PAGE_TEXT_LIMIT,
        "text": text[:PAGE_TEXT_LIMIT],
    }


SCOPES = ("all", "notebook", "runs")


def _in_scope(source: str, scope: str) -> bool:
    if scope == "notebook":
        return source.startswith("la_page:")
    if scope == "runs":
        return not source.startswith("la_page:")
    return True


@_quiet
def search(query: str, k: int = 6, scope: str = "all") -> dict:
    """Keyword search over notebook pages and past-run extractions, drafts excluded.

    ``scope`` narrows to people's notebook pages or to LORE's past-run material. The
    ranking favours dense machine extractions, so without it a question about what
    people observed can be answered entirely from LORE's own summaries.
    """
    k = _clamp(k)
    if scope not in SCOPES:
        scope = "all"
    results, skipped = [], 0
    # ask.search scores every document regardless of k, so asking for many costs
    # nothing extra and leaves room to filter.
    for score, src, fname, snippet in ask.search(query or "", 500):
        if not _in_scope(src, scope):
            continue
        if fname.startswith(_UNSIGNED):
            skipped += 1
            continue
        results.append({
            "source": src,
            "file": fname,
            "provenance": provenance(src, fname),
            "score": score,
            "snippet": snippet,
        })
        if len(results) >= k:
            break
    return {
        "query": query,
        "scope": scope,
        "results": results,
        "unreviewed_drafts_skipped": skipped,
        "note": ("Keyword match. Snippets are the best-matching lines; for a la_page result, "
                 "read_page() gives the whole page."),
    }


@_quiet
def ask_graph(question: str) -> dict:
    """A model-written answer from the knowledge graph, plus exact-match page candidates.

    The candidate pass always runs, because the graph cannot see bare identifiers: this
    is the same pairing ``cli/query_kb.py`` makes, which calling the service alone skips.
    """
    from ..rag import service

    candidates = [
        {"page": src, "match": tag, "provenance": provenance(src)}
        for src, tag in ask.candidate_pages(question or "", 6)
    ]
    answer = service.query_via_service(question or "", "hybrid", timeout=GRAPH_TIMEOUT_S)
    if answer is None:
        return {
            "available": False,
            "answer": None,
            "candidate_pages": candidates,
            "note": ("LORE's graph service did not answer. It runs inside LORE's Slack "
                     "listener, which may be down, still warming up (about 40 s after a "
                     "start) or reloading after the nightly refresh. resolve(), read_page() "
                     "and search() still work. Retry later or carry on without it."),
        }
    return {
        "available": True,
        # LightRAG's fixed reply when retrieval found nothing relevant.
        "graph_found_context": "[no-context]" not in answer,
        "answer": answer,
        "candidate_pages": candidates,
        "note": ("Written by a model from graph retrieval, not quoted notes. Treat numbers "
                 "as leads and confirm them with read_page() on the pages cited. "
                 "candidate_pages come from an exact-identifier and keyword pass."),
    }


@_quiet
def dr_status(hours: float = 2.0, now: datetime | None = None) -> dict:
    """The dilution refrigerator's thermometry over the last ``hours``, from its own logs."""
    from ..dr.live import recent_thermometry

    try:
        hours = max(0.1, min(float(hours), DR_MAX_HOURS))
    except (TypeError, ValueError):
        hours = 2.0
    data_path = os.environ.get("DR_DATA_PATH", "")
    if not data_path:
        return {"available": False,
                "note": "DR_DATA_PATH is not set on LORE's machine, so the fridge logs "
                        "cannot be found. Carry on without them."}

    end = now or datetime.now()
    result = recent_thermometry(data_path, end - timedelta(hours=hours), end)
    if "reason" in result:
        return {"available": False, "note": result["reason"]}

    warnings = []
    newest = result["newest_reading_at"]
    if newest is None:
        warnings.append(f"No thermometer reading in the last {hours:g} h. The fridge's "
                        "logging may have stopped; that says nothing about the fridge "
                        "itself. Check it directly or ask a person.")
    else:
        age_min = (end - datetime.fromisoformat(newest)).total_seconds() / 60
        if age_min > DR_STALE_MINUTES:
            warnings.append(f"The newest reading is {age_min:.0f} min old; the log "
                            "normally updates every few seconds, so logging may have "
                            "stopped. Treat these values as stale.")
    stale_other = [name for name, when in result["other_logs_last_written"].items()
                   if when is None or when < result["window_start"]]
    if stale_other:
        warnings.append("No pressure, flow or pulse-tube data: those logs ("
                        + ", ".join(stale_other) + ") have not been written during this "
                        "window. Only thermometry is available.")

    return {
        "available": result["found"],
        "units": "mK, as logged by the fridge software",
        "window": {"hours": hours, "start": result["window_start"], "end": result["window_end"]},
        "newest_reading_at": newest,
        "channels": result["channels"],
        "warnings": warnings,
        "source": {"files_read": result["files_read"],
                   "stub_files_skipped": result["stub_files_skipped"],
                   "other_logs_last_written": result["other_logs_last_written"]},
        "note": ("Read-only view of the fridge's own log on LORE's machine; channel names "
                 "come from the log header. The fridge's controls and alarms are "
                 "authoritative, not this. Labels with [MC] are on the mixing chamber."),
    }


@_quiet
def publish_notes(title: str, markdown: str, run: str = "") -> dict:
    """Queue the agent's notes for a new page in LabArchives' AI Agent folder.

    This server cannot upload: it holds no credentials. It writes the note to the
    inbox on LORE's machine and LORE's listener publishes it within about a minute.
    """
    from .. import agent_inbox
    from .calllog import caller

    record = agent_inbox.submit(title, markdown, run=run, caller=caller())
    if "error" in record:
        return {"queued": False, "note": record["error"]}
    state = record.get("state")
    return {
        "queued": state == "pending" and not record.get("duplicate"),
        "note_id": record["id"],
        "state": state,
        "page_title_will_be": record.get("page_title") or
                              f"[UNSIGNED] Agent notes {record['submitted_at'][:16].replace('T', ' ')}"
                              f" — {record['title']}",
        "note": ("Already submitted: these exact notes were sent before, so nothing new was "
                 "queued." if record.get("duplicate") else
                 "Queued. LORE's listener publishes it as a new page in the AI Agent folder "
                 "within about a minute, marked as unreviewed agent notes. Call "
                 "notes_status(note_id) to confirm it landed."),
    }


@_quiet
def notes_status(note_id: str = "") -> dict:
    """Where a submitted note is: pending, published (and where), or failed (and why)."""
    from .. import agent_inbox

    def view(r: dict) -> dict:
        keep = ("id", "state", "title", "run", "submitted_at", "attempts", "last_error",
                "published_at", "page_title", "folder", "notebook", "notebook_url")
        return {k: r[k] for k in keep if r.get(k) not in (None, "")}

    if note_id:
        record = agent_inbox.read(note_id.strip())
        if record is None:
            return {"found": False, "note": f"No note with id {note_id!r}."}
        out = {"found": True, **view(record)}
        if record["state"] == "pending":
            out["note"] = ("Waiting for LORE's listener to upload it. If it stays pending for "
                           "more than a few minutes the listener may be down; the note is kept "
                           "and will be published when it is back.")
        elif record["state"] == "failed":
            out["note"] = "LORE gave up publishing this note; last_error says why. Tell a person."
        else:
            out["note"] = ("Published. There is no per-page link: open the notebook and find "
                           "the page by its title in the folder named.")
        return out
    return {"recent": [view(r) for r in agent_inbox.recent(10)]}


@_quiet
def status() -> dict:
    """What LORE knows, how fresh it is, and what it cannot see."""
    from ..rag import service

    port = service.service_port()
    kb = kb_dir()
    manifest = kb / "kb_manifest.json"
    pages_in_graph = None
    if manifest.is_file():
        try:
            pages_in_graph = len(json.loads(manifest.read_text(encoding="utf-8")).get("docs", {}))
        except (OSError, ValueError):
            pass
    page_files = sorted(LA_DIR.glob("*.md")) if LA_DIR.is_dir() else []
    newest = max(page_files, key=lambda p: p.stat().st_mtime) if page_files else None
    model = os.environ.get("KB_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    try:
        refresh_hour: int | str = int(os.environ.get("KB_REFRESH_HOUR", "2"))
    except ValueError:
        refresh_hour = os.environ.get("KB_REFRESH_HOUR", "")

    limits = [
        "Knowledge is only as fresh as the last crawl; notes written after it are not visible.",
        "The graph covers notebook pages only, not Slack or LORE's own reports.",
        "Apart from the dilution refrigerator's thermometry log (dr_status), LORE cannot "
        "see instruments or raw data files, only what people wrote down.",
    ]
    if model == DEFAULT_EMBEDDING_MODEL:
        limits.append("The current embedding model reads only the opening of each passage "
                      "(about 200 tokens) for semantic matching. resolve() and search() "
                      "are unaffected.")
    return {
        "server": "LORE knowledge server (read-only)",
        "graph_service": "up" if _health(port) else "not responding",
        "notebook_pages": len(page_files),
        "notebook_last_crawled": _mtime(newest) if newest else None,
        "pages_in_graph": pages_in_graph,
        "graph_last_updated": _mtime(manifest) if manifest.is_file() else None,
        "nightly_refresh_hour": refresh_hour,
        "embedding_model": model,
        "known_limits": limits,
        "note": ("graph_service 'not responding' can mean the listener is down, warming up "
                 "or reloading; the other tools do not depend on it."),
    }
