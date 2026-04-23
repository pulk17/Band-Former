cmake_minimum_required(VERSION 3.16)

project(tab_engine VERSION 0.1 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -O2 -Wall -Wextra -g")

include_directories(include)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFTW REQUIRED fftw3)
pkg_check_modules(SNDFILE REQUIRED sndfile)

include_directories(${FFTW_INCLUDE_DIRS} ${SNDFILE_INCLUDE_DIRS})

add_executable(tab_engine
    src/main.cpp
    src/guitar.cpp
    src/fret_graph.cpp
    src/note_eliminator.cpp
    src/chroma_analyzer.cpp
    src/chord_classifier.cpp
    src/fingering_solver.cpp
)

target_link_libraries(tab_engine
    ${FFTW_LIBRARIES}
    ${SNDFILE_LIBRARIES}
)