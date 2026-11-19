# Band-Former improvement plan

Written 2026-11-16 from a full read of the code, measurements on every song in
`data/output/`, and a search for better models that fit an 8 GB RTX 5070 Laptop GPU.
Supersedes the task list in `HANDOFF.md` section 3. The golden rules in `HANDOFF.md`
section 1 still apply until Phase 2 deletes the C++ engine.

**The exact changes to make are in `FIXES.md`.** This file is the diagnosis and the
reasoning; where the two differ on order or detail, `FIXES.md` wins.

---

## 1. What is actually wrong (measured, not guessed)

### 1.1 Most of the library is stale output

| Song | tab.json written | Pipeline it reflects |
|---|---|---|
| where_is_my_mind, Bezubaan, darkhaast, iss_tarah, tiles_test, tiles_test_audio | 2026-07-11 | before the chord-quality fix and before `refine.py` (both 07-29) |
| Curtain_Blue | 2026-11-04 | current |

Six of seven songs still show the old engine's output: 37-56% power chords, 67 unique
chords in one song, notes 130 ms long. Nothing in `tab.json` records which pipeline
version produced it, so the app cannot tell you. Five songs also have zero vocals, and
three failed jobs (`aashiqana`, `Chaar_Diwaari...`, `Playing_God_Unplugged`) sit on disk
with stems but no `tab.json`, invisible in the UI.

### 1.2 Chords are over-complicated and wrong in a moving-target way

Current engine, re-run on scratch copies (nothing in `data/` was touched):

| Song | Unique chords | Changes / min | Median chord length | Specific errors |
|---|---|---|---|---|
| where_is_my_mind | 16 | 38 | 2.0 beats | key "G# minor" (song is in E major), C#:maj for 17 s (should be C#m), F:maj7 for 14 s, 30 slash chords |
| Bezubaan | 60 | 41 | 1.6 beats | D:min, D:maj and C#:maj7 all in one D-minor song |
| iss_tarah | 61 | 54 | 1.0 beat | no guitar in the song, garbage in, garbage out |
| darkhaast | 26 | 20 | 3.3 beats | D#:dim for 65 s (should be D# major, the V chord) |

A normal pop song has 4-8 chords changing every 2-4 beats. The classifier is a
hand-built template matcher on chroma with 13 knobs. The July fix stopped it dodging to
power chords, and on darkhaast it now dodges to `dim` instead. That is what tuning a
hand-built matcher looks like; a trained model does not have this failure mode.

### 1.3 The fingering solver produces tab that cannot be played

`tab_engine/src/fingering_solver.cpp` treats every note as one flat sequence and
minimises the single worst jump (minimax), not the total effort.

| Song | Simultaneous notes on the same string | Worst jump cost | String clashes `refine` then cut |
|---|---|---|---|
| where_is_my_mind | 155 | 38 | 371 |
| iss_tarah | 186 | 78 | 101 |
| Bezubaan | 130 | 68 | 124 |
| darkhaast | 48 | 37 | 35 |

- Nothing stops two notes of a chord landing on one string. `refine.resolve_strings`
  then cuts one of them to 50 ms, and `arrange.declutter` deletes anything under 70 ms.
  Real notes vanish because of a fingering bug.
- Once one bad jump exists, minimax does not care about any other jump, so 5-9% of
  consecutive notes leap more than 5 frets.
- Fingering runs before `refine` deletes 30-58% of the notes, so positions were chosen
  relative to notes that no longer exist. It also ignores the capo.

### 1.4 The transcriber invents notes, and "ultra" stems were never used

`refine` drops 17-42% of YourMT3's notes because the stem has no energy at their pitch
(where_is_my_mind 464 of 2258, iss_tarah 460 of 1108). YourMT3 is a multi-instrument
model being fed a separated stem. `BS-Roformer-SW.ckpt` is not in `pipeline/models/`,
so the "Ultra" separation profile has never actually run on this machine.

### 1.5 Key, capo, sections and strumming are not trustworthy

- Key comes from Krumhansl profiles on raw chroma: wrong on at least two of five songs.
  Roman numerals and the whole Learn view inherit the error.
- Capo: every song gets capo 4-7. The score is seconds-of-open-chords minus 0.5 per
  fret, so the fret penalty is noise. Curtain_Blue (A, G, Bm, D) is told capo 7.
- Sections: 14 segments of "verse 9 / bridge / chorus" on most songs.
- Strumming: the result is `D U D U D U D U` for every song.
- Bars: beat_this returns downbeats, the engine throws them away, and the UI assumes a
  bar is every 4th beat counted from the first beat.

### 1.6 Failures are silent

- `run_pipeline.py` rewrites `tab.json` about six times per run and catches most
  exceptions with a print. A failed arrange leaves a half-built song marked "done".
- The worker stores `str(exc)` and never logs the traceback.
- Jobs live in memory only: a restart loses queued and failed jobs.
- The YouTube download runs inside the HTTP request, so there is no progress and no row
  in the library until it finishes.
- Reloading the page during processing stops polling forever.
- Tiles songs have `beats: []` (beat tracking failed, nobody was told).
- All models stay loaded on an 8 GB card. On Windows that does not raise an OOM, it
  spills into shared memory and runs many times slower without any message.
- CREPE runs twice per song (notes, then contour). `refine` computes the same CQT twice
  (99 s on where_is_my_mind).
- Chord shape tables are duplicated in `arrange.py` and `app.js` and have drifted: the
  add9/6/sus2 open shapes in the uncommitted `arrange.py` diff never reach the UI.
- `.gitignore` ignores `test_*.py`, so no test can be committed.

### 1.7 The UI has no main path

Read from source; the server was not running and `HANDOFF.md` says not to start one.

- 7 views, 12 dock controls, 44 tuning knobs, 2 modals. Overview and Learn show mostly
  the same data. Tab, Vocals and Roll are separate views of the same timeline.
- A full-screen overlay blocks the app for the whole 3-6 minute job.
- Chords render as `C#:min7` and always with sharps (`A#` in F major).
- Warnings appear as one line of grey status text. Delete has no confirmation.
- "Both" and "All" in the dock are the same button.

---

## 2. Better tools that run on this laptop

| Job | Now | Candidate | Why | Risk |
|---|---|---|---|---|
| Chords | C++ template matcher | **lv-chordia** (`pip install lv-chordia`, MIT, PyTorch 2, weights bundled, 27 MB) | trained model, 170-chord vocabulary, same `C:maj` label grammar, about 80% on Billboard/RWC/Beatles | trained on Western pop; unverified on your Hindi songs, so benchmark first |
| Chords (alt) | | BTC (ChordMini 2026 paper: root 83, MIREX 80) | slightly stronger | no packaged weights confirmed |
| Guitar notes | YourMT3 | **hf-midi-transcription** guitar model (MIT, 95 MB) | guitar-specific, high-resolution onsets | README says best on clean solo audio; needs an A/B on separated stems |
| Stems | htdemucs_6s | BS-Roformer-SW (already wired as "Ultra") | better guitar/piano isolation | about 1 GB download, slower |
| Beats | beat_this | keep | already state of the art | |
| Sections | chord shingles | SongFormer (0.7 B params, 8 labels) | real intro/verse/chorus labels | Python 3.10, Ubuntu-tested only; do the cheap fix first |
| Fingering | C++ minimax | rewrite as plain DP in Python | deterministic, fixes 1.3 | ML options (Fretting-Transformer) have no released weights |
| Lyrics | none | Whisper large-v3-turbo on the vocal stem, or LRCLIB | chord sheet with lyrics | optional, Phase 5 |

Nothing here needs more than about 3 GB of VRAM at a time if models are freed between
stages.

---

## 3. The plan

### Phase 0: make it measurable (half a day)

1. Stamp `metadata.pipeline_version` (one integer constant in `run_pipeline.py`). The
   library shows an "outdated" badge and a "Reprocess outdated" button.
2. Reprocess the six stale songs (stems are on disk, about a minute each).
3. `eval/truth/<song>.chords.txt`: the real chords for 3-4 songs you know well, one line
   per change (`time chord`). This is the only step that needs you.
4. `tools/bench.py`: runs the pipeline tail on scratch copies for every truth song and
   prints one table: root accuracy, maj/min accuracy, unique chords, changes/min versus
   truth, same-string collisions, fret jumps over 5, percent of notes unverified.
5. Remove `test_*.py` from `.gitignore`. Commit the pending diff.

Done when: `python tools/bench.py` prints a baseline table and it is committed.

### Phase 1: chords a person can play (1-2 days) -- the biggest win

1. `pipeline/stages/chords.py`: lv-chordia on the song. Bench the full mix against the
   `(Combined)` stem and keep whichever scores higher.
2. `simplify_chords()` in `arrange.py`, a pure function:
   - boundaries snap to beats, minimum length 2 beats, changes prefer downbeats;
   - three detail levels: Simple (maj/min, 7 only if held a full bar), Standard (adds 7,
     maj7, min7, sus), Full;
   - a chord that fills under 2% of the song and appears once merges into its neighbour.
   `tab.json` stores the full-detail chords once; the level is applied on load, so
   switching it needs no reprocess.
3. Key from the chord sequence (duration-weighted match against the 24 diatonic sets),
   not from raw chroma.
4. One `displayName()` in `app.js`: `C#m7` not `C#:min7`, flats when the key has flats.
5. Capo: score is the fraction of time on open shapes minus 0.06 per fret, and a capo
   must beat no-capo by a clear margin. Show both options.
6. One shape table in `server/static/shapes.json`, read by both Python and JS.
7. If the bench confirms the win: delete `chroma_analyzer.cpp`, `chord_classifier.cpp`
   and the 15 chroma/chord knobs. If it does not, keep the engine and stop here.

Done when: on the truth songs, root accuracy beats the baseline, Simple mode shows at
most 8 chords for a 4-chord song, and changes/min is within 1.3x of truth.

### Phase 2: tab that is physically playable (1-2 days)

1. Reorder the pipeline: transcribe, verify and clean (`refine`), chords, lead/rhythm
   split, then fingering last.
2. `fingering.py` (about 120 lines) replaces the C++ solver:
   - notes are grouped into onset events; within an event every note gets its own string;
   - each event picks a hand position (4-fret window plus open strings);
   - DP over events minimises the sum of position shifts and stretches;
   - frets respect the capo;
   - a strum whose pitch classes match the current chord uses that chord's diagram shape.
3. `test_fingering.py`: asserts no shared strings, no fret below the capo, no stretch
   over 4 frets.
4. `refine`: compute the CQT once.
5. Transcriber A/B on the bench plus listening with "Hear: Transcription": YourMT3
   versus the hf-midi guitar model, each on htdemucs_6s and BS-Roformer-SW stems. Keep
   one combination. No model dropdown.
6. With chords and fingering gone, delete `tab_engine/` (about 1,900 lines of C++, CMake,
   vcpkg, the MSYS2 requirement) and golden rules 1, 4 and 16.

Done when: bench shows 0 same-string collisions, fret jumps over 5 under 2%, and the
transcription sounds closer to the song than today on at least 3 of 4 truth songs.

### Phase 3: reliability and error handling (1 day)

1. Build the tab in memory and write it once (`tab.json.tmp`, then `os.replace`). A
   failed required stage is a job error and the previous `tab.json` stays intact.
   Optional stages (beats, vocals) append to `metadata.warnings[]`.
2. `job.json` in each song folder: title, source URL, instrument, options, status,
   error, pipeline version. The library is rebuilt from disk, so failed and interrupted
   jobs appear with Retry and Delete, and titles survive restarts.
3. Worker logs the full traceback and maps known failures (CUDA out of memory, ffmpeg
   missing, YouTube bot check) to one plain sentence, with the raw detail expandable.
4. YouTube download becomes the first stage of the queued job.
5. Refuse reprocess, rename and delete while that song is being processed.
6. Free each model after its stage. Run CREPE once and derive notes and contour from it.
7. Pass downbeats through to `tab.json`; bars use them.
8. `/api/health`: GPU, ffmpeg, model files, free disk. The UI shows a banner if any fail.

Done when: killing the server mid-job, a bad URL, and a full disk each leave a visible,
retryable row and no broken song.

### Phase 4: UI with one main path (2-3 days)

The path is: add a song, keep using the app while it processes, play along.

1. **Add**: one box that takes a link or a dropped file. Instrument defaults to Auto
   (the loudest pitched stem). Everything else sits under "More options".
2. **Progress on the library row**: stage, elapsed time, Cancel. No overlay. Reload
   resumes it. Errors stay on the row with Retry.
3. **Three views instead of seven**:
   - Play: chord timeline, large NOW and NEXT diagrams, with tab, vocal line and piano
     roll as lanes you switch on;
   - Chords: a chord sheet by section and bar, plus the palette;
   - Learn: Overview and Learn merged.
4. **Dock from 12 controls to 5**: play, waveform, speed, loop, one Sound popover.
   Capo and chord detail move to the header beside the key. "Bars" and "All" go.
5. **Tuning**: two user-facing controls (chord detail, note density). The surviving
   knobs (about 8) move behind Advanced.
6. **Trust signals**: low-confidence chords drawn dim with a "?"; the silent-stem warning
   becomes a banner with a "Reprocess as all instruments" button.
7. Confirm before delete, inline rename, escape song titles before `innerHTML`.

Done when: a new user can add a song and play along without opening a modal.

### Phase 5: only if Phases 1-4 landed

- Sections from downbeats plus bar-level chord repetition; SongFormer only if that fails.
- Lyrics on the chord sheet (local Whisper, or LRCLIB which sends the song title out).
- Strumming from the onset envelope, or delete the card.
- PDF chord sheet export.

---

## 4. What not to do

- No more knob tuning on the C++ classifier.
- No new views, no model dropdowns.
- No SongFormer or ML fingering before the cheap versions are measured.
- No accuracy claim without a bench row.

## 5. Unknowns

- lv-chordia on Hindi film songs is untested. Phase 0 truth files exist to answer that.
- The hf-midi guitar model on separated, effected stems is untested.
- The UI audit is from source, not from watching the app run.
