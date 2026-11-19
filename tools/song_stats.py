"""Read-only health stats for every processed song. Usage: python stats.py <data/output>"""
import json, sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
for d in sorted(root.iterdir()):
    tj = d / "tab.json"
    if not tj.exists():
        print(f"\n== {d.name[:40]}: NO tab.json; files={len(list(d.iterdir()))}")
        continue
    t = json.loads(tj.read_text())
    m = t.get("metadata", {})
    dur = m.get("duration_sec") or 1
    ch = t.get("chords", [])
    notes = t.get("notes", [])
    raw = []
    if (d / "notes.json").exists():
        raw = json.loads((d / "notes.json").read_text())
    quals = Counter(c["name"].split(":")[1].split("/")[0] if ":" in c["name"] else "?" for c in ch)
    qtime = Counter()
    for c in ch:
        q = c["name"].split(":")[1].split("/")[0] if ":" in c["name"] else "?"
        qtime[q] += c["end"] - c["start"]
    tot = sum(qtime.values()) or 1
    uniq = Counter()
    for c in ch:
        uniq[c["name"]] += c["end"] - c["start"]
    slash = sum(1 for c in ch if "/" in c["name"])
    durs = sorted(c["end"] - c["start"] for c in ch)
    med = durs[len(durs) // 2] if durs else 0
    short = sum(1 for x in durs if x < 1.0)
    beats = t.get("beats", [])
    bpm = m.get("bpm") or 0
    beat_len = 60 / bpm if bpm else 0
    cov = tot / dur
    conf = sorted(c.get("confidence", 0) for c in ch)
    lowconf = sum(1 for x in conf if x < 0.3)
    print(f"\n== {d.name[:44]}  inst={m.get('instrument')} key={m.get('key')} bpm={bpm} dur={dur:.0f}s capo={m.get('capo')}")
    print(f"   chords: {len(ch)} segs, {len(uniq)} unique, {len(ch)/dur*60:.1f} changes/min, median {med:.2f}s ({med/beat_len if beat_len else 0:.1f} beats), <1s: {short}, slash: {slash}, coverage {cov:.0%}, lowconf(<.3): {lowconf}")
    print("   quality by time: " + ", ".join(f"{q} {v/tot:.0%}" for q, v in qtime.most_common()))
    print("   top chords: " + ", ".join(f"{n} {v:.0f}s" for n, v in uniq.most_common(10)))
    lead = sum(1 for n in notes if n.get("voice") == "lead")
    nd = sorted(n["duration"] for n in notes)
    print(f"   notes: raw {len(raw)} ({len(raw)/dur:.1f}/s) -> final {len(notes)} ({len(notes)/dur:.1f}/s), lead {lead}, median dur {nd[len(nd)//2] if nd else 0:.2f}s")
    if notes:
        frets = Counter(min(n['fret'] // 4, 5) for n in notes)
        print("   fret zones (0-3,4-7,8-11,12-15,16-19,20+): " + ", ".join(f"{frets.get(i,0)}" for i in range(6)))
        # same-onset same-string collisions
        by = Counter((round(n['start'], 2), n['string']) for n in notes)
        print(f"   same-onset same-string collisions: {sum(1 for v in by.values() if v > 1)}")
        # hand jumps between consecutive fretted notes
        fr = [n['fret'] for n in sorted(notes, key=lambda n: n['start']) if n['fret'] > 0]
        jumps = sum(1 for a, b in zip(fr, fr[1:]) if abs(a - b) > 5)
        print(f"   fret jumps >5 between consecutive fretted notes: {jumps} of {max(1,len(fr)-1)} ({jumps/max(1,len(fr)-1):.0%})")
    a = t.get("analysis", {})
    secs = a.get("sections", [])
    print(f"   sections: {[s['name'] for s in secs][:14]}  strum: {(a.get('strumming') or {}).get('pattern')}  prog: {(a.get('progression') or {}).get('chords')}")
    print(f"   vocals: {len(t.get('vocals', []))} notes, contour {len(t.get('vocal_pitch', []))}  roll: {len(t.get('roll', []))}  warning: {m.get('warning','')[:60]}")
    print(f"   downbeats in tab: {'downbeats' in t}  beats: {len(beats)}  tab.json size: {tj.stat().st_size/1e6:.1f} MB")
    wavs = sorted(p.name for p in d.glob('*.wav'))
    print(f"   stems: {len(wavs)} wav, {sum(p.stat().st_size for p in d.glob('*.wav'))/1e6:.0f} MB")
