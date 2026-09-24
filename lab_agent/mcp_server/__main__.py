"""Run LORE's read-only MCP server, or one of its setup and debugging commands.

    python -m lab_agent.mcp_server                     serve over stdio (the SSH forced
                                                       command runs this; do not run it
                                                       by hand, it waits silently for MCP)
    python -m lab_agent.mcp_server --selftest [ID] [--graph]
                                                       check every tool on this machine,
                                                       then a real handshake
    python -m lab_agent.mcp_server --write-launcher [lore_mcp.pub]
                                                       write the SSH launcher and print
                                                       the authorized_keys line
    python -m lab_agent.mcp_server --probe ssh -i KEY -T -o BatchMode=yes user@host
                                                       talk MCP to a server started by
                                                       that command, and report
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if not argv:
        from .server import run
        run()
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd in ("-h", "--help"):
        print(__doc__)
        return 0
    if cmd == "--selftest":
        from .ops import selftest
        ident = next((a for a in rest if not a.startswith("--")), "BE260416")
        return selftest(ident, graph="--graph" in rest)
    if cmd == "--write-launcher":
        from .ops import write_launcher
        return write_launcher(rest[0] if rest else None)
    if cmd == "--probe":
        from .ops import probe
        return probe(rest)

    print(f"Unknown option {cmd!r}.\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
