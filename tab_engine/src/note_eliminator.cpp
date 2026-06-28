#include "note_eliminator.hpp"
#include "note_event.hpp"
#include "guitar.hpp"
#include <algorithm>
#include <climits>
#include <iostream>
#include <utility>
#include <vector>
#include <cmath>

std::vector<Timestep> group_into_timesteps(const std::vector<NoteEvent>& notes, double tolerance_sec){
    std::vector<Timestep> timesteps;
    if(notes.empty()) return timesteps;

    Timestep current;
    current.onset_time = notes[0].start_time;

    for(int i = 0; i < static_cast<int>(notes.size()); ++i){
        double gap = notes[i].start_time - current.onset_time;

        if(gap > tolerance_sec){
            timesteps.push_back(std::move(current));
            current.onset_time = notes[i].start_time;
            current.note_indices.clear();
        }
        current.note_indices.push_back(i);
    }
    timesteps.push_back(std::move(current));

    return timesteps;
}


int harmonic_weight(const std::vector<int>& pitches, int target_pitch){
    if(pitches.empty()) return W_REDUNDANT;

    int bass_pitch = *std::min_element(pitches.begin(), pitches.end());
    int melody_pitch = *std::max_element(pitches.begin(), pitches.end());

    if(target_pitch == bass_pitch) return W_BASS;
    if(target_pitch == melody_pitch) return W_MELODY;

    int interval = ((target_pitch - bass_pitch) % 12 + 12) % 12;

    // Interval meanings (semitones from root):
    //  0 = unison/octave (redundant)
    //  1 = minor second
    //  2 = major second / ninth
    //  3 = minor third (guide tone)
    //  4 = major third (guide tone)
    //  5 = perfect fourth / eleventh
    //  6 = tritone
    //  7 = perfect fifth (redundant)
    //  8 = minor sixth
    //  9 = major sixth
    // 10 = minor seventh (guide tone)
    // 11 = major seventh (guide tone)

    switch(interval) {
        // Harsh dissonance: minor second / flat ninth
        case 1: return W_DISSONANT;
        
        // Guide Tones: thirds and sevenths define chord quality
        case 3:   // minor third
        case 4:   // major third
        case 10:  // minor seventh
        case 11:  // major seventh
            return W_GUIDE;
        
        // Extension tones: ninths and elevenths add color
        case 2:   // major second / ninth
        case 5:   // perfect fourth / eleventh
        case 6:   // tritone / #eleventh
        case 8:   // minor sixth / thirteenth
        case 9:   // major sixth / thirteenth
            return W_EXTENSION;

        // Redundant: fifths and octaves/unisons
        case 0:   // octave or unison
        case 7:   // perfect fifth
            return W_REDUNDANT;

        default:
            return W_EXTENSION;
    }
}

// Can these notes sound together on the guitar? Try every hand position: each
// note must have a candidate that is an open string, or within a MAX_FRET_SPAN
// window above some base fret, on a distinct string. This admits barre chords
// (e.g. A major at the 5th fret) — unlike the old test that judged playability
// from each note's lowest fret only and wrongly deleted valid barre voicings.
static bool playable_subset(const std::vector<int>& subset, const CandidateGraph& graph){
    if(subset.empty()) return true;
    if(static_cast<int>(subset.size()) > NUM_STRINGS) return false;
    for(int base = 0; base <= NUM_FRETS; ++base){
        bool used[NUM_STRINGS] = {false};
        bool ok = true;
        for(int idx : subset){
            bool placed = false;
            for(const auto& node : graph[static_cast<size_t>(idx)]){
                int f = node.position.fret, s = node.position.string_idx;
                if(s < 0 || s >= NUM_STRINGS || used[s]) continue;
                if(f == 0 || (f >= base && f <= base + MAX_FRET_SPAN)){
                    used[s] = true; placed = true; break;
                }
            }
            if(!placed){ ok = false; break; }
        }
        if(ok) return true;
    }
    return false;
}

EliminationResult eliminate_notes(const std::vector<int>& note_indices, const std::vector<NoteEvent>& notes, const CandidateGraph& graph){
    int n = static_cast<int>(note_indices.size());

    // Guard: cap brute-force enumeration at 18 notes (262k subsets).
    // For larger timesteps, keep only the top-weighted notes.
    constexpr int MAX_BRUTE_FORCE = 18;
    if (n > MAX_BRUTE_FORCE) {
        std::vector<int> pitches_full;
        for (int idx : note_indices) pitches_full.push_back(notes[idx].pitch);

        std::vector<std::pair<int,int>> scored;
        for (int i = 0; i < n; ++i) {
            int w = harmonic_weight(pitches_full, notes[note_indices[i]].pitch);
            scored.push_back({w, i});
        }
        std::sort(scored.begin(), scored.end(), [](const auto& a, const auto& b){
            return a.first > b.first;
        });

        std::vector<int> trimmed_indices;
        for (int i = 0; i < MAX_BRUTE_FORCE && i < static_cast<int>(scored.size()); ++i)
            trimmed_indices.push_back(note_indices[scored[i].second]);

        std::sort(trimmed_indices.begin(), trimmed_indices.end());
        return eliminate_notes(trimmed_indices, notes, graph);
    }

    std::vector<int> pitches;
    pitches.reserve(n);
    for(int idx : note_indices) pitches.push_back(notes[idx].pitch);

    int best_weight = -1;
    int best_mask = 0;
    int total_subsets = 1 << n;

    for(int mask = 0; mask < total_subsets; ++mask){
        std::vector<int> subset;          // playable note indices in this subset
        std::vector<int> subset_pitches;
        for(int i = 0; i < n; ++i){
            if((mask & (1 << i)) && !graph[note_indices[i]].empty()){
                subset.push_back(note_indices[i]);
                subset_pitches.push_back(pitches[i]);
            }
        }
        // A note can use at most one string, so >6 simultaneous is impossible.
        if(static_cast<int>(subset.size()) > NUM_STRINGS) continue;
        if(!playable_subset(subset, graph)) continue;

        int weight_sum = 0;
        for(int p : subset_pitches) weight_sum += harmonic_weight(subset_pitches, p);

        if(weight_sum > best_weight){
            best_mask = mask;
            best_weight = weight_sum;
        }
    }

    EliminationResult result;
    result.total_weight = best_weight;
    result.dropped_count = 0;

    for(int i = 0; i < n; ++i){
        int node_idx = note_indices[i];

        if(graph[node_idx].empty()){
            ++result.dropped_count;
            continue;
        }

        if(best_mask & (1 << i)) result.kept_indices.push_back(node_idx);
        else ++result.dropped_count;
    }

    return result;
}

std::vector<int> run_elimination(const std::vector<Timestep>& timesteps, const std::vector<NoteEvent>& notes, const CandidateGraph& graph){
    std::vector<int> surviving_notes;
    int total_dropped = 0;

    for(const auto& ts : timesteps){
        EliminationResult result = eliminate_notes(ts.note_indices, notes, graph);

        for(int idx : result.kept_indices) surviving_notes.push_back(idx);
        total_dropped += result.dropped_count;
    }

    std::sort(surviving_notes.begin(), surviving_notes.end());

    std::cout << "Elimination complete:\n";
    std::cout << "  Input notes:     " << notes.size()        << "\n";
    std::cout << "  Surviving notes: " << surviving_notes.size() << "\n";
    std::cout << "  Dropped:         " << total_dropped        << "\n";

    return surviving_notes;
}