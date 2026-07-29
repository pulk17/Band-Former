"""Measure the audio pipeline against ground truth.

A tiles song is the only exact ground truth this project has: its notes are read
off the video, not guessed from audio. So processing the same piece twice — once
as a tiles video, once through the audio path — and diffing the two gives real
recall/precision numbers instead of an opinion about whether a change "sounds
better".

Usage:
    python tools/eval.py data/output/tiles_test data/output/tiles_test_audio
    python tools/eval.py TRUTH TEST --ignore-octave      # pitch-class only
    python tools/eval.py TRUTH TEST --chords             # chord timeline too
    python tools/eval.py TRUTH TEST --no-align           # skip offset search

For chords on a song you know by ear (no tiles version needed), write the
detection out as a chart, fix the wrong lines, and score against it forever:

    python tools/eval.py --emit-chart data/output/SONG
    # edit data/output/SONG/chart.txt
    python tools/eval.py --chart data/output/SONG/chart.txt data/output/SONG

That prints root/quality agreement plus the most costly confusions, which is
what tells you which tuning.json knob to reach for.

Each side may be a song folder, a notes.json, or a tab.json. Notes are matched
greedily: same pitch, onset within --onset seconds (default 0.08). Runs on CPU
in a second — use it before and after every knob change, on BOTH reference
songs (see HANDOFF.md rule 15).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PC = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _load_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_notes(target: str) -> tuple[list[dict], str]:
    """Return [{start, pitch}] from a song folder / notes.json / tab.json.

    For a folder, this reads what the TRANSCRIPTION produced — tab.json's `roll`
    (video-read tiles notes) or notes.json — never tab.json's `notes`, which are
    the arranged guitar part: octave-shifted into guitar range and decluttered,
    so diffing those measures the arranger, not the transcriber. Point at a
    tab.json explicitly if that's what you actually want.
    """
    p = Path(target)
    if p.is_dir():
        tabp = p / "tab.json"
        if tabp.exists() and _load_json(tabp).get("roll"):
            p = tabp
        elif (p / "notes.json").exists():
            p = p / "notes.json"
        elif tabp.exists():
            p = tabp
        else:
            raise SystemExit(f"no tab.json or notes.json in {target}")
    data = _load_json(p)

    if isinstance(data, list):                       # notes.json
        return [{"start": n["start_time"], "pitch": int(n["pitch"])} for n in data], f"{p} (notes.json)"
    if data.get("roll"):                             # tiles tab.json — exact
        return [{"start": n["start"], "pitch": int(n["pitch"])} for n in data["roll"]], f"{p} (roll)"
    if data.get("notes"):
        return [{"start": n["start"], "pitch": int(n["pitch"])} for n in data["notes"]], f"{p} (arranged notes)"
    raise SystemExit(f"no notes found in {p}")


def match(truth: list[dict], test: list[dict], tol: float, ignore_octave: bool, offset: float) -> int:
    """Greedy one-to-one match count. Each truth note consumes at most one test
    note, so duplicate detections are punished as false positives."""
    key = (lambda p: p % 12) if ignore_octave else (lambda p: p)
    buckets: dict[int, list[float]] = {}
    for n in test:
        buckets.setdefault(key(n["pitch"]), []).append(n["start"] + offset)
    for v in buckets.values():
        v.sort()

    used = {k: [False] * len(v) for k, v in buckets.items()}
    hits = 0
    for t in sorted(truth, key=lambda n: n["start"]):
        cand = buckets.get(key(t["pitch"]))
        if not cand:
            continue
        best, best_d = -1, tol
        for i, s in enumerate(cand):
            if used[key(t["pitch"])][i]:
                continue
            d = abs(s - t["start"])
            if d <= best_d:
                best, best_d = i, d
            elif s > t["start"] + tol:
                break
        if best >= 0:
            used[key(t["pitch"])][best] = True
            hits += 1
    return hits


def best_offset(truth, test, tol, ignore_octave) -> float:
    """The two runs can start at different points (video intro vs audio trim).
    Search a global shift so we measure transcription error, not misalignment.

    Ties break toward no shift: a wide coarse tolerance scores many offsets
    identically, and picking an arbitrary one strands the fine pass away from
    the real alignment.
    """
    def pick(offsets, tolerance):
        return max(offsets, key=lambda o: (match(truth, test, tolerance, ignore_octave, o), -abs(o)))

    coarse = pick([k * 0.05 for k in range(-40, 41)], tol)
    return pick([round(coarse + k * 0.01, 3) for k in range(-5, 6)], tol)


def chords_of(target: str) -> list[dict]:
    p = Path(target)
    p = p / "tab.json" if p.is_dir() else p
    return _load_json(p).get("chords", [])


# ── Chord charts ─────────────────────────────────────────────────────────────
# A chart is a plain text file, one "time  chord" per line, blank lines and
# `#` comments ignored:
#     0:00    E
#     0:03.5  C#m
#     1:12    G#7
# Each chord runs until the next line. Written by --emit-chart from a detection,
# then corrected by ear — that turns "the chords feel wrong" into a number.

_QUAL_ALIASES = {
    "": "maj", "M": "maj", "maj": "maj", "major": "maj",
    "m": "min", "min": "min", "-": "min", "minor": "min",
    "5": "5", "no3": "5",
    "7": "7", "dom7": "7",
    "maj7": "maj7", "M7": "maj7", "Δ": "maj7", "Δ7": "maj7",
    "m7": "min7", "min7": "min7", "-7": "min7",
    "sus2": "sus2", "sus4": "sus4", "sus": "sus4",
    "dim": "dim", "o": "dim", "°": "dim",
    "aug": "aug", "+": "aug",
    "6": "6", "add9": "add9",
    "m7b5": "m7b5", "ø": "m7b5", "halfdim": "m7b5",
}


def parse_chord_label(label: str) -> str | None:
    """Loose chord spelling ("C#m7", "Ab", "E/G#") to the app's `ROOT:QUAL`."""
    s = label.strip()
    if not s or s.upper() in {"N", "NC", "N.C.", "-"}:
        return None
    s = s.split("/")[0].strip()                       # bass note is not scored
    m = re.match(r"^([A-Ga-g])([#b♭♯]?)(.*)$", s)
    if not m:
        return None
    root = m.group(1).upper() + m.group(2).replace("♯", "#").replace("♭", "b")
    if root.endswith("b"):                            # flats to the app's sharps
        i = PC.index(root[0])
        root = PC[(i - 1) % 12]
    qual = _QUAL_ALIASES.get(m.group(3).strip())
    if qual is None:
        qual = _QUAL_ALIASES.get(m.group(3).strip().lower())
    if qual is None:
        raise SystemExit(f"unrecognised chord quality in {label!r} "
                         f"(known: {', '.join(sorted(set(_QUAL_ALIASES.values())))})")
    return f"{root}:{qual}"


def _parse_time(text: str) -> float:
    if ":" in text:
        mins, _, secs = text.partition(":")
        return int(mins) * 60 + float(secs)
    return float(text)


def load_chart(path: str, end_time: float) -> list[dict]:
    entries = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise SystemExit(f"{path}:{lineno}: expected 'time chord', got {raw!r}")
        try:
            start = _parse_time(parts[0])
        except ValueError:
            raise SystemExit(f"{path}:{lineno}: bad timestamp {parts[0]!r}") from None
        entries.append({"start": start, "name": parse_chord_label(parts[1])})
    entries.sort(key=lambda e: e["start"])
    out = []
    for i, e in enumerate(entries):
        end = entries[i + 1]["start"] if i + 1 < len(entries) else max(end_time, e["start"] + 1)
        if e["name"]:                                  # N.C. lines just leave a hole
            out.append({"start": e["start"], "end": end, "name": e["name"]})
    return out


def emit_chart(song: str) -> None:
    chords = chords_of(song)
    if not chords:
        raise SystemExit(f"{song} has no chords to emit")
    dest = Path(song)
    dest = (dest / "chart.txt") if dest.is_dir() else dest.with_name("chart.txt")
    lines = ["# Detected chords — fix the wrong ones by ear, then score with:",
             f"#   python tools/eval.py --chart {dest.as_posix()} {Path(song).as_posix()}",
             "# One 'time chord' per line; a chord runs until the next line. N.C. = no chord.", ""]
    for c in chords:
        root, _, qual = c["name"].partition(":")
        pretty = {"maj": "", "min": "m", "min7": "m7"}.get(qual, qual)
        mins, secs = divmod(c["start"], 60)
        lines.append(f"{int(mins)}:{secs:05.2f}  {root}{pretty}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {dest} ({len(chords)} chords) — correct it, then re-run with --chart")


def chord_report(truth, test, offset: float, step: float = 0.1) -> None:
    """Frame-wise agreement between two chord timelines, plus the mistakes that
    actually cost the most — the confusion list is what tells you which knob to
    reach for (all-maj7 means complexity_penalty, flicker means transition_penalty)."""
    a = truth if isinstance(truth, list) else chords_of(truth)
    b = test if isinstance(test, list) else chords_of(test)
    if not a or not b:
        print("chords: one side has no chord timeline — skipped")
        return

    def at(chords, t):
        for c in chords:
            if c["start"] <= t < c["end"]:
                return c["name"]
        return None

    end = min(a[-1]["end"], b[-1]["end"] - offset)
    total = root_hit = full_hit = 0
    confusion: dict[tuple[str, str], int] = {}
    t = max(a[0]["start"], b[0]["start"] - offset)
    while t < end:
        ca, cb = at(a, t), at(b, t + offset)
        if ca and cb:
            total += 1
            ca, cb = ca.split("/")[0], cb.split("/")[0]
            root_hit += ca.split(":")[0] == cb.split(":")[0]
            if ca == cb:
                full_hit += 1
            else:
                confusion[(ca, cb)] = confusion.get((ca, cb), 0) + 1
        t += step
    if not total:
        print("chords: no overlapping span")
        return
    print(f"chords ({total} frames of {step}s over the shared span):")
    print(f"  root agreement    {root_hit / total:6.1%}")
    print(f"  root+quality      {full_hit / total:6.1%}")
    if confusion:
        print("  most costly mistakes (truth → detected):")
        for (want, got), n in sorted(confusion.items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {want:>10} → {got:<10} {n / total:5.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Diff a transcription against ground truth.")
    ap.add_argument("truth", nargs="?", help="song folder / notes.json / tab.json holding the truth (a tiles song)")
    ap.add_argument("test", nargs="?", help="the run being measured")
    ap.add_argument("--onset", type=float, default=0.08, help="onset tolerance in seconds (default 0.08)")
    ap.add_argument("--ignore-octave", action="store_true", help="match pitch class only (ignores octave errors)")
    ap.add_argument("--no-align", action="store_true", help="don't search for a global time offset")
    ap.add_argument("--chords", action="store_true", help="also compare the chord timelines")
    ap.add_argument("--chart", metavar="FILE", help="score a run's chords against a hand-corrected chord chart")
    ap.add_argument("--emit-chart", metavar="SONG", help="write SONG/chart.txt from its detected chords, to correct by ear")
    args = ap.parse_args()

    if args.emit_chart:
        emit_chart(args.emit_chart)
        return

    if args.chart:
        song = args.test or args.truth
        if not song:
            raise SystemExit("--chart needs the song folder to score: eval.py --chart chart.txt data/output/SONG")
        detected = chords_of(song)
        if not detected:
            raise SystemExit(f"{song} has no chords")
        chart = load_chart(args.chart, detected[-1]["end"])
        if not chart:
            raise SystemExit(f"{args.chart} has no chords")
        print(f"chart: {len(chart)} chords   detection: {len(detected)} chords")
        chord_report(chart, detected, 0.0)
        return

    if not args.truth or not args.test:
        ap.error("give TRUTH and TEST (or use --chart / --emit-chart)")

    truth, tsrc = load_notes(args.truth)
    test, esrc = load_notes(args.test)
    print(f"truth: {len(truth):5d} notes  {tsrc}")
    print(f"test:  {len(test):5d} notes  {esrc}")
    if not truth or not test:
        raise SystemExit("one side is empty")

    offset = 0.0 if args.no_align else best_offset(truth, test, args.onset, args.ignore_octave)
    if not args.no_align:
        print(f"aligned test by {offset:+.2f}s")

    hits = match(truth, test, args.onset, args.ignore_octave, offset)
    recall = hits / len(truth)
    precision = hits / len(test)
    f1 = 2 * recall * precision / (recall + precision) if hits else 0.0
    mode = "pitch class" if args.ignore_octave else "exact pitch"
    print(f"\nmatched {hits} ({mode}, onset ±{args.onset*1000:.0f}ms)")
    print(f"  recall     {recall:6.1%}   (truth notes found)")
    print(f"  precision  {precision:6.1%}   (test notes that are real)")
    print(f"  F1         {f1:6.1%}")

    if not args.ignore_octave:
        oct_hits = match(truth, test, args.onset, True, offset)
        if oct_hits > hits:
            print(f"  note: {oct_hits - hits} more match ignoring octave — suspect octave errors")

    if args.chords:
        print()
        chord_report(args.truth, args.test, offset)


if __name__ == "__main__":
    main()
