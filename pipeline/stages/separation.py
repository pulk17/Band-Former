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
    SUPPORTED_AUDIO_EXTENSIONS,
)

logger = logging.getLogger(__name__)


@dataclass
class SeparationResult:
    guitar_stem_path: Path
    duration_seconds: float
    source_file: Path


# Cache the loaded model so repeated calls in one process (e.g. the web server)
# don't reload the HTDemucs weights every time. output_dir is re-pointed per call.
_separator = None


def _get_separator(stem_output_dir: Path):
    global _separator
    if _separator is None:
        _separator = Separator(
            output_dir=str(stem_output_dir),
            model_file_dir=str(MODEL_CACHE_DIR),
            log_level=logging.WARNING,
            output_format="WAV",
            normalization_threshold=NORMALIZATION_THRESHOLD,
        )
        logger.info("Loading model: %s", SEPARATION_MODEL)
        _separator.load_model(model_filename=f"{SEPARATION_MODEL}.yaml")
    _separator.output_dir = str(stem_output_dir)
    return _separator


def _build_combined_stem(stem_output_dir: Path, stems: list[Path]) -> Path | None:
    """Sum the pitched instrument stems (guitar+bass+piano+other) into one WAV."""
    import numpy as np
    import soundfile as sf
    parts = [p for p in stems if any(k in p.name.lower() for k in ("guitar", "bass", "piano", "other"))]
    if not parts:
        return None
    mix, sr = None, None
    for p in parts:
        data, s = sf.read(str(p), dtype="float32")
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


def separate_guitar(audio_path: str | Path, instrument: str = "guitar") -> SeparationResult:
    """Separate a stem from a full mix and return it for transcription.

    htdemucs_6s outputs all six stems (vocals/drums/bass/guitar/piano/other).
    `instrument` selects which to transcribe; "all" builds one combined
    instrumental stem (so overlapping notes are quantified once, not per stem)."""
    audio_path = Path(audio_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Input audio file not found: {audio_path}")

    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        raise ValueError(
            f"Unsupported audio format: '{audio_path.suffix}'. "
            f"Supported: {sorted(SUPPORTED_AUDIO_EXTENSIONS)}"
        )

    stem_output_dir = OUTPUT_DIR / audio_path.stem
    stem_output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Starting separation for '%s'", audio_path.name)
    logger.info("  Output: %s | Device: %s", stem_output_dir, DEVICE)

    start_time = time.time()

    separator = _get_separator(stem_output_dir)

    logger.info("Running HTDemucs separation...")
    try:
        output_files = separator.separate(str(audio_path))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Source separation failed on '{audio_path.name}': {exc}. "
            f"Is the file a valid, decodable audio file?"
        ) from exc

    elapsed = time.time() - start_time
    logger.info("Separation completed in %.1f seconds", elapsed)

    # Discover stems from disk (the return value's shape varies by version and
    # can be empty even on success).
    stems = [p for p in sorted(stem_output_dir.glob("*.wav")) if "(Combined)" not in p.name]

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

    return SeparationResult(
        guitar_stem_path=guitar_stem_path,
        duration_seconds=elapsed,
        source_file=audio_path,
    )