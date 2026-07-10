from pathlib import Path

import torch

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"

SEPARATION_MODEL = "htdemucs_6s"
# Stage-A model for "best" quality: SOTA vocals/instrumental split (BS-Roformer,
# vocals SDR ~11.8, instrumental ~16.5). htdemucs then splits the instrumental.
VOCAL_SPLIT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
SEPARATION_QUALITY = "best"   # "best" = Roformer→htdemucs two-stage · "fast" = htdemucs only
MODEL_CACHE_DIR = ROOT_DIR / "pipeline" / "models"

TARGET_SAMPLE_RATE = 44_100
TARGET_CHANNELS = 1
NORMALIZATION_THRESHOLD = 0.9

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".aiff", ".opus",
}
