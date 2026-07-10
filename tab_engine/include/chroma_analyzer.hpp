#pragma once

#include <vector>
#include <string>
#include <array>

struct ChromaFrame {
    double time_sec;
    std::array<double, 12> chroma;       // full-range chroma (C2..B6): chord quality
    std::array<double, 12> bass_chroma;  // low band only (C1..B2): the bass note
    double energy;
};

struct ChromaResult {
    std::vector<ChromaFrame> frames;
    double peak_energy;
    double tuning_cents;   // detected global tuning offset applied to the CQT
    int sample_rate;
    int frame_size;
    int hop_size;
};

// Load a WAV file and compute tuning-compensated CQT chroma over the signal.
// frame_size: reference window for RMS/timing (default 4096)
// hop_size:   step between frames (default 2048)
// q_mult:     kernel sharpness multiplier (higher = less semitone leakage,
//             slower); inhibition: lateral inhibition factor 0..0.5
ChromaResult compute_chroma(const std::string& wav_path, int frame_size = 4096, int hop_size = 2048,
                            double q_mult = 1.8, double inhibition = 0.30);

void print_chroma(const ChromaResult& result, int max_frames = 10);
