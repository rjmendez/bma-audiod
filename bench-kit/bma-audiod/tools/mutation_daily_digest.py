#!/usr/bin/env python3
"""Mutation check for tools/daily_digest.py: each mutation edits one behaviour in a COPY and test_daily_digest.py must then exit 1.
The unmutated copy must exit 0 first.   python mutation_daily_digest.py [name-substring]"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    ("human labels are counted", 'if o.get("k") != "bird" or hidden(o):', 'if o.get("k") != "bird":'),
    ("the feature warnings are dropped", "    lines += feature_lines(root, registry)\n", "    lines += []\n"),
    (
        "the feature warnings come after the health line",
        "    lines += feature_lines(root, registry)\n    lines.append(health_line(root, now))\n",
        "    lines.append(health_line(root, now))\n    lines += feature_lines(root, registry)\n",
    ),
    ("the digest ignores the registry it was given", "FSTATE.load_approved(registry)", "FSTATE.load_approved(None)"),
    ("the day end is inclusive", "lo <= t < hi", "lo <= t <= hi"),
    ("the day start is exclusive", "lo <= t < hi", "lo < t < hi"),
    ("the detections day end is inclusive", 'lo <= o["t"] < hi', 'lo <= o["t"] <= hi'),
    ("the detections day start is exclusive", 'lo <= o["t"] < hi', 'lo < o["t"] < hi'),
    ("suspect is not an artifact level", 'ARTIFACT_LEVELS = ("likely", "suspect")', 'ARTIFACT_LEVELS = ("likely",)'),
    ("every artifact_flags record counts as flagged", 's["flagged"] += 1 if af.get("level") in ARTIFACT_LEVELS else 0', 's["flagged"] += 1'),
    ("the worst hour is the best hour", "worst = max(sorted(hours)", "worst = min(sorted(hours)"),
    ("the loss mean is the maximum", "sum(allv) / len(allv), worst", "max(allv), worst"),
    ("every eviction counts as a purge", 'if o.get("reason") == "human_voice":', "if True:"),
    ("audio_stored false counts as audio", 'o.get("audio_stored") is True', 'o.get("audio_stored") is not None'),
    ("perch line without perch.jsonl", 'if not (os.path.exists(p) or os.path.exists(p + ".1")):', "if False:"),
    ("the rotated generation is not read", 'for p in (path + ".1", path):', "for p in (path,):"),
    ("no bridge_status fallback", 'v = json.load(f).get("loss_pct")', "v = None"),
    ("plugin errors are not summed", "errs = sum(", "errs = max("),
    ("non-ASCII becomes ?", '.encode("ascii", "ignore")', '.encode("ascii", "replace")'),
    ("--top is off by one", "[:top]:", "[:top + 1]:"),
    ("species ties and order ignore the count", '(-kv[1]["n"], kv[0])', "(kv[0],)"),
    ("uptime from started is wrong", 'up = max(0.0, now - st["started"])', 'up = st["started"]'),
]


def run(d):
    p = subprocess.run([sys.executable, "-I", os.path.join(d, "test_daily_digest.py")], capture_output=True, text=True, timeout=900)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_dg_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        os.makedirs(base)
        for f in ("daily_digest.py", "test_daily_digest.py", "perch_report.py", "feature_state.py"):
            shutil.copy(os.path.join(HERE, f), base)
        shutil.copytree(os.path.join(HERE, "..", "analysis"), os.path.join(tmp, "analysis"), ignore=shutil.ignore_patterns("__pycache__", "*.sh"))
        rc, _ = run(base)
        print("baseline exit %d (must be 0)" % rc)
        if rc != 0:
            return 1
        for name, a, b in M:
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, "daily_digest.py")
            s = open(p, newline="", encoding="utf-8").read()
            if s.count(a) != 1:
                print("BROKEN mutation (pattern found %d times): %s" % (s.count(a), name))
                bad += 1
                continue
            open(p, "w", newline="", encoding="utf-8").write(s.replace(a, b))
            rc, fl = run(d)
            print(("RED  " if rc == 1 else "NOT RED (exit %d) " % rc) + name + ("  <- " + fl[0][:110] if fl else ""))
            bad += rc != 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
