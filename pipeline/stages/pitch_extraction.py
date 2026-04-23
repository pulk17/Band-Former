import logging
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import DEVICE

logger = logging.getLogger(__name__)


@dataclass
class NoteEvent:
    start_time: float    # seconds — when the note begins
    end_time: float      # seconds — when the note ends
    pitch: int           # MIDI pitch number (0-127). Middle C = 60.
    velocity: float      # amplitude/loudness, 0.0 to 1.0


def extract_notes(audio_path: Path | str) -> list[NoteEvent]:
    """
    Transcribe a guitar stem WAV into a list of NoteEvents using Basic Pitch.

    Basic Pitch returns polyphonic MIDI note events — multiple notes can have
    the same or overlapping timestamps, which is correct for chords.

    Args:
        audio_path: Path to the guitar stem WAV (output of separation stage)

    Returns:
        List of NoteEvent, sorted by start_time ascending
    """
    audio_path = Path(audio_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH

    logger.info("Running Basic Pitch on '%s'...", audio_path.name)

    # predict() returns a 3-tuple:
    #   [0] model_output   — raw neural network output dict (we don't need this)
    #   [1] midi_data      — pretty_midi object (useful later for export)
    #   [2] note_events    — list of tuples: (start_sec, end_sec, pitch, amplitude)
    #
    # We pass the model path explicitly so Basic Pitch doesn't re-search
    # for available backends on every call.
    _model_output, _midi_data, raw_note_events = predict(
        audio_path=str(audio_path),
        model_or_model_path=ICASSP_2022_MODEL_PATH,
    )

    notes: list[NoteEvent] = []

    for event in raw_note_events:
        # Each event is a tuple: (start_time, end_time, pitch, amplitude, ...)
        # We take the first 4 fields — extra fields (pitch_bend etc.) are ignored.
        start_t = float(event[0])
        end_t   = float(event[1])
        pitch   = int(event[2])
        amp     = float(event[3])

        if end_t <= start_t:
            continue
        if not (0 <= pitch <= 127):
            continue

        notes.append(NoteEvent(
            start_time=start_t,
            end_time=end_t,
            pitch=pitch,
            velocity=amp,
        ))

    # Sort by start time — downstream stages (fingering solver) expect notes in chronological order.
    notes.sort(key=lambda n: n.start_time)

    logger.info("Extracted %d notes.", len(notes))
    return notes