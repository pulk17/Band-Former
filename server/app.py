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
import time
import traceback

# The pipeline prints ✓/✗/→; on Windows the default cp1252 console can't encode
# them and raises UnicodeEncodeError mid-job. Force UTF-8 (replace on failure).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from dataclasses import dataclass, asdict, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
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
    error: str = ""                 # one plain sentence for the library row
    detail: str = ""                # the raw error / traceback tail behind it
    outdated: bool = False          # tab.json made by an older pipeline
    has_tab: bool = False           # a playable tab exists (kept even when a reprocess fails)
    started: float = 0.0            # when the current run began (epoch s), for elapsed time
    url: str = ""                   # YouTube source, if any
    instrument: str = "guitar"
    options: dict = field(default_factory=dict)   # what to queue again on Retry


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


# Optional: youtube.com cookies (Netscape cookies.txt) for when YouTube bot-checks
# this IP and the embedded-player fallback isn't enough. Git-ignored.
YT_COOKIES = BASE_DIR / "data" / "youtube_cookies.txt"


def _ydl() -> dict:
    """Options every yt-dlp call shares."""
    opts = {'quiet': True, 'noprogress': True, 'noplaylist': True, 'color': 'never',
            # YouTube needs a JS runtime for its player challenges; yt-dlp only
            # looks for deno unless told otherwise, and node is the common one.
            'js_runtimes': {'deno': {}, 'node': {}},
            # web_embedded still answers when the other clients get "Sign in to
            # confirm you're not a bot" (embeddable videos only, ~96k audio).
            'extractor_args': {'youtube': {'player_client': ['default', 'web_embedded']}}}
    if YT_COOKIES.exists():
        opts['cookiefile'] = str(YT_COOKIES)
    return opts


def _yt_error(e: Exception) -> str:
    msg = str(e)
    if "not a bot" in msg or "429" in msg:
        return msg + (" | YouTube is rate-limiting this connection (too many downloads in a short"
                      " time). It usually clears within a few hours. To get past it now, export"
                      f" your youtube.com cookies in cookies.txt format to {YT_COOKIES}")
    if "403" in msg:
        return msg + (" | YouTube changed something and yt-dlp is behind: run"
                      " pip install -U \"yt-dlp[default]\" and restart the server")
    return msg


class MusicManager:
    """Manages parsing, caching, and downloading of music files."""
    def __init__(self, input_dir: Path, output_dir: Path):
        self.input_dir = input_dir
        self.output_dir = output_dir

    def is_processed(self, stem: str) -> bool:
        tab = self.output_dir / stem / "tab.json"
        return tab.exists()

    def get_youtube_info(self, url: str) -> dict:
        with yt_dlp.YoutubeDL(_ydl()) as ydl:
            return ydl.extract_info(url, download=False)

    def download_youtube(self, url: str, stem: str) -> Path:
        """Download bestaudio -> {stem}.mp3 (readable title-based name)."""
        ydl_opts = {
            **_ydl(),
            'format': 'bestaudio/best',
            'outtmpl': str(self.input_dir / f'{stem}.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
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
            **_ydl(),
            'format': 'bv*[height<=720][ext=mp4]/bv*[ext=mp4]/bv*',
            'outtmpl': str(dest),
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


def _source_audio(stem: str) -> Path | None:
    """The song's own audio in data/input — no fallback to a stem."""
    return next((p for e in AUDIO_EXTS for p in [INPUT_DIR / f"{stem}{e}"] if p.exists()), None)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_job(job: Job) -> None:
    """job.json in the song folder: the library is rebuilt from these on
    startup, so titles, failures and interrupted runs survive a restart."""
    d = _output_dir_for(job.song_stem)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps({
            "title": job.name, "url": job.url, "instrument": job.instrument,
            "options": job.options, "status": job.status, "error": job.error,
            "detail": job.detail}, indent=2))
    except OSError as exc:
        print(f"[band-former] could not save job.json for {job.song_stem}: {exc}")


def _tab_state(stem: str) -> tuple[bool, bool]:
    """(has a tab.json, that tab is from an older pipeline)."""
    from run_pipeline import PIPELINE_VERSION
    tab = _output_dir_for(stem) / "tab.json"
    if not tab.exists():
        return False, False
    ver = (_read_json(tab).get("metadata") or {}).get("pipeline_version", 0)
    return True, ver < PIPELINE_VERSION


def _explain(exc: Exception) -> str:
    """Known failures in one plain sentence; anything else, its last line."""
    msg = str(exc) or type(exc).__name__
    low = msg.lower()
    if "out of memory" in low:
        return ("The GPU ran out of memory. Close other programs using the GPU "
                "(games, video players, other AI apps) and press Retry.")
    if "ffmpeg" in low or "ffprobe" in low:
        return "ffmpeg is missing or failed. Install ffmpeg, make sure it is on PATH, and press Retry."
    if isinstance(exc, yt_dlp.utils.DownloadError):
        return "YouTube download failed: " + _yt_error(exc)
    return msg.strip().splitlines()[-1]


def _enqueue(job: Job, audio_path: Path, options: dict) -> None:
    with _lock:
        job.status, job.stage, job.error, job.detail = "queued", "", "", ""
        job.options = options
        _jobs[job.id] = job
    _save_job(job)
    _queue.put((job.id, audio_path, job.instrument, options))


def _idle_job(job_id: str) -> Job:
    """The job, if it exists and is not being processed right now."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job.status in ("queued", "processing"):
        raise HTTPException(409, "song is being processed")
    return job


def _seed_existing_jobs() -> None:
    """Rebuild the library from data/output: every song folder is a row.

    job.json (if any) gives the title and last status. A run that was queued
    or processing when the server stopped becomes an error row with Retry, and
    so does a folder that never got a tab (an old failed job).
    """
    if not OUTPUT_DIR.is_dir():
        return
    for d in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        info = _read_json(d / "job.json")
        has_tab, outdated = _tab_state(d.name)
        status, error = info.get("status") or ("done" if has_tab else "error"), info.get("error", "")
        if status in ("queued", "processing"):
            status, error = "error", "Interrupted: the server stopped while this song was processing."
        elif status == "done" and not has_tab:
            status, error = "error", "No tab on disk."
        elif status == "error" and not error:
            error = "Processing never finished."
        _jobs[d.name] = Job(id=d.name, name=info.get("title") or d.name, song_stem=d.name,
                            status=status, stage="complete" if status == "done" else "",
                            error=error, detail=info.get("detail", ""), outdated=outdated,
                            has_tab=has_tab, url=info.get("url", ""),
                            instrument=info.get("instrument", "guitar"),
                            options=info.get("options") or {})


def _worker() -> None:
    """Single background worker: processes jobs sequentially, in-process."""
    from run_pipeline import process_audio, process_tiles_video, revocals_only
    while True:
        job_id, audio_path, instrument, options = _queue.get()
        job = _jobs.get(job_id)
        if job is None:                      # deleted while queued
            _queue.task_done()
            continue
        with _lock:
            job.status, job.stage, job.started = "processing", "starting", time.time()
        _save_job(job)

        def on_stage(stage_name: str):
            with _lock:
                job.stage = stage_name
        try:
            # YouTube songs download here, not in the HTTP request, so the row
            # shows progress at once — and a Retry skips what's already on disk.
            if options.get("url"):
                if not _source_audio(job.song_stem):
                    on_stage("Downloading")
                    audio_path = music_manager.download_youtube(options["url"], job.song_stem)
                if options.get("tiles") and not Path(options["video_path"]).exists():
                    on_stage("Downloading video")
                    music_manager.download_youtube_video(options["url"], job.song_stem)
            if options.get("vocals_only"):
                revocals_only(job.song_stem, vocal_model=options.get("vocal_model", "auto"),
                              on_stage=on_stage)
            elif options.get("tiles"):
                process_tiles_video(audio_path, options["video_path"], on_stage=on_stage, options=options)
            else:
                process_audio(audio_path, instrument, on_stage=on_stage, options=options)
            ok, error, detail = True, "", ""
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            ok, error, detail = False, _explain(exc), traceback.format_exc()[-4000:]
        has_tab, outdated = _tab_state(job.song_stem)
        with _lock:
            job.has_tab, job.outdated = has_tab, outdated
            if ok and has_tab:
                job.status, job.stage = "done", "complete"
            else:
                job.status, job.error, job.detail = "error", error or "No tab was produced.", detail
        _save_job(job)
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
        _enqueue(Job(id=stem, name=stem, song_stem=stem, instrument="tiles"), dest,
                 {"run_beats": run_beats, "tiles": True, "video_path": str(video_dest)})
        return JSONResponse({"job_id": stem})

    if suffix not in AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type: {suffix}"
                            + (" — tick 'Piano-tiles video' to upload a video" if suffix in VIDEO_EXTS else ""))

    stem = _safe_stem(Path(file.filename or "upload").stem)
    dest = INPUT_DIR / f"{stem}{suffix}"
    dest.write_bytes(await file.read())

    if stem in _jobs and _jobs[stem].status in ("queued", "processing"):
        return JSONResponse({"job_id": stem})
    if _processed_instrument(stem) == instrument:   # cached AND same instrument
        return JSONResponse({"job_id": stem})
    _enqueue(Job(id=stem, name=stem, song_stem=stem, instrument=instrument), dest, {
        "run_beats": run_beats, "run_vocals": run_vocals,
        "vocal_model": vocal_model, "separation_quality": separation_quality})
    return JSONResponse({"job_id": stem})


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
        raise HTTPException(400, f"Could not fetch YouTube info: {_yt_error(e)}")

    title = info.get('title') or info.get('id') or "youtube"
    stem = _safe_stem(title)                  # readable, title-based id used everywhere
    if stem in _jobs and _jobs[stem].status in ("queued", "processing"):
        return JSONResponse({"job_id": stem})
    if _processed_instrument(stem) == ("tiles" if req.tiles else req.instrument):
        return JSONResponse({"job_id": stem})

    # The download itself is the job's first stage (see _worker).
    video = INPUT_DIR.parent / "video" / f"{stem}.mp4"
    _enqueue(Job(id=stem, name=title, song_stem=stem, url=req.url,
                 instrument="tiles" if req.tiles else req.instrument),
             INPUT_DIR / f"{stem}.mp3", {
                 "url": req.url, "run_beats": req.run_beats, "run_vocals": req.run_vocals,
                 "vocal_model": req.vocal_model, "separation_quality": req.separation_quality,
                 "tiles": req.tiles, "video_path": str(video) if req.tiles else ""})
    return JSONResponse({"job_id": stem})


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
    job = _idle_job(job_id)
    _enqueue(job, INPUT_DIR / f"{job.song_stem}.mp3",
             {"vocals_only": True, "vocal_model": req.vocal_model})
    return JSONResponse({"job_id": job_id})


@app.post("/api/reprocess/{job_id}")
def reprocess_job(job_id: str, req: ReprocessRequest) -> JSONResponse:
    job = _idle_job(job_id)
    audio_path = _source_audio(job.song_stem) or (INPUT_DIR / f"{job.song_stem}.mp3")
    stored = _processed_instrument(job.song_stem) or job.instrument or "guitar"

    # Tiles songs re-run the VIDEO pipeline (e.g. after changing tiles knobs
    # in tuning.json) — the audio path would look for stems that don't exist.
    if stored == "tiles":
        vdir = INPUT_DIR.parent / "video"
        video = next((p for ext in (".mp4", ".webm", ".mkv", ".mov")
                      for p in [vdir / f"{job.song_stem}{ext}"] if p.exists()), None)
        if video is None:
            raise HTTPException(404, "the video file for this tiles song is gone — add the song again")
        job.instrument = "tiles"
        _enqueue(job, audio_path, {"tiles": True, "video_path": str(video),
                                   "reprocess": True, "run_beats": req.run_beats})
        return JSONResponse({"job_id": job_id})

    instrument = req.instrument or stored
    job.instrument = instrument
    _enqueue(job, audio_path, {
        "run_beats": req.run_beats, "run_vocals": req.run_vocals,
        "vocal_model": req.vocal_model, "reprocess": True,
        "retranscribe": instrument != stored})   # different stem -> notes must be redone
    return JSONResponse({"job_id": job_id})


@app.post("/api/retry/{job_id}")
def retry_job(job_id: str) -> JSONResponse:
    """Run a failed or interrupted job again with the options it had."""
    job = _idle_job(job_id)
    options = dict(job.options)
    if job.url and not options.get("url"):
        options["url"] = job.url
    audio_path = _source_audio(job.song_stem) or (INPUT_DIR / f"{job.song_stem}.mp3")
    if not options.get("url") and not audio_path.exists():
        raise HTTPException(404, "the original audio is gone — add the song again")
    _enqueue(job, audio_path, options)
    return JSONResponse({"job_id": job_id})


# ── Tuning knobs (in-app editor for tuning.json) ─────────────────────────────
# Factory defaults double as the editor's schema: sections, keys, and reset
# values. tuning.json overrides these; the pipeline re-reads it per run.
TUNING_FACTORY = {
    "chroma": {"q_mult": 1.8, "lateral_inhibition": 0.40},
    "chord": {
        "silence_threshold": 0.02, "transition_penalty": 0.15,
        "complexity_penalty": 0.15, "bass_bonus": 0.8, "no_chord_floor": -0.5,
        "key_penalty": 0.03, "thirdless_penalty": 0.20, "slash_bass_mass": 0.35,
        "key_window_segs": 16, "gate_tau": 0.18, "miss_weight": 0.6,
        "absent_weight": 1.5, "absent_tau": 0.08,
    },
    "refine": {
        "enabled": 1, "verify_tau": 1.5, "dechatter_ms": 110, "event_ms": 55,
        "min_event_gap_ms": 105, "grid_snap_ms": 45, "nonchord_tau": 4.0,
        "max_polyphony": 6, "ring_seconds": 1.6,
    },
    "arrange": {
        "ghost_dur": 0.07, "melody_min_dur": 0.10,
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


@app.get("/api/shapes")
def shapes() -> dict:
    """Every chord diagram the UI draws, keyed "<root relative to the capo>|<quality>".

    Computed by arrange.voicing, so the diagrams are exactly what the pipeline
    voices — the UI keeps no shape logic of its own to drift out of sync."""
    from arrange import SHAPES, voicing
    return {"voicings": {f"{t}|{q}": voicing(t, q) for t in range(12) for q in SHAPES}}


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
    job = _idle_job(job_id)
    new_stem = _safe_stem(req.name)
    if not new_stem:
        raise HTTPException(400, "invalid name")
    if new_stem == job.song_stem:
        with _lock:
            job.name = req.name
        _save_job(job)
        return {"ok": True, "job_id": job_id}
    if (OUTPUT_DIR / new_stem).exists() or any((INPUT_DIR / f"{new_stem}{e}").exists() for e in AUDIO_EXTS):
        raise HTTPException(409, "a song with that name already exists")

    for ext in AUDIO_EXTS:                       # rename the source audio
        src = INPUT_DIR / f"{job.song_stem}{ext}"
        if src.exists():
            src.rename(INPUT_DIR / f"{new_stem}{ext}")
    vdir = INPUT_DIR.parent / "video"            # rename the tiles video too,
    for ext in (".mp4", ".webm", ".mkv", ".mov"):  # or tiles reprocess breaks
        v = vdir / f"{job.song_stem}{ext}"
        if v.exists():
            v.rename(vdir / f"{new_stem}{ext}")
    old_dir = OUTPUT_DIR / job.song_stem         # rename the output folder
    if old_dir.is_dir():
        old_dir.rename(OUTPUT_DIR / new_stem)

    with _lock:
        _jobs.pop(job_id, None)
        job.id, job.name, job.song_stem = new_stem, req.name, new_stem
        _jobs[new_stem] = job
    _save_job(job)
    return {"ok": True, "job_id": new_stem}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = _idle_job(job_id)
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
    vdir = INPUT_DIR.parent / "video"            # tiles source video, if any
    for ext in (".mp4", ".webm", ".mkv", ".mov"):
        v = vdir / f"{job.song_stem}{ext}"
        if v.exists():
            try: v.unlink()
            except OSError: pass
    return {"ok": err is None, "error": err, "still_exists": d.exists()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
