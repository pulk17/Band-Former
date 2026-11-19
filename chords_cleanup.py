"""Chord cleanup: key, simple level, flicker removal, capo, display names.

Not wired in yet. FIXES.md fix 1 says how. Tested on five songs (results in FIXES.md).
"""
from arrange import NOTE_PC, PC_NOTE, OPEN_SHAPES, SHAPES, _parse

# ── Chord cleanup: key, simple level, flicker removal, capo, display names ────
from collections import defaultdict

_MAJ_DEG, _MAJ_Q = (0, 2, 4, 5, 7, 9, 11), ("maj", "min", "min", "maj", "maj", "min", "dim")
_MIN_DEG, _MIN_Q = (0, 2, 3, 5, 7, 8, 10), ("min", "dim", "maj", "min", "min", "maj", "maj")
# Which triad a detected quality really is. Qualities missing here (5, sus2,
# sus4, dim, aug, m7b5) have no trustworthy third and are resolved from context.
_TRIAD = {"maj": "maj", "maj7": "maj", "6": "maj", "add9": "maj", "7": "maj",
          "min": "min", "min7": "min"}
_FLAT_NAMES = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
_SUFFIX = {"maj": "", "min": "m", "min7": "m7"}


def _diatonic(root_pc, tonic, major):
    """Triad quality the key expects on this root, or None if it is outside the key."""
    degs, quals = (_MAJ_DEG, _MAJ_Q) if major else (_MIN_DEG, _MIN_Q)
    rel = (root_pc - tonic) % 12
    return quals[degs.index(rel)] if rel in degs else None


def _triad_seconds(chords):
    """Seconds spent on each (root_pc, 'maj'|'min') among chords with a clear third."""
    sec = defaultdict(float)
    for c in chords:
        p = _parse(c["name"])
        if p and p[1] in _TRIAD:
            sec[(p[0], _TRIAD[p[1]])] += c["end"] - c["start"]
    return sec


def key_from_chords(chords):
    """(tonic_pc, is_major) that best explains the chord sequence, or None.

    Every key is scored by how many seconds of the song sit on its own chords,
    plus half the time spent on its tonic chord. A major key and its relative
    minor share every chord, so when the two score within 10% of each other the
    one whose tonic chord opens the song wins.
    """
    sec = _triad_seconds(chords)
    if not sec:
        return None

    def score(tonic, major):
        total = 0.5 * sec.get((tonic, "maj" if major else "min"), 0.0)
        for (pc, tri), s in sec.items():
            want = _diatonic(pc, tonic, major)
            if want is None:
                continue
            dominant_in_minor = (not major) and (pc - tonic) % 12 == 7 and tri == "maj"
            total += s if (want == tri or dominant_in_minor) else 0.3 * s
        return total

    best = max(((t, m) for t in range(12) for m in (True, False)), key=lambda k: score(*k))
    relative = ((best[0] + 9) % 12, False) if best[1] else ((best[0] + 3) % 12, True)
    first = next((p for p in (_parse(c["name"]) for c in chords) if p), None)
    if first and first[0] == relative[0] and score(*relative) >= 0.9 * score(*best):
        return relative
    return best


def use_flats(tonic, major):
    """True when the key signature is written with flats (F, Bb, Eb, Ab, Db and relatives)."""
    return tonic in ({5, 10, 3, 8, 1} if major else {2, 7, 0, 5, 10, 3})


def display_name(name, flats=False):
    """'C#:min7/G#' -> 'C#m7/G#' (or 'Dbm7/Ab' in a flat key). Internal names never change."""
    if ":" not in name:
        return name
    root, rest = name.split(":", 1)
    qual, _, bass = rest.partition("/")
    spell = (lambda n: _FLAT_NAMES.get(n, n)) if flats else (lambda n: n)
    return spell(root) + _SUFFIX.get(qual, qual) + ("/" + spell(bass) if bass else "")


def _merge_same(chords):
    out = []
    for c in chords:
        if out and out[-1]["name"] == c["name"] and c["start"] - out[-1]["end"] < 0.75:
            out[-1]["end"] = c["end"]
        else:
            out.append(dict(c))
    return out


def tidy_chords(chords, beat_len, min_beats=2.0, absorb=()):
    """Remove flicker: a chord shorter than `min_beats` (or named in `absorb`)
    is swallowed by a neighbour, so its time is kept and only its label goes.

    The shortest offender goes first. If the chords either side of it are the
    same chord, all three become one; otherwise the longer neighbour takes it.
    """
    chords = _merge_same([c for c in chords if c.get("name") not in ("silence", "unknown")])
    min_len = min_beats * beat_len
    length = lambda c: c["end"] - c["start"]
    while len(chords) > 1:
        bad = [k for k, c in enumerate(chords) if length(c) < min_len or c["name"] in absorb]
        if not bad:
            break
        i = min(bad, key=lambda k: length(chords[k]))
        prev = chords[i - 1] if i > 0 and chords[i]["start"] - chords[i - 1]["end"] < 0.75 else None
        nxt = chords[i + 1] if i + 1 < len(chords) and chords[i + 1]["start"] - chords[i]["end"] < 0.75 else None
        if prev is not None and (nxt is None or length(prev) >= length(nxt)):
            prev["end"] = chords[i]["end"]
        elif nxt is not None:
            nxt["start"] = chords[i]["start"]
        chords.pop(i)                               # no neighbour at all: an isolated blip, dropped
        chords = _merge_same(chords)
    return chords


def simple_chords(chords, key, beat_len):
    """The play-along level: plain major/minor triads, no slash bass, no noise.

    1. Every chord becomes a triad. One with no reliable third (5, sus, dim,
       aug) takes the quality the same root has elsewhere in the song, else what
       the key expects, else major.
    2. A root heard as both major and minor keeps only the key's version, unless
       the other one is used at least twice as much (a real borrowed chord).
    3. Flicker under 2 beats is absorbed by a neighbour.
    4. A chord that is out of key and fills under 3% of the song, or that occurs
       once for under a bar, is absorbed too.
    """
    votes = _triad_seconds(chords)

    def expected(pc):
        return _diatonic(pc, key[0], key[1]) if key else None

    def triad(pc, qual):
        want = expected(pc)
        if qual in _TRIAD:
            tri = _TRIAD[qual]
        elif qual in ("dim", "m7b5") and want == "dim":
            return "dim"
        else:
            heard_maj, heard_min = votes.get((pc, "maj"), 0.0), votes.get((pc, "min"), 0.0)
            if max(heard_maj, heard_min) >= 2.0:
                tri = "maj" if heard_maj >= heard_min else "min"
            elif want in ("maj", "min"):
                tri = want
            else:
                tri = "min" if qual in ("dim", "m7b5") else "maj"
        other = "min" if tri == "maj" else "maj"
        if want == other and votes.get((pc, tri), 0.0) < 2.0 * votes.get((pc, other), 0.0):
            tri = other
        return tri

    out = []
    for c in chords:
        p = _parse(c["name"])
        if p:
            out.append({"start": c["start"], "end": c["end"],
                        "name": f"{PC_NOTE[p[0]]}:{triad(*p)}",
                        "confidence": c.get("confidence", 0.0)})
    out = tidy_chords(out, beat_len, 2.0)

    total = sum(c["end"] - c["start"] for c in out) or 1.0
    seconds, count = defaultdict(float), defaultdict(int)
    for c in out:
        seconds[c["name"]] += c["end"] - c["start"]
        count[c["name"]] += 1
    absorb = set()
    for name, sec in seconds.items():
        pc, tri = _parse(name)
        rare = key is not None and expected(pc) != tri and sec < 0.03 * total
        one_off = count[name] == 1 and sec < 4 * beat_len
        if rare or one_off:
            absorb.add(name)
    return tidy_chords(out, beat_len, 2.0, absorb)


def pick_capo(chords):
    """Capo (0..7) that puts the most playing time on open shapes.

    Scored as a fraction of the song so the per-fret cost means something, and a
    capo is only suggested when it clearly beats playing without one.
    """
    sec = defaultdict(float)
    for c in chords:
        p = _parse(c["name"])
        if p and p[1] != "5":
            sec[p] += max(0.0, c["end"] - c["start"])
    total = sum(sec.values())
    if total <= 0:
        return 0

    def score(capo):
        open_time = sum(s for (pc, q), s in sec.items() if ((pc - capo) % 12, q) in OPEN_SHAPES)
        return open_time / total - 0.04 * capo

    best = max(range(8), key=score)
    return best if score(best) >= score(0) + 0.15 else 0


def bar_phase(beats, chords, meter=4):
    """Index (0..meter-1) of the beat that starts a bar: the phase most chord changes land on."""
    if len(beats) < 2 * meter:
        return 0
    hits = [0] * meter
    for c in chords[1:]:
        i = min(range(len(beats)), key=lambda k: abs(beats[k] - c["start"]))
        if abs(beats[i] - c["start"]) < 0.12:
            hits[i % meter] += 1
    return max(range(meter), key=lambda p: hits[p])
