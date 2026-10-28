#!/usr/bin/env node
/* ms of CPU per second of audio per beam (48 kHz) for each transform in static/engine/dsp.js, measured on 10 s of synthetic audio (min of 3 runs after a warm-up).
   node bench_costs.js [--json] */
'use strict';
const path = require('path');
const D = require(path.join(__dirname, '..', '..', 'static', 'engine', 'dsp.js'));
const FS = 48000, SECS = 10, N = FS * SECS;
let s = 12345; const rnd = () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
const x = new Float64Array(N), y = new Float64Array(N);
for (let i = 0; i < N; i++) { const t = i / FS; const base = 0.05 * Math.sin(2 * Math.PI * 4000 * t) + 0.03 * Math.sin(2 * Math.PI * 440 * t) * (1 + Math.sin(2 * Math.PI * 3 * t)); x[i] = base + 0.01 * (rnd() - 0.5); y[i] = base + 0.01 * (rnd() - 0.5); }
function time(f) { f(); let best = 1e9; for (let r = 0; r < 3; r++) { const t0 = process.hrtime.bigint(); f(); best = Math.min(best, Number(process.hrtime.bigint() - t0) / 1e6); } return best / SECS; }
const rows = [];
const add = (name, f, note) => rows.push({ name, ms_per_s: +time(f).toFixed(2), note });
const P1024 = { n: 1024, hop: 480, window: 'hann' };
let st1024; add('STFT 1024 / hop 10 ms (hann)', () => { st1024 = D.stft(x, P1024); }, '513 bins, 94 Hz resolution');
add('STFT 4096 / hop 20 ms, pad 1', () => D.stft(x, { n: 4096, hop: 960, window: 'hann' }), '2049 bins, 12 Hz resolution');
add('STFT 512 / hop 5 ms (fast events)', () => D.stft(x, { n: 512, hop: 240, window: 'hann' }), '257 bins, 94 Hz, 200 fps');
add('STFT 1024 + zero-pad x2', () => D.stft(x, { n: 1024, hop: 480, pad: 2, window: 'hann' }), 'interpolated display');
add('Multitaper 1024 / 10 ms, K=4', () => D.multitaper(x, { n: 1024, hop: 480, K: 4 }), '4 FFTs per frame');
add('Reassigned 1024 / 10 ms', () => D.reassigned(x, { n: 1024, hop: 480 }), '3 FFTs per frame + bookkeeping');
add('CQT 24 bpo, 200 Hz..12.8 kHz (144 bins), hop 20 ms', () => D.cqt(x, { fs: FS, fmin: 200, bpo: 24, nBins: 144, hop: 960, window: 'hann' }), 'direct atoms');
add('Morlet / Gaussian atoms, 12 bpo, 200 Hz..12.8 kHz (72 bins), hop 10 ms', () => D.cqt(x, { fs: FS, fmin: 200, bpo: 12, nBins: 72, hop: 480, window: 'gauss', qScale: 0.5 }), 'direct atoms');
const mel = D.filterbank('mel', 64, 1024, FS, 100, 15500), erb = D.filterbank('erb', 48, 1024, FS, 100, 15500), bark = D.filterbank('bark', 24, 1024, FS, 100, 15500);
const pw = new Float64Array(st1024.mag.length); for (let i = 0; i < pw.length; i++) pw[i] = st1024.mag[i] * st1024.mag[i];
add('Mel 64 bands (from the STFT, filterbank only)', () => D.applyFilterbank(pw, st1024.nFrames, st1024.nBins, mel), 'dense matrix apply');
add('ERB 48 bands (filterbank only)', () => D.applyFilterbank(pw, st1024.nFrames, st1024.nBins, erb), '');
add('Bark 24 bands (filterbank only)', () => D.applyFilterbank(pw, st1024.nFrames, st1024.nBins, bark), '');
const melP = D.applyFilterbank(pw, st1024.nFrames, st1024.nBins, mel);
add('PCEN on 64 mel bands (100 fps)', () => D.pcen(melP.map(v => v * 1073741824), st1024.nFrames, 64, { s: D.pcenSmoothing(0.4, 100), init: 'first' }), 'recursive, per bin');
add('PCEN on 513 STFT bins (100 fps)', () => D.pcen(pw.map(v => v * 1073741824), st1024.nFrames, st1024.nBins, { s: D.pcenSmoothing(0.4, 100), init: 'first' }), '');
const db = D.toDb(st1024.mag);
add('dB conversion of 513 bins (100 fps)', () => D.toDb(st1024.mag), '');
add('Background normalisation (513 bins)', () => D.bgNorm(db, st1024.nFrames, st1024.nBins, { stepDb: 0.1 }), 'streaming median');
add('HPSS 17 x 17 on 513 bins', () => D.hpss(st1024.mag, st1024.nFrames, st1024.nBins, { kernelTime: 17, kernelFreq: 17 }), 'two median filters (JS sort per window; a rolling-median structure would cut this)');
add('Spectral flux strip', () => D.flux(db, st1024.nFrames, st1024.nBins), '');
add('Cepstrum strip (nfft 1024, 300 quefrency bins)', () => D.cepstrum(st1024.mag, st1024.nFrames, st1024.nBins, 300), 'one FFT per frame');
add('Frequency autocorrelation strip (120 lags)', () => D.freqAutocorr(st1024.mag, st1024.nFrames, st1024.nBins, 120), '');
let A, B;
add('Complex STFT of one beam (for coherence), 1024', () => { A = D.stft(x, { ...P1024, complex: true }); }, 'per beam');
B = D.stft(y, { ...P1024, complex: true });
add('Coherence + phase of a beam PAIR, 9 frames', () => D.coherence(A, B, A.nFrames, A.nBins, 9), 'per pair; 28 pairs for 8 beams');
if (process.argv.includes('--json')) console.log(JSON.stringify(rows)); else { const w = Math.max(...rows.map(r => r.name.length)); for (const r of rows) console.log(r.name.padEnd(w) + '  ' + String(r.ms_per_s).padStart(8) + ' ms/s   ' + r.note); }
