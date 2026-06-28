(() => {
  const $ = (id) => document.getElementById(id);
  const stage = $("stage"), sx = stage.getContext("2d");
  const tabStage = $("tabStage"), tx = tabStage.getContext("2d");

  // ── Data ──────────────────────────────────────────────────────────────────
  let tab = null, allNotes = [], melodyNotes = [], harmonyNotes = [], chords = [], beats = [], duration = 0;
  let view = "player", content = "both", capo = 0, useCapo = false;
  const voicingOf = (c) => (useCapo && capo > 0 && c.capoVoicing) ? c.capoVoicing : (c.voicing || null);

  // ── Web Audio (AudioContext.currentTime is the master clock) ──────────────
  let actx = null, buffer = null, src = null;
  let playing = false, t0ctx = 0, t0song = 0, paused = 0, rate = 1, seeking = false, dirty = true;
  let loopA = null, loopB = null;
  let metro = false, metroIdx = 0;

  const ctx = () => (actx || (actx = new (window.AudioContext || window.webkitAudioContext)()));
  const songTime = () => playing ? t0song + (ctx().currentTime - t0ctx) * rate : paused;

  function stopSrc() { if (src) { try { src.onended = null; src.stop(); } catch (e) {} src = null; } }
  function play(off) {
    ctx(); if (actx.state === "suspended") actx.resume();
    if (!buffer) return;
    stopSrc();
    off = Math.max(0, Math.min(off, duration));
    src = actx.createBufferSource();
    src.buffer = buffer; src.playbackRate.value = rate;
    src.connect(actx.destination);
    src.onended = () => { if (playing && songTime() >= duration - 0.06) { pause(); paused = 0; } };
    src.start(0, off);
    t0ctx = actx.currentTime; t0song = off; playing = true; metroIdx = 0;
    $("playBtn").textContent = "⏸";
  }
  function pause() { if (!playing) return; paused = songTime(); playing = false; stopSrc(); $("playBtn").textContent = "▶"; dirty = true; }
  const toggle = () => { if (buffer) (playing ? pause() : play(paused)); };
  const seekTo = (t) => { t = Math.max(0, Math.min(t, duration)); if (playing) play(t); else { paused = t; dirty = true; } };

  function click(when) {
    const o = actx.createOscillator(), g = actx.createGain();
    o.frequency.value = 1500; o.connect(g); g.connect(actx.destination);
    g.gain.setValueAtTime(0.0001, when);
    g.gain.exponentialRampToValueAtTime(0.5, when + 0.001);
    g.gain.exponentialRampToValueAtTime(0.0001, when + 0.05);
    o.start(when); o.stop(when + 0.06);
  }

  // ── Loading ───────────────────────────────────────────────────────────────
  async function refreshJobs(sel) {
    const j = await (await fetch("/api/jobs")).json();
    const s = $("jobSelect"); s.innerHTML = "";
    for (const job of j.jobs) {
      const o = document.createElement("option");
      o.value = job.id; o.textContent = `${job.name} · ${job.status}`;
      s.appendChild(o);
    }
    if (sel) s.value = sel;
    return j.jobs;
  }

  async function loadJob(id) {
    pause(); paused = 0; loopA = loopB = null; $("loopBtn").classList.remove("on");
    $("status").textContent = "Loading…";
    const r = await fetch(`/api/result/${id}`);
    if (!r.ok) { $("status").textContent = "Result not ready"; return; }
    tab = await r.json();
    allNotes = (tab.notes || []).slice().sort((a, b) => a.start - b.start);
    melodyNotes = allNotes.filter((n) => n.melody);
    harmonyNotes = allNotes.filter((n) => !n.melody);
    chords = (tab.chords || []).slice().sort((a, b) => a.start - b.start);
    beats = tab.beats || [];
    const m = tab.metadata || {};
    const last = allNotes.length ? allNotes[allNotes.length - 1] : null;
    duration = m.duration_sec || (last ? last.start + last.duration : 0);
    $("keyBadge").textContent = "key " + (m.key || "—");
    $("bpmBadge").textContent = (m.bpm ? Number(m.bpm).toFixed(0) : "—") + " bpm";
    capo = m.capo || 0;
    useCapo = capo > 0;
    $("capoBtn").textContent = capo > 0 ? `Capo ${capo}` : "No capo";
    $("capoBtn").classList.toggle("on", useCapo);
    $("capoBtn").disabled = capo === 0;

    ctx();
    const ab = await (await fetch(`/api/audio/${id}`)).arrayBuffer();
    buffer = await actx.decodeAudioData(ab);
    duration = Math.max(duration, buffer.duration);
    $("playBtn").disabled = false;
    buildChordGrid();
    $("status").textContent = "";
    dirty = true;
  }

  // ── Upload + poll ───────────────────────────────────────────────────────────
  $("fileInput").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    $("status").textContent = `Uploading ${f.name}…`;
    const fd = new FormData(); fd.append("file", f);
    const r = await fetch("/api/transcribe", { method: "POST", body: fd });
    if (!r.ok) { $("status").textContent = "Upload failed"; return; }
    poll((await r.json()).job_id);
  });
  async function poll(id) {
    const s = await (await fetch(`/api/status/${id}`)).json();
    if (s.status === "done") { $("status").textContent = ""; await refreshJobs(id); return loadJob(id); }
    if (s.status === "error") { $("status").textContent = "Error: " + (s.error || "").split("\n").pop(); return; }
    $("status").textContent = `Transcribing… ${s.stage || s.status} (a few minutes)`;
    setTimeout(() => poll(id), 2500);
  }

  // ── Chord diagram (SVG) ─────────────────────────────────────────────────────
  function chordSVG(v, size = 116) {
    if (!v || !v.frets) return "";
    const frets = v.frets, fretted = frets.filter((f) => f > 0);
    const maxF = fretted.length ? Math.max(...fretted) : 0;
    const minF = fretted.length ? Math.min(...fretted) : 0;
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
      else if (val === 0) s += `<circle cx="${x}" cy="${padT - size * 0.075}" r="${size * 0.045}" fill="none" stroke="#ff6a3d" stroke-width="1.5"/>`;
      else { const pos = open ? val : (val - startFret + 1); const y = padT + (pos - 0.5) * fy; s += `<circle cx="${x}" cy="${y}" r="${size * 0.072}" fill="#ff6a3d"/>`; }
    }
    return s + "</svg>";
  }

  function buildChordGrid() {
    const g = $("chordGrid"); g.innerHTML = "";
    // Unique chord shapes used in the song (deduped by name) — the set you
    // actually need to learn, not the 200-segment timeline.
    const seen = new Map(), count = new Map();
    for (const c of chords) {
      if (c.name === "silence" || c.name === "unknown") continue;
      count.set(c.name, (count.get(c.name) || 0) + 1);
      if (!seen.has(c.name)) seen.set(c.name, c);
    }
    for (const c of seen.values()) {
      const v = voicingOf(c);
      const shape = (useCapo && capo > 0 && v) ? `<div class="play">play ${v.name}</div>` : "";
      const d = document.createElement("div");
      d.className = "chordCard"; d.dataset.name = c.name;
      d.innerHTML = `<div class="cn">${c.name}</div>${chordSVG(v, 104)}${shape}<div class="ct">${count.get(c.name)}×</div>`;
      d.onclick = () => { const f = chords.find((x) => x.name === c.name); if (f) seekTo(f.start); };
      g.appendChild(d);
    }
  }

  // ── Helpers ─────────────────────────────────────────────────────────────────
  const LABELS = ["e", "B", "G", "D", "A", "E"];
  const COLORS = ["#ff6a3d", "#ff9f1c", "#ffd23f", "#4cc97f", "#3aa7ff", "#a06bff"]; // e B G D A E
  const NOWLINE = "#ffffff", INK = "#11131a";
  const PPS = 150, NOW = 0.24;
  const fmt = (s) => { s = Math.max(0, s | 0); return (s / 60 | 0) + ":" + String(s % 60).padStart(2, "0"); };
  function lower(arr, t) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m].start < t) lo = m + 1; else hi = m; } return lo; }
  function chordAt(t) { let cur = null; for (const c of chords) { if (c.start <= t && t < c.end) cur = c; if (c.start > t) break; } return cur; }
  function roundRect(c, x, y, w, h, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

  // Robust canvas sizing: a ResizeObserver sets the backing store from the
  // element's real box only when it changes — never a per-frame clientWidth
  // read (which can momentarily be 0 and corrupt the transform / now-line).
  const csize = new Map();
  function watchCanvas(cv) {
    const apply = () => {
      const dpr = window.devicePixelRatio || 1;
      const r = cv.getBoundingClientRect();
      const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
      const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
      if (cv.width !== bw || cv.height !== bh) { cv.width = bw; cv.height = bh; }
      csize.set(cv, { w, h, dpr });
      dirty = true;
    };
    new ResizeObserver(apply).observe(cv);
    apply();
  }
  function frameSize(cv, c) {
    const s = csize.get(cv) || { w: 1, h: 1, dpr: 1 };
    c.setTransform(s.dpr, 0, 0, s.dpr, 0, 0);
    return [s.w, s.h];
  }

  // ── Player canvas ─────────────────────────────────────────────────────────
  function drawPlayer(t) {
    const [W, H] = frameSize(stage, sx);
    sx.clearRect(0, 0, W, H);
    const nowX = Math.round(W * NOW), ribbon = 36, top = ribbon, laneH = (H - ribbon) / 6;

    // beat grid
    if (beats.length) { sx.strokeStyle = "rgba(255,255,255,0.045)"; for (const b of beats) { const x = nowX + (b - t) * PPS; if (x < 0 || x > W) continue; sx.beginPath(); sx.moveTo(x, top); sx.lineTo(x, H); sx.stroke(); } }

    // lanes
    sx.lineWidth = 1; sx.strokeStyle = "rgba(255,255,255,0.07)"; sx.font = "11px system-ui"; sx.textBaseline = "middle"; sx.textAlign = "left";
    for (let s = 0; s < 6; s++) { const y = top + laneH * (s + 0.5); sx.beginPath(); sx.moveTo(0, y); sx.lineTo(W, y); sx.stroke(); sx.fillStyle = COLORS[s]; sx.globalAlpha = 0.65; sx.fillText(LABELS[s], 7, y); sx.globalAlpha = 1; }

    // chord ribbon
    const tStart = t - nowX / PPS, tEnd = t + (W - nowX) / PPS;
    sx.textAlign = "center"; sx.font = "bold 13px system-ui";
    for (const c of chords) {
      if (c.end < tStart || c.start > tEnd) continue;
      const x1 = nowX + (c.start - t) * PPS, x2 = nowX + (c.end - t) * PPS;
      const active = c.start <= t && t < c.end;
      sx.fillStyle = active ? "rgba(255,106,61,0.20)" : "rgba(255,255,255,0.035)";
      sx.fillRect(x1, 0, Math.max(2, x2 - x1), ribbon);
      sx.fillStyle = active ? "#ff8a63" : "#8f99ab";
      if (x2 - x1 > 26) sx.fillText(c.name, (Math.max(x1, 0) + Math.min(x2, W)) / 2, ribbon / 2);
    }
    sx.strokeStyle = "rgba(255,255,255,0.10)"; sx.beginPath(); sx.moveTo(0, ribbon); sx.lineTo(W, ribbon); sx.stroke();

    // notes
    let list = content === "melody" ? melodyNotes : content === "chords" ? harmonyNotes : allNotes;
    sx.font = "bold 11px system-ui"; sx.textAlign = "center";
    for (let i = Math.max(0, lower(list, tStart) - 30); i < list.length; i++) {
      const n = list[i]; if (n.start > tEnd) break; if (n.start + n.duration < tStart) continue;
      const s = n.string - 1; if (s < 0 || s > 5) continue;
      const x = nowX + (n.start - t) * PPS, w = Math.max(11, n.duration * PPS);
      const y = top + laneH * (s + 0.5), h = laneH * 0.6;
      const active = n.start <= t && t <= n.start + n.duration;
      const mel = n.melody;
      sx.globalAlpha = active ? 1 : (mel ? 0.95 : 0.3);
      sx.fillStyle = active ? "#ffffff" : COLORS[s];
      roundRect(sx, x, y - h / 2, w, h, 4); sx.fill();
      sx.globalAlpha = 1;
      if ((active || mel) && w > 13) { sx.fillStyle = INK; sx.fillText(String(n.fret), x + Math.min(w / 2, 11), y); }
    }
    sx.textAlign = "left";

    // now-line
    sx.strokeStyle = NOWLINE; sx.lineWidth = 2; sx.beginPath(); sx.moveTo(nowX, 0); sx.lineTo(nowX, H); sx.stroke(); sx.lineWidth = 1;

    // chord panel
    const cur = chordAt(t);
    $("chordName").textContent = cur ? cur.name : "—";
    const diag = $("chordDiagram");
    const v = cur ? voicingOf(cur) : null;
    const key = (cur ? cur.name : "_") + "|" + useCapo;
    if (diag.dataset.k !== key) {
      diag.dataset.k = key;
      const label = (v && useCapo && capo > 0) ? `<div class="shapeName">play <b>${v.name}</b> · capo ${capo}</div>` : "";
      diag.innerHTML = v ? label + chordSVG(v, 150) : "";
    }
    const upcoming = chords.filter((c) => c.start > t).slice(0, 4);
    const nx = $("nextChords");
    const sig = upcoming.map((c) => c.name + c.start.toFixed(1)).join();
    if (nx.dataset.sig !== sig) {
      nx.dataset.sig = sig;
      nx.innerHTML = upcoming.map((c) => `<div class="nextChip"><b>${c.name}</b><span class="t">${fmt(c.start)}</span></div>`).join("");
    }
  }

  // ── Tab (scrolling melody tablature) ─────────────────────────────────────────
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
    // measure bars
    if (beats.length) { tx.strokeStyle = "rgba(255,255,255,0.06)"; beats.forEach((b, i) => { if (i % 4) return; const x = nowX + (b - t) * PPS; if (x < 0 || x > W) return; tx.beginPath(); tx.moveTo(x, pad); tx.lineTo(x, H - pad); tx.stroke(); }); }
    const tStart = t - nowX / PPS, tEnd = t + (W - nowX) / PPS;
    tx.font = "bold 16px ui-monospace, monospace"; tx.textAlign = "center";
    for (let i = Math.max(0, lower(melodyNotes, tStart) - 6); i < melodyNotes.length; i++) {
      const n = melodyNotes[i]; if (n.start > tEnd) break; if (n.start < tStart - 1) continue;
      const s = n.string - 1; if (s < 0 || s > 5) continue;
      const x = nowX + (n.start - t) * PPS, y = pad + laneH * (s + 0.5);
      const active = n.start <= t && t <= n.start + n.duration;
      tx.fillStyle = active ? "#ffffff" : COLORS[s];
      tx.fillText(String(n.fret), x, y);
    }
    tx.textAlign = "left";
    tx.strokeStyle = NOWLINE; tx.lineWidth = 2; tx.beginPath(); tx.moveTo(nowX, pad - 8); tx.lineTo(nowX, H - pad + 8); tx.stroke(); tx.lineWidth = 1;
  }

  // ── Chords grid highlight ────────────────────────────────────────────────────
  let lastName = "";
  function updateGrid(t) {
    const cur = chordAt(t), name = cur ? cur.name : "";
    if (name === lastName) return;
    lastName = name;
    for (const card of $("chordGrid").children) card.classList.toggle("active", card.dataset.name === name);
  }

  // ── Main loop ────────────────────────────────────────────────────────────────
  function frame() {
    requestAnimationFrame(frame);
    if (!tab) return;
    // Compensate for audio output latency so the sounding note sits on the line.
    const lat = (actx && playing) ? (actx.outputLatency || actx.baseLatency || 0) * rate : 0;
    const t = songTime() - lat;
    // loop
    if (playing && loopA != null && loopB != null && t >= loopB) { seekTo(loopA); }
    // metronome
    if (playing && metro && beats.length) {
      const ahead = ctx().currentTime;
      while (metroIdx < beats.length) {
        const bt = beats[metroIdx];
        if (bt < t - 0.05) { metroIdx++; continue; }
        if (bt < t + 0.2) { click(ahead + (bt - t) / rate); metroIdx++; } else break;
      }
    }
    // Idle when paused and nothing changed, so the page can settle.
    if (!playing && !dirty) return;
    dirty = false;
    if (view === "player") drawPlayer(t);
    else if (view === "tab") drawTab(t);
    else updateGrid(t);
    if (!seeking) $("seek").value = duration ? Math.round(t / duration * 1000) : 0;
    $("time").textContent = fmt(t) + " / " + fmt(duration);
  }

  // ── Wire-up ──────────────────────────────────────────────────────────────────
  $("playBtn").onclick = toggle;
  $("seek").addEventListener("input", () => { seeking = true; const tt = $("seek").value / 1000 * duration; $("time").textContent = fmt(tt) + " / " + fmt(duration); if (!playing) paused = tt; dirty = true; });
  $("seek").addEventListener("change", () => { seeking = false; seekTo($("seek").value / 1000 * duration); });
  window.addEventListener("resize", () => { dirty = true; });
  $("jobSelect").onchange = (e) => loadJob(e.target.value);
  $("speedSel").onchange = (e) => { rate = parseFloat(e.target.value); if (playing) play(songTime()); };

  function setView(v) { view = v; for (const b of $("viewSeg").children) b.classList.toggle("active", b.dataset.view === v); $("playerView").classList.toggle("hidden", v !== "player"); $("chordsView").classList.toggle("hidden", v !== "chords"); $("tabView").classList.toggle("hidden", v !== "tab"); lastName = ""; dirty = true; }
  $("viewSeg").onclick = (e) => { if (e.target.dataset.view) setView(e.target.dataset.view); };
  $("contentSeg").onclick = (e) => { const c = e.target.dataset.content; if (!c) return; content = c === "all" ? "both" : c; for (const b of $("contentSeg").children) b.classList.toggle("active", b.dataset.content === e.target.dataset.content); dirty = true; };

  $("loopBtn").onclick = () => {
    const btn = $("loopBtn");
    if (loopA == null) { loopA = songTime(); btn.textContent = "Set B"; btn.classList.add("on"); }
    else if (loopB == null) { loopB = songTime(); if (loopB < loopA) [loopA, loopB] = [loopB, loopA]; btn.textContent = `Loop ${fmt(loopA)}–${fmt(loopB)}`; }
    else { loopA = loopB = null; btn.textContent = "Loop"; btn.classList.remove("on"); }
  };
  $("metroBtn").onclick = () => { metro = !metro; $("metroBtn").classList.toggle("on", metro); };
  $("capoBtn").onclick = () => {
    if (capo === 0) return;
    useCapo = !useCapo;
    $("capoBtn").textContent = useCapo ? `Capo ${capo}` : "No capo";
    $("capoBtn").classList.toggle("on", useCapo);
    buildChordGrid();
    dirty = true;
  };

  document.addEventListener("keydown", (e) => {
    if (e.code === "Space") { e.preventDefault(); toggle(); }
    else if (e.code === "ArrowRight") seekTo(songTime() + 5);
    else if (e.code === "ArrowLeft") seekTo(songTime() - 5);
  });

  async function init() {
    watchCanvas(stage);
    watchCanvas(tabStage);
    requestAnimationFrame(frame);
    const jobs = await refreshJobs();
    const done = jobs.find((j) => j.status === "done");
    if (done) { $("jobSelect").value = done.id; await loadJob(done.id); }
    else $("status").textContent = "Upload a song to begin.";
  }
  init();
})();
