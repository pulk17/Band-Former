#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <cmath>
#include <iomanip>
#include <stdexcept>
#include <utility>

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

// Load a beats file. Accepts either a bare JSON array of times, or an object
// of the form {"beats":[...], "downbeats":[...], "bpm":...}.
static std::vector<double> load_beats_json(const std::string& path) {
    std::vector<double> beats;
    std::ifstream f(path);
    if (!f.is_open()) return beats;
    json j;
    try { f >> j; } catch (...) { return beats; }

    const json* arr = nullptr;
    if (j.is_array()) arr = &j;
    else if (j.is_object() && j.contains("beats") && j["beats"].is_array()) arr = &j["beats"];
    if (arr)
        for (const auto& v : *arr)
            if (v.is_number()) beats.push_back(v.get<double>());
    return beats;
}

static std::string parent_dir(const std::string& path) {
    size_t p = path.find_last_of("/\\");
    return (p == std::string::npos) ? std::string(".") : path.substr(0, p);
}

static std::string midi_name(int pitch) {
    static const char* N[] = {"C","C#","D","D#","E","F","F#","G","G#","A","A#","B"};
    return std::string(N[pitch % 12]) + std::to_string(pitch / 12 - 1);
}

// Snap a time to the beat grid (subdivided `subdiv` times per beat). Returns the
// quantized time and sets `beat_pos` to the fractional beat index (-1 if no grid).
static double quantize_to_beats(double t, const std::vector<double>& beats,
                                int subdiv, double& beat_pos) {
    beat_pos = -1.0;
    if (beats.size() < 2) return t;

    int i;
    if (t <= beats.front())      i = 0;
    else if (t >= beats.back())  i = static_cast<int>(beats.size()) - 2;
    else {
        int lo = 0, hi = static_cast<int>(beats.size()) - 1;
        while (lo + 1 < hi) { int mid = (lo + hi) / 2; if (beats[static_cast<size_t>(mid)] <= t) lo = mid; else hi = mid; }
        i = lo;
    }

    double dur  = beats[static_cast<size_t>(i + 1)] - beats[static_cast<size_t>(i)];
    double frac = dur > 1e-9 ? (t - beats[static_cast<size_t>(i)]) / dur : 0.0;
    double q_beat = std::round((i + frac) * subdiv) / subdiv;
    beat_pos = q_beat;

    int    qi = static_cast<int>(std::floor(q_beat));
    double qf = q_beat - qi;
    if (qi < 0) return beats.front();
    if (qi >= static_cast<int>(beats.size()) - 1) {
        double avg = (beats.back() - beats.front()) / (beats.size() - 1);
        return beats.back() + (q_beat - (beats.size() - 1)) * avg;
    }
    return beats[static_cast<size_t>(qi)] + qf * (beats[static_cast<size_t>(qi + 1)] - beats[static_cast<size_t>(qi)]);
}

static void write_tab_json(const std::string& out_path,
                           const std::vector<FingeringChoice>& choices,
                           const std::vector<NoteEvent>& notes,
                           const std::vector<ChordSegment>& segments,
                           const std::string& key,
                           const std::vector<double>& beats,
                           double bpm,
                           double duration_sec) {
    json j;
    j["metadata"] = {
        {"key", key},
        {"bpm", bpm},
        {"duration_sec", duration_sec},
        {"num_notes", choices.size()},
        // tuning indexed by string number 1..6 (high E first, low E last)
        {"tuning", {"E4","B3","G3","D3","A2","E2"}},
        {"quantize_subdiv", 4}
    };

    if (!beats.empty()) j["beats"] = beats;

    json jchords = json::array();
    for (const auto& seg : segments) {
        if (seg.name == "silence" || seg.name == "unknown") continue;
        jchords.push_back({
            {"start", seg.start_sec}, {"end", seg.end_sec},
            {"name", seg.name}, {"confidence", seg.avg_confidence}
        });
    }
    j["chords"] = jchords;

    json jnotes = json::array();
    for (const auto& ch : choices) {
        const NoteEvent& n = notes[static_cast<size_t>(ch.note_idx)];
        double beat_pos;
        double start_q = quantize_to_beats(n.start_time, beats, 4, beat_pos);
        jnotes.push_back({
            {"start", n.start_time},
            {"start_q", start_q},
            {"beat", beat_pos},
            {"duration", n.end_time - n.start_time},
            {"string", ch.position.string_idx + 1},   // 1 = high E … 6 = low E
            {"fret", ch.position.fret},
            {"pitch", n.pitch},
            {"name", midi_name(n.pitch)}
        });
    }
    j["notes"] = jnotes;

    std::ofstream out(out_path);
    if (!out.is_open()) {
        std::cerr << "  ! Could not write tab.json to " << out_path << "\n";
        return;
    }
    out << j.dump(2);
    std::cout << "\n  Tab JSON written: " << out_path
              << "  (" << choices.size() << " notes, " << jchords.size() << " chords)\n";
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

    // Optional beats file (argv[3]) for beat-grid quantization in tab.json.
    std::vector<double> beats;
    double bpm = 0.0;
    if (argc >= 4) {
        beats = load_beats_json(argv[3]);
        if (beats.size() >= 2) {
            bpm = std::round(600.0 * (beats.size() - 1) / (beats.back() - beats.front())) / 10.0;
            std::cout << "Loaded " << beats.size() << " beats (≈ " << bpm << " BPM).\n\n";
        }
    }

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
        // Chord stability is handled by the Viterbi transition penalty
        // (cfg.transition_penalty); see ClassifierConfig for tuning knobs.

        std::string detected_key;
        std::vector<ChordLabel> labels = classify_chords(chroma, notes, surviving, cfg, &detected_key);
        // Guard against an empty analysis (audio shorter than the FFT window or
        // unreadable) — chroma.frames.back() would be undefined behavior.
        double total_duration = chroma.frames.empty()
            ? 0.0
            : chroma.frames.back().time_sec
                  + static_cast<double>(chroma.frame_size) / chroma.sample_rate;
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

        // ── Serialize the playable tab ────────────────────────────────────────────
        std::string tab_path = parent_dir(notes_path) + "/tab.json";
        write_tab_json(tab_path, choices, notes, segments, detected_key,
                       beats, bpm, total_duration);
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