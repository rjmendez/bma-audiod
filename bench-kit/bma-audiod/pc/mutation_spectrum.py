#!/usr/bin/env python3
"""Mutation check for bma_spectrum: each mutation edits one behaviour in a COPY of pc/ (and contracts/), runs test_spectrum.py against
it and requires a non-zero exit (the suite must go red). The unmutated copy must pass first.

    python -I mutation_spectrum.py [name-substring|name-substring ...]

Exit 0 only if the baseline is green and every mutation was caught BY A FAILING CHECK: a mutation that is red only because the
process crashed or hung (no "FAIL" line) is reported as CRASH and counts as not caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "bma_spectrum.py")

M = [
    ("window power normalisation dropped", "self.scale = two / (float(n) ** 2 * float(np.mean(self.win ** 2)))", "self.scale = two / (float(n) ** 2)"),
    ("rectangular window instead of Hann", "return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)       # periodic", "return np.ones(n)"),
    (
        "quantize floors instead of rounding half even",
        "np.rint((np.asarray(db, dtype=np.float64) - DB_FLOOR) / DB_STEP)",
        "np.floor((np.asarray(db, dtype=np.float64) - DB_FLOOR) / DB_STEP)",
    ),
    ("quantize floor -100 dB -> -90 dB", "DB_FLOOR, DB_STEP = -100.0, 0.5", "DB_FLOOR, DB_STEP = -90.0, 0.5"),
    ("mean beam is the loudest beam, not the power mean", "mean = p.mean(axis=0)", "mean = p.max(axis=0)"),
    ("a zero beam is not flagged", 'per[i + 1] = (0, bytes(q[j])) if nonzero[j] else (FLAG_ZERO, bytes(tt["n_bins"]))', "per[i + 1] = (0, bytes(q[j]))"),
    ("a hole inside a window is not detected", "if (a < e and b > s) if b > a else (s < a < e):", "if False:"),
    (
        "a duplicate block is processed again",
        'if r == "dup":\n            self.dups += 1\n            return []',
        "if False:\n            self.dups += 1\n            return []",
    ),
    ("block index is not unwrapped", "return self.last_u + (d - (1 << 32) if d >= (1 << 31) else d)", "return self.last_u + d"),
    (
        "the producer gates and flattens every normal row again (flag bit 0, one value per row)",
        "return [build_row(t, fl, beam, k, t_us, bins) for beam, (fl, bins) in enumerate(rows)]",
        "return [build_row(t, 1 if fl == 0 else fl, beam, k, t_us, bytes([sum(bins) // len(bins)] * len(bins)) if fl == 0 else bins) for beam, (fl, bins) in enumerate(rows)]",
    ),
    ("non-loopback addresses accepted", "ok = ipaddress.ip_address(host).is_loopback", "ok = True"),
    ("standard window overlaps (hop 2400)", '"standard", "window": 4800, "hop": 4800,', '"standard", "window": 4800, "hop": 2400,'),
    ("BMAA reserved byte not checked", "pkt[4] != 1 or pkt[5] != 0:", "pkt[4] != 1:"),
    ("BMAA length not checked", "len(pkt) != 20 + 4 * n:", "False:"),
    ("BMAA sample range not checked", "if x.min() < -8388608 or x.max() > 8388607:", "if False:"),
    ("packet header drops the beam", "bytes([VERSION, tier, flags & 0xFF, beam])", "bytes([VERSION, tier, flags & 0xFF, 0])"),
]


def run(workdir):
    try:
        p = subprocess.run([sys.executable, "-I", os.path.join(workdir, "pc", "test_spectrum.py")], capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return 124, [], "(the test run hung: caught as a failure)"
    lines = p.stdout.splitlines()
    fails = [ln for ln in lines if ln.startswith("FAIL")]
    return p.returncode, fails, (lines[-1] if lines else "(no output; stderr: " + (p.stderr.strip().splitlines() or ["-"])[-1][:80] + ")")


def main():
    only = sys.argv[1].split("|") if len(sys.argv) > 1 else None
    root = tempfile.mkdtemp(prefix="bma_spec_mut_")
    try:
        work = os.path.join(root, "w")
        os.makedirs(work)
        shutil.copytree(HERE, os.path.join(work, "pc"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(HERE, "..", "contracts"), os.path.join(work, "contracts"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(HERE, "..", "ui"), os.path.join(work, "ui"), ignore=shutil.ignore_patterns("__pycache__"))  # the feed check imports bma_ui
        code, fails, last = run(work)
        print(f"baseline: exit {code}, {last}")
        if code != 0:
            print("baseline is not green; fix that first")
            return 2
        orig = open(SRC, encoding="utf-8", newline="").read()
        missed, ok = [], 0
        n_run = 0
        for name, old, new in M:
            if only and not any(o in name for o in only):
                continue
            if orig.count(old) != 1:
                print(f"BROKEN MUTATION (pattern occurs {orig.count(old)}x): {name}")
                missed.append(name)
                continue
            with open(os.path.join(work, "pc", "bma_spectrum.py"), "w", encoding="utf-8", newline="\n") as f:
                f.write(orig.replace(old, new))
            code, fails, last = run(work)
            n_run += 1
            if code != 0 and not fails:
                print(f"CRASH {name}: exit {code}, {last}  <-- red only by a crash or hang, no check failed")
                missed.append(name)
            elif code != 0:
                ok += 1
                print(f"RED   {name}: exit {code}, {last}; first failing check: {(fails[0] if fails else last)[:110]}")
            else:
                print(f"GREEN {name}: exit {code}, {last}  <-- NOT CAUGHT")
                missed.append(name)
        print(f"\n{n_run} mutation(s) run, {ok} caught by a failing check, {len(missed)} not caught")
        return 1 if missed else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
