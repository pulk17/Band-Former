import logging
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import DEVICE

try:
    from tuning import knob as _knob
except Exception:  # noqa: BLE001
    def _knob(_s, _k, d):
        return d

_CREPE_MODEL = _knob("vocals", "crepe_model", "full")
_PERIODICITY = _knob("vocals", "periodicity_threshold", 0.21)

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


# Cached ByteDance piano transcriptor (loaded on first piano job).
_piano_tr = None


def _extract_notes_piano(audio_path: Path) -> list[NoteEvent]:
    """ByteDance high-resolution piano transcription (Kong et al.) — onset F1
    ~96.8 on MAESTRO, far better than MT3 for piano. Optional dependency:
    pip install piano_transcription_inference (model auto-downloads)."""
    global _piano_tr
    import librosa
    from piano_transcription_inference import PianoTranscription, sample_rate

    y, _ = librosa.load(str(audio_path), sr=sample_rate, mono=True)
    if _piano_tr is None:
        _piano_tr = PianoTranscription(
            device="cuda" if DEVICE.type == "cuda" else "cpu")
    out_mid = str(Path(audio_path).with_suffix(".piano.mid"))
    res = _piano_tr.transcribe(y, out_mid)
    notes = [NoteEvent(start_time=float(e["onset_time"]),
                       end_time=float(e["offset_time"]),
                       pitch=int(e["midi_note"]),
                       velocity=float(e["velocity"]) / 128.0)
             for e in res["est_note_events"]]
    notes.sort(key=lambda n: n.start_time)
    logger.info("Piano model extracted %d notes.", len(notes))
    return notes


def extract_notes(audio_path: Path | str, instrument: str = "guitar") -> list[NoteEvent]:
    """
    Transcribe a stem WAV into NoteEvents. Piano stems route to the dedicated
    ByteDance piano model when installed (fallback: YourMT3); everything else
    uses YourMT3 (via the ``mt3-infer`` toolkit).

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

    if instrument == "piano":
        try:
            return _extract_notes_piano(audio_path)
        except Exception as exc:  # noqa: BLE001
            logger.info("Piano model unavailable (%s); using YourMT3. "
                        "For better piano: pip install piano_transcription_inference", exc)

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


def _segment_f0(times, midi, voiced,
                split_semi: float = None, hold_s: float = None,
                gap_s: float = None, min_dur: float = None) -> list[tuple]:
    split_semi = _knob("vocals", "split_semitones", 0.6) if split_semi is None else split_semi
    hold_s = _knob("vocals", "hold_seconds", 0.08) if hold_s is None else hold_s
    gap_s = _knob("vocals", "gap_seconds", 0.06) if gap_s is None else gap_s
    min_dur = _knob("vocals", "min_note_seconds", 0.05) if min_dur is None else min_dur
    """Hysteresis note segmentation for a continuous f0 track.

    Vibrato and slides stay ONE note: a new note only starts when the pitch
    stays more than `split_semi` semitones away from the running note mean for
    at least `hold_s` seconds (or after an unvoiced gap > `gap_s`). The old
    per-frame integer rounding splintered every vibrato into rapid fake notes.
    """
    notes: list[tuple] = []
    cur = None                     # {"t0", "sum", "n"}
    dev_t0, dev_sum, dev_n = None, 0.0, 0
    last_voiced_t = None

    def flush(end_t):
        nonlocal cur
        if cur is not None and end_t > cur["t0"]:
            notes.append((cur["t0"], end_t, int(round(cur["sum"] / cur["n"]))))
        cur = None

    for i in range(len(times)):
        t = float(times[i])
        if not voiced[i]:
            if cur is not None and last_voiced_t is not None and t - last_voiced_t > gap_s:
                flush(last_voiced_t)
                dev_t0 = None
            continue
        m = float(midi[i])
        last_voiced_t = t
        if cur is None:
            cur = {"t0": t, "sum": m, "n": 1}
            dev_t0 = None
            continue
        if abs(m - cur["sum"] / cur["n"]) <= split_semi:
            cur["sum"] += m
            cur["n"] += 1
            dev_t0 = None
        else:
            if dev_t0 is None:
                dev_t0, dev_sum, dev_n = t, m, 1
            else:
                dev_sum += m
                dev_n += 1
            if t - dev_t0 >= hold_s:               # sustained move → real new note
                flush(dev_t0)
                cur = {"t0": dev_t0, "sum": dev_sum, "n": dev_n}
                dev_t0 = None
    if cur is not None and last_voiced_t is not None:
        flush(last_voiced_t)
    return [(a, b, p) for (a, b, p) in notes if b - a >= min_dur]


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
        model=_CREPE_MODEL,
        batch_size=512,
        device=device,
        return_periodicity=True,
    )

    # torchcrepe's recommended cleanup: median-smooth the confidence, gate on
    # actual audio silence, then the 0.21 At-threshold (not a raw 0.5 cut).
    # The Silence gate internally touches torchaudio and can die with
    # "TorchCodec is required" on some installs — it's an enhancement, so a
    # failure must not kill the whole vocals stage.
    periodicity = torchcrepe.filter.median(periodicity, 5)
    try:
        periodicity = torchcrepe.threshold.Silence(-60.)(periodicity, audio, sr, hop_length)
    except Exception as exc:  # noqa: BLE001
        logger.info("Silence gate skipped (%s)", exc)
    pitch = torchcrepe.filter.median(pitch, 3)

    pitch = pitch.squeeze().cpu().numpy()
    periodicity = periodicity.squeeze().cpu().numpy()

    import numpy as np
    import librosa

    times = np.arange(len(pitch)) * hop_length / sr
    voiced = periodicity > _PERIODICITY
    midi = librosa.hz_to_midi(np.maximum(pitch, 1e-6))

    notes = [NoteEvent(start_time=a, end_time=b, pitch=p, velocity=1.0)
             for a, b, p in _segment_f0(times, midi, voiced & (pitch > 0))]

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

    if f0 is None:
        return []

    _ = voiced_probs  # probabilities unused; hysteresis segmentation decides
    times = librosa.times_like(f0, sr=sr, hop_length=hop_length)
    safe = np.where(np.isnan(f0), 1e-6, f0)
    midi = librosa.hz_to_midi(safe)
    voiced = np.asarray(voiced_flag) & ~np.isnan(f0)

    notes = [NoteEvent(start_time=a, end_time=b, pitch=p, velocity=1.0)
             for a, b, p in _segment_f0(times, midi, voiced)]

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
        audio, sr, hop, fmin=65.0, fmax=2093.0, model=_CREPE_MODEL,
        batch_size=512, device=device, return_periodicity=True,
    )
    periodicity = torchcrepe.filter.median(periodicity, 5)
    try:
        periodicity = torchcrepe.threshold.Silence(-60.)(periodicity, audio, sr, hop)
    except Exception as exc:  # noqa: BLE001
        logger.info("Silence gate skipped (%s)", exc)
    pitch = torchcrepe.filter.median(pitch, 3)
    pitch = pitch.squeeze().cpu().numpy()
    periodicity = periodicity.squeeze().cpu().numpy()
    times = np.arange(len(pitch)) * hop / sr
    out: list[list] = []
    for t, p, pr in zip(times, pitch, periodicity):
        voiced = pr > _PERIODICITY and p > 0
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
