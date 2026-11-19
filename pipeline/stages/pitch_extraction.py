import logging
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import DEVICE

try:
    from tuning import knob as _knob
except Exception:  # noqa: BLE001
    def _knob(_s, _k, d):
        return d

def _crepe_model(): return _knob("vocals", "crepe_model", "full")
def _periodicity(): return _knob("vocals", "periodicity_threshold", 0.21)

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


def _track_crepe(audio_path: Path):
    """(times, pitch Hz, voiced) from one GPU CREPE pass over a vocals stem."""
    import numpy as np
    import torch
    import torchcrepe
    import librosa

    logger.info("Vocal pitch via CREPE ('%s')...", audio_path.name)
    # librosa (not torchaudio.load, which now needs the TorchCodec backend).
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    audio = torch.from_numpy(y).unsqueeze(0)   # [1, samples]
    device = DEVICE if DEVICE.type == "cuda" else torch.device("cpu")
    hop = 160  # 10 ms
    pitch, periodicity = torchcrepe.predict(
        audio, sr, hop, fmin=65.0, fmax=2093.0, model=_crepe_model(),
        batch_size=512, device=device, return_periodicity=True,
    )
    # torchcrepe's recommended cleanup: median-smooth the confidence, gate on
    # actual audio silence, then the 0.21 threshold (not a raw 0.5 cut). The
    # Silence gate internally touches torchaudio and can die with "TorchCodec
    # is required" on some installs — it's an enhancement, so a failure must
    # not kill the whole vocals stage.
    periodicity = torchcrepe.filter.median(periodicity, 5)
    try:
        periodicity = torchcrepe.threshold.Silence(-60.)(periodicity, audio, sr, hop)
    except Exception as exc:  # noqa: BLE001
        logger.info("Silence gate skipped (%s)", exc)
    pitch = torchcrepe.filter.median(pitch, 3).squeeze().cpu().numpy()
    periodicity = periodicity.squeeze().cpu().numpy()
    times = np.arange(len(pitch)) * hop / sr
    return times, pitch, (periodicity > _periodicity()) & (pitch > 0)


def _track_pyin(audio_path: Path):
    """CPU fallback: (times, pitch Hz, voiced) from pYIN at 16 kHz."""
    import numpy as np
    import librosa

    logger.info("Vocal pitch via pYIN ('%s')...", audio_path.name)
    y, sr = librosa.load(str(audio_path), sr=16000, mono=True)
    hop = 256
    f0, voiced_flag, _ = librosa.pyin(
        y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"), sr=sr, hop_length=hop)
    times = librosa.times_like(f0, sr=sr, hop_length=hop)
    voiced = np.asarray(voiced_flag) & ~np.isnan(f0)
    return times, np.where(np.isnan(f0), 0.0, f0), voiced


def extract_vocals_and_contour(audio_path: Path | str, model_choice: str = "auto"):
    """Vocal notes AND the continuous pitch line [[t, midi|None], ...] from a
    single pitch-tracking pass (it used to run CREPE twice per song).

    "crepe" / "pyin" force a tracker; "auto" tries CREPE on the GPU and falls
    back to pYIN on any failure (missing dep, TorchCodec, CUDA).
    """
    import numpy as np
    import librosa

    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if model_choice == "pyin":
        times, hz, voiced = _track_pyin(audio_path)
    elif model_choice == "crepe":
        times, hz, voiced = _track_crepe(audio_path)
    else:
        try:
            times, hz, voiced = _track_crepe(audio_path)
        except Exception as exc:  # noqa: BLE001
            logger.info("CREPE unavailable (%s); falling back to pYIN.", exc)
            times, hz, voiced = _track_pyin(audio_path)

    midi = librosa.hz_to_midi(np.maximum(hz, 1e-6))
    notes = [NoteEvent(start_time=a, end_time=b, pitch=p, velocity=1.0)
             for a, b, p in _segment_f0(times, midi, voiced)]
    contour = [[round(float(t), 3), round(float(m), 2) if v else None]
               for t, m, v in zip(times, midi, voiced)]
    logger.info("Vocals: %d notes, %d voiced frames.", len(notes), int(voiced.sum()))
    return notes, contour
