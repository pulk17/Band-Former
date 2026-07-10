from pipeline.stages.separation import separate_guitar, SeparationResult
from pipeline.stages.beat_tracking import extract_beats, BeatResult
from pipeline.stages.pitch_extraction import (
    extract_notes, extract_vocals, extract_vocal_contour, NoteEvent,
)

__all__ = [
    "separate_guitar",
    "SeparationResult",
    "extract_beats",
    "BeatResult",
    "extract_notes",
    "extract_vocals",
    "extract_vocal_contour",
    "NoteEvent",
]
