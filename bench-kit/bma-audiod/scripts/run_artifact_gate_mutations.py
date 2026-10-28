#!/usr/bin/env python3
"""Re-runnable mutation proof for the artifact gate tests.

    python -I scripts/run_artifact_gate_mutations.py

For each mutation: copy analysis/, tools/ and ui/taxa.json of this tree into a scratch directory (tempfile; TMPDIR / TEMP redirect it),
apply ONE textual edit (the old text must occur exactly once), run the named test script there, and require exit code 1 and a FAIL line
for every expected check name. The unmutated copy is run first and must be green (the baseline), so a red result is caused by the edit.
Exit 0 only if the baseline is green and every mutation turned its named checks red."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
GATE, REPLAY = "analysis/artifact_gate.py", "tools/artifact_replay.py"
T_GATE, T_REPLAY = "analysis/test_artifact_gate.py", "tools/test_artifact_replay.py"

# (id, file, old text, new text, test script, substrings that must appear in FAIL lines)
MUTATIONS = [
    ("M1 floor < becomes <=", GATE, "elif temp < floor:", "elif temp <= floor:", T_GATE, ["floor: exactly 5.0 C is NOT flagged"]),
    (
        "M2 burst >= min becomes >",
        GATE,
        "if n >= cfg.burst_min_transients and",
        "if n > cfg.burst_min_transients and",
        T_GATE,
        ["burst: 20 transients flag burst_coincident"],
    ),
    (
        "M3 window 30 s becomes 60 s",
        GATE,
        "burst_window_s=30.0, burst_min_transients=20,",
        "burst_window_s=60.0, burst_min_transients=20,",
        T_GATE,
        ["burst: a 45 s offset is outside the pinned 30 s window", "config: defaults are the design's"],
    ),
    (
        "M4 remove the scope check",
        GATE,
        "if group in cfg.scope_groups:",
        "if group is not None:",
        T_GATE,
        ["scope: Red-bellied Woodpecker at -10 C", "scope: out-of-scope groups"],
    ),
    (
        "M5 remove the hidden-label guard (gate)",
        GATE,
        "    if is_hidden_label(label, scientific):\n        return None",
        "    if False:\n        return None",
        T_GATE,
        ["gives None for the insect group", "gives None for the mechanical group"],
    ),
    (
        "M5b remove the hidden-label guard (replay)",
        REPLAY,
        "if G.is_hidden_label(common, sci):",
        "if False:",
        T_REPLAY,
        ["hidden rows: excluded_hidden_reports == 5", "neither 'human' nor 'speech'"],
    ),
    (
        "M6 age limit 10800 becomes 14400",
        GATE,
        "temp_max_age_s=3 * HOUR_S, burst_window_s",
        "temp_max_age_s=4 * HOUR_S, burst_window_s",
        T_GATE,
        ["a record 4 h old is temp_unknown", "config: defaults are the design's"],
    ),
    (
        "M7 allow a later hour in temp_at",
        GATE,
        "i = bisect.bisect_right(self._hours, h) - 1",
        "i = min(bisect.bisect_left(self._hours, h), len(self._hours) - 1)",
        T_GATE,
        ["temp_at: a later hour is never used"],
    ),
    (
        "M8 last-wins becomes first-wins",
        GATE,
        'by_hour[r["t_hour_utc"]] = float(r["temp_c"])',
        'by_hour.setdefault(r["t_hour_utc"], float(r["temp_c"]))',
        T_GATE,
        ["duplicate hour, the last record wins"],
    ),
    (
        "M9 median becomes mean of spread",
        GATE,
        "statistics.median(self._s[lo:hi])",
        "statistics.mean(self._s[lo:hi])",
        T_GATE,
        ["the median over an even count"],
    ),
    (
        "M10 the replay writes into analysis/",
        REPLAY,
        "if inside(a.out, os.path.join(a.data_dir, sub)):",
        "if False:",
        T_REPLAY,
        ["--out analysis refused", "--out history refused"],
    ),
    (
        "M11 default taxa path wrong",
        REPLAY,
        'DEFAULT_TAXA = os.path.join(HERE, "..", "ui", "taxa.json")',
        'DEFAULT_TAXA = os.path.join(HERE, "..", "analysis", "taxa.json")',
        T_REPLAY,
        ["default taxa: taxa_missing is false", "valid run exits 0"],
    ),
    (
        "M12 degraded run exits 0",
        REPLAY,
        "        return 1\n    return 0",
        "        return 0\n    return 0",
        T_REPLAY,
        ["nonexistent --taxa: exit 1", "missing ambient: exit 1"],
    ),
    (
        "M13 no transient coverage check",
        REPLAY,
        'reasons.append("transients_do_not_cover_reports")',
        "pass",
        T_REPLAY,
        ["transient history that ends before the reports"],
    ),
    (
        "M14 hash only the first line of each input",
        REPLAY,
        "                self.h.update(line)\n                if len(line) > MAX_LINE",
        "                if len(line) > MAX_LINE",
        T_REPLAY,
        ["coverage.inputs: birds.jsonl sha256"],
    ),
]


def copy_tree(dst):
    for sub in ("analysis", "tools"):
        shutil.copytree(os.path.join(BMA, sub), os.path.join(dst, sub), ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(dst, "ui"))
    shutil.copy(os.path.join(BMA, "ui", "taxa.json"), os.path.join(dst, "ui", "taxa.json"))


def run_test(root, script):
    p = subprocess.run([sys.executable, "-I", os.path.join(root, script)], capture_output=True, text=True, encoding="utf-8")
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL ")], p.stdout[-300:]


def main():
    bad = 0
    work = tempfile.mkdtemp(prefix="agmut_")
    try:
        for script in (T_GATE, T_REPLAY):
            root = os.path.join(work, "base_" + os.path.basename(script))
            copy_tree(root)
            rc, fl, tail = run_test(root, script)
            ok = rc == 0 and not fl
            print(("OK   " if ok else "BAD  ") + "baseline " + script + " exit %d" % rc)
            bad += 0 if ok else 1
        for i, (name, rel, old, new, script, expect) in enumerate(MUTATIONS):
            root = os.path.join(work, "m%d" % i)
            copy_tree(root)
            p = os.path.join(root, *rel.split("/"))
            with open(p, "rb") as f:
                src = f.read().decode("utf-8")
            if src.count(old) != 1:
                print("BAD  %s: the old text occurs %d times (need exactly 1)" % (name, src.count(old)))
                bad += 1
                continue
            with open(p, "wb") as f:
                f.write(src.replace(old, new).encode("utf-8"))
            rc, fl, tail = run_test(root, script)
            missing = [e for e in expect if not any(e in l for l in fl)]
            ok = rc == 1 and not missing
            print(("OK   " if ok else "BAD  ") + "%s: %s exit %d, %d checks red%s" % (name, script, rc, len(fl), "" if ok else ", not red: %s" % missing))
            for l in fl[:4]:
                print("       " + l[:150])
            bad += 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d mutations, %d problem(s)" % (len(MUTATIONS), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
