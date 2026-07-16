# HANDOFF — complete Band-Former to "most accurate app" state

This document is the single source of truth for finishing this project. It assumes you
(the executing model) have the codebase and nothing else. Follow tasks in order. Every
task has: goal → files → exact steps → acceptance test → pitfalls. Read Section 1
(Golden Rules) before touching ANY file — each rule exists because violating it already
broke this app once.

Companion docs: `OVERHAUL.md` (audio-pipeline audit + what was already fixed),
`PIANO_TILES_PLAN.md` (tiles feature design + the tuning.json knob table).

---

## 0. What this app is

Band-Former: local web app (FastAPI + vanilla-JS canvas UI) that turns a song
(YouTube link / audio upload / Synthesia-style piano video) into a playable guitar
arrangement: synced note player, chord timeline with diagrams and capo logic, tab view,
vocal pitch line, piano-roll (tiles songs), and a music-theory Learn view generated from
the song's own data.

### Architecture (data flow)

```
AUDIO SONGS:
 input audio ─► separation (audio-separator: BS-Roformer → htdemucs_6s stems)
   ├► beats     (beat_this)                          → beats.json
   ├► notes     (YourMT3 via mt3-infer; piano stems → bytedance model) → notes.json
   ├► chords    (C++ tab_engine: tuning-compensated CQT chroma of the
   │            COMBINED instrumental, beat-synchronous Viterbi)        ┐
   ├► fingering (C++ minimax Viterbi)                                   ├→ tab.json
   ├► arrange.py (declutter, lead/rhythm, capo, voicings)               │
   ├► analyze.py (romans, cadences, difficulty, practice plan)          ┘
   └► vocals    (torchcrepe full + hysteresis) → merged into tab.json

TILES VIDEOS (Synthesia-style):
 video ─► tiles/extract.py (keyboard CV + scan-line note extraction)  → notes.json (EXACT)
 audio ─► beats + chord chroma (same engine) ; octave cross-check fixes absolute pitch
 → same arrange/analyze chain; raw video notes kept as tab.json["roll"] for the Roll view
```

### File inventory (roles)

| Path | Role |
|---|---|
| `server/app.py` | FastAPI: jobs dict, worker thread, all routes, MusicManager (yt-dlp) |
| `server/static/index.html/.js/.css` | Entire UI. No framework. Canvas rendering |
| `run_pipeline.py` | `process_audio()` and `process_tiles_video()` — the orchestrators |
| `pipeline/config.py` | dirs, DEVICE, model names, SEPARATION_QUALITY |
| `pipeline/stages/separation.py` | 2-stage separation, `ensure_chord_mix`, stem globs |
| `pipeline/stages/beat_tracking.py` | beat_this wrapper (median-interval BPM) |
| `pipeline/stages/pitch_extraction.py` | YourMT3 notes, bytedance piano, CREPE/pYIN vocals, `_segment_f0` hysteresis |
| `tab_engine/` (C++) | chroma_analyzer (CQT+tuning+lateral inhibition), chord_classifier (beat-sync Viterbi), note_eliminator, fingering_solver, main (reads `tuning.json` from CWD) |
| `arrange.py` | ghost filters, lead/rhythm split, capo, chord voicings; reads tuning.json |
| `analyze.py` | theory analysis → `tab.json["analysis"]`; CLI re-analyzes existing songs |
| `tiles/extract.py` | video → notes: keyboard detection, tile/highlight scan, octave validation |
| `tuning.py` / `tuning.json` | user-editable knob system (see PIANO_TILES_PLAN Part 3) |
| `render_tab.py` | ASCII tab (low priority) |
| `start.bat` | user's launcher (server + browser) |

### Data formats (exact — do not change without updating every consumer)

`notes.json` (pipeline interchange): `[{"start_time": s, "end_time": s, "pitch": midi_int, "velocity": 0..1}]`

`beats.json`: `{"beats": [s...], "downbeats": [s...], "bpm": float}`

`tab.json` (the app's whole world; written by C++ engine, ENRICHED by python):
```
metadata: {key:"D minor", bpm, capo, instrument, duration_sec, tuning[6], num_melody, tiles_mode?}
notes:  [{start, start_q, beat, duration, string(1=high e..6), fret, pitch, name, voice:"lead"|"rhythm", melody:bool}]
chords: [{start, end, name:"G:maj" or "G:maj/B", confidence, voicing?, capoVoicing?}]
melody: [...same-as-notes subset...]
beats:  [s...]
vocals: [{start, end|/duration, pitch, name}]          ← added by vocals stage
vocal_pitch: [[t, midi_float|null]...]                 ← contour
analysis: {romans{}, functions{}, progression, cadences[], borrowed[], transitions[], solo_scales[], difficulty{}, practice[]}
roll:   [{start, duration, pitch, hand:"left"|"right"|""}]   ← tiles songs only
```
Chord name grammar: `ROOT:QUAL[/BASS]`, ROOT/BASS ∈ C..B with `#`, QUAL ∈
maj min 5 7 maj7 min7 sus2 sus4 dim aug 6 m7b5 add9. UI/arrange strip `/BASS`
before shape lookup (`jsVoicing`, `_parse`).

Stem file naming (glob contracts — NEVER rename patterns without fixing globs in
`run_pipeline.py`, `separation.py`):
`{stem}_(Guitar|Bass|Piano|Other|Drums|Vocals)_htdemucs_6s.wav`,
`{stem}_(Vocals|Instrumental)_roformer.wav`, `{stem}_(Combined)_htdemucs_6s.wav`
(mono sum of guitar+bass+piano+other = chord-analysis mix), `(Residual)` = htdemucs'
vocal output when input was already de-vocaled (must NOT match vocal globs).

### Build & run

```bash
# C++ engine (MSYS2 ucrt64 g++ + ninja; vcpkg supplies libsndfile + nlohmann-json)
cmake --build tab_engine/build            # rebuild after ANY .cpp/.hpp change

# server — MUST run from repo root (tuning.json + relative dirs) with the venv python:
.venv/Scripts/python -m uvicorn server.app:app --port 8000     # or start.bat

# verification you run after EVERY change (non-negotiable):
node --check server/static/app.js
.venv/Scripts/python -m py_compile run_pipeline.py arrange.py analyze.py server/app.py \
    pipeline/stages/*.py tiles/extract.py
```

Fast per-song iteration WITHOUT reprocessing (engine+arrange+analyze on existing stems):
```bash
d=data/output/SONG
./tab_engine/build/tab_engine.exe "$d/notes.json" "$d"/*Guitar*.wav "$d/beats.json" "$d"/*Combined*.wav
.venv/Scripts/python arrange.py "$d/tab.json" && .venv/Scripts/python analyze.py "$d/tab.json"
```

---

## 1. GOLDEN RULES — every one of these already caused a real bug

1. **The C++ engine rewrites tab.json FROM SCRATCH.** Any key python added (vocals,
   vocal_pitch, analysis, roll) is DESTROYED by an engine run. `run_pipeline.py` snapshots
   and restores vocals; if you add new tab.json keys, extend that snapshot (`prev_vocals`
   logic) or re-add them after the engine. If you run the engine manually for testing,
   know that you just wiped those keys — reprocess restores them.
2. **audio-separator bakes `output_dir` into the model at `load_model()`.** The cache in
   `_get_separator` re-points BOTH `sep.output_dir` and `sep.model_instance.output_dir`.
   Never remove that; never cache a Separator another way. Symptom of regression: stems
   from song B appear inside song A's folder.
3. **Windows console is cp1252.** Any subprocess/CLI that prints ✓ → ⚠ needs
   `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the top (see
   run_pipeline.py/analyze.py header). Forgetting this KILLS the whole job with
   `UnicodeEncodeError: 'charmap' codec...`. Also: never put unicode in your own
   test-script prints.
4. **tuning.json is read from CWD by the C++ engine.** Server and manual engine runs must
   execute from repo root. If knobs "don't do anything", CWD is wrong.
5. **The user runs their own server on port 8000.** NEVER start a dev/preview server.
   Verify with `node --check`, `py_compile`, and direct CLI runs of pipeline pieces.
6. **Version pins that must not move**: `transformers==4.40.2` (YourMT3's T5 needs the
   legacy tuple-cache API; 4.44+ breaks, 5.x fatal), torch cu128 build for RTX 50-series
   (sm_120), Python 3.12. `torchaudio.load` is BANNED (needs TorchCodec) — always load
   audio via `librosa.load` or `soundfile`.
7. **`_processed_instrument()` reads `metadata.instrument` from tab.json** to decide
   cache hits and reprocess stems. Every pipeline path must write it (tiles writes
   `"tiles"`). If it's missing, reprocess silently assumes "guitar".
8. **Filenames**: all job ids/stems go through `_safe_stem` (regex `[^\w\- ]+` removed,
   spaces→underscores). audio-separator sanitizes DIFFERENTLY (collapses `__`→`_`), so
   never reconstruct stem filenames from the song stem — always glob by keyword
   (`*[Gg]uitar*.wav` etc.). Prefer-roformer ordering for vocals: sort key
   `0 if "roformer" in name else 1`.
9. **Don't trust `separator.separate()`'s return value** — its shape varies by version
   and can be empty on success. Discover outputs by globbing the output dir.
10. **Beat-sync chords mean boundaries are beats.** Don't add client-side chord smoothing
    back (the old "Smooth" button was removed for cause: three conflicting smoothing
    layers). Chord flicker fixes belong in `tuning.json` (`transition_penalty`).
11. **jsVoicing/`_parse` must strip `/bass` and unknown qualities return null voicing** —
    a chord card with no diagram is CORRECT behavior for e.g. `sus2` (no open shape
    table entry), not a bug to "fix" by inventing shapes.
12. **The frontend has NO framework and NO build step.** Keep it that way. Every element
    id referenced as `$("id")` must exist in index.html — after editing, grep-check:
    ids created dynamically inside template strings (`ovPractice`, `ovPlay`) are the only
    allowed exceptions.
13. **Reprocess ≠ full run**: it skips separation (stems on disk), reuses beats/notes
    unless `retranscribe` (set automatically when the instrument changed). If you add a
    stage, decide its reprocess behavior explicitly.
14. **GPU access**: pipeline needs CUDA; a sandboxed/CI shell may report
    `torch.cuda.is_available() == False` even on a GPU machine — that's the sandbox, not
    the app. Never "fix" DEVICE detection because of a sandbox result. CPU-safe pieces
    for local verification: tiles/extract.py, analyze.py, arrange.py, the C++ engine.
15. **Don't tune accuracy knobs on ONE song** (this happened; cost a day). Where Is My
    Mind = ground truth (verse must decode roots G#→A→E→C#m at ~1.5 s intervals).
    Bezubaan = drone-heavy stress test (D-rooted, −30 cents). Change one knob at a time;
    re-run BOTH songs with the fast iteration loop before accepting.
16. **The 5th harmonic of any root lands on its MAJOR THIRD's pitch class**, and CQT
    kernels leak into neighbor semitones. Both are already mitigated (lateral inhibition
    q_mult 1.8 / 0.30). If you touch chroma code, re-verify the WIMM A-major segment:
    A mass must exceed A#/G# leak bins, C# must be visible (~0.04+).
17. **Everything is currently UNCOMMITTED on top of `d58100e`.** First action of any
    session: `git add -A && git commit` the working state (message:
    "feat: chord engine v2, separation v2, tiles mode, UI shell, learn engine, tuning knobs").
    Never `git checkout --`/reset without the user asking.
18. **`data/` is precious** (hours of GPU output). Never delete/rename song folders except
    through the delete endpoint. `.gitignore` covers venv; check it covers `data/` before
    committing (add `data/` and `pipeline/models/` if absent).

---

## 2. Current state

**Working & verified**: chord engine v2 (WIMM verse roots 4/4, boundaries beat-locked,
tuning −30 cents detected = librosa's −31), UI shell + Overview/Learn/analysis, tiles
extraction on the beginner video (382 notes, keyboard C-anchor visually verified,
`tiles+highlight` fusion), octave validator (correctly conservative), tuning.json knobs
(engine reads per run — verified identical output on defaults), vocals hysteresis +
CREPE full, capo refret, rename-with-folder, reprocess instrument switch.

**Implemented but NOT verified end-to-end** (highest priority below):
separation v2 on GPU; `process_tiles_video()` as one function; the three tiles entry
points (worker branch, YouTube route with `tiles:true`, video file upload); piano model
routing; sidebar rename/delete/reprocess buttons in the browser.

**Known gaps**: tiles songs cannot be REPROCESSED (falls into the audio path — T3);
advanced/artifact tiles videos untested (T4); vocals were wiped on the 4 re-run songs
(T5); no sections/strumming detection (T8/T9); guided practice is a checklist, not an
interactive drill loop (T7).

---

## 3. TASKS — in this order

### T0. Commit the working state
As Golden Rule 17. Also add to `.gitignore` if missing: `data/`, `pipeline/models/`,
`*.pyc`. Acceptance: `git status` clean except intended files.

### T1. Verify the three tiles entry points end-to-end
**Goal**: user ticks "Piano-tiles video" → working song in the library with Roll view.
**Files**: server/app.py, run_pipeline.py, tiles/extract.py, server/static/app.js.
**Steps**:
1. Restart server (venv python, repo root). Hard-refresh browser.
2. Paste `https://youtu.be/OQeUvNHR0Ac`, tick the checkbox, Add. Watch server log:
   expect `[tiles] keyboard: ... pattern match`, `[tiles] octave check`, beats, engine,
   `Tiles arrangement:`.
3. When done: song loads; **Roll** tab visible; falling notes colored by hand; Player
   shows guitar refretting; Chords/Learn populated.
4. Upload path: download that video manually, upload the .mp4 with checkbox ticked.
5. Fix whatever breaks. Likely breakpoints & fixes:
   - `options["video_path"]` empty when tiles flag lost → check FormData/bool parsing
     (FastAPI Form(False) parses "false" string as True! JS sends `"true"/"false"` strings
     — VERIFY: `fd.append("tiles", $("tilesChk").checked)` arrives as string. If bool
     parsing is wrong, accept `tiles: str = Form("false")` and compare `== "true"`.) The
     JSON route (`YouTubeRequest.tiles: bool`) is safe — only the multipart upload route
     has this trap.
   - webm/m4a audio: `process_tiles_video` ffmpeg-converts to wav for the engine —
     requires ffmpeg on PATH (it is, gyan build).
   - beat_this on the video's audio: wrap already try/except — job must SURVIVE beat
     failure (falls back to 0.5 s chord grid).
**Acceptance**: both entry points produce a library song; `tab.json` has `roll` with
>300 entries and `metadata.instrument == "tiles"`.
**Avoid**: do not run separation for tiles songs; do not let the vocals stage run (no
stems — it prints the no-stem warning and restores nothing; that's fine).

### T2. Verify separation v2 (GPU required)
**Steps**:
1. Settings → Separation quality "Best". Add any NEW song.
2. First run downloads `model_bs_roformer_ep_317_sdr_12.9755.ckpt` (~600 MB) into
   `pipeline/models/`. Watch for `Stage A: BS-Roformer vocal split...`.
3. Verify output dir contains `(Vocals)_roformer`, `(Instrumental)_roformer`, six
   `_htdemucs_6s` stems with `(Residual)` instead of `(Vocals)`, plus `(Combined)`.
4. **Trap**: `custom_output_names` keys must match the model's internal stem names
   exactly ("Vocals"/"Instrumental" for this roformer — if files come out named by the
   default scheme instead, print `separator.model_instance.output_single_stem`/model
   data to find real stem labels and fix the dict keys in `separation.py`).
5. Listen: roformer vocals should be clearly cleaner than the old htdemucs vocals.
6. VRAM: roformer + htdemucs + MT3 + CREPE warm ≈ 8–10 GB. If OOM on the user's card,
   unload stage-A separator after use: `del _separators[VOCAL_SPLIT_MODEL];
   torch.cuda.empty_cache()` (reloads in ~5 s next song) — implement only if OOM actually
   happens.
**Acceptance**: two-stage stems on disk; vocals stage picks the roformer file (log line
shows its name); no crash on second song (cache reuse).

### T3. Reprocess support for tiles songs
**Goal**: ⟳ on a tiles song re-runs `process_tiles_video` (e.g. after tuning.json tile
knob changes), not the audio pipeline.
**Steps**: in `reprocess_job` (server/app.py): if `_processed_instrument(stem) == "tiles"`,
locate the video: `INPUT_DIR.parent / "video"` glob `{stem}.*` (mp4/webm/mkv). Queue with
`{"tiles": True, "video_path": ..., "reprocess": True}`. In `process_tiles_video`, honor
`options.get("reprocess")` by skipping beat re-extraction if beats.json exists.
**Acceptance**: ⟳ on the T1 song completes and updates tab.json.
**Avoid**: don't route tiles reprocess through `retranscribe`/stem logic — it's a
separate branch entirely.

### T4. Validate tiles on the advanced + artifact videos
`https://youtu.be/Kkd7grCASvw` (dense), `https://youtu.be/pNG-B8DjP7U` (particle
artifacts at the hit line).
**Steps**: run standalone first: `python -m tiles.extract <video.mp4> data/output/T`
then eyeball a debug frame (see the debug-frame script pattern in git history / write
one: draw `kb["keys"]` rectangles + C labels onto frame 3500, save png, view).
Checks: (a) keyboard band tight around actual keys; (b) note count plausible for the
piece; (c) durations median 0.1–1.5 s; (d) two hands detected (`hands > 0` in
tiles_meta.json). For the artifact video: if junk notes appear, raise
`tiles.artifact_margin_px` (14 → 24) and/or `tiles.min_note_ms` (60 → 80) in tuning.json
— do NOT change code first. Code changes only if: keyboard detection itself fails
(black-blob thresholds `<90` gray / width window in `detect_keyboard`) or tile colors
are unsaturated pastels (lower `tiles.sat_min`).
**Acceptance**: all three videos produce musically-plausible rolls (spot-check against
watching the video for 20 s).
**Caveat**: 4K/1440p videos → keyboard detection is resolution-independent but slow;
download capped at 720p already. Videos with a piano-cam (real hands, real piano, no
graphics) are OUT OF SCOPE — detect failure gracefully (keyboard pattern match < 60% →
raise clear error advising audio mode).

### T5. Regenerate wiped vocals
The 4 audio songs (where_is_my_mind, Bezubaan…, darkhaast, iss_tarah) lost
vocals/vocal_pitch to engine re-runs (pre-fix). One ⟳ Reprocess per song (vocals on)
regenerates them (separation skipped, ~1 min each). Also reprocess iss_tarah with
Instrument = "All instruments" (it has no guitar — that's why its notes/chords were
garbage). Acceptance: Vocals tab shows notes + white contour line for all four.

### T6. Roll view polish (small)
Add a fixed keyboard strip at the left edge of the Roll canvas (white/black key rows per
pitch, highlight active pitches at the now-line) and a small legend (blue=left hand,
green=right). Pure `drawRoll` change in app.js. Don't add libraries.

### T7. Guided practice v2 (the real learning loop)
**Goal**: plan steps become interactive drills, not text.
**Spec**:
- Each `analysis.practice` step gets a "Start" button (in Learn view).
- "shapes": opens Chords view filtered to the top chords (add a `?filter` mode to
  buildChordGrid).
- "changes": finds the longest segment where `from`/`to` chords alternate (scan
  tab.chords), sets `loopA/loopB` around 2 bars of it, sets speed 0.5, switches to
  Player, starts playback. Store drill state; on user click "faster" bump 0.5→0.75→1.0.
- "riff": melody phrases = split tab.melody at gaps > 1.5 beats; loop phrase i with the
  same speed-ramp; "next phrase" button.
- "full": speed 0.75, seek 0, play.
- Persist per-song progress (already `bf_done_` in localStorage) + last drill speed.
**Files**: app.js only (all data already in tab.json). ~150 lines.
**Acceptance**: clicking through a whole plan on WIMM works without touching the
transport manually.
**Avoid**: no server changes; no new tab.json fields (compute phrase boundaries client-side).

### T8. Section detection (enables section strip in the transport)
**Algorithm** (deterministic, python, in analyze.py):
1. Build a per-beat chord-root sequence from tab.chords + beats.
2. 8-beat shingles; label each beat with hash of its shingle.
3. Boundaries where the label stream changes and persists ≥ 8 beats → sections.
4. Name by repetition count: most-repeated label group = "chorus", first unique = "intro",
   others "verse N"/"bridge". This is heuristic — mark `confidence`.
5. Write `analysis.sections = [{name, start, end}]`.
**UI**: colored translucent spans on the waveform canvas (drawWave), click = seek;
Overview shows the section map bar.
**Acceptance**: WIMM shows intro/verse/chorus-ish blocks aligned with audible structure.
**Avoid**: don't use librosa self-similarity on audio (slow, new failure surface) —
chord-shingle version is 30 lines and works because chords are already beat-synced.

### T9. Strumming pattern (Learn view card)
Per section (T8): take rhythm-voice notes, histogram onsets modulo the beat at
subdivision 4 (sixteenths). Cells > 40% of max = strum hits; direction heuristic:
beat-aligned = Down, off-beat = Up. Emit e.g. "D · D U · U D U" with confidence =
histogram peakiness. Write `analysis.strumming`. Show in Learn + step 4 of practice.
**Avoid**: don't attempt audio onset analysis; MT3's rhythm-voice onsets are enough.

### T10. No-instrument auto-suggest (tiny)
`separate_guitar` already prints the near-silent-stem warning. Surface it to the user:
return the warning in `SeparationResult`, thread it into job status (new `warning` field
on Job dataclass, shown as a toast/status line + suggested action button "Reprocess as
All instruments"). Acceptance: iss_tarah-like songs prompt the fix instead of silently
producing garbage.

### T11. Housekeeping
- `requirements.txt`: add `opencv-python-headless`, note optional
  `piano_transcription_inference` (piano accuracy) — with comment they're needed for
  tiles / piano modes.
- README: 5-line quickstart (venv, requirements, torch cu128 note, build engine,
  start.bat), link OVERHAUL/PIANO_TILES_PLAN/HANDOFF.
- Commit per task (`feat:`/`fix:` prefixes), never amend, never push unless asked.

### T12. Later / explicitly deferred
MediaPipe hand tracking (which fingers); PDF/GuitarPro export; ear-training mode;
lead-vocal karaoke separation model; per-song tuning.json overrides; Electron packaging.

---

## 4. Accuracy iteration protocol (for any knob/algorithm change)

1. Reference set: WIMM (clear rock, truth = E–C#m–G#–A verse cycle), Bezubaan (drone,
   −30 cents), darkhaast (G# major-ish acoustic), one tiles video (exact ground truth!).
   Tiles songs are your only PERFECT ground truth — use them to measure the audio
   pipeline: process the same song via tiles AND via audio ("piano" instrument), then
   diff notes (onset ±80 ms, pitch exact) → recall/precision numbers. `tools/eval.py`
   does exactly this — run it before and after every knob change:
   `python tools/eval.py data/output/TILES_SONG data/output/AUDIO_SONG --chords`
   (it reads notes.json / tab.json["roll"], never the arranged notes; searches a global
   time offset; `--ignore-octave` isolates octave errors from real misses).
2. One knob per experiment; fast loop (Section 0 commands), ~10 s per song.
3. A change ships only if it improves/holds BOTH reference songs.
4. Chord truth sources: any published chord chart; compare at beat resolution, roots
   first (quality second, sus/maj confusion is a known acceptable miss).
5. Never delete tuning.json keys the C++ engine reads — it falls back silently and you'll
   think your knob does nothing.

## 5. Environment rebuild (if venv is ever lost)

Python 3.12 venv → `pip install torch torchaudio torchvision --index-url
https://download.pytorch.org/whl/cu128` (RTX 50-series needs cu128/sm_120) →
`pip install -r requirements.txt -c constraints.txt` → verify pins: transformers==4.40.2,
audio-separator 0.44.x, beat_this (checkpoint "final0" auto-downloads), mt3-infer,
torchcrepe, librosa, soundfile, yt-dlp, opencv-python-headless. FFmpeg on PATH.
C++: MSYS2 ucrt64 (g++, ninja), `cmake --preset` in tab_engine (vcpkg manifest pulls
libsndfile, nlohmann-json), binary lands at `tab_engine/build/tab_engine.exe` — the
static-link flags in CMakeLists keep it self-contained.
First GPU run downloads: htdemucs_6s (~2 GB), roformer ckpt (~600 MB), YourMT3, CREPE,
beat_this — all cached under `pipeline/models/` / HF cache; expect a slow first song.
