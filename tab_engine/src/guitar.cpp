#include "guitar.hpp"
#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <climits>

std::vector<FretPosition> positions_for_pitch(int midi_pitch){
    std::vector<FretPosition> candidates;

    if(midi_pitch < GUITAR_MIDI_MIN || midi_pitch > GUITAR_MIDI_MAX) return candidates;

    for(int s = 0; s < NUM_STRINGS; ++s){
        int fret = midi_pitch - OPEN_STRING_MIDI[s];
        
        if(fret < 0 || fret > NUM_FRETS) continue;

        candidates.push_back(FretPosition{s, fret});
    }

    std::sort(candidates.begin(), candidates.end(), [](const FretPosition& a, const FretPosition& b) {
        return a.fret < b.fret;
    });

    return candidates;
}

bool is_playable_chord(const std::vector<FretPosition>& positions){
    if(positions.empty()) return true;

    bool string_used[NUM_STRINGS] = {false};
    for(const auto& pos : positions){
        if(string_used[pos.string_idx]) return false;
        string_used[pos.string_idx] = true;
    }

    int min_fret = INT_MAX;
    int max_fret = 0;

    for(const auto& pos : positions){
        if(pos.fret == 0) continue;
        min_fret = std::min(min_fret, pos.fret);
        max_fret = std:: max(max_fret, pos.fret);
    }

    if(min_fret == INT_MAX) return true;

    return (max_fret - min_fret) <= MAX_FRET_SPAN;
}