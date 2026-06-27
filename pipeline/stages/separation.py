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


def separate_guitar(audio_path: str | Path) -> SeparationResult:
    """Separate the guitar stem from a full-mix audio file."""
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
    output_files = separator.separate(str(audio_path))

    elapsed = time.time() - start_time
    logger.info("Separation completed in %.1f seconds", elapsed)

    # Prefer a stem explicitly named "guitar"; fall back to "other" (which is
    # where guitar usually lands in 4-stem models) so a model that names its
    # stems differently still produces usable output instead of crashing.
    def _match_stem(keyword: str) -> Path | None:
        for file_path in output_files:
            file_name = Path(file_path).name
            if keyword in file_name.lower():
                return stem_output_dir / file_name
        return None

    guitar_stem_path = _match_stem("guitar")
    if guitar_stem_path is None:
        guitar_stem_path = _match_stem("other")
        if guitar_stem_path is not None:
            logger.warning(
                "No 'guitar' stem found; falling back to 'other' stem: %s",
                guitar_stem_path.name,
            )

    if guitar_stem_path is None:
        available = [Path(f).name for f in output_files]
        raise RuntimeError(
            f"Guitar stem not found. Available stems: {available}"
        )

    size_mb = guitar_stem_path.stat().st_size / (1024 * 1024)
    logger.info("Guitar stem: %s (%.1f MB)", guitar_stem_path.name, size_mb)

    return SeparationResult(
        guitar_stem_path=guitar_stem_path,
        duration_seconds=elapsed,
        source_file=audio_path,
    )