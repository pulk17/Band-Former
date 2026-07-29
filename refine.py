"""Turn a raw transcription into something playable.

A neural transcriber gives you every event it thinks it heard. On a strummed
section that means six new notes every sixteenth — technically defensible, and
completely unplayable. It also invents: on Where Is My Mind roughly 45% of the
notes had no matching energy in the guitar stem at all.

So this stage does what a person writing the tab out would do, in order:

  1. verify   — every note is checked against the audio it came from; notes with
                no energy at their own pitch are dropped. This is the big one.
  2. dechatter— the same pitch restruck impossibly fast is one note, not five.
  3. events   — onsets within a few tens of ms are one strum; snap those to the
                beat grid so the rhythm is readable instead of drifting.
  4. harmony  — inside a strum, notes outside the sounding chord need real
                evidence to survive. Weak ones are transcription overtones.
  5. playable — six strings, one note each, no note held through another on the
                same string.

Every threshold is a knob in tuning.json under "refine", because the right
amount of cleanup differs between a fingerpicked ballad and a wall of guitars.
Set refine.enabled to 0 to skip the whole stage.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from tuning import knob as _knob
except Exception:  # noqa: BLE001 - standalone use without the repo root on sys.path
    def _knob(_s, _k, d):
        return d

NOTE_PC = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6,
           "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}
_INTERVALS = {"maj": (0, 4, 7), "min": (0, 3, 7), "5": (0, 7), "7": (0, 4, 7, 10),
              "maj7": (0, 4, 7, 11), "min7": (0, 3, 7, 10), "sus2": (0, 2, 7),
              "sus4": (0, 5, 7), "dim": (0, 3, 6), "aug": (0, 4, 8), "6": (0, 4, 7, 9),
              "m7b5": (0, 3, 6, 10), "add9": (0, 2, 4, 7)}


def chord_pcs(name: str):
    if ":" not in name:
        return None
    root, qual = name.split(":", 1)
    qual = qual.split("/")[0]
    if root not in NOTE_PC or qual not in _INTERVALS:
        return None
    return {(NOTE_PC[root] + iv) % 12 for iv in _INTERVALS[qual]}


# ── 1. Audio verification ────────────────────────────────────────────────────

def note_support(notes, stem_path, sr=22050, hop=256):
    """How much this pitch stands out at the moment the note starts.

    Measured against the other pitches sounding AT THAT INSTANT, not against
    the song as a whole. That distinction decides whether this stage helps or
    ruins quiet passages: normalised against the whole song, every note in a
    soft intro looks like nothing and gets deleted, taking the riff with it.
    Compared with its own moment, a quiet note that clearly stands out survives
    and a loud-section overtone that doesn't, dies.

    Only a short window from the onset is used — a plucked note's energy is all
    at the front. Returns a list parallel to `notes`; every entry is 1.0 if the
    audio can't be read, so a missing stem degrades to "keep everything" rather
    than "delete everything".
    """
    import librosa
    import numpy as np

    try:
        y, _ = librosa.load(str(stem_path), sr=sr, mono=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  refine: cannot read {Path(stem_path).name} ({exc}) — skipping verification")
        return [1.0] * len(notes)
    if not len(y):
        return [1.0] * len(notes)

    C = np.abs(librosa.cqt(y, sr=sr, hop_length=hop, fmin=librosa.note_to_hz("C1"),
                           n_bins=84, bins_per_octave=12))
    C /= (C.max() or 1)
    frame = sr / hop
    win = max(1, int(0.12 * frame))
    floor = float(np.median(C)) or 1e-6              # silence guard

    out = []
    for n in notes:
        b = int(n["pitch"]) - 24                       # CQT bin 0 is MIDI 24 (C1)
        if not (0 <= b < C.shape[0]):
            out.append(0.0)
            continue
        i0 = max(0, min(int(n["start"] * frame), C.shape[1] - 1))
        i1 = min(i0 + win, C.shape[1])
        if i1 <= i0:
            out.append(0.0)
            continue
        col = C[:, i0:i1].max(axis=1)
        # The note's pitch against the typical pitch right now. The floor stops
        # near-silence from turning faint noise into a huge ratio.
        out.append(float(col[b] / max(float(np.median(col)), floor)))
    return out


# ── 2-5. The cleanup passes ──────────────────────────────────────────────────

def dechatter(notes, gap_s):
    """One pitch restruck faster than a player could pick it is one held note."""
    by_pitch = {}
    for i, n in enumerate(notes):
        by_pitch.setdefault(n["pitch"], []).append(i)
    drop = set()
    for idxs in by_pitch.values():
        idxs.sort(key=lambda i: notes[i]["start"])
        last = None
        for i in idxs:
            if last is not None and notes[i]["start"] - notes[last]["start"] < gap_s:
                end = max(notes[last]["start"] + notes[last]["duration"],
                          notes[i]["start"] + notes[i]["duration"])
                notes[last]["duration"] = end - notes[last]["start"]
                drop.add(i)
            else:
                last = i
    return [n for i, n in enumerate(notes) if i not in drop]


def group_events(notes, tol):
    """Notes that start together are one event (a strum, or a chord stab)."""
    order = sorted(range(len(notes)), key=lambda i: notes[i]["start"])
    events, cur = [], [order[0]]
    for i in order[1:]:
        if notes[i]["start"] - notes[cur[0]]["start"] <= tol:
            cur.append(i)
        else:
            events.append(cur)
            cur = [i]
    events.append(cur)
    return events


def snap_to_grid(notes, events, beats, tol, div=4):
    """Pull each event onto the nearest sixteenth of the beat grid.

    Only if it's already within `tol` — a note that lands nowhere near the grid
    is either a real push/drag or a transcription error, and dragging it onto a
    subdivision would invent a rhythm that isn't there.
    """
    if len(beats) < 2 or tol <= 0:
        return 0
    grid = []
    for a, b in zip(beats, beats[1:]):
        for k in range(div):
            grid.append(a + (b - a) * k / div)
    grid.append(beats[-1])
    moved = 0
    for ev in events:
        t = min(notes[i]["start"] for i in ev)
        lo, hi = 0, len(grid) - 1
        while lo < hi:                                  # nearest grid point
            mid = (lo + hi) // 2
            if grid[mid] < t:
                lo = mid + 1
            else:
                hi = mid
        cands = [g for g in grid[max(0, lo - 1):lo + 2]]
        if not cands:
            continue
        g = min(cands, key=lambda x: abs(x - t))
        if abs(g - t) <= tol:
            for i in ev:
                notes[i]["duration"] = max(0.05, notes[i]["duration"] - (g - notes[i]["start"]))
                notes[i]["start"] = g
            moved += 1
    return moved


def thin_events(notes, events, min_gap, support):
    """Drop whole events that crowd the one before them.

    Transcribers often emit a strum twice a few tens of ms apart. Keep whichever
    the audio backs more strongly, not simply the first.
    """
    keep = []
    for ev in events:
        t = min(notes[i]["start"] for i in ev)
        if keep:
            pt = min(notes[i]["start"] for i in keep[-1])
            if t - pt < min_gap:
                mine = sum(support[i] for i in ev) / len(ev)
                theirs = sum(support[i] for i in keep[-1]) / len(keep[-1])
                if mine > theirs:
                    keep[-1] = ev
                continue
        keep.append(ev)
    return keep


def harmonic_filter(notes, events, chords, support, tau):
    """Inside an event, a note outside the sounding chord must earn its place.

    Overtones and separation bleed land on non-chord tones far more often than
    real playing does, but genuine passing tones and colour notes exist — so the
    test is evidence, not membership.
    """
    if not chords:
        return set()
    spans = [(c["start"], c["end"], chord_pcs(c["name"])) for c in chords]
    si = 0
    drop = set()
    for ev in sorted(events, key=lambda e: min(notes[i]["start"] for i in e)):
        t = min(notes[i]["start"] for i in ev)
        while si + 1 < len(spans) and spans[si][1] <= t:
            si += 1
        pcs = spans[si][2] if spans[si][0] <= t < spans[si][1] else None
        if not pcs:
            continue
        for i in ev:
            if notes[i]["pitch"] % 12 not in pcs and support[i] < tau:
                drop.add(i)
    return drop


def playable(notes, events, support, max_poly):
    """Six strings, one note each. Keep the notes the audio backs best, but
    always keep the top and bottom of a voicing — those carry the melody and the
    root, and a chord missing them stops sounding like the chord."""
    drop = set()
    for ev in events:
        if len(ev) <= max_poly:
            continue
        ranked = sorted(ev, key=lambda i: -support[i])
        hi = max(ev, key=lambda i: notes[i]["pitch"])
        lo = min(ev, key=lambda i: notes[i]["pitch"])
        keep = {hi, lo}
        for i in ranked:
            if len(keep) >= max_poly:
                break
            keep.add(i)
        drop |= set(ev) - keep
    return drop


def resolve_strings(notes, ring_max=0.0):
    """A string sounds one note at a time, and it rings until the next one.

    Both halves matter. Overlapping notes on one string are impossible, so the
    earlier one gets cut. But the opposite case is what makes a transcription
    sound like a machine: transcribers report the length of the ATTACK, ~150 ms,
    when a plucked string actually rings for seconds. Playing those back gives a
    stream of blips instead of a guitar. With `ring_max` set, each note is held
    until its string is next used.
    """
    by_string = {}
    for n in notes:
        s = n.get("string")
        if s:
            by_string.setdefault(s, []).append(n)
    fixed = rung = 0
    for group in by_string.values():
        group.sort(key=lambda n: n["start"])
        for a, b in zip(group, group[1:]):
            if a["start"] + a["duration"] > b["start"] + 1e-6:
                a["duration"] = max(0.05, b["start"] - a["start"])
                fixed += 1
            elif ring_max > 0:
                want = min(b["start"] - a["start"], ring_max)
                if want > a["duration"]:
                    a["duration"] = want
                    rung += 1
        if ring_max > 0 and group:
            group[-1]["duration"] = max(group[-1]["duration"], min(ring_max, 1.0))
    return fixed, rung


# ── Driver ───────────────────────────────────────────────────────────────────

def refine(tab: dict, stem_path=None, verbose: bool = True) -> dict:
    notes = tab.get("notes", [])
    if not notes or not int(_knob("refine", "enabled", 1)):
        return tab

    verify_tau = float(_knob("refine", "verify_tau", 1.5))
    chatter_ms = float(_knob("refine", "dechatter_ms", 110))
    event_ms = float(_knob("refine", "event_ms", 55))
    min_gap_ms = float(_knob("refine", "min_event_gap_ms", 105))
    snap_ms = float(_knob("refine", "grid_snap_ms", 45))
    nonchord_tau = float(_knob("refine", "nonchord_tau", 4.0))
    max_poly = int(_knob("refine", "max_polyphony", 6))

    n0 = len(notes)
    notes = [dict(n) for n in notes]
    notes.sort(key=lambda n: (n["start"], n["pitch"]))

    support = note_support(notes, stem_path, ) if stem_path else [1.0] * len(notes)
    if stem_path and verify_tau > 0:
        pairs = [(n, s) for n, s in zip(notes, support) if s >= verify_tau]
        notes = [n for n, _ in pairs]
        support = [s for _, s in pairs]
    n1 = len(notes)
    if not notes:
        return tab

    notes = dechatter(notes, chatter_ms / 1000)
    support = note_support(notes, stem_path) if stem_path else [1.0] * len(notes)
    n2 = len(notes)

    events = group_events(notes, event_ms / 1000)
    events = thin_events(notes, events, min_gap_ms / 1000, support)
    kept = sorted({i for ev in events for i in ev})
    remap = {old: new for new, old in enumerate(kept)}
    notes = [notes[i] for i in kept]
    support = [support[i] for i in kept]
    events = [[remap[i] for i in ev] for ev in events]
    n3 = len(notes)

    moved = snap_to_grid(notes, events, tab.get("beats", []), snap_ms / 1000)

    drop = harmonic_filter(notes, events, tab.get("chords", []), support, nonchord_tau)
    drop |= playable(notes, events, support, max_poly)
    notes = [n for i, n in enumerate(notes) if i not in drop]
    n4 = len(notes)

    notes.sort(key=lambda n: (n["start"], n["pitch"]))
    fixed, rung = resolve_strings(notes, float(_knob("refine", "ring_seconds", 1.6)))

    tab["notes"] = notes
    if verbose:
        dur = tab.get("metadata", {}).get("duration_sec") or 1
        print(f"  refine: {n0} notes -> {n4}  "
              f"(unverified -{n0-n1}, chatter -{n1-n2}, crowded events -{n2-n3}, "
              f"off-chord/unplayable -{n3-n4}; {moved} events snapped, "
              f"{fixed} string clashes cut, {rung} notes left to ring)")
        print(f"  refine: density {n0/dur:.1f} -> {n4/dur:.1f} notes/sec")
    return tab
