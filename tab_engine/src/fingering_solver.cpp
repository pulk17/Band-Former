#include "fingering_solver.hpp"
#include "note_event.hpp"
#include "chroma_analyzer.hpp"
#include "chord_classifier.hpp"

#include <algorithm>
#include <climits>
#include <cmath>
#include <iostream>
#include <iomanip>
#include <vector>

static constexpr int MAX_CONTEXT_BONUS = 3;

static int chord_context_bonus(
    const FretPosition&            from,
    const FretPosition&            to,
    int                            note_i,
    int                            note_j,
    const std::vector<NoteEvent>&  notes,
    const std::vector<ChordLabel>& chord_labels,
    const ChromaResult&            chroma)
{
    double hop_sec = static_cast<double> (chroma.hop_size) / chroma.sample_rate;
    double t_i = notes[static_cast<size_t>(note_i)].start_time;
    double t_j = notes[static_cast<size_t>(note_j)].start_time;

    int frame_i = static_cast<int> (t_i / hop_sec);
    int frame_j = static_cast<int> (t_j / hop_sec);

    frame_i = std::clamp(frame_i, 0, static_cast<int>(chord_labels.size()) - 1);
    frame_j = std::clamp(frame_j, 0, static_cast<int>(chord_labels.size()) - 1);

    if(chord_labels[static_cast<size_t> (frame_i)].name != chord_labels[static_cast<size_t> (frame_j)].name) return 0;

    int fret_distance = std::abs(from.fret - to.fret);
    if(fret_distance <= 3) return MAX_CONTEXT_BONUS;
    if(fret_distance <= 5) return MAX_CONTEXT_BONUS / 2;
    return 0;
}

static int bio_cost(const FretPosition& from, const FretPosition& to){
    if(to.fret == 0) return 0;

    if(from.fret == 0) return std::abs(to.fret - 1) + std::abs(to.string_idx - from.string_idx);

    int fret_dist = std::abs(to.fret - from.fret);
    int string_dist = std::abs(to.string_idx - from.string_idx);

    int cost = (fret_dist * 2) + (string_dist * 1);

    if(fret_dist > 4) cost += (fret_dist - 4) * 3;

    return cost;
}


std::vector<FingeringChoice> solve_fingering(
    const std::vector<int>&        surviving,
    const std::vector<NoteEvent>&  notes,
    const CandidateGraph&          graph,
    const std::vector<ChordLabel>& chord_labels,
    const ChromaResult&            chroma)
{
    const int N = static_cast<int> (surviving.size());

    if(N == 0) return {};

    std::vector<std::vector<FretPosition>> candidates(static_cast<size_t> (N));
    for(int i = 0; i < N; ++i){
        int note_idx = surviving[static_cast<size_t> (i)];
        for(const auto& node : graph[static_cast<size_t> (note_idx)])
            candidates[static_cast<size_t>(i)].push_back(node.position);

        if(candidates[static_cast<size_t> (i)].empty())
            candidates[static_cast<size_t> (i)].push_back(FretPosition{0,0});
    }

    int max_cands = 0;
    for(int i = 0; i < N; ++i) max_cands = std::max(max_cands, static_cast<int>(candidates[i].size()));

    std::vector<int> back(static_cast<size_t>(N) * static_cast<size_t>(max_cands), -1);

    std::vector<int> prev_dp(static_cast<size_t>(max_cands), 0);
    std::vector<int> curr_dp(static_cast<size_t>(max_cands), INT_MAX);

    int n0_cands = static_cast<int>(candidates[0].size());
    for(int s = 0; s < n0_cands; ++s) prev_dp[static_cast<size_t> (s)] = 0;

    for (int i = 1; i < N; ++i) {
        int ni_cands   = static_cast<int>(candidates[static_cast<size_t>(i)].size());
        int prev_cands = static_cast<int>(candidates[static_cast<size_t>(i-1)].size());

        // Reset curr_dp for this note
        for (int s = 0; s < max_cands; ++s)
            curr_dp[static_cast<size_t>(s)] = INT_MAX;

        int note_i_idx = surviving[static_cast<size_t> (i)];
        int note_prev_idx = surviving[static_cast<size_t> (i - 1)];
        
        for(int s = 0; s < ni_cands; ++s){
            const FretPosition& to = candidates[static_cast<size_t>(i)][static_cast<size_t>(s)];

            int best_minimax = INT_MAX;
            int best_pred = 0;

            for(int p = 0; p < prev_cands; ++p){
                if(prev_dp[static_cast<size_t> (p)] == INT_MAX) continue;

                const FretPosition& from = candidates[static_cast<size_t> (i-1)][static_cast<size_t>(p)];

                int raw_cost = bio_cost(from, to);

                int bonus = chord_context_bonus(from, to, note_prev_idx, note_i_idx, notes, chord_labels, chroma);
                int edge_cost = std::max(0, raw_cost - bonus);

                int path_worst = std::max(prev_dp[static_cast<size_t> (p)], edge_cost);

                if(path_worst < best_minimax) {
                    best_minimax = path_worst;
                    best_pred = p;
                }
            }

            curr_dp[static_cast<size_t>(s)] = best_minimax;
            back[static_cast<size_t>(i) * static_cast<size_t>(max_cands) + static_cast<size_t>(s)] = best_pred;
        }
        std::swap(prev_dp, curr_dp);
    }

    int last_cands = static_cast<int> (candidates[static_cast<size_t> (N-1)].size());
    int best_final_cost = INT_MAX;
    int best_final_state = 0;

    for(int s = 0; s < last_cands; ++s){
        if(prev_dp[static_cast<size_t>(s)] < best_final_cost){
            best_final_cost = prev_dp[static_cast<size_t> (s)];
            best_final_state = s;
        }
    }

    std::vector<int> chosen_states(static_cast<size_t>(N));
    chosen_states[static_cast<size_t>(N-1)] = best_final_state;

    for (int i = N - 1; i > 0; --i) {
        int s    = chosen_states[static_cast<size_t>(i)];
        int pred = back[static_cast<size_t>(i) * static_cast<size_t>(max_cands)
                        + static_cast<size_t>(s)];
        chosen_states[static_cast<size_t>(i-1)] = pred;
    }

    std::vector<FingeringChoice> result;
    result.reserve(static_cast<size_t>(N));

    result.push_back(FingeringChoice{
        surviving[0],
        candidates[0][static_cast<size_t>(chosen_states[0])],
        0   // first note has no predecessor — cost = 0
    });

    for (int i = 1; i < N; ++i) {
        const FretPosition& from = candidates[static_cast<size_t>(i-1)][static_cast<size_t>(chosen_states[i-1])];
        const FretPosition& to   = candidates[static_cast<size_t>(i  )][static_cast<size_t>(chosen_states[i  ])];

        int note_prev_idx = surviving[static_cast<size_t>(i-1)];
        int note_i_idx    = surviving[static_cast<size_t>(i)];

        int raw_cost = bio_cost(from, to);
        int bonus    = chord_context_bonus(from, to,
                           note_prev_idx, note_i_idx,
                           notes, chord_labels, chroma);
        int edge_cost = std::max(0, raw_cost - bonus);

        // worst_cost = running maximum (for display purposes)
        int running_worst = std::max(result.back().worst_cost, edge_cost);

        result.push_back(FingeringChoice{
            surviving[static_cast<size_t>(i)],
            to,
            running_worst
        });
    }

    std::cout << "Fingering solver complete:\n";
    std::cout << "  Notes solved:  " << N << "\n";
    std::cout << "  Worst single transition cost: " << best_final_cost << "\n";

    return result;
}

void print_fingering(
    const std::vector<FingeringChoice>& choices,
    const std::vector<NoteEvent>&       notes,
    int max)
{
    static const char* NOTE_NAMES[] = {
        "C","C#","D","D#","E","F","F#","G","G#","A","A#","B"
    };

    std::cout << "\nFingering Results (first " << max << " notes):\n";
    std::cout << std::string(72, '-') << "\n";
    std::cout << std::setw(5)  << "#"
              << std::setw(8)  << "t(s)"
              << std::setw(6)  << "Note"
              << std::setw(8)  << "Choice"
              << std::setw(7)  << "String"
              << std::setw(6)  << "Fret"
              << std::setw(10) << "TranCost"
              << "\n"
              << std::string(72, '-') << "\n";

    int limit = std::min(max, static_cast<int>(choices.size()));
    for (int i = 0; i < limit; ++i) {
        const auto& ch   = choices[static_cast<size_t>(i)];
        const auto& note = notes[static_cast<size_t>(ch.note_idx)];

        int octave    = (note.pitch / 12) - 1;
        const char* name = NOTE_NAMES[note.pitch % 12];

        std::cout << std::fixed << std::setprecision(2)
                  << std::setw(5)  << i
                  << std::setw(8)  << note.start_time
                  << std::setw(5)  << name << octave
                  << std::setw(8)  << ch.position.label()
                  << std::setw(7)  << (ch.position.string_idx + 1)
                  << std::setw(6)  << ch.position.fret
                  << std::setw(10) << ch.worst_cost
                  << "\n";
    }
}