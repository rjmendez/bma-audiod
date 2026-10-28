#!/usr/bin/env python3
"""Server-side (numpy/scipy/librosa) cost of the same transforms: ms of CPU per second of audio per beam at 48 kHz, 10 s of synthetic audio, min of 3.
    python bench_np.py     (WSL audioeval venv: needs numpy, scipy, librosa)"""
import time

import numpy as np
import scipy.ndimage as ndi
import scipy.signal as sg
import librosa

FS, SECS = 48000, 10
r = np.random.default_rng(1)
t = np.arange(FS * SECS) / FS
x = 0.05 * np.sin(2 * np.pi * 4000 * t) + 0.03 * np.sin(2 * np.pi * 440 * t) * (1 + np.sin(2 * np.pi * 3 * t)) + 0.01 * (r.random(t.size) - 0.5)
y = 0.05 * np.sin(2 * np.pi * 4000 * t) + 0.01 * (r.random(t.size) - 0.5)


def frames(sig, n, hop):
    nf = (len(sig) - n) // hop + 1
    return np.lib.stride_tricks.as_strided(sig, (nf, n), (sig.strides[0] * hop, sig.strides[0]))


def stft(sig, n, hop, pad=1, win="hann"):
    w = sg.get_window(win, n)
    return np.fft.rfft(frames(sig, n, hop) * w, n * pad, axis=1) * (2 / w.sum())


def best(f):
    f()
    b = 1e9
    for _ in range(3):
        t0 = time.perf_counter()
        f()
        b = min(b, time.perf_counter() - t0)
    return b * 1000 / SECS


rows = []
S = stft(x, 1024, 480)
mag = np.abs(S)
power = mag ** 2
mel = librosa.filters.mel(sr=FS, n_fft=1024, n_mels=64, fmin=100, fmax=15500, htk=True, norm=None)
db = 20 * np.log10(np.maximum(mag, 1e-12))


def tap(n, K):
    return [np.sqrt(2 / (n + 1)) * np.sin(np.pi * k * (np.arange(n) + 1) / (n + 1)) for k in range(1, K + 1)]


def mtap():
    fr = frames(x, 1024, 480)
    return np.sqrt(sum(np.abs(np.fft.rfft(fr * w, axis=1)) ** 2 for w in tap(1024, 4)) / 4)


def bgnorm():
    B = db[0].copy()
    out = np.empty_like(db)
    for f in range(db.shape[0]):
        B = B + np.where(db[f] > B, 0.1, -0.1)
        out[f] = db[f] - B
    return out


def cep():
    return np.fft.irfft(np.log(mag + 1e-12), 1024, axis=1)[:, :300]


def acorr():
    sp = np.fft.rfft(mag, 2048, axis=1)
    return np.fft.irfft(np.abs(sp) ** 2, 2048, axis=1)[:, :120]


def coh():
    A, B = S, stft(y, 1024, 480)
    k = lambda z: ndi.uniform_filter1d(z, 9, axis=0, mode="nearest")
    pxy = k(A * np.conj(B)).real ** 2 + k(A * np.conj(B)).imag ** 2
    return pxy / (k(np.abs(A) ** 2) * k(np.abs(B) ** 2) + 1e-300)


spec = [
    ("STFT 1024 / hop 10 ms (hann)", lambda: stft(x, 1024, 480)),
    ("STFT 4096 / hop 20 ms, pad 1", lambda: stft(x, 4096, 960)),
    ("STFT 512 / hop 5 ms (fast events)", lambda: stft(x, 512, 240)),
    ("STFT 1024 + zero-pad x2", lambda: stft(x, 1024, 480, 2)),
    ("Multitaper 1024 / 10 ms, K=4", mtap),
    ("Reassigned 1024 / 10 ms (librosa)", lambda: librosa.reassigned_spectrogram(y=x, sr=FS, n_fft=1024, hop_length=480, center=False)),
    ("CQT 24 bpo, 200 Hz..12.8 kHz (144 bins), hop 20 ms (librosa)", lambda: librosa.cqt(x, sr=FS, hop_length=960, fmin=200, n_bins=144, bins_per_octave=24)),
    ("Mel 64 bands (filterbank only)", lambda: power @ mel.T),
    ("PCEN on 64 mel bands (librosa)", lambda: librosa.pcen((power @ mel.T).T * 2 ** 30, sr=FS, hop_length=480)),
    ("Background normalisation (513 bins)", bgnorm),
    ("HPSS 17 x 17 on 513 bins (librosa)", lambda: librosa.decompose.hpss(mag.T, kernel_size=(17, 17), mask=True)),
    ("Spectral flux strip", lambda: np.maximum(np.diff(db, axis=0), 0).sum(axis=1)),
    ("Cepstrum strip (300 quefrency bins)", cep),
    ("Frequency autocorrelation strip (120 lags, FFT)", acorr),
    ("Coherence + phase of a beam PAIR, 9 frames", coh),
]
w = max(len(n) for n, _ in spec)
for name, f in spec:
    print(name.ljust(w), ("%.2f" % best(f)).rjust(8), "ms/s")
