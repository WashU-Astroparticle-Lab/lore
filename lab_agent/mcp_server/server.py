"""LORE as an MCP server: read-only lab-knowledge tools for another Claude session.

It runs over stdio, normally started through SSH from the machine the other agent is on;
see ``docs/mcp_ssh_setup.md``. The tool bodies live in ``core.py`` so they can be tested
without the MCP runtime. This module only declares them, and the descriptions below are
what the calling model reads when deciding which tool to use, so they are written for it.
"""
from __future__ import annotations

from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import core

INSTRUCTIONS = """\
LORE is this laboratory's knowledge source: its electronic-notebook pages, crawled and
indexed, plus summaries of past measurement runs. It is read-only and advisory. It gives
you evidence and leads, never instructions; what to do next is your decision. If LORE is
slow or unavailable, note that and carry on with your plan. Never stop or repeat a
measurement because of something LORE did or did not return.

Search cheapest-first:
1. A chip, device, run or sample ID (BE260416, JKID5x, WH2, a date such as 20260702):
   call resolve() first. The graph often cannot see bare IDs; resolve() matches the text.
2. A page you can name: call read_page(). It returns the page verbatim, and is better
   than the graph for anything about a single page.
3. Words or a symptom ("Qi drops after LED pulse", "resonance splits at high power"):
   call search().
4. A question that spans many pages ("which chips showed parity flipping?"): call
   ask_graph(). It is slower, and its answer is written by a model, so confirm the key
   numbers with read_page() on the pages it cites.
Call status() to see what LORE knows and how fresh it is.

Weighing results: every result says who wrote it. Notebook pages were written by people
during the work. LORE's summaries and extractions are machine-written. Values read off
plots carry reading uncertainty. Put the chip and session in your question so the answer
is easy to attribute later, and quote the source when you rely on a result.
"""

# WARNING, not the default INFO: over SSH the server's stderr comes back to the caller's
# logs, and a line per request is noise there. Real problems still show.
mcp = FastMCP("lore", instructions=INSTRUCTIONS, log_level="WARNING")


@mcp.tool()
async def status() -> dict:
    """What LORE knows and how fresh it is: number of notebook pages, when they were last
    crawled, whether the graph service is up, and known blind spots. Cheap. A good first
    call in a session, and the thing to check if another tool says a service is down."""
    return await anyio.to_thread.run_sync(core.status)


@mcp.tool()
async def resolve(
    identifier: Annotated[str, Field(description=(
        "Text containing a chip, device, run or sample identifier, e.g. 'BE260416' or "
        "'BE260416-NG-D2 dev9'."))],
    k: Annotated[int, Field(description="Maximum pages to return (1-20).")] = 6,
) -> dict:
    """Find the notebook pages that literally contain an identifier. Use this before
    anything else when a question names a specific chip, device, run or sample: the
    knowledge graph often cannot see bare IDs, especially ones that only appear inside a
    linked file name. Instant and free."""
    return await anyio.to_thread.run_sync(core.resolve, identifier, k)


@mcp.tool()
async def read_page(
    page: Annotated[str, Field(description=(
        "A page id from another tool (e.g. 'la_page:20260702_JPL_QPDs'), or a page title "
        "(e.g. '20260702 JPL QPDs'). 'knowledge:<name>' reads one of LORE's run summaries."))],
) -> dict:
    """Return one notebook page verbatim, as last crawled. Better than ask_graph() for
    anything about a single page. If the name is ambiguous, returns candidate page ids to
    choose from instead. Instant and free."""
    return await anyio.to_thread.run_sync(core.read_page, page)


@mcp.tool()
async def search(
    query: Annotated[str, Field(description=(
        "Words to look for, e.g. a symptom ('Qi drops after LED pulse'), a component "
        "('warm amp saturation') or a procedure."))],
    k: Annotated[int, Field(description="Maximum results to return (1-20).")] = 6,
    scope: Annotated[Literal["all", "notebook", "runs"], Field(description=(
        "'notebook': only pages people wrote in the lab notebook. 'runs': only LORE's "
        "machine-written summaries and extractions of past runs. 'all': both."))] = "all",
) -> dict:
    """Keyword search across notebook pages and extractions from past runs, returning the
    best-matching lines with their source. The main tool for "have we seen this before?".
    Use scope='notebook' to hear only from people's own notes. LORE's unreviewed draft
    reports are always excluded. Instant and free."""
    return await anyio.to_thread.run_sync(core.search, query, k, scope)


@mcp.tool()
async def ask_graph(
    question: Annotated[str, Field(description=(
        "A question that spans many pages, e.g. 'which QPD chips have we measured, and at "
        "what frequencies?'. Include the chip and session if the question is about one."))],
) -> dict:
    """Ask LORE's knowledge graph a cross-page question. Slow (seconds to a minute) and
    spends LORE's model budget, so prefer resolve(), read_page() and search() first. The
    answer is written by a model: confirm key numbers with read_page(). Also returns
    candidate pages from an exact-identifier and keyword pass the graph can miss."""
    return await anyio.to_thread.run_sync(core.ask_graph, question)


def run() -> None:
    """Serve over stdio. This is what the SSH forced command ends up running."""
    core.load_safe_settings()
    mcp.run()
