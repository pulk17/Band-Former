"""Chord detection with lv-chordia (ISMIR 2019 large-vocabulary chord model).

A 5-network ensemble over a CQT of the full mix, decoded by an HMM. Output is
converted to the app's `ROOT:QUAL[/BASS]` grammar (sharps, qualities from
arrange.SHAPES).
"""
import os

_ROOTS = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6,
          "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11, "Cb": 11}
_PC = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_QUAL = {"maj": "maj", "maj6": "6", "maj7": "maj7", "maj9": "maj7", "min": "min",
         "min6": "min", "min7": "min7", "min9": "min7", "7": "7", "9": "7", "11": "7",
         "13": "7", "sus2": "sus2", "sus4": "sus4", "dim": "dim", "dim7": "dim",
         "hdim7": "m7b5", "aug": "aug", "minmaj7": "min", "5": "5"}
_BASS = {"2": 2, "b3": 3, "3": 4, "4": 5, "5": 7, "6": 9, "b7": 10, "7": 11}


def to_app_name(label):
    """'Bb:min7/b7' -> 'A#:min7/G#'; 'N', 'X' and unknown qualities -> None."""
    root, _, rest = label.partition(":")
    if root not in _ROOTS:
        return None
    rest, _, bass = (rest or "maj").partition("/")
    qual = _QUAL.get(rest.split("(")[0])
    if qual is None:
        return None
    pc = _ROOTS[root]
    name = f"{_PC[pc]}:{qual}"
    return name + f"/{_PC[(pc + _BASS[bass]) % 12]}" if bass in _BASS else name


def detect_chords(audio_path):
    from lv_chordia.chord_recognition import MODEL_NAMES, chord_recognition
    from lv_chordia.mir.common import CACHE_DATA_PATH
    # A missing weight file is skipped silently and the net runs untrained.
    missing = [m for m in MODEL_NAMES
               if not os.path.exists(os.path.join(CACHE_DATA_PATH, m + ".sdict"))]
    if missing:
        raise RuntimeError(f"lv-chordia weights missing in {CACHE_DATA_PATH}: reinstall lv-chordia")
    out = []
    # Relative paths resolve against the package folder, not the cwd.
    path = os.path.abspath(audio_path)
    for r in chord_recognition(audio_path=path, chord_dict_name="submission"):
        name = to_app_name(r["chord"])
        if name and r["end_time"] > r["start_time"]:
            out.append({"start": r["start_time"], "end": r["end_time"],
                        "name": name, "confidence": 0.5})
    return out


if __name__ == "__main__":
    assert to_app_name("Bb:min7/b7") == "A#:min7/G#"
    assert to_app_name("E:maj") == "E:maj" and to_app_name("C#:min/b3") == "C#:min/E"
    assert to_app_name("G:sus4(b7)") == "G:sus4" and to_app_name("D:hdim7") == "D:m7b5"
    assert to_app_name("N") is None and to_app_name("X") is None
    print("ok")
