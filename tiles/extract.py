"""Falling-tiles (Synthesia-style) piano video -> exact note events.

Deterministic computer vision, no ML:
  1. Keyboard detection on a median frame: white-key seams + black-key blob
     pattern anchor the x -> MIDI pitch map.
  2. Mode detection: falling tiles above the keyboard, key-highlight at the
     keys, or both.
  3. Single-line sampling trick: a tile of height h scrolling at speed v is
     present at a fixed scan line for exactly h/v seconds == the note's
     duration. So sampling ONE row above the keyboard gives onset AND duration
     without tracking. Key-highlight sampling covers videos with no tiles
     (keys grey/light up when played).
  4. Debounce (close 1-2 frame gaps, drop sub-minimum runs), hand assignment
     by tile hue (2-means), notes.json + MIDI out.

CLI:
    python -m tiles.extract <video.mp4> <out_dir> [--stride 1]

Knobs live in tuning.json under "tiles" (all optional):
    keyboard_y_override, leftmost_midi_override, sat_min, val_min,
    artifact_margin_px, min_note_ms, gap_close_ms, highlight_delta,
    white_center_frac
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from tuning import knob as _knob
except Exception:  # noqa: BLE001
    def _knob(_s, _k, d):
        return d

# Boundary-has-black pattern for white-key pairs starting at B|C:
# B|C=0, C|D=1, D|E=1, E|F=0, F|G=1, G|A=1, A|B=1
_BOUNDARY_PATTERN = [0, 1, 1, 0, 1, 1, 1]
_WHITE_LETTER_MIDI = [0, 2, 4, 5, 7, 9, 11]   # C D E F G A B (pc within octave)


def _median_frame(cap, n=31):
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    idxs = np.linspace(total * 0.15, total * 0.85, n).astype(int)
    frames = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            frames.append(f)
    if not frames:
        raise RuntimeError("Could not read frames from video")
    return np.median(np.stack(frames), axis=0).astype(np.uint8)


def detect_keyboard(frame) -> dict:
    """Locate the keyboard band and build the x -> MIDI map."""
    H, W = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Keyboard band: rows (bottom half) with a high fraction of bright pixels.
    bright_frac = (gray > 150).mean(axis=1)
    y0 = int(_knob("tiles", "keyboard_y_override", 0)) or None
    ys = np.where(bright_frac[H // 2:] > 0.4)[0] + H // 2
    if len(ys) < 8 and y0 is None:
        raise RuntimeError("No keyboard band found (no bright row region in bottom half)")
    if y0 is None:
        # longest contiguous run
        runs, start = [], ys[0]
        for a, b in zip(ys, ys[1:]):
            if b - a > 3:
                runs.append((start, a)); start = b
        runs.append((start, ys[-1]))
        kb_top, kb_bot = max(runs, key=lambda r: r[1] - r[0])
    else:
        kb_top, kb_bot = y0, H - 1
    band = kb_bot - kb_top
    if band < 20:
        raise RuntimeError(f"Keyboard band too thin ({band}px)")

    # White-key seams: dark vertical gaps near the bottom of the band.
    y_low = kb_top + int(band * 0.85)
    profile = gray[y_low - 2:y_low + 3, :].min(axis=0).astype(np.float32)
    inv = profile.max() - profile
    thr = inv.mean() + inv.std()
    cand = inv > thr
    seams = []
    x = 0
    while x < W:
        if cand[x]:
            x2 = x
            while x2 + 1 < W and cand[x2 + 1]:
                x2 += 1
            seams.append((x + x2) // 2)
            x = x2 + 1
        else:
            x += 1
    if len(seams) < 8:
        raise RuntimeError(f"Too few white-key seams found ({len(seams)})")
    widths = np.diff(seams)
    med_w = float(np.median(widths))
    # de-noise: drop seams creating slivers < 40% of median key width
    clean = [seams[0]]
    for s in seams[1:]:
        if s - clean[-1] >= 0.4 * med_w:
            clean.append(s)
    seams = clean

    # Black keys: dark blobs in the upper part of the band.
    y_blk = kb_top + int(band * 0.35)
    row = gray[y_blk - 2:y_blk + 3, :].max(axis=0)
    dark = row < 90
    blacks = []
    x = 0
    while x < W:
        if dark[x]:
            x2 = x
            while x2 + 1 < W and dark[x2 + 1]:
                x2 += 1
            w = x2 - x
            if 0.25 * med_w < w < 1.1 * med_w:
                blacks.append(((x + x2) // 2, x, x2))
            x = x2 + 1
        else:
            x += 1

    # boundary-has-black sequence for consecutive white keys
    has_black = []
    for s in seams[1:-1]:
        has_black.append(1 if any(bx0 - 3 <= s <= bx1 + 3 for _c, bx0, bx1 in blacks) else 0)

    # best rotation of the 7-pattern
    best_off, best_score = 0, -1
    for off in range(7):
        score = sum(1 for i, hb in enumerate(has_black)
                    if hb == _BOUNDARY_PATTERN[(off + i) % 7])
        if score > best_score:
            best_score, best_off = score, off
    match = best_score / max(1, len(has_black))
    # offset 0 => first boundary is B|C => leftmost white key is B
    letters = ["B", "C", "D", "E", "F", "G", "A"]
    first_letter = letters[best_off % 7]

    n_white = len(seams) - 1
    li = ["C", "D", "E", "F", "G", "A", "B"].index(first_letter)
    override = int(_knob("tiles", "leftmost_midi_override", 0))
    if override:
        first_midi = override
    else:
        # choose octave so the keyboard centre lands nearest middle C (60);
        # a full 88 (52 whites starting A) resolves to A0=21 automatically.
        best_midi, best_d = 60, 1e9
        for octave in range(0, 8):
            m = 12 * (octave + 1) + _WHITE_LETTER_MIDI[li]
            center = m + (n_white / 2) * 12 / 7
            if abs(center - 60) < best_d:
                best_d, best_midi = abs(center - 60), m
        first_midi = best_midi

    # build key list (white + black) with x ranges
    keys = []
    midi = first_midi
    wf = float(_knob("tiles", "white_center_frac", 0.5))
    for i in range(n_white):
        x0, x1 = seams[i], seams[i + 1]
        cw = (x1 - x0) * (1 - wf) / 2
        keys.append({"midi": midi, "x0": int(x0 + cw), "x1": int(x1 - cw), "black": False})
        # black key after this white? (pattern: after C,D,F,G,A)
        if (midi % 12) in (0, 2, 5, 7, 9):
            for _c, bx0, bx1 in blacks:
                if x1 - 0.7 * med_w < _c < x1 + 0.7 * med_w:
                    keys.append({"midi": midi + 1, "x0": int(bx0), "x1": int(bx1), "black": True})
                    break
        midi += 2 if (midi % 12) in (0, 2, 5, 7, 9) else 1

    return {"top": int(kb_top), "bottom": int(kb_bot), "keys": keys,
            "n_white": n_white, "first_midi": int(first_midi),
            "pattern_match": round(match, 3), "med_white_px": med_w}


def extract_notes(video_path: str | Path, stride: int = 1, progress=None) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    kb = detect_keyboard(_median_frame(cap))
    keys = kb["keys"]
    nk = len(keys)
    print(f"[tiles] keyboard: {kb['n_white']} white keys, first MIDI {kb['first_midi']} "
          f"({kb['pattern_match']:.0%} pattern match), band y {kb['top']}..{kb['bottom']}")

    sat_min = int(_knob("tiles", "sat_min", 70))
    val_min = int(_knob("tiles", "val_min", 70))
    margin = int(_knob("tiles", "artifact_margin_px", 14))
    hl_delta = float(_knob("tiles", "highlight_delta", 32))
    y_line = max(0, kb["top"] - margin)
    y_wht = kb["top"] + int((kb["bottom"] - kb["top"]) * 0.75)
    y_blk = kb["top"] + int((kb["bottom"] - kb["top"]) * 0.30)

    # per-frame, per-key: tile presence + tile hue + key patch color
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    pres, hues, patch = [], [], []
    fi = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if fi % stride:
            fi += 1
            continue
        line = frame[y_line - 1:y_line + 2, :, :]
        hsv = cv2.cvtColor(line, cv2.COLOR_BGR2HSV)
        colored = (hsv[..., 1] >= sat_min) & (hsv[..., 2] >= val_min)
        rowc = colored.mean(axis=0)          # fraction colored per x
        rowh = hsv[..., 0].astype(np.float32)
        p = np.zeros(nk, dtype=bool); h = np.full(nk, -1.0, dtype=np.float32)
        pc = np.zeros((nk, 3), dtype=np.float32)
        for i, k in enumerate(keys):
            sl = slice(k["x0"], max(k["x0"] + 1, k["x1"]))
            frac = rowc[sl].mean()
            if frac > 0.45:
                p[i] = True
                m = colored[:, sl]
                h[i] = float(rowh[:, sl][m].mean()) if m.any() else -1.0
            yy = y_blk if k["black"] else y_wht
            pc[i] = frame[yy - 1:yy + 2, sl].reshape(-1, 3).mean(axis=0)
        pres.append(p); hues.append(h); patch.append(pc)
        if progress and fi % 600 == 0:
            progress(f"scanning video {100 * fi // max(1, total)}%")
        fi += 1
    cap.release()

    pres = np.array(pres)                    # [T, nk]
    hues = np.array(hues)
    patch = np.array(patch)                  # [T, nk, 3]
    T = len(pres)
    dt = stride / fps
    tiles_mode = pres.mean() > 0.002

    # key-highlight signal: distance from per-key temporal median color
    base = np.median(patch, axis=0)          # [nk, 3]
    dist = np.linalg.norm(patch - base[None], axis=2)
    lit = dist > hl_delta
    hl_mode = lit.mean() > 0.002
    print(f"[tiles] modes: tiles={tiles_mode} highlight={hl_mode} "
          f"(frames {T}, fps {fps:.1f}, stride {stride})")

    active = pres | lit if (tiles_mode and hl_mode) else (pres if tiles_mode else lit)

    # debounce per key: close short gaps, drop short runs
    gap_n = max(1, int(round(_knob("tiles", "gap_close_ms", 50) / 1000 / dt)))
    min_n = max(1, int(round(_knob("tiles", "min_note_ms", 60) / 1000 / dt)))
    notes = []
    for i, k in enumerate(keys):
        col = active[:, i].copy()
        # close gaps
        t = 0
        while t < T:
            if not col[t]:
                t2 = t
                while t2 + 1 < T and not col[t2 + 1]:
                    t2 += 1
                if 0 < t and t2 + 1 < T and (t2 - t + 1) <= gap_n:
                    col[t:t2 + 1] = True
                t = t2 + 1
            else:
                t += 1
        # emit runs
        t = 0
        while t < T:
            if col[t]:
                t2 = t
                while t2 + 1 < T and col[t2 + 1]:
                    t2 += 1
                if (t2 - t + 1) >= min_n:
                    hv = hues[t:t2 + 1, i]
                    hv = hv[hv >= 0]
                    notes.append({"start_time": round(t * dt, 4),
                                  "end_time": round((t2 + 1) * dt, 4),
                                  "pitch": k["midi"],
                                  "velocity": 0.8,
                                  "hue": float(hv.mean()) if len(hv) else -1.0})
                t = t2 + 1
            else:
                t += 1
    notes.sort(key=lambda n: n["start_time"])

    # hand assignment: 2-means on hue (tiles mode colors hands differently)
    hv = np.array([n["hue"] for n in notes if n["hue"] >= 0], dtype=np.float32)
    if len(hv) > 10 and float(hv.std()) > 8:
        c1, c2 = float(hv.min()), float(hv.max())
        for _ in range(12):
            a1 = hv[np.abs(hv - c1) <= np.abs(hv - c2)]
            a2 = hv[np.abs(hv - c1) > np.abs(hv - c2)]
            c1, c2 = (float(a1.mean()) if len(a1) else c1,
                      float(a2.mean()) if len(a2) else c2)
        for n in notes:
            if n["hue"] >= 0:
                n["hand"] = "left" if abs(n["hue"] - min(c1, c2)) < abs(n["hue"] - max(c1, c2)) else "right"
    for n in notes:
        n.pop("hue", None)

    return {"notes": notes, "keyboard": kb, "fps": fps,
            "mode": "tiles+highlight" if (tiles_mode and hl_mode)
                    else ("tiles" if tiles_mode else "highlight")}


def validate_octave(notes: list[dict], audio_path: str | Path) -> int:
    """Cross-check the keyboard's absolute pitch against the audio.

    The visual map is exact in pitch CLASS and interval, but the absolute
    octave (and a whole-keyboard semitone shift from a mis-anchored edge key)
    can be off. Score the notes' predicted CQT bins against the real audio at
    shifts of ±1 semitone and ±1..2 octaves; return the best shift.
    """
    import librosa
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True, duration=150)
    hop = 512
    C = np.abs(librosa.cqt(y=y, sr=sr, fmin=librosa.note_to_hz("A0"),
                           n_bins=88, bins_per_octave=12, hop_length=hop))
    tgrid = np.arange(C.shape[1]) * hop / sr
    shifts = [0, -12, 12, -24, 24]
    # Spectral whitening kills the low-frequency tilt (bass dominates raw CQT).
    kernel = np.ones(13) / 13
    wins = {s: 0 for s in shifts}

    def at(col, b):
        return float(col[b]) if 0 <= b < 88 else 0.0

    for n in notes[:500]:
        if n["start_time"] >= tgrid[-1]:
            break
        i0 = int(np.searchsorted(tgrid, n["start_time"]))
        i1 = max(i0 + 1, int(np.searchsorted(tgrid, min(n["end_time"], tgrid[-1]))))
        col = C[:, i0:i1].mean(axis=1)
        col = col / (np.convolve(col, kernel, mode="same") + 1e-9)
        vals = {}
        for s in shifts:
            b = n["pitch"] + s - 21               # A0 = MIDI 21 = bin 0
            if not (0 <= b < 88):
                continue
            # Harmonic-aware: a real fundamental has energy at b and b+12 but
            # NO subharmonic at b-12 — that asymmetry pins the octave.
            vals[s] = at(col, b) + 0.5 * at(col, b + 12) - 0.9 * at(col, b - 12)
        if vals:
            wins[max(vals, key=vals.get)] += 1
    best_shift = max(wins, key=wins.get)
    total = sum(wins.values()) or 1
    print(f"[tiles] octave check: shift {best_shift:+d} semitones "
          f"({100 * wins[best_shift] // total}% of notes agree; {wins})")
    # Only act on a confident majority — a mush result keeps the visual map.
    return best_shift if wins[best_shift] > 0.5 * total else 0


def write_outputs(result: dict, out_dir: str | Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    notes = result["notes"]
    (out_dir / "notes.json").write_text(json.dumps(
        [{k: n[k] for k in ("start_time", "end_time", "pitch", "velocity")} for n in notes],
        indent=2))
    (out_dir / "tiles_meta.json").write_text(json.dumps(
        {"mode": result["mode"], "fps": result["fps"], "keyboard": result["keyboard"],
         "hands": sum(1 for n in notes if "hand" in n)}, indent=2))
    try:
        import mido
        mid = mido.MidiFile(); tr = mido.MidiTrack(); mid.tracks.append(tr)
        tempo = mido.bpm2tempo(120); tr.append(mido.MetaMessage("set_tempo", tempo=tempo))
        events = []
        for n in notes:
            events.append((n["start_time"], "note_on", n["pitch"]))
            events.append((n["end_time"], "note_off", n["pitch"]))
        events.sort()
        last = 0.0
        for t, kind, p in events:
            ticks = int(mido.second2tick(t - last, mid.ticks_per_beat, tempo))
            tr.append(mido.Message(kind, note=int(p), velocity=90, time=max(0, ticks)))
            last = t
        mid.save(str(out_dir / "tiles.mid"))
    except Exception as exc:  # noqa: BLE001
        print(f"[tiles] MIDI write skipped: {exc}")
    print(f"[tiles] {len(notes)} notes -> {out_dir / 'notes.json'}")
    return out_dir / "notes.json"


def main():
    if len(sys.argv) < 3:
        print("Usage: python -m tiles.extract <video> <out_dir> [--stride N]")
        sys.exit(1)
    stride = int(sys.argv[sys.argv.index("--stride") + 1]) if "--stride" in sys.argv else 1
    result = extract_notes(sys.argv[1], stride=stride)
    write_outputs(result, sys.argv[2])


if __name__ == "__main__":
    main()
