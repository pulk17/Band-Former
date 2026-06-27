#include "chroma_analyzer.hpp"

#include <sndfile.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <iostream>
#include <iomanip>
#include <stdexcept>
#include <utility>
#include <vector>

static constexpr double TAB_PI = 3.14159265358979323846;

// ── Constant-Q Transform parameters ───────────────────────────────────────────
// One bin per semitone, anchored at C2 so bin k maps to pitch class k % 12.
// Five octaves (C2..B6, ~65 Hz..1976 Hz) covers the guitar's fundamentals plus a
// couple of octaves of harmonics, which reinforce the chroma. Unlike a linear
// FFT, the CQT gives every semitone the same relative resolution — far better in
// the low register where chord roots live.
static constexpr int    CQT_BINS_PER_OCTAVE = 12;
static constexpr int    CQT_OCTAVES         = 5;
static constexpr int    CQT_N_BINS          = CQT_BINS_PER_OCTAVE * CQT_OCTAVES;  // 60
static constexpr double CQT_F_MIN           = 65.406391;                          // C2

// L2-normalize a chroma vector in place.
static void l2_normalize(std::array<double, 12>& chroma) {
    double norm = 0.0;
    for (double v : chroma) norm += v * v;
    norm = std::sqrt(norm);
    if (norm < 1e-9) return;
    for (double& v : chroma) v /= norm;
}

struct AudioBuffer {
    std::vector<double> samples;
    int sample_rate;
};

static AudioBuffer load_wav(const std::string& path){
    SF_INFO info{};
    SNDFILE* sf = sf_open(path.c_str(), SFM_READ, &info);
    if (!sf)
        throw std::runtime_error("Cannot open WAV: " + path + " — " + sf_strerror(nullptr));

    const int channels = info.channels;
    const sf_count_t total_frames = info.frames;
    const int sr = info.samplerate;

    std::vector<double> raw(static_cast<size_t>(total_frames * channels));
    sf_count_t read = sf_readf_double(sf, raw.data(), total_frames);
    sf_close(sf);

    if(read != total_frames) throw std::runtime_error("Short read on WAV file: " + path);

    std::vector<double> mono(static_cast<size_t>(total_frames));
    for (sf_count_t i = 0; i < total_frames; ++i) {
        double sum = 0.0;
        for (int c = 0; c < channels; ++c)
            sum += raw[static_cast<size_t>(i * channels + c)];
        mono[static_cast<size_t>(i)] = sum / channels;
    }

    return AudioBuffer{std::move(mono), sr};
}

// Per-bin temporal kernels: a Hann-windowed complex exponential at the bin's
// centre frequency, with length N_k = Q * sr / f_k (longer for low notes).
struct CQTKernel {
    std::vector<std::vector<double>> re;     // [bin][n]
    std::vector<std::vector<double>> im;
    std::vector<int>                 length; // N_k per bin
    int max_length = 0;
};

static CQTKernel build_cqt_kernels(int sr) {
    const double Q = 1.0 / (std::pow(2.0, 1.0 / CQT_BINS_PER_OCTAVE) - 1.0);

    CQTKernel K;
    K.re.resize(CQT_N_BINS);
    K.im.resize(CQT_N_BINS);
    K.length.resize(CQT_N_BINS);

    for (int k = 0; k < CQT_N_BINS; ++k) {
        double f_k = CQT_F_MIN * std::pow(2.0, static_cast<double>(k) / CQT_BINS_PER_OCTAVE);
        int N_k = static_cast<int>(std::ceil(Q * sr / f_k));
        if (N_k < 4) N_k = 4;

        K.length[static_cast<size_t>(k)] = N_k;
        K.max_length = std::max(K.max_length, N_k);
        K.re[static_cast<size_t>(k)].resize(static_cast<size_t>(N_k));
        K.im[static_cast<size_t>(k)].resize(static_cast<size_t>(N_k));

        for (int n = 0; n < N_k; ++n) {
            double w     = 0.5 * (1.0 - std::cos(2.0 * TAB_PI * n / (N_k - 1)));  // Hann
            double phase = -2.0 * TAB_PI * Q * n / N_k;   // == -2π f_k n / sr
            // Normalize by N_k so magnitudes are comparable across bin lengths.
            K.re[static_cast<size_t>(k)][static_cast<size_t>(n)] = w * std::cos(phase) / N_k;
            K.im[static_cast<size_t>(k)][static_cast<size_t>(n)] = w * std::sin(phase) / N_k;
        }
    }
    return K;
}

ChromaResult compute_chroma(const std::string& wav_path, int frame_size, int hop_size){
    AudioBuffer audio = load_wav(wav_path);
    const int sr = audio.sample_rate;
    const int N  = frame_size;   // window for the RMS energy / silence gate + timing

    std::cout << "[chroma] Loaded: " << wav_path << "\n"
              << "         Sample rate : " << sr << " Hz\n"
              << "         Duration    : "
              << std::fixed << std::setprecision(1)
              << static_cast<double>(audio.samples.size()) / sr << " s\n"
              << "         Transform   : CQT (" << CQT_BINS_PER_OCTAVE
              << " bins/oct, " << CQT_OCTAVES << " octaves from C2)\n"
              << "         Hop size    : " << hop_size << " samples ("
              << std::setprecision(0) << 1000.0 * hop_size / sr << " ms)\n";

    CQTKernel K = build_cqt_kernels(sr);

    ChromaResult result;
    result.sample_rate = sr;
    result.frame_size  = N;
    result.hop_size    = hop_size;
    result.peak_energy = 1e-10;

    const int     total = static_cast<int>(audio.samples.size());
    const double* x     = audio.samples.data();

    for (int start = 0; start + N <= total; start += hop_size) {
        // RMS energy over the reference window (drives the silence gate).
        double energy_sum = 0.0;
        for (int n = 0; n < N; ++n) {
            double s = x[start + n];
            energy_sum += s * s;
        }
        double rms = std::sqrt(energy_sum / N);
        result.peak_energy = std::max(result.peak_energy, rms);

        // CQT centred on the middle of the reference window.
        const int center = start + N / 2;
        std::array<double, 12> chroma{};
        chroma.fill(0.0);

        for (int k = 0; k < CQT_N_BINS; ++k) {
            const int     Nk  = K.length[static_cast<size_t>(k)];
            const double* kre = K.re[static_cast<size_t>(k)].data();
            const double* kim = K.im[static_cast<size_t>(k)].data();
            const int     s0  = center - Nk / 2;

            // Clamp the kernel support to the available samples (zero-pad edges).
            const int n_lo = std::max(0, -s0);
            const int n_hi = std::min(Nk, total - s0);

            double re = 0.0, im = 0.0;
            for (int n = n_lo; n < n_hi; ++n) {
                double s = x[s0 + n];
                re += s * kre[n];
                im += s * kim[n];
            }
            chroma[static_cast<size_t>(k % 12)] += std::sqrt(re * re + im * im);
        }

        l2_normalize(chroma);
        result.frames.push_back(ChromaFrame{static_cast<double>(start) / sr, chroma, rms});
    }

    std::cout << "[chroma] Frames: " << result.frames.size()
              << " | Peak RMS: " << std::scientific << std::setprecision(4)
              << result.peak_energy << std::defaultfloat << "\n";
    return result;
}

void print_chroma(const ChromaResult& result, int max_frames) {
    static const char* NOTE_NAMES[] = {"C","C#","D","D#","E","F","F#","G","G#","A","A#","B"};

    std::cout << "\nChroma frames (first " << max_frames << "):\n";
    std::cout << std::string(80, '-') << "\n";
    std::cout << std::setw(7) << "t(s)" << "  " << std::setw(7) << "RMS" << "  ";
    for (int i = 0; i < 12; ++i)
        std::cout << std::setw(5) << NOTE_NAMES[i];
    std::cout << "\n" << std::string(80, '-') << "\n";

    int limit = std::min(max_frames, static_cast<int>(result.frames.size()));
    for (int f = 0; f < limit; ++f) {
        const auto& frame = result.frames[static_cast<size_t>(f)];
        std::cout << std::fixed << std::setprecision(2)
                  << std::setw(7) << frame.time_sec << "  "
                  << std::setprecision(4) << std::setw(7) << frame.energy << "  ";
        for (int i = 0; i < 12; ++i)
            std::cout << std::fixed << std::setprecision(2) << std::setw(5) << frame.chroma[static_cast<size_t>(i)];
        std::cout << "\n";
    }
}
