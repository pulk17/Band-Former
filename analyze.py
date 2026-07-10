"""Derive music-theory analysis from an arranged tab.json.

Everything here is rule-based and computed from the song's own data — key,
chord functions (roman numerals), cadences, borrowed chords, secondary
dominants, practice-worthy transitions, solo scales, and a difficulty profile.
The result is written into tab.json under "analysis" and drives the Learn view
and the guided-practice mode.

Usage (re-analyze existing songs without reprocessing):
    python analyze.py data/output/*/tab.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):   # UTF-8 so ✓ prints don't crash on Windows
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from arrange import NOTE_PC, PC_NOTE, _parse, voicing

MAJ_DEG = [0, 2, 4, 5, 7, 9, 11]
MIN_DEG = [0, 2, 3, 5, 7, 8, 10]
MAJ_ROMAN = ["I", "ii", "iii", "IV", "V", "vi", "vii°"]
MIN_ROMAN = ["i", "ii°", "III", "iv", "v", "VI", "VII"]
# Expected triad quality per degree (for spotting e.g. a major IV in minor).
MAJ_QUAL = ["maj", "min", "min", "maj", "maj", "min", "dim"]
MIN_QUAL = ["min", "dim", "maj", "min", "min", "maj", "maj"]

ROLE_TEXT = {
    "I": "home — everything resolves here", "i": "home (minor) — the song's center",
    "V": "tension — pulls hard back to home", "v": "soft tension toward home",
    "IV": "movement away from home", "iv": "movement (minor color)",
    "vi": "the relative minor — 'sad' substitute for home",
    "VI": "brightens the minor key", "ii": "sets up the V chord",
    "iii": "gentle color chord", "III": "relative major — 'hopeful' lift",
    "VII": "rock/modal cadence chord", "ii°": "dark passing chord",
    "vii°": "leading-tone pull to home",
}


def _key_of(tab: dict):
    key = (tab.get("metadata") or {}).get("key") or ""
    parts = key.split()
    if len(parts) != 2 or parts[0] not in NOTE_PC:
        return None, None
    return NOTE_PC[parts[0]], parts[1] == "major"


def _root_name(name: str) -> str:
    return name.split(":")[0]


def _quality(name: str) -> str:
    q = name.split(":", 1)[1] if ":" in name else "maj"
    return q.split("/")[0]


def _roman_for(root_pc: int, qual: str, tonic: int, major: bool) -> dict:
    """Roman numeral + function classification for one chord in one key."""
    degs = MAJ_DEG if major else MIN_DEG
    romans = MAJ_ROMAN if major else MIN_ROMAN
    quals = MAJ_QUAL if major else MIN_QUAL
    rel = (root_pc - tonic) % 12
    base_q = "min" if qual.startswith("min") else "dim" if qual in ("dim", "m7b5") else "maj"

    if rel in degs:
        di = degs.index(rel)
        roman = romans[di]
        expected = quals[di]
        if base_q == expected or qual in ("5", "sus2", "sus4"):  # sus/power are quality-neutral
            return {"roman": roman, "function": "diatonic",
                    "role": ROLE_TEXT.get(roman, "")}
        # right root, unexpected quality
        if base_q == "maj" and expected == "min":
            # major chord on a minor degree: classic secondary dominant test
            target = (rel + 5) % 12   # a P4 up = what it would be V of
            if target in degs:
                t_roman = romans[degs.index(target)]
                return {"roman": roman.upper(), "function": "secondary dominant",
                        "role": f"V of {t_roman} — borrowed tension aimed at {PC_NOTE[(tonic + target) % 12]}"}
        return {"roman": roman.upper() if base_q == "maj" else roman.lower(),
                "function": "modal mixture",
                "role": f"borrowed quality — {base_q} where the key expects {expected}"}

    # Non-diatonic root: name the common borrowed degrees.
    flat_names = {1: "bII", 3: "bIII" if major else "#III", 6: "bV",
                  8: "bVI", 10: "bVII"}
    label = flat_names.get(rel, f"chromatic ({PC_NOTE[root_pc]})")
    role = {"bVII": "rock cadence borrow (mixolydian)",
            "bVI": "epic minor-borrow", "bIII": "minor-borrow color",
            "bII": "flamenco/phrygian color"}.get(label, "chromatic color chord")
    return {"roman": label, "function": "borrowed", "role": role}


def _pent_positions(tonic_pc: int, major: bool) -> list[dict]:
    """The five pentatonic boxes, anchored where box 1 sits for this key."""
    minor_root = tonic_pc if not major else (tonic_pc + 9) % 12   # relative minor
    fret = (minor_root - 4) % 12          # low-E string: open E = pc 4
    if fret == 0:
        fret = 12
    boxes = [{"box": i + 1, "fret": (fret + off - 1) % 12 + 1}
             for i, off in enumerate([0, 3, 5, 7, 10])]
    return [{"name": f"{PC_NOTE[minor_root]} minor pentatonic"
                      + (f" (= {PC_NOTE[tonic_pc]} major pentatonic)" if major else ""),
             "positions": boxes}]


def build_analysis(tab: dict) -> dict:
    tonic, major = _key_of(tab)
    chords = [c for c in tab.get("chords", [])
              if c.get("name") not in ("silence", "unknown")]
    meta = tab.get("metadata") or {}
    capo = int(meta.get("capo") or 0)
    out: dict = {}

    # ── Chord functions ───────────────────────────────────────────────────────
    time_per = Counter()
    for c in chords:
        time_per[c["name"]] += max(0.0, c["end"] - c["start"])
    romans, functions = {}, {}
    if tonic is not None:
        for name in time_per:
            root = _root_name(name)
            if root in NOTE_PC:
                info = _roman_for(NOTE_PC[root], _quality(name), tonic, major)
                romans[name] = info["roman"]
                functions[name] = info
    out["romans"] = romans
    out["functions"] = functions

    # ── Progression id: the most common 4-chord loop ─────────────────────────
    seq = []
    for c in chords:
        if not seq or seq[-1] != c["name"]:
            seq.append(c["name"])
    grams = Counter(tuple(seq[i:i + 4]) for i in range(len(seq) - 3))
    out["progression"] = None
    if grams:
        best, n = grams.most_common(1)[0]
        if n >= 2:
            out["progression"] = {
                "chords": list(best),
                "romans": [romans.get(x, "?") for x in best],
                "count": n,
            }

    # ── Cadences ──────────────────────────────────────────────────────────────
    cadences = []
    if tonic is not None:
        home = {"I", "i"}
        for a, b in zip(chords, chords[1:]):
            ra, rb = romans.get(a["name"], ""), romans.get(b["name"], "")
            kind = None
            if rb in home and ra in ("V", "v"):   kind = "authentic (V→I): the strongest 'coming home'"
            elif rb in home and ra in ("IV", "iv"): kind = "plagal (IV→I): the soft 'amen' ending"
            elif ra in ("V", "v") and rb in ("vi", "VI"): kind = "deceptive (V→vi): promises home, lands on the sad chord"
            if kind:
                cadences.append({"at": round(b["start"], 2), "from": a["name"],
                                 "to": b["name"], "type": kind})
    out["cadences"] = cadences[:12]

    # ── Borrowed chords ───────────────────────────────────────────────────────
    out["borrowed"] = [
        {"chord": n, "label": f["roman"], "why": f["role"]}
        for n, f in functions.items() if f["function"] in ("borrowed", "secondary dominant", "modal mixture")
    ]

    # ── Transition drills: which changes to practice, by frequency ──────────
    pair_count = Counter()
    for a, b in zip(chords, chords[1:]):
        if a["name"] != b["name"]:
            pair_count[(a["name"], b["name"])] += 1
    transitions = []
    for (a, b), n in pair_count.most_common(8):
        pa, pb = _parse(a), _parse(b)
        va = voicing(pa[0], pa[1], capo) if pa else None
        vb = voicing(pb[0], pb[1], capo) if pb else None
        hard = (va and not va["open"]) or (vb and not vb["open"])
        transitions.append({"from": a, "to": b, "count": n,
                            "barre": bool(hard)})
    out["transitions"] = transitions

    # ── Solo scales ───────────────────────────────────────────────────────────
    out["solo_scales"] = _pent_positions(tonic, major) if tonic is not None else []

    # ── Difficulty profile (1..5 each) ────────────────────────────────────────
    dur = float(meta.get("duration_sec") or (chords[-1]["end"] if chords else 0) or 1)
    uniq = len(time_per)
    barre_needed = 0
    for name in time_per:
        p = _parse(name)
        v = voicing(p[0], p[1], capo) if p else None
        if v and not v["open"]:
            barre_needed += 1
    changes_per_min = 60.0 * sum(pair_count.values()) / dur
    melody = tab.get("melody", [])
    notes_per_min = 60.0 * len(melody) / dur
    leaps = [abs(a["pitch"] - b["pitch"]) for a, b in zip(melody, melody[1:])]
    big_leaps = sum(1 for l in leaps if l > 7)

    def scale5(x, lo, hi):
        return max(1, min(5, 1 + round(4 * (x - lo) / max(1e-9, hi - lo))))

    d_chords = scale5(uniq + 2 * barre_needed, 3, 14)
    d_changes = scale5(changes_per_min, 4, 40)
    d_riff = scale5(notes_per_min + big_leaps, 20, 160)
    out["difficulty"] = {
        "chords": d_chords, "changes": d_changes, "riff": d_riff,
        "overall": round((d_chords + d_changes + d_riff) / 3),
        "barre_required": barre_needed > 0,
        "unique_chords": uniq,
        "changes_per_min": round(changes_per_min, 1),
    }

    # ── Practice plan (ordered, data-driven) ─────────────────────────────────
    top_names = [n for n, _ in time_per.most_common(4)]
    plan = [
        {"step": "shapes", "title": "Learn the shapes",
         "detail": f"Master the {min(uniq, 6)} most-used chords first: "
                   + ", ".join(top_names)
                   + (f" (capo {capo})" if capo else "")},
    ]
    if transitions:
        t0 = transitions[0]
        plan.append({"step": "changes", "title": "Drill the #1 change",
                     "detail": f"{t0['from']} → {t0['to']} happens {t0['count']}× "
                               f"— loop it at 0.5× with the metronome until clean"})
    plan.append({"step": "rhythm", "title": "Rhythm with the beat",
                 "detail": "Play along muted (just strum the beat), then add chords"})
    if melody:
        plan.append({"step": "riff", "title": "The lead line",
                     "detail": f"{len(melody)} melody notes — learn 2 bars at a time in the Tab view"})
    if tab.get("vocals"):
        plan.append({"step": "sing", "title": "Sing it",
                     "detail": "Vocals tab shows the pitch line — match it at 0.75×"})
    plan.append({"step": "full", "title": "Full run-through",
                 "detail": "0.75× start to finish, then 1×. You've got the song."})
    out["practice"] = plan

    return out


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python analyze.py <tab.json> [more tab.json ...]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"  ! missing: {p}")
            continue
        tab = json.loads(p.read_text())
        tab["analysis"] = build_analysis(tab)
        p.write_text(json.dumps(tab, indent=2))
        a = tab["analysis"]
        print(f"  ✓ {p.parent.name}: {len(a['romans'])} chords analyzed, "
              f"difficulty {a['difficulty']['overall']}/5, "
              f"{len(a['cadences'])} cadences, {len(a['borrowed'])} borrowed")


if __name__ == "__main__":
    main()
