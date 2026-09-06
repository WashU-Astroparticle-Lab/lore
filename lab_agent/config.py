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


def check_env(keys: list[str] | None = None) -> bool:
    """Print set/MISSING for each required .env key. Never prints values.

    Returns True if all keys are set.
    """
    from dotenv import dotenv_values
    env = dotenv_values(ENV_PATH)
    all_set = True
    for k in keys or REQUIRED_ENV_KEYS:
        present = bool(env.get(k))
        print(f"{k}: {'set' if present else 'MISSING'}")
        all_set = all_set and present
    return all_set


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
