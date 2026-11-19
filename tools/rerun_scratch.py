"""Re-run the CURRENT engine+refine+arrange+analyze on a scratch copy of a song.
Never writes inside data/. Usage (from repo root): python rerun.py <song_dir> <scratch_out_dir>"""
import json, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, ".")
src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
shutil.copy(src / "notes.json", out / "notes.json")
shutil.copy(src / "beats.json", out / "beats.json")
guitar = next(iter(sorted(src.glob("*[Gg]uitar*.wav"))), None)
comb = next(iter(sorted(src.glob("*[Cc]ombined*.wav"))), None) or guitar
t0 = time.time()
r = subprocess.run([str(Path("tab_engine/build/tab_engine.exe").resolve()), str(out / "notes.json"), str(guitar),
                    str(out / "beats.json"), str(comb)], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
print(f"engine rc={r.returncode} in {time.time()-t0:.0f}s")
for line in r.stdout.splitlines():
    if any(k in line for k in ("Tuning", "Estimated key", "Surviving", "Dropped", "Worst single", "Out-of-range")):
        print("   ", line.strip())
tab = json.loads((out / "tab.json").read_text())
raw_engine = json.loads(json.dumps(tab["notes"]))
by = {}
for n in raw_engine:
    by.setdefault((round(n["start"], 3), n["string"]), []).append(n)
print(f"   engine notes {len(raw_engine)}; exact-same-onset same-string collisions: {sum(1 for v in by.values() if len(v) > 1)}")
from run_pipeline import finish_tab
t1 = time.time()
tab = finish_tab(tab, guitar)
print(f"refine+arrange+fingering {time.time()-t1:.0f}s")
tab.setdefault("metadata", {})["instrument"] = "guitar(rerun)"
(out / "tab.json").write_text(json.dumps(tab))
