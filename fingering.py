"""Give every note a string and fret that a hand can actually play.

Notes that start together (one strum) form an event. Within an event every note
gets its own string, nothing sits below the capo, and the fretted notes fit in
one hand window. Between events a dynamic program picks the sequence of hand
positions with the least TOTAL movement.

This replaces the C++ minimax solver, which let two notes of a chord share a
string (refine then cut one, arrange then deleted it) and only minimised the
single worst jump, so once one bad jump existed nothing else mattered.
"""

from __future__ import annotations

TUNE = [64, 59, 55, 50, 45, 40]   # open-string MIDI, index 0 = string 1 (high e)
MAX_FRET = 20
EVENT_S = 0.055                  # same as refine.event_ms
SPAN = 4                         # widest fretted stretch, in frets
MAX_NOTES = 6


def _playable(pitch, capo):
    return any(capo <= pitch - t <= MAX_FRET for t in TUNE)


def _events(notes):
    events = []
    for n in sorted(notes, key=lambda n: (n["start"], n["pitch"])):
        if events and n["start"] - events[-1][0]["start"] <= EVENT_S:
            events[-1].append(n)
        else:
            events.append([n])
    return events


def _placements(pitches, capo):
    """Every assignment of distinct strings (0 = high e) to these pitches that
    keeps the fretted notes within SPAN frets. Most-constrained note first."""
    opts = [[s for s in range(6) if capo <= p - TUNE[s] <= MAX_FRET] for p in pitches]
    order = sorted(range(len(pitches)), key=lambda i: len(opts[i]))
    pick, out = [0] * len(pitches), []

    def dfs(k, used, lo, hi):
        if k == len(order):
            out.append(tuple(pick))
            return
        i = order[k]
        for s in opts[i]:
            if s in used:
                continue
            f = pitches[i] - TUNE[s]
            nlo, nhi = (min(lo, f), max(hi, f)) if f > capo else (lo, hi)
            if nhi - nlo > SPAN:
                continue
            pick[i] = s
            dfs(k + 1, used | {s}, nlo, nhi)

    dfs(0, frozenset(), 99, -1)
    return out


def _candidates(event, capo, shape):
    """(kept notes, [(position, own cost, strings, frets, mean string)]).

    One candidate per hand position (lowest fretted fret, None = all open),
    the cheapest one. An event with no legal placement loses its middle note
    until it has one: the top and bottom carry the melody and the root.
    """
    ev = sorted((n for n in event if _playable(n["pitch"], capo)), key=lambda n: n["pitch"])
    if len(ev) > MAX_NOTES:
        ev = [ev[0]] + ev[-(MAX_NOTES - 1):]
    placements = []
    while ev:
        placements = _placements([n["pitch"] for n in ev], capo)
        if placements:
            break
        ev.pop(len(ev) // 2)
    best = {}
    for strings in placements:
        frets = [n["pitch"] - TUNE[s] for n, s in zip(ev, strings)]
        fretted = [f for f in frets if f > capo]
        pos = min(fretted) if fretted else None
        cost = (2.0 if fretted and max(fretted) - pos == SPAN else 0.0) + 0.1 * ((pos or capo) - capo)
        if shape and len(ev) >= 3:      # a strum of the current chord uses its diagram
            cost -= 1.5 * sum(1 for s, f in zip(strings, frets) if shape.get(s) == f)
        if pos not in best or cost < best[pos][1]:
            best[pos] = (pos, cost, strings, frets, sum(strings) / len(strings))
    return ev, list(best.values())


def _shape_at(chords, t, capo):
    """{string index: absolute fret} of the chord diagram sounding at t."""
    for c in chords or ():
        if c["start"] <= t < c["end"]:
            v = c.get("capoVoicing") or {}
            # voicing frets run low E -> high e and are relative to the capo
            return {5 - i: f + capo for i, f in enumerate(v.get("frets", [])) if f >= 0}
        if c["start"] > t:
            break
    return None


def _move(a, b, gap):
    shift = 0.0
    if a[0] is not None and b[0] is not None:
        shift = min(12, abs(a[0] - b[0])) * (0.4 if gap > 0.4 else 1.0)
    return shift + 0.25 * abs(a[4] - b[4])


def assign_fingering(notes, capo=0, chords=None):
    """Overwrite `string` (1..6) and `fret` on each note; return the notes placed."""
    chords = sorted(chords or [], key=lambda c: c["start"])
    steps = []                                   # (start, kept notes, candidates)
    for event in _events(notes):
        t = event[0]["start"]
        kept, cands = _candidates(event, capo, _shape_at(chords, t, capo))
        if cands:
            steps.append((t, kept, cands))
    if not steps:
        return []

    cost = [c[1] for c in steps[0][2]]
    back = [[0] * len(steps[0][2])]
    for k in range(1, len(steps)):
        gap = steps[k][0] - steps[k - 1][0]
        prev_notes, notes_k = steps[k - 1][1], steps[k][1]
        # A strum can straddle two events; then the late notes of one and the
        # early notes of the next must not share a string either. Rare, so
        # only checked when the events are that close.
        close = min(n["start"] for n in notes_k) - max(n["start"] for n in prev_notes) <= EVENT_S
        prev, new, bk = steps[k - 1][2], [], []
        for b in steps[k][2]:
            def total(i):
                c = cost[i] + _move(prev[i], b, gap)
                if close and any(sa == sb and nb["start"] - na["start"] <= EVENT_S
                                 for na, sa in zip(prev_notes, prev[i][2])
                                 for nb, sb in zip(notes_k, b[2])):
                    c += 50.0
                return c
            i = min(range(len(prev)), key=total)
            new.append(total(i) + b[1])
            bk.append(i)
        cost, back = new, back + [bk]

    j = min(range(len(cost)), key=lambda j: cost[j])
    placed = []
    for k in range(len(steps) - 1, -1, -1):
        _, kept, cands = steps[k]
        _, _, strings, frets, _ = cands[j]
        for n, s, f in zip(kept, strings, frets):
            n["string"], n["fret"] = s + 1, f
            placed.append(n)
        j = back[k][j]
    placed.sort(key=lambda n: (n["start"], n["pitch"]))
    return placed
