#!/usr/bin/env python3
"""Cross-check analysis/indices.py against scikit-maad (BSD-3, NOT a runtime dependency: it is imported only here).
    PYTHONPATH=<dir holding maad> python -I compare_maad.py [--json]
Install maad into a scratch overlay, never into the live venv:   pip install --target <dir> --no-deps scikit-maad
Every signal is synthetic (fixed seeds). For each index the table gives ours, maad's and the ratio / difference, with the maad
function and convention used. Definitional differences found by reading the maad 1.5.2 source (not bugs on either side):
  aci   identical formula on the same amplitude spectrogram; maad returns nan for a silent bin (0/0), we add 0.
  hf    maad frequency_entropy(P, 'seewave') = entropy(sqrt(mean power)), 'QUT' = entropy(mean power); we use entropy(mean amplitude)
        (Sueur 2008 / seewave meanspec). All three are reported.
  ht    maad default is the frame-max envelope; only mode='hilbert', compatibility='seewave' is the Sueur definition. maad's Hilbert
        envelope is full rate over the whole signal (we use the exact complex baseband of the band, a fifth of the cost).
  bi    maad 'soundecology' divides the dB sum by df in Hz (Boelman's area multiplies by df): ours = maad * df^2 / 1000 (df in Hz, result in
        dB*kHz). maad's band is inclusive of both edges (index_bw), ours is [lo, hi).
  ndsi  maad bins the POWER spectrogram into 1 kHz bins (mean per bin) and sums whole bins; its band test snaps each edge to the nearest bin and is
        inclusive (limits (1000, 2000) select TWO 1 kHz bins), and maad's defaults are bio 1-10 kHz / anthro 0-1 kHz."""

import json, os, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import indices as I  # noqa: E402

FS = I.FS


def signals(n=240000):
    """name -> 1-D float64 window (5 s)."""
    t = np.arange(n) / FS
    rng = np.random.default_rng(7)
    out = {}
    out["white"] = rng.standard_normal(n) * 1e-3
    out["tone4k+noise"] = 0.1 * np.sin(2 * np.pi * 4000 * t) + rng.standard_normal(n) * 1e-4
    gate = (np.floor(t / 0.1) % 2 == 0).astype(float)  # 100 ms on / 100 ms off
    out["4k on/off 100ms + noise"] = 0.1 * gate * np.sin(2 * np.pi * 4000 * t) + rng.standard_normal(n) * 1e-4
    # real-shaped: 1/f noise, a 5.3 kHz cricket-like 25 Hz pulse train, a 3 s long 6.5 kHz hum swell, two chirps, a 1.5 kHz low rumble
    f = np.fft.rfftfreq(n, 1 / FS)
    pink = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) / np.sqrt(np.maximum(f, 50.0)), n)
    pink *= 2e-3 / pink.std()
    pulses = (np.floor(t * 25) % 2 == 0) * np.sin(2 * np.pi * 5300 * t) * 3e-3
    swell = np.sin(2 * np.pi * 6500 * t) * 2e-3 * np.clip(np.sin(np.pi * t / 5.0), 0, 1) ** 2
    chirp = np.zeros(n)
    for t0 in (1.0, 3.2):
        m = (t >= t0) & (t < t0 + 0.3)
        chirp[m] = 5e-3 * np.sin(2 * np.pi * (3000 * (t[m] - t0) + 6000 * (t[m] - t0) ** 2 / 0.6)) * np.hanning(m.sum())
    out["real-shaped"] = pink + pulses + swell + chirp + 3e-3 * np.sin(2 * np.pi * 1500 * t) * (0.5 + 0.5 * np.sin(2 * np.pi * 0.3 * t))
    return out


def ours_spectrograms(x):
    nfr = len(x) // I.NFFT
    from scipy import fft as sfft

    X = sfft.rfft(x[: nfr * I.NFFT].reshape(nfr, I.NFFT) * I._HANN, axis=1)
    return np.abs(X).T  # (bins, frames): maad's orientation


def compare(x, maad):
    """-> dict index -> {"ours": v, "maad": v, "note": ...} for one 1-D window x. maad: the imported maad module."""
    from scipy import signal as ssig

    r = I.analyze(x)
    A = ours_spectrograms(x)
    fn = np.arange(A.shape[0]) * I.DF
    a0, a1 = I._bins(I.ACI_BAND_HZ)
    res = {}
    # ---- ACI on the very same amplitude spectrogram, and on maad's own spectrogram
    _, _, aci_m = maad.features.acoustic_complexity_index(A[a0:a1])
    res["aci"] = {"ours": r["aci"], "maad": float(aci_m), "note": "same amplitude spectrogram, band bins 10..109"}
    Sm, _, fm, _ = maad.sound.spectrogram(x, FS, window="hann", nperseg=I.NFFT, noverlap=0, mode="amplitude")
    Sm = np.asarray(Sm)
    res["aci_maad_spectrogram"] = {
        "ours": r["aci"],
        "maad": float(maad.features.acoustic_complexity_index(Sm[a0:a1])[2]),
        "note": f"maad's own spectrogram (shape {Sm.shape}, ours {A.shape})",
    }
    # ---- Hf
    e0, e1 = I._bins(I.ENT_BAND_HZ)
    P = A[e0:e1] ** 2
    res["hf_seewave"] = {
        "ours": r["hf"],
        "maad": float(maad.features.frequency_entropy(P, compatibility="seewave")[0]),
        "note": "entropy(sqrt(mean power)) vs entropy(mean amplitude)",
    }
    res["hf_QUT"] = {"ours": r["hf"], "maad": float(maad.features.frequency_entropy(P, compatibility="QUT")[0]), "note": "entropy(mean power)"}
    # ---- Ht: signal limited to the band first so both see the same band
    sos = None
    X = np.fft.rfft(x)
    lo, hi = int(np.ceil(I.ENT_BAND_HZ[0] * len(x) / FS)), int(np.ceil(I.ENT_BAND_HZ[1] * len(x) / FS))
    Xb = np.zeros_like(X)
    Xb[lo:hi] = X[lo:hi]
    xb = np.fft.irfft(Xb, len(x))
    res["ht_hilbert"] = {
        "ours": I.envelope_ht(xb),
        "maad": float(maad.features.temporal_entropy(xb, compatibility="seewave", mode="hilbert")),
        "note": "band-limited signal; maad full-rate Hilbert envelope, seewave convention",
    }
    res["ht_default"] = {"ours": I.envelope_ht(xb), "maad": float(maad.features.temporal_entropy(xb)), "note": "maad default (QUT, fast frame-max envelope)"}
    # ---- BI
    i0, i1 = I._bins(I.BI_BAND_HZ)
    bi_m = float(
        maad.features.bioacoustics_index(A, fn, flim=(I.BI_BAND_HZ[0], I.BI_BAND_HZ[1] - I.DF))
    )  # maad's index_bw snaps both edges to the NEAREST bin, inclusive: 2000..7900 = our 60 bins
    # maad normalises by a per-row maximum before the dB step; the offset cancels after subtracting the minimum
    amp_scale = 2.0 / I._SUMW
    res["bi"] = {"ours": r["bi"], "maad": bi_m * I.DF**2 / 1000.0, "note": "maad soundecology BI * df^2/1000, flim (2000, 7900) = the same 60 bins"}
    res["bi_maad_raw"] = {"ours": r["bi"], "maad": bi_m, "note": "maad soundecology BI as returned (dB sum / df in Hz)"}
    # ---- NDSI on the power spectrogram
    Pw = 2.0 * A**2 / (I.NFFT * I._SUMW2)
    # R_compatible=None: 1 kHz bins and the 0 kHz bin dropped; index_bw is inclusive of the bin each edge snaps to, so bio (2000, 7000) = the
    # bins [2,3) ... [7,8) kHz and anthro (1000, 1000) = the bin [1,2) kHz. (soundecology mode takes its bin width from the anthro limits.)
    nd = maad.features.soundscape_index(Pw, fn, flim_bioPh=(2000, 7000), flim_antroPh=(1000, 1000), R_compatible=None)
    res["ndsi"] = {"ours": r["ndsi"], "maad": float(nd[0]), "note": "maad R_compatible=None, 1 kHz bins: bio bins 2..7 kHz, anthro bin 1 kHz"}
    return res


def main():
    try:
        import maad, maad.features, maad.sound
    except Exception as e:
        print(f"SKIP: scikit-maad not importable ({type(e).__name__}: {e}); set PYTHONPATH to the overlay holding it")
        return 3
    rows = {}
    for name, x in signals().items():
        rows[name] = compare(x, maad)
    if "--json" in sys.argv:
        print(json.dumps({"maad_version": getattr(maad, "__version__", "1.5.2"), "rows": rows}, indent=1))
        return 0
    print(f"scikit-maad {getattr(maad, '__version__', '1.5.2')}")
    for name, d in rows.items():
        print(f"\n== {name}")
        for k, v in d.items():
            o, m = v["ours"], v["maad"]
            print(f"  {k:22s} ours {o:12.5f}  maad {m:12.5f}  diff {o - m:+.5f}  ratio {o / m if m else float('nan'):.5f}   {v['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
