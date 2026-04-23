#include "chroma_analyzer.hpp"

#include <fftw3.h>
#include <sndfile.h>

#include <array>
#include <cmath>
#include <iostream>
#include <iomanip>
#include <stdexcept>
#include <vector>

static std::vector<double> make_hann_window(int N) {
    std::vector<double> w(N);
    for (int n = 0; n < N; ++n)
        w[n] = 0.5 * (1.0 - std::cos(2.0 * M_PI * n / (N - 1)));
    return w;
}

static int bin_to_chroma(int bin, int sample_rate, int frame_size){
    if(bin == 0) return -1;

    double freq = static_cast<double>(bin) * sample_rate / frame_size;

    // Guitar range: ~82 Hz (E2) to ~1175 Hz (D6).
    if (freq < 75.0 || freq > 1300.0) return -1;

    // Convert frequency -> continuous MIDI pitch
    // Formula: A4 = 440 Hz = MIDI 69
    double midi = 69.0 + 12.0 * std::log2(freq / 440.0);

    int pitch_class = static_cast<int>(std::round(midi)) % 12;
    if(pitch_class < 0) pitch_class += 12;

    return pitch_class;
}

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

ChromaResult compute_chroma(const std::string& wav_path, int frame_size, int hop_size){
    AudioBuffer audio = load_wav(wav_path);
    const int sr = audio.sample_rate;
    const int N = frame_size;
    const int N_bins = N / 2 + 1;

    std::cout << "[chroma] Loaded: " << wav_path << "\n"
              << "         Sample rate : " << sr << " Hz\n"
              << "         Duration    : "
              << std::fixed << std::setprecision(1)
              << static_cast<double>(audio.samples.size()) / sr << " s\n"
              << "         Frame size  : " << N << " samples ("
              << std::setprecision(0) << 1000.0 * N / sr << " ms)\n"
              << "         Hop size    : " << hop_size << " samples ("
              << std::setprecision(0) << 1000.0 * hop_size / sr << " ms)\n";
    
    // Build Hann Window 
    std::vector<double> window = make_hann_window(N);

    std::vector<int> bin_chroma_map(N_bins);
    // Pre-map every FFT bin to its chroma class (or -1 if out of range).
    for(int b = 0; b < N_bins; ++b) bin_chroma_map[b] = bin_to_chroma(b, sr, N);

    // Allocate FFTW buffers
    double* fftw_in  = fftw_alloc_real(static_cast<size_t>(N));
    fftw_complex* fftw_out = fftw_alloc_complex(static_cast<size_t>(N_bins));
    fftw_plan plan = fftw_plan_dft_r2c_1d(N, fftw_in, fftw_out, FFTW_ESTIMATE);

    ChromaResult result;
    result.sample_rate = sr;
    result.frame_size = N;
    result.hop_size = hop_size;
    result.peak_energy = 1e-10; 

    const int total_samples = static_cast<int>(audio.samples.size());

    for(int start = 0; start + N <= total_samples; start += hop_size){
        //RMS of this frame for energy
        double energy_sum = 0.0;
        for(int n = 0; n < N; ++n){
            double s  = audio.samples[static_cast<size_t>(start + n)];
            energy_sum += s * s;
        }
        double rms = std::sqrt(energy_sum / N);
        result.peak_energy = std::max(result.peak_energy, rms);

        // Apply Hann Window
        for(int n = 0; n < N; ++n) fftw_in[n] = audio.samples[static_cast<size_t>(start + n)] * window[n];
        
        fftw_execute(plan);

        // Compute magnitude spectrum and accumulate into chroma bins
        // fftw_out[b] is a complex number (real + imaginary).
        // Magnitude = sqrt(real² + imag²).
        std::array<double, 12> chroma{};
        chroma.fill(0.0);

        for(int b = 1; b < N_bins; ++b){
            int pc = bin_chroma_map[b];
            if(pc < 0) continue;

            double real = fftw_out[b][0];
            double imag = fftw_out[b][1];
            chroma[static_cast<size_t>(pc)] += std::sqrt(real * real + imag * imag);
        }

        l2_normalize(chroma);

        result.frames.push_back(ChromaFrame{static_cast<double>(start) / sr, chroma, rms});
    }

    fftw_destroy_plan(plan);
    fftw_free(fftw_in);
    fftw_free(fftw_out);

    std::cout << "[chroma] Peak RMS (actual): " << std::scientific 
          << std::setprecision(6) << result.peak_energy << "\n";
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
        const auto& frame = result.frames[f];
        std::cout << std::fixed << std::setprecision(2)
                  << std::setw(7) << frame.time_sec << "  "
                  << std::setprecision(4) << std::setw(7) << frame.energy << "  ";
        for (int i = 0; i < 12; ++i)
            std::cout << std::fixed << std::setprecision(2) << std::setw(5) << frame.chroma[i];
        std::cout << "\n";
    }
}