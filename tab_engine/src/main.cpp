#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <map>
#include <fstream>

#include "nlohmann/json.hpp"
#include "note_event.hpp"
#include "guitar.hpp"
#include "fret_graph.hpp"
#include "note_eliminator.hpp"
#include "chroma_analyzer.hpp"
#include "chord_classifier.hpp"
#include "fingering_solver.hpp"

using json = nlohmann::json;

std::vector<NoteEvent> load_notes_from_json(const std::string& path) {
    std::ifstream file(path);
    if(!file.is_open()) throw std::runtime_error("Cannot open notes file: " + path);

    json j;
    file >> j;

    std::vector<NoteEvent> notes;
    notes.reserve(j.size());

    for(const auto& item: j){
        notes.push_back(NoteEvent{
            .start_time = item["start_time"].get<double>(),
            .end_time = item["end_time"].get<double>(),
            .pitch = item["pitch"].get<int>(),
            .velocity = item["velocity"].get<double>()
        });
    }

    return notes;
}

int main(int argc, char* argv[]){
    if(argc < 2){
        std::cerr << "Usage: tab_engine <notes.json>\n";
        return 1;
    }

    const std::string notes_path = argv[1];

    std::cout << "Loading notes from: " << notes_path << "\n";
    std::vector<NoteEvent> notes;

    try {
        notes = load_notes_from_json(notes_path);
    }
    catch(const std::exception& e){
        std::cerr << "Error: " << e.what() << "\n";
        return 1;
    }

    std::cout << "Loaded " << notes.size() << " note events.\n\n";

    std::cout << "Building fret candidate graph....\n";
    CandidateGraph graph = build_candidate_graph(notes);

    int total_candidates = 0;
    int out_of_range = 0;
    for(const auto& candidates : graph){
        if(candidates.empty()) ++out_of_range;
        total_candidates += static_cast<int>(candidates.size());
    }

    std::cout << "Graph built:\n";
    std::cout << "  Notes:            " << notes.size()       << "\n";
    std::cout << "  Out-of-range:     " << out_of_range       << "\n";
    std::cout << "  Total candidates: " << total_candidates   << "\n";
    std::cout << "  Avg per note:     "
              << (notes.empty() ? 0.0
                  : static_cast<double>(total_candidates) / notes.size())
              << "\n\n";

    // ── Group into timesteps ──────────────────────────────────────────────────
    std::cout << "Grouping notes into timesteps...\n";
    std::vector<Timestep> timesteps = group_into_timesteps(notes);
    std::cout << "  Timesteps: " << timesteps.size() << "\n\n";

    // ── Run note elimination ──────────────────────────────────────────────────
    std::cout << "Running note elimination (ILP)...\n";
    std::vector<int> surviving = run_elimination(timesteps, notes, graph);

    if (argc >= 3) {
        const std::string wav_path = argv[2];

        std::cout << "\nRunning chroma analysis...\n";
        ChromaResult chroma = compute_chroma(wav_path);

        std::cout << "\nRunning chord classification...\n";
        ClassifierConfig cfg;
        cfg.silence_threshold = 0.02;   // gate at 2% of peak RMS
        cfg.smooth_radius     = 6;      // 9-frame median window ≈ 370ms

        std::vector<ChordLabel> labels = classify_chords(chroma, notes, surviving, cfg);
        double total_duration = chroma.frames.back().time_sec + static_cast<double>(chroma.frame_size) / chroma.sample_rate;
        std::vector<ChordSegment> segments = collapse_to_segments(labels, total_duration);

        print_chord_segments(segments, 50);

        // ── Fingering Solver ──────────────────────────────────────────────────────
        std::cout << "\nRunning Minimax Viterbi fingering solver...\n";
        std::vector<FingeringChoice> choices = solve_fingering(
            surviving, notes, graph, labels, chroma);

        print_fingering(choices, notes, 30);

        // Summary: count how many frames hit each chord
        std::cout << "\nChord distribution (top 8):\n";
        std::map<std::string, int> counts;
        for (const auto& lbl : labels) ++counts[lbl.name];

        // Sort by frequency descending
        std::vector<std::pair<std::string,int>> sorted_counts(counts.begin(), counts.end());
        std::sort(sorted_counts.begin(), sorted_counts.end(),
            [](const auto& a, const auto& b){ return a.second > b.second; });

        int shown = 0;
        for (const auto& [name, count] : sorted_counts) {
            double pct = 100.0 * count / static_cast<int>(labels.size());
            std::cout << "  " << std::setw(10) << name
                    << ": " << std::setw(4) << count << " frames ("
                    << std::fixed << std::setprecision(1) << pct << "%)\n";
            if (++shown >= 8) break;
        }
    } else {
        std::cout << "\n[chroma] No WAV path provided.\n";
        std::cout << "Usage: tab_engine <notes.json> <guitar_stem.wav>\n";
    }

    // ── Print first 20 surviving notes ───────────────────────────────────────
    std::cout << "\nFirst 20 surviving notes:\n";
    std::cout << std::string(70, '-') << "\n";

    int print_count = std::min(20, static_cast<int>(surviving.size()));
    std::vector<NoteEvent>  notes_view;
    CandidateGraph          graph_view;

    for (int i = 0; i < print_count; ++i) {
        notes_view.push_back(notes[surviving[i]]);
        graph_view.push_back(graph[surviving[i]]);
    }

    print_candidate_graph(graph_view, notes_view);

    return 0;
}