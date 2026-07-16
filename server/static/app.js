(() => {
  const $ = (id) => document.getElementById(id);
  const stage = $("stage"), sx = stage.getContext("2d");
  const tabStage = $("tabStage"), tx = tabStage.getContext("2d");
  const vocalStage = $("vocalStage"), vc = vocalStage.getContext("2d");
  const rollStage = $("rollStage"), rx = rollStage.getContext("2d");
  const wave = $("wave"), wx = wave.getContext("2d");
  const PLAY_ICON = '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M4 3l9 5-9 5z" fill="currentColor"/></svg>';
  const PAUSE_ICON = '<svg width="16" height="16" viewBox="0 0 16 16"><rect x="4" y="3" width="3" height="10" rx="1" fill="currentColor"/><rect x="9" y="3" width="3" height="10" rx="1" fill="currentColor"/></svg>';

  // ── Data ──────────────────────────────────────────────────────────────────
  // (Chords arrive beat-synchronous from the engine — no client smoothing.)
  let tab = null, allNotes = [], melodyNotes = [], harmonyNotes = [], chords = [], chordsRaw = [], beats = [], duration = 0;
  let barMode = false;   // "Bars": one chord per bar (dominant by coverage)
  let vocals = [], vpitch = [], vlo = 48, vhi = 72;
  let roll = [];   // tiles videos: exact piano notes {start,duration,pitch,hand}
  let rlo = 48, rhi = 72;   // roll view's smoothed, auto-following pitch window
  let view = "overview", content = "both", capo = 0, recommendedCapo = 0, curJob = null;
  let analysis = {};   // tab.analysis — romans, functions, difficulty, practice…

  // ── Chord voicings (computed live so any capo can be selected) ────────────
  const NOTE_PC = { C: 0, "C#": 1, D: 2, "D#": 3, E: 4, F: 5, "F#": 6, G: 7, "G#": 8, A: 9, "A#": 10, B: 11 };
  const PC_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  // Kept in sync with arrange.py's SHAPES — every quality the chord engine
  // can emit needs an entry or its diagram silently never renders, capo or not.
  const BARRE = { maj: [0,2,2,1,0,0], min: [0,2,2,0,0,0], "5": [0,2,2,-1,-1,-1],
                  "7": [0,2,0,1,0,0], maj7: [0,2,1,1,0,0], min7: [0,2,0,0,0,0], sus4: [0,2,2,2,0,0],
                  sus2: [0,2,4,4,0,0], add9: [0,2,2,1,0,2], "6": [0,2,2,1,2,0],
                  dim: [0,1,2,0,-1,0], aug: [0,3,2,1,1,0], m7b5: [0,1,0,0,-1,0] };
  const OPEN = {
    "0|maj":["C",[-1,3,2,0,1,0]],"2|maj":["D",[-1,-1,0,2,3,2]],"4|maj":["E",[0,2,2,1,0,0]],
    "7|maj":["G",[3,2,0,0,0,3]],"9|maj":["A",[-1,0,2,2,2,0]],
    "2|min":["Dm",[-1,-1,0,2,3,1]],"4|min":["Em",[0,2,2,0,0,0]],"9|min":["Am",[-1,0,2,2,1,0]],
    "0|7":["C7",[-1,3,2,3,1,0]],"2|7":["D7",[-1,-1,0,2,1,2]],"4|7":["E7",[0,2,0,1,0,0]],
    "7|7":["G7",[3,2,0,0,0,1]],"9|7":["A7",[-1,0,2,0,2,0]],"11|7":["B7",[-1,2,1,2,0,2]],
    "0|maj7":["Cmaj7",[-1,3,2,0,0,0]],"2|maj7":["Dmaj7",[-1,-1,0,2,2,2]],"4|maj7":["Emaj7",[0,2,1,1,0,0]],
    "5|maj7":["Fmaj7",[-1,-1,3,2,1,0]],"7|maj7":["Gmaj7",[3,2,0,0,0,2]],"9|maj7":["Amaj7",[-1,0,2,1,2,0]],
    "2|min7":["Dm7",[-1,-1,0,2,1,1]],"4|min7":["Em7",[0,2,0,0,0,0]],"9|min7":["Am7",[-1,0,2,0,1,0]],
    "2|sus4":["Dsus4",[-1,-1,0,2,3,3]],"4|sus4":["Esus4",[0,2,2,2,0,0]],"9|sus4":["Asus4",[-1,0,2,2,3,0]],
    "2|5":["D5",[-1,-1,0,2,3,-1]],"4|5":["E5",[0,2,2,-1,-1,-1]],"9|5":["A5",[-1,0,2,2,-1,-1]],
  };
  function jsVoicing(name, c) {
    if (!name || !name.includes(":")) return null;
    const root = name.split(":")[0];
    const qual = name.split(":")[1].split("/")[0];   // "G:maj/B" → G major shape
    if (!(root in NOTE_PC) || !(qual in BARRE)) return null;
    const t = ((NOTE_PC[root] - c) % 12 + 12) % 12;
    const o = OPEN[t + "|" + qual];
    if (o) return { name: o[0], frets: o[1], baseFret: 0, open: true };
    const base = ((t - 4) % 12 + 12) % 12;
    return { name: PC_NOTE[t] + (qual === "min" ? "m" : qual === "maj" ? "" : qual),
             frets: BARRE[qual].map((v) => v < 0 ? -1 : v + base), baseFret: base, open: false };
  }
  const voicingOf = (c) => jsVoicing(c.name, capo);
  const shortName = (n) => n.replace(":maj", "").replace(":min", "m").replace(":", " ");

  // With a capo, a note recorded below the capo fret can't be played on its
  // original string — re-fret the same pitch onto the nearest string that can
  // (what a real player does), instead of hiding the note.
  const TUNE = [64, 59, 55, 50, 45, 40];   // open-string MIDI, string 1 (high e) … 6 (low E)
  function refret(n, c) {
    if (n.fret >= c) return { s: n.string - 1, f: n.fret };
    let best = null;
    for (let s = 0; s < 6; s++) {
      const f = n.pitch - TUNE[s];
      if (f < c || f > 22) continue;
      const d = Math.abs(s - (n.string - 1));
      if (!best || d < best.d) best = { s, f, d };
    }
    return best;   // null → genuinely unplayable at this capo
  }

  // ── Web Audio ─────────────────────────────────────────────────────────────
  let actx = null, buffer = null, src = null;
  let playing = false, t0ctx = 0, t0song = 0, paused = 0, rate = 1, dirty = true;
  let loopA = null, loopB = null, metro = false, metroIdx = 0;

  const ctx = () => (actx || (actx = new (window.AudioContext || window.webkitAudioContext)()));
  const songTime = () => playing ? t0song + (ctx().currentTime - t0ctx) * rate : paused;
  function stopSrc() { if (src) { try { src.onended = null; src.stop(); } catch (e) {} src = null; } }
  function play(off) {
    ctx(); if (actx.state === "suspended") actx.resume();
    if (!buffer) return;
    stopSrc();
    off = Math.max(0, Math.min(off, duration));
    src = actx.createBufferSource(); src.buffer = buffer; src.playbackRate.value = rate;
    src.connect(actx.destination);
    src.onended = () => { if (playing && songTime() >= duration - 0.06) { pause(); paused = 0; } };
    src.start(0, off);
    t0ctx = actx.currentTime; t0song = off; playing = true; metroIdx = 0;
    $("playBtn").innerHTML = PAUSE_ICON;
  }
  function pause() { if (!playing) return; paused = songTime(); playing = false; stopSrc(); $("playBtn").innerHTML = PLAY_ICON; dirty = true; }
  const toggle = () => { if (buffer) (playing ? pause() : play(paused)); };
  const seekTo = (t) => { t = Math.max(0, Math.min(t, duration)); if (playing) play(t); else { paused = t; dirty = true; } };
  function click(when) {
    const o = actx.createOscillator(), g = actx.createGain();
    o.frequency.value = 1500; o.connect(g); g.connect(actx.destination);
    g.gain.setValueAtTime(0.0001, when); g.gain.exponentialRampToValueAtTime(0.5, when + 0.001);
    g.gain.exponentialRampToValueAtTime(0.0001, when + 0.05); o.start(when); o.stop(when + 0.06);
  }

  // ── Overlay ───────────────────────────────────────────────────────────────
  // The step list depends on which pipeline is running: tiles videos read notes
  // from the video and never separate or transcribe. Each entry is
  // [stage prefix reported by the backend, label].
  const FLOWS = {
    audio: [["starting", "Initializing"], ["Separating stems", "Separating stems"], ["Tracking beats", "Tracking beats"],
            ["Transcribing notes", "Transcribing notes"], ["Building tab", "Building tab"], ["Arranging", "Arranging"],
            ["Extracting vocals", "Extracting vocals"]],
    tiles: [["starting", "Initializing"], ["Reading tiles video", "Reading tiles video"], ["Tracking beats", "Tracking beats"],
            ["Building tab", "Building tab"], ["Arranging", "Arranging"]],
  };
  // Stages the backend reports that stand in for a listed step.
  const STEP_ALIAS = { "Skipping separation": "Separating stems" };
  let stepFlow = "audio", overlayT0 = 0;

  function setStepFlow(flow) {
    if (flow === stepFlow && $("progressSteps").children.length) return;
    stepFlow = flow;
    $("progressSteps").innerHTML = FLOWS[flow].map(([step, label]) =>
      `<div class="pStep" data-step="${step}"><span class="stepDot"></span><span class="stepLabel">${label}</span></div>`).join("");
  }
  function showOverlay(msg, sub, stg) {
    const ov = $("overlay");
    if (ov.classList.contains("hidden")) overlayT0 = Date.now();
    ov.classList.remove("hidden");
    $("overlayMsg").textContent = msg;
    $("overlaySub").textContent = sub || "";
    updateSteps(stg);
  }
  function updateSteps(currentStage) {
    const stage = STEP_ALIAS[currentStage] || currentStage || "";
    // "Reading tiles video" is reported only by the tiles pipeline, so it also
    // covers reprocesses, where the UI never knew the job's kind.
    if (stage.startsWith("Reading tiles video")) setStepFlow("tiles");
    else if (["Separating stems", "Transcribing notes", "Extracting vocals"].some((s) => stage.startsWith(s))) setStepFlow("audio");
    const order = FLOWS[stepFlow].map(([s]) => s);
    // Prefix match: two-stage separation reports "Separating stems (1/2 — …)"
    // etc. — those still light up the "Separating stems" step.
    const idx = order.findIndex((s) => stage.startsWith(s));
    document.querySelectorAll(".pStep").forEach((el) => {
      const stepIdx = order.indexOf(el.dataset.step);
      el.classList.remove("done", "active");
      if (idx < 0) return;
      if (stepIdx < idx) el.classList.add("done");
      else if (stepIdx === idx) el.classList.add("active");
    });
  }
  const hideOverlay = () => { $("overlay").classList.add("hidden"); overlayT0 = 0; };
  const elapsedStr = () => { const e = overlayT0 ? Math.floor((Date.now() - overlayT0) / 1000) : 0; return Math.floor(e / 60) + ":" + String(e % 60).padStart(2, "0"); };

  // ── Library sidebar ───────────────────────────────────────────────────────
  async function getJobs() { return (await (await fetch("/api/jobs")).json()).jobs; }

  function renderSongList(jobs) {
    const list = $("songList"); list.innerHTML = "";
    if (!jobs.length) {
      list.innerHTML = "<p class='muted' style='padding:0 8px;color:var(--muted);font-size:12.5px'>No songs yet — paste a YouTube link or upload a file above.</p>";
      return;
    }
    for (const j of jobs) {
      const row = document.createElement("div");
      row.className = "songRow " + j.status + (j.id === curJob ? " active" : "");
      row.innerHTML = `<span class="songDot"></span><span class="songName" title="${j.name}">${j.name}</span>`;
      row.onclick = () => { if (j.status === "done") loadJob(j.id); };

      const acts = document.createElement("div"); acts.className = "songActs";
      const rn = document.createElement("button"); rn.textContent = "✎"; rn.title = "Rename";
      rn.onclick = async (e) => {
        e.stopPropagation();
        const name = prompt("Rename song to:", j.name);
        if (!name || name.trim() === j.name) return;
        const r = await fetch(`/api/rename/${j.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
        if (!r.ok) { let d = ""; try { d = (await r.json()).detail; } catch (e2) {} $("status").textContent = "Rename failed: " + (d || r.status); return; }
        refreshJobs();
      };
      const rp = document.createElement("button"); rp.textContent = "⟳"; rp.title = "Reprocess";
      rp.onclick = (e) => {
        e.stopPropagation();
        settingsReprocessJobId = j.id;
        $("runSettingsBtn").textContent = "Apply & Reprocess";
        $("settingsModal").classList.remove("hidden");
      };
      const del = document.createElement("button"); del.textContent = "✕"; del.title = "Delete"; del.className = "danger";
      del.onclick = async (e) => {
        e.stopPropagation();
        del.disabled = true;
        try {
          const r = await fetch(`/api/jobs/${j.id}`, { method: "DELETE" });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
          if (d.still_exists) throw new Error("files in use — close players and retry");
          row.remove();
        } catch (err) {
          del.disabled = false;
          $("status").textContent = "Delete failed: " + err.message;
        }
      };
      acts.append(rn, rp, del);
      row.appendChild(acts);
      list.appendChild(row);
    }
  }
  async function refreshJobs() { const jobs = await getJobs(); renderSongList(jobs); return jobs; }

  async function loadJob(id) {
    pause(); paused = 0; loopA = loopB = null; $("loopBtn").classList.remove("on"); $("loopBtn").textContent = "Loop";
    curJob = id;
    const r = await fetch(`/api/result/${id}`);
    if (!r.ok) { $("status").textContent = "Result not ready"; return; }
    tab = await r.json();
    analysis = tab.analysis || {};
    allNotes = (tab.notes || []).slice().sort((a, b) => a.start - b.start);
    melodyNotes = allNotes.filter((n) => n.voice === "lead" || n.melody);
    harmonyNotes = allNotes.filter((n) => !(n.voice === "lead" || n.melody));
    chordsRaw = (tab.chords || []).slice().sort((a, b) => a.start - b.start);
    beats = tab.beats || [];
    chords = barMode ? quantizeToBars(chordsRaw) : chordsRaw;
    vocals = (tab.vocals || []).slice().sort((a, b) => a.start - b.start);
    vpitch = tab.vocal_pitch || [];
    if (vocals.length) { const ps = vocals.map((n) => n.pitch); vlo = Math.min(...ps) - 3; vhi = Math.max(...ps) + 3; }
    $("viewSeg").querySelector('[data-view=vocals]').style.display = (vocals.length || vpitch.length) ? "" : "none";
    roll = (tab.roll || []).slice().sort((a, b) => a.start - b.start);
    $("viewSeg").querySelector('[data-view=roll]').style.display = roll.length ? "" : "none";

    const m = tab.metadata || {};
    const last = allNotes.length ? allNotes[allNotes.length - 1] : null;
    duration = m.duration_sec || (last ? last.start + last.duration : 0);
    $("songTitle").textContent = id;
    $("songTitle").title = id;
    $("keyBadge").textContent = "key " + (m.key || "—");
    $("bpmBadge").textContent = (m.bpm ? Number(m.bpm).toFixed(0) : "—") + " bpm";
    const diff = (analysis.difficulty || {}).overall;
    $("diffBadge").style.display = diff ? "" : "none";
    if (diff) $("diffBadge").textContent = "difficulty " + diff + "/5";
    recommendedCapo = m.capo || 0;
    fillCapoSelect();
    capo = recommendedCapo;
    $("capoSel").value = String(capo);
    updateCapoBadge();

    ctx();
    const ab = await (await fetch(`/api/audio/${id}`)).arrayBuffer();
    buffer = await actx.decodeAudioData(ab);
    duration = Math.max(duration, buffer.duration);
    computePeaks();
    $("playBtn").disabled = false;
    buildOverview(); buildChordGrid(); buildLearn();
    setView("overview");
    $("status").textContent = m.warning ? "⚠ " + m.warning : "";
    dirty = true;
    refreshJobs();
    hideOverlay();
  }

  function fillCapoSelect() {
    const s = $("capoSel"); s.innerHTML = "";
    for (let k = 0; k <= 9; k++) {
      const o = document.createElement("option"); o.value = String(k);
      o.textContent = (k === 0 ? "0 (none)" : String(k)) + (k === recommendedCapo && k > 0 ? " ★" : "");
      s.appendChild(o);
    }
  }
  const updateCapoBadge = () => {
    const b = $("capoBadge");
    b.style.display = capo > 0 ? "" : "none";
    b.textContent = "capo " + capo;
  };

  // ── Add: upload / youtube ─────────────────────────────────────────────────
  $("fileInput").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    setStepFlow($("tilesChk").checked ? "tiles" : "audio");
    showOverlay("Uploading…", f.name);
    const fd = new FormData(); fd.append("file", f); fd.append("instrument", $("instSel").value);
    fd.append("run_beats", $("optBeats").checked);
    fd.append("run_vocals", $("optVocals").checked);
    fd.append("vocal_model", $("optVocalModel").value);
    fd.append("separation_quality", $("optSep").value);
    fd.append("tiles", $("tilesChk").checked);
    const r = await fetch("/api/transcribe", { method: "POST", body: fd });
    e.target.value = "";
    if (!r.ok) { showOverlay("Upload failed", await r.text()); return; }
    refreshJobs();
    poll((await r.json()).job_id);
  });
  $("ytBtn").addEventListener("click", async () => {
    const url = $("ytInput").value.trim(); if (!url) return;
    setStepFlow($("tilesChk").checked ? "tiles" : "audio");
    showOverlay("Fetching from YouTube…", url);
    const r = await fetch("/api/transcribe/youtube", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        url,
        instrument: $("instSel").value,
        run_beats: $("optBeats").checked,
        run_vocals: $("optVocals").checked,
        vocal_model: $("optVocalModel").value,
        separation_quality: $("optSep").value,
        tiles: $("tilesChk").checked
      }),
    });
    if (!r.ok) { let d = ""; try { d = (await r.json()).detail; } catch (e) {} showOverlay("YouTube failed", d || `HTTP ${r.status}`); return; }
    $("ytInput").value = "";
    refreshJobs();
    poll((await r.json()).job_id);
  });

  async function poll(id) {
    let s;
    try { s = await (await fetch(`/api/status/${id}`)).json(); }
    catch (e) { setTimeout(() => poll(id), 2000); return; }   // transient blip — keep polling
    if (s.status === "done") { hideOverlay(); await refreshJobs(); return loadJob(id); }
    if (s.status === "error") { showOverlay("Processing failed", (s.error || "").split("\n").filter(Boolean).pop() || "unknown error"); return; }
    const stg = s.stage || "starting";
    const hint = (stg === "Separating stems" || stg === "Transcribing notes") ? " · this stage is the slow one" : "";
    showOverlay("Processing  " + elapsedStr(), stg + " · first run only (cached after)" + hint, stg);
    setTimeout(() => poll(id), 1000);
  }

  // ── Chord diagram (SVG) ───────────────────────────────────────────────────
  function chordSVG(v, size = 116) {
    if (!v || !v.frets) return "";
    const frets = v.frets, fretted = frets.filter((f) => f > 0);
    const maxF = fretted.length ? Math.max(...fretted) : 0, minF = fretted.length ? Math.min(...fretted) : 0;
    const open = maxF <= 4, startFret = open ? 0 : minF;
    const FR = 5, ST = 6, w = size, h = size * 1.18;
    const padX = size * 0.16, padT = size * 0.2, padB = size * 0.06;
    const gw = w - 2 * padX, gh = h - padT - padB, st = gw / (ST - 1), fy = gh / FR;
    let s = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}">`;
    for (let i = 0; i < ST; i++) { const x = padX + i * st; s += `<line x1="${x}" y1="${padT}" x2="${x}" y2="${padT + gh}" stroke="rgba(255,255,255,.28)"/>`; }
    for (let j = 0; j <= FR; j++) { const y = padT + j * fy; const o = (j === 0 && open); s += `<line x1="${padX}" y1="${y}" x2="${padX + gw}" y2="${y}" stroke="rgba(255,255,255,${o ? .65 : .18})" stroke-width="${o ? 3 : 1}"/>`; }
    if (!open) s += `<text x="${padX - 7}" y="${padT + fy * 0.72}" fill="#9aa3b6" font-size="${size * 0.11}" text-anchor="end">${startFret}</text>`;
    for (let i = 0; i < ST; i++) {
      const x = padX + i * st, val = frets[i];
      if (val < 0) s += `<text x="${x}" y="${padT - 5}" fill="#e06b6b" font-size="${size * 0.12}" text-anchor="middle">×</text>`;
      else if (val === 0) s += `<circle cx="${x}" cy="${padT - size * 0.075}" r="${size * 0.045}" fill="none" stroke="#ffffff" stroke-width="1.5"/>`;
      else { const pos = open ? val : (val - startFret + 1); const y = padT + (pos - 0.5) * fy; s += `<circle cx="${x}" cy="${y}" r="${size * 0.072}" fill="#ffffff"/>`; }
    }
    return s + "</svg>";
  }

  // Bar quantize: one chord per bar (groups of 4 beats), chosen by which
  // chord covers the most of that bar. Same-name bars merge. The engine's
  // beat-level changes stay available by toggling off.
  function quantizeToBars(list) {
    if (!beats.length || beats.length < 5 || !list.length) return list;
    const bars = [];
    for (let i = 0; i < beats.length; i += 4) bars.push(beats[i]);
    bars.push(Math.max(duration, beats[beats.length - 1]));
    const out = [];
    for (let b = 0; b < bars.length - 1; b++) {
      const s = bars[b], e = bars[b + 1];
      if (e - s <= 0) continue;
      const cover = new Map();
      for (const c of list) {
        const ov = Math.min(e, c.end) - Math.max(s, c.start);
        if (ov > 0) cover.set(c.name, (cover.get(c.name) || 0) + ov);
      }
      if (!cover.size) continue;
      const name = [...cover.entries()].sort((x, y) => y[1] - x[1])[0][0];
      if (out.length && out[out.length - 1].name === name && out[out.length - 1].end >= s - 0.01)
        out[out.length - 1].end = e;
      else out.push({ name, start: s, end: e, confidence: 1 });
    }
    return out.length ? out : list;
  }

  function uniqueChords() {
    const seen = new Map(), count = new Map(), time = new Map();
    for (const c of chords) {
      if (c.name === "silence" || c.name === "unknown") continue;
      count.set(c.name, (count.get(c.name) || 0) + 1);
      time.set(c.name, (time.get(c.name) || 0) + (c.end - c.start));
      if (!seen.has(c.name)) seen.set(c.name, c);
    }
    return { list: [...seen.values()], count, time };
  }

  function buildChordGrid() {
    const g = $("chordGrid"); g.innerHTML = "";
    const { list, count } = uniqueChords();
    const romans = analysis.romans || {};
    for (const c of list) {
      const v = voicingOf(c);
      const shape = (capo > 0 && v) ? `<div class="play">play ${v.name}</div>` : "";
      const ro = romans[c.name] ? `<div class="ro">${romans[c.name]}</div>` : "";
      const d = document.createElement("div");
      d.className = "chordCard"; d.dataset.name = c.name;
      d.innerHTML = `<div class="cn">${c.name}</div>${ro}${chordSVG(v, 104)}${shape}<div class="ct">${count.get(c.name)}×</div>`;
      d.onclick = () => { const f = chords.find((x) => x.name === c.name); if (f) seekTo(f.start); };
      g.appendChild(d);
    }
  }

  // ── Overview ──────────────────────────────────────────────────────────────
  function buildOverview() {
    const el = $("overviewBody"); if (!el || !tab) return;
    const m = tab.metadata || {};
    const { list, time } = uniqueChords();
    const romans = analysis.romans || {};
    const diff = analysis.difficulty || {};
    const prog = analysis.progression;
    const sorted = list.slice().sort((a, b) => (time.get(b.name) || 0) - (time.get(a.name) || 0));

    const chordTiles = sorted.slice(0, 8).map((c) => {
      const v = voicingOf(c);
      return `<div class="ovChord" data-name="${c.name}">${chordSVG(v, 74)}
        <div class="nm">${c.name}</div><div class="ro">${romans[c.name] || ""}</div></div>`;
    }).join("");

    const bars = ["chords", "changes", "riff"].map((k) => {
      const v = diff[k] || 0;
      return `<div class="diffBar"><span class="lbl">${k}</span><div class="track"><div class="fill" style="width:${v * 20}%"></div></div><span>${v || "—"}/5</span></div>`;
    }).join("");

    const progHtml = prog
      ? `<p class="prog">${prog.chords.map((n, i) => `<span>${shortName(n)}<span class="rn">${prog.romans[i] || ""}</span></span>`).join("<i>→</i>")}</p>
         <p class="muted">This loop repeats ${prog.count}× — it IS the song.</p>`
      : "<p class='muted'>No dominant loop found — the chords move freely.</p>";

    el.innerHTML = `
      <div class="ovGrid">
        <section class="card">
          <div class="ovBig">${curJob || ""}</div>
          <div class="ovStats">
            <div class="ovStat"><b>${m.key || "—"}</b><span>key</span></div>
            <div class="ovStat"><b>${m.bpm ? Number(m.bpm).toFixed(0) : "—"}</b><span>bpm</span></div>
            <div class="ovStat"><b>${recommendedCapo || "0"}</b><span>capo ★</span></div>
            <div class="ovStat"><b>${list.length}</b><span>chords</span></div>
            <div class="ovStat"><b>${(tab.melody || []).length}</b><span>lead notes</span></div>
          </div>
          <div class="ovCTA">
            <button class="primary" id="ovPractice">Start guided practice</button>
            <button id="ovPlay">Open player</button>
          </div>
        </section>
        <section class="card">
          <h3>Difficulty</h3>
          ${bars}
          <p class="muted">${diff.barre_required ? "Includes barre shapes — the capo suggestion removes most of them." : "No barre chords needed at the suggested capo."}</p>
        </section>
        <section class="card">
          <h3>The loop</h3>
          ${progHtml}
        </section>
        <section class="card" style="grid-column: 1 / -1;">
          <h3>Chord palette — most played first</h3>
          <div class="ovChords">${chordTiles}</div>
          <p class="muted">Click a chord to jump to where it first plays.</p>
        </section>
      </div>`;
    el.querySelectorAll(".ovChord").forEach((tile) => {
      tile.onclick = () => { const f = chords.find((x) => x.name === tile.dataset.name); if (f) { seekTo(f.start); setView("player"); } };
    });
    const bp = $("ovPractice"); if (bp) bp.onclick = () => setView("learn");
    const bo = $("ovPlay"); if (bo) bo.onclick = () => setView("player");
  }

  // ── Learn view (driven by tab.analysis) ───────────────────────────────────
  const progressKey = () => "bf_done_" + curJob;
  const getDone = () => { try { return new Set(JSON.parse(localStorage.getItem(progressKey()) || "[]")); } catch (e) { return new Set(); } };
  const setDone = (s) => localStorage.setItem(progressKey(), JSON.stringify([...s]));

  function buildLearn() {
    const el = $("learnBody"); if (!el || !tab) return;
    const m = tab.metadata || {};
    const key = m.key || "";
    const [tonicName, mode] = key.split(" ");
    const tonic = NOTE_PC[tonicName]; const major = mode === "major";
    const romans = analysis.romans || {};
    const functions = analysis.functions || {};
    const { list, count } = uniqueChords();
    const sorted = list.sort((a, b) => count.get(b.name) - count.get(a.name));

    // chords & roles
    const chordRows = sorted.slice(0, 10).map((c) => {
      const f = functions[c.name] || {};
      const v = jsVoicing(c.name, capo);
      const play = capo > 0 && v ? `play <b>${v.name}</b>` : (v ? `<b>${v.name}</b> shape` : "");
      return `<tr><td><b>${c.name}</b></td><td>${romans[c.name] || "?"}</td><td>${count.get(c.name)}×</td><td class="muted">${f.role || ""}</td><td>${play}</td></tr>`;
    }).join("");

    // scale
    let scaleHtml = "<p class='muted'>Key not detected.</p>";
    if (tonic !== undefined) {
      const degs = major ? [0,2,4,5,7,9,11] : [0,2,3,5,7,8,10];
      const names = degs.map((d) => PC_NOTE[(tonic + d) % 12]);
      scaleHtml = `<p><b>${key} scale:</b> ${names.join(" · ")}</p>
        <p class="muted">These 7 notes are the "safe" notes to solo or sing over this song.</p>`;
    }
    const scales = (analysis.solo_scales || []).map((s) =>
      `<p><b>${s.name}</b></p><div class="pillRow">${s.positions.map((p) => `<span class="pill">box ${p.box} · fret ${p.fret}</span>`).join("")}</div>
       <p class="muted">Box 1 at fret ${s.positions[0] ? s.positions[0].fret : "?"} is the classic solo position for this song.</p>`).join("");

    // cadences + borrowed
    const cad = (analysis.cadences || []).slice(0, 6).map((c) =>
      `<div class="cadRow" data-t="${c.at}"><b>${fmt(c.at)}</b> — ${shortName(c.from)} → ${shortName(c.to)}<br><span class="muted">${c.type}</span></div>`).join("")
      || "<p class='muted'>No clear cadences detected.</p>";
    const borrowed = (analysis.borrowed || []).slice(0, 5).map((b) =>
      `<p><b>${b.chord}</b> (${b.label}) — <span class="muted">${b.why}</span></p>`).join("");

    // transitions
    const trans = (analysis.transitions || []).slice(0, 5).map((t) =>
      `<tr><td><b>${shortName(t.from)} → ${shortName(t.to)}</b></td><td>${t.count}×</td><td class="muted">${t.barre ? "barre involved" : "open shapes"}</td></tr>`).join("");

    // practice plan with persistent checkmarks
    const done = getDone();
    const plan = (analysis.practice || []).map((p) =>
      `<div class="planStep ${done.has(p.step) ? "done" : ""}" data-step="${p.step}">
        <span class="chk">${done.has(p.step) ? "✓" : ""}</span>
        <div style="flex:1"><div class="tt">${p.title}</div><div class="dd">${p.detail}</div></div>
        <button class="drillBtn" data-step="${p.step}" title="Set up this drill (loop + speed) and play">▶</button>
      </div>`).join("") || "<p class='muted'>Process the song to generate a plan.</p>";

    el.innerHTML = `
      <div class="learnGrid">
        <section class="card">
          <h3>This song in a nutshell</h3>
          <p><b>Key:</b> ${key || "—"} &nbsp; <b>Tempo:</b> ${m.bpm ? Number(m.bpm).toFixed(0) : "?"} BPM &nbsp; ${capo > 0 ? `<b>Capo:</b> fret ${capo}` : "<b>No capo</b>"}</p>
          ${scaleHtml}
        </section>
        <section class="card">
          <h3>Practice plan</h3>
          ${plan}
          <p class="muted">Click a step to mark it done — progress is saved per song.</p>
        </section>
        <section class="card" style="grid-column: 1 / -1;">
          <h3>The chords &amp; their job</h3>
          <table class="ltab"><tr><th>Chord</th><th>Role</th><th>Uses</th><th>What it does</th><th>Shape</th></tr>${chordRows}</table>
        </section>
        <section class="card">
          <h3>Changes to drill</h3>
          <table class="ltab"><tr><th>Change</th><th>Count</th><th></th></tr>${trans || "<tr><td class='muted'>—</td></tr>"}</table>
          <p class="muted">Loop these with the Loop button at 0.5× — transitions, not shapes, are what actually make songs hard.</p>
        </section>
        <section class="card">
          <h3>Moments that resolve (cadences)</h3>
          ${cad}
          <p class="muted">Click one to hear it — this is where the song "comes home".</p>
        </section>
        ${borrowed ? `<section class="card"><h3>Borrowed colors</h3>${borrowed}<p class="muted">Chords from outside the key — the spice. Hearing WHY they sound surprising is real ear training.</p></section>` : ""}
        ${analysis.strumming ? `<section class="card"><h3>Strumming pattern</h3>
          <p style="font-family:ui-monospace,monospace;font-size:20px;letter-spacing:4px">${analysis.strumming.pattern}</p>
          <p class="muted">One bar, in eighth notes: D = downstrum on the beat, U = upstrum on the "&", · = let it ring. Confidence ${(analysis.strumming.confidence * 100).toFixed(0)}% — trust your ears over this on syncopated songs.</p>
        </section>` : ""}
        ${(analysis.sections || []).length ? `<section class="card"><h3>Song map</h3>
          <div class="pillRow">${analysis.sections.map((s) => `<span class="pill secPill" data-t="${s.start}">${s.name} · ${fmt(s.start)}</span>`).join("")}</div>
          <p class="muted">Click a section to jump there. Learn the chorus first — it repeats the most.</p>
        </section>` : ""}
        <section class="card">
          <h3>For soloing</h3>
          ${scales || "<p class='muted'>—</p>"}
        </section>
      </div>`;

    el.querySelectorAll(".planStep").forEach((st) => {
      st.onclick = () => {
        const s = getDone();
        if (s.has(st.dataset.step)) s.delete(st.dataset.step); else s.add(st.dataset.step);
        setDone(s); buildLearn();
      };
    });
    el.querySelectorAll(".cadRow").forEach((rw) => {
      rw.onclick = () => { seekTo(Math.max(0, parseFloat(rw.dataset.t) - 3)); setView("player"); if (!playing) toggle(); };
    });
    el.querySelectorAll(".secPill").forEach((p) => {
      p.style.cursor = "pointer";
      p.onclick = () => { seekTo(parseFloat(p.dataset.t)); setView("player"); };
    });
    el.querySelectorAll(".drillBtn").forEach((b) => {
      b.onclick = (e) => { e.stopPropagation(); startDrill(b.dataset.step); };
    });
  }

  // ── Guided drills: one click sets loop + speed + view and plays ──────────
  let riffPhrase = 0;
  function setLoop(a, b) {
    loopA = Math.max(0, a); loopB = Math.min(duration, b);
    const btn = $("loopBtn");
    btn.classList.add("on"); btn.textContent = `Loop ${fmt(loopA)}–${fmt(loopB)}`;
  }
  function clearLoop() {
    loopA = loopB = null;
    const btn = $("loopBtn"); btn.classList.remove("on"); btn.textContent = "Loop";
  }
  function setSpeed(v) { rate = v; $("speedSel").value = String(v); if (playing) play(songTime()); }
  function startDrill(step) {
    if (!buffer) return;
    if (step === "shapes") { setView("chords"); return; }
    if (step === "changes") {
      // loop the first spot where the #1 transition actually happens, ~2 bars wide
      const t0 = (analysis.transitions || [])[0];
      if (t0) {
        for (let i = 0; i < chords.length - 1; i++) {
          if (chords[i].name === t0.from && chords[i + 1].name === t0.to) {
            setLoop(chords[i].start - 0.5, chords[i + 1].end + 0.5);
            break;
          }
        }
      }
      setSpeed(0.5); setView("player"); play(loopA ?? 0); return;
    }
    if (step === "rhythm") {
      clearLoop(); metro = true; $("metroBtn").classList.add("on");
      setSpeed(0.75); setView("player"); play(paused); return;
    }
    if (step === "riff") {
      // melody phrases = gaps > 1.5 s; each click advances to the next phrase
      const mel = (tab.melody || []);
      if (mel.length) {
        const phrases = [];
        let s = mel[0].start, last = mel[0];
        for (const n of mel.slice(1)) {
          if (n.start - (last.start + last.duration) > 1.5) { phrases.push([s, last.start + last.duration]); s = n.start; }
          last = n;
        }
        phrases.push([s, last.start + last.duration]);
        const ph = phrases[riffPhrase % phrases.length]; riffPhrase++;
        setLoop(ph[0] - 0.3, ph[1] + 0.3);
        setSpeed(0.5); setView("tab"); play(loopA); return;
      }
    }
    if (step === "sing") { clearLoop(); setSpeed(0.75); setView("vocals"); play(0); return; }
    clearLoop(); setSpeed(step === "full" ? 0.75 : 1); setView("player"); play(0);   // full run
  }

  // ── Canvas helpers ────────────────────────────────────────────────────────
  const LABELS = ["e", "B", "G", "D", "A", "E"];
  const COLORS = ["#ff6a3d", "#ff9f1c", "#ffd23f", "#4cc97f", "#3aa7ff", "#a06bff"];
  const NOWLINE = "#ffffff", INK = "#11131a";
  const PPS = 150, NOW = 0.24;
  const fmt = (s) => { s = Math.max(0, s | 0); return (s / 60 | 0) + ":" + String(s % 60).padStart(2, "0"); };
  function lower(arr, t) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m].start < t) lo = m + 1; else hi = m; } return lo; }
  function chordAt(t) { let cur = null; for (const c of chords) { if (c.start <= t && t < c.end) cur = c; if (c.start > t) break; } return cur; }
  function roundRect(c, x, y, w, h, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }
  const csize = new Map();
  function watchCanvas(cv) {
    const apply = () => {
      const dpr = window.devicePixelRatio || 1;
      const r = cv.getBoundingClientRect();
      const w = Math.min(8000, Math.max(1, Math.round(r.width))), h = Math.min(8000, Math.max(1, Math.round(r.height)));
      const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
      if (cv.width !== bw || cv.height !== bh) { cv.width = bw; cv.height = bh; }
      csize.set(cv, { w, h, dpr }); dirty = true;
    };
    new ResizeObserver(apply).observe(cv); apply();
  }
  function frameSize(cv, c) { const s = csize.get(cv) || { w: 1, h: 1, dpr: 1 }; c.setTransform(s.dpr, 0, 0, s.dpr, 0, 0); return [s.w, s.h]; }

  // ── Waveform transport ────────────────────────────────────────────────────
  let peaks = null;
  function computePeaks() {
    if (!buffer) { peaks = null; return; }
    const N = 1000, data = buffer.getChannelData(0), step = Math.floor(data.length / N) || 1;
    peaks = new Float32Array(N);
    for (let i = 0; i < N; i++) {
      let mx = 0;
      const s0 = i * step, s1 = Math.min(data.length, s0 + step);
      for (let s = s0; s < s1; s += 16) { const v = Math.abs(data[s]); if (v > mx) mx = v; }
      peaks[i] = mx;
    }
  }
  function drawWave(t) {
    const [W, H] = frameSize(wave, wx);
    wx.clearRect(0, 0, W, H);
    if (!peaks || !duration) return;
    const mid = H / 2, px = Math.max(1, W / peaks.length);
    const playedX = (t / duration) * W;
    // section map: alternating shading + names (choruses slightly brighter)
    const secs = analysis.sections || [];
    wx.font = "9px system-ui"; wx.textBaseline = "top";
    secs.forEach((s, i) => {
      const x0 = (s.start / duration) * W, x1 = (s.end / duration) * W;
      wx.fillStyle = s.name === "chorus" ? "rgba(255,255,255,0.10)"
                   : (i % 2 ? "rgba(255,255,255,0.045)" : "rgba(255,255,255,0.02)");
      wx.fillRect(x0, 0, x1 - x0, H);
      if (x1 - x0 > 34) { wx.fillStyle = "rgba(255,255,255,0.5)"; wx.textAlign = "left"; wx.fillText(s.name, x0 + 3, 2); }
    });
    for (let i = 0; i < peaks.length; i++) {
      const x = i / peaks.length * W;
      const h = Math.max(1.5, peaks[i] * (H * 0.85));
      wx.fillStyle = x <= playedX ? "rgba(255,255,255,0.85)" : "rgba(255,255,255,0.22)";
      wx.fillRect(x, mid - h / 2, px * 0.8, h);
    }
    if (loopA != null) { wx.fillStyle = "rgba(255,255,255,0.9)"; wx.fillRect((loopA / duration) * W - 1, 0, 2, H); }
    if (loopB != null) { wx.fillStyle = "rgba(255,255,255,0.9)"; wx.fillRect((loopB / duration) * W - 1, 0, 2, H);
      wx.fillStyle = "rgba(255,255,255,0.08)"; wx.fillRect((loopA / duration) * W, 0, ((loopB - loopA) / duration) * W, H); }
    wx.fillStyle = "#ffffff"; wx.fillRect(playedX - 1, 0, 2, H);
  }
  let waveDrag = false;
  const waveSeek = (e) => {
    const r = wave.getBoundingClientRect();
    seekTo(((e.clientX - r.left) / r.width) * duration);
  };
  wave.addEventListener("pointerdown", (e) => { if (!duration) return; waveDrag = true; wave.setPointerCapture(e.pointerId); waveSeek(e); });
  wave.addEventListener("pointermove", (e) => { if (waveDrag) waveSeek(e); });
  wave.addEventListener("pointerup", () => { waveDrag = false; });

  // ── Player ────────────────────────────────────────────────────────────────
  function drawPlayer(t) {
    const [W, H] = frameSize(stage, sx);
    sx.clearRect(0, 0, W, H);
    const nowX = Math.round(W * NOW), ribbon = 36, top = ribbon, laneH = (H - ribbon) / 6;
    const tStart = t - nowX / PPS, tEnd = t + (W - nowX) / PPS;

    if (beats.length) { sx.strokeStyle = "rgba(255,255,255,0.045)"; for (const b of beats) { const x = nowX + (b - t) * PPS; if (x < 0 || x > W) continue; sx.beginPath(); sx.moveTo(x, top); sx.lineTo(x, H); sx.stroke(); } }
    sx.lineWidth = 1; sx.font = "11px system-ui"; sx.textBaseline = "middle"; sx.textAlign = "left";
    for (let s = 0; s < 6; s++) { const y = top + laneH * (s + 0.5); sx.strokeStyle = "rgba(255,255,255,0.07)"; sx.beginPath(); sx.moveTo(0, y); sx.lineTo(W, y); sx.stroke(); sx.fillStyle = COLORS[s]; sx.globalAlpha = 0.65; sx.fillText(LABELS[s], 7, y); sx.globalAlpha = 1; }

    sx.textAlign = "center"; sx.font = "bold 13px system-ui";
    for (const c of chords) {
      if (c.end < tStart || c.start > tEnd) continue;
      const x1 = nowX + (c.start - t) * PPS, x2 = nowX + (c.end - t) * PPS;
      const active = c.start <= t && t < c.end;
      sx.fillStyle = active ? "rgba(255,255,255,0.16)" : "rgba(255,255,255,0.035)";
      sx.fillRect(x1, 0, Math.max(2, x2 - x1), ribbon);
      sx.fillStyle = active ? "#ffffff" : "#8f99ab";
      if (x2 - x1 > 26) sx.fillText(c.name, (Math.max(x1, 0) + Math.min(x2, W)) / 2, ribbon / 2);
    }
    sx.strokeStyle = "rgba(255,255,255,0.10)"; sx.beginPath(); sx.moveTo(0, ribbon); sx.lineTo(W, ribbon); sx.stroke();

    const list = content === "melody" ? melodyNotes : content === "chords" ? harmonyNotes : allNotes;
    sx.font = "bold 11px system-ui"; sx.textAlign = "center";
    for (let i = Math.max(0, lower(list, tStart) - 30); i < list.length; i++) {
      const n = list[i]; if (n.start > tEnd) break; if (n.start + n.duration < tStart) continue;
      const rp = capo ? refret(n, capo) : { s: n.string - 1, f: n.fret };
      if (!rp) continue;
      const s = rp.s; if (s < 0 || s > 5) continue;
      const x = nowX + (n.start - t) * PPS, w = Math.max(11, n.duration * PPS);
      const y = top + laneH * (s + 0.5), h = laneH * 0.6;
      const active = n.start <= t && t <= n.start + n.duration;
      const mel = n.voice === "lead" || n.melody;
      const sf = rp.f - capo;                   // capo-relative fret a player frets
      sx.globalAlpha = active ? 1 : (mel ? 0.95 : 0.3);
      sx.fillStyle = active ? "#ffffff" : COLORS[s];
      roundRect(sx, x, y - h / 2, w, h, 4); sx.fill();
      sx.globalAlpha = 1;
      if ((active || mel) && w > 13) { sx.fillStyle = INK; sx.fillText(String(sf), x + Math.min(w / 2, 11), y); }
    }
    sx.textAlign = "left";
    sx.strokeStyle = NOWLINE; sx.lineWidth = 2; sx.beginPath(); sx.moveTo(nowX, 0); sx.lineTo(nowX, H); sx.stroke(); sx.lineWidth = 1;

    // chord panel — NOW shape + role + next 3 distinct chords WITH shapes
    const cur = chordAt(t);
    $("chordName").textContent = cur ? cur.name : "—";
    const fn = cur && (analysis.functions || {})[cur.name];
    $("chordRole").textContent = fn ? `${fn.roman || ""} — ${fn.role || ""}` : "";
    const diag = $("chordDiagram");
    const v = cur ? voicingOf(cur) : null;
    const key = (cur ? cur.name : "_") + "|" + capo;
    if (diag.dataset.k !== key) {
      diag.dataset.k = key;
      const label = (v && capo > 0) ? `<div class="shapeName">play <b>${v.name}</b></div>` : "";
      diag.innerHTML = v ? label + chordSVG(v, 116) : "";
    }
    const up = [];
    for (const c of chords) {
      if (c.start <= t || c.name === "silence" || c.name === "unknown") continue;
      if (up.length && up[up.length - 1].name === c.name) continue;
      up.push(c); if (up.length >= 3) break;
    }
    const nl = $("nextList");
    const sig = up.map((c) => c.name).join(",") + "|" + capo;
    if (nl.dataset.sig !== sig) {
      nl.dataset.sig = sig;
      nl.innerHTML = up.map((c) => {
        const vv = voicingOf(c);
        const shape = (capo > 0 && vv) ? `<span class="ni-shape">${vv.name}</span>` : "";
        return `<div class="nextItem">${chordSVG(vv, 48)}<div class="ni-meta"><b>${c.name}</b>${shape}</div></div>`;
      }).join("");
    }
  }

  function drawTab(t) {
    const [W, H] = frameSize(tabStage, tx);
    tx.clearRect(0, 0, W, H);
    const nowX = Math.round(W * NOW), pad = 44, laneH = (H - 2 * pad) / 6;
    tx.lineWidth = 1; tx.font = "13px ui-monospace, monospace"; tx.textBaseline = "middle";
    for (let s = 0; s < 6; s++) {
      const y = pad + laneH * (s + 0.5);
      tx.strokeStyle = "rgba(255,255,255,0.12)"; tx.beginPath(); tx.moveTo(0, y); tx.lineTo(W, y); tx.stroke();
      tx.textAlign = "left"; tx.fillStyle = COLORS[s]; tx.globalAlpha = 0.7; tx.fillText(LABELS[s], 9, y); tx.globalAlpha = 1;
    }
    if (beats.length) { tx.strokeStyle = "rgba(255,255,255,0.06)"; beats.forEach((b, i) => { if (i % 4) return; const x = nowX + (b - t) * PPS; if (x < 0 || x > W) return; tx.beginPath(); tx.moveTo(x, pad); tx.lineTo(x, H - pad); tx.stroke(); }); }
    const tStart = t - nowX / PPS, tEnd = t + (W - nowX) / PPS;
    tx.font = "bold 16px ui-monospace, monospace"; tx.textAlign = "center";
    for (let i = Math.max(0, lower(melodyNotes, tStart) - 6); i < melodyNotes.length; i++) {
      const n = melodyNotes[i]; if (n.start > tEnd) break; if (n.start < tStart - 1) continue;
      const rp = capo ? refret(n, capo) : { s: n.string - 1, f: n.fret };
      if (!rp) continue;
      const s = rp.s; if (s < 0 || s > 5) continue;
      const sf = rp.f - capo;
      const x = nowX + (n.start - t) * PPS, y = pad + laneH * (s + 0.5);
      const active = n.start <= t && t <= n.start + n.duration;
      tx.fillStyle = active ? "#ffffff" : COLORS[s];
      tx.fillText(String(sf), x, y);
    }
    tx.textAlign = "left";
    tx.strokeStyle = NOWLINE; tx.lineWidth = 2; tx.beginPath(); tx.moveTo(nowX, pad - 8); tx.lineTo(nowX, H - pad + 8); tx.stroke(); tx.lineWidth = 1;
  }

  function drawVocals(t) {
    const [W, H] = frameSize(vocalStage, vc);
    vc.clearRect(0, 0, W, H);
    if (!vocals.length && !vpitch.length) {
      vc.fillStyle = "#8f99ab"; vc.font = "14px system-ui"; vc.textAlign = "center"; vc.textBaseline = "middle";
      vc.fillText("No vocals detected for this song.", W / 2, H / 2); vc.textAlign = "left"; return;
    }
    const nowX = Math.round(W * NOW), padT = 16, padB = 16;
    const tStart = t - nowX / PPS, tEnd = t + (W - nowX) / PPS;

    let lo = 1e9, hi = -1e9;
    for (const n of vocals) { if (n.start > t + 3 || n.start + n.duration < t - 1) continue; lo = Math.min(lo, n.pitch); hi = Math.max(hi, n.pitch); }
    for (const p of vpitch) { if (p[1] == null || p[0] > t + 3 || p[0] < t - 1) continue; lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); }
    if (hi >= lo) {
      const tlo = lo - 3, thi = hi + 3, f = playing ? 0.08 : 1;
      vlo += (tlo - vlo) * f; vhi += (thi - vhi) * f;
    }
    const span = Math.max(4, vhi - vlo);
    const yOf = (p) => padT + (H - padT - padB) * (1 - (p - vlo) / span);
    const noteH = Math.min(22, Math.max(7, (H - padT - padB) / span * 0.85));
    const stepPx = yOf(vlo) - yOf(vlo + 1);

    vc.font = "10px system-ui"; vc.textBaseline = "middle";
    for (let p = Math.ceil(vlo); p <= vhi; p++) {
      const y = yOf(p), isC = (((p % 12) + 12) % 12) === 0;
      vc.strokeStyle = isC ? "rgba(255,255,255,0.14)" : "rgba(255,255,255,0.05)";
      vc.beginPath(); vc.moveTo(0, y); vc.lineTo(W, y); vc.stroke();
      if (stepPx >= 11 || isC) {
        vc.fillStyle = isC ? "#bbb" : "#666"; vc.textAlign = "left";
        vc.fillText(PC_NOTE[(((p % 12) + 12) % 12)] + (Math.floor(p / 12) - 1), 6, y);
      }
    }
    if (beats.length) { vc.strokeStyle = "rgba(255,255,255,0.04)"; for (const b of beats) { const x = nowX + (b - t) * PPS; if (x < 0 || x > W) continue; vc.beginPath(); vc.moveTo(x, padT); vc.lineTo(x, H - padB); vc.stroke(); } }

    vc.font = "bold 10px system-ui"; vc.textAlign = "center";
    let curName = null;
    for (let i = Math.max(0, lower(vocals, tStart) - 10); i < vocals.length; i++) {
      const n = vocals[i]; if (n.start > tEnd) break; if (n.start + n.duration < tStart) continue;
      const x = nowX + (n.start - t) * PPS, w = Math.max(8, n.duration * PPS), y = yOf(n.pitch);
      const active = n.start <= t && t <= n.start + n.duration;
      if (active) curName = n.name;
      vc.fillStyle = active ? "#ffd9a8" : "rgba(255,159,28,0.55)";
      roundRect(vc, x, y - noteH / 2, w, noteH, 3); vc.fill();
      if (w > 22 && noteH >= 11) { vc.fillStyle = INK; vc.fillText(n.name, x + Math.min(w / 2, 16), y); }
    }

    if (vpitch.length) {
      vc.strokeStyle = "#ffffff"; vc.lineWidth = 2; vc.beginPath();
      let pen = false;
      for (let i = 0; i < vpitch.length; i++) {
        const pt = vpitch[i], pt_t = pt[0], mid = pt[1];
        if (pt_t < tStart) continue; if (pt_t > tEnd) break;
        if (mid == null) { pen = false; continue; }
        const x = nowX + (pt_t - t) * PPS, y = yOf(mid);
        if (!pen) { vc.moveTo(x, y); pen = true; } else vc.lineTo(x, y);
      }
      vc.stroke(); vc.lineWidth = 1;
    }
    vc.textAlign = "left";
    vc.strokeStyle = NOWLINE; vc.lineWidth = 2; vc.beginPath(); vc.moveTo(nowX, padT); vc.lineTo(nowX, H - padB); vc.stroke(); vc.lineWidth = 1;
    if (curName) { vc.fillStyle = "#ff9f1c"; vc.font = "bold 22px system-ui"; vc.textAlign = "left"; vc.fillText(curName, nowX + 10, padT + 16); }
  }

  // ── Piano roll (tiles videos — exact notes, hand-colored) ─────────────────
  function drawRoll(t) {
    const [W, H] = frameSize(rollStage, rx);
    rx.clearRect(0, 0, W, H);
    if (!roll.length) return;
    const nowX = Math.round(W * NOW), padT = 14, padB = 14;
    const tStart = t - nowX / PPS, tEnd = t + (W - nowX) / PPS;

    // Auto-follow: window to what's actually playing right now (± a few
    // seconds), smoothly easing toward it — same idea as the vocals view, so
    // the roll zooms into the current register instead of showing the whole
    // song's span squashed flat.
    let lo = 1e9, hi = -1e9;
    for (const n of roll) { if (n.start > t + 4 || n.start + n.duration < t - 2) continue; lo = Math.min(lo, n.pitch); hi = Math.max(hi, n.pitch); }
    if (hi >= lo) {
      const tlo = lo - 3, thi = hi + 3, f = playing ? 0.06 : 1;
      rlo += (tlo - rlo) * f; rhi += (thi - rhi) * f;
    }
    const span = Math.max(8, rhi - rlo);
    const yOf = (p) => padT + (H - padT - padB) * (1 - (p - rlo) / span);
    const nh = Math.max(3, Math.min(16, (H - padT - padB) / span * 0.8));
    const stepPx = yOf(rlo) - yOf(rlo + 1);

    const KBW = 38;   // fixed keyboard strip along the left edge
    rx.font = "10px system-ui"; rx.textBaseline = "middle";
    for (let p = Math.ceil(rlo); p <= rhi; p++) {
      const isC = ((p % 12) + 12) % 12 === 0;
      const black = [1, 3, 6, 8, 10].includes(((p % 12) + 12) % 12);
      const y = yOf(p);
      if (black) { rx.fillStyle = "rgba(255,255,255,0.025)"; rx.fillRect(KBW, y - nh / 2, W - KBW, nh); }
      if (isC) { rx.strokeStyle = "rgba(255,255,255,0.12)"; rx.beginPath(); rx.moveTo(KBW, y + nh / 2); rx.lineTo(W, y + nh / 2); rx.stroke(); }
    }
    if (beats.length) { rx.strokeStyle = "rgba(255,255,255,0.05)"; for (const b of beats) { const x = nowX + (b - t) * PPS; if (x < KBW || x > W) continue; rx.beginPath(); rx.moveTo(x, padT); rx.lineTo(x, H - padB); rx.stroke(); } }

    const sounding = new Set();
    rx.save(); rx.beginPath(); rx.rect(KBW, 0, W - KBW, H); rx.clip();
    for (let i = Math.max(0, lower(roll, tStart) - 20); i < roll.length; i++) {
      const n = roll[i]; if (n.start > tEnd) break; if (n.start + n.duration < tStart) continue;
      const x = nowX + (n.start - t) * PPS, w = Math.max(6, n.duration * PPS), y = yOf(n.pitch);
      const active = n.start <= t && t <= n.start + n.duration;
      if (active) sounding.add(n.pitch);
      rx.fillStyle = active ? "#ffffff" : handColor(n.hand);
      roundRect(rx, x, y - nh / 2, w, nh, 3); rx.fill();
    }
    rx.restore();
    rx.strokeStyle = NOWLINE; rx.lineWidth = 2; rx.beginPath(); rx.moveTo(nowX, padT); rx.lineTo(nowX, H - padB); rx.stroke(); rx.lineWidth = 1;

    drawRollKeys(KBW, H, rlo, rhi, yOf, nh, stepPx, sounding);
    drawRollLegend(W);
  }

  const handColor = (h) => h === "left" ? "rgba(58,167,255,0.85)" : h === "right" ? "rgba(76,201,127,0.85)" : "rgba(255,159,28,0.8)";

  // Fixed piano strip down the left edge: one key per pitch row, lit while the
  // note is sounding at the now-line, so the roll reads like a keyboard.
  function drawRollKeys(KBW, H, rlo, rhi, yOf, nh, stepPx, sounding) {
    rx.fillStyle = "#0b0d10"; rx.fillRect(0, 0, KBW, H);
    rx.font = "9px system-ui"; rx.textBaseline = "middle"; rx.textAlign = "left";
    for (let p = Math.ceil(rlo); p <= rhi; p++) {
      const pc = ((p % 12) + 12) % 12, black = [1, 3, 6, 8, 10].includes(pc), isC = pc === 0;
      const y = yOf(p), lit = sounding.has(p);
      const kw = black ? KBW * 0.62 : KBW;
      rx.fillStyle = lit ? "#ffd166" : black ? "#181c22" : "#e8e8ea";
      rx.fillRect(0, y - nh / 2 + 0.5, kw, Math.max(1, nh - 1));
      if (!black) { rx.strokeStyle = "rgba(0,0,0,0.35)"; rx.beginPath(); rx.moveTo(0, y + nh / 2); rx.lineTo(kw, y + nh / 2); rx.stroke(); }
      // Label only when the row is tall enough to hold text — C octaves always,
      // so you never lose your place in the register.
      if (isC || stepPx >= 11) {
        rx.fillStyle = black ? "#8a8f98" : "#33363c";
        rx.fillText(PC_NOTE[pc] + (Math.floor(p / 12) - 1), black ? kw + 2 : 3, y);
      }
    }
    rx.strokeStyle = "rgba(255,255,255,0.15)"; rx.beginPath(); rx.moveTo(KBW + 0.5, 0); rx.lineTo(KBW + 0.5, H); rx.stroke();
  }

  function drawRollLegend(W) {
    const items = [["left", "left hand"], ["right", "right hand"], ["", "unassigned"]];
    rx.font = "10px system-ui"; rx.textBaseline = "middle"; rx.textAlign = "left";
    let x = W - 8;
    for (const [hand, label] of items.slice().reverse()) {
      const tw = rx.measureText(label).width;
      x -= tw; rx.fillStyle = "#8a8f98"; rx.fillText(label, x, 10);
      x -= 14; rx.fillStyle = handColor(hand); roundRect(rx, x, 6, 9, 8, 2); rx.fill();
      x -= 10;
    }
  }

  let lastName = "";
  function updateGrid(t) {
    const cur = chordAt(t), name = cur ? cur.name : "";
    if (name === lastName) return; lastName = name;
    for (const card of $("chordGrid").children) card.classList.toggle("active", card.dataset.name === name);
  }

  // ── Frame loop ────────────────────────────────────────────────────────────
  function frame() {
    requestAnimationFrame(frame);
    if (!tab) return;
    const lat = (actx && playing) ? (actx.outputLatency || actx.baseLatency || 0) * rate : 0;
    const t = songTime() - lat;
    if (playing && loopA != null && loopB != null && t >= loopB) seekTo(loopA);
    if (playing && metro && beats.length) {
      const ahead = ctx().currentTime;
      while (metroIdx < beats.length) { const bt = beats[metroIdx]; if (bt < t - 0.05) { metroIdx++; continue; } if (bt < t + 0.2) { click(ahead + (bt - t) / rate); metroIdx++; } else break; }
    }
    if (!playing && !dirty) return;
    dirty = false;
    if (view === "player") drawPlayer(t);
    else if (view === "roll") drawRoll(t);
    else if (view === "tab") drawTab(t);
    else if (view === "vocals") drawVocals(t);
    else if (view === "chords") updateGrid(t);
    drawWave(t);
    $("time").textContent = fmt(t) + " / " + fmt(duration);
  }

  // ── Wire-up ───────────────────────────────────────────────────────────────
  $("playBtn").onclick = toggle;
  window.addEventListener("resize", () => { dirty = true; });
  $("speedSel").onchange = (e) => { rate = parseFloat(e.target.value); if (playing) play(songTime()); };
  $("capoSel").onchange = (e) => { capo = parseInt(e.target.value, 10) || 0; updateCapoBadge(); buildChordGrid(); buildLearn(); buildOverview(); dirty = true; };

  function setView(v) {
    view = v;
    for (const b of $("viewSeg").children) b.classList.toggle("active", b.dataset.view === v);
    for (const id of ["overview", "player", "roll", "chords", "tab", "vocals", "learn"]) $(id + "View").classList.toggle("hidden", id !== v);
    lastName = ""; dirty = true;
  }
  $("viewSeg").onclick = (e) => { if (e.target.dataset.view) setView(e.target.dataset.view); };
  $("contentSeg").onclick = (e) => { const c = e.target.dataset.content; if (!c) return; content = c === "all" ? "both" : c; for (const b of $("contentSeg").children) b.classList.toggle("active", b.dataset.content === e.target.dataset.content); dirty = true; };

  $("loopBtn").onclick = () => {
    const btn = $("loopBtn");
    if (loopA == null) { loopA = songTime(); btn.textContent = "Set B"; btn.classList.add("on"); }
    else if (loopB == null) { loopB = songTime(); if (loopB < loopA) [loopA, loopB] = [loopB, loopA]; btn.textContent = `Loop ${fmt(loopA)}–${fmt(loopB)}`; }
    else { loopA = loopB = null; btn.textContent = "Loop"; btn.classList.remove("on"); }
    dirty = true;
  };
  $("metroBtn").onclick = () => { metro = !metro; $("metroBtn").classList.toggle("on", metro); };
  $("barsBtn").onclick = () => {
    barMode = !barMode;
    $("barsBtn").classList.toggle("on", barMode);
    chords = barMode ? quantizeToBars(chordsRaw) : chordsRaw;
    lastName = ""; buildChordGrid(); buildOverview(); buildLearn(); dirty = true;
  };

  // settings modal
  let settingsReprocessJobId = null;
  $("settingsBtn").onclick = () => {
    settingsReprocessJobId = null;
    $("runSettingsBtn").textContent = "Save (applies to next add)";
    $("settingsModal").classList.remove("hidden");
  };
  $("settingsClose").onclick = () => $("settingsModal").classList.add("hidden");
  $("settingsModal").onclick = (e) => { if (e.target.id === "settingsModal") $("settingsModal").classList.add("hidden"); };

  $("runSettingsBtn").onclick = async () => {
    $("settingsModal").classList.add("hidden");
    if (settingsReprocessJobId) {
      showOverlay("Reprocessing…", "");
      const r = await fetch(`/api/reprocess/${settingsReprocessJobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_beats: $("optBeats").checked,
          run_vocals: $("optVocals").checked,
          vocal_model: $("optVocalModel").value,
          // "" = keep the song's stored instrument. The sidebar dropdown is
          // NOT used here — it's for new adds and would silently force a
          // re-transcription on every reprocess.
          instrument: $("optInst").value
        })
      });
      if (!r.ok) {
        let d = ""; try { d = (await r.json()).detail; } catch (e) {}
        showOverlay("Reprocess failed", d || `HTTP ${r.status}`); return;
      }
      poll(settingsReprocessJobId);
    }
  };

  $("redoVocalsBtn").onclick = async () => {
    const id = settingsReprocessJobId || curJob;
    if (!id) { $("status").textContent = "Load a song first."; return; }
    $("settingsModal").classList.add("hidden");
    const r = await fetch(`/api/revocals/${id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vocal_model: $("optVocalModel").value }),
    });
    if (!r.ok) { $("status").textContent = "Vocals redo failed to start"; return; }
    $("status").textContent = "Redoing vocals in the background…";
    // quiet poll — no overlay, keep playing; reload data when done
    const tick = async () => {
      let s;
      try { s = await (await fetch(`/api/status/${id}`)).json(); }
      catch (e) { setTimeout(tick, 3000); return; }
      if (s.status === "done") {
        $("status").textContent = "";
        if (curJob === id) {                       // refresh vocals without touching playback
          const rr = await fetch(`/api/result/${id}`);
          if (rr.ok) {
            const t2 = await rr.json();
            vocals = (t2.vocals || []).slice().sort((a, b) => a.start - b.start);
            vpitch = t2.vocal_pitch || [];
            if (vocals.length) { const ps = vocals.map((n) => n.pitch); vlo = Math.min(...ps) - 3; vhi = Math.max(...ps) + 3; }
            $("viewSeg").querySelector('[data-view=vocals]').style.display = (vocals.length || vpitch.length) ? "" : "none";
            dirty = true;
          }
        }
      } else if (s.status === "error") {
        $("status").textContent = "Vocals redo failed: " + ((s.error || "").split("\n").pop() || "");
      } else setTimeout(tick, 2500);
    };
    setTimeout(tick, 2500);
  };

  // ── Tuning panel: every pipeline knob, editable in-app ────────────────────
  const TUNING_SECTIONS = {
    chord:   "Chords",
    chroma:  "Note detection (chroma)",
    arrange: "Lead / rhythm & note cleanup",
    vocals:  "Vocals",
    tiles:   "Piano-tiles video",
  };
  const TUNING_HINTS = {
    "chord.transition_penalty": "higher = chords change less often",
    "chord.complexity_penalty": "higher = plain maj/min preferred over maj7/7ths",
    "chord.thirdless_penalty": "higher = fewer sus2/sus4/5 chords",
    "chord.bass_bonus": "higher = trust the bass note for the root",
    "chord.gate_tau": "higher = color chords (7ths/sus) need clearer evidence",
    "chord.key_penalty": "higher = chords forced into the detected key",
    "chord.silence_threshold": "lower = quiet intros still get chords",
    "chord.no_chord_floor": "raise toward 0 = weak sections become 'unknown'",
    "chord.slash_bass_mass": "lower = more slash chords (G/B)",
    "chord.key_window_segs": "lower = adapts to key changes faster",
    "chord.miss_weight": "higher = off-chord notes hurt more",
    "chord.absent_weight": "higher = missing chord tones hurt more",
    "chord.absent_tau": "how loud a chord tone must be to count",
    "chroma.q_mult": "higher = cleaner note separation, slower (1.5–2.5)",
    "chroma.lateral_inhibition": "higher = less bleed between neighbor notes",
    "arrange.ghost_dur": "notes shorter than this (s) are dropped",
    "arrange.melody_min_dur": "min length (s) for a lead-line note",
    "arrange.min_chord_dur": "chord blips shorter than this (s) get merged",
    "arrange.lead_max_poly": "1 = strict single-note lead, 2 = allow doublestops",
    "arrange.skyline_gap_semitones": "lower = more strum-top notes join the lead",
    "arrange.harmonic_ghost_max_dur": "short off-chord notes under this (s) dropped",
    "vocals.crepe_model": "full = accurate, tiny = fast",
    "vocals.periodicity_threshold": "lower = catches quiet singing, more ghosts",
    "vocals.split_semitones": "higher = vibrato stays one note",
    "vocals.hold_seconds": "how long a pitch move must hold to be a new note",
    "vocals.gap_seconds": "silence longer than this (s) ends a note",
    "vocals.min_note_seconds": "vocal notes shorter than this are dropped",
    "tiles.sat_min": "lower if tiles are pastel-colored",
    "tiles.val_min": "lower for dark/dim videos",
    "tiles.artifact_margin_px": "raise if hit-line sparkles create junk notes",
    "tiles.min_note_ms": "shortest believable note from the video",
    "tiles.gap_close_ms": "flicker gaps shorter than this get bridged",
    "tiles.highlight_delta": "key grey-out sensitivity (no-tiles videos)",
    "tiles.white_center_frac": "how much of each white key is sampled",
    "tiles.keyboard_y_override": "manual keyboard top (px), 0 = auto",
    "tiles.leftmost_midi_override": "manual leftmost key MIDI, 0 = auto",
  };
  let tuningDefaults = null;

  function labelize(k) { return k.replace(/_/g, " "); }
  function renderTuning(defaults, current) {
    const body = $("tuningBody");
    body.innerHTML = Object.keys(TUNING_SECTIONS).map((sec) => {
      const rows = Object.entries(defaults[sec] || {}).map(([k, dv]) => {
        const cur = (current[sec] || {})[k];
        const val = cur === undefined ? dv : cur;
        const hint = TUNING_HINTS[sec + "." + k] || "";
        const input = (typeof dv === "string")
          ? `<select data-sec="${sec}" data-key="${k}">
               <option value="full" ${val === "full" ? "selected" : ""}>full</option>
               <option value="tiny" ${val === "tiny" ? "selected" : ""}>tiny</option>
             </select>`
          : `<input type="number" step="any" data-sec="${sec}" data-key="${k}" value="${val}" data-def="${dv}" />`;
        const changed = cur !== undefined && cur !== dv ? " changed" : "";
        return `<div class="tunRow${changed}"><span class="tunLbl" title="${sec}.${k}">${labelize(k)}</span>${input}<span class="tunHint">${hint}</span></div>`;
      }).join("");
      return `<h4 class="tunSec">${TUNING_SECTIONS[sec]}</h4>${rows}`;
    }).join("");
  }
  function collectTuning() {
    const out = {};
    $("tuningBody").querySelectorAll("[data-sec]").forEach((el) => {
      const sec = el.dataset.sec, k = el.dataset.key;
      out[sec] = out[sec] || {};
      out[sec][k] = el.tagName === "SELECT" ? el.value : parseFloat(el.value);
    });
    return out;
  }
  async function saveTuning() {
    const r = await fetch("/api/tuning", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectTuning()),
    });
    if (!r.ok) { $("status").textContent = "Tuning save failed"; return false; }
    return true;
  }
  $("tuningBtn").onclick = async () => {
    const d = await (await fetch("/api/tuning")).json();
    tuningDefaults = d.defaults;
    renderTuning(d.defaults, d.current || {});
    $("tuningModal").classList.remove("hidden");
  };
  $("tuningClose").onclick = () => $("tuningModal").classList.add("hidden");
  $("tuningModal").onclick = (e) => { if (e.target.id === "tuningModal") $("tuningModal").classList.add("hidden"); };
  $("tuningReset").onclick = () => { if (tuningDefaults) renderTuning(tuningDefaults, {}); };
  $("tuningSave").onclick = async () => { if (await saveTuning()) $("tuningModal").classList.add("hidden"); };
  $("tuningApply").onclick = async () => {
    if (!(await saveTuning())) return;
    $("tuningModal").classList.add("hidden");
    if (!curJob) { $("status").textContent = "Saved. Load a song to hear it applied."; return; }
    showOverlay("Reprocessing with new tuning…", "");
    const r = await fetch(`/api/reprocess/${curJob}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_beats: true, run_vocals: true, vocal_model: $("optVocalModel").value }),
    });
    if (!r.ok) { let dd = ""; try { dd = (await r.json()).detail; } catch (e) {} showOverlay("Reprocess failed", dd || `HTTP ${r.status}`); return; }
    poll(curJob);
  };

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT" || e.target.tagName === "BUTTON") return;
    if (e.code === "Space") { e.preventDefault(); toggle(); }
    else if (e.code === "ArrowRight") seekTo(songTime() + 5);
    else if (e.code === "ArrowLeft") seekTo(songTime() - 5);
  });

  async function init() {
    watchCanvas(stage); watchCanvas(tabStage); watchCanvas(vocalStage); watchCanvas(rollStage); watchCanvas(wave);
    requestAnimationFrame(frame);
    const jobs = await refreshJobs();
    const done = jobs.find((j) => j.status === "done");
    if (done) await loadJob(done.id);
    else $("status").textContent = "Add a song — paste a YouTube link or upload a file.";
  }
  init();
})();
