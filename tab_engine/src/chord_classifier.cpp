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
    ChromaVec   vec;
    std::string name;
    int         root;
    bool        is_minor;
    int         n_tones;   // number of pitch classes in the chord
};

static const char* NOTE_NAMES[] = {
    "C","C#","D","D#","E","F","F#","G","G#","A","A#","B"
};

static std::vector<Template> build_templates() {
    std::vector<Template> templates;
    templates.reserve(96);

    // Interval sets (semitones from root)
    const int major_iv[] = {0, 4, 7};        // major triad
    const int minor_iv[] = {0, 3, 7};        // minor triad
    const int power_iv[] = {0, 7};           // power chord (root + fifth, no third)
    const int dom7_iv[]  = {0, 4, 7, 10};    // dominant 7th
    const int maj7_iv[]  = {0, 4, 7, 11};    // major 7th
    const int min7_iv[]  = {0, 3, 7, 10};    // minor 7th
    const int sus4_iv[]  = {0, 5, 7};        // sus4

    struct ChordType {
        const int*  intervals;
        int         count;
        std::string suffix;
        bool        is_minor;
    };

    ChordType types[] = {
        {major_iv, 3, ":maj",  false},
        {minor_iv, 3, ":min",  true },
        {power_iv, 2, ":5",    false},   // power chord — common in rock/punk
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
                type.is_minor,
                type.count
            });
        }
    }
    return templates;
}

// Per-template fit score for one frame. Rewards present chord tones (weighted by
// the template), lightly penalizes chord tones that are absent from the frame.
static double template_score(const ChromaFrame& frame, const Template& tmpl) {
    double max_chroma = *std::max_element(frame.chroma.begin(), frame.chroma.end());
    double score   = 0.0;
    int    present = 0;
    for (int i = 0; i < 12; ++i) {
        double t_val = tmpl.vec[i];
        if (t_val < 0.01) continue;
        double relative_energy = frame.chroma[static_cast<size_t>(i)] / (max_chroma + 1e-9);
        if (relative_energy >= 0.15) {
            score += t_val * frame.chroma[static_cast<size_t>(i)];
            ++present;
        } else {
            score -= 0.03 * t_val;
        }
    }
    int absent = tmpl.n_tones - present;
    if (absent > 1) score -= 0.04 * (absent - 1);
    return score;
}

// Krumhansl-Schmuckler key estimation from a prepared 12-bin pitch profile.
static void estimate_key(const std::array<double, 12>& profile,
                         int& out_tonic, bool& out_major) {
    static const double KMAJ[12] = {6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88};
    static const double KMIN[12] = {6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17};

    double best = -1e18;
    out_tonic = 0;
    out_major = true;
    for (int t = 0; t < 12; ++t) {
        double cmaj = 0.0, cmin = 0.0;
        for (int i = 0; i < 12; ++i) {
            int deg = ((i - t) % 12 + 12) % 12;
            cmaj += profile[static_cast<size_t>(i)] * KMAJ[deg];
            cmin += profile[static_cast<size_t>(i)] * KMIN[deg];
        }
        if (cmaj > best) { best = cmaj; out_tonic = t; out_major = true;  }
        if (cmin > best) { best = cmin; out_tonic = t; out_major = false; }
    }
}

// Diatonic scale membership for the 12 pitch classes of a key.
static std::array<bool, 12> scale_membership(int tonic, bool major) {
    static const int MAJ_DEG[] = {0, 2, 4, 5, 7, 9, 11};
    static const int MIN_DEG[] = {0, 2, 3, 5, 7, 8, 10};   // natural minor
    std::array<bool, 12> in{};
    in.fill(false);
    const int* degs = major ? MAJ_DEG : MIN_DEG;
    for (int k = 0; k < 7; ++k) in[static_cast<size_t>((tonic + degs[k]) % 12)] = true;
    return in;
}

std::vector<ChordLabel> classify_chords(const ChromaResult& chroma,
                                        const std::vector<NoteEvent>& notes,
                                        const std::vector<int>& surviving,
                                        const ClassifierConfig config,
                                        std::string* out_key)
{
    static const std::vector<Template> templates = build_templates();

    const int F   = static_cast<int>(chroma.frames.size());
    const int T   = static_cast<int>(templates.size());
    const int SIL = T;            // "no chord / silence" state
    const int S   = T + 1;        // total states

    std::vector<ChordLabel> labels;
    if (F == 0) return labels;

    const double silence_cutoff = config.silence_threshold * chroma.peak_energy;
    const double frame_window   = static_cast<double>(chroma.frame_size) / chroma.sample_rate;
    constexpr double BIG = 1e9;

    // ── Bass pitch class per frame (lowest surviving note active here) ────────
    std::vector<int>    bass_pc(static_cast<size_t>(F), -1);
    std::array<double, 12> bass_hist{};
    bass_hist.fill(0.0);
    for (int f = 0; f < F; ++f) {
        double ft  = chroma.frames[static_cast<size_t>(f)].time_sec;
        int    lo  = 1000;
        for (int ni : surviving) {
            const NoteEvent& n = notes[static_cast<size_t>(ni)];
            if (n.start_time <= ft + frame_window && n.end_time >= ft && n.pitch < lo)
                lo = n.pitch;
        }
        if (lo != 1000) {
            bass_pc[static_cast<size_t>(f)] = lo % 12;
            bass_hist[static_cast<size_t>(lo % 12)] += 1.0;
        }
    }

    // ── Global key estimate → penalize chords with out-of-key tones ──────────
    // The tonic is almost always the bass root, so blend the (octave-collapsed)
    // chroma profile with the bass-note histogram. This resolves the classic
    // K-S confusion between a major key and its mediant minor (e.g. E major vs
    // G# minor), which share six of seven notes.
    std::array<double, 12> key_profile{};
    key_profile.fill(0.0);
    double chroma_sum = 0.0, bass_sum = 0.0;
    for (const auto& fr : chroma.frames) {
        if (fr.energy < silence_cutoff) continue;
        for (int i = 0; i < 12; ++i) { key_profile[static_cast<size_t>(i)] += fr.chroma[static_cast<size_t>(i)]; chroma_sum += fr.chroma[static_cast<size_t>(i)]; }
    }
    for (double v : bass_hist) bass_sum += v;
    if (chroma_sum > 0 && bass_sum > 0) {
        // Normalize each to unit sum, then add bass with a strong weight.
        for (int i = 0; i < 12; ++i)
            key_profile[static_cast<size_t>(i)] =
                key_profile[static_cast<size_t>(i)] / chroma_sum
                + 1.5 * bass_hist[static_cast<size_t>(i)] / bass_sum;
    }

    int  key_tonic; bool key_major;
    estimate_key(key_profile, key_tonic, key_major);
    std::array<bool, 12> in_key = scale_membership(key_tonic, key_major);
    std::string key_name = std::string(NOTE_NAMES[key_tonic]) + (key_major ? " major" : " minor");
    std::cout << "[chord] Estimated key: " << key_name << "\n";
    if (out_key) *out_key = key_name;

    // Pre-count out-of-key tones for each template.
    std::vector<int> out_of_key(static_cast<size_t>(T), 0);
    for (int t = 0; t < T; ++t) {
        int cnt = 0;
        for (int i = 0; i < 12; ++i)
            if (templates[static_cast<size_t>(t)].vec[static_cast<size_t>(i)] > 0.01
                && !in_key[static_cast<size_t>(i)]) ++cnt;
        out_of_key[static_cast<size_t>(t)] = cnt;
    }

    // ── Emission scores: emission[f*S + s] ────────────────────────────────────
    std::vector<double> emission(static_cast<size_t>(F) * static_cast<size_t>(S));
    std::vector<double> raw_score(static_cast<size_t>(F) * static_cast<size_t>(S), 0.0);

    for (int f = 0; f < F; ++f) {
        const ChromaFrame& frame = chroma.frames[static_cast<size_t>(f)];
        bool silent = frame.energy < silence_cutoff;
        size_t base = static_cast<size_t>(f) * static_cast<size_t>(S);

        for (int t = 0; t < T; ++t) {
            if (silent) { emission[base + static_cast<size_t>(t)] = -BIG; continue; }

            double sc = template_score(frame, templates[static_cast<size_t>(t)]);
            raw_score[base + static_cast<size_t>(t)] = sc;

            double e = sc;
            if (templates[static_cast<size_t>(t)].n_tones >= 4) e -= config.complexity_penalty;
            if (templates[static_cast<size_t>(t)].root == bass_pc[static_cast<size_t>(f)])
                e += config.bass_bonus;
            e -= config.key_penalty * out_of_key[static_cast<size_t>(t)];

            emission[base + static_cast<size_t>(t)] = e;
        }
        emission[base + static_cast<size_t>(SIL)] = silent ? BIG : config.no_chord_floor;
    }

    // ── Viterbi (max-sum) with a uniform chord-switch penalty ─────────────────
    const double lambda = config.transition_penalty;
    std::vector<double> dp(static_cast<size_t>(F) * static_cast<size_t>(S));
    std::vector<int>    back(static_cast<size_t>(F) * static_cast<size_t>(S), -1);

    for (int s = 0; s < S; ++s) dp[static_cast<size_t>(s)] = emission[static_cast<size_t>(s)];

    for (int f = 1; f < F; ++f) {
        size_t prev = static_cast<size_t>(f - 1) * static_cast<size_t>(S);
        size_t cur  = static_cast<size_t>(f)     * static_cast<size_t>(S);

        // Top-2 previous states (so "switch" can avoid penalizing a self-stay).
        int    best_prev = 0;
        double best_val  = dp[prev + 0];
        int    second_prev = -1;
        double second_val  = -BIG;
        for (int s = 1; s < S; ++s) {
            double v = dp[prev + static_cast<size_t>(s)];
            if (v > best_val) {
                second_val = best_val; second_prev = best_prev;
                best_val   = v;        best_prev   = s;
            } else if (v > second_val) {
                second_val = v;        second_prev = s;
            }
        }

        for (int s = 0; s < S; ++s) {
            double stay = dp[prev + static_cast<size_t>(s)];      // no penalty to remain
            double sw_val; int sw_from;
            if (best_prev != s) { sw_val = best_val   - lambda; sw_from = best_prev; }
            else                { sw_val = second_val - lambda; sw_from = second_prev; }

            double chosen; int from;
            if (sw_from < 0 || stay >= sw_val) { chosen = stay;   from = s; }
            else                               { chosen = sw_val; from = sw_from; }

            dp[cur + static_cast<size_t>(s)]   = emission[cur + static_cast<size_t>(s)] + chosen;
            back[cur + static_cast<size_t>(s)] = from;
        }
    }

    // ── Backtrack ─────────────────────────────────────────────────────────────
    size_t lastbase = static_cast<size_t>(F - 1) * static_cast<size_t>(S);
    int    last = 0;
    double lastv = dp[lastbase + 0];
    for (int s = 1; s < S; ++s)
        if (dp[lastbase + static_cast<size_t>(s)] > lastv) {
            lastv = dp[lastbase + static_cast<size_t>(s)];
            last = s;
        }

    std::vector<int> path(static_cast<size_t>(F));
    path[static_cast<size_t>(F - 1)] = last;
    for (int f = F - 1; f > 0; --f)
        path[static_cast<size_t>(f - 1)] =
            back[static_cast<size_t>(f) * static_cast<size_t>(S) + static_cast<size_t>(path[static_cast<size_t>(f)])];

    // ── Build labels ──────────────────────────────────────────────────────────
    labels.reserve(static_cast<size_t>(F));
    for (int f = 0; f < F; ++f) {
        int    s = path[static_cast<size_t>(f)];
        double t = chroma.frames[static_cast<size_t>(f)].time_sec;

        if (s == SIL) {
            bool silent = chroma.frames[static_cast<size_t>(f)].energy < silence_cutoff;
            labels.push_back(ChordLabel{t, silent ? "silence" : "unknown", -1, false, 0.0});
        } else {
            const Template& tmpl = templates[static_cast<size_t>(s)];
            double conf = std::clamp(raw_score[static_cast<size_t>(f) * static_cast<size_t>(S)
                                               + static_cast<size_t>(s)], 0.0, 1.0);
            labels.push_back(ChordLabel{t, tmpl.name, tmpl.root, tmpl.is_minor, conf});
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
        const auto& seg = segments[static_cast<size_t>(i)];
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
