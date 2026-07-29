from pathlib import Path

import torch

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"

# Stage-A model for "best" quality: SOTA vocals/instrumental split (BS-Roformer,
# vocals SDR ~11.8, instrumental ~16.5). Stage B then splits the instrumental.
VOCAL_SPLIT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
MODEL_CACHE_DIR = ROOT_DIR / "pipeline" / "models"

# Stage-B model — splits the (de-vocaled) mix into playable instrument stems.
# The guitar stem gates EVERYTHING downstream (notes, chords, tab), so this is
# the highest-leverage model in the pipeline.
#   htdemucs_6s     — Meta's 6-source Demucs. Fast, ~2 GB, the long-time default.
#   BS-Roformer-SW  — jarredou's 6-stem Band-Split RoFormer. Same six sources,
#                     markedly better guitar/piano isolation; slower and a bigger
#                     download. Community's leading 6-stem model.
# Each entry: (model file for audio-separator, filename tag written into stems).
INSTRUMENT_SPLIT_MODELS = {
    "htdemucs_6s": ("htdemucs_6s.yaml", "htdemucs_6s"),
    "roformer_sw": ("BS-Roformer-SW.ckpt", "roformer_sw"),
}

# "fast"  = one htdemucs pass on the raw mix (no vocal pre-split)
# "best"  = Roformer vocal split → htdemucs_6s instrument split
# "ultra" = Roformer vocal split → BS-Roformer-SW instrument split (slowest, best)
SEPARATION_QUALITY = "best"
SEPARATION_PROFILES = {
    "fast":  {"vocal_split": False, "instrument_model": "htdemucs_6s"},
    "best":  {"vocal_split": True,  "instrument_model": "htdemucs_6s"},
    "ultra": {"vocal_split": True,  "instrument_model": "roformer_sw"},
}

TARGET_SAMPLE_RATE = 44_100
TARGET_CHANNELS = 1
NORMALIZATION_THRESHOLD = 0.9

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".aiff", ".opus",
}
