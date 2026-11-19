import json
from pathlib import Path

import pytest

from fingering import EVENT_S, SPAN, TUNE, _events, assign_fingering


def _check(notes, capo):
    by_string = {}
    for n in notes:
        by_string.setdefault(n["string"], []).append(n["start"])
    for s, starts in by_string.items():
        starts.sort()
        clash = [a for a, b in zip(starts, starts[1:]) if b - a <= EVENT_S]
        assert not clash, f"two notes at once on string {s} at {clash[:3]}"
    for ev in _events(notes):
        fretted = [n["fret"] for n in ev if n["fret"] > capo]
        if fretted:
            assert max(fretted) - min(fretted) <= SPAN
        for n in ev:
            assert n["fret"] >= capo
            assert TUNE[n["string"] - 1] + n["fret"] == n["pitch"]


def test_e_major_strum_is_the_open_shape():
    notes = [{"start": 1.0, "pitch": p} for p in (40, 47, 52, 56, 59, 64)]
    out = assign_fingering(notes, 0)
    frets = [n["fret"] for n in sorted(out, key=lambda n: -n["string"])]   # low E first
    assert frets == [0, 2, 2, 1, 0, 0]


def test_capo_and_unplayable_notes():
    notes = [{"start": 0.0, "pitch": 40}, {"start": 0.5, "pitch": 60},
             {"start": 0.5 + EVENT_S / 2, "pitch": 64}, {"start": 1.0, "pitch": 120}]
    out = assign_fingering(notes, 2)
    assert {n["pitch"] for n in out} == {60, 64}     # 40 is below capo 2, 120 off the neck
    _check(out, 2)


def test_real_song_is_playable():
    tab = Path("data/output/where_is_my_mind/tab.json")
    if not tab.exists():
        pytest.skip("no processed song on this machine")
    from refine import dechatter      # the pipeline merges re-detections before fingering
    notes = dechatter([{"start": n["start"], "duration": n["duration"], "pitch": n["pitch"]}
                       for n in json.loads(tab.read_text())["notes"]], 0.11)
    out = assign_fingering(notes, 4)
    assert len(out) > 0.9 * len([n for n in notes if any(4 <= n["pitch"] - t <= 20 for t in TUNE)])
    _check(out, 4)
