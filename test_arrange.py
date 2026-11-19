from arrange import OPEN_SHAPES, PC_NOTE, _chord_pcs

OPEN_PCS = [4, 9, 2, 7, 11, 4]   # E A D G B e


def test_open_shapes_play_exactly_the_chord():
    for (pc, qual), (name, frets) in OPEN_SHAPES.items():
        played = {(OPEN_PCS[s] + f) % 12 for s, f in enumerate(frets) if f >= 0}
        want = _chord_pcs(f"{PC_NOTE[pc]}:{qual}")
        # open C7/C6 omit the 5th, like most guitar voicings may
        assert played in (want, want - {(pc + 7) % 12}), name
