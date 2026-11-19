# FIXES — exact changes, in order

**Status (2026-11-20, branch `roadmap-fixes`):** fixes 0-5 and Fix 6 steps 1-4 are done and
committed (see `git log`), and Fix 6 step 5 (guitar note model). Step 6 (Ultra stems) is not.
Deviations from the text below, found while doing it:
- Fix 2: arrange's duration-based ghost filters are skipped for notes refine verified
  (`metadata.verified`); with fingering moved last, they deleted half of the real notes.
- Fix 2: fingering also forbids one string twice within 55 ms across neighbouring events.
- Fix 3 step 11 / Fix 5 step 1: the overlay is gone entirely; rows show progress.
- Fix 4: the three dead folders were processed from their mp3s, not deleted. Every song
  is on pipeline version 4 except `tiles_test_audio`: its "video" is an audio-only .webm,
  so it can't be re-read; its old tab still plays. Old tabs are kept as `tab.before_v3.json`.
- Fix 5 step 3: one Sound select; capo sits in the header as a select.
- Fix 6 step 3: no chart could be corrected by ear, so the decision used what can be
  measured (results under Fix 6). lv-chordia reads the COMBINED instrumental, not the source
  mp3: on the full mix it lost darkhaast (21.7% vs 79.7% chord-tone coverage).
- Fix 6 step 5: `hf-midi-transcription` is not on PyPI. It is github.com/xavriley/hf_midi_transcription;
  the pipeline calls its dependency (a fork of piano_transcription_inference) directly with
  `guitar-fl.pth`, which beat the package default `guitar-gaps.pth` on every song.
- Fix 6 step 4: `tab_engine/` is unused but NOT deleted (its build folder and vcpkg
  packages are not in git, so deleting is the owner's call).
- Fix 6 found a cleanup bug: `simple_chords` absorbed chords under 2.0 beats, which ate
  half-bar chords measured at 1.9 (WIMM's G#). The floor is 1.5 beats now.
- Extra: A-shape barre voicings (no diagram above fret 7), section names by repetition,
  strumming card omitted when every eighth is a hit, `tools/ui_harness.py`.

For the model doing the work. `PLAN.md` says why; this says what to type.
Do the fixes in order. After each one run the check, then commit.

Rules that still apply (from `HANDOFF.md`): never start a server on port 8000, never
delete anything in `data/`, run everything from the repo root with `.venv/Scripts/python`.

Measuring tools already in the repo:

- `python tools/song_stats.py data/output` prints chord count, changes/min, same-string
  collisions and fret jumps for every song.
- `python tools/rerun_scratch.py data/output/SONG <scratch_dir>` re-runs chords + refine +
  arrange + fingering on a copy, without touching `data/`. Use it to get before/after numbers.

---

## Fix 0 — housekeeping (10 minutes)

1. `tools/eval.py`: add `import re` next to `import json`. Without it `--chart` crashes
   with `NameError: name 're' is not defined`.
2. `.gitignore`: delete the line `test_*.py`. Then `git add test_arrange.py`.
3. `run_pipeline.py`: add `PIPELINE_VERSION = 2` at module level. In `process_audio`
   where `meta["instrument"] = instrument` is set, and in `process_tiles_video` where
   `meta["instrument"] = "tiles"` is set, add `meta["pipeline_version"] = PIPELINE_VERSION`.
   Bump the number in every fix below that changes output.
4. `server/app.py`: add `outdated: bool = False` to the `Job` dataclass. In
   `_seed_existing_jobs` set it to
   `metadata.get("pipeline_version", 0) < PIPELINE_VERSION` (read `tab.json`).
   `server/static/app.js` `renderSongList`: when `j.outdated`, append
   `<span class="badge">outdated</span>` to the row.

Check: `python tools/eval.py --emit-chart data/output/where_is_my_mind` writes `chart.txt`.

---

## Fix 1 — chords a person can play (code is written and tested)

`chords_cleanup.py` is in the repo and tested. It needs wiring only.

Measured on the current engine's output:

| Song | Chords before | Chords after | Changes/min before → after | Key before → after | Capo before → after |
|---|---|---|---|---|---|
| where_is_my_mind | 16 | 7 | 38 → 20 (one per bar) | G# minor → C# minor | 4 → 4 |
| Bezubaan | 60 | 7 | 41 → 11 | D minor → D minor | 5 → 0 |
| iss_tarah | 61 | 8 | 54 → 20 | E major → E major | 4 → 4 |
| darkhaast | 26 | 6 | 20 → 13 | G# major → Ab major | 1 → 1 |
| Curtain_Blue | 11 | 4 | 19 → 14 | G major → B minor | 7 → 0 |

Known leftovers it cannot fix (they are detector errors, see Fix 6): `F` for 16 s in
where_is_my_mind, `Db` for 46 s in Bezubaan. C# minor is the relative minor of E major
(same notes), which is acceptable.

### 1a. `arrange.py`, function `arrange()`

Replace the block from `chords = merge_chords(tab.get("chords", []))` to the end of
the function with:

```python
    from chords_cleanup import (bar_phase, key_from_chords, pick_capo as pick_capo_v2,
                                simple_chords, tidy_chords, use_flats)
    beats = tab.get("beats", [])
    gaps = sorted(b - a for a, b in zip(beats, beats[1:]) if b > a)
    beat_len = gaps[len(gaps) // 2] if gaps else 0.5
    full = tidy_chords(tab.get("chords", []), beat_len, 1.0)   # detailed level
    key = key_from_chords(full)
    chords = simple_chords(full, key, beat_len)                # default level
    capo = pick_capo_v2(chords)
    for level in (full, chords):
        for c in level:
            p = _parse(c["name"])
            if p:
                c["voicing"] = voicing(p[0], p[1], 0)
                c["capoVoicing"] = voicing(p[0], p[1], capo)
    tab["chords"] = chords
    tab["chords_full"] = full

    meta = tab.setdefault("metadata", {})
    if key:
        meta["key"] = f"{PC_NOTE[key[0]]} {'major' if key[1] else 'minor'}"
        meta["flats"] = use_flats(*key)
    meta["bar_phase"] = bar_phase(beats, chords)
    meta["capo"] = capo
    meta["num_melody"] = len(melody)
    meta["num_notes_clean"] = len(notes)
    return tab
```

Then delete the old `merge_chords` and `pick_capo` from `arrange.py`. Keep the import of
`chords_cleanup` inside the function (it imports `arrange`, so a top-level import loops).

### 1b. `analyze.py`

Chord names inside the practice-plan text are raw (`D:min`). Import `display_name` from
`chords_cleanup` and wrap every chord name that goes into a `detail` string:
`display_name(n, meta.get("flats", False))`.

### 1c. `server/static/app.js`

1. Add next to `shortName`:

```js
  const FLAT = { "C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb" };
  const SUFFIX = { maj: "", min: "m", min7: "m7" };
  function displayName(name) {
    if (!name || !name.includes(":")) return name || "";
    const flats = !!(tab && tab.metadata && tab.metadata.flats);
    const sp = (n) => (flats && FLAT[n]) || n;
    const [root, rest] = name.split(":");
    const [qual, bass] = rest.split("/");
    return sp(root) + (qual in SUFFIX ? SUFFIX[qual] : qual) + (bass ? "/" + sp(bass) : "");
  }
```

2. Replace every place a chord name is shown with `displayName(...)`: `c.name` in
   `drawPlayer` (ribbon text, `chordName`, `nextList`), `buildChordGrid` (`.cn`),
   `buildOverview` (`.nm`), `buildLearn` (chord rows, `b.chord`), and every `shortName(`
   call. Do not change `dataset.name` or any lookup key: those stay raw.
3. Key badge and Overview/Learn key text: split `m.key` on the space and show
   `displayName(tonic + ":maj") + " " + mode`.
4. `loadJob`: add `let detail = "simple"` state; set
   `chordsRaw = detail === "full" ? (tab.chords_full || tab.chords) : tab.chords`.
   Add a two-option select "Chords: Simple / Detailed" in the header that flips `detail`
   and re-runs the same four lines the `barsBtn` handler runs.
5. `quantizeToBars`: start the loop at `(tab.metadata.bar_phase || 0)` instead of 0.
   `drawTab` bar lines: `if ((i - phase) % 4) return;`.
6. Copy the eleven new open shapes (add9, 6, sus2) from `arrange.py` `OPEN_SHAPES` into
   the JS `OPEN` table. They are currently missing there, so they never display.

Check: reprocess one song; the Chords view shows `C#m` not `C#:min`; darkhaast shows
`Ab Eb Cm Db`; `python -m pytest test_arrange.py` passes; `node --check server/static/app.js`.

---

## Fix 2 — playable fingering (spec; write and test it)

New file `fingering.py` with `assign_fingering(notes, capo=0, chords=None) -> list`.
It overwrites `string` and `fret` on each note and returns the notes it could place.

Constants: `TUNE = [64, 59, 55, 50, 45, 40]` (string 1..6), max fret 20, event window
0.055 s (same as `refine.event_ms`).

Algorithm:

1. Sort notes by `start`. Group into events: a note joins the current event if its start
   is within 0.055 s of the event's first note. Cap an event at 6 notes (keep highest,
   lowest, then the rest by pitch).
2. For each event, enumerate placements by depth-first search. A placement gives every
   note a `(string, fret)` such that `TUNE[s] + fret == pitch`, `capo <= fret <= 20`, all
   strings distinct, and the fretted notes (fret > capo) span at most 4 frets.
   If no placement exists, drop the middle-pitched note and retry until one does.
3. The hand position of a placement is its lowest fretted fret, or `None` if every note
   is open. Keep only the cheapest placement per hand position (at most 22 per event).
   Own cost = `2` if the span is exactly 4, plus `0.1 * position`.
4. Dynamic programming over events, minimising the **sum** of
   `own cost + shift`, where `shift = min(12, |pos_a - pos_b|)`, multiplied by `0.4` when
   the gap between the events is over 0.4 s, and `0` when either position is `None`.
   Add `0.25 * |mean string a - mean string b|` to keep lines on nearby strings.
5. Optional bonus: for an event of 3+ notes inside a chord whose `capoVoicing` exists,
   subtract `1.5` for each note sitting on that voicing's string and fret
   (`voicing fret + capo`).
6. Backtrack and write `string` (1..6) and `fret`.

`test_fingering.py` must assert, on a synthetic strum plus a real song from
`tools/rerun_scratch.py`: no two notes of one event share a string; no fret below the
capo; span of fretted notes in an event is at most 4; an E major chord
(pitches 40 47 52 56 59 64) at capo 0 comes out as frets `0 2 2 1 0 0`.

Wire it in `run_pipeline.py` `process_audio`, replacing the current
`refine` → `arrange` sequence:

```python
            data = refine(data, separation_result.guitar_stem_path)
            data = arrange(data)                       # sets voice + capo + chords
            from fingering import assign_fingering
            from refine import resolve_strings
            data["notes"] = assign_fingering(data["notes"], data["metadata"]["capo"], data["chords"])
            resolve_strings(data["notes"], float(_knob("refine", "ring_seconds", 1.6)))
            data["melody"] = [n for n in data["notes"] if n.get("voice") == "lead"]
```

and in `refine.py` `refine()` delete the `resolve_strings(...)` call at the end (it must
run after fingering now). Do the same in `process_tiles_video`.

`app.js`: delete `refret()` and its two call sites; frets now already respect the
recommended capo. When the user picks a different capo, show
`n.fret - capo` and hide notes with `n.fret < capo`.

Check: `tools/song_stats.py` shows 0 same-string collisions and fret jumps over 5 under
2% on all songs. Today: 48-186 collisions and 5-9%.

---

## Fix 3 — stop failing silently

1. **One write.** `run_pipeline.py` `process_audio`: keep the tab in a Python dict from
   the engine output to the end; write once with
   `tmp = tab_json.with_suffix(".tmp"); tmp.write_text(json.dumps(data)); os.replace(tmp, tab_json)`.
   The engine writes `tab.json` itself, so before running it copy the existing file to
   `tab.prev.json`; if any later required step raises, `os.replace` it back and re-raise.
2. **Required vs optional.** Remove the `try/except` around `arrange` and
   `build_analysis`: a failure there must fail the job. Keep `try/except` only around
   refine, vocals, beats and `render_tab`, and in each `except` append the message to
   `data["metadata"].setdefault("warnings", [])`. `app.js` `loadJob` shows
   `m.warnings.join(" · ")` in `#status`.
3. **Traceback.** `server/app.py` `_worker` `except` block: add
   `import traceback; traceback.print_exc()` before storing the error.
4. **Jobs on disk.** Write `data/output/<stem>/job.json`
   (`{"title", "url", "instrument", "status", "error"}`) whenever a job's status changes.
   `_seed_existing_jobs`: register every folder that has `job.json` or `tab.json`; a
   folder with neither `tab.json` nor `status == "done"` becomes `status="error"`,
   `error="interrupted"`. `renderSongList`: error rows get a Retry button (calls the same
   endpoint that created the job) and keep the Delete button.
5. **YouTube in the worker.** `transcribe_youtube`: keep `get_youtube_info`, create the
   job, queue it with `options["url"] = req.url`, return immediately. `_worker`: if
   `options.get("url")`, call `on_stage("Downloading")` then
   `music_manager.download_youtube(...)` first. Add `["Downloading", "Downloading"]` as
   the second entry of both `FLOWS` lists in `app.js`.
6. **Busy guard.** `reprocess_job`, `revocals_job`, `rename_job`, `delete_job`: if
   `job.status in ("queued", "processing")` raise `HTTPException(409, "song is being processed")`.
7. **VRAM.** `run_pipeline.py` after the separation stage: call
   `from pipeline.stages.separation import release_models; release_models()`.
   After `extract_notes`: delete the transcriber the same way
   (`gc.collect(); torch.cuda.empty_cache()`).
8. **CREPE once.** `pitch_extraction.py`: split `_extract_vocals_crepe` so the
   `torchcrepe.predict` + filtering part returns `(times, pitch, periodicity)`; add
   `extract_vocals_and_contour(path, model_choice)` that calls it once and builds both
   the notes and the contour. Use it in `process_audio` and `revocals_only`.
9. **CQT once.** `refine.py`: `note_support(notes, stem_path)` loads the audio and
   computes the CQT on every call and is called twice. Compute `C` once in `refine()`
   and pass it in.
10. **Tiles beats.** `process_tiles_video`: when `extract_beats` fails or returns no
    beats, append a warning as in step 2.
11. **Delete confirm.** `app.js` delete handler: `if (!confirm("Delete " + j.name + "?")) return;`
    first. Song names go into `innerHTML` unescaped: build that row with `textContent`.

Check: kill the server mid-job and restart: the song shows as an error row with Retry.
Break `arrange` on purpose (`raise`): the job ends in error and the old tab still loads.

---

## Fix 4 — reprocess the stale library

After fixes 0-3, reprocess every song marked outdated (stems are on disk; about a minute
each). Reprocess `iss_tarah` with Instrument = All instruments: it has no guitar.
Delete the three dead folders through the UI (they now appear as error rows).

---

## Fix 5 — UI with one path

Do these as separate commits; each is independent.

1. **No blocking overlay.** Remove `showOverlay` calls from `poll`. Instead `poll` updates
   the job's row: `<span class="songStage">Transcribing notes · 1:42</span>`. On `init`,
   call `poll(j.id)` for every job with status `queued` or `processing`, so a reload
   resumes. Keep the overlay only for the first song when the library is empty.
2. **Views 7 → 4.** Remove the Overview view: move its stats card and chord palette to
   the top of Learn, and make Player the default in `loadJob` (`setView("player")`).
   Remove the Tab view: draw fret numbers in the Player lanes for lead notes only (they
   already are). Keep Player, Chords, Vocals, Learn (and Roll for tiles songs).
3. **Dock 12 → 7.** Delete `barsBtn` (Fix 1 already removes flicker) and the `all` button
   in `contentSeg` (it is the same as `both`). Move `capoSel` and the new chord-detail
   select into `#songHeader` beside the badges. Merge `soundSel` and `synthSel` into one
   select: Original / Original + guitar / Guitar only / Original + piano / Piano only.
4. **Tuning.** Hide `tuningBtn` behind the settings modal as an "Advanced tuning" link.
5. **Silent-stem warning.** When `m.warning` exists, show it in a bar above the stage
   with a button that calls `/api/reprocess/{id}` with `instrument: "all"`.
6. **Low confidence.** In `drawPlayer`, chords with `confidence < 0.3` draw at half
   opacity with a trailing `?`.

Check: add a song, reload the page mid-job, play another song while it runs.

---

## Fix 6 — better detector (needs a download, so ask the owner first)

**Done (steps 1-4), 2026-11-20.** The dry run changed no pinned package (it added `h5py`
and `lv-chordia` only). Chord-tone coverage = share of note time whose pitch class is in
the sounding chord, simple level, both detectors through the same cleanup, lv-chordia on
the COMBINED stem:

| song | engine | lv-chordia |
|---|---|---|
| aashiqana | 82.5% | 85.1% |
| Bezubaan | 70.2% (Db 11.7% of the song) | 69.2% (no Db) |
| Chaar Diwaari | 75.1% | 85.0% |
| Curtain Blue (38 notes) | 43.7% | 43.7% |
| darkhaast | 80.0% | 79.0% |
| iss_tarah | 75.0% | 79.5% |
| Playing God | 57.3% | 58.7% |
| tiles_test (exact notes) | 42.3% | 36.1% |
| where_is_my_mind | 81.3% (F 6.4%) | 85.4% (no F) |

WIMM's known loop E C#m G# A covers 92.1% of the song with lv-chordia, 84.5% with the
engine. The engine's chord classifier takes the transcribed notes as input, so coverage
flatters it; its narrow wins are within that bias. lv-chordia also changes chord less
often (Bezubaan 35 changes vs 66).

Original steps:

1. `pip install --dry-run lv-chordia`. If it would change `torch`, `numpy`, `librosa` or
   `transformers`, stop and install with `--no-deps` plus only the missing packages
   (`h5py`, `pydub`, `pretty_midi`).
2. New `pipeline/stages/chords.py`:

```python
def detect_chords(audio_path):
    from lv_chordia.chord_recognition import chord_recognition
    out = []
    for r in chord_recognition(audio_path=str(audio_path), chord_dict_name="submission"):
        name = to_app_name(r["chord"])
        if name:
            out.append({"start": r["start_time"], "end": r["end_time"],
                        "name": name, "confidence": 0.5})
    return out
```

   `to_app_name` converts its labels to this app's grammar: root flats to sharps
   (`Bb` → `A#`), `N`/`X` → `None`, quality by this table and anything unknown by its
   first match: `maj maj6→6 maj7 maj9→maj7 min min6→min min7 min9→min7 7 9 11 13→7
   sus2 sus4 dim dim7→dim hdim7→m7b5 aug minmaj7→min`. Drop the `/bass` part.
3. Decision rule, no judgement needed. For each of the five songs run it on the source
   mp3, put the result through `tidy_chords` + `simple_chords`, and compare with the
   engine's simple chords using a hand-corrected `chart.txt` for at least two songs
   (`tools/eval.py --emit-chart`, fix by ear, `--chart`). Switch only if root agreement
   is higher on both charts. Expected tell-tales: where_is_my_mind has no `F` chord and
   Bezubaan has no `Db`.
4. If it wins: in `process_audio` call `detect_chords(audio_path)` and put the result in
   `data["chords"]` before `refine`. Then the C++ engine only does fingering, which Fix 2
   replaced, so build the tab dict in Python
   (`metadata {key, bpm, duration_sec}`, `beats`, `chords`, `notes` from `notes.json` as
   `{start, duration, pitch, name}`) and delete `tab_engine/`, the engine subprocess
   calls, the `chroma` and `chord` sections of `tuning.json` and `TUNING_FACTORY`, and
   golden rules 1, 4 and 16 in `HANDOFF.md`.
5. **Done, 2026-11-20.** Share of notes refine drops as unverified, YourMT3 → guitar-fl:
   aashiqana 18.5 → 4.5%, Bezubaan 29.2 → 0.4%, Chaar Diwaari 9.7 → 2.7%, Curtain Blue
   6.8 → 0.9%, darkhaast 19.8 → 2.8%, Playing God 17.8 → 3.2%, WIMM 22.8 → 16.6%. Raw
   notes on a chord tone (lv-chordia chords, which never see the notes): 6 of 7 songs up,
   darkhaast 67.8 → 86.3%, WIMM 68.3 → 83.7%. Bezubaan keeps 194 notes instead of 736.
   Not done: the listening check. YourMT3's notes are kept as `notes.yourmt3.json`.
   Original step: Same procedure for notes: `pip install hf-midi-transcription`, transcribe the guitar
   stem with `instrument="guitar"`, and compare the share of notes `refine` drops as
   unverified (today 17-42%). Switch only if that share is lower on at least four of
   five songs and "Hear: Transcription" sounds closer.
6. Download the Ultra stems model once by processing one new song with Separation
   quality = Ultra, and repeat step 5's comparison on its guitar stem.

---

## Not verified

- No chord chart was corrected by ear; the Fix 6 decision rests on the table above.
- Fix 6 step 5 was judged on measurements only; nobody has listened to the new notes yet.
- Fix 6 step 6 (Ultra stems) was never run.
- The UI was checked in `tools/ui_harness.py` (stubbed backend), not against a running
  server; the server lifecycle was checked in-process with FastAPI's TestClient.
