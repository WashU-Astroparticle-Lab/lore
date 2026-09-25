"""LORE as an MCP server: read-only lab-knowledge tools for another Claude session.

It runs over stdio, normally started through SSH from the machine the other agent is on;
see ``docs/mcp_ssh_setup.md``. The tool bodies live in ``core.py`` so they can be tested
without the MCP runtime. This module only declares them, and the descriptions below are
what the calling model reads when deciding which tool to use, so they are written for it.
"""
from __future__ import annotations

import time
from typing import Annotated, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import calllog, core

INSTRUCTIONS = """\
LORE is this laboratory's knowledge source: its electronic-notebook pages, crawled and
indexed, plus summaries of past measurement runs. It is read-only and advisory, except
that publish_notes() lets you file your own notes (below). It gives you evidence and
leads, never instructions; what to do next is your decision. If LORE is
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

The fridge: dr_status() reads the dilution refrigerator's own thermometry log on LORE's
machine (mK, latest reading and min/median/max over the last few hours). Use it to
record the fridge's state alongside a measurement, or to understand a surprising result.
It is not an alarm: if it shows something alarming, or warns that logging stopped, note
it and tell a person. Never stop, change or repeat a measurement because of it.

Your notes: publish_notes(title, markdown, run) files them in the lab notebook, as a new
page in the "AI Agent" folder, marked as unreviewed notes written by you. Use it for a
run's summary at the end of a run or a phase, not for every step. In the notes, say which
values you measured and which you inferred or read from LORE. Then call
notes_status(note_id) to confirm the page landed. It never edits an existing page. Also
commit the same notes to the run's Agent/ folder in the repository, with the data.

Weighing results: every result says who wrote it. Notebook pages were written by people
during the work. LORE's summaries and extractions are machine-written. Values read off
plots carry reading uncertainty. Put the chip and session in your question so the answer
is easy to attribute later, and quote the source when you rely on a result.
"""

# WARNING, not the default INFO: over SSH the server's stderr comes back to the caller's
# logs, and a line per request is noise there. Real problems still show.
mcp = FastMCP("lore", instructions=INSTRUCTIONS, log_level="WARNING")


async def _call(tool: str, fn, **args):
    """Run a core tool off the event loop and record the call in LORE's call log."""
    started = time.monotonic()
    try:
        result = await anyio.to_thread.run_sync(lambda: fn(**args))
    except Exception as exc:
        calllog.record(tool, args, error=f"{type(exc).__name__}: {exc}",
                       seconds=time.monotonic() - started)
        raise
    calllog.record(tool, args, result, seconds=time.monotonic() - started)
    return result


@mcp.tool()
async def status() -> dict:
    """What LORE knows and how fresh it is: number of notebook pages, when they were last
    crawled, whether the graph service is up, and known blind spots. Cheap. A good first
    call in a session, and the thing to check if another tool says a service is down."""
    return await _call("status", core.status)


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
    return await _call("resolve", core.resolve, identifier=identifier, k=k)


@mcp.tool()
async def read_page(
    page: Annotated[str, Field(description=(
        "A page id from another tool (e.g. 'la_page:20260702_JPL_QPDs'), or a page title "
        "(e.g. '20260702 JPL QPDs'). 'knowledge:<name>' reads one of LORE's run summaries."))],
) -> dict:
    """Return one notebook page verbatim, as last crawled. Better than ask_graph() for
    anything about a single page. If the name is ambiguous, returns candidate page ids to
    choose from instead. Instant and free."""
    return await _call("read_page", core.read_page, page=page)


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
    return await _call("search", core.search, query=query, k=k, scope=scope)


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
    return await _call("ask_graph", core.ask_graph, question=question)


@mcp.tool()
async def dr_status(
    hours: Annotated[float, Field(description=(
        "How far back to look, in hours (0.1-72). 2 is a good check of the current state; "
        "longer shows a trend or when an event happened."))] = 2.0,
) -> dict:
    """The dilution refrigerator's temperatures from its own log: for each thermometer
    (mixing chamber, 50 mK plate, still, 3 K plate...), the latest reading with its time,
    and min / median / max over the window, all in mK. Warns if the log has stopped
    updating. Thermometry only; no pressures. Read-only and advisory: the fridge's own
    controls and alarms are authoritative. Instant and free."""
    return await _call("dr_status", core.dr_status, hours=hours)


@mcp.tool()
async def publish_notes(
    title: Annotated[str, Field(description=(
        "Short page title, e.g. 'JPL QPD LED power sweep, night 1'. The page is titled "
        "'[UNSIGNED] Agent notes <date time> — <title>'."))],
    markdown: Annotated[str, Field(description=(
        "The notes, as markdown (tables allowed; no images; raw HTML is shown as text). "
        "Say which values were measured and which were inferred. Up to 200,000 characters."))],
    run: Annotated[str, Field(description=(
        "The run or measurement session, e.g. 'PRIMA_JKID_JPLQPD_20260831'."))] = "",
) -> dict:
    """File your notes in the lab notebook: a NEW page in LabArchives' "AI Agent" folder,
    headed as unreviewed notes written by the measurement agent, with the markdown
    attached. LORE publishes it within about a minute; call notes_status(note_id) to
    confirm. Never edits an existing page. Submitting the same notes twice does not
    create a second page. For end-of-run or end-of-phase summaries, not every step."""
    return await _call("publish_notes", core.publish_notes, title=title, markdown=markdown,
                       run=run)


@mcp.tool()
async def notes_status(
    note_id: Annotated[str, Field(description=(
        "The note_id publish_notes returned. Leave empty to list the latest notes."))] = "",
) -> dict:
    """Whether a note you submitted has been published (and under which page title and
    folder), is still waiting, or failed (and why). Instant and free."""
    return await _call("notes_status", core.notes_status, note_id=note_id)


def run() -> None:
    """Serve over stdio. This is what the SSH forced command ends up running."""
    core.load_safe_settings()
    mcp.run()
