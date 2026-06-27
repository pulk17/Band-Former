import logging
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import DEVICE

logger = logging.getLogger(__name__)

# YourMT3 (and the MT3 family) operate at 16 kHz mono.
_MT3_SAMPLE_RATE = 16_000
_MODEL = "yourmt3"


@dataclass
class NoteEvent:
    start_time: float    # seconds — when the note begins
    end_time: float      # seconds — when the note ends
    pitch: int           # MIDI pitch number (0-127). Middle C = 60.
    velocity: float      # amplitude/loudness, 0.0 to 1.0


def _midi_to_note_events(midi) -> list["NoteEvent"]:
    """Convert a mido.MidiFile into NoteEvents.

    Iterating a mido.MidiFile yields its tracks already merged in chronological
    order, with each message's ``.time`` expressed as a *delta in seconds*
    (mido applies the tempo map for us). We accumulate absolute time and pair
    each note_on with its matching note_off.
    """
    notes: list[NoteEvent] = []
    active: dict[tuple[int, int], tuple[float, int]] = {}
    abs_time = 0.0

    for msg in midi:
        abs_time += msg.time

        is_note_on = msg.type == "note_on" and msg.velocity > 0
        is_note_off = msg.type == "note_off" or (
            msg.type == "note_on" and msg.velocity == 0
        )

        if is_note_on:
            active[(msg.channel, msg.note)] = (abs_time, msg.velocity)
        elif is_note_off:
            key = (msg.channel, msg.note)
            if key in active:
                start_t, vel = active.pop(key)
                if abs_time > start_t and 0 <= msg.note <= 127:
                    notes.append(NoteEvent(
                        start_time=start_t,
                        end_time=abs_time,
                        pitch=msg.note,
                        velocity=vel / 127.0,
                    ))

    return notes


def extract_notes(audio_path: Path | str) -> list[NoteEvent]:
    """
    Transcribe a guitar stem WAV into a list of NoteEvents using YourMT3
    (via the ``mt3-infer`` toolkit).

    YourMT3 is a multi-instrument transcription model that is dramatically more
    accurate than Basic Pitch on real, full-band material. Because the input
    here is the *isolated* guitar stem, every transcribed note is treated as a
    guitar note.

    Args:
        audio_path: Path to the guitar stem WAV (output of separation stage)

    Returns:
        List of NoteEvent, sorted by start_time ascending
    """
    audio_path = Path(audio_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    import librosa
    from mt3_infer import transcribe

    logger.info("Loading '%s' at %d Hz mono...", audio_path.name, _MT3_SAMPLE_RATE)
    audio, _sr = librosa.load(str(audio_path), sr=_MT3_SAMPLE_RATE, mono=True)

    device = "cuda" if DEVICE.type == "cuda" else "cpu"
    logger.info("Running YourMT3 transcription (device: %s)...", device)

    midi = transcribe(
        audio,
        model=_MODEL,
        sr=_MT3_SAMPLE_RATE,
        device=device,
    )

    notes = _midi_to_note_events(midi)

    # Sort by start time — downstream stages (fingering solver) expect notes in
    # chronological order.
    notes.sort(key=lambda n: n.start_time)

    logger.info("Extracted %d notes.", len(notes))
    return notes
