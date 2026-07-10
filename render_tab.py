"""Render a tab.json (produced by the C++ tab engine) into classic ASCII
guitar tablature.

Usage:
    python render_tab.py <tab.json> [output.txt]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

# Display order: string 1 (high E) on top, string 6 (low E) on the bottom.
STRING_LABELS = ["e", "B", "G", "D", "A", "E"]   # index 0 -> string 1 ... index 5 -> string 6

COLS_PER_LINE = 40       # tab columns before wrapping to a new staff
BEATS_PER_BAR = 4        # assume 4/4


def _columns_from_notes(notes):
    """Collapse notes that share a (quantized) onset into vertical columns."""
    grouped = defaultdict(dict)   # onset -> {string: fret}
    beat_of = {}
    for n in notes:
        onset = round(n.get("start_q", n["start"]), 3)
        grouped[onset][n["string"]] = n["fret"]
        beat_of[onset] = n.get("beat", -1.0)

    columns = []
    for onset in sorted(grouped):
        frets = grouped[onset]
        width = max((len(str(f)) for f in frets.values()), default=1)
        columns.append({"onset": onset, "beat": beat_of[onset], "frets": frets, "width": width})
    return columns


def render(tab: dict) -> str:
    meta = tab.get("metadata", {})
    notes = tab.get("notes", [])
    chords = tab.get("chords", [])
    columns = _columns_from_notes(notes)

    out = []
    out.append("=" * 72)
    out.append("  Band-Former - Guitar Tab")
    out.append("=" * 72)
    if meta:
        bpm = meta.get("bpm", 0) or 0
        out.append(f"  Key: {meta.get('key', '?')}    "
                   f"BPM: {bpm:g}    "
                   f"Notes: {meta.get('num_notes', len(notes))}    "
                   f"Duration: {meta.get('duration_sec', 0):.0f}s")
    out.append("=" * 72)
    out.append("")

    if not columns:
        out.append("(no notes)")
        return "\n".join(out)

    # Walk columns, wrapping into staves of COLS_PER_LINE, with barlines at
    # each new 4/4 measure (when a beat crosses a multiple of BEATS_PER_BAR).
    idx = 0
    while idx < len(columns):
        chunk = columns[idx: idx + COLS_PER_LINE]

        # Time header for this staff.
        t0 = chunk[0]["onset"]
        t1 = chunk[-1]["onset"]
        out.append(f"  [{t0:6.1f}s - {t1:6.1f}s]")

        rows = [f"{STRING_LABELS[s]}|" for s in range(6)]
        prev_bar = None
        for col in chunk:
            beat = col["beat"]
            bar = int(beat // BEATS_PER_BAR) if beat is not None and beat >= 0 else None
            if prev_bar is not None and bar is not None and bar != prev_bar:
                for s in range(6):
                    rows[s] += "|"
            prev_bar = bar if bar is not None else prev_bar

            w = col["width"]
            for s in range(6):
                string_num = s + 1
                cell = str(col["frets"][string_num]) if string_num in col["frets"] else ""
                rows[s] += "-" + cell.rjust(w, "-")
        for s in range(6):
            rows[s] += "-|"
        out.extend(rows)
        out.append("")
        idx += COLS_PER_LINE

    # A compact chord chart underneath.
    if chords:
        out.append("-" * 72)
        out.append("  Chord chart:")
        line = "   "
        for c in chords:
            tok = f"{c['name']}({c['start']:.0f}s) "
            if len(line) + len(tok) > 72:
                out.append(line)
                line = "   "
            line += tok
        if line.strip():
            out.append(line)
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print("Usage: python render_tab.py <tab.json> [output.txt]")
        sys.exit(1)

    tab = json.loads(Path(sys.argv[1]).read_text())
    text = render(tab)

    if len(sys.argv) >= 3:
        Path(sys.argv[2]).write_text(text, encoding="utf-8")
        print(f"Tab written to {sys.argv[2]}")
    else:
        print(text)


if __name__ == "__main__":
    main()
