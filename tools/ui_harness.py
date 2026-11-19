"""See the real UI without starting a server (HANDOFF.md rule 5).

Builds one self-contained HTML file: the real index.html with style.css,
synth.js and app.js inlined and `fetch` stubbed to answer from a fixture (a
library with done / outdated / processing / error rows, one song's tab.json,
and the chord shapes from arrange.py). Open it in any browser, or in the
desktop app's built-in browser, which renders project files as data: URLs
(hence everything inline).

Usage (from repo root):
    python tools/ui_harness.py <tab.json> [out_dir]   # default out_dir: _ui_harness/
Delete the out_dir afterwards; it is a throwaway.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
from arrange import SHAPES, voicing  # noqa: E402

tab_path = Path(sys.argv[1])
out = Path(sys.argv[2] if len(sys.argv) > 2 else "_ui_harness")
out.mkdir(parents=True, exist_ok=True)

row = {"stage": "", "error": "", "detail": "", "outdated": False, "has_tab": False,
       "started": 0, "url": "", "instrument": "guitar", "options": {}}
fixture = {
    "jobs": [
        {**row, "id": "song", "name": tab_path.parent.name or "song", "status": "done",
         "stage": "complete", "has_tab": True},
        {**row, "id": "old_song", "name": "An Old Song", "status": "done", "stage": "complete",
         "has_tab": True, "outdated": True},
        {**row, "id": "new_song", "name": "Some New Song (Official Video)", "status": "processing",
         "stage": "Transcribing notes", "started": time.time() - 102},
        {**row, "id": "broken", "name": "A Failed Song", "status": "error",
         "error": "The GPU ran out of memory. Close other programs using the GPU and press Retry.",
         "detail": "Traceback ...\nRuntimeError: CUDA error: out of memory"},
    ],
    "tab": json.loads(tab_path.read_text()),
    "shapes": {"voicings": {f"{t}|{q}": voicing(t, q) for t in range(12) for q in SHAPES}},
}

stub = """<script>
window.FIX = %s;
window.CALLS = [];   // every request the UI made: [method, url, body]
function silentWav(sec) {
  const sr = 8000, n = sr * sec, b = new ArrayBuffer(44 + n * 2), v = new DataView(b);
  const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, n * 2, true); return b;
}
const J = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opts = {}) => {
  CALLS.push([opts.method || "GET", url, opts.body || null]);
  if (url === "/api/jobs") return J({ jobs: FIX.jobs });
  if (url === "/api/shapes") return J(FIX.shapes);
  if (url.startsWith("/api/result/")) return J(FIX.tab);
  if (url.startsWith("/api/audio/")) return new Response(silentWav(2));
  if (url.startsWith("/api/status/")) return J(FIX.jobs.find((j) => url.endsWith(j.id)));
  if (opts.method === "POST") return J({ job_id: url.split("/").pop() });
  return J({ detail: "not stubbed" }, 404);
};
</script>
""" % json.dumps(fixture)

static = Path("server/static")
inline = lambda f: (static / f).read_text(encoding="utf-8").replace("</script>", "<\\/script>")
html = (static / "index.html").read_text(encoding="utf-8")
html = html.replace('<link rel="stylesheet" href="/static/style.css" />', "<style>" + inline("style.css") + "</style>")
html = html.replace('<script src="/static/synth.js"></script>', stub + "<script>" + inline("synth.js") + "</script>")
html = html.replace('<script src="/static/app.js"></script>', "<script>" + inline("app.js") + "</script>")
assert "/static/" not in html, "index.html references an asset this harness doesn't inline"
(out / "harness.html").write_text(html, encoding="utf-8")
print((out / "harness.html").resolve().as_uri())
