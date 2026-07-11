"""Turn the engine's raw tab.json into a *playable* arrangement.

The transcription is faithful but dense — every detected note, including
overtones and ghost notes. A guitarist wants three things instead:

  • a clean monophonic **melody** line (the lead/riff),
  • the **chord changes**, cleaned of flicker, and
  • a concrete, *easy* way to **fret each chord** — ideally open shapes, with a
    single **capo** position chosen so the song's barre chords become open ones.

Enriched fields are written back into tab.json:
  metadata.capo, metadata.num_melody;
  melody[];  each chord gets voicing (no capo) and capoVoicing (with the capo).

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
PC_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Movable E-shape voicings (root barred on the low E string); low E -> high E.
# All six extra qualities below were verified by hand against the open-string
# pitch classes they produce (E=4 A=9 D=2 G=7 B=11 e=4) — each must reduce to
# exactly the chord's tone set, same as the original seven already did.
SHAPES = {
    "maj":  [0, 2, 2, 1, 0, 0], "min":  [0, 2, 2, 0, 0, 0], "5": [0, 2, 2, -1, -1, -1],
    "7":    [0, 2, 0, 1, 0, 0], "maj7": [0, 2, 1, 1, 0, 0], "min7": [0, 2, 0, 0, 0, 0],
    "sus4": [0, 2, 2, 2, 0, 0],
    "sus2": [0, 2, 4, 4, 0, 0],     # root-2-5, no 3rd
    "add9": [0, 2, 2, 1, 0, 2],     # maj triad + 9th
    "6":    [0, 2, 2, 1, 2, 0],     # maj triad + 6th
    "dim":  [0, 1, 2, 0, -1, 0],    # root-b3-b5
    "aug":  [0, 3, 2, 1, 1, 0],     # root-3-#5
    "m7b5": [0, 1, 0, 0, -1, 0],    # root-b3-b5-b7 (half-diminished)
}

# Easy open chords, keyed by (pitch class, quality) -> (shape name, frets low E->high E).
OPEN_SHAPES = {
    (0, "maj"): ("C", [-1, 3, 2, 0, 1, 0]),  (2, "maj"): ("D", [-1, -1, 0, 2, 3, 2]),
    (4, "maj"): ("E", [0, 2, 2, 1, 0, 0]),   (7, "maj"): ("G", [3, 2, 0, 0, 0, 3]),
    (9, "maj"): ("A", [-1, 0, 2, 2, 2, 0]),
    (2, "min"): ("Dm", [-1, -1, 0, 2, 3, 1]), (4, "min"): ("Em", [0, 2, 2, 0, 0, 0]),
    (9, "min"): ("Am", [-1, 0, 2, 2, 1, 0]),
    (0, "7"): ("C7", [-1, 3, 2, 3, 1, 0]), (2, "7"): ("D7", [-1, -1, 0, 2, 1, 2]),
    (4, "7"): ("E7", [0, 2, 0, 1, 0, 0]), (7, "7"): ("G7", [3, 2, 0, 0, 0, 1]),
    (9, "7"): ("A7", [-1, 0, 2, 0, 2, 0]), (11, "7"): ("B7", [-1, 2, 1, 2, 0, 2]),
    (0, "maj7"): ("Cmaj7", [-1, 3, 2, 0, 0, 0]), (2, "maj7"): ("Dmaj7", [-1, -1, 0, 2, 2, 2]),
    (4, "maj7"): ("Emaj7", [0, 2, 1, 1, 0, 0]), (5, "maj7"): ("Fmaj7", [-1, -1, 3, 2, 1, 0]),
    (7, "maj7"): ("Gmaj7", [3, 2, 0, 0, 0, 2]), (9, "maj7"): ("Amaj7", [-1, 0, 2, 1, 2, 0]),
    (2, "min7"): ("Dm7", [-1, -1, 0, 2, 1, 1]), (4, "min7"): ("Em7", [0, 2, 0, 0, 0, 0]),
    (9, "min7"): ("Am7", [-1, 0, 2, 0, 1, 0]),
    (2, "sus4"): ("Dsus4", [-1, -1, 0, 2, 3, 3]), (4, "sus4"): ("Esus4", [0, 2, 2, 2, 0, 0]),
    (9, "sus4"): ("Asus4", [-1, 0, 2, 2, 3, 0]),
    (2, "5"): ("D5", [-1, -1, 0, 2, 3, -1]), (4, "5"): ("E5", [0, 2, 2, -1, -1, -1]),
    (9, "5"): ("A5", [-1, 0, 2, 2, -1, -1]),
}

try:
    from tuning import knob as _knob
except Exception:  # noqa: BLE001
    def _knob(_s, _k, d):
        return d

GHOST_DUR = _knob("arrange", "ghost_dur", 0.07)             # drop notes shorter than this
MELODY_MIN_DUR = _knob("arrange", "melody_min_dur", 0.10)   # melody candidates min length
MIN_CHORD_DUR = _knob("arrange", "min_chord_dur", 0.45)     # merge chord segments shorter
LEAD_MAX_POLY = _knob("arrange", "lead_max_poly", 2)        # sparse-slot cutoff for lead
SKYLINE_GAP = _knob("arrange", "skyline_gap_semitones", 5)  # top-of-strum melody separation
GHOST_MAX_DUR = _knob("arrange", "harmonic_ghost_max_dur", 0.09)  # chord-clash ghost filter


def _parse(name: str):
    if ":" not in name:
        return None
    root, qual = name.split(":", 1)
    qual = qual.split("/", 1)[0]      # "G:maj/B" → shape of plain G major
    if root not in NOTE_PC or qual not in SHAPES:
        return None
    return NOTE_PC[root], qual


def barre_voicing(root_pc: int, qual: str, capo: int = 0) -> dict:
    """Movable E-shape voicing relative to the capo (baseFret is above the capo)."""
    t = (root_pc - capo) % 12
    base = (t - 4) % 12
    frets = [(-1 if v < 0 else v + base) for v in SHAPES[qual]]
    return {"name": PC_NOTE[t] + ("m" if qual == "min" else "" if qual == "maj" else qual),
            "frets": frets, "baseFret": base, "open": False}


def voicing(root_pc: int, qual: str, capo: int = 0) -> dict:
    """Best playable shape for a chord at a given capo: open if possible, else barre."""
    t = (root_pc - capo) % 12
    if (t, qual) in OPEN_SHAPES:
        nm, frets = OPEN_SHAPES[(t, qual)]
        return {"name": nm, "frets": frets, "baseFret": 0, "open": True}
    return barre_voicing(root_pc, qual, capo)


def pick_capo(chords) -> int:
    """Choose the capo (0..7) that makes the most (frequency-weighted) chords open.

    Power chords are excluded: they're a movable 2-finger shape that's equally
    easy at any fret, so they shouldn't sway the capo. A mild per-fret penalty
    breaks near-ties toward a lower, more comfortable capo position.
    """
    counts = defaultdict(float)
    for c in chords:
        p = _parse(c["name"])
        if p and p[1] != "5":
            counts[p] += max(0.0, c.get("end", 0) - c.get("start", 0))  # weight by playing time
    if not counts:
        return 0
    best_capo, best_score = 0, -1e9
    for capo in range(8):
        cov = sum(n for (pc, q), n in counts.items()
                  if ((pc - capo) % 12, q) in OPEN_SHAPES)
        score = cov - 0.5 * capo
        if score > best_score:
            best_score, best_capo = score, capo
    return best_capo


def merge_chords(chords):
    """Collapse same-name neighbours and absorb sub-threshold flicker segments."""
    out = []
    for c in chords:
        if c.get("name") in ("silence", "unknown"):
            continue
        if out and out[-1]["name"] == c["name"]:
            out[-1]["end"] = c["end"]
        elif out and (c["end"] - c["start"]) < MIN_CHORD_DUR and \
                (out[-1]["end"] - out[-1]["start"]) >= MIN_CHORD_DUR:
            out[-1]["end"] = c["end"]           # swallow the brief blip
        else:
            out.append(dict(c))
    # second pass: merge any new same-name adjacencies created above
    merged = []
    for c in out:
        if merged and merged[-1]["name"] == c["name"]:
            merged[-1]["end"] = c["end"]
        else:
            merged.append(c)
    return merged


def declutter(notes, min_dur):
    """Drop ghost notes (too short) and exact duplicate onsets of the same pitch."""
    kept, seen = [], set()
    for n in sorted(notes, key=lambda n: (n["start"], -n["pitch"])):
        if n["duration"] < min_dur:
            continue
        key = (round(n["start"], 2), n["pitch"])
        if key in seen:
            continue
        seen.add(key)
        kept.append(n)
    return kept


def _onset_slots(notes, tol=0.08):
    """Cluster notes whose onsets fall within `tol` seconds into the same slot."""
    notes = sorted(notes, key=lambda n: n["start"])
    slots, cur, t0 = [], [notes[0]], notes[0]["start"]
    for n in notes[1:]:
        if n["start"] - t0 <= tol:
            cur.append(n)
        else:
            slots.append(cur); cur, t0 = [n], n["start"]
    slots.append(cur)
    return slots


def extract_melody(notes):
    """Extract a clean, singable monophonic melody.

    Per onset slot we keep the 3 highest notes as candidates, then find the
    cheapest path with a Viterbi pass that rewards higher/longer notes but
    *penalizes large pitch leaps*. That follows the actual lead line instead of
    jumping to whichever overtone happens to be highest in a given slot (the old
    skyline problem), and it ignores the low accompaniment voices entirely.
    """
    cand_notes = [n for n in notes if n["duration"] >= MELODY_MIN_DUR]
    if not cand_notes:
        return []
    slots = _onset_slots(cand_notes)
    cands = [sorted(s, key=lambda n: -n["pitch"])[:3] for s in slots]

    def emit(n):                       # lower is better: prefer higher + longer
        return -(n["pitch"] - 48) * 0.06 - min(n["duration"], 0.6) * 1.2

    dp = [[0.0] * len(c) for c in cands]
    bk = [[0] * len(c) for c in cands]
    for j, n in enumerate(cands[0]):
        dp[0][j] = emit(n)
    for i in range(1, len(cands)):
        for j, n in enumerate(cands[i]):
            best, bestk = float("inf"), 0
            for k, pn in enumerate(cands[i - 1]):
                leap = abs(n["pitch"] - pn["pitch"])
                c = dp[i - 1][k] + leap * 0.45 + (leap > 12) * 4.0   # leap penalty (+ hard octave cap)
                if c < best:
                    best, bestk = c, k
            dp[i][j] = best + emit(n)
            bk[i][j] = bestk

    j = min(range(len(dp[-1])), key=lambda j: dp[-1][j])
    path = []
    for i in range(len(cands) - 1, -1, -1):
        path.append(cands[i][j])
        j = bk[i][j]
    path.reverse()

    out = []
    for i, n in enumerate(path):
        dur = n["duration"]
        if i + 1 < len(path):
            gap = path[i + 1]["start"] - n["start"]
            if gap > 0:
                dur = min(dur, gap)
        if out and out[-1]["pitch"] == n["pitch"] and \
                n["start"] - (out[-1]["start"] + out[-1]["duration"]) < 0.1:
            out[-1]["duration"] = (n["start"] + max(0.06, dur)) - out[-1]["start"]
        else:
            out.append({**n, "duration": max(0.06, dur), "melody": True})
    return out


def clean_monophonic(notes, min_dur=0.10):
    """Reduce a (near-monophonic) transcription — e.g. vocals — to one note at a
    time: drop ghosts, resolve overlaps to the higher pitch, merge repeats."""
    notes = [dict(n) for n in notes if n["duration"] >= min_dur]
    notes.sort(key=lambda n: n["start"])
    out = []
    for n in notes:
        if out:
            prev = out[-1]
            if n["start"] < prev["start"] + prev["duration"]:        # overlap
                if n["pitch"] <= prev["pitch"]:
                    continue
                prev["duration"] = max(0.06, n["start"] - prev["start"])
            if prev["pitch"] == n["pitch"] and \
                    n["start"] - (prev["start"] + prev["duration"]) < 0.12:
                prev["duration"] = (n["start"] + n["duration"]) - prev["start"]
                continue
        out.append(n)
    return out


def _chord_pcs(name: str) -> set | None:
    """Pitch classes of a chord label, or None if unparseable."""
    IV = {"maj": (0, 4, 7), "min": (0, 3, 7), "5": (0, 7), "7": (0, 4, 7, 10),
          "maj7": (0, 4, 7, 11), "min7": (0, 3, 7, 10), "sus2": (0, 2, 7),
          "sus4": (0, 5, 7), "dim": (0, 3, 6), "aug": (0, 4, 8), "6": (0, 4, 7, 9),
          "m7b5": (0, 3, 6, 10), "add9": (0, 2, 4, 7)}
    if ":" not in name:
        return None
    root, qual = name.split(":", 1)
    qual = qual.split("/")[0]
    if root not in NOTE_PC or qual not in IV:
        return None
    return {(NOTE_PC[root] + iv) % 12 for iv in IV[qual]}


def drop_harmonic_ghosts(notes, chords):
    """Drop very short notes that clash with the sounding chord — typical MT3
    harmonic ghosts. Conservative: only sub-90 ms notes are candidates, real
    passing tones are longer."""
    if not chords:
        return notes
    spans = [(c["start"], c["end"], _chord_pcs(c["name"])) for c in chords]
    out = []
    for n in notes:
        if n["duration"] < GHOST_MAX_DUR:
            pcs = next((p for s, e, p in spans if p and s <= n["start"] < e), None)
            if pcs is not None and (n["pitch"] % 12) not in pcs:
                continue
        out.append(n)
    return out


def arrange(tab: dict) -> dict:
    raw = tab.get("notes", [])
    notes = declutter(raw, GHOST_DUR)          # drop ghosts from the displayed set too
    notes = drop_harmonic_ghosts(notes, tab.get("chords", []))

    # ── Two-voice separation: lead vs rhythm ─────────────────────────────────
    # Sparse onset slots feed the lead line (single-note playing). Dense strum
    # slots are rhythm — EXCEPT a clearly separated top note (5+ semitones
    # above the rest of the strum), which is a melody note played over chords.
    slots = _onset_slots(notes) if notes else []
    lead_pool = []
    for s in slots:
        if len(s) <= LEAD_MAX_POLY:
            lead_pool.extend(s)
        else:
            top = max(s, key=lambda n: n["pitch"])
            second = sorted((n["pitch"] for n in s), reverse=True)[1]
            if top["pitch"] - second >= SKYLINE_GAP:
                lead_pool.append(top)
    melody = extract_melody(lead_pool)
    melody_keys = {(round(n["start"], 3), n["pitch"]) for n in melody}
    for n in notes:
        n["voice"] = "lead" if (round(n["start"], 3), n["pitch"]) in melody_keys else "rhythm"
        n["melody"] = n["voice"] == "lead"   # back-compat flag
    tab["notes"] = notes
    tab["melody"] = melody

    chords = merge_chords(tab.get("chords", []))
    capo = pick_capo(chords)
    for c in chords:
        p = _parse(c["name"])
        if p:
            c["voicing"] = voicing(p[0], p[1], 0)
            c["capoVoicing"] = voicing(p[0], p[1], capo)
    tab["chords"] = chords

    meta = tab.setdefault("metadata", {})
    meta["capo"] = capo
    meta["num_melody"] = len(melody)
    meta["num_notes_clean"] = len(notes)
    return tab


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python arrange.py <tab.json>")
        sys.exit(1)
    p = Path(sys.argv[1])
    tab = arrange(json.loads(p.read_text()))
    p.write_text(json.dumps(tab, indent=2))
    print(f"Arranged: {len(tab.get('notes', []))} notes -> {len(tab['melody'])} melody, "
          f"{len(tab['chords'])} chords, capo {tab['metadata']['capo']}")


if __name__ == "__main__":
    main()
