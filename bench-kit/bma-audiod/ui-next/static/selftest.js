/* Engine self-test in the browser: probes what differs between engines and posts one JSON result to /api/selftest?browser=<?b=name>.
   Open selftest.html?b=chrome in each browser (headless is fine). Synthetic data only. */
(async function () {
  'use strict';
  const D = self.LabDSP, R = self.LabRender, A = self.LabAudition, FS = 48000;
  const out = { env: {}, checks: [], bench: {} };
  const q = new URLSearchParams(location.search), bname = q.get('b') || 'unknown';
  const log = (id, ok, detail, skip) => { out.checks.push({ id, ok: !!ok || !!skip, skip: !!skip, detail: detail == null ? '' : String(detail) }); };
  const guard = async (id, fn) => { try { await fn(); } catch (e) { log(id, false, 'threw ' + (e && e.message || e)); } };
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const withTimeout = (p, ms, tag) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('timeout ' + tag)), ms))]);

  // ---- env
  out.env = { ua: navigator.userAgent, cores: navigator.hardwareConcurrency, dpr: devicePixelRatio, secure: isSecureContext, pointerEvent: 'PointerEvent' in self, touch: 'ontouchstart' in self, coarse: matchMedia('(pointer:coarse)').matches, visualViewport: !!self.visualViewport };

  // ---- WebGL2 + float textures
  let glInfo = {};
  await guard('webgl2.context', async () => {
    const c = document.createElement('canvas'), gl = c.getContext('webgl2');
    if (!gl) { log('webgl2.context', false, 'no webgl2'); return; }
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    glInfo = { renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER), vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR), maxTex: gl.getParameter(gl.MAX_TEXTURE_SIZE), floatLinear: !!gl.getExtension('OES_texture_float_linear'), colorBufferFloat: !!gl.getExtension('EXT_color_buffer_float') };
    out.env.gl = glInfo; log('webgl2.context', true, glInfo.renderer + ' | maxTex ' + glInfo.maxTex);
  });
  await guard('webgl2.r32f_nearest', async () => {          // the renderer needs R32F + texelFetch only (no float filtering)
    const wf = new R.Waterfall(document.createElement('canvas'), { mode: 'webgl', nBins: 8, nCols: 8, fs: FS, fmin: 0, fmax: FS / 2 });
    log('webgl2.r32f_nearest', wf.kind === 'webgl', wf.kind + (wf.glError ? ' ' + wf.glError : ''));
  });

  // ---- back-end parity: the WebGL and Canvas2D renderers must draw the same picture
  function pattern(nBins, k) { const c = new Float32Array(nBins); for (let i = 0; i < nBins; i++) c[i] = -95 + 70 * (0.5 + 0.5 * Math.sin(i * 0.19 + k * 0.31)) * (0.4 + 0.6 * (i % 17 === (k % 17) ? 1 : 0.55)); return c; }
  async function parity(id, nCols, pushes, mode) {
    const W = 320, H = 140, mk = (m) => { const cv = document.createElement('canvas'); cv.width = W; cv.height = H; document.body.appendChild(cv); return [cv, new R.Waterfall(cv, { mode: m, nBins: 129, nCols, fs: FS, fmin: 200, fmax: 6000, yScale: mode, cmap: 'viridis', lo: -100, hi: -20 })]; };
    const [cg, g] = mk('webgl'), [cc, c] = mk('canvas');
    if (g.kind !== 'webgl') { log(id, false, 'webgl back end not available: ' + g.glError); return; }
    for (let k = 0; k < pushes; k++) { const col = pattern(129, k); g.pushColumn(col); c.pushColumn(col); }
    g.render(); const gl = g.gl, px = new Uint8Array(W * H * 4); gl.readPixels(0, 0, W, H, gl.RGBA, gl.UNSIGNED_BYTE, px);
    c.render(); const ci = cc.getContext('2d').getImageData(0, 0, W, H).data;
    let sum = 0, mx = 0, n = 0, big = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) for (let ch = 0; ch < 3; ch++) { const a = px[((H - 1 - y) * W + x) * 4 + ch], b = ci[(y * W + x) * 4 + ch], d = Math.abs(a - b); sum += d; n++; if (d > mx) mx = d; if (d > 16) big++; }
    const mean = sum / n, frac = big / n;
    log(id, mean < 3 && frac < 0.02, 'mean abs diff ' + mean.toFixed(2) + '/255, max ' + mx + ', share of channel values off by more than 16: ' + (100 * frac).toFixed(2) + ' percent');
    document.body.removeChild(cg); document.body.removeChild(cc);
  }
  await guard('parity.full', () => parity('parity.full_ring', 64, 64, 'lin'));
  await guard('parity.wrap', () => parity('parity.wrapped_ring_log_scale', 64, 100, 'log'));
  await guard('parity.partial', () => parity('parity.partially_filled_mel_scale', 64, 25, 'mel'));

  // ---- tiles: two rings and three layers of different heights on one canvas (the dashboard's big tile next to small tiles), and a lost context
  const FREQS46 = Array.from({ length: 46 }, (_, i) => 50 * Math.pow(16000 / 50, i / 45));
  function readGl(w, W, H) { w.render(); const px = new Uint8Array(W * H * 4); w.gl.readPixels(0, 0, W, H, w.gl.RGBA, w.gl.UNSIGNED_BYTE, px); return px; }
  await guard('parity.layers', async () => {
    const W = 360, H = 200, layers = [{ ring: 0, x: 0, y: 0, w: 360, h: 120 }, { ring: 1, x: 0, y: 128, w: 170, h: 72 }, { ring: 0, x: 190, y: 128, w: 170, h: 72 }];
    const mk = (m) => { const cv = document.createElement('canvas'); cv.width = W; cv.height = H; document.body.appendChild(cv); return [cv, new R.Waterfall(cv, { mode: m, nBins: 46, nCols: 48, fs: 32000, binFreqs: FREQS46, fmin: 50, fmax: 16000, yScale: 'log', lo: -100, hi: -20, rings: 2, layers })]; };
    const [cg, g] = mk('webgl'), [cc, c] = mk('canvas');
    if (g.kind !== 'webgl') { log('parity.layers', false, 'webgl back end not available: ' + g.glError); return; }
    for (let k = 0; k < 60; k++) { for (const w of [g, c]) { w.pushColumn(0, pattern(46, k)); w.pushColumn(1, pattern(46, k + 7)); } }
    const px = readGl(g, W, H); c.render(); const ci = cc.getContext('2d').getImageData(0, 0, W, H).data;
    let sum = 0, n = 0, big = 0, gap = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) for (let ch = 0; ch < 3; ch++) { const d = Math.abs(px[((H - 1 - y) * W + x) * 4 + ch] - ci[(y * W + x) * 4 + ch]); sum += d; n++; if (d > 16) big++; if (y >= 121 && y <= 126) gap = Math.max(gap, d); }
    log('parity.layers', sum / n < 3 && big / n < 0.02 && gap <= 2, 'three layers over two rings: mean abs diff ' + (sum / n).toFixed(2) + '/255, share off by more than 16: ' + (100 * big / n).toFixed(2) + ' percent, gap between layers differs by at most ' + gap);
    g.setLayerRing(1, 0); c.setLayerRing(1, 0); const a = readGl(g, W, H); c.render(); const b = cc.getContext('2d').getImageData(0, 0, W, H).data; let s2 = 0; for (let y = 129; y < H; y++) for (let x = 0; x < 170; x++) s2 += Math.abs(a[((H - 1 - y) * W + x) * 4 + 1] - b[(y * W + x) * 4 + 1]);
    log('parity.layer_source_switch', s2 / (71 * 170) < 3, 'after pointing layer 1 at ring 0: mean abs diff of the green channel ' + (s2 / (71 * 170)).toFixed(2));
    document.body.removeChild(cg); document.body.removeChild(cc);
  });
  await guard('webgl.context_restore', async () => {
    const W = 160, H = 90, cv = document.createElement('canvas'); cv.width = W; cv.height = H; document.body.appendChild(cv);
    const w = new R.Waterfall(cv, { mode: 'webgl', nBins: 46, nCols: 32, fs: 32000, binFreqs: FREQS46, fmin: 50, fmax: 16000, yScale: 'log', lo: -100, hi: -20 });
    const ext = w.kind === 'webgl' && w.gl.getExtension('WEBGL_lose_context');
    if (!ext) { log('webgl.context_restore', false, 'SKIPPED: ' + (w.kind === 'webgl' ? 'WEBGL_lose_context is not exposed' : 'no WebGL'), true); document.body.removeChild(cv); return; }
    for (let k = 0; k < 32; k++) w.pushColumn(pattern(46, k));
    readGl(w, W, H); let lostCalls = 0; w.onlost = () => { lostCalls++; };
    ext.loseContext(); await sleep(150); const lostFlag = w.lost; w.pushColumn(pattern(46, 33));                  // pushing while lost must not throw: the CPU copy keeps the column
    ext.restoreContext(); await sleep(400);
    const cv2 = document.createElement('canvas'); cv2.width = W; cv2.height = H; document.body.appendChild(cv2);
    const twin = new R.Waterfall(cv2, { mode: 'webgl', nBins: 46, nCols: 32, fs: 32000, binFreqs: FREQS46, fmin: 50, fmax: 16000, yScale: 'log', lo: -100, hi: -20 });
    for (let k = 0; k < 32; k++) twin.pushColumn(pattern(46, k)); twin.pushColumn(pattern(46, 33));
    const after = readGl(w, W, H), ref = readGl(twin, W, H); let d = 0; for (let i = 0; i < after.length; i++) d = Math.max(d, Math.abs(after[i] - ref[i]));
    log('webgl.context_restore', lostFlag === true && lostCalls === 1 && w.lost === false && d === 0, 'lost flag ' + lostFlag + ', onlost calls ' + lostCalls + ', restored ' + !w.lost + ', picture after the restore vs a fresh renderer fed the same columns: max channel diff ' + d);
    document.body.removeChild(cv); document.body.removeChild(cv2);
  });

  // ---- OffscreenCanvas
  await guard('offscreen', async () => {
    const has = typeof OffscreenCanvas !== 'undefined', ctl = 'transferControlToOffscreen' in HTMLCanvasElement.prototype;
    let gl2 = false; if (has) { try { gl2 = !!new OffscreenCanvas(8, 8).getContext('webgl2'); } catch (e) { gl2 = false; } }
    log('offscreen.canvas', has && ctl, 'OffscreenCanvas ' + has + ', transferControlToOffscreen ' + ctl);
    log('offscreen.webgl2', gl2, 'webgl2 in an OffscreenCanvas: ' + gl2);
  });

  // ---- audio: context, unlock, worklet
  let live = null;
  await guard('audio.context', async () => {
    live = new AudioContext({ latencyHint: 'interactive', sampleRate: 48000 });
    const s0 = live.state; let s1 = s0;
    try { await withTimeout(live.resume(), 1500, 'resume'); s1 = live.state; } catch (e) { s1 = live.state + ' (resume did not settle in 1.5 s)'; }
    out.env.audio = { stateAtCreate: s0, stateAfterResumeWithoutGesture: s1, sampleRate: live.sampleRate, baseLatency: live.baseLatency, outputLatency: live.outputLatency };
    log('audio.context', true, 'state at create ' + s0 + ', after resume() with no user gesture ' + s1 + ', sampleRate ' + live.sampleRate + ', baseLatency ' + live.baseLatency);
  });
  await guard('audio.worklet', async () => {
    const off = new OfflineAudioContext(1, 1024, FS); await off.audioWorklet.addModule('/engine/worklets.js'); const n = new AudioWorkletNode(off, 'freqdiv'); log('audio.worklet', !!n, 'addModule + AudioWorkletNode in an OfflineAudioContext');
  });

  // ---- offline audition checks (numbers, not sound)
  const mkBuf = (n, fn) => { const b = new AudioBuffer({ length: n, sampleRate: FS, numberOfChannels: 8 }); for (let c = 0; c < 8; c++) { const d = b.getChannelData(c); for (let i = 0; i < n; i++) d[i] = fn(c, i / FS); } return b; };
  const rmsRange = (buf, s0, s1) => { const d = buf.getChannelData(0); let s = 0; for (let i = s0; i < s1; i++) s += d[i] * d[i]; return Math.sqrt(s / (s1 - s0)); };
  const peakFreq = (buf) => { const d = buf.getChannelData(0), N = 16384, x = new Float64Array(N); for (let i = 0; i < N; i++) x[i] = d[buf.length - N + i]; const st = D.stft(x, { n: N, hop: N, window: 'hann' }); let b = 1, v = 0; for (let k = 1; k < st.nBins; k++) if (st.mag[k] > v) { v = st.mag[k]; b = k; } const l = st.mag[b - 1], r = st.mag[b + 1], dd = (r - l) / (2 * (2 * v - l - r) + 1e-30); return { f: (b + dd) * FS / N, amp: v }; };
  const specAmp = (buf, f) => { const d = buf.getChannelData(0), N = 16384, x = new Float64Array(N); for (let i = 0; i < N; i++) x[i] = d[buf.length - N + i]; const st = D.stft(x, { n: N, hop: N, window: 'hann' }); const k = Math.round(f * N / FS); return Math.max(st.mag[k - 1], st.mag[k], st.mag[k + 1]); };
  const W1 = (b) => { const w = new Array(8).fill(0); w[b] = 1; return w; };
  const secs = 1.0, len = FS;
  await guard('audition.heterodyne', async () => {
    const buf = mkBuf(len, (c, t) => c === 0 ? 0.5 * Math.sin(2 * Math.PI * 6000 * t) : 0);
    const r = await A.renderSelection(buf, 0, secs, { weights: W1(0), band: [5000, 7000], mode: { kind: 'hetero', target: 1500 }, gain: 1 }), p = peakFreq(r), want = 6000 - (Math.sqrt(5000 * 7000) - 1500);
    log('audition.heterodyne', Math.abs(p.f - want) < 15 && p.amp > 0.27 && p.amp < 0.38, 'a 6000 Hz tone with band 5-7 kHz and target 1500 Hz is heard at ' + p.f.toFixed(1) + ' Hz (expected ' + want.toFixed(1) + '), amplitude ' + p.amp.toFixed(3) + ' (0.5 in x box loss 0.73 x post-filter 0.89 = 0.325 expected)');
  });
  await guard('audition.divide', async () => {
    const buf = mkBuf(len, (c, t) => c === 0 ? 0.5 * Math.sin(2 * Math.PI * 8000 * t) : 0);
    const r = await A.renderSelection(buf, 0, secs, { weights: W1(0), band: null, mode: { kind: 'divide', div: 8 }, gain: 1 }), p = peakFreq(r);
    log('audition.divide', Math.abs(p.f - 1000) < 15 && p.amp > 0.45 && p.amp < 0.72, 'an 8 kHz tone divided by 8 is heard at ' + p.f.toFixed(1) + ' Hz (expected 1000), fundamental amplitude ' + p.amp.toFixed(3) + ' (a 0.5 square wave has a 0.637 fundamental; the envelope is accurate to about 20 percent at 6 samples per cycle)');
  });
  await guard('audition.band_solo', async () => {
    const buf = mkBuf(len, (c, t) => c === 0 ? 0.3 * Math.sin(2 * Math.PI * 1000 * t) + 0.3 * Math.sin(2 * Math.PI * 6000 * t) : 0);
    const r = await A.renderSelection(buf, 0, secs, { weights: W1(0), band: [5000, 7000], mode: { kind: 'normal' }, gain: 1 }), a6 = specAmp(r, 6000), a1 = specAmp(r, 1000), att = 20 * Math.log10(a6 / Math.max(a1, 1e-12));
    const eb = mkBuf(len, (c, t) => c === 0 ? 0.3 * Math.sin(2 * Math.PI * 5000 * t) : 0), edge = specAmp(await A.renderSelection(eb, 0, secs, { weights: W1(0), band: [5000, 7000], mode: { kind: 'normal' }, gain: 1 }), 5000);
    log('audition.band_solo', a6 > 0.2 && a6 < 0.24 && edge < a6 && edge > 0.16 && att > 40, 'box 5-7 kHz keeps 6 kHz at ' + a6.toFixed(3) + ' (0.3 in: -2.8 dB, the 24 dB/oct skirts of a 2 kHz wide box), the 5 kHz box edge at ' + edge.toFixed(3) + ' (below the centre), removes 1 kHz by ' + att.toFixed(1) + ' dB');
  });
  await guard('audition.beam_solo_mix', async () => {
    const buf = mkBuf(len, (c, t) => 0.04 * (c + 1) * Math.sin(2 * Math.PI * 1500 * t));
    const s3 = await A.renderSelection(buf, 0, secs, { weights: W1(3), band: null, mode: { kind: 'normal' }, gain: 1 });
    const w = new Array(8).fill(0); w[0] = 0.5; w[1] = 0.5;
    const m01 = await A.renderSelection(buf, 0, secs, { weights: w, band: null, mode: { kind: 'normal' }, gain: 1 });
    const rs = rmsRange(s3, len / 2, len) * Math.SQRT2, rm = rmsRange(m01, len / 2, len) * Math.SQRT2;
    log('audition.beam_solo_mix', Math.abs(rs - 0.16) < 0.0008 && Math.abs(rm - 0.06) < 0.0003, 'solo beam 3 amplitude ' + rs.toFixed(4) + ' (0.1600), mix of beams 0 and 1 ' + rm.toFixed(4) + ' (0.0600)');
  });
  await guard('audition.limiter', async () => {
    const buf = mkBuf(len, (c, t) => c === 0 ? 0.5 * Math.sin(2 * Math.PI * 1000 * t) : 0), r = await A.renderSelection(buf, 0, secs, { weights: W1(0), band: null, mode: { kind: 'normal' }, gain: 100 });
    const d = r.getChannelData(0); let pk = 0; for (let i = len / 2; i < len; i++) pk = Math.max(pk, Math.abs(d[i]));
    const thru = await A.renderSelection(buf, 0, secs, { weights: W1(0), band: null, mode: { kind: 'normal' }, gain: 1 }), td = thru.getChannelData(0); let tp = 0; for (let i = len / 2; i < len; i++) tp = Math.max(tp, Math.abs(td[i]));
    log('audition.limiter', pk <= 1.0 + 1e-6 && Math.abs(tp - 0.5) < 0.005, 'a 0.5 amplitude tone boosted x100 peaks at ' + pk.toFixed(4) + ' (bounded by 1.0; unlimited would be 50) and passes at x1 unchanged: peak ' + tp.toFixed(4) + ' (expected 0.5000)');
  });
  await guard('audition.level_match', async () => {
    const quiet = mkBuf(len, (c, t) => c === 0 ? 0.0014142 * Math.sin(2 * Math.PI * 1000 * t) : 0), cfg = { weights: W1(0), band: null, mode: { kind: 'normal' }, gain: 1 };
    const g = A.levelGain(A.rmsOf(await A.renderSelection(quiet, 0, secs, cfg)), -30), silent = A.levelGain(0, -30), tiny = A.levelGain(1e-6, -30);
    log('audition.level_match', Math.abs(g - 31.62) < 0.7 && silent === 1 && Math.abs(tiny - 100) < 1e-9, 'a -60 dBFS RMS selection gets x' + g.toFixed(2) + ' (31.62 = +30 dB), silence x' + silent + ', a -120 dBFS selection is capped at x' + tiny.toFixed(1) + ' (+40 dB)');
  });
  await guard('audition.tape_speed', async () => {
    const off = new OfflineAudioContext(1, FS, FS), b = off.createBuffer(1, FS, FS), d = b.getChannelData(0); for (let i = 0; i < FS; i++) d[i] = 0.5 * Math.sin(2 * Math.PI * 4000 * i / FS);
    const s = off.createBufferSource(); s.buffer = b; s.playbackRate.value = 0.5; s.connect(off.destination); s.start(); const r = await off.startRendering(), p = peakFreq(r);
    log('audition.tape_speed', Math.abs(p.f - 2000) < 10, 'playbackRate 0.5 moves 4000 Hz to ' + p.f.toFixed(1) + ' Hz (time expansion, pitch drops)');
  });
  await guard('audition.preserves_pitch', async () => {
    const e = document.createElement('audio'), has = 'preservesPitch' in e, moz = 'mozPreservesPitch' in e, web = 'webkitPreservesPitch' in e;
    log('audition.preserves_pitch', has || moz || web, 'HTMLMediaElement.preservesPitch ' + has + ', moz ' + moz + ', webkit ' + web);
  });
  await guard('codec.webcodecs', async () => {
    const has = typeof AudioDecoder !== 'undefined'; let sup = false; if (has) { try { sup = (await AudioDecoder.isConfigSupported({ codec: 'opus', sampleRate: 48000, numberOfChannels: 1 })).supported; } catch (e) { sup = false; } }
    log('codec.webcodecs_opus', has && sup, 'AudioDecoder ' + has + ', opus 48 kHz mono supported ' + sup);
    out.env.webcodecs = { audioDecoder: has, opus: sup };
  });

  // ---- the listening stream over a WebSocket (a consent token first, as the dashboard does it)
  await guard('stream.ws', async () => {
    const tok = (await (await fetch('/api/v2/listen/consent', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json()).token;
    const t0 = performance.now(), ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/v2/listen?token=' + encodeURIComponent(tok) + '&beams=0,1,2,3,4,5,6,7'); ws.binaryType = 'arraybuffer';
    const arr = []; let hdr = null, got = 0, last = 0, tFirst = 0;
    await new Promise((res) => {
      ws.onmessage = (e) => {
        const now = performance.now();
        if (!hdr) { const b = new Uint8Array(e.data), dv = new DataView(e.data); hdr = { magic: new TextDecoder().decode(b.slice(0, 6)), fs: dv.getUint32(8, true), ch: dv.getUint16(12, true), bits: dv.getUint16(14, true) }; tFirst = last = now; return; }
        arr.push(now - last); last = now; got += e.data.byteLength; if (now - t0 > 3000) res();
      };
      ws.onerror = () => res(); setTimeout(res, 6000);
    });
    ws.close(); arr.sort((a, b) => a - b); const secs = (last - tFirst) / 1000, rate = got / secs, ttfb = tFirst - t0;
    log('stream.ws', !!hdr && hdr.magic === 'UILAB1' && hdr.fs === FS && hdr.ch === 8 && Math.abs(rate - 768000) < 30000, 'time to first frame ' + ttfb.toFixed(0) + ' ms, header ' + JSON.stringify(hdr) + ', ' + (rate / 1000).toFixed(0) + ' kB/s (expected 768), frame intervals p50 ' + (arr[arr.length >> 1] || 0).toFixed(1) + ' ms, p95 ' + (arr[Math.floor(arr.length * 0.95)] || 0).toFixed(1) + ' ms');
  });

  // ---- live ring in a real-time context (needs a running context; headless may have no audio device)
  await guard('audio.live_ring', async () => {
    if (!live || live.state !== 'running') { log('audio.live_ring', false, 'SKIPPED: the AudioContext is ' + (live && live.state) + ' (autoplay policy: it needs a user gesture)', true); return; }
    await live.audioWorklet.addModule('/engine/worklets.js');
    const node = new AudioWorkletNode(live, 'pcmring', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [8], processorOptions: { channels: 8, capacity: 48000, prebuffer: 8640 } });
    const g = live.createGain(); g.gain.value = 0; node.connect(g); g.connect(live.destination); let stats = null; node.port.onmessage = (e) => { stats = e.data; };
    const frames = 960; let sent = 0; const t0 = performance.now();
    const iv = setInterval(() => { const d = new Float32Array(8 * frames); for (let i = 0; i < d.length; i++) d[i] = 0.1 * Math.sin(i * 0.05); node.port.postMessage({ frames, data: d }, [d.buffer]); sent += frames; }, 20);
    await sleep(1500); const warm = stats && stats.under; await sleep(2200); clearInterval(iv); node.disconnect();
    log('audio.live_ring', stats && stats.under === warm && stats.under <= 2 && stats.fill < 20000, 'after ' + ((performance.now() - t0) / 1000).toFixed(1) + ' s of 20 ms chunks: ' + JSON.stringify(stats) + '; underruns after the 1.5 s warm-up: ' + (stats ? stats.under - warm : '?') + ' (the jitter buffer widens after each underrun)');
  });

  // ---- in-browser DSP cost (JIT differs by engine)
  await guard('bench', async () => {
    const x = new Float64Array(FS * 4); for (let i = 0; i < x.length; i++) x[i] = 0.05 * Math.sin(2 * Math.PI * 4000 * i / FS) + 0.01 * Math.sin(i * 12.9898);
    const t = (f) => { f(); let b = 1e9; for (let r = 0; r < 3; r++) { const t0 = performance.now(); f(); b = Math.min(b, performance.now() - t0); } return +(b / 4).toFixed(2); };
    out.bench = { stft1024_ms_per_audio_s: t(() => D.stft(x, { n: 1024, hop: 480 })), multitaper1024_k4: t(() => D.multitaper(x, { n: 1024, hop: 480, K: 4 })), reassigned1024: t(() => D.reassigned(x, { n: 1024, hop: 480 })), cqt24bpo_144bins_hop20ms: t(() => D.cqt(x, { fs: FS, fmin: 200, bpo: 24, nBins: 144, hop: 960 })) };
    log('bench', true, JSON.stringify(out.bench));
  });

  out.summary = { passed: out.checks.filter(c => c.ok && !c.skip).length, skipped: out.checks.filter(c => c.skip).length, failed: out.checks.filter(c => !c.ok).length };
  document.getElementById('out').textContent = JSON.stringify(out, null, 1);
  try { await fetch('/api/selftest?browser=' + encodeURIComponent(bname), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(out) }); } catch (e) { document.title = 'post failed'; }
  document.title = 'selftest done';
})();
