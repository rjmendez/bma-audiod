#!/usr/bin/env python3
"""Mutation run for the dashboard's artifact flags: copies ui/ and analysis/artifact_gate.py into a scratch tree, breaks ONE thing at a time and requires the
matching test to go red (a surviving mutant is a test that proves nothing). The baseline copy must be green first.

    python -I mutation_artifact.py            (exit 0 when every mutant is killed; add --dom to also run the Edge DOM mutants, one at a time)
Prints one line per mutant: KILLED <name> (the first failing check) or SURVIVED <name>."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYSIS = os.path.join(HERE, "..", "analysis")

PY_MUTANTS = [
    (
        "artifact=none keeps the flagged events instead of leaving them out",
        "bma_ui.py",
        'self._flagged(e, now) is (am == "only")',
        'self._flagged(e, now) is (am != "only")',
        "test_artifact_ui.py",
    ),
    (
        "the default artifact mode hides the flagged events",
        "bma_ui.py",
        'p_enum("artifact", q["artifact"], ARTIFACT_MODES) if "artifact" in q else "any"\n        rows = [e for e in self.dets.snapshot()',
        'p_enum("artifact", q["artifact"], ARTIFACT_MODES) if "artifact" in q else "none"\n        rows = [e for e in self.dets.snapshot()',
        "test_artifact_ui.py",
    ),
    (
        "a suspect (burst only) event is not counted as flagged",
        "bma_ui.py",
        'FLAGGED_LEVELS = ("suspect", "likely")',
        'FLAGGED_LEVELS = ("likely",)',
        "test_artifact_ui.py",
    ),
    (
        "burst evidence is produced for windows the dsp file does not cover",
        "bma_ui.py",
        "return self.first_t is not None and self.first_t <= t - half_s and t + half_s <= self.last_t",
        "return self.first_t is not None and t + half_s <= self.last_t",
        "test_artifact_ui.py",
    ),
    (
        "the cached verdict ignores a newer ambient file",
        "bma_ui.py",
        "if e.gate is not None and e.gate[0] == rev:",
        "if e.gate is not None:",
        "test_artifact_ui.py",
    ),
    ("the hidden-event count is not kept", "bma_ui.py", "flagged_total += flagged ", "flagged_total += 0 ", "test_artifact_ui.py"),
    ("rollup events are not reported as unscored", "bma_ui.py", 'unscored += r["events"] ', "unscored += 0 ", "test_artifact_ui.py"),
    (
        "the floor test is not strict: exactly 5.0 C is flagged",
        "../analysis/artifact_gate.py",
        "elif temp < floor:",
        "elif temp <= floor:",
        "test_artifact_ui.py",
    ),
    (
        "a missing ambient file raises instead of reading as no data",
        "bma_ui.py",
        "            except OSError:\n                continue\n            for ln in data.split",
        "            except OSError:\n                raise\n            for ln in data.split",
        "test_artifact_ui.py",
    ),
    (
        "the badge helper flags level none",
        "static/app.js",
        'const ARTIFACT_LEVELS = ["likely", "suspect"];',
        'const ARTIFACT_LEVELS = ["likely", "suspect", "none"];',
        "JS",
    ),
    (
        "the count line says hidden while the toggle is off",
        "static/app.js",
        'return (hidden ? n + " flagged insect events hidden"',
        'return (!hidden ? n + " flagged insect events hidden"',
        "JS",
    ),
    ("the hide toggle is ON by default", "static/app.js", "showNon: false, hideArtifact: false", "showNon: false, hideArtifact: true", "JS"),
    ("the tips ignore the floor the server reports", "static/app.js", "ARTIFACT_FLOOR_C = ag.floor_c;", "ARTIFACT_FLOOR_C = 5;", "JS"),
]
DOM_MUTANTS = [
    (
        "the dashboard asks for artifact=none whether or not the toggle is on",
        "static/app.js",
        'artifact: d.hideArtifact ? "none" : "" }',
        'artifact: "none" }',
        "test_dom_artifact.py",
    ),
    (
        "a flagged row gets no badge",
        "static/app.js",
        'artifactFlagged(x.artifact_flags) ? [" ", Tip.badge(',
        'false ? [" ", Tip.badge(',
        "test_dom_artifact.py",
    ),
]


def scratch():
    d = tempfile.mkdtemp(prefix="bma_mut_art_")
    shutil.copytree(HERE, os.path.join(d, "ui"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(d, "analysis"))
    shutil.copy(os.path.join(ANALYSIS, "artifact_gate.py"), os.path.join(d, "analysis"))
    return d


def run(d, which):
    ui = os.path.join(d, "ui")
    env = dict(os.environ, BMA_UI_DIR=ui, BMA_DOM_WAIT="2")
    cmd = ["node", os.path.join(ui, "test_app_js.js")] if which == "JS" else [sys.executable, "-I", os.path.join(ui, which)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=280, env=env, cwd=ui)
    first = next((l for l in p.stdout.splitlines() if l.startswith("FAIL")), "")
    return p.returncode, first[:160]


def main():
    dom = "--dom" in sys.argv
    base = scratch()
    try:
        for which in ("test_artifact_ui.py", "JS"):
            rc, first = run(base, which)
            print("baseline %s: %s" % (which, "green" if rc == 0 else "RED " + first))
            if rc != 0:
                return 2
    finally:
        shutil.rmtree(base, ignore_errors=True)
    survived = 0
    for name, rel, old, new, which in PY_MUTANTS + (DOM_MUTANTS if dom else []):
        d = scratch()
        try:
            path = os.path.normpath(os.path.join(d, "ui", rel))
            s = open(path, newline="", encoding="utf-8").read()
            if s.count(old) != 1:
                print("BROKEN-MUTANT %s: pattern found %d times" % (name, s.count(old)))
                survived += 1
                continue
            open(path, "w", newline="", encoding="utf-8").write(s.replace(old, new))
            rc, first = run(d, which)
            print(("KILLED   " if rc != 0 else "SURVIVED ") + name + (("  <- " + first) if rc != 0 else ""))
            survived += rc == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
