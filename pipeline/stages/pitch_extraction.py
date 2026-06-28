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


def extract_vocals(audio_path: Path | str) -> list[NoteEvent]:
    """
    Transcribe a vocals stem WAV into a monophonic list of NoteEvents using librosa.pyin.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    import librosa
    import numpy as np

    logger.info("Loading vocals '%s' for pYIN...", audio_path.name)
    y, sr = librosa.load(str(audio_path), sr=None, mono=True)

    logger.info("Running pYIN pitch tracking on vocals...")
    f0, voiced_flag, voiced_probs = librosa.pyin(
        y, 
        fmin=librosa.note_to_hz('C2'), 
        fmax=librosa.note_to_hz('C7'),
        sr=sr
    )

    notes: list[NoteEvent] = []
    if f0 is None:
        return notes

    hop_length = 512
    times = librosa.times_like(f0, sr=sr, hop_length=hop_length)

    current_note = None
    
    for i, freq in enumerate(f0):
        if voiced_flag[i] and not np.isnan(freq):
            pitch = int(round(librosa.hz_to_midi(freq)))
            if current_note is None:
                current_note = {"start": times[i], "pitch": pitch, "end": times[i]}
            else:
                if current_note["pitch"] == pitch:
                    current_note["end"] = times[i]
                else:
                    if current_note["end"] > current_note["start"]:
                        notes.append(NoteEvent(
                            start_time=float(current_note["start"]),
                            end_time=float(current_note["end"]),
                            pitch=current_note["pitch"],
                            velocity=float(voiced_probs[i-1] if i>0 else 1.0)
                        ))
                    current_note = {"start": times[i], "pitch": pitch, "end": times[i]}
        else:
            if current_note is not None:
                if current_note["end"] > current_note["start"]:
                    notes.append(NoteEvent(
                        start_time=float(current_note["start"]),
                        end_time=float(current_note["end"]),
                        pitch=current_note["pitch"],
                        velocity=1.0
                    ))
                current_note = None
                
    if current_note is not None and current_note["end"] > current_note["start"]:
        notes.append(NoteEvent(
            start_time=float(current_note["start"]),
            end_time=float(current_note["end"]),
            pitch=current_note["pitch"],
            velocity=1.0
        ))

    logger.info("Extracted %d vocal notes.", len(notes))
    return notes


def extract_vocal_contour(audio_path: Path | str) -> list[list]:
    """
    Continuous fundamental-frequency contour of a vocals stem via pYIN.

    Returns a list of [time_sec, midi_float] points (midi is None on unvoiced
    frames). Unlike the quantized note list, this keeps the raw pitch curve —
    vibrato, slides and scoops — for drawing the "pitch line" over the notes.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    import librosa
    import numpy as np

    # 22.05 kHz is plenty for a vocal fundamental and ~halves pYIN cost.
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    hop_length = 512  # ~23 ms — fine enough to show vibrato
    f0, voiced_flag, _ = librosa.pyin(
        y,
        fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C7"),
        sr=sr,
        hop_length=hop_length,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=hop_length)

    contour: list[list] = []
    for t, f, vf in zip(times, f0, voiced_flag):
        if vf and f and not np.isnan(f):
            contour.append([round(float(t), 3), round(float(librosa.hz_to_midi(f)), 2)])
        else:
            contour.append([round(float(t), 3), None])
    return contour
