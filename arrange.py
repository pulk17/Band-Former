"""Turn the engine's raw tab.json into a *playable* arrangement.

The transcription is faithful but dense — every detected note, including
overtones and ghost notes. A guitarist wants two things instead:

  • a clean monophonic **melody** line (the lead/riff), and
  • the **chord changes** with a concrete fingering to play.

This module declutters the notes, extracts a skyline melody, tags the remaining
notes as harmony, and attaches a movable chord voicing to every chord segment.
The enriched fields are written back into tab.json (under "melody" and each
chord's "voicing"), leaving the original "notes" intact for an "all notes" view.

Usage:
    python arrange.py <tab.json>
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

NOTE_PC = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
           "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}

# Movable E-shape voicings (root barred on the low E string).
# Six entries, low E -> high E; -1 means the string is muted.
SHAPES = {
    "maj":  [0, 2, 2, 1, 0, 0],
    "min":  [0, 2, 2, 0, 0, 0],
    "5":    [0, 2, 2, -1, -1, -1],   # power chord
    "7":    [0, 2, 0, 1, 0, 0],
    "maj7": [0, 2, 1, 1, 0, 0],
    "min7": [0, 2, 0, 0, 0, 0],
    "sus4": [0, 2, 2, 2, 0, 0],
}

MIN_NOTE_DUR = 0.06   # seconds; shorter notes are treated as ghosts/transients


def chord_voicing(name: str) -> dict | None:
    """Return a concrete fingering {baseFret, frets[6]} for a chord label."""
    if ":" not in name:
        return None
    root, qual = name.split(":", 1)
    if root not in NOTE_PC or qual not in SHAPES:
        return None
    base = (NOTE_PC[root] - 4) % 12          # barre fret on the low E string
    frets = [(-1 if v < 0 else v + base) for v in SHAPES[qual]]   # low E -> high E
    return {"baseFret": base, "frets": frets}


def declutter(notes: list[dict]) -> list[dict]:
    """Drop ghost notes and exact-duplicate overlaps."""
    kept: list[dict] = []
    seen: dict[tuple, float] = {}
    for n in sorted(notes, key=lambda n: (n["start"], -n["pitch"])):
        if n["duration"] < MIN_NOTE_DUR:
            continue
        key = (round(n["start"], 2), n["pitch"])
        if key in seen:               # same pitch, same onset -> duplicate
            continue
        seen[key] = n["start"]
        kept.append(n)
    return kept


def extract_melody(notes: list[dict]) -> list[dict]:
    """Skyline melody: the highest note at each (quantized) onset, made monophonic."""
    groups: dict[float, list[dict]] = defaultdict(list)
    for n in notes:
        key = round(n.get("start_q", n["start"]), 2)
        groups[key].append(n)

    melody = [max(groups[k], key=lambda n: n["pitch"]) for k in sorted(groups)]
    melody.sort(key=lambda n: n["start"])

    # Trim overlaps so only one melody note sounds at a time.
    out = []
    for i, n in enumerate(melody):
        dur = n["duration"]
        if i + 1 < len(melody):
            gap = melody[i + 1]["start"] - n["start"]
            if gap > 0:
                dur = min(dur, gap)
        out.append({**n, "duration": max(0.05, dur), "melody": True})
    return out


def arrange(tab: dict) -> dict:
    notes = tab.get("notes", [])
    clean = declutter(notes)

    melody = extract_melody(clean)
    melody_keys = {(round(n["start"], 3), n["pitch"]) for n in melody}

    # Tag every note as melody or harmony for the "all notes" view.
    for n in notes:
        n["melody"] = (round(n["start"], 3), n["pitch"]) in melody_keys

    tab["melody"] = melody
    for c in tab.get("chords", []):
        v = chord_voicing(c["name"])
        if v:
            c["voicing"] = v

    meta = tab.setdefault("metadata", {})
    meta["num_melody"] = len(melody)
    meta["num_notes_clean"] = len(clean)
    return tab


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python arrange.py <tab.json>")
        sys.exit(1)
    p = Path(sys.argv[1])
    tab = arrange(json.loads(p.read_text()))
    p.write_text(json.dumps(tab, indent=2))
    print(f"Arranged: {len(tab.get('notes', []))} notes -> "
          f"{len(tab.get('melody', []))} melody notes, "
          f"{len(tab.get('chords', []))} chords with voicings")


if __name__ == "__main__":
    main()
