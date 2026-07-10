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

// A chord template with per-tone weights: root and third matter most for the
// label, the fifth is the least informative (it's in almost everything).
struct Template {
    ChromaVec   vec;        // weight per pitch class (0 = not a chord tone)
    double      wsum;       // sum of weights (for normalization)
    std::string name;
    int         root;
    bool        is_minor;
    int         n_tones;
    bool        has_third;      // false for :5 / :sus2 / :sus4
    std::vector<int> gate_pcs;  // pitch classes that must carry real mass
};

static const char* NOTE_NAMES[] = {
    "C","C#","D","D#","E","F","F#","G","G#","A","A#","B"
};

static std::vector<Template> build_templates() {
    // {interval, weight} pairs per chord quality. `gate` lists intervals whose
    // chroma mass must clear a threshold for the template to be usable at all
    // — the tones that DEFINE the color (a maj7 without an audible 7th is just
    // a triad, and letting 4-tone templates assemble themselves from harmonic
    // spill is how every chord turns into somebody's maj7).
    struct Tone { int iv; double w; };
    struct ChordType {
        std::vector<Tone> tones;
        std::vector<int>  gate;
        const char*       suffix;
        bool              is_minor;
    };

    static const std::vector<ChordType> types = {
        {{{0,1.0},{4,0.9},{7,0.75}},          {},       ":maj",   false},
        {{{0,1.0},{3,0.9},{7,0.75}},          {},       ":min",   true },
        {{{0,1.0},{7,0.9}},                   {},       ":5",     false},  // power chord
        {{{0,1.0},{4,0.9},{7,0.7},{10,0.85}}, {10},     ":7",     false},
        {{{0,1.0},{4,0.9},{7,0.7},{11,0.85}}, {11},     ":maj7",  false},
        {{{0,1.0},{3,0.9},{7,0.7},{10,0.85}}, {10},     ":min7",  true },
        {{{0,1.0},{2,0.85},{7,0.8}},          {2},      ":sus2",  false},
        {{{0,1.0},{5,0.85},{7,0.8}},          {5},      ":sus4",  false},
        {{{0,1.0},{3,0.9},{6,0.85}},          {6},      ":dim",   true },
        {{{0,1.0},{4,0.9},{8,0.85}},          {8},      ":aug",   false},
        {{{0,1.0},{4,0.85},{7,0.7},{9,0.8}},  {9},      ":6",     false},
        {{{0,1.0},{3,0.9},{6,0.8},{10,0.8}},  {6,10},   ":m7b5",  true },
        {{{0,1.0},{2,0.7},{4,0.9},{7,0.7}},   {2},      ":add9",  false},
    };

    std::vector<Template> templates;
    templates.reserve(12 * types.size());
    for (int root = 0; root < 12; ++root) {
        for (const auto& type : types) {
            Template t;
            t.vec.fill(0.0);
            t.wsum = 0.0;
            t.has_third = false;
            for (const auto& tone : type.tones) {
                t.vec[static_cast<size_t>((root + tone.iv) % 12)] = tone.w;
                t.wsum += tone.w;
                if (tone.iv == 3 || tone.iv == 4) t.has_third = true;
            }
            t.name     = std::string(NOTE_NAMES[root]) + type.suffix;
            t.root     = root;
            t.is_minor = type.is_minor;
            t.n_tones  = static_cast<int>(type.tones.size());
            for (int giv : type.gate)
                t.gate_pcs.push_back((root + giv) % 12);
            templates.push_back(std::move(t));
        }
    }
    return templates;
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

// One beat-segment of pooled chroma.
struct Seg {
    int    f0, f1;                 // frame range [f0, f1)
    double t0, t1;                 // time range
    ChromaVec chroma;              // log-compressed, L1-normalized chord chroma
    ChromaVec bass;                // L1-normalized bass-band chroma
    int    bass_pc = -1;           // dominant bass pitch class (-1 = none)
    bool   silent  = true;
    int    key_tonic = 0;          // local key (filled later)
    bool   key_major = true;
};

std::vector<ChordLabel> classify_chords(const ChromaResult& chroma,
                                        const std::vector<NoteEvent>& notes,
                                        const std::vector<int>& surviving,
                                        const std::vector<double>& beats,
                                        const ClassifierConfig config,
                                        std::string* out_key)
{
    (void)notes; (void)surviving;   // bass now comes from the bass-band chroma
    static const std::vector<Template> templates = build_templates();

    const int F = static_cast<int>(chroma.frames.size());
    std::vector<ChordLabel> labels;
    if (F == 0) return labels;

    const int T   = static_cast<int>(templates.size());
    const int SIL = T;
    const int S   = T + 1;
    constexpr double BIG = 1e9;

    // ── Percentile-based silence gate (#13) ───────────────────────────────────
    double gate;
    {
        std::vector<double> e;
        e.reserve(static_cast<size_t>(F));
        for (const auto& fr : chroma.frames) e.push_back(fr.energy);
        std::nth_element(e.begin(), e.begin() + static_cast<long>(e.size() * 95 / 100),
                         e.end());
        double p95 = e[static_cast<size_t>(e.size() * 95 / 100)];
        gate = std::max(1e-7, config.silence_threshold * p95);
    }

    // ── Build beat-segment boundaries (#9) ────────────────────────────────────
    const double t_end     = chroma.frames[static_cast<size_t>(F - 1)].time_sec
                             + static_cast<double>(chroma.frame_size) / chroma.sample_rate;

    std::vector<double> bounds;
    bounds.push_back(0.0);
    if (beats.size() >= 2) {
        for (double b : beats)
            if (b > bounds.back() + 0.06 && b < t_end) bounds.push_back(b);
    } else {
        for (double t = 0.5; t < t_end; t += 0.5) bounds.push_back(t);
    }
    bounds.push_back(t_end);

    // ── Pool chroma per segment ───────────────────────────────────────────────
    std::vector<Seg> segs;
    segs.reserve(bounds.size());
    int f = 0;
    for (size_t b = 0; b + 1 < bounds.size(); ++b) {
        Seg s;
        s.t0 = bounds[b];
        s.t1 = bounds[b + 1];
        s.f0 = f;
        while (f < F && chroma.frames[static_cast<size_t>(f)].time_sec < s.t1) ++f;
        s.f1 = f;
        if (s.f1 <= s.f0) continue;   // no frames in this sliver — skip

        ChromaVec acc{}, bacc{};
        acc.fill(0.0); bacc.fill(0.0);
        double wsum = 0.0;
        for (int i = s.f0; i < s.f1; ++i) {
            const ChromaFrame& fr = chroma.frames[static_cast<size_t>(i)];
            if (fr.energy < gate) continue;
            for (int k = 0; k < 12; ++k) {
                acc[static_cast<size_t>(k)]  += fr.energy * fr.chroma[static_cast<size_t>(k)];
                bacc[static_cast<size_t>(k)] += fr.energy * fr.bass_chroma[static_cast<size_t>(k)];
            }
            wsum += fr.energy;
        }
        if (wsum > 1e-12) {
            s.silent = false;
            // L1-normalize the energy-weighted mean chroma. No compression:
            // the scoring's missing-tone threshold handles moderate tones, and
            // flattening the profile drowns real chords in off-chord mass.
            double sum = 0.0;
            for (int k = 0; k < 12; ++k) sum += acc[static_cast<size_t>(k)];
            if (sum > 1e-12)
                for (double& v : acc) v /= sum;
            s.chroma = acc;

            double bsum = 0.0;
            for (double v : bacc) bsum += v;
            if (bsum > 1e-12) {
                for (double& v : bacc) v /= bsum;
                s.bass = bacc;
                int arg = 0;
                for (int k = 1; k < 12; ++k)
                    if (bacc[static_cast<size_t>(k)] > bacc[static_cast<size_t>(arg)]) arg = k;
                if (bacc[static_cast<size_t>(arg)] >= 0.25) s.bass_pc = arg;
            }
        }
        segs.push_back(s);
    }

    const int NS = static_cast<int>(segs.size());
    if (NS == 0) {
        for (int i = 0; i < F; ++i)
            labels.push_back(ChordLabel{chroma.frames[static_cast<size_t>(i)].time_sec,
                                        "silence", -1, false, 0.0});
        return labels;
    }

    // ── Windowed key tracking (#11) ───────────────────────────────────────────
    const int win  = std::max(4, config.key_window_segs);
    const int nwin = (NS + win - 1) / win;
    std::vector<int>  win_tonic(static_cast<size_t>(nwin), 0);
    std::vector<bool> win_major(static_cast<size_t>(nwin), true);

    std::array<double, 12> global_profile{};
    global_profile.fill(0.0);
    for (int w = 0; w < nwin; ++w) {
        std::array<double, 12> prof{}, bass_hist{};
        prof.fill(0.0); bass_hist.fill(0.0);
        double csum = 0.0, bsum = 0.0;
        for (int i = w * win; i < std::min(NS, (w + 1) * win); ++i) {
            if (segs[static_cast<size_t>(i)].silent) continue;
            double dur = segs[static_cast<size_t>(i)].t1 - segs[static_cast<size_t>(i)].t0;
            for (int k = 0; k < 12; ++k) {
                prof[static_cast<size_t>(k)] += dur * segs[static_cast<size_t>(i)].chroma[static_cast<size_t>(k)];
                csum += dur * segs[static_cast<size_t>(i)].chroma[static_cast<size_t>(k)];
            }
            if (segs[static_cast<size_t>(i)].bass_pc >= 0) {
                bass_hist[static_cast<size_t>(segs[static_cast<size_t>(i)].bass_pc)] += dur;
                bsum += dur;
            }
        }
        // Blend the (octave-collapsed) chroma profile with the bass-note
        // histogram: the tonic is almost always the bass root. This resolves
        // the classic K-S confusion between a major key and its mediant minor.
        std::array<double, 12> blended{};
        blended.fill(0.0);
        if (csum > 1e-12)
            for (int k = 0; k < 12; ++k) {
                blended[static_cast<size_t>(k)] = prof[static_cast<size_t>(k)] / csum
                    + (bsum > 1e-12 ? 1.5 * bass_hist[static_cast<size_t>(k)] / bsum : 0.0);
                global_profile[static_cast<size_t>(k)] += blended[static_cast<size_t>(k)];
            }
        int tonic; bool major;
        estimate_key(blended, tonic, major);
        win_tonic[static_cast<size_t>(w)] = tonic;
        win_major[static_cast<size_t>(w)] = major;
    }
    for (int i = 0; i < NS; ++i) {
        int w = std::min(i / win, nwin - 1);
        segs[static_cast<size_t>(i)].key_tonic = win_tonic[static_cast<size_t>(w)];
        segs[static_cast<size_t>(i)].key_major = win_major[static_cast<size_t>(w)];
    }

    int  g_tonic; bool g_major;
    estimate_key(global_profile, g_tonic, g_major);
    std::string key_name = std::string(NOTE_NAMES[g_tonic]) + (g_major ? " major" : " minor");
    std::cout << "[chord] Estimated key: " << key_name << "\n";
    if (out_key) *out_key = key_name;

    // ── Emissions per segment ─────────────────────────────────────────────────
    std::vector<double> emission(static_cast<size_t>(NS) * static_cast<size_t>(S));
    std::vector<double> fit_score(static_cast<size_t>(NS) * static_cast<size_t>(S), 0.0);

    for (int i = 0; i < NS; ++i) {
        const Seg& sg = segs[static_cast<size_t>(i)];
        size_t base = static_cast<size_t>(i) * static_cast<size_t>(S);
        std::array<bool, 12> in_key = scale_membership(sg.key_tonic, sg.key_major);

        for (int t = 0; t < T; ++t) {
            if (sg.silent) { emission[base + static_cast<size_t>(t)] = -BIG; continue; }
            const Template& tp = templates[static_cast<size_t>(t)];

            // Characteristic-tone gate: the color tone must actually sound
            // (above the uniform 1/12 floor, so flat mush can't pass).
            bool gated_out = false;
            for (int pc : tp.gate_pcs)
                if (sg.chroma[static_cast<size_t>(pc)] < config.gate_tau) { gated_out = true; break; }
            if (gated_out) {
                emission[base + static_cast<size_t>(t)]  = -BIG;
                fit_score[base + static_cast<size_t>(t)] = -1.0;
                continue;
            }

            // Chordino-style scoring on the L1-normalized chroma:
            //   + mass on chord tones (weighted)
            //   − mass OFF the chord (a sounding third kills a :5 label)
            //   − missing-tone penalty (a chord tone that ISN'T sounding
            //     kills a maj7 label when no 7th is there)
            double hit = 0.0, miss = 0.0, absent = 0.0;
            int    off_key = 0;
            for (int k = 0; k < 12; ++k) {
                double w = tp.vec[static_cast<size_t>(k)];
                double c = sg.chroma[static_cast<size_t>(k)];
                if (w > 0.0) {
                    hit += w * c;
                    absent += w * std::max(0.0, config.absent_tau - c);
                    if (!in_key[static_cast<size_t>(k)]) ++off_key;
                } else {
                    miss += c;
                }
            }
            double fit = hit - config.miss_weight * miss - config.absent_weight * absent;
            fit_score[base + static_cast<size_t>(t)] = fit;

            double e = fit;
            e -= config.complexity_penalty * (tp.n_tones - 3);
            if (!tp.has_third) e -= config.thirdless_penalty;
            // Continuous bass anchoring: the root must be supported by actual
            // bass-register mass. This is what pins the root when supersets
            // (add one stray bin, capture more mass) would otherwise win.
            e += config.bass_bonus * sg.bass[static_cast<size_t>(tp.root)];
            e -= config.key_penalty * off_key;
            emission[base + static_cast<size_t>(t)] = e;
        }
        emission[base + static_cast<size_t>(SIL)] = sg.silent ? BIG : config.no_chord_floor;
    }

    // ── Viterbi over segments ─────────────────────────────────────────────────
    const double lambda = config.transition_penalty;
    std::vector<double> dp(static_cast<size_t>(NS) * static_cast<size_t>(S));
    std::vector<int>    back(static_cast<size_t>(NS) * static_cast<size_t>(S), -1);

    for (int s = 0; s < S; ++s) dp[static_cast<size_t>(s)] = emission[static_cast<size_t>(s)];

    for (int i = 1; i < NS; ++i) {
        size_t prev = static_cast<size_t>(i - 1) * static_cast<size_t>(S);
        size_t cur  = static_cast<size_t>(i)     * static_cast<size_t>(S);

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
            double stay = dp[prev + static_cast<size_t>(s)];
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

    size_t lastbase = static_cast<size_t>(NS - 1) * static_cast<size_t>(S);
    int    last = 0;
    double lastv = dp[lastbase + 0];
    for (int s = 1; s < S; ++s)
        if (dp[lastbase + static_cast<size_t>(s)] > lastv) {
            lastv = dp[lastbase + static_cast<size_t>(s)];
            last = s;
        }

    std::vector<int> path(static_cast<size_t>(NS));
    path[static_cast<size_t>(NS - 1)] = last;
    for (int i = NS - 1; i > 0; --i)
        path[static_cast<size_t>(i - 1)] =
            back[static_cast<size_t>(i) * static_cast<size_t>(S) + static_cast<size_t>(path[static_cast<size_t>(i)])];

    // ── Segment → name (with slash bass, #12) + expand to per-frame labels ────
    struct SegLabel { std::string name; int root; bool is_minor; double conf; };
    std::vector<SegLabel> seg_labels(static_cast<size_t>(NS));

    for (int i = 0; i < NS; ++i) {
        const Seg& sg = segs[static_cast<size_t>(i)];
        int s = path[static_cast<size_t>(i)];
        size_t base = static_cast<size_t>(i) * static_cast<size_t>(S);

        if (s == SIL) {
            seg_labels[static_cast<size_t>(i)] =
                {sg.silent ? "silence" : "unknown", -1, false, 0.0};
            continue;
        }
        const Template& tp = templates[static_cast<size_t>(s)];
        std::string name = tp.name;

        // Slash bass: dominant bass note that isn't the root, carrying real
        // mass. "G:maj/B" reads as G major over B.
        if (sg.bass_pc >= 0 && sg.bass_pc != tp.root
            && sg.bass[static_cast<size_t>(sg.bass_pc)] >= config.slash_bass_mass
            && tp.vec[static_cast<size_t>(sg.bass_pc)] > 0.0
            && tp.name.find(":5") == std::string::npos) {
            name += std::string("/") + NOTE_NAMES[sg.bass_pc];
        }

        // Confidence = margin between the chosen chord's fit and the best
        // *other-root* fit in this segment (quality-mistakes are cheap; root
        // mistakes are what the margin should reflect).
        double best_other = -BIG;
        for (int t = 0; t < T; ++t)
            if (templates[static_cast<size_t>(t)].root != tp.root)
                best_other = std::max(best_other, fit_score[base + static_cast<size_t>(t)]);
        double conf = std::clamp((fit_score[base + static_cast<size_t>(s)] - best_other) * 2.0
                                 + 0.5, 0.0, 1.0);

        seg_labels[static_cast<size_t>(i)] = {name, tp.root, tp.is_minor, conf};
    }

    labels.reserve(static_cast<size_t>(F));
    int si = 0;
    for (int i = 0; i < F; ++i) {
        double t = chroma.frames[static_cast<size_t>(i)].time_sec;
        while (si + 1 < NS && t >= segs[static_cast<size_t>(si)].t1) ++si;
        const SegLabel& sl = seg_labels[static_cast<size_t>(si)];
        labels.push_back(ChordLabel{t, sl.name, sl.root, sl.is_minor, sl.conf});
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
