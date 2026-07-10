# syntax=docker/dockerfile:1
#
# Reproducible Linux build of the whole Band-Former stack: the C++ tab engine
# is compiled inside the image, and the Python ML pipeline is installed on top.
#
# GPU base (CUDA). For a CPU-only image, replace the FROM line with:
#   FROM python:3.11-slim
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# System deps: Python, C++ toolchain, FFmpeg (audio decode), and the two
# native libraries the tab engine links against (resolved via pkg-config).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        build-essential cmake pkg-config \
        ffmpeg \
        libsndfile1-dev nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Build the C++ tab engine ──────────────────────────────────────────────────
COPY tab_engine/ ./tab_engine/
RUN cmake -B tab_engine/build -S tab_engine -DCMAKE_BUILD_TYPE=Release \
    && cmake --build tab_engine/build -j

# ── Python pipeline ───────────────────────────────────────────────────────────
# Install PyTorch first so the rest of requirements resolves against it.
COPY requirements.txt .
RUN pip3 install --no-cache-dir torch torchaudio \
    && pip3 install --no-cache-dir -r requirements.txt

COPY pipeline/ ./pipeline/
COPY run_pipeline.py .

# Audio in / tabs out are mounted at runtime: -v "$PWD/data:/app/data"
ENTRYPOINT ["python3", "run_pipeline.py"]
