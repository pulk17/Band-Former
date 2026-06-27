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
import threading
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR    = Path(__file__).resolve().parent.parent
INPUT_DIR   = BASE_DIR / "data" / "input"
OUTPUT_DIR  = BASE_DIR / "data" / "output"
STATIC_DIR  = Path(__file__).resolve().parent / "static"

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus")

app = FastAPI(title="Band-Former")


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
_queue: "queue.Queue[tuple[str, Path]]" = queue.Queue()


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
            jid = f"demo-{d.name}"
            _jobs[jid] = Job(id=jid, name=d.name, song_stem=d.name, status="done")


def _worker() -> None:
    """Single background worker: processes jobs sequentially, in-process, so the
    ML models loaded by the pipeline stages stay warm across uploads."""
    from run_pipeline import process_audio   # imported here so server startup stays light
    while True:
        job_id, audio_path = _queue.get()
        with _lock:
            _jobs[job_id].status = "processing"
            _jobs[job_id].stage = "separation → beats → transcription → engine"
        try:
            process_audio(audio_path)
            tab = _output_dir_for(audio_path.stem) / "tab.json"
            with _lock:
                if tab.exists():
                    _jobs[job_id].status = "done"; _jobs[job_id].stage = "complete"
                else:
                    _jobs[job_id].status = "error"; _jobs[job_id].error = "no tab produced"
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _jobs[job_id].status = "error"; _jobs[job_id].error = str(exc)[-2000:]
        finally:
            _queue.task_done()


@app.on_event("startup")
def _startup() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _seed_existing_jobs()
    threading.Thread(target=_worker, daemon=True).start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)) -> JSONResponse:
    suffix = Path(file.filename or "upload.mp3").suffix.lower()
    if suffix not in AUDIO_EXTS:
        raise HTTPException(400, f"Unsupported audio type: {suffix}")

    stem = Path(file.filename or "upload").stem
    dest = INPUT_DIR / f"{stem}{suffix}"
    dest.write_bytes(await file.read())

    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id, name=stem, song_stem=stem)
    with _lock:
        _jobs[job_id] = job

    _queue.put((job_id, dest))
    return JSONResponse({"job_id": job_id})


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


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
