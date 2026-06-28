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


def extract_vocals(audio_path: Path | str, model_choice: str = "auto") -> list[NoteEvent]:
    """Transcribe a vocals stem WAV into a monophonic list of NoteEvents.
    
    Uses CREPE (GPU-accelerated neural pitch tracker) or pYIN.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if model_choice == "crepe":
        return _extract_vocals_crepe(audio_path)
    elif model_choice == "pyin":
        return _extract_vocals_pyin(audio_path)
    
    # "auto" fallback — any CREPE failure (missing dep, TorchCodec, CUDA) -> pYIN
    try:
        return _extract_vocals_crepe(audio_path)
    except Exception as exc:  # noqa: BLE001
        logger.info("CREPE unavailable (%s); falling back to pYIN.", exc)
        return _extract_vocals_pyin(audio_path)


def _extract_vocals_crepe(audio_path: Path) -> list[NoteEvent]:
    """GPU-accelerated vocal pitch tracking via CREPE."""
    import torch
    import torchcrepe
    import librosa

    logger.info("Loading vocals '%s' for CREPE...", audio_path.name)
    # librosa (not torchaudio.load, which now needs the TorchCodec backend).
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    audio = torch.from_numpy(y).unsqueeze(0)   # [1, samples]

    device = DEVICE if DEVICE.type == "cuda" else torch.device("cpu")
    logger.info("Running CREPE pitch tracking (device: %s)...", device)

    # Predict pitch with CREPE
    hop_length = 160  # 10ms at 16kHz
    pitch, periodicity = torchcrepe.predict(
        audio, sr, hop_length,
        fmin=65.0,   # C2
        fmax=2093.0, # C7
        model='tiny',
        batch_size=512,
        device=device,
        return_periodicity=True,
    )

    pitch = pitch.squeeze().cpu().numpy()
    periodicity = periodicity.squeeze().cpu().numpy()

    import numpy as np
    import librosa

    times = np.arange(len(pitch)) * hop_length / sr
    voiced = periodicity > 0.5

    notes: list[NoteEvent] = []
    current_note = None

    for i in range(len(pitch)):
        if voiced[i] and pitch[i] > 0:
            midi = int(round(librosa.hz_to_midi(float(pitch[i]))))
            if current_note is None:
                current_note = {"start": times[i], "pitch": midi, "end": times[i]}
            elif current_note["pitch"] == midi:
                current_note["end"] = times[i]
            else:
                if current_note["end"] > current_note["start"]:
                    notes.append(NoteEvent(
                        start_time=float(current_note["start"]),
                        end_time=float(current_note["end"]),
                        pitch=current_note["pitch"],
                        velocity=float(np.mean(periodicity[max(0,i-5):i]))
                    ))
                current_note = {"start": times[i], "pitch": midi, "end": times[i]}
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

    logger.info("CREPE extracted %d vocal notes.", len(notes))
    return notes


def _extract_vocals_pyin(audio_path: Path) -> list[NoteEvent]:
    """CPU fallback: pYIN at 16kHz for speed."""
    import librosa
    import numpy as np

    logger.info("Loading vocals '%s' at 16kHz for pYIN...", audio_path.name)
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)

    logger.info("Running pYIN pitch tracking on vocals...")
    hop_length = 256
    f0, voiced_flag, voiced_probs = librosa.pyin(
        y,
        fmin=librosa.note_to_hz('C2'),
        fmax=librosa.note_to_hz('C7'),
        sr=sr,
        hop_length=hop_length,
    )

    notes: list[NoteEvent] = []
    if f0 is None:
        return notes

    times = librosa.times_like(f0, sr=sr, hop_length=hop_length)
    current_note = None

    for i, freq in enumerate(f0):
        if voiced_flag[i] and not np.isnan(freq):
            pitch = int(round(librosa.hz_to_midi(freq)))
            if current_note is None:
                current_note = {"start": times[i], "pitch": pitch, "end": times[i]}
            elif current_note["pitch"] == pitch:
                current_note["end"] = times[i]
            else:
                if current_note["end"] > current_note["start"]:
                    notes.append(NoteEvent(
                        start_time=float(current_note["start"]),
                        end_time=float(current_note["end"]),
                        pitch=current_note["pitch"],
                        velocity=float(voiced_probs[i-1] if i > 0 else 1.0)
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

    logger.info("pYIN extracted %d vocal notes.", len(notes))
    return notes


def extract_vocal_contour(audio_path: Path | str, model_choice: str = "auto") -> list[list]:
    """Continuous f0 contour [time, midi_float|None] of a vocals stem.

    Keeps the raw pitch curve (vibrato, slides) for the "pitch line". Uses CREPE
    on the GPU by default (fast); pYIN is the CPU fallback — running pYIN here on
    a long song is what made the last stage look frozen.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if model_choice == "pyin":
        return _contour_pyin(audio_path)
    try:
        return _contour_crepe(audio_path)
    except Exception as exc:  # noqa: BLE001
        logger.info("CREPE contour unavailable (%s); falling back to pYIN.", exc)
        return _contour_pyin(audio_path)


def _contour_crepe(audio_path: Path) -> list[list]:
    import torch
    import torchcrepe
    import librosa
    import numpy as np

    logger.info("Vocal contour via CREPE...")
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    audio = torch.from_numpy(y).unsqueeze(0)
    device = DEVICE if DEVICE.type == "cuda" else torch.device("cpu")
    hop = 160  # 10 ms
    pitch, periodicity = torchcrepe.predict(
        audio, sr, hop, fmin=65.0, fmax=2093.0, model="tiny",
        batch_size=512, device=device, return_periodicity=True,
    )
    pitch = pitch.squeeze().cpu().numpy()
    periodicity = periodicity.squeeze().cpu().numpy()
    times = np.arange(len(pitch)) * hop / sr
    out: list[list] = []
    for t, p, pr in zip(times, pitch, periodicity):
        voiced = pr > 0.5 and p > 0
        out.append([round(float(t), 3), round(float(librosa.hz_to_midi(float(p))), 2) if voiced else None])
    return out


def _contour_pyin(audio_path: Path) -> list[list]:
    import librosa
    import numpy as np

    logger.info("Vocal contour via pYIN...")
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    hop_length = 256
    f0, voiced_flag, _ = librosa.pyin(
        y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
        sr=sr, hop_length=hop_length,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=hop_length)
    out: list[list] = []
    for t, f, vf in zip(times, f0, voiced_flag):
        if vf and f and not np.isnan(f):
            out.append([round(float(t), 3), round(float(librosa.hz_to_midi(f)), 2)])
        else:
            out.append([round(float(t), 3), None])
    return out
