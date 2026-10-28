#!/usr/bin/env node
/* Tests for static/engine/dsp.js against test/refs.json (numpy / scipy / librosa / analysis/pcen.py, built by gen_refs.py) plus property tests on
   synthetic signals. Run: node test_dsp.js  (prints PASS/FAIL lines and "N checks, M failure(s)"; exit 1 on a failure). */
'use strict';
const fs = require('fs'), path = require('path');
const D = require(path.join(__dirname, '..', 'static', 'engine', 'dsp.js'));
const R = JSON.parse(fs.readFileSync(path.join(__dirname, 'refs.json'), 'utf8'));
const FS = R.fs;
let n = 0; const fails = [];
function check(name, ok, detail) { n++; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  [' + detail + ']')); if (!ok) fails.push(name); }
const dec = (b) => { const u = Buffer.from(b, 'base64'); const a = new Uint8Array(u.length); a.set(u); return new Float64Array(a.buffer); };
function maxErr(a, b) { if (a.length !== b.length) return Infinity; let m = 0; for (let i = 0; i < a.length; i++) m = Math.max(m, Math.abs(a[i] - b[i])); return m; }
function maxRel(a, b, floor) { let m = 0; for (let i = 0; i < a.length; i++) m = Math.max(m, Math.abs(a[i] - b[i]) / Math.max(Math.abs(b[i]), floor)); return m; }
const X = {}; for (const k of Object.keys(R.inputs)) X[k] = dec(R.inputs[k]);
function lcg(seed) { let s = seed >>> 0; return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; }; }
function gauss(rnd) { return Math.sqrt(-2 * Math.log(1 - rnd())) * Math.cos(2 * Math.PI * rnd()); }

// ---- fft
{
  const re = new Float64Array(8), im = new Float64Array(8); re[0] = 1; D.fft(re, im);
  check('fft: an impulse is flat (all bins 1+0j)', maxErr(re, new Float64Array(8).fill(1)) < 1e-12 && maxErr(im, new Float64Array(8)) < 1e-12, '');
  const N = 64, r2 = new Float64Array(N), i2 = new Float64Array(N); for (let i = 0; i < N; i++) r2[i] = Math.cos(2 * Math.PI * 5 * i / N); D.fft(r2, i2);
  check('fft: a cosine at bin 5 peaks at bins 5 and 59 with N/2', Math.abs(r2[5] - 32) < 1e-9 && Math.abs(r2[59] - 32) < 1e-9 && Math.abs(r2[6]) < 1e-9, r2[5] + ' ' + r2[6]);
  let thrown = null; try { D.fft(new Float64Array(12), new Float64Array(12)); } catch (e) { thrown = String(e.message); }
  check('fft: a length that is not a power of 2 throws a message naming the size', thrown && thrown.indexOf('power of 2') >= 0 && thrown.indexOf('12') >= 0, thrown);
  let ok = true; try { D.fft(new Float64Array(16), new Float64Array(16)); } catch (e) { ok = false; }
  check('fft: positive twin, length 16 runs', ok, '');
}
// ---- windows vs scipy
for (const k of Object.keys(R.windows)) {
  const name = k === 'kaiser4' ? 'kaiser' : k, w = D.makeWindow(name, 64, k === 'kaiser4' ? 4.0 : 8.6);
  check('window ' + k + ' (n=64, periodic' + (k.indexOf('kaiser') === 0 ? ', beta ' + (k === 'kaiser4' ? 4 : 8.6) : '') + ') equals scipy.signal.get_window within 1e-12', maxErr(w, dec(R.windows[k])) < 1e-12, maxErr(w, dec(R.windows[k])));
}
// ---- stft
{
  const s = D.stft(X.x1, { n: 256, hop: 64, pad: 2, window: 'hann' }), ref = dec(R.stft_hann_pad2.mag);
  check('stft hann n=256 hop=64 pad=2 equals numpy (and librosa, checked in gen_refs) within 1e-9, ' + s.nFrames + ' frames x ' + s.nBins + ' bins',
    s.nFrames === R.stft_hann_pad2.frames && s.nBins === R.stft_hann_pad2.bins && maxErr(s.mag, ref) < 1e-9, maxErr(s.mag, ref));
  const b = D.stft(X.x1, { n: 128, hop: 128, pad: 1, window: 'blackmanharris' }), rb = dec(R.stft_bh.mag);
  check('stft blackmanharris n=128 hop=128 equals numpy within 1e-9', maxErr(b.mag, rb) < 1e-9, maxErr(b.mag, rb));
  const tone = new Float64Array(2048); for (let i = 0; i < 2048; i++) tone[i] = 0.5 * Math.sin(2 * Math.PI * (32 / 256) * i);   // bin-centred (bin 32 of 256)
  const t = D.stft(tone, { n: 256, hop: 256, window: 'hann' });
  check('stft: a bin-centred sine of amplitude 0.5 reads 0.5 at its bin (amplitude calibration)', Math.abs(t.mag[32] - 0.5) < 1e-9, t.mag[32]);
  const t2 = D.stft(tone.map(v => 2 * v), { n: 256, hop: 256, window: 'hann' });
  check('stft: positive twin, amplitude 1.0 reads 1.0', Math.abs(t2.mag[32] - 1.0) < 1e-9, t2.mag[32]);
}
// ---- multitaper
{
  const m = D.multitaper(X.x1, { n: 256, hop: 128, K: 4 }), ref = dec(R.multitaper.mag);
  check('multitaper n=256 hop=128 K=4 equals numpy within 1e-9', m.nFrames === R.multitaper.frames && maxErr(m.mag, ref) < 1e-9, maxErr(m.mag, ref));
  const nb = 129, tone = new Float64Array(1024); for (let i = 0; i < 1024; i++) tone[i] = Math.sin(2 * Math.PI * (Math.round(nb / 4) / 256) * i);
  const t = D.multitaper(tone, { n: 256, hop: 256, K: 4 }); let pk = 0; for (let k = 0; k < nb; k++) pk = Math.max(pk, t.mag[k]);
  check('multitaper: a bin-centred unit sine peaks at 1.0 (calibration)', Math.abs(pk - 1) < 1e-9, pk);
  const rnd = lcg(5), noise = new Float64Array(16384); for (let i = 0; i < noise.length; i++) noise[i] = 0.1 * gauss(rnd);
  const sd = (mag, nF, nBn) => { const v = []; for (let f = 0; f < nF; f++) for (let k = 5; k < nBn - 5; k++) v.push(20 * Math.log10(mag[f * nBn + k] + 1e-12)); const mu = v.reduce((a, b) => a + b, 0) / v.length; return Math.sqrt(v.reduce((a, b) => a + (b - mu) * (b - mu), 0) / v.length); };
  const a = D.stft(noise, { n: 256, hop: 256, window: 'hann' }), b = D.multitaper(noise, { n: 256, hop: 256, K: 4 });
  const sA = sd(a.mag, a.nFrames, a.nBins), sB = sd(b.mag, b.nFrames, b.nBins);
  check('multitaper: the dB scatter of white noise is lower than the single-window STFT (' + sB.toFixed(2) + ' < ' + sA.toFixed(2) + ' dB)', sB < 0.7 * sA, sA + ' ' + sB);
}
// ---- reassigned vs librosa
{
  const o = R.reassigned, r = D.reassigned(X.x2, { n: o.n, hop: o.hop }), fh = dec(o.freq_hz), th = dec(o.time_s), mg = dec(o.mag);
  let mx = 0; for (const v of mg) mx = Math.max(mx, v);
  let dF = 0, dT = 0, cnt = 0;
  for (let f = 0; f < r.nFrames; f++) for (let k = 0; k < r.nBins; k++) {
    const i = f * r.nBins + k; if (mg[i] < 0.2 * mx) continue; cnt++;
    dF = Math.max(dF, Math.abs(r.fHat[i] * FS / (r.nBins - 1) / 2 - fh[i])); dT = Math.max(dT, Math.abs((r.tHat[i] * o.hop + o.n / 2) / FS - th[i]));
  }
  // librosa differentiates the window with np.gradient (finite difference) and ramps time from (n-1)/2; this code uses the analytic derivative of the hann
  // window and a ramp from n/2, so the two agree to about 1e-4 of a bin (0.006 Hz) and 0.14 ms (a quarter of a hop), not to rounding.
  check('reassigned: per-cell frequency equals librosa.reassigned_spectrogram within 0.02 Hz on ' + cnt + ' strong cells', cnt > 100 && dF < 0.02, dF);
  check('reassigned: per-cell time equals librosa within 0.25 ms on the same cells (the sign convention matches: a wrong sign errs by up to 8 ms)', dT < 2.5e-4, dT);
  { // ground truth, independent of librosa: an impulse at sample 1000 and a 1000.5 Hz tone
    const imp = new Float64Array(2048); imp[1000] = 1;
    const ri = D.reassigned(imp, { n: 256, hop: 64 }); let worst = 0, c = 0;
    for (let f = 0; f < ri.nFrames; f++) for (let k = 5; k < ri.nBins - 5; k++) { const i = f * ri.nBins + k; if (Math.abs(ri.tHat[i] * 64 + 128 - 1000) < 60) { worst = Math.max(worst, Math.abs(ri.tHat[i] * 64 + 128 - 1000)); c++; } }
    check('reassigned: an impulse at sample 1000 is placed at sample 1000 (+-0.01 sample) on ' + c + ' cells', c > 100 && worst < 0.01, worst);
    const tn = new Float64Array(2048); for (let i = 0; i < 2048; i++) tn[i] = 0.5 * Math.sin(2 * Math.PI * 1000.5 / FS * i);
    const rt = D.reassigned(tn, { n: 256, hop: 64 }); let wf = 0; const kc = Math.round(1000.5 / (FS / 256));
    for (let f = 4; f < rt.nFrames - 4; f++) for (let k = kc - 1; k <= kc + 1; k++) wf = Math.max(wf, Math.abs(rt.fHat[f * rt.nBins + k] * FS / 256 - 1000.5));
    check('reassigned: a 1000.5 Hz tone is estimated at 1000.5 Hz (+-0.02 Hz) in the 3 bins around it', wf < 0.02, wf);
    const rz = D.reassigned(imp.map(v => 0), { n: 256, hop: 64 }); let moved = 0; for (const v of rz.mag) moved += v;
    check('reassigned: positive twin, silence reassigns no energy (sum 0)', moved === 0, moved);
  }
  const tone = new Float64Array(2048); for (let i = 0; i < 2048; i++) tone[i] = 0.5 * Math.sin(2 * Math.PI * 1000.5 / FS * i);
  const st = D.stft(tone, { n: 256, hop: 64 }), re = D.reassigned(tone, { n: 256, hop: 64 });
  const conc = (m, nF, nB) => { let near = 0, tot = 0; const kc = Math.round(1000.5 / (FS / 256)); for (let f = 4; f < nF - 4; f++) for (let k = 0; k < nB; k++) { const p = m[f * nB + k] ** 2; tot += p; if (Math.abs(k - kc) <= 1) near += p; } return near / tot; };
  const cS = conc(st.mag, st.nFrames, st.nBins), cR = conc(re.mag, re.nFrames, re.nBins);
  check('reassigned: a stationary tone concentrates into 3 bins (' + cR.toFixed(3) + ') more than the STFT (' + cS.toFixed(3) + ')', cR > 0.99 && cR > cS, cS + ' ' + cR);
}
// ---- cqt
{
  const o = R.cqt, c = D.cqt(X.x2, { fs: FS, fmin: o.fmin, bpo: o.bpo, nBins: o.nBins, hop: o.hop, window: 'hann' }), cg = D.cqt(X.x2, { fs: FS, fmin: o.fmin, bpo: o.bpo, nBins: o.nBins, hop: o.hop, window: 'gauss' });
  check('cqt (hann atoms) equals the numpy direct implementation within 1e-9', c.nFrames === o.frames && maxErr(c.mag, dec(o.mag)) < 1e-9, maxErr(c.mag, dec(o.mag)));
  check('cqt (gaussian / Morlet atoms) equals numpy within 1e-9', maxErr(cg.mag, dec(o.mag_gauss)) < 1e-9, maxErr(cg.mag, dec(o.mag_gauss)));
  const tone = (f) => { const x = new Float64Array(8000); for (let i = 0; i < x.length; i++) x[i] = 0.5 * Math.sin(2 * Math.PI * f / FS * i); return x; };
  const peak = (f) => { const r = D.cqt(tone(f), { fs: FS, fmin: 200, bpo: 12, nBins: 60, hop: 400, window: 'hann' }); let b = 0, v = 0; const row = Math.floor(r.nFrames / 2); for (let k = 0; k < 60; k++) if (r.mag[row * 60 + k] > v) { v = r.mag[row * 60 + k]; b = k; } return [b, v]; };
  const [b1, v1] = peak(1000), [b2] = peak(2000);
  check('cqt: a 1000 Hz tone peaks at bin ' + Math.round(12 * Math.log2(1000 / 200)) + ' with amplitude 0.5 (+-5 percent)', b1 === Math.round(12 * Math.log2(1000 / 200)) && Math.abs(v1 - 0.5) < 0.025, b1 + ' ' + v1);
  check('cqt: positive twin, a 2000 Hz tone moves the peak up one octave (12 bins)', b2 - b1 === 12, b1 + ' ' + b2);
}
// ---- filterbanks
{
  const m = D.filterbank('mel', 40, 512, FS, 100, 7500); check('mel filterbank (htk, peak 1) equals librosa.filters.mel within 1e-12', maxErr(m.fb, dec(R.mel_fb.fb)) < 1e-12, maxErr(m.fb, dec(R.mel_fb.fb)));
  const e = D.filterbank('erb', 32, 512, FS, 100, 7500), b = D.filterbank('bark', 24, 512, FS, 100, 7500);
  check('erb filterbank equals the numpy triangular construction within 1e-12', maxErr(e.fb, dec(R.erb_fb.fb)) < 1e-12, maxErr(e.fb, dec(R.erb_fb.fb)));
  check('bark filterbank equals the numpy construction within 1e-12', maxErr(b.fb, dec(R.bark_fb.fb)) < 1e-12, maxErr(b.fb, dec(R.bark_fb.fb)));
  const sp = e.centres.map((c, i) => i ? c - e.centres[i - 1] : 0).slice(1);
  check('erb centres widen with frequency (first spacing < last spacing), twin of linear scale being constant', sp[0] < sp[sp.length - 1], sp[0] + ' ' + sp[sp.length - 1]);
  const li = D.filterbank('lin', 10, 512, FS, 100, 7500), ls = li.centres.map((c, i) => i ? c - li.centres[i - 1] : 0).slice(1);
  check('linear centres are evenly spaced (max spread < 1e-9)', Math.max(...ls) - Math.min(...ls) < 1e-9, '');
}
// ---- pcen
{
  const o = R.pcen, E = dec(o.E), a = D.pcen(E, o.frames, o.bins, { s: o.s, init: 'librosa' }), b = D.pcen(E, o.frames, o.bins, { s: o.s, init: 'first' });
  check('pcen (librosa init) equals analysis/pcen.py and librosa.pcen: max relative error < 1e-9', maxRel(a, dec(o.out_librosa_init), 1e-9) < 1e-9, maxRel(a, dec(o.out_librosa_init), 1e-9));
  check('pcen (first-frame init) equals analysis/pcen.py init=first: max relative error < 1e-9', maxRel(b, dec(o.out_first_init), 1e-9) < 1e-9, maxRel(b, dec(o.out_first_init), 1e-9));
  check('pcen: the two inits differ on frame 0 (positive twin of the equality above)', maxErr(a.subarray(0, o.bins), b.subarray(0, o.bins)) > 1e-3, '');
  check('pcenSmoothing(0.4 s, 250 fps) = librosa b = ' + o.s.toFixed(6), Math.abs(D.pcenSmoothing(0.4, 250) - o.s) < 1e-15, D.pcenSmoothing(0.4, 250));
}
// ---- bgNorm
{
  const o = R.bgnorm, r = D.bgNorm(dec(o.db), o.frames, o.bins, { stepDb: 0.1 });
  check('bgNorm equals the numpy recursion within 1e-9', maxErr(r, dec(o.out)) < 1e-9, maxErr(r, dec(o.out)));
  const rnd = lcg(11), N = 48000, x = new Float64Array(N); for (let i = 0; i < N; i++) x[i] = 0.01 * gauss(rnd) + 0.1 * Math.sin(2 * Math.PI * (40 / 256) * i);
  const s = D.stft(x, { n: 256, hop: 128 }), db = D.toDb(s.mag), bn = D.bgNorm(db, s.nFrames, s.nBins, { stepDb: 0.1 });
  const med = (k) => { const v = []; for (let f = 100; f < s.nFrames; f++) v.push(bn[f * s.nBins + k]); v.sort((p, q) => p - q); return v[v.length >> 1]; };
  check('bgNorm: stationary white noise reads about 0 dB (|median| < 1 dB at bins 100 and 20) while a steady tone also reads about 0 (it is the background)', Math.abs(med(100)) < 1 && Math.abs(med(20)) < 1 && Math.abs(med(40)) < 2, med(100) + ' ' + med(20) + ' ' + med(40));
  const x2 = x.slice(); for (let i = 30000; i < 33000; i++) x2[i] += 0.2 * Math.sin(2 * Math.PI * (80 / 256) * i);
  const s2 = D.stft(x2, { n: 256, hop: 128 }), b2 = D.bgNorm(D.toDb(s2.mag), s2.nFrames, s2.nBins, { stepDb: 0.1 }); const fm = Math.floor(31500 / 128);
  check('bgNorm: positive twin, a new tone burst at bin 80 stands out above 15 dB over the background', b2[fm * s2.nBins + 80] > 15, b2[fm * s2.nBins + 80]);
}
// ---- hpss
{
  const o = R.hpss, S = dec(o.S), r = D.hpss(S, o.frames, o.bins, { kernelTime: 17, kernelFreq: 17 }), mh = dec(o.mask_h), mp = dec(o.mask_p);
  let eh = 0, ep = 0; for (let i = 0; i < S.length; i++) if (S[i] > 1e-9) { eh = Math.max(eh, Math.abs(r.harm[i] / S[i] - mh[i])); ep = Math.max(ep, Math.abs(r.perc[i] / S[i] - mp[i])); }
  check('hpss harmonic mask equals librosa.decompose.hpss (kernel 17/17, power 2, margin 1) within 1e-9', eh < 1e-9, eh);
  check('hpss percussive mask equals librosa within 1e-9', ep < 1e-9, ep);
  const nF = 40, nB = 33, tone = new Float64Array(nF * nB), click = new Float64Array(nF * nB);
  for (let f = 0; f < nF; f++) { tone[f * nB + 10] = 1; } for (let k = 0; k < nB; k++) click[20 * nB + k] = 1;
  const th = D.hpss(tone, nF, nB, { kernelTime: 9, kernelFreq: 9 }), tc = D.hpss(click, nF, nB, { kernelTime: 9, kernelFreq: 9 });
  check('hpss: a steady line is harmonic (mask > 0.99) and a broadband click is percussive (mask > 0.99)', th.harm[20 * nB + 10] > 0.99 && tc.perc[20 * nB + 16] > 0.99, th.harm[20 * nB + 10] + ' ' + tc.perc[20 * nB + 16]);
  check('hpss: positive twin, the steady line is not percussive and the click is not harmonic (< 0.01)', th.perc[20 * nB + 10] < 0.01 && tc.harm[20 * nB + 16] < 0.01, th.perc[20 * nB + 10] + ' ' + tc.harm[20 * nB + 16]);
}
// ---- flux, cepstrum, autocorrelation
{
  const o = R.flux, f = D.flux(dec(o.db), o.frames, o.bins); check('flux equals the numpy formula within 1e-9', maxErr(f, dec(o.out)) < 1e-9, maxErr(f, dec(o.out)));
  const c = R.cepstrum, S3 = dec(c.S), ce = D.cepstrum(S3, c.frames, c.bins, c.nq);
  check('cepstrum equals numpy irfft(log mag) within 1e-9', maxErr(ce, dec(c.out)) < 1e-9, maxErr(ce, dec(c.out)));
  let b = 0, v = -1e9; const row = 6; for (let q = 20; q < c.nq; q++) if (ce[row * c.nq + q] > v) { v = ce[row * c.nq + q]; b = q; }
  check('cepstrum: the harmonic stack f0=220 Hz peaks at quefrency ' + Math.round(FS / 220) + ' samples (+-1)', Math.abs(b - Math.round(FS / 220)) <= 1, b);
  const a = R.autocorr, ac = D.freqAutocorr(S3, c.frames, c.bins, a.nLag);
  check('frequency autocorrelation equals the numpy formula within 1e-9', maxErr(ac, dec(a.out)) < 1e-9, maxErr(ac, dec(a.out)));
  let lag = 0, lv = -1; for (let l = 3; l < a.nLag; l++) if (ac[row * a.nLag + l] > lv) { lv = ac[row * a.nLag + l]; lag = l; }
  check('frequency autocorrelation: f0=220 Hz gives a peak at lag 7 bins (220 / 31.25 = 7.04)', lag === 7, lag);
}
// ---- coherence
{
  const o = R.coherence, A = D.stft(X.xa, { n: o.n, hop: o.hop, complex: true }), B = D.stft(X.xb, { n: o.n, hop: o.hop, complex: true }), r = D.coherence(A, B, A.nFrames, A.nBins, o.M);
  check('coherence equals the numpy boxcar-averaged estimate within 1e-9', maxErr(r.coh, dec(o.coh)) < 1e-9, maxErr(r.coh, dec(o.coh)));
  check('coherence phase equals numpy within 1e-9 where coherence > 0.5', (() => { const p = dec(o.phase), c = dec(o.coh); let m = 0; for (let i = 0; i < p.length; i++) if (c[i] > 0.5) m = Math.max(m, Math.abs(r.phase[i] - p[i])); return m < 1e-9; })(), '');
  const kc = Math.round(1200 / (FS / o.n)), mid = Math.floor(A.nFrames / 2);
  const expPh = 2 * Math.PI * (kc * FS / o.n) * o.delay_samples / FS;
  check('coherence: a shared 1200 Hz tone is coherent (> 0.95) with phase = 2 pi f delay (' + expPh.toFixed(3) + ' rad, +-0.15)', r.coh[mid * A.nBins + kc] > 0.95 && Math.abs(r.phase[mid * A.nBins + kc] - expPh) < 0.15, r.coh[mid * A.nBins + kc] + ' ' + r.phase[mid * A.nBins + kc]);
  let s = 0, c = 0; for (let k = 40; k < 100; k++) { s += r.coh[mid * A.nBins + k]; c++; }
  check('coherence: positive twin, independent noise bins average about 1/M = 0.11 (0.03 to 0.3)', s / c > 0.03 && s / c < 0.3, s / c);
}
// ---- streaming forms equal the batch forms
{
  const plan = new D.StftPlan({ n: 256, pad: 2, window: 'hann' }), batch = D.stft(X.x1, { n: 256, hop: 64, pad: 2, window: 'hann' }), col = new Float64Array(plan.nBins);
  let e = 0; for (let f = 0; f < batch.nFrames; f++) { plan.frame(X.x1, f * 64, col); for (let k = 0; k < plan.nBins; k++) e = Math.max(e, Math.abs(col[k] - batch.mag[f * plan.nBins + k])); }
  check('StftPlan frame-by-frame equals stft() on all ' + batch.nFrames + ' frames within 1e-12', e < 1e-12, e);
  const o = R.pcen, E = dec(o.E);
  for (const init of ['librosa', 'first']) {
    const ps = new D.PcenStream(o.bins, { s: o.s, init }), ref = D.pcen(E, o.frames, o.bins, { s: o.s, init }); let m = 0;
    for (let f = 0; f < o.frames; f++) { const row = ps.feed(E.subarray(f * o.bins, (f + 1) * o.bins)); for (let k = 0; k < o.bins; k++) m = Math.max(m, Math.abs(row[k] - ref[f * o.bins + k])); }
    check('PcenStream (init ' + init + ') equals pcen() on all frames exactly (max difference ' + m + ')', m === 0, m);
  }
  const bg = R.bgnorm, db = dec(bg.db), bs = new D.BgNormStream(bg.bins, 0.1), br = D.bgNorm(db, bg.frames, bg.bins, { stepDb: 0.1 }); let mb = 0;
  for (let f = 0; f < bg.frames; f++) { const row = bs.feed(db.subarray(f * bg.bins, (f + 1) * bg.bins)); for (let k = 0; k < bg.bins; k++) mb = Math.max(mb, Math.abs(row[k] - br[f * bg.bins + k])); }
  check('BgNormStream equals bgNorm() on all frames exactly (max difference ' + mb + ')', mb === 0, mb);
}
// ---- display helpers
{
  const m = D.makeRowMap('log', 100, 129, FS, 200, 6000), df = FS / 256;
  check('makeRowMap log: row 0 is fmax and the last row is fmin (in fractional bins)', Math.abs(m[0] - 6000 / df) < 1e-9 && Math.abs(m[99] - 200 / df) < 1e-9, m[0] + ' ' + m[99]);
  const cf = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000], rm = D.makeRowMapFreqs('lin', 10, cf, 100, 1000);
  check('makeRowMapFreqs lin: rows that land on a centre map to exactly that bin (row r -> 9 - r)', rm.every((v, r) => Math.abs(v - (9 - r)) < 1e-9), Array.from(rm).join(','));
  const rm2 = D.makeRowMapFreqs('log', 50, cf, 50, 2000);
  check('makeRowMapFreqs clamps outside the centres (first row = last bin, last row = bin 0) and falls monotonically down the screen', rm2[0] === 9 && rm2[49] === 0 && rm2.every((v, r) => r === 0 || v <= rm2[r - 1]), rm2[0] + ' ' + rm2[49]);
  const rm3 = D.makeRowMapFreqs('lin', 3, cf, 150, 950);
  check('makeRowMapFreqs interpolates linearly between centres (150 Hz -> 0.5, 550 Hz -> 4.5, 950 Hz -> 8.5)', Math.abs(rm3[2] - 0.5) < 1e-9 && Math.abs(rm3[1] - 4.5) < 1e-9 && Math.abs(rm3[0] - 8.5) < 1e-9, Array.from(rm3).join(','));
  const col = new Float64Array([0, 10, 20, 30]), out = new Float64Array(3); D.resampleColumn(col, [0.5, 2.25, 9], out);
  check('resampleColumn interpolates linearly and clamps to the last bin (5, 22.5, 30)', Math.abs(out[0] - 5) < 1e-12 && Math.abs(out[1] - 22.5) < 1e-12 && out[2] === 30, Array.from(out).join(','));
  check('rms of a unit sine is 1/sqrt(2)', Math.abs(D.rms(Float64Array.from({ length: 4800 }, (_, i) => Math.sin(2 * Math.PI * i / 48))) - Math.SQRT1_2) < 1e-9, '');
}
console.log('\n' + n + ' checks, ' + fails.length + ' failure(s)');
process.exit(fails.length ? 1 : 0);
