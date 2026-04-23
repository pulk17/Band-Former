#include "chord_classifier.hpp"
#include "note_event.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <iostream>
#include <iomanip>
#include <string>
#include <vector>

using ChromaVec = std::array<double, 12>;

struct Template {
    ChromaVec vec;
    std::string name;
    int root;
    bool is_minor;
};

static const char* NOTE_NAMES[] = {
    "C","C#","D","D#","E","F","F#","G","G#","A","A#","B"
};

static std::vector<Template> build_templates() {
    std::vector<Template> templates;
    templates.reserve(60);

    // Interval sets
    const int major_iv[]  = {0, 4, 7};           // major triad
    const int minor_iv[]  = {0, 3, 7};           // minor triad
    const int dom7_iv[]   = {0, 4, 7, 10};       // dominant 7th (e.g. E7 = E G# B D)
    const int maj7_iv[]   = {0, 4, 7, 11};       // major 7th
    const int min7_iv[]   = {0, 3, 7, 10};       // minor 7th
    const int sus4_iv[]   = {0, 5, 7};           // sus4 (common on guitar)

    struct ChordType {
        const int* intervals;
        int        count;
        std::string suffix;
        bool       is_minor;
    };

    ChordType types[] = {
        {major_iv, 3, ":maj",  false},
        {minor_iv, 3, ":min",  true },
        {dom7_iv,  4, ":7",    false},
        {maj7_iv,  4, ":maj7", false},
        {min7_iv,  4, ":min7", true },
        {sus4_iv,  3, ":sus4", false},
    };

    for (int root = 0; root < 12; ++root) {
        for (const auto& type : types) {
            ChromaVec vec;
            vec.fill(0.0);
            for (int k = 0; k < type.count; ++k)
                vec[static_cast<size_t>((root + type.intervals[k]) % 12)] = 1.0;

            double norm = 0.0;
            for (double v : vec) norm += v * v;
            norm = std::sqrt(norm);
            for (double& v : vec) v /= norm;

            templates.push_back(Template{
                vec,
                std::string(NOTE_NAMES[root]) + type.suffix,
                root,
                type.is_minor
            });
        }
    }
    return templates;
}

static double cosine_sim(const ChromaVec& a, const ChromaVec& b) {
    double dot = 0.0;
    for (int i = 0; i < 12; ++i) dot += a[i] * b[i];
    return dot;
}

static std::vector<int> median_filter(const std::vector<int>& labels, int radius) {
    int n = static_cast<int>(labels.size());
    std::vector<int> out(n);

    for (int i = 0; i < n; ++i) {
        int lo = std::max(0, i - radius);
        int hi = std::min(n - 1, i + radius);

        std::vector<int> valid;
        int total_count = hi - lo + 1;
        for(int j = lo; j <= hi; ++j){
            if(labels[j] >= 0) valid.push_back(labels[j]);
        }

        if(static_cast<int>(valid.size()) < (total_count + 1) / 2){
            out[i] = -1;
        } else {
            std::sort(valid.begin(), valid.end());
            out[i] = valid[valid.size() / 2];
        }
    }
    return out;
}

// Bass note tie-breaker
static int bass_note_check(
    int template_idx, int bass_pc, const std::vector<Template>& templates)
{
    if (bass_pc < 0) return template_idx;

    int root = templates[static_cast<size_t>(template_idx)].root;
    if (bass_pc == root) return template_idx;  // bass confirms root

    // Search for a template whose root matches the bass note and that shares the most pitch classes with the current template
    const ChromaVec& cur_vec = templates[static_cast<size_t>(template_idx)].vec;
    int    best_match_idx   = template_idx;
    double best_overlap     = -1.0;

    for (int t = 0; t < static_cast<int>(templates.size()); ++t) {
        if (templates[static_cast<size_t>(t)].root != bass_pc) continue;

        // Dot product between current template and candidate = overlap
        double overlap = 0.0;
        for (int i = 0; i < 12; ++i)
            overlap += cur_vec[i] * templates[static_cast<size_t>(t)].vec[i];

        if (overlap > best_overlap) {
            best_overlap     = overlap;
            best_match_idx   = t;
        }
    }

    if (best_overlap > 0.5 && best_match_idx != template_idx)
        return best_match_idx;

    return template_idx;
}


std::vector<ChordLabel> classify_chords(const ChromaResult& chroma, const std::vector<NoteEvent>& notes, const std::vector<int>& surviving, const ClassifierConfig config)
{
    static const std::vector<Template> templates = build_templates();

    const int n_frames = static_cast<int>(chroma.frames.size());
    const double silence_cutoff = config.silence_threshold * chroma.peak_energy;
    double hop_sec = static_cast<double>(chroma.frame_size) / chroma.sample_rate;

    // ── Pass 1: per-frame template matching + bass tie-breaker ───────────────

    std::vector<int>    raw_indices(n_frames, -1);
    std::vector<double> confidences(n_frames,  0.0);

    for (int f = 0; f < n_frames; ++f) {
        const ChromaFrame& frame = chroma.frames[static_cast<size_t>(f)];

        // Silence gate
        if (frame.energy < silence_cutoff) {
            raw_indices[f] = -1;
            continue;
        }

        // Find best template by cosine similarity
        int    best_idx   = 0;
        double best_score = -1.0;
        for (int t = 0; t < static_cast<int>(templates.size()); ++t) {
            int chord_tones = 0;
            for (int i = 0; i < 12; ++i)
                if (templates[t].vec[i] > 0.01) ++chord_tones;

            double max_chroma = *std::max_element(frame.chroma.begin(), frame.chroma.end());
            double score = 0.0;
            int present_tones = 0;
            for (int i = 0; i < 12; ++i) {
                double t_val = templates[t].vec[i];
                if (t_val < 0.01) continue;
                double relative_energy = frame.chroma[i] / (max_chroma + 1e-9);
                if (relative_energy >= 0.15) {
                    score += t_val * frame.chroma[i];
                    ++present_tones;
                } else {
                    score -= 0.03 * t_val;
                }
            }
            int absent_tones = chord_tones - present_tones;
            if (absent_tones > 1) score -= 0.04 * (absent_tones - 1);

            if (score > best_score) {
                best_score = score;
                best_idx   = t;
            }
        }

        // Bass note tie-breaker: find lowest MIDI pitch active at this frame
        double frame_time = frame.time_sec;
        int    bass_pc    = -1;
        int    bass_midi  = 999;
        for (int ni : surviving) {
            const NoteEvent& n = notes[static_cast<size_t>(ni)];
            if (n.start_time <= frame_time + hop_sec && n.end_time >= frame_time) {
                if (n.pitch < bass_midi) {
                    bass_midi = n.pitch;
                    bass_pc   = n.pitch % 12;
                }
            }
        }
        best_idx = bass_note_check(best_idx, bass_pc, templates);

        // Confidence gate: reject weak matches
        if (best_score >= config.min_confidence) {
            raw_indices[f] = best_idx;
            confidences[f] = best_score;
        } else {
            raw_indices[f] = -1;
            confidences[f] = 0.0;
        }
    }

    // ── Pass 2: median filter ────────────────────────────────────────────────

    std::vector<int> smoothed = median_filter(raw_indices, config.smooth_radius);

    // ── Pass 3: build ChordLabel output ──────────────────────────────────────

    std::vector<ChordLabel> labels;
    labels.reserve(static_cast<size_t>(n_frames));

    for (int f = 0; f < n_frames; ++f) {
        int idx = smoothed[f];
        const ChromaFrame& frame = chroma.frames[static_cast<size_t>(f)];

        if (idx < 0) {
            std::string label = (frame.energy < silence_cutoff) ? "silence" : "unknown";
            labels.push_back(ChordLabel{frame.time_sec, label, -1, false, 0.0});
        } else {
            const Template& tmpl = templates[static_cast<size_t>(idx)];
            double conf = confidences[f];
            if(idx != raw_indices[f]) conf = cosine_sim(frame.chroma, tmpl.vec);

            labels.push_back(ChordLabel{
                frame.time_sec,
                tmpl.name,
                tmpl.root,
                tmpl.is_minor,
                conf
            });
        }
    }

    return labels;
}


std::vector<ChordSegment> collapse_to_segments(const std::vector<ChordLabel>& labels, double total_duration_sec) {
    std::vector<ChordSegment> segments;
    if (labels.empty()) return segments;

    std::string cur_name  = labels[0].name;
    double      seg_start = labels[0].time_sec;
    double      conf_sum  = labels[0].confidence;
    int         seg_count = 1;

    auto flush = [&](double end_time) {
        segments.push_back(ChordSegment{
            seg_start, end_time, cur_name, conf_sum / seg_count
        });
    };

    for (int f = 1; f < static_cast<int>(labels.size()); ++f) {
        const ChordLabel& lbl = labels[static_cast<size_t>(f)];
        if (lbl.name != cur_name) {
            flush(lbl.time_sec);
            cur_name  = lbl.name;
            seg_start = lbl.time_sec;
            conf_sum  = lbl.confidence;
            seg_count = 1;
        } else {
            conf_sum += lbl.confidence;
            ++seg_count;
        }
    }
    flush(total_duration_sec);

    return segments;
}

void print_chord_segments(const std::vector<ChordSegment>& segments, int max) {
    std::cout << "\nChord Timeline:\n";
    std::cout << std::string(55, '-') << "\n";
    std::cout << std::setw(8)  << "Start"
              << std::setw(8)  << "End"
              << std::setw(10) << "Chord"
              << std::setw(8)  << "Conf"
              << "\n"
              << std::string(55, '-') << "\n";

    int limit = std::min(max, static_cast<int>(segments.size()));
    for (int i = 0; i < limit; ++i) {
        const auto& seg = segments[i];
        std::cout << std::fixed << std::setprecision(2)
                  << std::setw(7)  << seg.start_sec << "s"
                  << std::setw(7)  << seg.end_sec   << "s"
                  << std::setw(10) << seg.name
                  << std::setw(7)  << std::setprecision(3) << seg.avg_confidence
                  << "\n";
    }
    if (static_cast<int>(segments.size()) > max)
        std::cout << "  ... (" << segments.size() - max << " more segments)\n";
}