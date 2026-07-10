"""Load tuning.json — the user-editable knob file for self-iteration.

Every accuracy-relevant constant reads through here so behavior can be changed
without touching code. Missing file or missing keys → defaults. The C++ engine
reads the same file itself (chord + chroma sections).

Edit tuning.json, hit Reprocess on a song, compare. That's the loop.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "tuning.json"
_cache: dict | None = None


def load_tuning() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_PATH.read_text())
        except Exception:  # noqa: BLE001
            _cache = {}
    return _cache


def knob(section: str, key: str, default):
    return load_tuning().get(section, {}).get(key, default)
