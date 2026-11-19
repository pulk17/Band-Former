import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
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
    extract_vocals_and_contour, BeatResult,
)

# Bump whenever a change alters what ends up in tab.json, so the library can
# flag songs processed by an older pipeline as outdated.
PIPELINE_VERSION = 4

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_to_name(midi_pitch: int) -> str:
    octave = (midi_pitch // 12) - 1
    return f"{_NOTE_NAMES[midi_pitch % 12]}{octave}"


def _banner(title: str) -> None:
    print("=" * 60 + f"\n  {title}\n" + "=" * 60)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - missing or unreadable = nothing to keep
        return {}


def _write_tab(tab_json: Path, data: dict) -> None:
    """Atomic: a crash mid-write can't leave half a tab.json behind."""
    tmp = tab_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, tab_json)


@contextmanager
def _keep_previous(tab_json: Path):
    """The C++ engine overwrites tab.json the moment it runs. If anything after
    that fails, put the previous tab back (or remove the half-built one), so a
    failed run never leaves a broken song marked as done."""
    prev = tab_json.with_name("tab.prev.json")
    had = tab_json.exists()
    if had:
        shutil.copy2(tab_json, prev)
    try:
        yield
    except BaseException:
        if had:
            os.replace(prev, tab_json)
        else:
            tab_json.unlink(missing_ok=True)
        raise
    prev.unlink(missing_ok=True)


def free_gpu() -> None:
    """Drop every cached model and its VRAM.

    On an 8 GB card under Windows, keeping separation + YourMT3 + beat_this
    loaded together doesn't raise out-of-memory: the driver silently spills
    into shared system memory and the next stage runs many times slower.
    Reloading a model costs seconds; the spill costs minutes.
    """
    import gc
    from pipeline.stages import beat_tracking, pitch_extraction, separation
    separation.release_models()
    beat_tracking._predictor = None
    pitch_extraction._piano_tr = None
    try:
        import mt3_infer
        mt3_infer.clear_cache()
    except Exception:  # noqa: BLE001
        pass
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _warn(data: dict, msg: str) -> None:
    """Optional stage failed: say so in the console AND in the song itself."""
    print(f"  ⚠ {msg}")
    data.setdefault("metadata", {}).setdefault("warnings", []).append(msg)


def finish_tab(data: dict, stem_path=None) -> dict:
    """Engine output -> a tab a person can play, in the order that matters:

    1. refine  — drop notes the stem has no energy for (skipped for tiles songs:
                 their notes were read off the video, there is nothing to verify);
    2. arrange — chords, key, capo, lead vs rhythm;
    3. fingering, last — strings and frets for the notes that survived, above
                 the capo that arrange chose;
    4. let each string ring until it is next played (audio songs only — tiles
       notes already carry their real lengths), then analyse.

    Refine is optional (a failure leaves the raw notes and a warning); the rest
    is required and raises.
    """
    from arrange import arrange
    from analyze import build_analysis
    from fingering import assign_fingering
    from refine import refine, resolve_strings
    from tuning import knob
    if stem_path:
        try:
            data = refine(data, stem_path)
        except Exception as exc:  # noqa: BLE001
            _warn(data, f"Note cleanup skipped ({exc}); the tab shows every raw note.")
    data = arrange(data)
    data["notes"] = assign_fingering(data["notes"], data["metadata"]["capo"], data["chords"])
    resolve_strings(data["notes"], float(knob("refine", "ring_seconds", 1.6)) if stem_path else 0.0)
    data["melody"] = [n for n in data["notes"] if n.get("voice") == "lead"]
    data["analysis"] = build_analysis(data)
    return data


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


def _run_engine(notes_path: Path, stem: Path, beats_path: Path, chord_mix: Path) -> None:
    exe = find_tab_engine_binary()
    if exe is None:
        raise RuntimeError("C++ tab engine not built — see README (cmake --build tab_engine/build)")
    r = subprocess.run([str(exe), str(notes_path), str(stem), str(beats_path), str(chord_mix)],
                       capture_output=False, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"C++ engine failed with code {r.returncode}")


def _vocal_stem(out_dir: Path) -> Path | None:
    # Prefer the Roformer vocals (two-stage mode) over htdemucs'.
    cands = sorted(out_dir.glob("*[Vv]ocals*.wav"),
                   key=lambda p: 0 if "roformer" in p.name.lower() else 1)
    return cands[0] if cands else None


def _vocals(vstem: Path, model: str):
    """(vocal notes, pitch contour) from ONE pitch-tracking pass."""
    from arrange import clean_monophonic
    vnotes, contour = extract_vocals_and_contour(vstem, model_choice=model)
    vdicts = [{"start": n.start_time, "end": n.end_time, "pitch": n.pitch,
               "duration": n.end_time - n.start_time, "name": midi_to_name(n.pitch)}
              for n in vnotes]
    return clean_monophonic(vdicts), contour


def process_audio(audio_path: Path | str, instrument: str = "guitar", on_stage=None, options=None) -> Path:
    """Run the full pipeline on one file and return its output directory.

    Raises on a failed required stage (separation, transcription, engine,
    arrangement) and leaves the song's previous tab.json untouched. Optional
    stages (beats, note cleanup, vocals) record a warning in the tab instead.
    """
    options = options or {}
    reprocess = options.get("reprocess", False)
    run_beats = options.get("run_beats", True)
    run_vocals = options.get("run_vocals", True)
    vocal_model = options.get("vocal_model", "auto")
    from pipeline.config import OUTPUT_DIR
    from pipeline.stages.separation import ensure_chord_mix

    report = on_stage or (lambda _stage: None)
    audio_path = Path(audio_path)
    if not audio_path.exists() and not reprocess:
        raise FileNotFoundError(f"file not found — {audio_path}")

    pipeline_start = time.time()
    out_dir = OUTPUT_DIR / audio_path.stem
    pending = {"metadata": {}}                     # warnings raised before the tab exists

    # ── 1. Source separation (required) ───────────────────────────────────────
    if reprocess:
        report("Skipping separation")
        _banner("STAGE 1 — Source Separation (SKIPPED)")
        # "all" songs are transcribed from the combined stem, piano/bass/other
        # from their own stem — not the guitar one.
        want = "combined" if instrument == "all" else instrument
        if instrument == "all":
            ensure_chord_mix(out_dir)
        stems = sorted(out_dir.glob("*.wav"))
        stem = (next((p for p in stems if want in p.name.lower()), None)
                or next((p for p in stems if "guitar" in p.name.lower()), None))
        if not stem:
            raise FileNotFoundError(f"Reprocess failed: no {want} stem found in {out_dir}")
        old_meta = _read_json(out_dir / "tab.json").get("metadata", {})
        sep_warning = old_meta.get("warning", "") if old_meta.get("instrument") == instrument else ""
    else:
        _banner("STAGE 1 — Source Separation")
        sep = separate_guitar(audio_path, instrument, quality=options.get("separation_quality"),
                              on_stage=report)
        stem, out_dir = sep.guitar_stem_path, sep.guitar_stem_path.parent
        sep_warning = getattr(sep, "warning", "")
        print(f"\n  ✓ Stem   : {stem}\n  ✓ Elapsed: {sep.duration_seconds:.1f} s\n")
        free_gpu()

    # ── 2. Beats (optional) ───────────────────────────────────────────────────
    beats_path = out_dir / "beats.json"
    if run_beats and not (reprocess and beats_path.exists()):
        report("Tracking beats")
        _banner("STAGE 2 — Beat Tracking")
        beat_result = BeatResult()
        try:
            beat_result = extract_beats(audio_path)
            print(f"\n  ✓ Beats {len(beat_result.beats)} · downbeats "
                  f"{len(beat_result.downbeats)} · BPM {beat_result.bpm}\n")
            if not beat_result.beats:
                _warn(pending, "No beats found; chords and bar lines are not beat-aligned.")
        except Exception as exc:  # noqa: BLE001
            _warn(pending, f"Beat tracking failed ({exc}); chords and bar lines are not beat-aligned.")
        beats_path.write_text(json.dumps({"beats": beat_result.beats,
                                          "downbeats": beat_result.downbeats,
                                          "bpm": beat_result.bpm}, indent=2))
    elif not beats_path.exists():                 # the engine needs the file
        beats_path.write_text(json.dumps({"beats": [], "downbeats": [], "bpm": 0}))

    # ── 3. Notes (required) ───────────────────────────────────────────────────
    notes_path = out_dir / "notes.json"
    if reprocess and notes_path.exists() and not options.get("retranscribe"):
        _banner("STAGE 3 — Pitch Extraction (SKIPPED)")
    else:
        report("Transcribing notes")
        _banner("STAGE 3 — Pitch Extraction")
        notes = extract_notes(stem, instrument=instrument)
        print(f"\n  ✓ Notes detected: {len(notes)}\n")
        notes_path.write_text(json.dumps(
            [{"start_time": n.start_time, "end_time": n.end_time,
              "pitch": n.pitch, "velocity": float(n.velocity)} for n in notes], indent=2))
        free_gpu()

    # ── 4. Engine + arrangement (required), vocals (optional) ─────────────────
    report("Building tab")
    _banner("STAGE 4 — C++ Tab Engine")
    tab_json = out_dir / "tab.json"
    old = _read_json(tab_json)                     # vocals etc. the engine is about to wipe
    # Chords come from the full instrumental mix (bass + keys carry the
    # harmony), not from the transcribed stem alone.
    chord_mix = ensure_chord_mix(out_dir) or stem
    with _keep_previous(tab_json):
        _run_engine(notes_path, stem, beats_path, chord_mix)
        data = json.loads(tab_json.read_text())
        meta = data.setdefault("metadata", {})
        meta["instrument"] = instrument
        meta["pipeline_version"] = PIPELINE_VERSION
        meta["warnings"] = list(pending["metadata"].get("warnings", []))
        if sep_warning:
            meta["warning"] = sep_warning

        report("Arranging")
        data = finish_tab(data, stem)
        print(f"  ✓ Arrangement: {len(data['melody'])} melody notes + {len(data['chords'])} chords")
        try:
            from render_tab import render
            (out_dir / "tab.txt").write_text(render(data), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            _warn(data, f"ASCII tab not written ({exc}).")

        if run_vocals:
            report("Extracting vocals")
            vstem = _vocal_stem(out_dir)
            if vstem is None:
                _warn(data, "No vocals stem (interrupted separation?); vocals skipped. "
                            "Reprocess with vocals enabled.")
            else:
                try:
                    data["vocals"], data["vocal_pitch"] = _vocals(vstem, vocal_model)
                    print(f"  ✓ Vocals: {len(data['vocals'])} notes")
                except Exception as exc:  # noqa: BLE001
                    _warn(data, f"Vocals failed ({exc}); kept the previous vocals, if any.")
        for k in ("vocals", "vocal_pitch"):        # the engine wiped them; keep the old ones
            if k not in data and k in old:
                data[k] = old[k]

        _write_tab(tab_json, data)
    print(f"  ✓ Done in {time.time() - pipeline_start:.0f} s: {tab_json}")
    return out_dir


def revocals_only(song_stem: str, vocal_model: str = "auto", on_stage=None) -> Path:
    """Re-extract JUST the vocals of an already-processed song (different model,
    or after a failed vocals stage) and merge them into its tab.json. Runs in
    the same worker queue but takes seconds, not minutes — no separation, no
    transcription."""
    from pipeline.config import OUTPUT_DIR

    out_dir = OUTPUT_DIR / song_stem
    tab_json = out_dir / "tab.json"
    if not tab_json.exists():
        raise FileNotFoundError(f"No tab.json for '{song_stem}' — process the song first")
    vstem = _vocal_stem(out_dir)
    if vstem is None:
        raise FileNotFoundError(f"No vocals stem in {out_dir} — full reprocess needed")

    if on_stage:
        on_stage("Extracting vocals")
    vocals, contour = _vocals(vstem, vocal_model)
    data = json.loads(tab_json.read_text())
    data["vocals"], data["vocal_pitch"] = vocals, contour
    _write_tab(tab_json, data)
    print(f"  ✓ Vocals redone ({vocal_model}): {len(vocals)} notes")
    return out_dir


def process_tiles_video(audio_path: Path | str, video_path: Path | str,
                        on_stage=None, options=None) -> Path:
    """Tiles mode: notes come from the VIDEO (exact), everything else reuses
    the normal chain — beats + chords from the audio, arrange, analysis."""
    options = options or {}
    from pipeline.config import OUTPUT_DIR

    report = on_stage or (lambda _stage: None)
    audio_path, video_path = Path(audio_path), Path(video_path)
    # The video is always saved as data/video/<song id>.<ext>; the audio may be
    # the song folder's own converted wav when the original source is gone.
    stem = video_path.stem
    out_dir = OUTPUT_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    pending = {"metadata": {}}

    report("Reading tiles video")
    _banner("STAGE 1 — Tiles extraction (video)")
    from tiles.extract import extract_notes as tiles_extract, write_outputs, validate_octave
    result = tiles_extract(video_path, progress=report)
    try:                       # anchor the absolute octave against the audio
        shift = validate_octave(result["notes"], audio_path)
        if shift:
            for n in result["notes"]:
                n["pitch"] += shift
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ Octave validation skipped: {exc}")
    notes_path = write_outputs(result, out_dir)

    beats_path = out_dir / "beats.json"
    if options.get("reprocess") and beats_path.exists():
        print("  ✓ Reusing existing beats (reprocess)")
    else:
        report("Tracking beats")
        beat_result = BeatResult()
        try:
            beat_result = extract_beats(audio_path)
            if not beat_result.beats:
                _warn(pending, "No beats found; chords and bar lines are not beat-aligned.")
        except Exception as exc:  # noqa: BLE001
            _warn(pending, f"Beat tracking failed ({exc}); chords and bar lines are not beat-aligned.")
        beats_path.write_text(json.dumps(
            {"beats": beat_result.beats, "downbeats": beat_result.downbeats,
             "bpm": beat_result.bpm}, indent=2))

    # Engine needs a WAV (chroma). Convert the source audio once.
    wav = out_dir / f"{stem}_audio.wav"
    if not wav.exists():
        subprocess.run(["ffmpeg", "-y", "-i", str(audio_path), "-ac", "1",
                        "-ar", "44100", str(wav)], capture_output=True)
    if not wav.exists():
        raise RuntimeError("ffmpeg could not convert audio for chord analysis")

    report("Building tab")
    tab_json = out_dir / "tab.json"
    with _keep_previous(tab_json):
        _run_engine(notes_path, wav, beats_path, wav)
        report("Arranging")
        data = json.loads(tab_json.read_text())
        meta = data.setdefault("metadata", {})
        meta["instrument"] = "tiles"
        meta["pipeline_version"] = PIPELINE_VERSION
        meta["tiles_mode"] = result["mode"]
        meta["warnings"] = list(pending["metadata"].get("warnings", []))
        # No stem: these notes were read off the video, not guessed from
        # audio, so there is nothing to verify (see tools/tiles_audit.py).
        data = finish_tab(data)
        # Raw video notes (with hand colors) for the piano-roll view — the
        # guitar fingering drops piano notes a guitar can't reach.
        data["roll"] = [{"start": n["start_time"],
                         "duration": round(n["end_time"] - n["start_time"], 4),
                         "pitch": n["pitch"], "hand": n.get("hand", "")}
                        for n in result["notes"]]
        _write_tab(tab_json, data)
    print(f"  ✓ Tiles arrangement: {len(data['notes'])} notes, "
          f"{len(data['chords'])} chords ({result['mode']})")
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
