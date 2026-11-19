"""Re-run the CURRENT chords+refine+arrange+fingering+analyze on a scratch copy of a song.
Never writes inside data/. Usage (from repo root): python tools/rerun_scratch.py <song_dir> <scratch_out_dir>"""
import json, shutil, sys, time
from pathlib import Path

sys.path.insert(0, ".")
from run_pipeline import _build_tab, finish_tab

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
for f in ("notes.json", "beats.json"):
    shutil.copy(src / f, out / f)
guitar = next(iter(sorted(src.glob("*[Gg]uitar*.wav"))), None)
comb = next(iter(sorted(src.glob("*[Cc]ombined*.wav"))), None) or guitar
t0 = time.time()
tab = finish_tab(_build_tab(out / "notes.json", out / "beats.json", comb), guitar)
print(f"chords+refine+arrange+fingering {time.time() - t0:.0f}s: "
      f"{len(tab['notes'])} notes, {len(tab['chords'])} chords, key {tab['metadata'].get('key')}")
tab["metadata"]["instrument"] = "guitar(rerun)"
(out / "tab.json").write_text(json.dumps(tab))
