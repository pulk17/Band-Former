# syntax=docker/dockerfile:1
#
# Reproducible Linux build of the Band-Former pipeline.
#
# GPU base (CUDA). For a CPU-only image, replace the FROM line with:
#   FROM python:3.11-slim
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# System deps: Python and FFmpeg (audio decode).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python pipeline ───────────────────────────────────────────────────────────
# Install PyTorch first so the rest of requirements resolves against it.
COPY requirements.txt .
RUN pip3 install --no-cache-dir torch torchaudio \
    && pip3 install --no-cache-dir -r requirements.txt

COPY pipeline/ ./pipeline/
COPY *.py tuning.json ./

# Audio in / tabs out are mounted at runtime: -v "$PWD/data:/app/data"
ENTRYPOINT ["python3", "run_pipeline.py"]
