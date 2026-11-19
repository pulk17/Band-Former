from arrange import OPEN_SHAPES, PC_NOTE, SHAPES, _chord_pcs, voicing

OPEN_PCS = [4, 9, 2, 7, 11, 4]   # E A D G B e


def test_open_shapes_play_exactly_the_chord():
    for (pc, qual), (name, frets) in OPEN_SHAPES.items():
        played = {(OPEN_PCS[s] + f) % 12 for s, f in enumerate(frets) if f >= 0}
        want = _chord_pcs(f"{PC_NOTE[pc]}:{qual}")
        # open C7/C6 omit the 5th, like most guitar voicings may
        assert played in (want, want - {(pc + 7) % 12}), name


def test_every_voicing_plays_the_chord_low_on_the_neck():
    for t in range(12):
        for qual in SHAPES:
            v = voicing(t, qual)
            played = {(OPEN_PCS[s] + f) % 12 for s, f in enumerate(v["frets"]) if f >= 0}
            want = _chord_pcs(f"{PC_NOTE[t]}:{qual}")
            assert played in (want, want - {(t + 7) % 12}), (PC_NOTE[t], qual, v)
            assert v["baseFret"] <= 7, (PC_NOTE[t], qual, v)
    assert voicing(11, "min")["frets"] == [-1, 2, 4, 4, 3, 2]     # Bm: A-shape at fret 2
    assert voicing(5, "maj")["frets"] == [1, 3, 3, 2, 1, 1]       # F: E-shape at fret 1
