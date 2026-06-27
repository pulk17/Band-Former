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
    double      confidence;  // weighted template match score (higher = better)
};

struct ClassifierConfig {
    double silence_threshold = 0.02;  // RMS gate as a fraction of peak RMS

    // ── Viterbi chord smoothing ───────────────────────────────────────────────
    // classify_chords() decodes the chord sequence with a Viterbi pass that
    // trades per-frame fit against stability, replacing the old argmax+median
    // approach (which flickered between chords every few frames).
    double transition_penalty = 0.30;  // cost to switch chords between frames
    double complexity_penalty = 0.10;  // bias against 4-note chords (favor triads)
    double bass_bonus         = 0.08;  // bonus when a template root == the bass note
    double no_chord_floor     = 0.30;  // min fit to prefer a chord over "no chord"
    double key_penalty        = 0.10;  // penalty per chord tone outside the detected key

    // Retained for source compatibility; unused by the Viterbi path.
    int    smooth_radius      = 4;
    double min_confidence     = 0.0;
};

// If out_key is non-null it receives the detected key name (e.g. "E major").
std::vector<ChordLabel> classify_chords(const ChromaResult& chroma, const std::vector<NoteEvent>& notes, const std::vector<int>& surviving, const ClassifierConfig config = {}, std::string* out_key = nullptr);

struct ChordSegment {
    double      start_sec;
    double      end_sec;
    std::string name;
    double      avg_confidence;
};
std::vector<ChordSegment> collapse_to_segments(const std::vector<ChordLabel>& labels, double total_duration_sec);

void print_chord_segments(const std::vector<ChordSegment>& segments, int max = 40);