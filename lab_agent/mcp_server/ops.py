"""Operator commands for setting LORE's MCP server up and debugging the connection.

These print to the terminal for a person to read. They are never part of the server
itself, which must keep stdout clean.

    --selftest [ID] [--graph]   exercise every tool on this machine, then a real handshake
    --write-launcher [PUBKEY]   write the launcher the SSH forced command runs
    --probe CMD ...             run CMD as an MCP server and talk to it, e.g. over ssh
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..config import PROJECT_ROOT


def _leaf(exc: BaseException) -> BaseException:
    """The first real error inside an exception group, which is what a person needs.

    Duck-typed on ``.exceptions`` so it works on Python 3.10, where ExceptionGroup is
    not a builtin.
    """
    while getattr(exc, "exceptions", None):
        exc = exc.exceptions[0]
    return exc


def handshake(command: list[str], cwd: Path | None = None, env_extra: dict | None = None,
              timeout: float = 90.0, call_status: bool = True) -> dict:
    """Start ``command`` as an MCP server over stdio and run a real session against it.

    Initialize, list the tools, and optionally call ``status``. This is the same
    exchange Claude Code performs, so passing here means the transport, the framing and
    the server all work, with nothing in between left to guess at.
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

    env = get_default_environment()
    env.update(env_extra or {})
    params = StdioServerParameters(command=command[0], args=command[1:], env=env,
                                   cwd=str(cwd) if cwd else None)

    async def session_run() -> dict:
        with anyio.fail_after(timeout):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    listed = await session.list_tools()
                    status = None
                    if call_status:
                        res = await session.call_tool("status", {})
                        status = res.structuredContent
                        if status is None and res.content:
                            try:
                                status = json.loads(res.content[0].text)
                            except (AttributeError, ValueError):
                                status = {"raw": getattr(res.content[0], "text", "")}
                    return {
                        "ok": True,
                        "server": init.serverInfo.name,
                        "tools": sorted(t.name for t in listed.tools),
                        "has_instructions": bool(init.instructions),
                        "status": status,
                        "error": None,
                    }

    try:
        return anyio.run(session_run)
    except BaseException as exc:  # noqa: BLE001 — anyio can wrap errors in groups
        leaf = _leaf(exc)
        if isinstance(leaf, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(leaf, TimeoutError):
            return {"ok": False, "error": f"no complete MCP exchange within {timeout:.0f} s"}
        return {"ok": False, "error": f"{type(leaf).__name__}: {leaf}"}


def selftest(identifier: str = "BE260416", graph: bool = False) -> int:
    """Run every tool against this machine's real corpus, then a real MCP handshake."""
    from . import core

    core.load_safe_settings()
    problems: list[str] = []

    st = core.status()
    print("== status ==")
    for key in ("notebook_pages", "notebook_last_crawled", "pages_in_graph",
                "graph_last_updated", "graph_service", "embedding_model",
                "nightly_refresh_hour"):
        print(f"  {key}: {st.get(key)}")
    if not st["notebook_pages"]:
        problems.append("no crawled notebook pages under knowledge/labarchives "
                        "(run: python -m lab_agent.cli.build_kb)")

    print(f"\n== resolve({identifier!r}) ==")
    r = core.resolve(identifier)
    for p in r["pages"][:5]:
        print(f"  {p['page']}  (ids matched: {p['distinct_ids_matched']}, "
              f"occurrences: {p['occurrences']})")
    if not r["pages"]:
        print(f"  (none) {r.get('note', '')}")
    else:
        first = r["pages"][0]["page"]
        pg = core.read_page(first)
        print(f"\n== read_page({first!r}) ==")
        print(f"  found: {pg['found']}, chars: {pg.get('chars')}, "
              f"last crawled: {pg.get('last_crawled')}")
        if not pg["found"]:
            problems.append(f"read_page could not open {first}, which resolve() returned")

    print("\n== search('LED') ==")
    s = core.search("LED", 3)
    for h in s["results"]:
        print(f"  {h['source']} / {h['file']}  [{h['provenance']}]")
    print(f"  unreviewed drafts skipped: {s['unreviewed_drafts_skipped']}")

    if graph:
        print("\n== ask_graph (slow: one model call on this machine) ==")
        g = core.ask_graph("Which QPD chips have we measured, and at what frequencies?")
        print(f"  available: {g['available']}")
        if g["available"]:
            print(f"  found context: {g['graph_found_context']}")
            print("  " + (g["answer"] or "")[:300].replace("\n", " ") + " ...")
        else:
            print(f"  {g['note']}")

    print("\n== MCP handshake: a fresh server process, over stdio ==")
    hs = handshake([sys.executable, "-m", "lab_agent.mcp_server"], cwd=PROJECT_ROOT)
    if hs["ok"]:
        print(f"  server: {hs['server']}   tools: {', '.join(hs['tools'])}")
    else:
        problems.append(f"MCP handshake failed: {hs['error']}")

    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("All checks passed. LORE's MCP server works on this machine.")
    return 0


def _is_windows_admin_member() -> bool | None:
    """Whether this account is in BUILTIN\\Administrators (S-1-5-32-544).

    Windows' SSH server reads an administrator's keys from a different file than
    everyone else's, which is the most common reason key login fails there.
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["whoami", "/groups"], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "S-1-5-32-544" in out


def write_launcher(pubkey_path: str | None = None) -> int:
    """Write the launcher the MCP-only SSH key's forced command runs, and print the key line."""
    py = sys.executable
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        target = base / "lore_mcp" / "lore_mcp.cmd"
        body = ("@echo off\r\n"
                "rem LORE MCP launcher. Run by the forced command on the MCP-only SSH key.\r\n"
                "rem Nothing here may print to stdout: stdout carries the MCP protocol.\r\n"
                f'cd /d "{PROJECT_ROOT}"\r\n'
                f'"{py}" -m lab_agent.mcp_server\r\n')
    else:
        target = Path.home() / ".local" / "share" / "lore_mcp" / "lore_mcp.sh"
        body = ("#!/bin/sh\n"
                "# LORE MCP launcher. Run by the forced command on the MCP-only SSH key.\n"
                f'cd "{PROJECT_ROOT}" && exec "{py}" -m lab_agent.mcp_server\n')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body.encode("utf-8"))
    if os.name != "nt":
        target.chmod(0o755)

    key = "ssh-ed25519 AAAA...paste-the-contents-of-lore_mcp.pub-here... lore-mcp"
    if pubkey_path:
        text = Path(pubkey_path).read_text(encoding="utf-8").strip()
        if not text.startswith(("ssh-", "ecdsa-")):
            print(f"{pubkey_path} does not look like a public key (.pub). Not using it.")
        else:
            key = text

    print(f"Launcher written:\n  {target}\n")
    for warn, bad in (("contains a space", " " in str(target)),
                      ("contains non-ASCII characters", not str(target).isascii()),
                      ("project path contains non-ASCII characters",
                       not str(PROJECT_ROOT).isascii())):
        if bad:
            print(f"WARNING: the path {warn}; the forced command may not start. "
                  "Move the project or the launcher to a plain path.\n")

    print("Add this ONE line to the SSH authorized-keys file on this machine. It lets the")
    print("MCP key start LORE's read-only server and nothing else, no shell:\n")
    print(f'  restrict,command="{target}" {key}\n')
    print("A normal key for your own debugging goes in the same file WITHOUT the")
    print("'restrict,command=...' prefix, so it gives you a regular shell.\n")

    admin = _is_windows_admin_member()
    if admin:
        f = r"C:\ProgramData\ssh\administrators_authorized_keys"
        print("This account is an administrator, so Windows' SSH server reads keys from")
        print(f"  {f}")
        print("and NOT from your user folder. After editing it, lock it down, or sshd")
        print("will ignore it:")
        print(f'  icacls "{f}" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"')
    elif admin is False:
        print(r"This account is not an administrator, so the file is %USERPROFILE%\.ssh\authorized_keys")
    else:
        print("The file is ~/.ssh/authorized_keys for this account.")
    return 0


_PROBE_HINTS = """\
If it failed, compare with these:
  * "Permission denied (publickey)" in the output above: the key was rejected. On Windows
    the usual cause is an administrator account, whose keys must be in
    C:\\ProgramData\\ssh\\administrators_authorized_keys with Administrators/SYSTEM-only
    permissions (run --write-launcher on the lab machine for the exact commands).
  * "Connection timed out" or "Could not resolve hostname": you are not on the campus
    network or VPN, the host name is wrong, or port 22 is blocked by a firewall.
  * The connection works but no MCP reply arrives: something is printing to stdout on
    the lab machine, most often a PowerShell profile when PowerShell is the SSH default
    shell. Or ssh is waiting for a password or host-key prompt: run the same ssh command
    once by hand first, and keep "-o BatchMode=yes" in it.
  * "The system cannot find the path specified": the launcher path in the forced
    command is wrong. Re-run --write-launcher on the lab machine and copy its line.
"""


def probe(command: list[str]) -> int:
    """Run ``command`` as an MCP server (e.g. an ssh command) and hold a real session with it."""
    if not command:
        print("Usage: python -m lab_agent.mcp_server --probe ssh -i KEY -T -o BatchMode=yes user@host")
        return 2
    print(f"Starting: {' '.join(command)}\n(any ssh messages appear below)\n")
    hs = handshake(command, timeout=60)
    if hs["ok"]:
        st = hs["status"] or {}
        print(f"\nOK: server '{hs['server']}' answered.")
        print(f"  tools: {', '.join(hs['tools'])}")
        print(f"  notebook pages: {st.get('notebook_pages')}   "
              f"graph service: {st.get('graph_service')}")
        print("\nThis exact command is ready to use as the MCP server in Claude Code.")
        return 0
    print(f"\nFAILED: {hs['error']}\n")
    print(_PROBE_HINTS)
    return 1
