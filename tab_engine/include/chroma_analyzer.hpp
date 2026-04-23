#pragma once

#include <vector>
#include <string>
#include <array>

struct ChromaFrame {
    double time_sec;
    std::array<double, 12> chroma;  // index 0 = C, 1 = C#, ..., 11 = B
    double energy;
};

struct ChromaResult {
    std::vector<ChromaFrame> frames;
    double peak_energy;
    int sample_rate;
    int frame_size;
    int hop_size;
};

// Load a WAV file and compute chroma features over the entire signal.
// frame_size: FFT window length (default 4096)
// hop_size:   step between frames  (default 2048)
ChromaResult compute_chroma(const std::string& wav_path, int frame_size = 4096, int hop_size = 2048);

void print_chroma(const ChromaResult& result, int max_frames = 10);