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
    double      confidence;  // cosine similarity score [0, 1]
};

struct ClassifierConfig {
    double silence_threshold = 0.02;
    int    smooth_radius     = 4;
    double min_confidence    = 0.65; 
};

std::vector<ChordLabel> classify_chords(const ChromaResult& chroma, const std::vector<NoteEvent>& notes, const std::vector<int>& surviving, const ClassifierConfig config = {});

struct ChordSegment {
    double      start_sec;
    double      end_sec;
    std::string name;
    double      avg_confidence;
};
std::vector<ChordSegment> collapse_to_segments(const std::vector<ChordLabel>& labels, double total_duration_sec);

void print_chord_segments(const std::vector<ChordSegment>& segments, int max = 40);