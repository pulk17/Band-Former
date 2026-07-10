#pragma once

#include "note_event.hpp"
#include "guitar.hpp"
#include "fret_graph.hpp"
#include <vector>
#include <unordered_map>

inline constexpr int W_BASS      = 10;  // lowest pitch in the chord
inline constexpr int W_MELODY    = 9;   // highest pitch in the chord
inline constexpr int W_GUIDE     = 7;   // thirds and sevenths
inline constexpr int W_EXTENSION = 5;   // ninths, elevenths (color tones)
inline constexpr int W_REDUNDANT = 2;   // perfect fifths, octaves
inline constexpr int W_DISSONANT = 0;
inline constexpr int MAX_FINGERS = 4;


struct Timestep {
    double onset_time;
    std::vector<int> note_indices; 
};

struct EliminationResult {
    std::vector<int> kept_indices;
    int dropped_count;
    int total_weight; 
};

std::vector<Timestep> group_into_timesteps(const std::vector<NoteEvent>& notes, double tolerance_sec = 0.05);

int harmonic_weight(const std::vector<int>& pitches, int target_pitch);

EliminationResult eliminate_notes(const std::vector<int>& note_indices, const std::vector<NoteEvent>& notes, const CandidateGraph& graph);

std::vector<int> run_elimination(const std::vector<Timestep>& timesteps, const std::vector<NoteEvent>& notes, const CandidateGraph& graph);