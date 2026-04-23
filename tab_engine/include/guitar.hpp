#pragma once

#include <array>
#include <vector>
#include <string>

inline constexpr int NUM_STRINGS = 6;
inline constexpr int NUM_FRETS = 22;

inline constexpr std::array<int, NUM_STRINGS> OPEN_STRING_MIDI = {
    64,  // String 1: E4  
    59,  // String 2: B3
    55,  // String 3: G3
    50,  // String 4: D3
    45,  // String 5: A2
    40   // String 6: E2  
};

inline constexpr int GUITAR_MIDI_MIN = 40;   // E2  (open low E string)
inline constexpr int GUITAR_MIDI_MAX = 86;   // D6  (string 1, fret 22)

inline constexpr int MAX_FRET_SPAN = 4;

struct FretPosition {
    int string_idx;
    int fret;

    int midi_pitch() const{
        return OPEN_STRING_MIDI[string_idx] + fret;
    }

    std::string label() const{
        return "S" + std::to_string(string_idx+1)
            + "F" + std::to_string(fret);
    }

    bool operator == (const FretPosition& other) const {
        return string_idx == other.string_idx && fret == other.fret;
    }
};

std::vector<FretPosition> positions_for_pitch(int midi_pitch);

bool is_playable_chord(const std::vector<FretPosition>& positions);