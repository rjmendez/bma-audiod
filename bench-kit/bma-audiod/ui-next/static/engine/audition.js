/* ui-lab audition engine. Pure Web Audio, no dependencies. The same chain builder serves live playback (AudioContext) and offline rendering
   (OfflineAudioContext: level matching, pitch-preserving slow playback, tests).
   Chain:  8-channel input -> per-beam gain (solo / mix weights) -> sum -> band-pass of the selected time-frequency box (2 x HP + 2 x LP biquads)
           -> optional heterodyne (multiply by a sine, low-pass the difference) or frequency division (worklet) -> master gain -> safety limiter.
   The soft-clip limiter stays in every chain: boosting a -60 dBFS field recording by 40 dB must never reach the ears unlimited. */
(function (root) {
  'use strict';
  const NB = 8, LIMIT_DB_MAX = 40, BAND_PAD = 1.25, LIVE_PREBUFFER = 8640;       // 180 ms: 120 ms underran once in 3.7 s in Firefox at 100 percent machine load

  // Web Audio's Q for lowpass/highpass is in dB (resonance), not a linear Q: Butterworth (Q = 1/sqrt 2) is -3.0103 dB.
  const BUTTERWORTH_DB = 20 * Math.log10(Math.SQRT1_2);
  function biquad(ctx, type, f, qDb) { const b = ctx.createBiquadFilter(); b.type = type; b.frequency.value = f; b.Q.value = qDb == null ? BUTTERWORTH_DB : qDb; return b; }

  /* cfg: {weights: number[8], band: [f0, f1] | null, mode: {kind: 'normal'|'hetero'|'divide', target: Hz, div: N}, gain: linear}  ->  {input, output, beamGains, master, nodes} */
  function buildChain(ctx, cfg) {
    const input = ctx.createGain(); input.channelCount = NB; input.channelCountMode = 'explicit'; input.channelInterpretation = 'discrete';
    const split = ctx.createChannelSplitter(NB); input.connect(split);
    const mix = ctx.createGain(); mix.channelCount = 1; mix.channelCountMode = 'explicit'; mix.channelInterpretation = 'speakers';
    const beamGains = [];
    for (let b = 0; b < NB; b++) { const g = ctx.createGain(); g.gain.value = cfg.weights[b]; split.connect(g, b, 0); g.connect(mix); beamGains.push(g); }
    let node = mix; const nodes = [];
    const chain = (n) => { node.connect(n); node = n; nodes.push(n); };
    const band = cfg.band, mode = cfg.mode || { kind: 'normal' };
    // corners sit BAND_PAD outside the box so its edges are -3 dB points (two 2nd-order Butterworth stages per side) and the inside is flat
    if (band) { const hp = band[0] / BAND_PAD, lp = Math.min(band[1] * BAND_PAD, ctx.sampleRate * 0.45); chain(biquad(ctx, 'highpass', hp)); chain(biquad(ctx, 'highpass', hp)); chain(biquad(ctx, 'lowpass', lp)); chain(biquad(ctx, 'lowpass', lp)); }
    if (mode.kind === 'hetero' && band) {
      const centre = Math.sqrt(band[0] * band[1]), target = mode.target || 1500, lo = Math.max(1, centre - target);
      const het = ctx.createGain(); het.gain.value = 0; const osc = ctx.createOscillator(); osc.type = 'sine'; osc.frequency.value = lo; osc.connect(het.gain); osc.start();
      chain(het); const post = (band[1] - band[0]) / 2 + target + 200; chain(biquad(ctx, 'lowpass', post)); chain(biquad(ctx, 'lowpass', post));
      const comp = ctx.createGain(); comp.gain.value = 2; chain(comp);                      // multiplying by a unit sine halves the difference tone
      nodes.push(osc);
    } else if (mode.kind === 'divide') {
      const w = new root.AudioWorkletNode(ctx, 'freqdiv', { parameterData: { div: mode.div || 8 } }); chain(w); chain(biquad(ctx, 'lowpass', 6000));
    }
    const master = ctx.createGain(); master.gain.value = cfg.gain == null ? 1 : cfg.gain; chain(master);
    const lim = ctx.createWaveShaper(); lim.curve = softClipCurve(); lim.oversample = 'none'; chain(lim);       // 'none': an oversampling filter would overshoot +-1.0 by 8 percent
    return { input, output: lim, beamGains, master, nodes };
  }

  /* Safety limiter: identity below 0.8, smooth saturation above, never beyond +-1.0. (A DynamicsCompressorNode would add automatic makeup gain: +3.4 dB at
     -6 dB / 20:1, measured, which silently falsifies every level.) */
  const CLIP_KNEE = 0.8;
  function softClipCurve() {
    const n = 4097, c = new Float32Array(n);
    for (let i = 0; i < n; i++) { const x = (i / (n - 1)) * 2 - 1, a = Math.abs(x); c[i] = a <= CLIP_KNEE ? x : Math.sign(x) * (CLIP_KNEE + (1 - CLIP_KNEE) * Math.tanh((a - CLIP_KNEE) / (1 - CLIP_KNEE))); }
    return c;
  }

  const WORKLET_URL = (root.document && root.document.baseURI ? new URL('engine/worklets.js', root.document.baseURI).href : '/engine/worklets.js');   // next to the page: / on the mock server, /next/ under --ui-next

  /* Offline render of the selection through the chain with gain 1 -> mono AudioBuffer (the unlimited, un-boosted result when cfg.gain is 1). */
  async function renderSelection(buffer, t0, t1, cfg, workletUrl) {
    const fs = buffer.sampleRate, n = Math.max(1, Math.round((t1 - t0) * fs)), off = new root.OfflineAudioContext(1, n, fs);
    if (cfg.mode && cfg.mode.kind === 'divide') await off.audioWorklet.addModule(workletUrl || WORKLET_URL);
    const src = off.createBufferSource(); src.buffer = buffer;
    const ch = buildChain(off, cfg); src.connect(ch.input); ch.output.connect(off.destination); src.start(0, t0, t1 - t0);
    return off.startRendering();
  }
  function rmsOf(buf) { const d = buf.getChannelData(0); let s = 0; for (let i = 0; i < d.length; i++) s += d[i] * d[i]; return Math.sqrt(s / Math.max(1, d.length)); }
  /* gain that brings the rendered selection to targetDbfs RMS, capped at +LIMIT_DB_MAX dB (never an unbounded boost of a silent box) */
  function levelGain(rms, targetDbfs) { if (!(rms > 1e-9)) return 1; return Math.min(Math.pow(10, LIMIT_DB_MAX / 20), Math.pow(10, targetDbfs / 20) / rms); }

  function toWav16(buf) {
    const n = buf.length, c = buf.numberOfChannels, fs = buf.sampleRate, out = new DataView(new ArrayBuffer(44 + n * c * 2)), w = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); out.setUint32(4, 36 + n * c * 2, true); w(8, 'WAVEfmt '); out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, c, true); out.setUint32(24, fs, true); out.setUint32(28, fs * c * 2, true); out.setUint16(32, c * 2, true); out.setUint16(34, 16, true); w(36, 'data'); out.setUint32(40, n * c * 2, true);
    const ch = []; for (let k = 0; k < c; k++) ch.push(buf.getChannelData(k));
    for (let i = 0, o = 44; i < n; i++) for (let k = 0; k < c; k++, o += 2) out.setInt16(o, Math.max(-32768, Math.min(32767, Math.round(ch[k][i] * 32767))), true);
    return new Blob([out.buffer], { type: 'audio/wav' });
  }

  class Audition {
    constructor(o) {
      o = o || {}; this.workletUrl = o.workletUrl || WORKLET_URL;
      this.ctx = null; this.buffer = null; this.src = null; this.chain = null; this.live = null;
      this.cfg = { weights: new Array(NB).fill(0).map((_, i) => i === 0 ? 1 : 0), band: null, mode: { kind: 'normal', target: 1500, div: 8 }, gain: 1 };
      this.loop = null; this.rate = 1; this.levelTarget = -30; this.autoLevel = true;
      this.playing = false; this.startedAt = 0; this.offset = 0; this.media = null; this.listening = false;
    }
    async ensure() {
      if (!this.ctx) { this.ctx = new root.AudioContext({ latencyHint: 'interactive', sampleRate: 48000 }); await this.ctx.audioWorklet.addModule(this.workletUrl); }
      if (this.ctx.state !== 'running') await this.ctx.resume();     // must be called from a user gesture on iOS Safari and (before activation) in Chrome
      return this.ctx;
    }
    load(buffer) { this.stop(); this.buffer = buffer; this.offset = 0; }
    setWeights(w) { this.cfg.weights = Array.from(w); if (this.chain) this.chain.beamGains.forEach((g, i) => { g.gain.value = this.cfg.weights[i]; }); }
    soloBeam(b) { this.setWeights(this.cfg.weights.map((_, i) => i === b ? 1 : 0)); }
    mixBeams(list) { const w = new Array(NB).fill(0); for (const b of list) w[b] = 1 / Math.max(1, list.length); this.setWeights(w); }
    setBand(b) { this.cfg.band = b ? [Math.min(b[0], b[1]), Math.max(b[0], b[1])] : null; this._restartIfPlaying(); }
    setMode(m) { this.cfg.mode = Object.assign({}, this.cfg.mode, m); this._restartIfPlaying(); }
    setLoop(t0, t1) { this.loop = t1 > t0 ? [t0, t1] : null; this._restartIfPlaying(); }
    setRate(r) { this.rate = r; if (this.src) this.src.playbackRate.value = r; }
    setGain(g) { this.cfg.gain = g; if (this.chain) this.chain.master.gain.value = g; }
    get position() {
      if (!this.playing) return this.offset;
      const t = this.offset + (this.ctx.currentTime - this.startedAt) * this.rate;
      if (this.loop) { const L = this.loop[1] - this.loop[0]; return this.loop[0] + (((t - this.loop[0]) % L) + L) % L; }
      return Math.min(t, this.buffer ? this.buffer.duration : t);
    }
    async _levelMatch() {
      if (!this.autoLevel || !this.buffer) return;
      const t0 = this.loop ? this.loop[0] : Math.max(0, this.offset), t1 = this.loop ? this.loop[1] : Math.min(this.buffer.duration, t0 + 3);
      const r = await renderSelection(this.buffer, t0, t1, Object.assign({}, this.cfg, { gain: 1 }), this.workletUrl);
      this.cfg.gain = levelGain(rmsOf(r), this.levelTarget);
    }
    async play(from) {
      if (!this.buffer) return; await this.ensure(); this.stopSource();
      if (from != null) this.offset = from; else if (this.loop && (this.offset < this.loop[0] || this.offset >= this.loop[1])) this.offset = this.loop[0];
      await this._levelMatch();
      const ctx = this.ctx, src = ctx.createBufferSource(); src.buffer = this.buffer; src.playbackRate.value = this.rate;
      if (this.loop) { src.loop = true; src.loopStart = this.loop[0]; src.loopEnd = this.loop[1]; }
      const ch = buildChain(ctx, this.cfg); src.connect(ch.input); ch.output.connect(ctx.destination);
      src.start(0, this.offset); this.src = src; this.chain = ch; this.startedAt = ctx.currentTime; this.playing = true;
      src.onended = () => { if (this.src === src && !src.loop) { this.playing = false; this.offset = this.buffer.duration; } };
    }
    stopSource() { if (this.src) { try { this.src.onended = null; this.src.stop(); } catch (e) { /* already stopped */ } } this.src = null; if (this.chain) { try { this.chain.output.disconnect(); } catch (e) { /* ok */ } } this.chain = null; }
    pause() { if (this.playing) { this.offset = this.position; this.playing = false; } this.stopSource(); if (this.media) { this.media.pause(); this.media = null; } }
    stop() { this.pause(); this.offset = 0; }
    seek(t) { const was = this.playing; this.offset = t; if (was) this.play(t); }
    _restartIfPlaying() { if (this.playing) this.play(this.position); }
    /* slow playback that keeps the pitch: render the selection through the chain, play it from an <audio> element (preservesPitch) */
    async playPreserved(rate) {
      if (!this.buffer) return null; const t0 = this.loop ? this.loop[0] : 0, t1 = this.loop ? this.loop[1] : this.buffer.duration;
      await this._levelMatch(); const r = await renderSelection(this.buffer, t0, t1, this.cfg, this.workletUrl);
      this.pause(); const a = new root.Audio(root.URL.createObjectURL(toWav16(r))); a.preservesPitch = true; a.playbackRate = rate; a.loop = !!this.loop; this.media = a; await a.play(); return a;
    }
    /* live PCM (opt-in per session, indicator in the UI, nothing is recorded or stored here) */
    async startLive() {
      await this.ensure(); this.stopLive(); this.pause();
      const node = new root.AudioWorkletNode(this.ctx, 'pcmring', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [NB], processorOptions: { channels: NB, capacity: 48000, prebuffer: LIVE_PREBUFFER } });
      const ch = buildChain(this.ctx, Object.assign({}, this.cfg, { gain: this.autoLevel ? 1 : this.cfg.gain })); node.connect(ch.input); ch.output.connect(this.ctx.destination);
      this.live = { node, chain: ch, stats: { fill: 0, under: 0, over: 0 } }; node.port.onmessage = (e) => { this.live.stats = e.data; }; this.listening = true; this.chain = ch;
    }
    feedLive(planar, frames) { if (this.live) this.live.node.port.postMessage({ frames, data: planar }, [planar.buffer]); }
    stopLive() { if (this.live) { try { this.live.node.disconnect(); this.live.chain.output.disconnect(); } catch (e) { /* ok */ } this.live = null; } this.listening = false; }
  }

  root.LabAudition = { Audition, buildChain, renderSelection, rmsOf, levelGain, toWav16, softClipCurve, LIMIT_DB_MAX, CLIP_KNEE, BAND_PAD };
})(typeof self !== 'undefined' ? self : this);
