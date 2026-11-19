# Band-Former

Turn a song into a playable guitar tab. Band-Former isolates the guitar,
tracks the beat, transcribes the notes, detects the chords, and works out a
fingering a guitarist can actually play.

```
audio ──► separation → beats → notes (YourMT3) → chords (lv-chordia)
          → note cleanup → key, capo, chord shapes → fingering → tab.json
```

All of it is Python: `pipeline/` holds the GPU stages (separation, beat
tracking, transcription, chord recognition), and the modules at the repo root
(`refine`, `arrange`, `chords_cleanup`, `fingering`, `analyze`) turn their
output into a tab.

---

## Quickstart

```bash
python -m venv .venv                       # Python 3.12; FFmpeg must be on PATH
.venv/Scripts/python -m pip install torch torchaudio torchvision \
    --index-url https://download.pytorch.org/whl/cu128    # cu128 = RTX 50-series
.venv/Scripts/python -m pip install -r requirements.txt -c constraints.txt
start.bat                                  # or: python -m uvicorn server.app:app --port 8000
```

Then open http://127.0.0.1:8000, paste a YouTube link or
upload a file, and pick the instrument. Tick **Piano-tiles video** for
Synthesia-style falling-tile videos: the notes are read from the video itself
instead of transcribed from audio, which is near-exact.

Accuracy is adjustable from the **Tune** button — every detection knob
(`tuning.json`) with a plain-language hint, plus "Save & reprocess song" to hear
the change. Nothing needs a restart.

Further reading: [HANDOFF.md](HANDOFF.md) (how to finish/extend it — read the
golden rules first), [OVERHAUL.md](OVERHAUL.md) (audio-pipeline design notes),
[PIANO_TILES_PLAN.md](PIANO_TILES_PLAN.md) (tiles mode + the knob table).

---

## Platform support

Band-Former runs on **Linux, Windows, and macOS** (macOS on CPU / MPS).

> GPU (CUDA) is optional. Without it the ML stages fall back to CPU
> automatically — they just run slower.

---

## 1. Set up the Python pipeline

Python 3.10+ and [FFmpeg](https://ffmpeg.org/) (on PATH, for audio decoding).

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

# Install PyTorch first, matching your CUDA/CPU — see https://pytorch.org/get-started
pip install torch torchaudio

pip install -r requirements.txt
```

---

## 2. Run the full pipeline

```bash
python run_pipeline.py path/to/song.mp3
```

This separates the guitar, tracks beats, transcribes notes, detects chords
and builds the tab. The run produces, under `data/output/<song>/`:

- `notes.json` / `beats.json` — pipeline intermediates
- **`tab.json`** — key, BPM, capo, chord timeline, fingered notes
- **`tab.txt`** — human-readable ASCII guitar tab

Each stage is wrapped so one failure produces a clear message instead of a raw
stack trace; the optional beat-tracking stage degrades gracefully if it fails.

---

## 3. Web app (upload, transcribe, play a synced tab)

A FastAPI backend plus a Canvas player that scrolls the tab in time with the
audio (Web Audio `AudioContext.currentTime` as the master clock).

```bash
python -m uvicorn server.app:app --port 8000
# then open http://127.0.0.1:8000
```

Any songs you've already processed (anything with a `tab.json` under
`data/output/`) show up immediately in the dropdown. Uploading a new file runs
the full pipeline in the background and loads the result when it's done.

- `POST /api/transcribe` — upload audio, returns a job id
- `GET /api/status/{id}` — poll progress
- `GET /api/result/{id}` — the `tab.json`
- `GET /api/audio/{id}` — the song audio for playback

The frontend (`server/static/`) is plain HTML/Canvas/JS so it runs with no build
step; it can be ported to Next.js later without changing the backend contract.

---

## 4. Docker (Linux container for the ML pipeline)

For a reproducible Linux environment (the fragile part of the stack):

```bash
docker build -t band-former .
docker run --rm --gpus all \
  -v "$PWD/data:/app/data" \
  band-former data/input/song.mp3
```

Drop `--gpus all` to run CPU-only.

---

## Repository layout

```
pipeline/                 Python ML pipeline
  config.py               paths, device, model selection
  stages/
    separation.py         HTDemucs guitar-stem isolation
    beat_tracking.py      beat-this beat/downbeat detection
    pitch_extraction.py   note transcription
    chords.py             lv-chordia chord recognition
refine.py                 drop notes the stem has no energy for
arrange.py                chords cleanup, key, capo, voicings, lead vs rhythm
fingering.py              strings and frets for every note
analyze.py                roman numerals, sections, practice plan
server/                   FastAPI backend + Canvas player
  app.py                  API + job runner
  static/                 index.html, app.js, style.css
run_pipeline.py           end-to-end driver
render_tab.py             tab.json -> ASCII guitar tab
requirements.txt          Python dependencies
Dockerfile                reproducible Linux build
```
