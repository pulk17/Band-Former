#pragma once

#include "guitar.hpp"
#include "note_event.hpp"
#include "fret_graph.hpp"
#include "chord_classifier.hpp"

#include <vector>
#include <limits>

struct FingeringChoice {
    int          note_idx;
    FretPosition position;
    int          worst_cost;
};


std::vector<FingeringChoice> solve_fingering(
    const std::vector<int>&        surviving,  
    const std::vector<NoteEvent>&  notes,
    const CandidateGraph&          graph,
    const std::vector<ChordLabel>& chord_labels, 
    const ChromaResult&            chroma 
);

void print_fingering(const std::vector<FingeringChoice>& choices, const std::vector<NoteEvent>& notes, int max = 30);