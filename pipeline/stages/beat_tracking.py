import logging
from dataclasses import dataclass, field
from pathlib import Path

import torch

from pipeline.config import DEVICE

logger = logging.getLogger(__name__)


@dataclass
class BeatResult:
    beats: list[float] = field(default_factory=list)
    downbeats: list[float] = field(default_factory=list)
    bpm: float | None = None


# Cache the loaded model across calls in one process (warm web-server serving).
_predictor = None


def _get_predictor():
    global _predictor
    if _predictor is None:
        from beat_this.inference import File2Beats
        _predictor = File2Beats(
            checkpoint_path="final0",
            device=str(DEVICE),   # beat_this expects a device string ("cuda"/"cpu")
            dbn=False,
        )
    return _predictor


def extract_beats(audio_path: Path | str) -> BeatResult:
    """Detect beat and downbeat positions in an audio file."""
    audio_path = Path(audio_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    logger.info("Running beat tracking on '%s' (device: %s)...", audio_path.name, DEVICE)

    predictor = _get_predictor()
    beats_raw, downbeats_raw = predictor(str(audio_path))

    beats = beats_raw.tolist() if hasattr(beats_raw, "tolist") else list(beats_raw)
    downbeats = (
        downbeats_raw.tolist()
        if hasattr(downbeats_raw, "tolist")
        else list(downbeats_raw)
    )

    bpm = None
    if len(beats) >= 2:
        avg_interval = (beats[-1] - beats[0]) / (len(beats) - 1)
        bpm = round(60.0 / avg_interval, 1)

    logger.info("Extracted %d beats, %d downbeats.", len(beats), len(downbeats))
    if bpm is not None:
        logger.info("Estimated BPM: %.1f", bpm)

    return BeatResult(beats=beats, downbeats=downbeats, bpm=bpm)