"""Band-Former web backend (FastAPI).

Endpoints:
  GET  /                      -> the Canvas player UI
  POST /api/transcribe        -> upload audio, start a job, returns {job_id}
  GET  /api/jobs              -> list known jobs
  GET  /api/status/{job_id}   -> {status, stage, error}
  GET  /api/result/{job_id}   -> tab.json
  GET  /api/audio/{job_id}    -> the song audio (for synced playback)

Processing reuses run_pipeline.py as a subprocess, so the whole pipeline
(separation -> beats -> YourMT3 -> C++ engine -> tab.json) runs per job.
Existing results under data/output/ are auto-registered as completed jobs.
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import sys
import threading
import uuid

# The pipeline prints ✓/✗/→; on Windows the default cp1252 console can't encode
# them and raises UnicodeEncodeError mid-job. Force UTF-8 (replace on failure).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from dataclasses import dataclass, asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import yt_dlp

BASE_DIR    = Path(__file__).resolve().parent.parent
INPUT_DIR   = BASE_DIR / "data" / "input"
OUTPUT_DIR  = BASE_DIR / "data" / "output"
STATIC_DIR  = Path(__file__).resolve().parent / "static"

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus")

app = FastAPI(title="Band-Former")


@app.middleware("http")
async def _no_cache(request, call_next):
    # The static UI assets change during development; never let the browser
    # serve a stale index.html / app.js / style.css.
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@dataclass
class Job:
    id: str
    name: str
    song_stem: str
    status: str = "queued"          # queued | processing | done | error
    stage: str = ""
    error: str = ""


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_queue: "queue.Queue[tuple[str, Path, str, dict]]" = queue.Queue()


def _processed_instrument(stem: str) -> str | None:
    tab = OUTPUT_DIR / stem / "tab.json"
    if not tab.exists():
        return None
    try:
        return (json.loads(tab.read_text()).get("metadata") or {}).get("instrument", "guitar")
    except Exception:
        return "guitar"


class MusicManager:
    """Manages parsing, caching, and downloading of music files."""
    def __init__(self, input_dir: Path, output_dir: Path):
        self.input_dir = input_dir
        self.output_dir = output_dir

    def is_processed(self, stem: str) -> bool:
        tab = self.output_dir / stem / "tab.json"
        return tab.exists()

    def get_youtube_info(self, url: str) -> dict:
        ydl_opts = {'quiet': True, 'noplaylist': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    def download_youtube(self, url: str, stem: str) -> Path:
        """Download bestaudio -> {stem}.mp3 (readable title-based name)."""
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': str(self.input_dir / f'{stem}.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'noplaylist': True,
            'overwrites': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        return self.input_dir / f"{stem}.mp3"

    def download_youtube_video(self, url: str, stem: str) -> Path:
        """Download the VIDEO track (<=720p mp4) for tiles extraction."""
        vdir = self.input_dir.parent / "video"
        vdir.mkdir(parents=True, exist_ok=True)
        dest = vdir / f"{stem}.mp4"
        ydl_opts = {
            'format': 'bv*[height<=720][ext=mp4]/bv*[ext=mp4]/bv*',
            'outtmpl': str(dest),
            'quiet': True,
            'noplaylist': True,
            'overwrites': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
        return dest

music_manager = MusicManager(INPUT_DIR, OUTPUT_DIR)


def _safe_stem(name: str) -> str:
    """Filesystem-safe song id: keeps separation output names + dirs consistent."""
    s = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "_")
    return s or "track"


def _output_dir_for(stem: str) -> Path:
    return OUTPUT_DIR / stem


def _find(stem: str, pattern: str) -> Path | None:
    d = _output_dir_for(stem)
    if not d.is_dir():
        return None
    matches = sorted(d.glob(pattern))
    return matches[0] if matches else None


def _find_source_audio(stem: str) -> Path | None:
    for ext in AUDIO_EXTS:
        p = INPUT_DIR / f"{stem}{ext}"
        if p.exists():
            return p
    # fall back to the isolated guitar stem
    return _find(stem, "*[Gg]uitar*.wav")


def _seed_existing_jobs() -> None:
    """Register any already-processed songs (those with a tab.json) as done."""
    if not OUTPUT_DIR.is_dir():
        return
    for d in sorted(OUTPUT_DIR.iterdir()):
        if d.is_dir() and (d / "tab.json").exists():
            jid = d.name
            _jobs[jid] = Job(id=jid, name=d.name, song_stem=d.name, status="done", stage="complete")


def _worker() -> None:
    """Single background worker: processes jobs sequentially, in-process, so the
    ML models loaded by the pipeline stages stay warm across uploads."""
    from run_pipeline import process_audio, process_tiles_video, revocals_only
    while True:
        job_id, audio_path, instrument, options = _queue.get()
        with _lock:
            _jobs[job_id].status = "processing"
            _jobs[job_id].stage = "starting"
        def on_stage(stage_name: str):
            with _lock:
                _jobs[job_id].stage = stage_name
        try:
            if options.get("vocals_only"):
                revocals_only(_jobs[job_id].song_stem,
                              vocal_model=options.get("vocal_model", "auto"),
                              on_stage=on_stage)
            elif options.get("tiles"):
                process_tiles_video(audio_path, options["video_path"],
                                    on_stage=on_stage, options=options)
            else:
                process_audio(audio_path, instrument, on_stage=on_stage, options=options)
            tab = _output_dir_for(audio_path.stem) / "tab.json"
            with _lock:
                if tab.exists():
                    _jobs[job_id].status = "done"; _jobs[job_id].stage = "complete"
                else:
                    _jobs[job_id].status = "error"; _jobs[job_id].error = "no tab produced"
        except Exception as exc:
            with _lock:
                _jobs[job_id].status = "error"; _jobs[job_id].error = str(exc)[-2000:]
        finally:
            _queue.task_done()


@app.on_event("startup")
def _startup() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import torch
        if torch.cuda.is_available():
            print(f"[band-former] GPU: {torch.cuda.get_device_name(0)} · {sys.executable}")
        else:
            print("[band-former] WARNING: CUDA not available — transcription will be SLOW (CPU).")
            print(f"[band-former] python in use: {sys.executable}")
            print("[band-former] launch with the venv: .venv\\Scripts\\python -m uvicorn server.app:app --port 8000")
    except Exception:
        pass
    _seed_existing_jobs()
    threading.Thread(target=_worker, daemon=True).start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    instrument: str = Form("guitar"),
    run_beats: bool = Form(True),
    run_vocals: bool = Form(True),
    vocal_model: str = Form("auto"),
    separation_quality: str = Form("best"),
    tiles: bool = Form(False)
) -> JSONResponse:
    VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi")
    suffix = Path(file.filename or "upload.mp3").suffix.lower()
    if tiles and suffix in VIDEO_EXTS:
        stem = _safe_stem(Path(file.filename or "upload").stem)
        vdir = INPUT_DIR.parent / "video"
        vdir.mkdir(parents=True, exist_ok=True)
        video_dest = vdir / f"{stem}{suffix}"
        video_dest.write_bytes(await file.read())
        dest = INPUT_DIR / f"{stem}.mp3"       # playback audio from the video
        import subprocess as _sp
        _sp.run(["ffmpeg", "-y", "-i", str(video_dest), "-vn", "-b:a", "192k",
                 str(dest)], capture_output=True)
        if not dest.exists():
            raise HTTPException(400, "Could not extract audio from the video (ffmpeg)")
        job_id = stem
        with _lock:
            _jobs[job_id] = Job(id=job_id, name=stem, song_stem=stem)
        _queue.put((job_id, dest, instrument, {
            "run_beats": run_beats, "tiles": True, "video_path": str(video_dest)}))
        return JSONResponse({"job_id": job_id})

    if suffix not in AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type: {suffix}"
                            + (" — tick 'Piano-tiles video' to upload a video" if suffix in VIDEO_EXTS else ""))

    stem = _safe_stem(Path(file.filename or "upload").stem)
    dest = INPUT_DIR / f"{stem}{suffix}"
    dest.write_bytes(await file.read())

    job_id = stem
    if _processed_instrument(stem) == instrument:   # cached AND same instrument
        with _lock:
            if job_id not in _jobs:
                _jobs[job_id] = Job(id=job_id, name=stem, song_stem=stem, status="done", stage="complete")
        return JSONResponse({"job_id": job_id})

    with _lock:
        _jobs[job_id] = Job(id=job_id, name=stem, song_stem=stem)
    _queue.put((job_id, dest, instrument, {
        "run_beats": run_beats,
        "run_vocals": run_vocals,
        "vocal_model": vocal_model,
        "separation_quality": separation_quality
    }))
    return JSONResponse({"job_id": job_id})


class YouTubeRequest(BaseModel):
    url: str
    instrument: str = "guitar"
    run_beats: bool = True
    run_vocals: bool = True
    vocal_model: str = "auto"
    separation_quality: str = "best"
    tiles: bool = False   # Synthesia-style falling-tiles video → notes from VIDEO

@app.post("/api/transcribe/youtube")
def transcribe_youtube(req: YouTubeRequest) -> JSONResponse:
    try:
        info = music_manager.get_youtube_info(req.url)   # metadata only (no download)
    except Exception as e:
        raise HTTPException(400, f"Could not fetch YouTube info: {e}")

    title = info.get('title') or info.get('id') or "youtube"
    stem = _safe_stem(title)                  # readable, title-based id used everywhere
    job_id = stem

    if _processed_instrument(stem) == req.instrument:
        with _lock:
            if job_id not in _jobs:
                _jobs[job_id] = Job(id=job_id, name=title, song_stem=stem, status="done", stage="complete")
        return JSONResponse({"job_id": job_id})

    try:
        dest = music_manager.download_youtube(req.url, stem)
        video_path = music_manager.download_youtube_video(req.url, stem) if req.tiles else None
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")

    job = Job(id=job_id, name=title, song_stem=stem)
    with _lock:
        _jobs[job_id] = job

    _queue.put((job_id, dest, req.instrument, {
        "run_beats": req.run_beats,
        "run_vocals": req.run_vocals,
        "vocal_model": req.vocal_model,
        "separation_quality": req.separation_quality,
        "tiles": req.tiles,
        "video_path": str(video_path) if video_path else ""
    }))
    return JSONResponse({"job_id": job_id})


class ReprocessRequest(BaseModel):
    run_beats: bool = True
    run_vocals: bool = True
    vocal_model: str = "auto"
    instrument: str = ""   # empty = keep the instrument the song was processed with


class RevocalsRequest(BaseModel):
    vocal_model: str = "auto"


@app.post("/api/revocals/{job_id}")
def revocals_job(job_id: str, req: RevocalsRequest) -> JSONResponse:
    """Re-extract just the vocals with a chosen model — quick background job."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    with _lock:
        job.status = "queued"
        job.stage = "starting"
        job.error = ""
    _queue.put((job_id, INPUT_DIR / f"{job_id}.mp3", "",
                {"vocals_only": True, "vocal_model": req.vocal_model}))
    return JSONResponse({"job_id": job_id})


@app.post("/api/reprocess/{job_id}")
def reprocess_job(job_id: str, req: ReprocessRequest) -> JSONResponse:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    
    with _lock:
        job.status = "queued"
        job.stage = "starting"
        job.error = ""
        
    src = _find_source_audio(job.song_stem)
    audio_path = src if src else (INPUT_DIR / f"{job_id}.mp3")
    stored = _processed_instrument(job.song_stem) or "guitar"

    # Tiles songs re-run the VIDEO pipeline (e.g. after changing tiles knobs
    # in tuning.json) — the audio path would look for stems that don't exist.
    if stored == "tiles":
        vdir = INPUT_DIR.parent / "video"
        video = next((p for ext in (".mp4", ".webm", ".mkv", ".mov")
                      for p in [vdir / f"{job.song_stem}{ext}"] if p.exists()), None)
        if video is None:
            with _lock:
                job.status = "error"; job.error = "tiles video file missing — re-add the song"
            raise HTTPException(404, "video file for this tiles song is gone")
        _queue.put((job_id, audio_path, "tiles", {
            "tiles": True, "video_path": str(video),
            "reprocess": True, "run_beats": req.run_beats,
        }))
        return JSONResponse({"job_id": job_id})

    instrument = req.instrument or stored
    retranscribe = instrument != stored   # different stem → notes must be redone

    _queue.put((
        job_id,
        audio_path,
        instrument,
        {
            "run_beats": req.run_beats,
            "run_vocals": req.run_vocals,
            "vocal_model": req.vocal_model,
            "reprocess": True,
            "retranscribe": retranscribe
        }
    ))
    return JSONResponse({"job_id": job_id})


# ── Tuning knobs (in-app editor for tuning.json) ─────────────────────────────
# Factory defaults double as the editor's schema: sections, keys, and reset
# values. tuning.json overrides these; the pipeline re-reads it per run.
TUNING_FACTORY = {
    "chroma": {"q_mult": 1.8, "lateral_inhibition": 0.30},
    "chord": {
        "silence_threshold": 0.02, "transition_penalty": 0.15,
        "complexity_penalty": 0.15, "bass_bonus": 0.8, "no_chord_floor": -0.5,
        "key_penalty": 0.03, "thirdless_penalty": 0.05, "slash_bass_mass": 0.35,
        "key_window_segs": 16, "gate_tau": 0.09, "miss_weight": 0.6,
        "absent_weight": 1.5, "absent_tau": 0.08,
    },
    "arrange": {
        "ghost_dur": 0.07, "melody_min_dur": 0.10, "min_chord_dur": 0.45,
        "lead_max_poly": 2, "skyline_gap_semitones": 5, "harmonic_ghost_max_dur": 0.09,
    },
    "vocals": {
        "crepe_model": "full", "periodicity_threshold": 0.21,
        "split_semitones": 0.6, "hold_seconds": 0.08, "gap_seconds": 0.06,
        "min_note_seconds": 0.05,
    },
    "tiles": {
        "sat_min": 70, "val_min": 70, "artifact_margin_px": 14,
        "min_note_ms": 60, "gap_close_ms": 50, "highlight_delta": 32,
        "white_center_frac": 0.5, "keyboard_y_override": 0, "leftmost_midi_override": 0,
    },
}
_TUNING_PATH = Path(__file__).resolve().parent.parent / "tuning.json"


@app.get("/api/tuning")
def get_tuning() -> dict:
    current = {}
    try:
        current = json.loads(_TUNING_PATH.read_text())
    except Exception:  # noqa: BLE001
        pass
    return {"defaults": TUNING_FACTORY, "current": current}


@app.post("/api/tuning")
def set_tuning(body: dict) -> JSONResponse:
    """Persist knob values. Only known sections/keys are written; values equal
    to factory defaults are still written (explicit is fine — file is small)."""
    clean: dict = {}
    for sec, keys in TUNING_FACTORY.items():
        if sec not in body or not isinstance(body[sec], dict):
            continue
        for k, dv in keys.items():
            if k not in body[sec]:
                continue
            v = body[sec][k]
            if isinstance(dv, bool):
                v = bool(v)
            elif isinstance(dv, int) and not isinstance(dv, bool):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            elif isinstance(dv, float):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
            elif isinstance(dv, str):
                v = str(v)
            clean.setdefault(sec, {})[k] = v
    keep = {}
    try:   # preserve unknown top-level keys like _readme
        keep = {k: v for k, v in json.loads(_TUNING_PATH.read_text()).items()
                if k not in TUNING_FACTORY}
    except Exception:  # noqa: BLE001
        pass
    _TUNING_PATH.write_text(json.dumps({**keep, **clean}, indent=2))
    return JSONResponse({"ok": True})


@app.get("/api/jobs")
def list_jobs() -> dict:
    with _lock:
        return {"jobs": [asdict(j) for j in _jobs.values()]}


@app.get("/api/status/{job_id}")
def status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return asdict(job)


@app.get("/api/result/{job_id}")
def result(job_id: str) -> JSONResponse:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    tab = _output_dir_for(job.song_stem) / "tab.json"
    if not tab.exists():
        raise HTTPException(409, "tab not ready")
    return JSONResponse(json.loads(tab.read_text()))


@app.get("/api/audio/{job_id}")
def audio(job_id: str) -> FileResponse:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    src = _find_source_audio(job.song_stem)
    if not src or not src.exists():
        raise HTTPException(404, "audio not found")
    return FileResponse(src)


def _force_rm(path: Path) -> None:
    """rmtree that survives read-only / transiently-locked files on Windows."""
    import os, stat
    def onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=onerr)


class RenameRequest(BaseModel):
    name: str


@app.post("/api/rename/{job_id}")
def rename_job(job_id: str, req: RenameRequest) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    new_stem = _safe_stem(req.name)
    if not new_stem:
        raise HTTPException(400, "invalid name")
    if new_stem == job.song_stem:
        with _lock:
            job.name = req.name
        return {"ok": True, "job_id": job_id}
    if (OUTPUT_DIR / new_stem).exists() or any((INPUT_DIR / f"{new_stem}{e}").exists() for e in AUDIO_EXTS):
        raise HTTPException(409, "a song with that name already exists")

    for ext in AUDIO_EXTS:                       # rename the source audio
        src = INPUT_DIR / f"{job.song_stem}{ext}"
        if src.exists():
            src.rename(INPUT_DIR / f"{new_stem}{ext}")
    old_dir = OUTPUT_DIR / job.song_stem         # rename the output folder
    if old_dir.is_dir():
        old_dir.rename(OUTPUT_DIR / new_stem)

    with _lock:
        _jobs.pop(job_id, None)
        _jobs[new_stem] = Job(id=new_stem, name=req.name, song_stem=new_stem,
                              status=job.status, stage=job.stage)
    return {"ok": True, "job_id": new_stem}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    with _lock:                                   # drop from the list first
        _jobs.pop(job_id, None)
    err = None
    d = _output_dir_for(job.song_stem)
    try:
        if d.is_dir():
            _force_rm(d)
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    for ext in AUDIO_EXTS:
        p = INPUT_DIR / f"{job.song_stem}{ext}"
        if p.exists():
            try: p.unlink()
            except OSError: pass
    return {"ok": err is None, "error": err, "still_exists": d.exists()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
