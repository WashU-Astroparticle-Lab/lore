"""A record of every tool call the MCP server answers, kept on LORE's machine.

The answers go back over SSH to the calling agent, so without this LORE's side keeps
nothing: after the first overnight run the only trace of what the measurement agent
asked was two ask_graph questions left in LightRAG's cache. One JSON line per call:
when, which tool, the arguments, which machine asked (from SSH_CLIENT), how long it
took, and the full result as the agent received it.

This is the server's only write, and it is to its own log. It never touches stdout
(the protocol channel), and a failure to log never fails a call: the agent gets its
answer either way.

    LORE_MCP_LOG   path of the log (default: session_logs/mcp_calls.jsonl, gitignored)
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from ..config import PROJECT_ROOT

MAX_BYTES = 20 * 1024 * 1024   # then the log moves to .1 (replacing the previous one)

# One server process serves one SSH connection, so this groups a session's calls.
SESSION = uuid.uuid4().hex[:8]
_lock = threading.Lock()


def log_path() -> Path:
    return Path(os.environ.get("LORE_MCP_LOG") or PROJECT_ROOT / "session_logs" / "mcp_calls.jsonl")


def caller() -> str:
    """The calling machine's address: sshd sets SSH_CLIENT to "ip port localport"."""
    parts = (os.environ.get("SSH_CLIENT") or "").split()
    return parts[0] if parts else "local"


def record(tool: str, args: dict, result=None, error: str | None = None,
           seconds: float = 0.0) -> None:
    """Append one call to the log. Never raises."""
    try:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "session": SESSION,
            "caller": caller(),
            "tool": tool,
            "args": args,
            "seconds": round(seconds, 3),
        }
        if error is not None:
            entry["error"] = error
        else:
            entry["result"] = result
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        path = log_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # noqa: BLE001 — logging must never cost the agent its answer
        print(f"[lore-mcp] could not write the call log: {exc}", file=sys.stderr)
