import logging
import sys
import time
import subprocess
import shutil
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from pipeline import separate_guitar, extract_beats, extract_notes

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_name(midi_pitch: int) -> str:
    octave = (midi_pitch // 12) - 1
    return f"{_NOTE_NAMES[midi_pitch % 12]}{octave}"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 run_pipeline.py <path_to_audio_file>")
        sys.exit(1)

    audio_path = Path(sys.argv[1])
    if not audio_path.exists():
        print(f"Error: file not found — {audio_path}")
        sys.exit(1)

    pipeline_start = time.time()

    # Stage 1: Source Separation
    print("=" * 60)
    print("  STAGE 1 — Source Separation")
    print("=" * 60)

    separation_result = separate_guitar(audio_path)

    print(f"\n  ✓ Guitar stem : {separation_result.guitar_stem_path}")
    print(f"  ✓ Elapsed     : {separation_result.duration_seconds:.1f} s\n")

    # Stage 2: Beat Tracking (uses original mix for drums/bass)
    print("=" * 60)
    print("  STAGE 2 — Beat Tracking")
    print("=" * 60)

    beat_result = extract_beats(audio_path)

    print(f"\n  ✓ Beats      : {len(beat_result.beats)}")
    print(f"  ✓ Downbeats  : {len(beat_result.downbeats)}")
    if beat_result.bpm is not None:
        print(f"  ✓ Est. BPM   : {beat_result.bpm}")
    print()

    # Stage 3: Pitch Extraction (uses guitar stem)
    print("=" * 60)
    print("  STAGE 3 — Pitch Extraction")
    print("=" * 60)

    notes = extract_notes(separation_result.guitar_stem_path)

    print(f"\n  ✓ Notes detected: {len(notes)}")

    if notes:
        print("\n  First 10 notes:")
        print(f"    {'#':>3}  {'Note':>5}  {'Start':>7}  {'End':>7}  {'Vel':>5}")
        print(f"    {'─' * 3}  {'─' * 5}  {'─' * 7}  {'─' * 7}  {'─' * 5}")

        for i, note in enumerate(notes[:10]):
            name = midi_to_name(note.pitch)
            print(
                f"    {i + 1:3d}  {name:>5}  "
                f"{note.start_time:6.2f}s  {note.end_time:6.2f}s  "
                f"{note.velocity:.2f}"
            )
    print()

    import json

    notes_output_path = separation_result.guitar_stem_path.parent / "notes.json"
    notes_data = [
        {
            "start_time": n.start_time,
            "end_time":   n.end_time,
            "pitch":      n.pitch,
            "velocity":   float(n.velocity),
        }
        for n in notes
    ]
    with open(notes_output_path, "w") as f:
        json.dump(notes_data, f, indent=2)

    print(f"  Notes JSON: {notes_output_path}")

    # Summary
    total_elapsed = time.time() - pipeline_start

    print("=" * 60)
    print("  PIPELINE COMPLETE")
    print("=" * 60)
    print(f"  Total time : {total_elapsed:.1f} s")
    print(f"  Input      : {audio_path.name}")
    print(f"  Guitar stem: {separation_result.guitar_stem_path.name}")
    print(f"  Beats      : {len(beat_result.beats)}")
    print(f"  Notes      : {len(notes)}")
    if beat_result.bpm:
        print(f"  BPM        : {beat_result.bpm}")
    print("=" * 60)

    # ── Stage 4: C++ Tab Engine ───────────────────────────────────────────────
    print("=" * 60)
    print("  STAGE 4 — C++ Tab Engine (Chord + Fingering)")
    print("=" * 60)

    tab_engine_bin = Path(__file__).parent / "tab_engine" / "build" / "tab_engine"

    if not tab_engine_bin.exists():
        print(f"  ✗ C++ binary not found at {tab_engine_bin}")
        print("    Run: cd tab_engine/build && make -j$(nproc)")
    else:
        result_cpp = subprocess.run(
            [str(tab_engine_bin), str(notes_output_path), str(separation_result.guitar_stem_path)],
            capture_output=False,   # let it print directly to terminal
            text=True,
        )
        if result_cpp.returncode == 0:
            print(f"\n  ✓ C++ engine completed successfully (output printed to the terminal)")
        else:
            print(f"  ✗ C++ engine failed with code {result_cpp.returncode}")


if __name__ == "__main__":
    main()
