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


def process_audio(audio_path: Path | str, instrument: str = "guitar", on_stage=None, options=None) -> Path:
    """Run the full pipeline on one file and return its output directory.

    Raises on a fatal stage failure. Safe to call in-process: the web server
    calls this directly so the ML models stay warm across jobs (the stages
    cache their loaded models at module level).
    """
    options = options or {}
    reprocess = options.get("reprocess", False)
    run_beats = options.get("run_beats", True)
    run_vocals = options.get("run_vocals", True)
    vocal_model = options.get("vocal_model", "auto")
    from pipeline.config import OUTPUT_DIR

    def report(stage: str):
        if on_stage:
            on_stage(stage)
    audio_path = Path(audio_path)
    if not audio_path.exists() and not reprocess:
        raise FileNotFoundError(f"file not found — {audio_path}")

    pipeline_start = time.time()
    out_dir = OUTPUT_DIR / audio_path.stem

    # ── Stage 1: Source Separation (required) ─────────────────────────────────
    if reprocess:
        report("Skipping separation")
        print("=" * 60 + "\n  STAGE 1 — Source Separation (SKIPPED)\n" + "=" * 60)
        guitar_stem_path = next(iter(out_dir.glob("*[Gg]uitar*.wav")), None)
        if not guitar_stem_path:
            raise FileNotFoundError(f"Reprocess failed: no guitar stem found in {out_dir}")
        class _DummyResult: pass
        separation_result = _DummyResult()
        separation_result.guitar_stem_path = guitar_stem_path
        print(f"\n  ✓ Using existing guitar stem: {guitar_stem_path}\n")
    else:
        report("Separating stems")
        print("=" * 60 + "\n  STAGE 1 — Source Separation\n" + "=" * 60)
        separation_result = separate_guitar(audio_path, instrument)
        out_dir = separation_result.guitar_stem_path.parent
        print(f"\n  ✓ Guitar stem : {separation_result.guitar_stem_path}")
        print(f"  ✓ Elapsed     : {separation_result.duration_seconds:.1f} s\n")

    # ── Stage 2: Beat Tracking (optional — degrade gracefully) ────────────────
    beats_path = out_dir / "beats.json"
    beat_result = BeatResult()
    if not run_beats or (reprocess and beats_path.exists()):
        print("=" * 60 + "\n  STAGE 2 — Beat Tracking (SKIPPED)\n" + "=" * 60)
        print(f"\n  ✓ Skipping beat tracking\n")
        if beats_path.exists():
            try:
                bdata = json.loads(beats_path.read_text())
                beat_result.beats = bdata.get("beats", [])
                beat_result.downbeats = bdata.get("downbeats", [])
                beat_result.bpm = bdata.get("bpm", 0)
            except Exception: pass
    else:
        report("Tracking beats")
        print("=" * 60 + "\n  STAGE 2 — Beat Tracking\n" + "=" * 60)
        try:
            beat_result = extract_beats(audio_path)
            print(f"\n  ✓ Beats {len(beat_result.beats)} · downbeats "
                  f"{len(beat_result.downbeats)} · BPM {beat_result.bpm}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  ⚠ Beat tracking failed: {exc} — continuing without beats.\n")

    # ── Stage 3: Pitch Extraction (required) ──────────────────────────────────
    notes_path = out_dir / "notes.json"
    notes = []
    if reprocess and notes_path.exists():
        print("=" * 60 + "\n  STAGE 3 — Pitch Extraction (SKIPPED)\n" + "=" * 60)
        print(f"\n  ✓ Skipping pitch extraction (found {notes_path})\n")
        try:
            ndata = json.loads(notes_path.read_text())
            notes = ndata
        except Exception: pass
    else:
        report("Transcribing notes")
        print("=" * 60 + "\n  STAGE 3 — Pitch Extraction\n" + "=" * 60)
        notes = extract_notes(separation_result.guitar_stem_path)
        print(f"\n  ✓ Notes detected: {len(notes)}\n")

        notes_path.write_text(json.dumps(
            [{"start_time": n.start_time, "end_time": n.end_time,
              "pitch": n.pitch, "velocity": float(n.velocity)} for n in notes],
            indent=2,
        ))
        
    if not beats_path.exists() or (not reprocess and run_beats) or (reprocess and not beats_path.exists() and run_beats):
        beats_path.write_text(json.dumps(
            {"beats": beat_result.beats, "downbeats": beat_result.downbeats, "bpm": beat_result.bpm},
            indent=2,
        ))

    print("=" * 60)
    print(f"  PIPELINE: {time.time() - pipeline_start:.1f} s · {len(notes)} notes · "
          f"{len(beat_result.beats)} beats")
    print("=" * 60)

    # ── Stage 4: C++ Tab Engine ───────────────────────────────────────────────
    report("Building tab")
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
        report("Arranging")
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
        if not run_vocals:
            print("  ✓ Skipping vocals transcription (disabled)")
        else:
            report("Extracting vocals")
            try:
                from arrange import clean_monophonic
                vstem = next(iter(out_dir.glob("*[Vv]ocals*.wav")), None)
                if vstem:
                    vnotes = extract_vocals(vstem, model_choice=vocal_model)
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
