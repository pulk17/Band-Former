import logging
import time
from dataclasses import dataclass
from pathlib import Path

from audio_separator.separator import Separator

from pipeline.config import (
    DEVICE,
    MODEL_CACHE_DIR,
    NORMALIZATION_THRESHOLD,
    OUTPUT_DIR,
    SEPARATION_MODEL,
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
    out = stem_output_dir / f"{stem_output_dir.name}_(Combined)_htdemucs_6s.wav"
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
                    quality: str | None = None) -> SeparationResult:
    """Separate a stem from a full mix and return it for transcription.

    quality="best" (default): two-stage — BS-Roformer first (SOTA vocals +
    clean instrumental), then htdemucs_6s splits the *instrumental* into
    guitar/bass/piano/drums/other. Vocals come from the Roformer (far cleaner
    for pitch tracking) and the guitar stem has no vocal bleed.

    quality="fast": single htdemucs_6s pass on the original mix (old behavior).

    `instrument` selects which stem to transcribe; "all" builds one combined
    instrumental stem (so overlapping notes are quantified once, not per stem)."""
    audio_path = Path(audio_path)
    quality = quality or SEPARATION_QUALITY

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

    logger.info("Starting separation for '%s' (quality=%s)", audio_path.name, quality)
    logger.info("  Output: %s | Device: %s", stem_output_dir, DEVICE)

    start_time = time.time()
    output_files = []

    # ── Stage A (best): vocals / instrumental via BS-Roformer ────────────────
    demucs_input = audio_path
    if quality == "best":
        try:
            sep_a = _get_separator(VOCAL_SPLIT_MODEL, stem_output_dir)
            logger.info("Stage A: BS-Roformer vocal split...")
            sep_a.separate(str(audio_path), custom_output_names={
                "Vocals":       f"{name}_(Vocals)_roformer",
                "Instrumental": f"{name}_(Instrumental)_roformer",
            })
            inst = stem_output_dir / f"{name}_(Instrumental)_roformer.wav"
            if inst.exists():
                demucs_input = inst
            else:
                logger.warning("Roformer instrumental not found; falling back to single-stage.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stage A failed (%s); falling back to single-stage htdemucs.", exc)
            demucs_input = audio_path

    # ── Stage B: htdemucs_6s six-stem split ──────────────────────────────────
    separator = _get_separator(f"{SEPARATION_MODEL}.yaml", stem_output_dir)
    logger.info("Running HTDemucs separation on '%s'...", demucs_input.name)
    two_stage = demucs_input != audio_path
    custom = {
        "Guitar": f"{name}_(Guitar)_htdemucs_6s",
        "Bass":   f"{name}_(Bass)_htdemucs_6s",
        "Piano":  f"{name}_(Piano)_htdemucs_6s",
        "Other":  f"{name}_(Other)_htdemucs_6s",
        "Drums":  f"{name}_(Drums)_htdemucs_6s",
        # In two-stage mode the instrumental has no vocals left — name the
        # residue so vocal globs can't pick it up over the Roformer vocals.
        "Vocals": f"{name}_(Residual)_htdemucs_6s" if two_stage
                  else f"{name}_(Vocals)_htdemucs_6s",
    }
    try:
        output_files = separator.separate(str(demucs_input), custom_output_names=custom)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Source separation failed on '{audio_path.name}': {exc}. "
            f"Is the file a valid, decodable audio file?"
        ) from exc

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
    if instrument != "all":
        try:
            import numpy as np
            import soundfile as sf
            probe, _sr = sf.read(str(guitar_stem_path), frames=44100 * 60, dtype="float32")
            rms = float(np.sqrt(np.mean(np.square(probe))))
            if rms < 0.01:
                print(f"  ⚠ The '{instrument}' stem is nearly silent (RMS {rms:.4f}) — "
                      f"this song may not contain a {instrument}. "
                      f"Reprocess with Instrument = 'All instruments' for usable notes.")
        except Exception:  # noqa: BLE001
            pass

    return SeparationResult(
        guitar_stem_path=guitar_stem_path,
        duration_seconds=elapsed,
        source_file=audio_path,
    )