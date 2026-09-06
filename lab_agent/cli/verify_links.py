"""Deterministic link checker for an experiment output directory (WS4).

Extracts every URL from the markdown files in ``outputs/<exp>/`` and checks each
with CODE, not agent judgment:
  * GitHub URLs      → resolved via the API (private repos work with GITHUB_TOKEN)
  * LabArchives URLs → marked ``not_checked`` (auth-walled — never falsely failed)
  * other URLs       → HEAD-then-GET, following redirects

Writes ``link_check.md`` (a `| URL | Status | Final URL |` table). The Report Writer
must not cite a `not_found`/`unreachable` link; the Critic gets a matching checklist
item.

Usage:  python -m lab_agent.cli.verify_links outputs/<experiment_id>
"""
from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from ..config import load_env

_URL_RE = re.compile(r"https?://[^\s\)\"'<>\]]+")
# Trailing markdown/punctuation to strip off a captured URL (e.g. a backtick or ** that
# hugged the link in prose), so they aren't sent to the checker as part of the URL.
_URL_TRAILING = ".,;:)]}*\"'`"
_GITHUB_TREE_BLOB_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
    r"(?:/(?:tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>[^?#]+))?)?"
)


def extract_urls(out_dir: Path) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for md in sorted(out_dir.glob("*.md")):
        for m in _URL_RE.finditer(md.read_text(encoding="utf-8", errors="replace")):
            url = m.group().rstrip(_URL_TRAILING)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _get_status(url: str, method: str, token: str | None = None, timeout: int = 12) -> tuple[str, str]:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "lab-agent/1.0")
    if token:
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
            return ("redirect" if final.rstrip("/") != url.rstrip("/") else "ok"), final
    except urllib.error.HTTPError as exc:
        return ("not_found" if exc.code == 404 else f"http_{exc.code}"), url
    except Exception:  # noqa: BLE001 — URLError/timeout/etc.
        return "unreachable", url


def check_github(url: str, token: str | None) -> tuple[str, str]:
    """Resolve a GitHub web URL through the API so private repos verify with a token."""
    m = _GITHUB_TREE_BLOB_RE.match(url)
    if not m:
        return _get_status(url, "GET", token)
    owner, repo = m.group("owner"), m.group("repo")
    ref, path = m.group("ref"), m.group("path")
    if path:
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path.rstrip('/')}"
        if ref:
            api += f"?ref={ref}"
    else:
        api = f"https://api.github.com/repos/{owner}/{repo}"
    status, _final = _get_status(api, "GET", token)
    return status, url


def check_url(url: str, token: str | None) -> tuple[str, str]:
    host = url.split("/", 3)[2].lower() if "://" in url else ""
    if "github.com" in host or "raw.githubusercontent.com" in host:
        return check_github(url, token)
    if "labarchives.com" in host:
        return "not_checked", url          # auth-walled — do not falsely fail
    status, final = _get_status(url, "HEAD", None)
    if status in ("unreachable", "http_405"):   # some servers reject HEAD
        status, final = _get_status(url, "GET", None)
    return status, final


def verify(out_dir: Path) -> list[tuple[str, str, str]]:
    load_env()
    token = os.environ.get("GITHUB_TOKEN") or None
    rows: list[tuple[str, str, str]] = []
    for url in extract_urls(out_dir):
        status, final = check_url(url, token)
        rows.append((url, status, final))
    return rows


def write_report(out_dir: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["---", "type: Link Check", "---", "", "# Link Check", "",
             "| URL | Status | Final URL |", "| --- | --- | --- |"]
    for url, status, final in rows:
        fin = final if final != url else ""
        lines.append(f"| {url} | {status} | {fin} |")
    dead = [u for u, s, _ in rows if s in ("not_found", "unreachable")]
    lines += ["", f"**{len(rows)} link(s) checked; {len(dead)} dead.**"]
    if dead:
        lines.append("Do not cite these as sources: " + ", ".join(dead))
    (out_dir / "link_check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    out_dir = Path(sys.argv[1])
    if not out_dir.is_dir():
        print(f"Not a directory: {out_dir}")
        sys.exit(2)
    rows = verify(out_dir)
    write_report(out_dir, rows)
    dead = sum(1 for _, s, _ in rows if s in ("not_found", "unreachable"))
    print(f"[verify_links] {len(rows)} link(s) checked, {dead} dead -> link_check.md")


if __name__ == "__main__":
    main()
