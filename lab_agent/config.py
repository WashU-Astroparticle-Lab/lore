"""
lab_agent/config.py — single place for environment and lab configuration.

Every entry point should call ``load_env()`` once instead of repeating the
``load_dotenv`` incantation, and read lab-specific settings (notebook names,
upload folder, wiring diagram page) through ``lab_config()``.

Two configuration files live in the project root:

    .env           credentials (gitignored)          → load_env() / os.environ
    lab_config.md  lab-specific settings (gitignored, → lab_config()
                   copied from lab_config.template.md)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Repo root = parent of the lab_agent/ package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
LAB_CONFIG_PATH = PROJECT_ROOT / "lab_config.md"
OUTPUT_ROOT = PROJECT_ROOT / "outputs"
# Cross-run knowledge bundle (OKF Phase 3) — local per-machine memory, gitignored.
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"


def kb_dir() -> Path:
    """LightRAG knowledge-graph index directory. Defaults to ``knowledge/kb``.

    Override with ``KB_STORAGE_DIR`` to a path OUTSIDE a cloud-synced folder
    (OneDrive/Dropbox): their background sync locks files mid-write and breaks
    LightRAG's atomic ``.tmp -> rename``, causing WinError 5 (Access denied) during a
    build. On a machine where the repo lives under OneDrive, point this at e.g.
    ``%LOCALAPPDATA%/lore_kb``.
    """
    override = os.environ.get("KB_STORAGE_DIR")
    return Path(override) if override else (KNOWLEDGE_ROOT / "kb")

# .env keys the experiment pipeline needs (checked by check_env()).
REQUIRED_ENV_KEYS = ["GITHUB_TOKEN", "LA_AKID", "LA_SECRET", "LA_UID"]

_env_loaded = False


def load_env() -> None:
    """Load .env from the project root into os.environ (idempotent)."""
    global _env_loaded
    if _env_loaded:
        return
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    _env_loaded = True


def check_env(keys: list[str] | None = None, live: bool = False) -> bool:
    """Print set/MISSING for each required .env key. Never prints values.

    With ``live=True`` also asks each service whether the credential actually
    works. "set" is not "works": a run once burned 75 minutes over six
    exchanges because check_env reported GITHUB_TOKEN as set while GitHub was
    answering 401 to every request.

    Returns True if all keys are set (and, when live, all probes passed).
    """
    from dotenv import dotenv_values
    env = dotenv_values(ENV_PATH)
    all_ok = True
    for k in keys or REQUIRED_ENV_KEYS:
        present = bool(env.get(k))
        print(f"{k}: {'set' if present else 'MISSING'}")
        all_ok = all_ok and present

    if not live:
        return all_ok

    print("--- live checks ---")
    for label, probe in _live_probes().items():
        try:
            ok, detail = probe()
        except Exception as exc:                      # a probe must never abort the run
            ok, detail = False, f"probe error: {type(exc).__name__}"
        # Never print the credential itself — only the verdict and the reason.
        print(f"{label}: {'valid' if ok else 'INVALID'}" + (f" ({detail})" if detail else ""))
        all_ok = all_ok and ok
    return all_ok


def _live_probes() -> dict:
    """Name -> callable returning (ok, detail). Each does one cheap request."""
    import json as _json
    import os as _os
    import urllib.error
    import urllib.request

    load_env()

    def _github() -> tuple[bool, str]:
        token = _os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return False, "GITHUB_TOKEN missing"
        req = urllib.request.Request("https://api.github.com/user")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "lab-agent/1.0")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                login = _json.loads(resp.read()).get("login", "?")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, "401 — token expired or revoked; generate a new one"
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, f"unreachable ({type(exc).__name__})"
        return True, f"authenticated as {login}"

    def _slack() -> tuple[bool, str]:
        if not _os.environ.get("SLACK_BOT_TOKEN"):
            return False, "SLACK_BOT_TOKEN missing"
        from .slack import api
        try:
            body = api.api_get("auth.test", {})
        except Exception as exc:
            return False, str(exc).replace("auth.test: ", "")
        return True, f"bot {body.get('user', '?')} in {body.get('team', '?')}"

    def _labarchives() -> tuple[bool, str]:
        from .sources.labarchives import adapter as la_adapter
        try:
            notebooks = la_adapter.LabArchivesAdapter("probe")._list_notebooks()
        except Exception as exc:
            return False, f"HMAC auth failed ({type(exc).__name__})"
        return (True, f"{len(notebooks)} notebook(s) visible") if notebooks else (False, "no notebooks visible")

    def _la_cookie() -> tuple[bool, str]:
        cookie = _os.environ.get("LA_SESSION_COOKIE", "").strip()
        if not cookie:
            return False, "not set — run get_la_cookies.py (needed for figures/uploads)"
        from .sources.labarchives.auth import cookies_still_valid
        if cookies_still_valid(cookie):
            return True, "web session live"
        return False, "expired — run get_la_cookies.py"

    return {
        "GITHUB_TOKEN": _github,
        "SLACK_BOT_TOKEN": _slack,
        "LabArchives API (HMAC)": _labarchives,
        "LA_SESSION_COOKIE": _la_cookie,
    }


_TABLE_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$")


def lab_config() -> dict[str, str]:
    """Parse lab_config.md into a flat {key: value} dict.

    Reads every two-column markdown table row in the file (all sections),
    skipping header and separator rows. Returns {} if lab_config.md does
    not exist (fresh checkout — copy it from lab_config.template.md).
    """
    if not LAB_CONFIG_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for line in LAB_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        m = _TABLE_ROW_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if key in ("Key", "") or set(key) <= {"-", ":"}:
            continue  # header / separator rows
        values[key] = value
    return values


def lab_config_value(key: str, default: str = "") -> str:
    """Return one lab_config.md value, or *default* if absent."""
    return lab_config().get(key, default) or default


def dr_data_path() -> str:
    """Return DR_DATA_PATH from .env ('' if unset)."""
    load_env()
    return os.getenv("DR_DATA_PATH", "")
