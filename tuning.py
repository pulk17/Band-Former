"""Load tuning.json — the user-editable knob file for accuracy tuning.

Every accuracy-relevant constant reads through here so behavior can be changed
without touching code — from the file directly or from the in-app Tuning
panel. Missing file or missing keys → defaults. The C++ engine reads the same
file itself (chord + chroma sections).

The cache is mtime-aware: edits (file or API) take effect on the next
pipeline run without restarting the server.
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "tuning.json"
_cache: dict | None = None
_mtime: float = -1.0


def load_tuning() -> dict:
    global _cache, _mtime
    try:
        mt = _PATH.stat().st_mtime
    except OSError:
        _cache, _mtime = {}, -1.0
        return _cache
    if _cache is None or mt != _mtime:
        try:
            _cache = json.loads(_PATH.read_text())
        except Exception:  # noqa: BLE001
            _cache = {}
        _mtime = mt
    return _cache


def knob(section: str, key: str, default):
    return load_tuning().get(section, {}).get(key, default)
