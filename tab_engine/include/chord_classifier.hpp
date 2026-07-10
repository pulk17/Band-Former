#pragma once

#include "chroma_analyzer.hpp"
#include "note_event.hpp"

#include <array>
#include <string>
#include <vector>

struct ChordLabel {
    double      time_sec;
    std::string name;
    int         root;        // 0=C … 11=B, -1 for silence
    bool        is_minor;
    double      confidence;  // decode margin, 0..1 (higher = more certain)
};

struct ClassifierConfig {
    // RMS gate as a fraction of the 95th-percentile frame energy (percentile,
    // not peak, so one loud transient can't silence a quiet intro).
    double silence_threshold = 0.02;

    // ── Beat-synchronous Viterbi decoding ─────────────────────────────────────
    // Chroma is pooled between consecutive beats and the Viterbi runs over
    // beat-segments, so chords can only change on beats. Emissions are a
    // chordino-style fit (hit − off-chord − missing-tone) in roughly
    // [-0.3, 0.6].
    double transition_penalty = 0.15;   // cost to switch chords at a beat
    double complexity_penalty = 0.15;   // per chord tone beyond a triad —
                                        // must exceed the mass a stray 4th
                                        // tone can capture (~1.6 × gate τ),
                                        // or every triad becomes a maj7
    double bass_bonus         = 0.8;    // × bass-chroma mass at the template
                                        // root (continuous root anchoring)
    double no_chord_floor     = -0.5;   // only true silence beats a chord: a
                                        // low-confidence name is better UX
                                        // than an "unknown" hole mid-song
    double key_penalty        = 0.03;   // per chord tone outside the local key
    double thirdless_penalty  = 0.05;   // prior against 5/sus chords (no third):
                                        // when a third is audible the triad
                                        // should win; these only fire when the
                                        // third is genuinely absent
    double slash_bass_mass    = 0.35;   // min L1 mass for a slash-bass label
    int    key_window_segs    = 16;     // local key window length (segments)
    // Scoring internals (see tuning.json):
    double gate_tau           = 0.09;   // color tone must exceed this mass
    double miss_weight        = 0.6;    // penalty weight for off-chord mass
    double absent_weight      = 1.5;    // penalty weight for silent chord tones
    double absent_tau         = 0.08;   // "present" threshold for chord tones

    // Retained for source compatibility; unused.
    int    smooth_radius      = 4;
    double min_confidence     = 0.0;
};

// Classify chords beat-synchronously. `beats` may be empty (falls back to a
// fixed 0.5 s grid). Returns one label per chroma FRAME (segment labels are
// expanded) so downstream frame-indexed consumers keep working. If out_key is
// non-null it receives the global key name (e.g. "E major").
std::vector<ChordLabel> classify_chords(const ChromaResult& chroma,
                                        const std::vector<NoteEvent>& notes,
                                        const std::vector<int>& surviving,
                                        const std::vector<double>& beats,
                                        const ClassifierConfig config = {},
                                        std::string* out_key = nullptr);

struct ChordSegment {
    double      start_sec;
    double      end_sec;
    std::string name;
    double      avg_confidence;
};
std::vector<ChordSegment> collapse_to_segments(const std::vector<ChordLabel>& labels, double total_duration_sec);

void print_chord_segments(const std::vector<ChordSegment>& segments, int max = 40);
