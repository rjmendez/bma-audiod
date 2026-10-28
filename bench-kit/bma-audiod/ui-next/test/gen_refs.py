#!/usr/bin/env python3
"""Writes test/refs.json: reference vectors for static/engine/dsp.js from numpy / scipy / librosa (INDEPENDENT implementations) and the repo's
analysis/pcen.py. Needs numpy, scipy, librosa (WSL audioeval venv; it is only used to build the fixture, the tests need Node alone).
    python gen_refs.py            (run from this directory; fs = 16000 so the fixture stays small)
Arrays are float64, little endian, base64."""
import base64
import json
import os
import sys

import numpy as np
import scipy.signal as sg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "analysis"))
sys.path.insert(0, os.path.join(HERE, ".."))
import pcen as P  # noqa: E402

FS = 16000


def b64(a):
    return base64.b64encode(np.ascontiguousarray(a, dtype="<f8").tobytes()).decode("ascii")


def stft_np(x, n, hop, pad, win):
    w = sg.get_window(win, n, fftbins=True) if not isinstance(win, np.ndarray) else win
    nfft = n * pad
    nF = (len(x) - n) // hop + 1
    X = np.array([np.fft.rfft(x[f * hop:f * hop + n] * w, nfft) for f in range(nF)])
    return X * (2.0 / w.sum()), w


def main():
    r = np.random.default_rng(3)
    t = np.arange(2048) / FS
    x1 = 0.5 * np.sin(2 * np.pi * 1000.5 * t) + 0.01 * r.standard_normal(2048)
    x1[1000:1012] += np.hanning(12) * 0.6
    x2 = sg.chirp(t, 500, t[-1], 3000, method="linear") * 0.4
    t3 = np.arange(4096) / FS
    x3 = sum(0.7 ** (k - 1) * np.sin(2 * np.pi * 220.0 * k * t3) for k in range(1, 9)) * 0.1 + 0.001 * r.standard_normal(4096)
    sh = 3
    common = np.sin(2 * np.pi * 1200 * t)
    xa = 0.3 * common + 0.05 * r.standard_normal(2048)
    xb = 0.3 * np.roll(common, sh) + 0.05 * r.standard_normal(2048)
    out = {"fs": FS, "inputs": {k: b64(v) for k, v in dict(x1=x1, x2=x2, x3=x3, xa=xa, xb=xb).items()}, "len": {"x1": 2048, "x2": 2048, "x3": 4096, "xa": 2048, "xb": 2048}}

    # A windows
    out["windows"] = {k: b64(sg.get_window(v, 64, fftbins=True)) for k, v in dict(hann="hann", hamming="hamming", blackman="blackman", blackmanharris="blackmanharris", rect="boxcar").items()}
    out["windows"]["kaiser"] = b64(sg.get_window(("kaiser", 8.6), 64, fftbins=True))
    out["windows"]["kaiser4"] = b64(sg.get_window(("kaiser", 4.0), 64, fftbins=True))

    # B stft (+ librosa cross check)
    import librosa
    X, w = stft_np(x1, 256, 64, 2, "hann")
    L = librosa.stft(x1, n_fft=512, hop_length=64, win_length=256, window="hann", center=False)
    Lm = np.abs(L).T * 2.0 / w.sum()
    # librosa centres the 256-sample window inside its 512-sample frame: frame f of librosa is frame f+2 of ours (|FFT| ignores the shift)
    assert np.allclose(Lm, np.abs(X)[2:2 + Lm.shape[0]], rtol=1e-9, atol=1e-12), "numpy stft disagrees with librosa"
    out["stft_hann_pad2"] = {"n": 256, "hop": 64, "pad": 2, "mag": b64(np.abs(X)), "frames": X.shape[0], "bins": X.shape[1]}
    X2, _ = stft_np(x1, 128, 128, 1, "blackmanharris")
    out["stft_bh"] = {"n": 128, "hop": 128, "pad": 1, "mag": b64(np.abs(X2)), "frames": X2.shape[0], "bins": X2.shape[1]}

    # C multitaper (sine tapers, K=4, calibrated to a bin-centred unit sine)
    def mt(x, n, hop, K, pad):
        nfft, nb = n * pad, n * pad // 2 + 1
        tap = [np.sqrt(2.0 / (n + 1)) * np.sin(np.pi * k * (np.arange(n) + 1) / (n + 1)) for k in range(1, K + 1)]
        kc = int(round(nb / 4))
        tt = np.sin(2 * np.pi * (kc / nfft) * np.arange(n))
        p = sum(np.abs(np.fft.rfft(tt * w_, nfft)) ** 2 for w_ in tap) / K
        cal = 1.0 / np.sqrt(p.max())
        nF = (len(x) - n) // hop + 1
        res = np.array([np.sqrt(sum(np.abs(np.fft.rfft(x[f * hop:f * hop + n] * w_, nfft)) ** 2 for w_ in tap) / K) * cal for f in range(nF)])
        return res
    M = mt(x1, 256, 128, 4, 1)
    out["multitaper"] = {"n": 256, "hop": 128, "K": 4, "pad": 1, "mag": b64(M), "frames": M.shape[0], "bins": M.shape[1]}

    # D reassigned spectrogram (librosa is the independent reference for the per-cell frequency and time estimates)
    n, hop = 256, 64
    fr, tm, mg = librosa.reassigned_spectrogram(y=x2, sr=FS, n_fft=n, hop_length=hop, center=False, window="hann", reassign_frequencies=True, reassign_times=True, ref_power=1e-6, clip=False)
    out["reassigned"] = {"n": n, "hop": hop, "freq_hz": b64(fr.T), "time_s": b64(tm.T), "mag": b64(mg.T * 2.0 / sg.get_window("hann", n).sum()), "frames": fr.shape[1], "bins": fr.shape[0]}

    # E constant-Q: direct atoms in numpy (same definition) at hop 160
    def cqt_np(x, fs, fmin, bpo, nb, hop, win):
        Q = 1.0 / (2 ** (1.0 / bpo) - 1)
        nF = len(x) // hop
        res = np.zeros((nF, nb))
        for k in range(nb):
            f = fmin * 2 ** (k / bpo)
            N = max(8, int(np.ceil(Q * fs / f)))
            i = np.arange(N)
            if win == "gauss":
                ww = np.exp(-0.5 * ((i - (N - 1) / 2) / (N / 6)) ** 2)
            else:
                ww = 0.5 - 0.5 * np.cos(2 * np.pi * (i + 0.5) / N)
            atom = 2 * ww / ww.sum() * np.exp(-2j * np.pi * f * i / fs)
            for m in range(nF):
                s0 = m * hop - (N >> 1)
                lo, hi = max(0, -s0), min(N, len(x) - s0)
                res[m, k] = abs(np.dot(x[s0 + lo:s0 + hi], atom[lo:hi]))
        return res
    C = cqt_np(x2, FS, 200.0, 12, 30, 160, "hann")
    Cg = cqt_np(x2, FS, 200.0, 12, 30, 160, "gauss")
    out["cqt"] = {"fmin": 200.0, "bpo": 12, "nBins": 30, "hop": 160, "mag": b64(C), "mag_gauss": b64(Cg), "frames": C.shape[0]}

    # F filterbanks: mel vs librosa (htk, norm None); erb and bark from the formulas with numpy
    F = librosa.filters.mel(sr=FS, n_fft=512, n_mels=40, fmin=100.0, fmax=7500.0, htk=True, norm=None, dtype=np.float64)
    out["mel_fb"] = {"nfft": 512, "nBands": 40, "fmin": 100.0, "fmax": 7500.0, "fb": b64(F)}

    def tri_np(fwd, inv, nb, nfft, fs, fmin, fmax):
        pts = inv(np.linspace(fwd(fmin), fwd(fmax), nb + 2))
        f = np.arange(nfft // 2 + 1) * fs / nfft
        fbm = np.zeros((nb, len(f)))
        for b in range(nb):
            l, c, rr = pts[b], pts[b + 1], pts[b + 2]
            fbm[b] = np.where((f > l) & (f < c), (f - l) / (c - l), np.where((f >= c) & (f < rr), (rr - f) / (rr - c), 0.0))
        return fbm
    erb = tri_np(lambda f: 21.4 * np.log10(1 + 0.00437 * f), lambda e: (10 ** (e / 21.4) - 1) / 0.00437, 32, 512, FS, 100.0, 7500.0)
    bark = tri_np(lambda f: 26.81 * f / (1960 + f) - 0.53, lambda z: 1960 * (z + 0.53) / (26.28 - z), 24, 512, FS, 100.0, 7500.0)
    out["erb_fb"] = {"nBands": 32, "fb": b64(erb)}
    out["bark_fb"] = {"nBands": 24, "fb": b64(bark)}

    # G PCEN: analysis/pcen.py (repo) == librosa.pcen on the same input
    Xp, _ = stft_np(x1, 256, 64, 1, "hann")
    E = (np.abs(Xp) ** 2) * P.INT16_POWER
    s = P.smoothing_from_time_constant(0.4, FS / 64.0)
    pr = P.pcen(E, s=s)
    pf = P.pcen(E, s=s, init="first")
    lr = librosa.pcen(E.T, sr=FS, hop_length=64, time_constant=0.4, gain=0.98, bias=2, power=0.5, eps=1e-6, max_size=1).T
    assert np.allclose(pr, lr, rtol=1e-9, atol=1e-12), "analysis/pcen.py disagrees with librosa.pcen"
    out["pcen"] = {"s": s, "frames": E.shape[0], "bins": E.shape[1], "E": b64(E), "out_librosa_init": b64(pr), "out_first_init": b64(pf)}

    # H background normalisation (frugal median tracker, 0.1 dB per frame)
    db = 20 * np.log10(np.maximum(np.abs(X), 1e-12))
    B = db[0].copy()
    bg = np.zeros_like(db)
    for f in range(db.shape[0]):
        B = B + np.where(db[f] > B, 0.1, -0.1)
        bg[f] = db[f] - B
    out["bgnorm"] = {"db": b64(db), "out": b64(bg), "frames": db.shape[0], "bins": db.shape[1]}

    # I harmonic/percussive masks vs librosa.decompose.hpss (kernel 17/17, power 2, margin 1)
    S = np.abs(stft_np(x1, 128, 32, 1, "hann")[0])
    mh, mp = librosa.decompose.hpss(S.T, kernel_size=(17, 17), power=2.0, mask=True, margin=1.0)
    out["hpss"] = {"frames": S.shape[0], "bins": S.shape[1], "S": b64(S), "mask_h": b64(mh.T), "mask_p": b64(mp.T)}

    # J flux, K cepstrum, L frequency autocorrelation
    d = np.diff(db, axis=0)
    flux = np.concatenate([[0.0], np.maximum(d, 0).sum(axis=1)])
    out["flux"] = {"db": b64(db), "out": b64(flux), "frames": db.shape[0], "bins": db.shape[1]}
    S3 = np.abs(stft_np(x3, 512, 256, 1, "hann")[0])
    cep = np.array([np.fft.irfft(np.log(S3[f] + 1e-12), 512)[:100] for f in range(S3.shape[0])])
    out["cepstrum"] = {"frames": S3.shape[0], "bins": S3.shape[1], "S": b64(S3), "nq": 100, "out": b64(cep)}
    ac = np.zeros((S3.shape[0], 60))
    for f in range(S3.shape[0]):
        e = (S3[f] ** 2).sum() + 1e-30
        for l in range(60):
            ac[f, l] = (S3[f, :len(S3[f]) - l] * S3[f, l:]).sum() / e
    out["autocorr"] = {"nLag": 60, "out": b64(ac)}

    # M coherence of two beams over a 9-frame boxcar
    A_, _ = stft_np(xa, 256, 128, 1, "hann")
    B_, _ = stft_np(xb, 256, 128, 1, "hann")
    nF, nB = A_.shape
    coh = np.zeros((nF, nB))
    ph = np.zeros((nF, nB))
    for f in range(nF):
        f0, f1 = max(0, f - 4), min(nF - 1, f + 4)
        a, b = A_[f0:f1 + 1], B_[f0:f1 + 1]
        pxy = (a * np.conj(b)).sum(axis=0)
        coh[f] = np.abs(pxy) ** 2 / ((np.abs(a) ** 2).sum(axis=0) * (np.abs(b) ** 2).sum(axis=0) + 1e-300)
        ph[f] = np.angle(pxy)
    out["coherence"] = {"n": 256, "hop": 128, "M": 9, "frames": nF, "bins": nB, "coh": b64(coh), "phase": b64(ph), "delay_samples": sh}

    with open(os.path.join(HERE, "refs.json"), "w", encoding="ascii", newline="\n") as f:
        json.dump(out, f, separators=(",", ":"))
    print("refs.json", os.path.getsize(os.path.join(HERE, "refs.json")), "bytes")


if __name__ == "__main__":
    main()
