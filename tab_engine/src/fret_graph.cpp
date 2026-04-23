#include "fret_graph.hpp"
#include "guitar.hpp"
#include <iostream>
#include <iomanip>
#include <vector>

CandidateGraph build_candidate_graph(const std::vector<NoteEvent>& notes){
    CandidateGraph graph;
    graph.reserve(notes.size());

    for(int i = 0; i < static_cast<int> (notes.size()); ++i){
        const NoteEvent& note = notes[i];

        std::vector<FretPosition> positions = positions_for_pitch(note.pitch);

        std::vector<CandidateNode> nodes;
        nodes.reserve(positions.size());

        for(const auto& pos : positions) {
            nodes.push_back(CandidateNode{
                .note_idx = i,
                .position = pos,
                .midi_pitch = note.pitch
            });
        }

        graph.push_back(std::move(nodes));
    }
    return graph;
}

void print_candidate_graph(const CandidateGraph& graph, const std::vector<NoteEvent>& notes){
    static const char* NOTE_NAMES[] = {"C","C#","D","D#","E","F","F#","G","G#","A","A#","B"};

    for(int i = 0; i < static_cast<int> (graph.size()); ++i){
        const auto& candidates = graph[i];
        const auto& note = notes[i];

        int octave = (note.pitch / 12) - 1;
        const char* name  = NOTE_NAMES[note.pitch % 12];

        std::cout << "Note " << std::setw(4) << i
                  << " | t=" << std::fixed << std::setprecision(2)
                  << note.start_time << "s"
                  << " | MIDI " << std::setw(3) << note.pitch
                  << " (" << name << octave << ")"
                  << " | " << candidates.size() << " candidates: ";

        for(const auto& node : candidates) std::cout << node.position.label() << " ";
        std::cout << "\n";
    }
}