"""Lightweight local Q&A search over past experiment outputs.

Answers cross-experiment questions ("what attenuation were we running in February?",
"which runs saw an MXC anomaly?", "has this resonator's f0 drifted?") WITHOUT the
report pipeline: it greps the already-produced, normalized markdown under
``outputs/<exp>/`` (``extracted_*.md``, ``connections.md``, ``dr_conditions.md``, the
``[UNSIGNED]`` report), ranks by query-term overlap, and prints the top snippets WITH
their source experiment id so the caller can answer with provenance and never fabricate.

Deliberately excludes raw dumps (``notebooks.md``, ``data_summaries.md``) as noisy, and
the agent memory (private notes, not lab knowledge). This is the Stage-0 fast path; the
OKF knowledge bundle and, later, LightRAG retrieval supersede it once the archive
outgrows a grep.

Usage:  python -m lab_agent.cli.ask "your question" [--k 6]
"""
from __future__ import annotations

import math
import re
import sys

from ..config import KNOWLEDGE_ROOT, OUTPUT_ROOT

_STOP = {
    "the", "and", "for", "with", "was", "were", "what", "which", "when", "where",
    "how", "did", "does", "are", "our", "that", "this", "from", "have", "has",
    "of", "in", "to", "on", "is", "we", "you", "it", "at", "by", "or", "but",
    "not", "us", "a", "an", "any", "there", "been", "over", "into",
}

# High-signal, normalized artifacts only (skip raw notebooks/CSV dumps).
_CORPUS_GLOBS = [
    "extracted_*.md", "connections.md", "dr_conditions.md",
    "[[]UNSIGNED[]] *.md", "labarchives.md",
]


def _terms(query: str) -> list[str]:
    """Content terms from a question: keep words/numbers >2 chars, drop stopwords.

    Internal dots are kept (so '3.032' stays intact) but leading/trailing dots are
    stripped, so a token like 'calibration.' matches 'calibration' in the text.
    """
    terms = []
    for w in re.findall(r"[a-z0-9.]+", query.lower()):
        w = w.strip(".")
        if len(w) > 2 and w not in _STOP:
            terms.append(w)
    return terms


def candidate_pages(query: str, k: int = 6) -> list[tuple[str, str]]:
    """Ordered page candidates for resolving a question to a LabArchives page/concept.

    Identifier matches first (high precision — pages literally containing a chip/run ID from
    the query), then deep TF-IDF page matches. Returns ``[(source_id, tag)]`` deduped, best
    first. Shared by ``query_kb`` (what it prints) and the QA-resolution eval, so the eval
    checks exactly the behaviour the agent sees — no drift between them.

    The TF-IDF pass searches deep then filters to page sources: ``outputs/`` experiment dirs
    (not fetchable pages) otherwise crowd LA pages out of a shallow top-k.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for n, _occ, src in resolve_identifiers(query, 5):
        if src not in seen:
            seen.add(src)
            out.append((src, f"id×{n}"))
    for s, src, _fn, _sn in search(query, 30):
        if len(out) >= k:
            break
        if src.startswith(("la_page:", "knowledge:")) and src not in seen:
            seen.add(src)
            out.append((src, f"kw {round(s, 1)}"))
    return out[:k]


def _iter_docs():
    # Curated cross-run knowledge concepts first (distilled per experiment), then the
    # per-run extracted files/reports.
    kexp = KNOWLEDGE_ROOT / "experiments"
    if kexp.is_dir():
        for f in sorted(kexp.glob("*.md")):
            yield f"knowledge:{f.stem}", f
    la = KNOWLEDGE_ROOT / "labarchives"
    if la.is_dir():
        for f in sorted(la.glob("*.md")):
            yield f"la_page:{f.stem}", f
    if OUTPUT_ROOT.is_dir():
        for exp_dir in sorted(p for p in OUTPUT_ROOT.iterdir() if p.is_dir()):
            for glob in _CORPUS_GLOBS:
                for f in exp_dir.glob(glob):
                    yield exp_dir.name, f


# A measurement VALUE, not an identifier: number (+decimal/scientific) + optional unit —
# e.g. 40db, 3ghz, 100k, 250nm, 2e-6, 1.3e-6, 1000x. These recur across many pages and
# identify nothing, so they must NOT be treated as resolution identifiers.
_UNIT = (r"db|dbm|dbc|ghz|mhz|khz|hz|nm|um|mm|cm|kohm|ohm|mv|uv|kv|na|ua|ma|pa|ka|k|meg|"
         r"x|sccm|wt|mk|mbar|bar|torr|psi|rpm|fps|dbfs|dbc")
_VALUE_RE = re.compile(rf"^\d+(?:\.\d+)?(?:e-?\d+)(?:{_UNIT})?$|^\d+(?:\.\d+)?(?:{_UNIT})$", re.I)


def _identifier_tokens(query: str) -> list[str]:
    """Identifier-like tokens that uniquely pin a page: mixed letters+digits (chip/run/
    sample IDs like ``BE260416`` or ``JKID5x``) or long pure-digit runs (dates/run numbers
    like ``20260702``). These are exactly what the concept graph fails to resolve — the LLM
    rarely lifts a bare ID into an entity — but they identify a page almost unambiguously.

    Measurement values (``40db``, ``3ghz``, ``2e-6``, ``100k``, ``250nm``) are excluded: they
    look ID-like (letters+digits) but recur everywhere and would surface misleading pages.
    """
    toks: list[str] = []
    # min length 3 so short device IDs like WH2 are caught; pure-alpha words are still
    # excluded below (they lack a digit), so lowering the floor adds IDs, not noise.
    for m in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]{2,}", query):
        if m.isdigit() and len(m) >= 6:
            toks.append(m.lower())               # date / run number (e.g. 20260702)
            continue
        has_alpha = any(c.isalpha() for c in m)
        has_digit = any(c.isdigit() for c in m)
        if has_alpha and has_digit and not _VALUE_RE.match(m):
            toks.append(m.lower())               # chip/run/sample ID (e.g. BE260416, JKID5x)
    return toks


def resolve_identifiers(query: str, k: int = 6) -> list[tuple[int, int, str]]:
    """High-precision page resolver: find pages whose TEXT contains the query's identifier
    tokens (exact substring, case-insensitive). Returns
    ``[(n_distinct_ids_matched, total_occurrences, source_id)]`` best first.

    This is the fix for identifier ambiguity: a chip/run ID in the question (e.g. BE260416,
    often only present inside a hyperlink filename) reliably maps to its page here, where a
    TF-IDF pass over the whole question would let common words (quantum capacitance, devices)
    outrank the rare, decisive ID.
    """
    toks = _identifier_tokens(query)
    if not toks:
        return []
    out: list[tuple[int, int, str]] = []
    for src, f in _iter_docs():
        if not src.startswith(("la_page:", "knowledge:")):
            continue
        try:
            low = f.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        matched = {t for t in toks if t in low}
        if matched:
            occ = sum(low.count(t) for t in matched)
            out.append((len(matched), occ, src))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return out[:k]


def search(query: str, k: int = 6) -> list[tuple[float, str, str, list[str]]]:
    """Return up to k (score, source_id, filename, snippet_lines), best first.

    Ranking is TF-IDF-ish: term counts weighted by inverse document frequency so a
    common word ("run", "power") doesn't drown out a rare, distinctive one ("strax").
    A term appearing in the source id (experiment / la_page) adds a weighted bonus.
    """
    terms = _terms(query)
    if not terms:
        return []
    docs: list[tuple[str, str, str]] = []  # (source_id, filename, text)
    for src, f in _iter_docs():
        try:
            docs.append((src, f.name, f.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    if not docs:
        return []
    N = len(docs)
    df = {t: sum(1 for _, _, tx in docs if t in tx.lower()) for t in terms}
    idf = {t: math.log(1 + N / (1 + df[t])) for t in terms}

    results: list[tuple[float, str, str, list[str]]] = []
    for src, fname, text in docs:
        low = text.lower()
        score = sum(low.count(t) * idf[t] for t in terms)
        score += 3 * sum(idf[t] for t in terms if t in src.lower())
        if score <= 0:
            continue
        ranked_lines = sorted(
            ((sum(ln.lower().count(t) * idf[t] for t in terms), ln.strip()) for ln in text.splitlines()),
            key=lambda x: x[0], reverse=True,
        )
        snippet = [ln for s, ln in ranked_lines[:3] if s > 0 and ln]
        results.append((round(score, 2), src, fname, snippet))
    results.sort(key=lambda x: x[0], reverse=True)
    return results[:k]


def main() -> None:
    # Reports contain Unicode (µ, −, —); avoid a cp1252 console crash on Windows.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:]
    k = 6
    if "--k" in args:
        i = args.index("--k")
        try:
            k = int(args[i + 1])
            del args[i:i + 2]
        except (IndexError, ValueError):
            print("--k requires an integer")
            sys.exit(2)
    query = " ".join(args).strip()
    if not query:
        print(__doc__)
        sys.exit(2)

    hits = search(query, k)
    if not hits:
        print("No matching past-experiment content found. "
              "Fall back to Slack search, then LabArchives, then ask the user.")
        return
    print(f"Top {len(hits)} matches for: {query!r}\n")
    for score, exp, fname, snippet in hits:
        print(f"[{exp}] {fname}  (score {score})")
        for line in snippet:
            print(f"    {line[:200]}")
        print()


if __name__ == "__main__":
    main()
