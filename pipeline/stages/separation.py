import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from audio_separator.separator import Separator

from pipeline.config import (
    DEVICE,
    INSTRUMENT_SPLIT_MODELS,
    MODEL_CACHE_DIR,
    NORMALIZATION_THRESHOLD,
    OUTPUT_DIR,
    SEPARATION_PROFILES,
    SEPARATION_QUALITY,
    SUPPORTED_AUDIO_EXTENSIONS,
    VOCAL_SPLIT_MODEL,
)

logger = logging.getLogger(__name__)


@dataclass
class SeparationResult:
    guitar_stem_path: Path
    duration_seconds: float
    source_file: Path
    warning: str = ""   # e.g. "stem nearly silent" — surfaced in the UI


# Cache loaded models (keyed by model file) so repeated calls in one process
# (e.g. the web server) don't reload weights every time. output_dir is
# re-pointed per call.
_separators: dict = {}


def _get_separator(model_filename: str, stem_output_dir: Path):
    sep = _separators.get(model_filename)
    if sep is None:
        sep = Separator(
            output_dir=str(stem_output_dir),
            model_file_dir=str(MODEL_CACHE_DIR),
            log_level=logging.WARNING,
            output_format="WAV",
            normalization_threshold=NORMALIZATION_THRESHOLD,
        )
        logger.info("Loading separation model: %s", model_filename)
        sep.load_model(model_filename=model_filename)
        _separators[model_filename] = sep
    sep.output_dir = str(stem_output_dir)
    # audio-separator bakes output_dir into the model instance at load_model()
    # time; re-point it too, or every later job writes into the FIRST job's
    # folder (stems end up in another song's directory and the job fails).
    if getattr(sep, "model_instance", None) is not None:
        sep.model_instance.output_dir = str(stem_output_dir)
    return sep


def release_models(keep: str | None = None) -> None:
    """Drop cached separators (and their VRAM). `keep` spares one model file."""
    for key in [k for k in _separators if k != keep]:
        _separators.pop(key, None)
    try:
        import gc

        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _is_oom(exc: Exception) -> bool:
    return "out of memory" in str(exc).lower() or type(exc).__name__ == "OutOfMemoryError"


def _separate_with_retry(model_file: str, stem_output_dir: Path, source: Path, custom: dict):
    """Run one separation pass, retrying once after freeing the other cached
    models. Three models warm at a time (two separators + the transcriber) is
    enough to OOM a smaller card, and reloading takes seconds against a job
    that takes minutes."""
    sep = _get_separator(model_file, stem_output_dir)
    try:
        return sep.separate(str(source), custom_output_names=custom)
    except Exception as exc:  # noqa: BLE001
        if not _is_oom(exc):
            raise
        logger.warning("Out of memory during separation — freeing other models and retrying once.")
        release_models(keep=model_file)
        sep = _get_separator(model_file, stem_output_dir)
        return sep.separate(str(source), custom_output_names=custom)


# Stem keywords in the order we prefer to recognise them, matched against the
# LAST parenthesised group of a filename.
_STEM_KEYS = ("vocals", "vocal", "drums", "bass", "guitar", "piano", "other", "instrumental")
_CANON = {"vocal": "Vocals", "vocals": "Vocals", "drums": "Drums", "bass": "Bass",
          "guitar": "Guitar", "piano": "Piano", "other": "Other", "instrumental": "Instrumental"}


def _stem_kind(filename: str) -> str | None:
    """Which source a separator wrote, read from the last `(...)` group.

    Reading the LAST group matters: in two-stage mode the input file is already
    called `..._(Instrumental)_roformer.wav`, so a naive substring search would
    label every stage-B stem "instrumental"."""
    groups = re.findall(r"\(([^)]+)\)", filename) or [Path(filename).stem]
    for group in reversed(groups):
        low = group.lower()
        for key in _STEM_KEYS:
            if key in low:
                return _CANON[key]
    return None


def _canonicalize_stems(stem_output_dir: Path, name: str, tag: str,
                        two_stage: bool, before: set[Path]) -> None:
    """Rename this run's outputs to `{name}_(Stem)_{tag}.wav`.

    `custom_output_names` only works when its keys match the model's internal
    stem labels exactly, and those differ per model (and aren't published for
    some, e.g. BS-Roformer-SW ships no stem list). Rather than guess, we let the
    model name files however it likes and fix them afterwards — so swapping in a
    new separation model can't break the glob contracts the rest of the
    pipeline depends on."""
    for path in sorted(stem_output_dir.glob("*.wav")):
        if path in before:
            continue
        kind = _stem_kind(path.name)
        if kind is None:
            logger.warning("Unrecognised separator output (left as-is): %s", path.name)
            continue
        # In two-stage mode the input had no vocals left, so whatever the model
        # calls "vocals" is residue — name it so the vocal globs skip it.
        if kind == "Vocals" and two_stage:
            kind = "Residual"
        target = stem_output_dir / f"{name}_({kind})_{tag}.wav"
        if path == target:
            continue
        try:
            if target.exists():
                target.unlink()
            path.rename(target)
            logger.info("Normalised stem name: %s -> %s", path.name, target.name)
        except OSError as exc:
            logger.warning("Could not rename %s (%s)", path.name, exc)


def _build_combined_stem(stem_output_dir: Path, stems: list[Path]) -> Path | None:
    """Sum the pitched instrument stems (guitar+bass+piano+other) into one mono WAV."""
    import numpy as np
    import soundfile as sf
    parts = [p for p in stems if any(k in p.name.lower() for k in ("guitar", "bass", "piano", "other"))]
    if not parts:
        return None
    mix, sr = None, None
    for p in parts:
        data, s = sf.read(str(p), dtype="float32")
        if data.ndim > 1:                       # downmix so channel counts can't clash
            data = data.mean(axis=1)
        if mix is None:
            mix, sr = data, s
        else:
            n = min(len(mix), len(data))
            mix = mix[:n] + data[:n]
    peak = float(np.max(np.abs(mix))) or 1.0
    if peak > 1.0:
        mix = mix / peak
    # Model-agnostic name: which separator produced the parts doesn't matter,
    # and hard-coding one model's tag here made the file lie once we could swap.
    out = stem_output_dir / f"{stem_output_dir.name}_(Combined).wav"
    sf.write(str(out), mix, sr)
    return out


def ensure_chord_mix(stem_output_dir: Path) -> Path | None:
    """The WAV chords should be analyzed from: the full instrumental (no vocals,
    no drums). Reuses an existing combined stem or builds one from the stems on
    disk; returns None when no stems exist yet."""
    stem_output_dir = Path(stem_output_dir)
    existing = next(iter(stem_output_dir.glob("*[Cc]ombined*.wav")), None)
    if existing:
        return existing
    stems = [p for p in sorted(stem_output_dir.glob("*.wav")) if "(Combined)" not in p.name]
    return _build_combined_stem(stem_output_dir, stems)


def separate_guitar(audio_path: str | Path, instrument: str = "guitar",
                    quality: str | None = None, on_stage=None) -> SeparationResult:
    """Separate a stem from a full mix and return it for transcription.

    Two stages, selected by `quality` (see SEPARATION_PROFILES):

      fast   one htdemucs_6s pass on the raw mix.
      best   BS-Roformer vocal split, then htdemucs_6s on the instrumental.
      ultra  BS-Roformer vocal split, then BS-Roformer-SW on the instrumental —
             same six sources as htdemucs but cleaner guitar/piano, at the cost
             of speed and a bigger first-run download.

    Splitting vocals off first means the instrument stems carry no vocal bleed
    and the vocals themselves come from a model built for them (much better for
    pitch tracking). That's why the UI reports "separating stems" twice — two
    real neural-net passes, labelled 1/2 and 2/2 through `on_stage`.

    `instrument` selects which stem to transcribe; "all" builds one combined
    instrumental stem (so overlapping notes are quantified once, not per stem)."""
    audio_path = Path(audio_path)
    quality = quality or SEPARATION_QUALITY
    profile = SEPARATION_PROFILES.get(quality)
    if profile is None:
        logger.warning("Unknown separation quality %r — using 'best'.", quality)
        profile = SEPARATION_PROFILES["best"]
    split_model, split_tag = INSTRUMENT_SPLIT_MODELS[profile["instrument_model"]]

    def report(stage: str):
        if on_stage:
            on_stage(stage)

    if not audio_path.exists():
        raise FileNotFoundError(f"Input audio file not found: {audio_path}")

    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        raise ValueError(
            f"Unsupported audio format: '{audio_path.suffix}'. "
            f"Supported: {sorted(SUPPORTED_AUDIO_EXTENSIONS)}"
        )

    stem_output_dir = OUTPUT_DIR / audio_path.stem
    stem_output_dir.mkdir(parents=True, exist_ok=True)
    name = audio_path.stem

    logger.info("Starting separation for '%s' (quality=%s, split=%s)",
                audio_path.name, quality, profile["instrument_model"])
    logger.info("  Output: %s | Device: %s", stem_output_dir, DEVICE)

    start_time = time.time()
    output_files = []

    # ── Stage A: vocals / instrumental via BS-Roformer ───────────────────────
    split_input = audio_path
    if profile["vocal_split"]:
        try:
            report("Separating stems (1/2 — vocal split)")
            logger.info("Stage A: BS-Roformer vocal split...")
            before = set(stem_output_dir.glob("*.wav"))
            _separate_with_retry(VOCAL_SPLIT_MODEL, stem_output_dir, audio_path, {
                "Vocals":       f"{name}_(Vocals)_roformer",
                "Instrumental": f"{name}_(Instrumental)_roformer",
            })
            _canonicalize_stems(stem_output_dir, name, "roformer", False, before)
            inst = stem_output_dir / f"{name}_(Instrumental)_roformer.wav"
            if inst.exists():
                split_input = inst
            else:
                logger.warning("Roformer instrumental not found; falling back to single-stage.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stage A failed (%s); falling back to single-stage split.", exc)
            split_input = audio_path

    # ── Stage B: six-stem instrument split ───────────────────────────────────
    report("Separating stems (2/2 — instrument split)" if profile["vocal_split"]
           else "Separating stems")
    logger.info("Running %s separation on '%s'...", split_model, split_input.name)
    two_stage = split_input != audio_path
    custom = {
        "Guitar": f"{name}_(Guitar)_{split_tag}",
        "Bass":   f"{name}_(Bass)_{split_tag}",
        "Piano":  f"{name}_(Piano)_{split_tag}",
        "Other":  f"{name}_(Other)_{split_tag}",
        "Drums":  f"{name}_(Drums)_{split_tag}",
        # In two-stage mode the instrumental has no vocals left — name the
        # residue so vocal globs can't pick it up over the Roformer vocals.
        "Vocals": f"{name}_({'Residual' if two_stage else 'Vocals'})_{split_tag}",
    }
    before_b = set(stem_output_dir.glob("*.wav"))
    try:
        output_files = _separate_with_retry(split_model, stem_output_dir, split_input, custom)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Source separation failed on '{audio_path.name}' with {split_model}: {exc}. "
            f"Is the file a valid, decodable audio file?"
        ) from exc
    # Models that ignore custom_output_names (or use different stem labels)
    # still end up with the filenames the rest of the pipeline globs for.
    _canonicalize_stems(stem_output_dir, name, split_tag, two_stage, before_b)

    elapsed = time.time() - start_time
    logger.info("Separation completed in %.1f seconds", elapsed)

    # Discover stems from disk (the return value's shape varies by version and
    # can be empty even on success).
    stems = [p for p in sorted(stem_output_dir.glob("*.wav"))
             if not any(k in p.name for k in ("(Combined)", "(Instrumental)", "(Residual)"))]

    def _match_stem(keyword: str) -> Path | None:
        for p in stems:
            if keyword in p.name.lower():
                return p
        return None

    if instrument == "all":
        # One combined instrumental stem (guitar+bass+piano+other) so overlapping
        # notes across instruments are transcribed once, and chord detection sees
        # the full harmony. Vocals + drums are excluded (handled separately / no pitch).
        guitar_stem_path = _build_combined_stem(stem_output_dir, stems)
        if guitar_stem_path is None:
            raise RuntimeError(f"Could not build combined stem for '{audio_path.name}'.")
    else:
        guitar_stem_path = _match_stem(instrument)
        if guitar_stem_path is None and instrument == "guitar":
            guitar_stem_path = _match_stem("other")
            if guitar_stem_path is not None:
                logger.warning("No 'guitar' stem; falling back to 'other': %s", guitar_stem_path.name)
        if guitar_stem_path is None:
            raise RuntimeError(
                f"No '{instrument}' stem for '{audio_path.name}'. "
                f"Files written: {[p.name for p in stems]} "
                f"(separator returned {len(output_files or [])} items)."
            )

    size_mb = guitar_stem_path.stat().st_size / (1024 * 1024)
    logger.info("Selected stem (%s): %s (%.1f MB)", instrument, guitar_stem_path.name, size_mb)

    # A near-silent stem means the song doesn't contain this instrument —
    # transcribing bleed produces garbage notes and chords.
    warning = ""
    if instrument != "all":
        try:
            import numpy as np
            import soundfile as sf
            probe, _sr = sf.read(str(guitar_stem_path), frames=44100 * 60, dtype="float32")
            rms = float(np.sqrt(np.mean(np.square(probe))))
            if rms < 0.01:
                warning = (f"The '{instrument}' stem is nearly silent — this song may not "
                           f"contain a {instrument}. Reprocess with Instrument = 'All instruments'.")
                print(f"  ⚠ {warning}")
        except Exception:  # noqa: BLE001
            pass

    return SeparationResult(
        guitar_stem_path=guitar_stem_path,
        duration_seconds=elapsed,
        source_file=audio_path,
        warning=warning,
    )