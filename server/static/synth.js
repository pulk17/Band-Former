/* Band-Former — instrument synthesis.
 *
 * Renders one AudioBuffer per pitch per instrument so the transcription can be
 * played back and judged by ear. Everything is generated in the browser: no
 * samples to download, no library, nothing to build.
 *
 * Guitar is an extended Karplus-Strong plucked string — a delay line the length
 * of one period, fed back through a lowpass. That IS the physics of a vibrating
 * string, which is why 40 lines of it sounds more like a guitar than a megabyte
 * of oscillators. The loop filter doubles as the fractional delay, so pitch
 * lands within a cent instead of being quantised to whole samples (at high
 * frets integer-only delays are audibly sharp).
 *
 * Piano is additive with the two things that actually make a piano sound like
 * one: inharmonic partials (real strings are stiff, so overtones stretch sharp)
 * and a hammer strike point that notches out one partial in every eight.
 *
 * Buffers are cached per (instrument, pitch) and played at rate 1.0 — never
 * resampled — so playback speed changes can't detune the instrument.
 */
(function () {
  "use strict";

  const cache = new Map();               // "guitar:64" -> AudioBuffer
  const midiToHz = (m) => 440 * Math.pow(2, (m - 69) / 12);

  // Deterministic noise: a fixed seed means the same note sounds identical every
  // time, so an A/B between two transcriptions differs only where the notes do.
  function rng(seed) {
    let s = seed >>> 0 || 1;
    return () => {
      s ^= s << 13; s >>>= 0;
      s ^= s >> 17;
      s ^= s << 5; s >>>= 0;
      return s / 4294967296 * 2 - 1;
    };
  }

  // ── Guitar: extended Karplus-Strong ───────────────────────────────────────
  function renderGuitar(sr, midi, velocity) {
    const f0 = midiToHz(midi);
    const period = sr / f0;
    // The one-zero loop filter y = (1-S)·d[n-D] + S·d[n-D-1] delays by D + S,
    // so the fractional part of the period lives in S — that's the tuning.
    let D = Math.floor(period), S = period - D;
    if (S < 0.05) { D -= 1; S += 1; }    // keep S away from 0 where the filter
    if (S > 0.95) { S = 0.95; }          // stops lowpassing and the string rings dead

    const seconds = Math.min(3.2, 1.4 + 240 / f0);
    const len = Math.floor(sr * seconds);
    const y = new Float32Array(len);

    // Decay: pick the loop gain so the string falls 60 dB in `t60` seconds.
    // Low strings ring far longer than high ones on a real instrument.
    const t60 = Math.min(3.0, 0.9 + 200 / f0);
    const g = Math.pow(10, -3 / (f0 * t60));

    // Excitation — one period of noise, lowpassed by how hard it was picked.
    // A soft pluck is duller: that's the single biggest dynamics cue.
    const rand = rng(midi * 2654435761);
    const bright = 0.28 + 0.5 * velocity;
    const exc = new Float32Array(D + 2);
    let lp = 0;
    for (let i = 0; i < exc.length; i++) {
      lp += bright * (rand() - lp);
      exc[i] = lp;
    }
    // Plucking at a point kills the harmonics with a node there — comb-filter
    // the excitation to place the pick about a fifth of the way along.
    const pick = Math.max(1, Math.round(0.19 * D));
    for (let i = exc.length - 1; i >= pick; i--) exc[i] -= 0.72 * exc[i - pick];

    for (let n = 0; n < len; n++) {
      const e = n < exc.length ? exc[n] : 0;
      const a = n - D >= 0 ? y[n - D] : 0;
      const b = n - D - 1 >= 0 ? y[n - D - 1] : 0;
      y[n] = e + g * ((1 - S) * a + S * b);
    }

    bodyResonance(y, sr);
    fadeEnds(y, sr);
    normalize(y, 0.5 + 0.45 * velocity);
    return y;
  }

  // Two lightly-damped resonators standing in for the body of the instrument.
  // Without them a Karplus-Strong string sounds like a rubber band.
  function bodyResonance(y, sr) {
    const out = new Float32Array(y.length);
    for (const [freq, q, mix] of [[97, 12, 0.18], [196, 14, 0.10]]) {
      const w = 2 * Math.PI * freq / sr, r = 1 - w / (2 * q);
      const a1 = 2 * r * Math.cos(w), a2 = -r * r;
      let z1 = 0, z2 = 0;
      for (let n = 0; n < y.length; n++) {
        const v = y[n] + a1 * z1 + a2 * z2;
        z2 = z1; z1 = v;
        out[n] += mix * v * (1 - r);
      }
    }
    for (let n = 0; n < y.length; n++) y[n] += out[n];
  }

  // ── Piano: inharmonic additive + hammer noise ─────────────────────────────
  function renderPiano(sr, midi, velocity) {
    const f0 = midiToHz(midi);
    const seconds = Math.min(4.0, 1.6 + 320 / f0);
    const len = Math.floor(sr * seconds);
    const y = new Float32Array(len);

    // Inharmonicity: stiff strings stretch overtones sharp, more so on the
    // short thick bass strings. Get this wrong and it sounds like an organ.
    const B = 0.00008 * Math.pow(2, (60 - midi) / 16) + 0.00002;
    const nyq = sr / 2;
    // Hammers strike about 1/8 along the string, which silences every 8th
    // partial — the notch is a big part of "piano" versus "generic struck tone".
    const hammer = 1 / 8;
    const t60 = Math.min(9, 1.2 + 480 / f0) * (0.8 + 0.4 * velocity);

    for (let k = 1; k <= 48; k++) {
      const fk = f0 * k * Math.sqrt(1 + B * k * k);
      if (fk >= nyq * 0.95) break;
      const amp = Math.abs(Math.sin(Math.PI * k * hammer)) / Math.pow(k, 1.18)
                * (0.35 + 0.65 * Math.pow(velocity, 0.6 + 0.05 * k));
      if (amp < 1e-4) continue;
      // Higher partials die first, and each note has a fast early decay over a
      // slow tail (two coupled strings) rather than one clean exponential.
      const tau = t60 / (1 + 0.55 * (k - 1)) / 6.9;
      const tauFast = tau * 0.28;
      // Three strings per note, minutely detuned — the beating is what stops it
      // sounding synthetic.
      for (const [det, w] of [[0, 0.6], [-0.0006, 0.2], [0.0007, 0.2]]) {
        const w0 = 2 * Math.PI * fk * (1 + det) / sr;
        const ph = (k * 0.37 + midi * 0.11) % (2 * Math.PI);
        for (let n = 0; n < len; n++) {
          const t = n / sr;
          const env = 0.72 * Math.exp(-t / tau) + 0.28 * Math.exp(-t / tauFast);
          if (env < 1e-4) break;
          y[n] += w * amp * env * Math.sin(w0 * n + ph);
        }
      }
    }

    // Hammer/key noise: a short filtered click at the onset.
    const rand = rng(midi * 40503 + 7);
    let lp = 0;
    const nlen = Math.floor(sr * 0.012);
    for (let n = 0; n < nlen && n < len; n++) {
      lp += 0.5 * (rand() - lp);
      y[n] += lp * 0.12 * velocity * Math.exp(-n / (sr * 0.003));
    }

    fadeEnds(y, sr);
    normalize(y, 0.45 + 0.5 * velocity);
    return y;
  }

  function fadeEnds(y, sr) {
    const f = Math.min(Math.floor(sr * 0.04), y.length >> 1);
    for (let i = 0; i < f; i++) y[y.length - 1 - i] *= i / f;   // no click on cutoff
  }

  function normalize(y, target) {
    let peak = 0;
    for (let i = 0; i < y.length; i++) { const a = Math.abs(y[i]); if (a > peak) peak = a; }
    if (peak < 1e-6) return;
    const g = target / peak;
    for (let i = 0; i < y.length; i++) y[i] *= g;
  }

  // ── Public API ────────────────────────────────────────────────────────────
  function noteBuffer(actx, instrument, midi, velocity) {
    midi = Math.max(21, Math.min(108, Math.round(midi)));
    const vel = Math.max(0.15, Math.min(1, velocity == null ? 0.75 : velocity));
    const key = instrument + ":" + midi + ":" + Math.round(vel * 4);
    let buf = cache.get(key);
    if (buf) return buf;
    const data = instrument === "piano"
      ? renderPiano(actx.sampleRate, midi, vel)
      : renderGuitar(actx.sampleRate, midi, vel);
    // Rendered at the context's own sample rate, so it plays back at rate 1.0
    // and can't be detuned by resampling.
    buf = actx.createBuffer(1, data.length, actx.sampleRate);
    buf.copyToChannel(data, 0);
    cache.set(key, buf);
    return buf;
  }

  /** Render a set of pitches ahead of time, a few per frame so the UI stays
   *  responsive. Rendering mid-playback would drop frames. */
  function prepare(actx, instrument, midis, onProgress) {
    const todo = [...new Set(midis.map((m) => Math.round(m)))];
    let i = 0;
    return new Promise((resolve) => {
      const step = () => {
        const until = performance.now() + 12;
        while (i < todo.length && performance.now() < until) {
          noteBuffer(actx, instrument, todo[i++], 0.75);
        }
        if (onProgress) onProgress(i / todo.length);
        if (i < todo.length) requestAnimationFrame(step);
        else resolve();
      };
      step();
    });
  }

  window.BFSynth = { noteBuffer, prepare, clear: () => cache.clear() };
})();
