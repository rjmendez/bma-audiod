#!/usr/bin/env python3
"""Mutation check for static/engine/dsp.js: each mutation edits one behaviour in a COPY of the module; test_dsp.js must then exit 1.
The unmutated copy must exit 0 first.   python mutation_dsp.py [name-substring]"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "static", "engine", "dsp.js")
M = [
    ("fft twiddle sign flipped", "s[i] = -Math.sin(PI2 * i / n)", "s[i] = Math.sin(PI2 * i / n)"),
    ("stft amplitude norm halved", "norm = 2 / sum(w), nF = frameCount(x.length, n, hop);\n    const mag", "norm = 1 / sum(w), nF = frameCount(x.length, n, hop);\n    const mag"),
    ("hamming coefficient", "0.54 - 0.46 * Math.cos(p)", "0.53 - 0.46 * Math.cos(p)"),
    ("kaiser beta ignored", "const b = beta == null ? 8.6 : beta", "const b = 8.6"),
    ("multitaper calibration dropped", "_mtCal[key] = 1 / Math.sqrt(m);", "_mtCal[key] = 1;"),
    ("multitaper taper phase", "Math.PI * k * (i + 1) / (n + 1)", "Math.PI * k * i / (n + 1)"),
    ("reassigned time sign", "const tf = f + dt / hop;", "const tf = f - dt / hop;"),
    ("reassigned frequency sign", "const df = -(C[1][k] * hr - C[0][k] * hi) / p;", "const df = (C[1][k] * hr - C[0][k] * hi) / p;"),
    ("reassigned time ramp origin", "th[i] = (i - n / 2) * h[i];", "th[i] = i * h[i];"),
    ("cqt gaussian width", "(N / 6)", "(N / 5)"),
    ("cqt atom normalisation", "re[i] = 2 * w[i] / sw * Math.cos(a)", "re[i] = w[i] / sw * Math.cos(a)"),
    ("filterbank rising edge slope", "(f - l) / (c - l)", "(f - l) / (r - l)"),
    ("mel scale constant", "2595 * Math.log10(1 + f / 700)", "2595 * Math.log10(1 + f / 800)"),
    ("pcen smoother gain", "M[k] = (1 - s) * M[k] + s * e;", "M[k] = (1 - s) * M[k] + 0.5 * s * e;"),
    ("pcen initial state", "M[k] = o.init === 'first' ? E[k] : 1;", "M[k] = o.init === 'first' ? E[k] : 0;"),
    ("pcen exponent", "Math.pow(e / Math.pow(eps + M[k], alpha) + delta, r) - dr", "Math.pow(e / Math.pow(eps + M[k], alpha) + delta, r) - delta"),
    ("bgNorm falling step doubled", "B[k] += v > B[k] ? step : -step;", "B[k] += v > B[k] ? step : -2 * step;"),
    ("bgNorm starts at zero", "for (let k = 0; k < nBins; k++) B[k] = db[k];", "for (let k = 0; k < nBins; k++) B[k] = 0;"),
    ("hpss mask power", "const h2 = H[i] * H[i], p2 = P[i] * P[i]", "const h2 = H[i], p2 = P[i]"),
    ("median filter reflect boundary", "return i < len ? i : p - 1 - i; };", "return i < len ? i : p - i; };"),
    ("flux counts falls", "if (d > 0) s += d;", "s += Math.abs(d);"),
    ("cepstrum not mirrored", "for (let k = 1; k < nB - 1; k++) re[nfft - k] = re[k];", ""),
    ("frequency autocorrelation unnormalised", "out[f * nLag + l] = s / e;", "out[f * nLag + l] = s;"),
    ("StftPlan window not applied", "for (let i = 0; i < n; i++) re[i] = x[off + i] * w[i];\n      re.fill(0, n); im.fill(0); fft(re, im);\n      for (let k = 0; k < nBins; k++) {\n        outMag", "for (let i = 0; i < n; i++) re[i] = x[off + i];\n      re.fill(0, n); im.fill(0); fft(re, im);\n      for (let k = 0; k < nBins; k++) {\n        outMag"),
    ("PcenStream state not carried", "M[k] = (1 - s) * M[k] + s * E[k]; this.out[k]", "M[k] = s * E[k]; this.out[k]"),
    ("BgNormStream resets each frame", "const B = this.B || (this.B = Float64Array.from(db));", "const B = (this.B = Float64Array.from(db));"),
    ("coherence phase sign", "xi += ai * br - ar * bi;", "xi += ar * bi - ai * br;"),
    ("coherence window", "const f0 = Math.max(0, f - half), f1 = Math.min(nF - 1, f + half);", "const f0 = f, f1 = f;"),
    ("row map freqs interpolation", "m[r] = a + (f - binFreqs[a]) / (binFreqs[b] - binFreqs[a]);", "m[r] = a + 0.5;"),
    ("row map freqs clamp", "if (f >= binFreqs[n - 1]) { m[r] = n - 1; continue; }", "if (f >= binFreqs[n - 1]) { m[r] = n - 2; continue; }"),
    ("row map direction", "m[r] = f / df; }", "m[r] = (S.inv(lo + (hi - lo) * (r / (nRows - 1)))) / df; }"),
    ("resample clamp", "const p = Math.min(n - 1, Math.max(0, rowMap[r]))", "const p = Math.max(0, rowMap[r])"),
]


def run(d):
    p = subprocess.run(["node", os.path.join(d, "test", "test_dsp.js")], capture_output=True, text=True, timeout=600)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2], p.stderr[-200:]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="uinext_mut_")
    bad = 0
    try:
        os.makedirs(os.path.join(tmp, "test"))
        os.makedirs(os.path.join(tmp, "static", "engine"))
        for f in ("test_dsp.js", "refs.json"):
            shutil.copy(os.path.join(HERE, f), os.path.join(tmp, "test"))
        dst = os.path.join(tmp, "static", "engine", "dsp.js")
        shutil.copy(SRC, dst)
        rc, _, _ = run(tmp)
        print("baseline exit %d (must be 0)" % rc)
        if rc != 0:
            return 1
        base = open(SRC, encoding="utf-8").read()
        for name, a, b in M:
            if only and only not in name:
                continue
            if base.count(a) != 1:
                print("BROKEN mutation (pattern found %d times): %s" % (base.count(a), name))
                bad += 1
                continue
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(base.replace(a, b))
            rc, fl, err = run(tmp)
            if rc == 0:
                print("ALIVE  %s" % name)
                bad += 1
            else:
                print("RED    %s  <- %s" % (name, (fl[0] if fl else "crash: " + err.strip().splitlines()[-1] if err.strip() else "exit %d" % rc)[:110]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d mutations, %d not caught" % (len(M) if not only else sum(1 for m in M if only in m[0]), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
