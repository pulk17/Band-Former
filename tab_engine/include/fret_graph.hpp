#pragma once

#include "guitar.hpp"
#include "note_event.hpp"
#include <vector>

struct CandidateNode {
    int note_idx;
    FretPosition position;
    int midi_pitch;
};

using CandidateGraph = std::vector<std::vector<CandidateNode>>;

CandidateGraph build_candidate_graph(const std::vector<NoteEvent>& notes);

void print_candidate_graph(const CandidateGraph& graph, const std::vector<NoteEvent>& notes);
