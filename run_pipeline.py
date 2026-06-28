import json
import logging
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):   # UTF-8 so ✓/→ prints don't crash on Windows
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from pipeline import (
    separate_guitar, extract_beats, extract_notes,
    extract_vocals, extract_vocal_contour, BeatResult,
)

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_name(midi_pitch: int) -> str:
    octave = (midi_pitch // 12) - 1
    return f"{_NOTE_NAMES[midi_pitch % 12]}{octave}"


def find_tab_engine_binary() -> Path | None:
    """Locate the compiled C++ tab engine across platforms / generators."""
    build_dir = Path(__file__).parent / "tab_engine" / "build"
    for path in [
        build_dir / "tab_engine",
        build_dir / "tab_engine.exe",
        build_dir / "Release" / "tab_engine.exe",
        build_dir / "Debug" / "tab_engine.exe",
        build_dir / "RelWithDebInfo" / "tab_engine.exe",
    ]:
        if path.exists():
            return path
    return None


def process_audio(audio_path: Path | str, instrument: str = "guitar") -> Path:
    """Run the full pipeline on one file and return its output directory.

    Raises on a fatal stage failure. Safe to call in-process: the web server
    calls this directly so the ML models stay warm across jobs (the stages
    cache their loaded models at module level).
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"file not found — {audio_path}")

    pipeline_start = time.time()

    # ── Stage 1: Source Separation (required) ─────────────────────────────────
    print("=" * 60 + "\n  STAGE 1 — Source Separation\n" + "=" * 60)
    separation_result = separate_guitar(audio_path, instrument)
    out_dir = separation_result.guitar_stem_path.parent
    print(f"\n  ✓ Guitar stem : {separation_result.guitar_stem_path}")
    print(f"  ✓ Elapsed     : {separation_result.duration_seconds:.1f} s\n")

    # ── Stage 2: Beat Tracking (optional — degrade gracefully) ────────────────
    print("=" * 60 + "\n  STAGE 2 — Beat Tracking\n" + "=" * 60)
    try:
        beat_result = extract_beats(audio_path)
        print(f"\n  ✓ Beats {len(beat_result.beats)} · downbeats "
              f"{len(beat_result.downbeats)} · BPM {beat_result.bpm}\n")
    except Exception as exc:  # noqa: BLE001
        beat_result = BeatResult()
        print(f"\n  ⚠ Beat tracking failed: {exc} — continuing without beats.\n")

    # ── Stage 3: Pitch Extraction (required) ──────────────────────────────────
    print("=" * 60 + "\n  STAGE 3 — Pitch Extraction\n" + "=" * 60)
    notes = extract_notes(separation_result.guitar_stem_path)
    print(f"\n  ✓ Notes detected: {len(notes)}\n")

    notes_path = out_dir / "notes.json"
    notes_path.write_text(json.dumps(
        [{"start_time": n.start_time, "end_time": n.end_time,
          "pitch": n.pitch, "velocity": float(n.velocity)} for n in notes],
        indent=2,
    ))
    beats_path = out_dir / "beats.json"
    beats_path.write_text(json.dumps(
        {"beats": beat_result.beats, "downbeats": beat_result.downbeats, "bpm": beat_result.bpm},
        indent=2,
    ))

    print("=" * 60)
    print(f"  PIPELINE: {time.time() - pipeline_start:.1f} s · {len(notes)} notes · "
          f"{len(beat_result.beats)} beats")
    print("=" * 60)

    # ── Stage 4: C++ Tab Engine ───────────────────────────────────────────────
    print("=" * 60 + "\n  STAGE 4 — C++ Tab Engine\n" + "=" * 60)
    tab_engine_bin = find_tab_engine_binary()
    if tab_engine_bin is None:
        raise RuntimeError("C++ tab engine not built — see README (cmake --build build)")

    result = subprocess.run(
        [str(tab_engine_bin), str(notes_path),
         str(separation_result.guitar_stem_path), str(beats_path)],
        capture_output=False, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"C++ engine failed with code {result.returncode}")

    # ── Enrich into a playable arrangement + ASCII tab ────────────────────────
    tab_json = out_dir / "tab.json"
    if tab_json.exists():
        try:
            from arrange import arrange
            data = arrange(json.loads(tab_json.read_text()))
            data.setdefault("metadata", {})["instrument"] = instrument
            tab_json.write_text(json.dumps(data, indent=2))
            print(f"  ✓ Arrangement: {data['metadata'].get('num_melody', 0)} "
                  f"melody notes + {len(data.get('chords', []))} chords")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Could not build arrangement: {exc}")
        try:
            from render_tab import render
            (out_dir / "tab.txt").write_text(render(json.loads(tab_json.read_text())), encoding="utf-8")
            print(f"  ✓ Tab JSON : {tab_json}\n  ✓ ASCII tab: {out_dir / 'tab.txt'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Could not render ASCII tab: {exc}")

        # Vocals pitch line — transcribe the isolated Vocals stem.
        try:
            from arrange import clean_monophonic
            vstem = next(iter(out_dir.glob("*[Vv]ocals*.wav")), None)
            if vstem:
                vnotes = extract_vocals(vstem)
                vdicts = [{"start": n.start_time, "end": n.end_time, "pitch": n.pitch,
                           "duration": n.end_time - n.start_time, "name": midi_to_name(n.pitch)}
                          for n in vnotes]
                data = json.loads(tab_json.read_text())
                data["vocals"] = clean_monophonic(vdicts)
                data["vocal_pitch"] = extract_vocal_contour(vstem)   # continuous f0 line
                tab_json.write_text(json.dumps(data, indent=2))
                print(f"  ✓ Vocals   : {len(data['vocals'])} notes + "
                      f"{sum(1 for p in data['vocal_pitch'] if p[1] is not None)} pitch pts")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ Vocals transcription failed: {exc}")

    return out_dir


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python run_pipeline.py <path_to_audio_file>")
        sys.exit(1)
    try:
        process_audio(Path(sys.argv[1]))
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ✗ {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
