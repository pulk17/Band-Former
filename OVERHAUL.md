# Band-Former Overhaul Plan

A full audit of everything limiting accuracy, plus a redesign plan for the pipeline, the UI, and the
learning experience. Ordered so each phase is shippable on its own. File/line references point at the
code as of this writing.

---

## Part 1 — Accuracy audit (every issue found, ranked)

### 🔴 P0 — Bugs that corrupt results outright

| # | Issue | Where | Status |
|---|-------|-------|--------|
| 1 | **Cached separator writes stems into the FIRST job's folder.** `audio-separator` bakes `output_dir` into the model instance at `load_model()` time; reassigning `separator.output_dir` per job does nothing. Every job after the first wrote its 6 stems into the first song's folder → "Could not build combined stem", "no guitar stem", and missing vocals. | `pipeline/stages/separation.py:44` (`_get_separator`) | ✅ **FIXED** — model_instance.output_dir re-pointed per job; stray stems moved back to their own folders |
| 2 | **Vocals silently skipped.** If no `*Vocals*.wav` is found in the output dir (which #1 caused constantly), the vocals stage does nothing and prints nothing — the UI just shows "No vocals detected". This is the actual reason "torchcrepe does not show". | `run_pipeline.py:189` (`if vstem:` with no `else`) | ✅ FIXED — explicit warning now printed |
| 3 | **Reprocess of an "All instruments" song grabs the wrong stem.** The reprocess path globs `*[Gg]uitar*.wav`, but combined-stem songs should be re-run against `*(Combined)*.wav`. Result: reprocess quietly analyzes only the guitar stem, losing piano/bass content. | `run_pipeline.py:76` | ✅ FIXED — instrument-aware glob |
| 4 | **Notes vanish from the UI when a capo is selected.** Player/Tab views compute `sf = fret - capo` and skip/blank notes with `sf < 0` (`app.js` drawTab `if (sf < 0) continue`). A capo at 4 deletes every note recorded below fret 4 — this is the "some notes aren't even there" complaint. Correct behavior: re-fret the pitch onto another string (what a real player does), or show it as unplayable. | `server/static/app.js:386,443` | ✅ FIXED — client-side refret (same pitch, alternative string ≥ capo) |

### 🟠 P1 — The biggest accuracy levers (model & input choices)

**5. Separation model is the ceiling on everything downstream.**
`htdemucs_6s` (config.py:10) is a 2022 model: vocals SDR ≈ 9, and its guitar/piano stems are known-weak
(guitar SDR ≈ 7–8, piano worse; that's why "other" often contains half the guitar). Installed
`audio-separator 0.44.2` already supports current SOTA Roformer models — no code change needed beyond
the model name and a two-stage flow:

| Model (exact filename) | Stems | SDR | Use for |
|---|---|---|---|
| `model_bs_roformer_ep_317_sdr_12.9755.ckpt` | vocals / instrumental | voc 11.8 / inst **16.5** | Stage A: master vocal–instrumental split |
| `vocals_mel_band_roformer.ckpt` (Kimberley Jensen) | vocals / other | voc **12.6** | Alternative Stage A, best vocal quality |
| `mel_band_roformer_karaoke_becruily.ckpt` | lead vs backing vocals | – | Optional: isolate LEAD vocal for pitch tracking |
| `htdemucs_6s` (keep) | 6 stems | – | Stage B: split the *instrumental* into guitar/bass/piano/drums/other |

**Two-stage plan:** Roformer split first (vocals + clean instrumental) → run htdemucs_6s **on the
instrumental only**. Benefits: CREPE tracks a far cleaner vocal; guitar stem no longer polluted by
vocal bleed; chord chroma gets a 16.5-SDR instrumental. Cost: ~2× separation time (still ≪ MT3 time).

**6. Chords are classified from the wrong audio.**
`main.cpp` computes chroma from `argv[2]` — the *transcription* stem. For instrument=guitar that means
chords are guessed **from the guitar stem alone**: no bass (root!), no keys. This is why roots/inversions
come out wrong. Fix: pass the **instrumental mix** (Stage A output, or sum of non-drum stems) to the
chroma/chord stage, independent of which stem is transcribed for notes. One-line change in
`run_pipeline.py` (pass a second WAV) + accept it in `main.cpp`.

**7. Vocal pitch tracking runs the worst CREPE variant.**
`pitch_extraction.py` used `model='tiny'` (5% of full-model size — much noisier), raw periodicity gate
at 0.5 with no smoothing, and per-frame `round(hz_to_midi)` so vibrato splinters into rapid fake note
changes. Fixes (✅ applied): `model='full'`, median-filtered periodicity, torchcrepe's
silence threshold, gate at 0.21 (torchcrepe's recommended At-threshold), median-smoothed pitch.
Still TODO (Phase 3): hysteresis note segmentation — only split a note when pitch moves >0.6 semitone
for >80 ms, so vibrato/slides stay one note.

### 🟡 P2 — Algorithmic weaknesses in the chord/key engine

All in `tab_engine/src/chord_classifier.cpp`:

8. **No tuning compensation** (chroma_analyzer.cpp:25). CQT bins anchored at A440-based C2. Songs
   tuned down half a step (very common) or slightly sharp/flat smear every chroma bin across two
   semitones. Fix: estimate global tuning offset (parabolic peak on the CQT log-frequency histogram)
   and shift kernel frequencies once per song.
9. **Frame-level classification with a blunt switch penalty** instead of beat-synchronous analysis.
   Chords change on beats; classifying 93 ms frames then Viterbi-smoothing fights flicker instead of
   preventing it. Fix: average chroma **between consecutive beats** (we already have `beat_this`
   beats!) and run the Viterbi over beat-segments. Chord boundaries then land exactly on beats —
   which also makes the UI "Smooth" toggle unnecessary.
10. **Hard binary presence test in template scoring** (line 90: `relative_energy >= 0.15`) — a chord
    tone at 14% of frame max counts as fully absent, at 15% fully present. Replace with continuous
    weighted dot product against the (tuning-corrected, log-compressed) chroma.
11. **Global key penalty punishes borrowed chords** (line 229). One key estimated for the whole song;
    every non-diatonic template pays `key_penalty` on every frame. Songs with a bVII or secondary
    dominant get forced to a wrong diatonic guess. Fix: windowed key tracking (est. per ~16 beats,
    smoothed), penalty scaled by confidence, never applied to the actual best-scoring template by a
    large margin.
12. **Template set too small**: maj, min, 5, 7, maj7, min7, sus4. No sus2, dim, aug, add9, m7b5, 6,
    and **no slash-bass detection** even though we compute `bass_pc` per frame. With the bass *stem*
    (from separation) we can read the real bass note and emit `C/G` style labels.
13. **Silence gate at 2% of peak RMS** (main.cpp:231) mutes quiet intros/outros — they become
    "silence" segments even though a chord is clearly playing. Use a percentile-based floor (e.g.
    5th percentile of non-zero frame energy) instead of peak-relative.
14. **`collapse_to_segments` has no minimum duration**, then `arrange.merge_chords` absorbs short
    blips only into the *previous* segment, and `app.js applyChordSmoothing` re-implements a third
    variant client-side. Three inconsistent smoothing passes = unpredictable timeline. Consolidate:
    beat-sync classification (#9) makes all three obsolete.

### 🟡 P3 — Note-transcription and arrangement weaknesses

15. **YourMT3 output is trusted verbatim.** Typical MT3 failure modes — octave errors, harmonic
    ghosts (a 5th/octave above real notes), smeared offsets — flow straight into the tab. Add a
    post-filter pass: drop notes whose pitch class is absent from the simultaneous chroma frame
    (harmonic ghost check), clamp durations at next-onset, snap onsets to the beat grid (subdiv 4)
    *before* fingering. Cheap and typically removes 10–20% junk notes.
16. **Lead/rhythm split is polyphony-only** (`arrange.py:250`, `LEAD_MAX_POLY=2`): an arpeggiated
    chord (1 note per onset slot) classifies as "lead"; a doubled lead line classifies as "rhythm".
    This is the "lead and rhythm are not what is expected" complaint. Better voice separation:
    cluster notes by **pitch register + temporal continuity + onset density** — keep a running lead
    centroid (EMA of last lead pitch), assign a note to lead only if it's within a leap window of the
    centroid AND locally the highest voice AND not inside a strum cluster; everything else is rhythm.
    Melody extraction should also prefer **contour smoothness over raw height** (current emit() still
    biases high).
17. **Note eliminator's harmonic weights use the timestep's lowest *transcribed* pitch as "bass"**
    (`note_eliminator.cpp:37`) — one ghost sub-bass note reshuffles every weight. Use the bass-stem
    pitch (or chord root from #6) as the reference root instead.
18. **`pick_capo` counts chord types, not playing time** (`arrange.py:99` counts segments; a 20 s
    chord counts the same as a 0.5 s blip) — weight by duration. Also cap search at fret 7 is fine,
    but tie-breaking should prefer the *recommended-shape coverage of the chorus*, i.e. weight by
    how often the chord is on a downbeat.

### 🟢 P4 — Robustness / correctness papercuts

19. `_build_combined_stem` can mix stems with mismatched channel counts (mono vs stereo would
    broadcast-error). Normalize shapes before summing. (`separation.py:56`)
20. BPM = simple mean of beat intervals (`beat_tracking.py:56`); one dropped beat skews it. Use
    median interval.
21. `applyChordSmoothing` snaps `start`/`end` independently to the nearest beat — a 1.4-beat chord
    can snap to zero length and vanish (guarded by `e <= s` skip, which *drops* the chord). Beat-sync
    classification (#9) removes this whole code path.
22. `metadata.instrument` is only written when arrangement succeeds (`run_pipeline.py:169`) — a
    failed arrange leaves reprocess guessing "guitar" for an "all" song.

---

## Part 2 — Target pipeline architecture (v2)

```
                        ┌─────────────────────────────────────────────┐
 audio / YouTube ──────▶│ 0. INGEST  normalize → 44.1k stereo master  │
                        └───────────────────┬─────────────────────────┘
                                            ▼
                        ┌─────────────────────────────────────────────┐
                        │ 1. SPLIT A  BS-Roformer                      │
                        │    → vocals.wav        (SDR ~12)             │
                        │    → instrumental.wav  (SDR ~16.5)           │
                        └───────┬──────────────────────────┬──────────┘
                                ▼                          ▼
              ┌────────────────────────┐   ┌─────────────────────────────┐
              │ 2. SPLIT B  htdemucs_6s │   │ 5. VOCALS  CREPE full        │
              │    on instrumental      │   │    notes + contour           │
              │    → guitar/bass/piano/ │   │    (hysteresis segmentation) │
              │      drums/other        │   └─────────────────────────────┘
              └───────┬───────┬────────┘
                      ▼       ▼
     ┌──────────────────┐   ┌──────────────────────────────────────────┐
     │ 3. NOTES YourMT3  │   │ 4. CHORDS (C++ engine v2)                │
     │  on guitar stem   │   │  input: INSTRUMENTAL mix + bass stem     │
     │  or combined stem │   │  tuning comp → beat-sync chroma →        │
     │  + ghost filter   │   │  rich templates + slash bass →           │
     │  + beat quantize  │   │  windowed-key Viterbi                    │
     └────────┬─────────┘   └───────────────┬──────────────────────────┘
              ▼                             ▼
        ┌─────────────────────────────────────────────┐
        │ 6. ARRANGE v2  lead/rhythm by register+      │
        │    continuity · duration-weighted capo ·     │
        │    voicings                                  │
        └───────────────────┬─────────────────────────┘
                            ▼
        ┌─────────────────────────────────────────────┐
        │ 7. ANALYZE (new)  → analysis.json            │
        │    sections (self-similarity novelty) ·      │
        │    strumming pattern (onset autocorrelation) │
        │    · difficulty score · roman numerals ·     │
        │    cadences · borrowed chords · pentatonic   │
        │    positions · practice plan                 │
        └─────────────────────────────────────────────┘
```

Beats stay on `beat_this` (it is current SOTA; only change: median BPM).

**Why this fixes "at least the exact chords":** chords currently fail for three compounding reasons —
wrong input audio (#6), no tuning compensation (#8), frame-flicker smoothing (#9). Beat-synchronous
chroma from a 16.5-SDR instrumental with a real bass stem is the difference between hobby-grade and
Chordify-grade output. These are input fixes, not exotic ML.

---

## Part 3 — UI/UX redesign

### Problems with the current UI
- **One overloaded toolbar**: transport + 5-tab view switch + content filter + speed + capo + loop +
  metro + smooth — 12 controls in one row, no grouping.
- **Header does four jobs** (brand, song picker, add-song forms, badges) and wraps awkwardly.
- **Notes "scattered"**: Player view draws all 6 string lanes full-width with no section context, no
  waveform, no sense of *where you are in the song*.
- **Chord panel** floats as a sidebar only in Player view; Next-chords list disappears in other views.
- **Learn view is a static text dump** generated once; nothing responds to where you are in the song.
- **Modals** (library/settings) for what should be a sidebar and an inline panel.

### Target layout (single-page, 3 zones)

```
┌────────┬──────────────────────────────────────────────┬─────────┐
│        │  Song title · key · bpm · capo · difficulty   │         │
│  LIB   ├──────────────────────────────────────────────┤ CONTEXT │
│  RARY  │  [Overview] [Player] [Chords] [Tab] [Vocals]  │  RAIL   │
│        │  [Learn]                                      │         │
│  song  │                                               │ NOW/    │
│  list  │              main stage                       │ NEXT    │
│  +add  │                                               │ chords, │
│  +src  │                                               │ lesson  │
│        │                                               │ card    │
├────────┴──────────────────────────────────────────────┴─────────┤
│ ▶  0:42/3:51  [waveform+section strip═══╪═══════]  1× capo:4 ⟳ M │
└──────────────────────────────────────────────────────────────────┘
```

- **Left sidebar — Library.** Song list (rename/delete inline), add-by-YouTube/upload at the top.
  Kills the library modal and the header adder.
- **Bottom dock — Transport.** Play, time, **waveform seek bar with section markers** (intro/verse/
  chorus colored from analysis.json), speed, capo, loop, metronome. Always visible in every tab.
- **Right rail — context aware.** Player: NOW+NEXT chords (current panel, kept). Chords tab: selected
  chord detail. Learn: current lesson step. Collapsible.
- **New Overview tab** (landing view per song): section map, chord palette with diagrams, difficulty,
  tempo/key/capo summary, "Start guided practice" CTA.
- **Section strip everywhere**: clicking a section seeks to it; loop-a-section is one click, replacing
  manual A/B looping as primary flow (A/B stays for power users).
- Visual language: keep the monochrome scheme; one accent color; 8-px spacing grid; cards with 1-px
  borders instead of glows; system font stack; no emoji in UI chrome.

---

## Part 4 — Learning engine (the real "Learn" feature)

Rule-based lesson generation from `tab.json` + `analysis.json` — no LLM required, everything derived
from the actual song. (An optional LLM "explain more" hook can come later.)

### 4.1 analysis.json (computed in stage 7)
```json
{
  "sections": [{"name": "verse", "start": 12.4, "end": 41.0, "chords": ["E:maj","C#:min"], "bars": 8}],
  "strumming": {"pattern": "D DU UDU", "confidence": 0.7, "per_section": {}},
  "difficulty": {"overall": 3, "chords": 2, "changes": 3, "riff": 4, "barre_required": false},
  "romans": {"E:maj": "I", "B:maj": "V", "C#:min": "vi", "A:maj": "IV"},
  "progression_id": "I–V–vi–IV",
  "cadences": [{"at": 39.8, "type": "authentic", "from": "B:maj", "to": "E:maj"}],
  "borrowed": [{"chord": "D:maj", "function": "bVII (mixolydian borrow)"}],
  "solo_scales": [{"name": "E minor pentatonic", "positions": [{"box": 1, "fret": 12}]}],
  "transitions": [{"from": "E:maj", "to": "A:maj", "count": 14, "difficulty": 1}]
}
```

### 4.2 Guided practice mode (step engine)
A per-song course, each step gated on the previous, progress persisted (localStorage + `progress.json`
server-side). Steps generated from the data:

1. **Song map** — sections, where the loop is, what repeats. "This song is 4 chords in a I–V–vi–IV
   loop; the bridge borrows bVII."
2. **Chord shapes** — one card per chord, sorted by playing time: diagram (capo-aware), finger
   numbers, common mistakes for that shape, audio: loop a bar where only that chord rings.
3. **Transition drills** — the top transition *pairs* by count (that's what's actually hard), looped
   two-bar segment at 0.5×, metronome on, auto speed-ramp 0.5→0.75→1.0 when user taps "got it".
4. **Rhythm** — strumming pattern card (D/U arrows synced to the beat grid), loop one section.
5. **Riff/lead phrases** — melody auto-chunked into 2-bar phrases at rests; each phrase is a loop
   card with tab view zoomed; same speed-ramp flow.
6. **Play-through** — full song at 0.75× then 1×, with the section strip showing progress.
7. **Theory unlocks** (contextual cards appearing *when relevant*, not a wall of text):
   - after step 2: why these shapes — intervals, what makes minor minor;
   - after step 3: functional harmony — I/IV/V tension-release, why vi is "sad";
   - capo selected: transposition math ("shapes are in C, sounding key is E♭");
   - song has borrowed chord: modal mixture explained with *this* chord;
   - before step 5: the pentatonic box over this key, which notes to bend into.
8. **Ear training (later)**: mute a random bar's chord label, play it, multiple-choice which chord.

Each theory card: 3–6 sentences max, one diagram, one "try it" action that seeks/loops the song. The
knowledge grows *with the song you're learning* — that's the "intelligent" part, and none of it needs
a model.

---

## Part 5 — Roadmap

| Phase | Scope | Effort | Acceptance test |
|---|---|---|---|
| **0 — Hot fixes** ✅ done today | #1 output_dir, #2 vocals warning, #3 reprocess glob, #4 capo refret, #7 CREPE full+filters | – | New song after a first song → stems in its own folder; vocals render; capo 4 hides nothing |
| **1 — Separation v2** ✅ built | Two-stage Roformer→htdemucs implemented (`separation.py`): Stage A `model_bs_roformer_ep_317_sdr_12.9755.ckpt` → vocals + instrumental, Stage B htdemucs_6s on the instrumental (its vocal residue named "(Residual)" so globs can't grab it); per-model separator cache; "Separation quality" select in Settings (best/fast); Roformer vocals preferred for CREPE. First "best" run downloads the model (~600 MB). | done | **Needs your GPU run to verify** — process one new song on "best" and listen to the vocals stem |
| **2 — Chord engine v2** ✅ done | #6 instrumental input, #8 tuning comp, #9 beat-sync chroma, #10 chordino scoring, #11 windowed key, #12 gated templates + slash bass, #13 percentile gate, **CQT lateral inhibition** (the decisive fix — Hann-widened kernels leaked ~60% of each note into neighbouring semitone bins, drowning every chord's third; sharper Q + peak inhibition fixed root AND quality detection) | done | Where Is My Mind verse: G#→A→E/B→C#m7 — 4/4 roots correct, boundaries beat-locked. Residual: washy mixes sometimes read maj as sus2 (third weak in mix) — acceptable, revisit with harmonic suppression |
| **3 — Notes & arrange v2** ✅ mostly | Done: #15 chord-aware harmonic-ghost filter (sub-90 ms off-chord notes dropped), #16 strum-skyline lead extraction (a top note 5+ semitones above a strum joins the melody), #18 duration-weighted capo, #20 median BPM, vocal hysteresis segmentation (vibrato = one note). Deferred to the iteration bucket: #17 bass-informed eliminator weights, beat-quantizing onsets pre-fingering. | done | **Listen and judge** — lead/rhythm quality is inherently iterative |
| **4 — UI shell** ✅ done | 3-zone layout: library sidebar (inline rename/reprocess/delete, status dots), song header with view tabs + Overview landing tab, right context rail (NOW chord + roman/role + NEXT 3), bottom transport dock with click/drag **waveform seek** + loop region display. Library & seek-slider removed; Smooth button removed (obsolete). | done | Load the app — every feature reachable without a modal |
| **5 — Learning engine** ✅ core done | `analyze.py`: roman numerals + plain-English chord roles, progression loop detection, cadence spotting (authentic/plagal/deceptive, clickable → plays the moment), borrowed-chord/secondary-dominant explanations, pentatonic box positions, difficulty profile, data-driven practice plan with per-song saved progress (click to check off). Deferred: section detection, strumming-pattern detection (both iterative). | done | Learn tab teaches from THIS song's data |
| **6 — Polish / iteration bucket** | sus2-vs-maj bias on washy mixes (chord engine); iss_tarah over-segmentation (transition penalty per-song?); section detection; strumming patterns; #17; beat-quantize onsets; lead-vocal karaoke model; ear-training; PDF export | ongoing | – |

### Suggested order of *implementation* inside each phase
Phase 2 first (chords are the user-stated priority: "at least the exact chords"), then 1 (it lifts
2 and 5's ceiling), then 3, 4, 5.

---

## Part 6 — Risks & notes

- **VRAM**: BS-Roformer + htdemucs + YourMT3 + CREPE full all cached warm ≈ 8–10 GB. On the 12 GB
  RTX 5070 that fits; if not, unload the separator after stage 2 (`del` + `torch.cuda.empty_cache()`),
  it reloads in ~5 s.
- **Roformer speed**: ~1.5–2× htdemucs runtime per track. Worth it; keep "fast mode" toggle.
- **madmom/Essentia** (classic MIREX chord stacks) were considered and rejected: Python 3.12/Windows
  wheel pain, unmaintained; our C++ engine with fixed inputs (#6/#8/#9) reaches comparable quality
  and stays dependency-free.
- **Beat-sync chroma requires beats** — when beat tracking is disabled/fails, fall back to fixed
  500 ms windows (current behavior).
- The tab engine binary interface grows one argument (chroma WAV separate from notes stem). Keep
  backward compat: if arg absent, use the notes stem.
