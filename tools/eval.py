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


def chord_report(truth_dir: str, test_dir: str, offset: float, step: float = 0.1) -> None:
    def chords_of(target: str):
        p = Path(target)
        p = p / "tab.json" if p.is_dir() else p
        return _load_json(p).get("chords", [])

    a, b = chords_of(truth_dir), chords_of(test_dir)
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
    t = max(a[0]["start"], b[0]["start"] - offset)
    while t < end:
        ca, cb = at(a, t), at(b, t + offset)
        if ca and cb:
            total += 1
            ra, rb = ca.split(":")[0], cb.split(":")[0]
            root_hit += ra == rb
            full_hit += ca.split("/")[0] == cb.split("/")[0]
        t += step
    if not total:
        print("chords: no overlapping span")
        return
    print(f"chords ({total} frames of {step}s over the shared span):")
    print(f"  root agreement    {root_hit / total:6.1%}")
    print(f"  root+quality      {full_hit / total:6.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Diff a transcription against ground truth.")
    ap.add_argument("truth", help="song folder / notes.json / tab.json holding the truth (a tiles song)")
    ap.add_argument("test", help="the run being measured")
    ap.add_argument("--onset", type=float, default=0.08, help="onset tolerance in seconds (default 0.08)")
    ap.add_argument("--ignore-octave", action="store_true", help="match pitch class only (ignores octave errors)")
    ap.add_argument("--no-align", action="store_true", help="don't search for a global time offset")
    ap.add_argument("--chords", action="store_true", help="also compare the chord timelines")
    args = ap.parse_args()

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
