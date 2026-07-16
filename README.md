# Band-Former

Turn a song into a playable guitar tab. Band-Former isolates the guitar,
tracks the beat, transcribes notes, then runs a native C++ engine that maps
notes to frets, classifies chords, and solves a globally-consistent fingering.

```
audio ──► [Python pipeline]                       ──► [C++ tab engine]
          separation → beat tracking → notes.json      frets, chords, fingering
```

- **Python pipeline** (`pipeline/`, `run_pipeline.py`) — GPU ML stages:
  source separation (HTDemucs), beat tracking (beat-this), note transcription.
- **C++ tab engine** (`tab_engine/`) — fast native solver: fret-candidate graph,
  note elimination, chroma/chord classification, minimax-Viterbi fingering.

The two halves talk over a `notes.json` handoff, so each can be built and run
independently.

---

## Quickstart

```bash
python -m venv .venv                       # Python 3.12; FFmpeg must be on PATH
.venv/Scripts/python -m pip install torch torchaudio torchvision \
    --index-url https://download.pytorch.org/whl/cu128    # cu128 = RTX 50-series
.venv/Scripts/python -m pip install -r requirements.txt -c constraints.txt
cmake --preset vcpkg && cmake --build build   # in tab_engine/ — see section 1
start.bat                                  # or: python -m uvicorn server.app:app --port 8000
```

Run the server **from the repo root** — the C++ engine reads `tuning.json` from
the working directory. Then open http://127.0.0.1:8000, paste a YouTube link or
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

Band-Former runs on **Linux, Windows, and macOS**. The C++ engine uses
[vcpkg](https://github.com/microsoft/vcpkg) for portable dependencies, and the
build also supports system packages / pkg-config on Linux and MSYS2.

| Component | Linux | Windows | macOS |
|---|:--:|:--:|:--:|
| C++ tab engine | ✅ | ✅ | ✅ |
| Python pipeline | ✅ | ✅ | ✅ (CPU / MPS) |

> GPU (CUDA) is optional. Without it the ML stages fall back to CPU
> automatically — they just run slower.

---

## 1. Build the C++ tab engine

The engine needs two libraries: **libsndfile** and **nlohmann-json**
(chroma uses a self-contained Constant-Q Transform, so no FFT library is
required). Pick whichever path matches your platform.

### Option A — vcpkg (recommended, all platforms)

```bash
# one-time: get vcpkg and point VCPKG_ROOT at it
git clone https://github.com/microsoft/vcpkg
./vcpkg/bootstrap-vcpkg.sh      # bootstrap-vcpkg.bat on Windows
export VCPKG_ROOT=$PWD/vcpkg    # set this env var (setx on Windows)

cd tab_engine
cmake --preset vcpkg            # installs deps from vcpkg.json, configures
cmake --build build --config Release
```

The dependencies are declared in [`tab_engine/vcpkg.json`](tab_engine/vcpkg.json),
so vcpkg installs them automatically on first configure.

If you are **not** using MSVC (e.g. MinGW/Clang on Windows), select a matching
triplet, for example:

```bash
cmake -B build -S . \
  -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
  -DVCPKG_TARGET_TRIPLET=x64-mingw-dynamic
cmake --build build
```

### Option B — system packages (Linux / macOS / MSYS2)

The `CMakeLists.txt` falls back to **pkg-config** when no vcpkg config package is
found, so distro packages work with no extra flags:

```bash
# Debian/Ubuntu
sudo apt install cmake g++ pkg-config libsndfile1-dev nlohmann-json3-dev

# macOS (Homebrew)
brew install cmake libsndfile nlohmann-json

# Arch / MSYS2 (pacman)
pacman -S cmake gcc pkgconf libsndfile nlohmann-json

cd tab_engine
cmake -B build -S .
cmake --build build
```

### Result

The binary lands at one of:

- `tab_engine/build/tab_engine` (Linux/macOS, Ninja/Make)
- `tab_engine/build/tab_engine.exe` (Windows, Ninja/MinGW)
- `tab_engine/build/Release/tab_engine.exe` (Windows, Visual Studio)

`run_pipeline.py` finds it automatically in any of these locations.

You can run the engine standalone once you have a `notes.json` and a guitar
stem WAV (an optional `beats.json` enables beat-grid quantization):

```bash
./tab_engine/build/tab_engine notes.json guitar_stem.wav [beats.json]
```

It writes a **`tab.json`** next to `notes.json` containing the detected key,
BPM, chord timeline, and the fingered, beat-quantized notes
(string/fret/pitch/duration). Render it to readable ASCII tab with:

```bash
python render_tab.py tab.json tab.txt
```

> **Note on libsndfile features:** the C++ engine only ever reads the WAV stem
> produced by the Python stage, so `vcpkg.json` builds libsndfile *without* its
> optional codecs (FLAC/Ogg/Vorbis/Opus/MP3). This keeps the build fast and
> avoids a fragile dependency chain. Feed it WAV.

---

## 2. Set up the Python pipeline

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

## 3. Run the full pipeline

```bash
python run_pipeline.py path/to/song.mp3
```

This separates the guitar, tracks beats, transcribes notes, then runs the C++
engine. The run produces, under `data/output/<song>/`:

- `notes.json` / `beats.json` — pipeline intermediates
- **`tab.json`** — key, BPM, chord timeline, fingered + beat-quantized notes
- **`tab.txt`** — human-readable ASCII guitar tab

Each stage is wrapped so one failure produces a clear message instead of a raw
stack trace; the optional beat-tracking stage degrades gracefully if it fails.

---

## 4. Web app (upload, transcribe, play a synced tab)

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

## 5. Docker (Linux container for the ML pipeline)

For a reproducible Linux environment (the fragile part of the stack), build the
image, which compiles the C++ engine inside it too:

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
tab_engine/               native C++ engine
  CMakeLists.txt          cross-platform build (vcpkg or pkg-config)
  vcpkg.json              C++ dependency manifest
  CMakePresets.json       `cmake --preset vcpkg`
  include/  src/          engine sources (CQT chroma, Viterbi chords, fingering)
server/                   FastAPI backend + Canvas player
  app.py                  API + job runner
  static/                 index.html, app.js, style.css
run_pipeline.py           end-to-end driver
render_tab.py             tab.json -> ASCII guitar tab
requirements.txt          Python dependencies
Dockerfile                reproducible Linux build
```
