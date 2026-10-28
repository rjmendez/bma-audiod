#!/usr/bin/env python3
"""Mutation run for pcen.py: each mutant is one source edit applied to a COPY of pcen.py in a temp dir; test_pcen.py (--module-dir) must
exit 1 and name the check(s) that went red. Exit 0 only if every mutant is killed and the unmutated copy passes (exit 0 or 77).
    python -I mutation_pcen.py      (temp files under tempfile; set TEMP/TMPDIR to redirect)"""

import os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "pcen.py"), newline="").read()
MUTANTS = [
    ("smoother weights swapped", "M = (1.0 - self.s) * M0 + self.s * e", "M = self.s * M0 + (1.0 - self.s) * e"),
    ("alpha ignored (exponent fixed at 1)", "np.exp(-self.alpha * (math.log", "np.exp(-1.0 * (math.log"),
    ("librosa init uses first frame", 'np.ones(self.shape) if self.init == "librosa" else e', "e"),
    ("delta**r offset not subtracted", "out = (e * smooth + self.delta) ** self.r - self._dr", "out = (e * smooth + self.delta) ** self.r"),
    ("time constant: exponential formula", "return (math.sqrt(1.0 + 4.0 * T * T) - 1.0) / (2.0 * T * T)", "return 1.0 - math.exp(-1.0 / T)"),
    ("NaN/inf accepted", "if not np.all(np.isfinite(a)):", "if False:"),
    ("negative input accepted", "if (a < 0).any():", "if False:"),
    ("scale ignored", "e = e * self.scale", "e = e"),
    ("epsilon dropped from the AGC", "math.log(self.eps) + np.log1p(M / self.eps)", "np.log(M + 1e-300)"),
    (
        "state not carried between feed_one calls",
        "        M = (1.0 - self.s) * M0 + self.s * e\n",
        "        M = (1.0 - self.s) * (np.ones(self.shape) if self.init == 'librosa' else e) + self.s * e\n",
    ),
    ("render: 50 ms column rule removed", "if not frame_s >= MIN_COL_S - 1e-12:", "if False:"),
    ("render: 50 ms rule lets NaN through (the original form)", "if not frame_s >= MIN_COL_S - 1e-12:", "if frame_s < MIN_COL_S - 1e-12:"),
    ("scale-overflow check removed", "if not np.all(np.isfinite(e)):", "if False:"),
    ("output-finite check removed", "if not np.all(np.isfinite(out)):", "if False:"),
    (
        "state advances before the output check",
        '        with np.errstate(over="ignore", invalid="ignore"):\n',
        '        self.M = M\n        with np.errstate(over="ignore", invalid="ignore"):\n',
    ),
    ("delta**r OverflowError escapes", "        except OverflowError:\n", "        except ZeroDivisionError:\n"),
    ("render: 96 row rule removed", "if P.shape[1] > MAX_ROWS:", "if False:"),
    ("render: row order not flipped", ".astype(np.uint8).T[::-1].copy()", ".astype(np.uint8).T.copy()"),
    ("render: no clip before uint8", "np.clip(out / vmax * 255.0, 0, 255).round()", "(out / vmax * 255.0).round()"),
    ("render: default init not first", 'pcen_kw.setdefault("init", "first")', 'pcen_kw.setdefault("init", "librosa")'),
]


def run(src, tmp):
    d = tempfile.mkdtemp(dir=tmp)
    open(os.path.join(d, "pcen.py"), "w", newline="").write(src)
    r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "test_pcen.py"), "--module-dir", d], capture_output=True, text=True)
    shutil.rmtree(d, ignore_errors=True)
    return r.returncode, [l[5:90] for l in r.stdout.splitlines() if l.startswith("FAIL ")] + (
        [r.stderr.strip().splitlines()[-1][:90]] if r.returncode not in (0, 1, 77) and r.stderr.strip() else []
    )


with tempfile.TemporaryDirectory() as tmp:
    rc, _ = run(SRC, tmp)
    print(f"unmutated: exit {rc} ({'ok' if rc in (0, 77) else 'BROKEN BASELINE'})")
    bad = rc not in (0, 77)
    for name, old, new in MUTANTS:
        if SRC.count(old) != 1:
            print(f"MUTANT NOT APPLICABLE ({SRC.count(old)} matches): {name}")
            bad = True
            continue
        rc, red = run(SRC.replace(old, new), tmp)
        ok = rc != 0 and rc != 77 and bool(red)
        print(f"{'KILLED  ' if ok else 'SURVIVED'} {name}: exit {rc}; red: {len(red)} " + (red[0] if red else ""))
        bad |= not ok
sys.exit(1 if bad else 0)
