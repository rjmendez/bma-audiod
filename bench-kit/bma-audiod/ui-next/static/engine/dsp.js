/* ui-lab DSP core: pure functions, no DOM, no dependencies. Runs in the browser and in Node (tests).
   Conventions: signals are Float64Array/Float32Array; spectrograms are row-major frames x bins; "mag" = amplitude where a full-scale
   sine at a bin centre reads 1.0 (0 dBFS); frames start at i*hop (no centring). Original code; the techniques are textbook. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LabDSP = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const PI2 = 2 * Math.PI;

  // ------------------------------------------------------------------ FFT (radix-2, in place, forward e^{-j...})
  const TW = {}, REV = {};
  function plan(n) {
    if (TW[n]) return;
    if (n < 2 || (n & (n - 1))) throw new Error('fft size must be a power of 2 >= 2, got ' + n);
    const c = new Float64Array(n / 2), s = new Float64Array(n / 2);
    for (let i = 0; i < n / 2; i++) { c[i] = Math.cos(PI2 * i / n); s[i] = -Math.sin(PI2 * i / n); }
    const r = new Uint32Array(n), bits = Math.round(Math.log2(n));
    for (let i = 0; i < n; i++) { let x = i, y = 0; for (let b = 0; b < bits; b++) { y = (y << 1) | (x & 1); x >>= 1; } r[i] = y; }
    TW[n] = { c, s }; REV[n] = r;
  }
  function fft(re, im) {
    const n = re.length; plan(n);
    const { c, s } = TW[n], r = REV[n];
    for (let i = 0; i < n; i++) { const j = r[i]; if (j > i) { let t = re[i]; re[i] = re[j]; re[j] = t; t = im[i]; im[i] = im[j]; im[j] = t; } }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1, step = n / size;
      for (let i = 0; i < n; i += size) {
        for (let j = 0, k = 0; j < half; j++, k += step) {
          const wr = c[k], wi = s[k], a = i + j, b = a + half;
          const tr = re[b] * wr - im[b] * wi, ti = re[b] * wi + im[b] * wr;
          re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
        }
      }
    }
  }

  // ------------------------------------------------------------------ windows (periodic, as scipy.signal.get_window)
  function i0(x) { let s = 1, t = 1; for (let k = 1; k < 60; k++) { t *= (x / (2 * k)) * (x / (2 * k)); s += t; if (t < 1e-18 * s) break; } return s; }
  function makeWindow(name, n, beta) {
    const w = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const p = PI2 * i / n;
      switch (name) {
        case 'rect': w[i] = 1; break;
        case 'hann': w[i] = 0.5 - 0.5 * Math.cos(p); break;
        case 'hamming': w[i] = 0.54 - 0.46 * Math.cos(p); break;
        case 'blackman': w[i] = 0.42 - 0.5 * Math.cos(p) + 0.08 * Math.cos(2 * p); break;
        case 'blackmanharris': w[i] = 0.35875 - 0.48829 * Math.cos(p) + 0.14128 * Math.cos(2 * p) - 0.01168 * Math.cos(3 * p); break;
        case 'kaiser': { const b = beta == null ? 8.6 : beta, a = 2 * i / n - 1; w[i] = i0(b * Math.sqrt(Math.max(0, 1 - a * a))) / i0(b); break; }
        default: throw new Error('unknown window ' + name);
      }
    }
    return w;
  }
  const WINDOWS = ['hann', 'hamming', 'blackman', 'blackmanharris', 'kaiser', 'rect'];
  function sum(a) { let s = 0; for (let i = 0; i < a.length; i++) s += a[i]; return s; }

  // ------------------------------------------------------------------ STFT
  function frameCount(len, n, hop) { return len < n ? 0 : Math.floor((len - n) / hop) + 1; }
  /* o: {n, hop, window, beta, pad (zero-padding factor, power of 2), complex (also return re/im)} -> {mag, re, im, nFrames, nBins, nfft, df(fs=1)} */
  function stft(x, o) {
    const n = o.n, hop = o.hop, pad = o.pad || 1, nfft = n * pad, nBins = nfft / 2 + 1;
    const w = makeWindow(o.window || 'hann', n, o.beta), norm = 2 / sum(w), nF = frameCount(x.length, n, hop);
    const mag = new Float64Array(nF * nBins), re = new Float64Array(nfft), im = new Float64Array(nfft);
    const cre = o.complex ? new Float64Array(nF * nBins) : null, cim = o.complex ? new Float64Array(nF * nBins) : null;
    for (let f = 0; f < nF; f++) {
      const s0 = f * hop;
      for (let i = 0; i < n; i++) re[i] = x[s0 + i] * w[i];
      re.fill(0, n); im.fill(0);
      fft(re, im);
      const base = f * nBins;
      for (let k = 0; k < nBins; k++) {
        mag[base + k] = Math.hypot(re[k], im[k]) * norm;
        if (cre) { cre[base + k] = re[k] * norm; cim[base + k] = im[k] * norm; }
      }
    }
    return { mag, re: cre, im: cim, nFrames: nF, nBins, nfft, n, hop };
  }

  // ------------------------------------------------------------------ multitaper (sine tapers, K averaged)
  function sineTaper(n, k) { const w = new Float64Array(n), a = Math.sqrt(2 / (n + 1)); for (let i = 0; i < n; i++) w[i] = a * Math.sin(Math.PI * k * (i + 1) / (n + 1)); return w; }
  const _mtCal = {};
  function multitaper(x, o) {
    const n = o.n, hop = o.hop, K = o.K || 4, pad = o.pad || 1, nfft = n * pad, nBins = nfft / 2 + 1, nF = frameCount(x.length, n, hop);
    const tapers = []; for (let k = 1; k <= K; k++) tapers.push(sineTaper(n, k));
    const key = n + ':' + K + ':' + pad;
    if (!_mtCal[key]) {                      // calibrate so a bin-centred unit sine reads 1.0 on average over the K tapers
      const t = new Float64Array(n); const kc = Math.round(nBins / 4) | 0; for (let i = 0; i < n; i++) t[i] = Math.sin(PI2 * (kc / nfft) * i);
      const p = new Float64Array(nBins), re = new Float64Array(nfft), im = new Float64Array(nfft);
      for (const w of tapers) { re.fill(0); im.fill(0); for (let i = 0; i < n; i++) re[i] = t[i] * w[i]; fft(re, im); for (let b = 0; b < nBins; b++) p[b] += re[b] * re[b] + im[b] * im[b]; }
      let m = 0; for (let b = 0; b < nBins; b++) m = Math.max(m, p[b] / K); _mtCal[key] = 1 / Math.sqrt(m);
    }
    const cal = _mtCal[key], mag = new Float64Array(nF * nBins), re = new Float64Array(nfft), im = new Float64Array(nfft), pw = new Float64Array(nBins);
    for (let f = 0; f < nF; f++) {
      pw.fill(0);
      for (const w of tapers) { for (let i = 0; i < n; i++) re[i] = x[f * hop + i] * w[i]; re.fill(0, n); im.fill(0); fft(re, im); for (let b = 0; b < nBins; b++) pw[b] += re[b] * re[b] + im[b] * im[b]; }
      for (let b = 0; b < nBins; b++) mag[f * nBins + b] = Math.sqrt(pw[b] / K) * cal;
    }
    return { mag, nFrames: nF, nBins, nfft, n, hop };
  }

  // ------------------------------------------------------------------ reassigned spectrogram (Auger-Flandrin; hann window)
  /* Each STFT cell's power moves to its centre of gravity: t' = t + Re(X_Th conj X_h)/|X_h|^2 (samples after the window centre), f' = f - Im(X_Dh conj X_h)/|X_h|^2 * fs/2pi.
     Returns the stft grid with energy reassigned (mag = sqrt(summed power)) plus the per-cell estimates (tHat in frames, fHat in bins). */
  function reassigned(x, o) {
    const n = o.n, hop = o.hop, pad = o.pad || 1, nfft = n * pad, nBins = nfft / 2 + 1, nF = frameCount(x.length, n, hop), floor = o.floor == null ? 1e-9 : o.floor;
    const h = makeWindow('hann', n), th = new Float64Array(n), dh = new Float64Array(n), norm = 2 / sum(h);
    for (let i = 0; i < n; i++) { th[i] = (i - n / 2) * h[i]; dh[i] = 0.5 * (PI2 / n) * Math.sin(PI2 * i / n); }   // time ramp in samples, d/dsample of hann
    const A = [new Float64Array(nfft), new Float64Array(nfft)], B = [new Float64Array(nfft), new Float64Array(nfft)], C = [new Float64Array(nfft), new Float64Array(nfft)];
    const out = new Float64Array(nF * nBins), tHat = new Float64Array(nF * nBins), fHat = new Float64Array(nF * nBins);
    const run = (w, buf, f) => { buf[0].fill(0); buf[1].fill(0); for (let i = 0; i < n; i++) buf[0][i] = x[f * hop + i] * w[i]; fft(buf[0], buf[1]); };
    for (let f = 0; f < nF; f++) {
      run(h, A, f); run(th, B, f); run(dh, C, f);
      for (let k = 0; k < nBins; k++) {
        const hr = A[0][k], hi = A[1][k], p = hr * hr + hi * hi, idx = f * nBins + k;
        const pw = p * norm * norm;
        if (pw < floor * floor) { tHat[idx] = f; fHat[idx] = k; continue; }
        const dt = (B[0][k] * hr + B[1][k] * hi) / p;                    // Re(X_Th conj X_h)/|X_h|^2, samples
        const df = -(C[1][k] * hr - C[0][k] * hi) / p;                   // -Im(X_Dh conj X_h)/|X_h|^2, rad/sample
        const tf = f + dt / hop;                                         // frame units: the energy sits dt samples after the window centre
        const fb = k + df * nfft / PI2;                                  // bin units
        tHat[idx] = tf; fHat[idx] = fb;
        const fi = Math.round(tf), kb = Math.round(fb);
        if (fi >= 0 && fi < nF && kb >= 0 && kb < nBins) out[fi * nBins + kb] += pw;
      }
    }
    for (let i = 0; i < out.length; i++) out[i] = Math.sqrt(out[i]);
    return { mag: out, tHat, fHat, nFrames: nF, nBins, nfft, n, hop };
  }

  // ------------------------------------------------------------------ constant-Q / Morlet (direct atoms, hop in samples)
  /* o: {fs, fmin, bpo, nBins, hop, window:'hann'|'gauss', qScale}. Atom k: window of length ceil(Q fs / f_k) times e^{-j2pi f_k n/fs}; mag = amplitude. */
  function cqtKernels(o) {
    const fs = o.fs, bpo = o.bpo || 24, Q = (o.qScale || 1) / (Math.pow(2, 1 / bpo) - 1), kern = [];
    for (let k = 0; k < o.nBins; k++) {
      const f = o.fmin * Math.pow(2, k / bpo), N = Math.max(8, Math.ceil(Q * fs / f)), re = new Float64Array(N), im = new Float64Array(N), w = new Float64Array(N);
      for (let i = 0; i < N; i++) w[i] = o.window === 'gauss' ? Math.exp(-0.5 * Math.pow((i - (N - 1) / 2) / (N / 6), 2)) : 0.5 - 0.5 * Math.cos(PI2 * (i + 0.5) / N);
      const sw = sum(w);
      for (let i = 0; i < N; i++) { const a = -PI2 * f * i / fs; re[i] = 2 * w[i] / sw * Math.cos(a); im[i] = 2 * w[i] / sw * Math.sin(a); }
      kern.push({ f, N, re, im });
    }
    return { kern, Q };
  }
  function cqt(x, o) {
    const { kern, Q } = cqtKernels(o), hop = o.hop, nB = o.nBins, nF = Math.floor(x.length / hop), mag = new Float64Array(nF * nB);
    for (let m = 0; m < nF; m++) {
      const c = m * hop;
      for (let k = 0; k < nB; k++) {
        const { N, re, im } = kern[k], s0 = c - (N >> 1); let sr = 0, si = 0;
        const i0_ = Math.max(0, -s0), i1 = Math.min(N, x.length - s0);
        for (let i = i0_; i < i1; i++) { const v = x[s0 + i]; sr += v * re[i]; si += v * im[i]; }
        mag[m * nB + k] = Math.hypot(sr, si);
      }
    }
    return { mag, nFrames: nF, nBins: nB, freqs: kern.map(k => k.f), Q };
  }

  // ------------------------------------------------------------------ frequency scales and filterbanks (triangular, peak 1)
  const SCALES = {
    mel: { fwd: f => 2595 * Math.log10(1 + f / 700), inv: m => 700 * (Math.pow(10, m / 2595) - 1) },
    erb: { fwd: f => 21.4 * Math.log10(1 + 0.00437 * f), inv: e => (Math.pow(10, e / 21.4) - 1) / 0.00437 },
    bark: { fwd: f => 26.81 * f / (1960 + f) - 0.53, inv: z => 1960 * (z + 0.53) / (26.28 - z) },
    log: { fwd: f => Math.log(Math.max(f, 1)), inv: l => Math.exp(l) },
    lin: { fwd: f => f, inv: f => f },
  };
  function filterbank(scale, nBands, nfft, fs, fmin, fmax) {
    const S = SCALES[scale], nBins = nfft / 2 + 1, lo = S.fwd(fmin), hi = S.fwd(fmax), pts = [];
    for (let i = 0; i < nBands + 2; i++) pts.push(S.inv(lo + (hi - lo) * i / (nBands + 1)));
    const fb = new Float64Array(nBands * nBins);
    for (let b = 0; b < nBands; b++) {
      const l = pts[b], c = pts[b + 1], r = pts[b + 2];
      for (let k = 0; k < nBins; k++) { const f = k * fs / nfft; fb[b * nBins + k] = f > l && f < c ? (f - l) / (c - l) : f >= c && f < r ? (r - f) / (r - c) : 0; }
    }
    return { fb, nBands, nBins, centres: pts.slice(1, -1) };
  }
  function applyFilterbank(power, nFrames, nBins, F) {      // power: frames x bins (linear power) -> frames x bands
    const out = new Float64Array(nFrames * F.nBands);
    for (let f = 0; f < nFrames; f++) for (let b = 0; b < F.nBands; b++) { let s = 0; for (let k = 0; k < nBins; k++) s += F.fb[b * nBins + k] * power[f * nBins + k]; out[f * F.nBands + b] = s; }
    return out;
  }

  // ------------------------------------------------------------------ PCEN (Wang et al. 2017; same recursion as analysis/pcen.py and librosa)
  function pcenSmoothing(timeConstantS, frameRateHz) { const T = timeConstantS * frameRateHz; return (Math.sqrt(1 + 4 * T * T) - 1) / (2 * T * T); }
  /* E: frames x bins linear power >= 0. init 'librosa' (M(-1)=1) | 'first' (M(-1)=E(0)). */
  function pcen(E, nFrames, nBins, o) {
    const s = o.s, alpha = o.alpha == null ? 0.98 : o.alpha, delta = o.delta == null ? 2 : o.delta, r = o.r == null ? 0.5 : o.r, eps = o.eps == null ? 1e-6 : o.eps;
    const out = new Float64Array(nFrames * nBins), M = new Float64Array(nBins), dr = Math.pow(delta, r);
    for (let k = 0; k < nBins; k++) M[k] = o.init === 'first' ? E[k] : 1;
    for (let f = 0; f < nFrames; f++) for (let k = 0; k < nBins; k++) {
      const e = E[f * nBins + k]; M[k] = (1 - s) * M[k] + s * e;
      out[f * nBins + k] = Math.pow(e / Math.pow(eps + M[k], alpha) + delta, r) - dr;
    }
    return out;
  }

  // ------------------------------------------------------------------ background normalisation (streaming median in the dB domain)
  /* B tracks the per-bin median of the level with a fixed step (dB per frame) in each direction; output = level - B (0 dB = typical background). */
  function bgNorm(db, nFrames, nBins, o) {
    const step = o.stepDb == null ? 0.1 : o.stepDb, out = new Float64Array(nFrames * nBins), B = new Float64Array(nBins);
    for (let k = 0; k < nBins; k++) B[k] = db[k];
    for (let f = 0; f < nFrames; f++) for (let k = 0; k < nBins; k++) {
      const v = db[f * nBins + k]; B[k] += v > B[k] ? step : -step; out[f * nBins + k] = v - B[k];
    }
    return out;
  }

  // ------------------------------------------------------------------ harmonic / percussive split (Fitzgerald 2010 median filtering, soft masks)
  function medianFilterAxis(a, nF, nB, k, axis) {           // axis 0 = along time (per bin), axis 1 = along frequency (per frame); reflect boundary
    const out = new Float64Array(a.length), half = k >> 1, buf = new Float64Array(k), tmp = new Float64Array(k), len = axis === 0 ? nF : nB, other = axis === 0 ? nB : nF;
    const refl = (i) => { const p = 2 * len; i = ((i % p) + p) % p; return i < len ? i : p - 1 - i; };      // scipy 'reflect': d c b a | a b c d | d c b a
    for (let o = 0; o < other; o++) for (let i = 0; i < len; i++) {
      for (let j = -half; j <= half; j++) { const ii = refl(i + j); buf[j + half] = axis === 0 ? a[ii * nB + o] : a[o * nB + ii]; }
      tmp.set(buf); tmp.sort(); out[axis === 0 ? i * nB + o : o * nB + i] = tmp[half];
    }
    return out;
  }
  function hpss(mag, nF, nB, o) {                           // -> {harm, perc} magnitude spectrograms (mask^power softmask, margin 1)
    const kh = o.kernelTime || 17, kp = o.kernelFreq || 17, H = medianFilterAxis(mag, nF, nB, kh, 0), P = medianFilterAxis(mag, nF, nB, kp, 1);
    const tiny = 1.1754943508222875e-38, harm = new Float64Array(mag.length), perc = new Float64Array(mag.length);
    for (let i = 0; i < mag.length; i++) {
      const h2 = H[i] * H[i], p2 = P[i] * P[i], den = h2 + p2 + tiny;
      harm[i] = mag[i] * (h2 / den); perc[i] = mag[i] * (p2 / den);
    }
    return { harm, perc };
  }

  // ------------------------------------------------------------------ strips: flux, cepstrum, frequency autocorrelation, coherence
  function toDb(mag, floorDb) { const o = new Float64Array(mag.length), fl = floorDb == null ? -140 : floorDb; for (let i = 0; i < mag.length; i++) o[i] = Math.max(fl, 20 * Math.log10(Math.max(mag[i], 1e-12))); return o; }
  function flux(db, nF, nB) {                               // onset strength: sum over bins of the positive dB change since the previous frame
    const o = new Float64Array(nF);
    for (let f = 1; f < nF; f++) { let s = 0; for (let k = 0; k < nB; k++) { const d = db[f * nB + k] - db[(f - 1) * nB + k]; if (d > 0) s += d; } o[f] = s; }
    return o;
  }
  function cepstrum(mag, nF, nB, nq) {                      // real cepstrum of log magnitude per frame, first nq quefrency bins (index = quefrency in samples of the nfft grid)
    const nfft = (nB - 1) * 2, out = new Float64Array(nF * nq), re = new Float64Array(nfft), im = new Float64Array(nfft);
    for (let f = 0; f < nF; f++) {
      for (let k = 0; k < nB; k++) re[k] = Math.log(mag[f * nB + k] + 1e-12);
      for (let k = 1; k < nB - 1; k++) re[nfft - k] = re[k];
      im.fill(0); fft(re, im);
      for (let q = 0; q < nq; q++) out[f * nq + q] = re[q] / nfft;
    }
    return out;
  }
  function freqAutocorr(mag, nF, nB, nLag) {                // normalised autocorrelation of the magnitude spectrum along frequency, lags 0..nLag-1 bins
    const out = new Float64Array(nF * nLag);
    for (let f = 0; f < nF; f++) {
      let e = 1e-30; for (let k = 0; k < nB; k++) e += mag[f * nB + k] * mag[f * nB + k];
      for (let l = 0; l < nLag; l++) { let s = 0; for (let k = 0; k + l < nB; k++) s += mag[f * nB + k] * mag[f * nB + k + l]; out[f * nLag + l] = s / e; }
    }
    return out;
  }
  /* coherence of two complex STFTs (same grid): boxcar average over M frames. -> {coh, phase} frames x bins (edges average the frames available). */
  function coherence(A, B, nF, nB, M) {
    const coh = new Float64Array(nF * nB), ph = new Float64Array(nF * nB), half = M >> 1;
    for (let f = 0; f < nF; f++) {
      const f0 = Math.max(0, f - half), f1 = Math.min(nF - 1, f + half);
      for (let k = 0; k < nB; k++) {
        let xr = 0, xi = 0, pa = 0, pb = 0;
        for (let g = f0; g <= f1; g++) {
          const i = g * nB + k, ar = A.re[i], ai = A.im[i], br = B.re[i], bi = B.im[i];
          xr += ar * br + ai * bi; xi += ai * br - ar * bi; pa += ar * ar + ai * ai; pb += br * br + bi * bi;
        }
        coh[f * nB + k] = (xr * xr + xi * xi) / (pa * pb + 1e-300); ph[f * nB + k] = Math.atan2(xi, xr);
      }
    }
    return { coh, phase: ph };
  }

  // ------------------------------------------------------------------ streaming forms (one frame at a time; equal to the batch functions)
  class StftPlan {                                           // reusable single-frame STFT: frame(x, offset, outMag [, outRe, outIm])
    constructor(o) {
      this.n = o.n; this.pad = o.pad || 1; this.nfft = this.n * this.pad; this.nBins = this.nfft / 2 + 1;
      this.w = makeWindow(o.window || 'hann', this.n, o.beta); this.norm = 2 / sum(this.w);
      this.re = new Float64Array(this.nfft); this.im = new Float64Array(this.nfft);
    }
    frame(x, off, outMag, outRe, outIm) {
      const { n, re, im, w, nBins, norm } = this;
      for (let i = 0; i < n; i++) re[i] = x[off + i] * w[i];
      re.fill(0, n); im.fill(0); fft(re, im);
      for (let k = 0; k < nBins; k++) {
        outMag[k] = Math.hypot(re[k], im[k]) * norm;
        if (outRe) { outRe[k] = re[k] * norm; outIm[k] = im[k] * norm; }
      }
      return outMag;
    }
  }
  class PcenStream {                                         // same recursion as pcen(); feed(E row) -> out row (Float64Array, reused)
    constructor(nBins, o) {
      this.o = { s: o.s, alpha: o.alpha == null ? 0.98 : o.alpha, delta: o.delta == null ? 2 : o.delta, r: o.r == null ? 0.5 : o.r, eps: o.eps == null ? 1e-6 : o.eps };
      this.M = null; this.init = o.init || 'librosa'; this.nBins = nBins; this.out = new Float64Array(nBins); this.dr = Math.pow(this.o.delta, this.o.r);
    }
    feed(E) {
      const { s, alpha, delta, r, eps } = this.o, M = this.M || (this.M = Float64Array.from({ length: this.nBins }, (_, k) => this.init === 'first' ? E[k] : 1));
      for (let k = 0; k < this.nBins; k++) { M[k] = (1 - s) * M[k] + s * E[k]; this.out[k] = Math.pow(E[k] / Math.pow(eps + M[k], alpha) + delta, r) - this.dr; }
      return this.out;
    }
  }
  class BgNormStream {                                       // same tracker as bgNorm(); feed(dB row) -> level minus background row
    constructor(nBins, stepDb) { this.nBins = nBins; this.step = stepDb == null ? 0.1 : stepDb; this.B = null; this.out = new Float64Array(nBins); }
    feed(db) {
      const B = this.B || (this.B = Float64Array.from(db));
      for (let k = 0; k < this.nBins; k++) { B[k] += db[k] > B[k] ? this.step : -this.step; this.out[k] = db[k] - B[k]; }
      return this.out;
    }
  }

  // ------------------------------------------------------------------ helpers for display
  function rms(x) { let s = 0; for (let i = 0; i < x.length; i++) s += x[i] * x[i]; return Math.sqrt(s / Math.max(1, x.length)); }
  function resampleColumn(col, rowMap, out) {               // rowMap[row] = fractional bin; linear interpolation
    const n = col.length;
    for (let r = 0; r < rowMap.length; r++) { const p = Math.min(n - 1, Math.max(0, rowMap[r])), i = Math.floor(p), j = Math.min(n - 1, i + 1), t = p - i; out[r] = col[i] * (1 - t) + col[j] * t; }
    return out;
  }
  function makeRowMap(scale, nRows, nBins, fs, fmin, fmax) { // screen row 0 = fmax at the top -> fractional STFT bin index
    const S = SCALES[scale], lo = S.fwd(fmin), hi = S.fwd(fmax), m = new Float64Array(nRows), df = fs / ((nBins - 1) * 2);
    for (let r = 0; r < nRows; r++) { const f = S.inv(lo + (hi - lo) * (1 - r / (nRows - 1))); m[r] = f / df; }
    return m;
  }

  /* row -> fractional bin index when the bins are NOT linear in frequency (CQT, mel, ERB, Bark): binFreqs = ascending centre frequencies (Hz).
     Linear interpolation in Hz between neighbouring centres, clamped to the first and last bin. */
  function makeRowMapFreqs(scale, nRows, binFreqs, fmin, fmax) {
    const S = SCALES[scale], lo = S.fwd(fmin), hi = S.fwd(fmax), m = new Float64Array(nRows), n = binFreqs.length;
    for (let r = 0; r < nRows; r++) {
      const f = S.inv(lo + (hi - lo) * (1 - r / (nRows - 1)));
      if (f <= binFreqs[0]) { m[r] = 0; continue; }
      if (f >= binFreqs[n - 1]) { m[r] = n - 1; continue; }
      let a = 0, b = n - 1; while (b - a > 1) { const c = (a + b) >> 1; if (binFreqs[c] <= f) a = c; else b = c; }
      m[r] = a + (f - binFreqs[a]) / (binFreqs[b] - binFreqs[a]);
    }
    return m;
  }

  return { fft, makeWindow, WINDOWS, stft, multitaper, reassigned, cqt, cqtKernels, SCALES, filterbank, applyFilterbank, pcen, pcenSmoothing, bgNorm,
    medianFilterAxis, hpss, toDb, flux, cepstrum, freqAutocorr, coherence, rms, resampleColumn, makeRowMap, makeRowMapFreqs, frameCount, StftPlan, PcenStream, BgNormStream };
});
