# Plan: Video (falling-tiles) transcription + audio-transcription roadmap + self-iteration guide

Three parts. Part 1 is the new big feature (Synthesia-style video → perfect notes).
Part 2 is how to keep improving audio transcription. Part 3 is the knob guide so YOU can
iterate without me.

---

## Part 1 — Falling-tiles video → notes ("tiles mode")

### Why this wins
Audio transcription guesses. A Synthesia video **renders the MIDI directly on screen** —
tile position = pitch, tile length = duration, tile-bottom touching the keyboard = onset.
Extracting that is a computer-vision problem with a deterministic answer: near-100%
accuracy is achievable, which no audio model can promise. Videos referenced:
beginner https://youtu.be/OQeUvNHR0Ac · advanced https://youtu.be/Kkd7grCASvw ·
advanced-with-artifacts https://youtu.be/pNG-B8DjP7U.

### Prior art (start by reading these — don't reinvent)
- [svsdval/video2midi](https://github.com/svsdval/video2midi) — the mature one; OpenCV, GUI to set the sample line, handles key overlap. Best reference for the *key-highlight* method.
- [emilamaj/SynToMid](https://github.com/emilamaj/SynToMid) — frame-diff based tile extraction.
- [devbridie/synthesiavideo2midi](https://github.com/devbridie/synthesiavideo2midi) — clean structure of keyboard→pitch mapping.
- [Adelost/piano-video-2-midi](https://github.com/Adelost/piano-video-2-midi), [tu500/synthesia_to_midi](https://github.com/tu500/synthesia_to_midi), [41pha1/MIDI-Converter](https://github.com/41pha1/MIDI-Converter), [venividiviciuss/Video-To-Midi-Converter](https://github.com/venividiviciuss/Video-To-Midi-Converter) — the last has ghost-note debouncing + white/black key handling worth stealing.

None combines BOTH signals (tiles + key highlights) with a per-key HMM — that's our
accuracy edge.

### Pipeline design (new module `tiles/`, pure OpenCV + numpy, no ML needed for v1)

```
yt-dlp (video, prefer 60fps ≥720p)
  → 1. keyboard detection          → x-position → MIDI pitch map
  → 2. mode detection              → tiles? highlights? both?
  → 3A. tile tracking (lookahead)  → onsets + durations + hand (color)
  → 3B. key-highlight tracking     → ground-truth onsets at the keyboard line
  → 4. fusion + per-key HMM        → note events
  → 5. beat/tempo inference        → quantized MIDI
  → 6. into existing pipeline      → tab.json (piano mode) → player/learn UI
```

**1. Keyboard detection (once, on a median frame of N samples)**
- Find the keyboard band: horizontal row with maximal white/black alternation
  (row-wise variance + brightness profile). Usually bottom ~15–20% of frame.
- White-key boundaries: vertical dark seams in the white band (Sobel-x peaks at
  near-constant spacing). Black keys: dark blobs in the top half of the band.
- Anchor pitch: black keys cluster in 2-3-2-3 pattern; the gap left of a 2-group = C.
  Count white keys to the left edge → leftmost pitch (88-key starts at A0, but many
  videos crop — NEVER assume, derive from the pattern).
- Output: `key_map = [(x_left, x_right, midi_pitch, is_black), ...]` + keyboard top y.
- **Setting**: manual override (keyboard y, leftmost note) for weird crops.

**2. Mode detection**
- Sample 100 frames. If colored rectangles exist above the keyboard line → tiles mode.
  If key colors change at the keyboard itself (your "greys out" videos) → highlight mode.
  Both → fusion mode (best).

**3A. Tile tracking (max accuracy path)**
- Background subtraction: tiles are saturated color on dark bg → HSV threshold
  (S > s_min, V > v_min). Auto-learn tile hues from a histogram of moving pixels
  (typically 2 hues = left/right hand — keep hue → hand mapping).
- Per key column (from key_map): scan the column strip; connected runs of tile pixels =
  tiles. Track tile bottom edge y_b(t) across frames.
- **Scroll speed**: median of dy/frame across all tiles (constant in Synthesia).
  This gives sub-frame onset precision: onset = frame_time + (keyboard_y − y_b)/speed.
- Duration = tile pixel-height / speed. Both independent of frame rate jitter.
- Artifacts (video 3): particles/glow at the hit line → only trust tile geometry ABOVE
  the artifact zone (keyboard_y − artifact_margin) and extrapolate to the line by speed.
- Octave-duplicate glow, decorative flashes → rejected by minimum tile width (≥60% of
  key width) and minimum height.

**3B. Key-highlight tracking (the "greys out" videos)**
- Per key: mean color of a small patch on the key (white keys: lower third; black keys:
  center). Baseline = temporal median (unpressed color).
- Pressed = color distance from baseline > threshold for that key class.
  Track per-frame boolean → onsets/offsets.
- Handles videos with NO falling tiles at all.

**4. Fusion + per-key 2-state HMM**
- States on/off per key; observations: tile-present-at-line (3A) and key-highlighted (3B).
- Transition penalty = debounce (kills 1-frame flickers); min note length ~40 ms.
- Where both signals exist, tiles set the timing (sub-frame), highlight confirms.
- Output note list: {onset, offset, pitch, hand, velocity=const}.

**5. Tempo + quantization**
- Onset autocorrelation → tempo; or reuse `beat_this` on the video's audio track
  (also lets us cross-validate CV notes against audio onsets — flag disagreements).
- Write MIDI (mido) + notes.json in the pipeline's format.

**6. App integration**
- New source type: paste YouTube link + "This is a piano-tiles video" toggle (or
  auto-detect: run keyboard detection on 5 frames; if found → tiles mode).
- Skips separation/MT3 entirely → runs engine only for key/chord analysis of the
  note set (chords from the NOTES here, not chroma — they're exact) → arrange
  (piano: no fret solving; melody/accompaniment split by hand color when present,
  else by pitch register) → analyze → same UI. Player gets a **piano-roll view**
  (we already render falling notes for guitar strings — add an 88-key lane mode).
- Hand detection (MediaPipe Hands) = later phase, exactly as you said.

### Accuracy checklist (order of implementation)
1. keyboard map + highlight mode (simplest, works on all 3 video types) — v0.
2. tile tracking + scroll speed + sub-frame onsets — v1.
3. HMM fusion + artifact zone — v2.
4. audio cross-validation — v3.
5. MediaPipe hands — someday.

### Settings to expose in tuning.json ("tiles" section, when built)
`keyboard_y_override, leftmost_midi_override, sat_min, val_min, hue_left, hue_right,
artifact_margin_px, min_tile_width_frac, min_note_ms, highlight_delta, fps_process`

---

## Part 2 — Audio transcription: what's left + iss_tarah diagnosis

**iss_tarah fails because it has no guitar.** Vocals+synth song → htdemucs "guitar" stem
is bleed/noise → MT3 transcribes mush → chords over-segment (254 segments). Fixes, in order:
1. **Reprocess it with Instrument = "All instruments"** — now possible: the instrument
   selector applies on Reprocess and forces re-transcription (just added).
2. Separation "best" mode (Roformer) cleans the synth stems too.
3. Auto-detect coming (Phase 6): if guitar-stem RMS < 10% of combined → warn/auto-switch.

**Model upgrades worth doing (deterministic, high yield):**
- **Piano songs**: ByteDance `piano_transcription_inference` (Kong et al., "High-resolution
  Piano Transcription", onset F1 ~96.8 on MAESTRO) — far better than MT3 for piano.
  `pip install piano_transcription_inference`; route instrument=piano through it.
- **Vocals-as-melody**: your CREPE contour is already the best available signal; the
  hysteresis segmentation knobs (Part 3) control note quality.
- **Chords**: current engine is good on clear mixes (WIMM 4/4 roots). Remaining sus2-vs-maj
  bias on washy mixes → knobs `thirdless_penalty` / `gate_tau` (see Part 3).
- **Beats**: beat_this is SOTA; done.

---

## Part 3 — Self-iteration guide (tuning.json)

Edit `tuning.json` → click **Reprocess** on a song → compare. C++ engine reads the file
on every run (no rebuild); python stages read it per pipeline run (server restart NOT
needed for engine knobs; needed for vocals/arrange knobs since modules cache them).

| Symptom | Knob | Direction |
|---|---|---|
| Chords flicker / change too often | `chord.transition_penalty` (0.15) | raise (0.2–0.3) |
| Chords too sticky, misses real changes | same | lower (0.06–0.10) |
| Too many maj7/7/sus color chords | `chord.complexity_penalty` (0.15), `chord.gate_tau` (0.09) | raise both |
| Everything is a power chord (:5) | `chord.thirdless_penalty` (0.05) | raise (0.08–0.12) |
| Everything forced to maj/min, real sus songs mislabeled | same | lower |
| Wrong ROOTS (e.g. relative-major confusion) | `chord.bass_bonus` (0.8) | raise = trust bass more |
| Slash chords missing / too many | `chord.slash_bass_mass` (0.35) | lower / raise |
| Quiet intro labeled silence | `chord.silence_threshold` (0.02) | lower |
| Muddy chroma, thirds drowned (distorted mixes) | `chroma.q_mult` (1.8), `chroma.lateral_inhibition` (0.30) | raise q_mult to 2.2–2.5 (slower), inhibition to 0.35 |
| Real notes disappearing from chroma | `chroma.lateral_inhibition` | lower (0.15–0.25) |
| Key modulations missed | `chord.key_window_segs` (16) | lower (8) |
| Borrowed chords forced diatonic | `chord.key_penalty` (0.03) | lower |
| Vocal notes splintered (vibrato splits) | `vocals.split_semitones` (0.6), `vocals.hold_seconds` (0.08) | raise |
| Vocal slides merged into one note | same | lower |
| Vocals missing quiet phrases | `vocals.periodicity_threshold` (0.21) | lower (0.15) |
| Vocal ghost notes in silence | same | raise (0.3) |
| Too many short junk guitar notes | `arrange.ghost_dur` (0.07), `arrange.harmonic_ghost_max_dur` (0.09) | raise |
| Real fast notes being deleted | same | lower |
| Lead line includes chord tones | `arrange.lead_max_poly` (2) | lower to 1 |
| Melody-over-strum notes missing from lead | `arrange.skyline_gap_semitones` (5) | lower (3–4) |

Workflow per song without full reprocess (fast, engine-only):
```
./tab_engine/build/tab_engine.exe data/output/SONG/notes.json data/output/SONG/*Guitar*.wav data/output/SONG/beats.json data/output/SONG/*Combined*.wav
.venv/Scripts/python arrange.py data/output/SONG/tab.json
.venv/Scripts/python analyze.py data/output/SONG/tab.json
```
then reload the song in the browser. ~10 s per experiment.
