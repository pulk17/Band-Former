"""Audit a piano-tiles extraction against the video's own audio.

The video gives us the notes; the audio is an independent witness to the same
performance. Where they disagree, the extraction is wrong — no external ground
truth needed. Reports:

  onset lag      systematic timing bias, in ms. Tiles are read where they cross
                 a scan line ABOVE the keys, so notes come out early by
                 (scan-line offset / tile speed) unless that's corrected.
  onset jitter   spread around that bias — frame quantisation shows up here.
  pitch support  share of notes with real energy at their pitch while they
                 sound. Low means invented notes or a wrong octave.
  missed         audio onsets with no note near them (dropped notes).
  spurious       notes with no audio onset near them (artifacts read as notes).

Usage:
    python tools/tiles_audit.py data/output/SONG [audio.wav]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_notes(song: Path) -> list[dict]:
    data = json.loads((song / "notes.json").read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("notes", [])
    return sorted(data, key=lambda n: n["start_time"])


def find_audio(song: Path, override: str | None) -> Path:
    if override:
        return Path(override)
    wavs = [p for p in sorted(song.glob("*.wav")) if "(" not in p.name]
    if not wavs:
        wavs = sorted(song.glob("*.wav"))
    if not wavs:
        raise SystemExit(f"no audio in {song} — pass one explicitly")
    return wavs[0]


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    song = Path(sys.argv[1])
    notes = load_notes(song)
    audio_path = find_audio(song, sys.argv[2] if len(sys.argv) > 2 else None)
    if not notes:
        raise SystemExit("no notes to audit")

    import librosa
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    hop = 256                                     # 11.6 ms — finer than a video frame
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    times = librosa.frames_to_time(np.arange(len(onset_env)), sr=sr, hop_length=hop)

    print(f"song      {song.name}")
    print(f"audio     {audio_path.name}  ({len(y)/sr:.1f}s)")
    print(f"notes     {len(notes)}  span {notes[0]['start_time']:.2f}–"
          f"{max(n['end_time'] for n in notes):.2f}s")

    # ── Timing: where do the note onsets sit relative to audio onsets? ────────
    # Build an impulse train from the notes on the same grid as the onset
    # envelope, then slide it: the lag that maximises agreement is the bias.
    train = np.zeros_like(onset_env)
    for n in notes:
        i = int(round(n["start_time"] * sr / hop))
        if 0 <= i < len(train):
            train[i] += 1.0
    env = (onset_env - onset_env.mean()) / (onset_env.std() or 1)
    tr = (train - train.mean()) / (train.std() or 1)
    max_lag = int(round(0.40 * sr / hop))
    lags = np.arange(-max_lag, max_lag + 1)
    scores = np.array([np.dot(np.roll(tr, int(l)), env) for l in lags])
    best = int(lags[int(np.argmax(scores))])
    lag_ms = best * hop / sr * 1000
    peak, floor = scores.max(), np.median(scores)
    sharp = (peak - floor) / (scores.std() or 1)

    print(f"\nonset lag     {lag_ms:+7.1f} ms   (notes are "
          f"{'EARLY' if lag_ms > 0 else 'late'} by this much; "
          f"correlation peak {sharp:.1f}σ above baseline)")

    # ── Match notes to audio onsets, after removing the bias ─────────────────
    peaks = librosa.util.peak_pick(onset_env, pre_max=3, post_max=3, pre_avg=5,
                                   post_avg=5, delta=0.4, wait=2)
    onset_times = times[peaks]
    shifted = np.array([n["start_time"] - lag_ms / 1000 for n in notes])
    tol = 0.06

    matched_notes = 0
    residuals = []
    for s in shifted:
        if len(onset_times) == 0:
            break
        j = int(np.argmin(np.abs(onset_times - s)))
        d = onset_times[j] - s
        if abs(d) <= tol:
            matched_notes += 1
            residuals.append(d)
    matched_onsets = 0
    for o in onset_times:
        if len(shifted) and np.min(np.abs(shifted - o)) <= tol:
            matched_onsets += 1

    jitter = np.std(residuals) * 1000 if residuals else float("nan")
    print(f"onset jitter  {jitter:7.1f} ms   (spread once the bias is removed)")
    print(f"spurious      {1 - matched_notes/len(notes):7.1%}   "
          f"notes with no audio onset within ±{tol*1000:.0f} ms")
    if len(onset_times):
        print(f"missed        {1 - matched_onsets/len(onset_times):7.1%}   "
              f"audio onsets with no note (chords count once, so some of this is normal)")

    # ── Pitch: energy at the note's own pitch, and the octave question ───────
    # Read the caveat below before trusting these two numbers.
    C = np.abs(librosa.cqt(y, sr=sr, hop_length=hop, fmin=librosa.note_to_hz("C1"),
                           n_bins=84, bins_per_octave=12))
    C = C / (C.max() or 1)
    ctimes = librosa.frames_to_time(np.arange(C.shape[1]), sr=sr, hop_length=hop)
    med = np.median(C, axis=1)

    def pitch_scores(shift: int) -> tuple[float, float, int]:
        loud = peak = checked = 0
        for n in notes:
            b = int(n["pitch"]) + shift - 24      # CQT starts at C1 = MIDI 24
            if not (2 <= b < C.shape[0] - 2):
                continue
            s = n["start_time"] - lag_ms / 1000
            i0 = int(np.searchsorted(ctimes, s))
            i1 = min(i0 + 12, C.shape[1])
            if i1 <= i0:
                continue
            checked += 1
            col = C[:, i0:i1].max(axis=1)
            if col[b] > max(2.5 * med[b], 0.01):
                loud += 1
            if col[b] >= max(col[b - 2], col[b - 1], col[b + 1], col[b + 2]):
                peak += 1
        return (loud / checked if checked else 0.0,
                peak / checked if checked else 0.0, checked)

    loud0, peak0, checked = pitch_scores(0)
    print(f"pitch energy  {loud0:7.1%}   notes with energy above the norm at their "
          f"pitch ({checked} checked)")
    print(f"pitch peak    {peak0:7.1%}   notes whose pitch is a local spectral peak")
    print("\noctave check (higher is better, but see the caveat):")
    for shift in (-12, 0, 12):
        loud, peak, _ = pitch_scores(shift)
        mark = "  <- as extracted" if shift == 0 else ""
        print(f"   {shift:+3d} semitones   energy {loud:6.1%}   peak {peak:6.1%}{mark}")
    print("   CAVEAT: with pedal down and several notes sounding at once, a real note's\n"
          "   pitch often isn't a local peak, and ±12/±24 shifts land on harmonics that\n"
          "   have genuine energy. Treat a flat sweep as 'audio can't tell', not as proof.\n"
          "   Only a ground-truth MIDI settles pitch: tools/eval.py --midi.")

    # ── Shape sanity ────────────────────────────────────────────────────────
    durs = np.array([n["end_time"] - n["start_time"] for n in notes])
    print(f"\ndurations     median {np.median(durs)*1000:.0f} ms, "
          f"p10 {np.percentile(durs,10)*1000:.0f}, p90 {np.percentile(durs,90)*1000:.0f}")
    starts = np.array([n["start_time"] for n in notes])
    simultaneous = np.sum(np.diff(starts) < 0.03) + 1
    print(f"polyphony     {simultaneous} notes share an onset with a neighbour "
          f"({simultaneous/len(notes):.0%} — chords)")
    hands = {}
    for n in notes:
        hands[n.get("hand", "")] = hands.get(n.get("hand", ""), 0) + 1
    print(f"hands         {hands}")


if __name__ == "__main__":
    main()
