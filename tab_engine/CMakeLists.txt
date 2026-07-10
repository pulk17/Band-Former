cmake_minimum_required(VERSION 3.16)

project(tab_engine VERSION 0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Default to a Release build for single-config generators (Ninja, Makefiles).
# Multi-config generators (Visual Studio) pick the config at build time.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ── Warnings & optimization (per-compiler, portable) ──────────────────────────
# The old flags (-O2 -Wall -Wextra -g) are GCC/Clang-only and break MSVC.
if(MSVC)
    add_compile_options(/W4 /permissive-)
else()
    add_compile_options(-Wall -Wextra)
endif()

# ─────────────────────────────────────────────────────────────────────────────
#  Dependencies
#
#  Strategy: prefer CMake "config" packages (what vcpkg installs on every OS).
#  Fall back to pkg-config for Linux / MSYS2 systems that provide .pc files
#  instead of CMake config files. This makes the same CMakeLists work for:
#    • Windows + vcpkg (MSVC or MinGW)
#    • macOS + vcpkg or Homebrew
#    • Linux + vcpkg or system packages (apt/pacman)
# ─────────────────────────────────────────────────────────────────────────────

# ── nlohmann_json (header-only) ───────────────────────────────────────────────
find_package(nlohmann_json CONFIG QUIET)
if(nlohmann_json_FOUND)
    set(JSON_LIB nlohmann_json::nlohmann_json)
    message(STATUS "tab_engine: using nlohmann_json from CMake config package")
else()
    # Allow a vendored single-header at include/nlohmann/json.hpp
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/include/nlohmann/json.hpp")
        set(JSON_LIB "")
        message(STATUS "tab_engine: using vendored include/nlohmann/json.hpp")
    else()
        message(FATAL_ERROR
            "nlohmann_json not found. Install it (vcpkg: nlohmann-json) "
            "or vendor the single header at include/nlohmann/json.hpp")
    endif()
endif()

# ── libsndfile ────────────────────────────────────────────────────────────────
find_package(SndFile CONFIG QUIET)
if(TARGET SndFile::sndfile)
    set(SNDFILE_LIB SndFile::sndfile)
    message(STATUS "tab_engine: using libsndfile from CMake config package")
else()
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(SNDFILE REQUIRED IMPORTED_TARGET sndfile)
    set(SNDFILE_LIB PkgConfig::SNDFILE)
    message(STATUS "tab_engine: using libsndfile from pkg-config")
endif()

# ─────────────────────────────────────────────────────────────────────────────
add_executable(tab_engine
    src/main.cpp
    src/guitar.cpp
    src/fret_graph.cpp
    src/note_eliminator.cpp
    src/chroma_analyzer.cpp
    src/chord_classifier.cpp
    src/fingering_solver.cpp
)

target_include_directories(tab_engine PRIVATE include)

# On MinGW, statically link the GCC/libstdc++/winpthread runtime so the binary
# is self-contained (it then only needs libsndfile.dll, which vcpkg deploys
# next to the exe). Avoids "missing libstdc++-6.dll" at launch.
if(MINGW)
    target_link_options(tab_engine PRIVATE -static-libgcc -static-libstdc++ -static)
endif()

target_link_libraries(tab_engine PRIVATE ${SNDFILE_LIB})
if(JSON_LIB)
    target_link_libraries(tab_engine PRIVATE ${JSON_LIB})
endif()
