#!/usr/bin/env python3
"""Mutation proof for test_labels.py / test_review_queue.py / test_events.py: each mutation edits ONE line of a COPY of the sources (never the real tree),
runs the named test, and must see it FAIL (non-zero exit with at least one FAIL line); the unmutated copy must pass first.

    python -I run_label_mutations.py --work <scratch dir>

Prints one line per mutation: its name, the test, the exit code and the first FAIL check, and exits 1 if any mutation stayed green."""

import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
# (name, file relative to bma-audiod, old text, new text, test relative to bma-audiod)
MUTATIONS = [
    ("M1 delete the speech_tag check", "analysis/labels.py", '            return "speech_tag"', "            pass", "analysis/test_labels.py"),
    ("M2 delete the speech_unknown fail-closed branch", "analysis/labels.py", '        return "speech_unknown"', "        pass", "analysis/test_labels.py"),
    (
        "M3 add() drops the speech guard",
        "tools/bma_labels.py",
        "if ev is None or L.speech_reason(ev, tags, a.speech_margin_s, a.speech_p_min, a.include_untagged) is not None:",
        "if ev is None:",
        "analysis/test_labels.py",
    ),
    (
        "M3b the Human prefix no longer matches 'Human vocal'",
        "analysis/labels.py",
        'HUMAN_LABEL_PREFIXES = ("human",)',
        'HUMAN_LABEL_PREFIXES = ("humanx",)',
        "analysis/test_labels.py",
    ),
    (
        "M3c refusal message differs for a speech event (an oracle)",
        "tools/bma_labels.py",
        "    if ev is None or L.speech_reason(",
        '    if ev is not None and L.speech_reason(ev, tags, a.speech_margin_s, a.speech_p_min, a.include_untagged) is not None:\n        eprint("speech_excluded")\n        return 2\n    if ev is None or L.speech_reason(',
        "analysis/test_labels.py",
    ),
    ("M4 library gap 3.0 -> 5.0 s", "analysis/labels.py", "EVENT_GAP_S = 3.0 ", "EVENT_GAP_S = 5.0 ", "analysis/test_labels.py"),
    (
        "M4b cluster only in-region rows (drop bird_filtered)",
        "analysis/labels.py",
        "kinds=E.KINDS, drop_human=False)",
        'kinds=("bird",), drop_human=False)',
        "analysis/test_labels.py",
    ),
    (
        "M4c bird_filtered rows keep in_region",
        "analysis/labels.py",
        'reps = [dict(o, in_region=False) if o["k"] == "bird_filtered" else o for o in c.rows]',
        "reps = list(c.rows)",
        "analysis/test_events.py",
    ),
    (
        "M5 near-hi < becomes <=",
        "analysis/review_queue.py",
        '    if conf < near_lo:\n        return "low"\n    if conf < near_hi:',
        '    if conf < near_lo:\n        return "low"\n    if conf <= near_hi:',
        "analysis/test_review_queue.py",
    ),
    (
        "M6 drop the per-species cap",
        "analysis/review_queue.py",
        "if pos[s] < len(lists[s]) and pos[s] < cap:",
        "if pos[s] < len(lists[s]):",
        "analysis/test_review_queue.py",
    ),
    (
        "M7 unseeded audit RNG",
        "analysis/review_queue.py",
        'random.Random("%s:audit:%s" % (p["seed"], s)).sample',
        "random.Random().sample",
        "analysis/test_review_queue.py",
    ),
    ("M8 fold picks the earliest", "analysis/labels.py", "if cur is None or key >= cur[0]:", "if cur is None or key <= cur[0]:", "analysis/test_labels.py"),
    (
        "M9 audit p_include with the wrong denominator",
        "analysis/review_queue.py",
        "quota[s] / r) for i in sorted(drawn)",
        "quota[s] / frame_all) for i in sorted(drawn)",
        "analysis/test_review_queue.py",
    ),
    (
        "M10 provenance model_opinion allowed by the validator",
        "analysis/labels.py",
        '    if rec["provenance"] != PROVENANCE:',
        "    if False:",
        "analysis/test_labels.py",
    ),
    (
        "M11 writer without O_BINARY (Windows: LF becomes CRLF)",
        "analysis/labels.py",
        'fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)',
        "fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)",
        "analysis/test_labels.py",
    ),
    (
        "M12 audit frame includes the uncertainty picks",
        "analysis/review_queue.py",
        "        if id(e) not in chosen:",
        "        if True:",
        "analysis/test_review_queue.py",
    ),
    (
        "M13 no per-species cap on the audit slice",
        "analysis/review_queue.py",
        'quota[s] < min(len(remaining[s]), p["audit_per_species_cap"])',
        "quota[s] < len(remaining[s])",
        "analysis/test_review_queue.py",
    ),
    (
        "M14 filtered_overlap flag never set",
        "analysis/review_queue.py",
        '                fl.append("filtered_overlap")',
        "                pass",
        "analysis/test_review_queue.py",
    ),
    (
        "M15 ambient: a record exactly 3 h old is rejected",
        "analysis/labels.py",
        "hr - self.h[i] > self.MAX_AGE_S",
        "hr - self.h[i] >= self.MAX_AGE_S",
        "analysis/test_labels.py",
    ),
    (
        "M16 queue --out under analysis/ allowed",
        "tools/bma_labels.py",
        "        sub = protected_hit(a.out, pa)\n        if sub:",
        "        sub = protected_hit(a.out, pa)\n        if False:",
        "analysis/test_labels.py",
    ),
    (
        "M17 stderr text mode (CRLF on Windows)",
        "tools/bma_labels.py",
        '    sys.stderr.buffer.write((" ".join(str(x) for x in a) + "\\n").encode("utf-8", "replace"))      # bytes: LF stays LF on Windows\n    sys.stderr.buffer.flush()',
        '    sys.stderr.write(" ".join(str(x) for x in a) + "\\n")',
        "analysis/test_labels.py",
    ),
    (
        "M18 probe ignores the temperature class",
        "analysis/review_queue.py",
        'cls = "unknown" if temp is None else ("cold" if temp < COLD_C else "warm")',
        'cls = "unknown"',
        "analysis/test_review_queue.py",
    ),
    (
        "M19 insect test uses nothing (insects never excluded)",
        "analysis/review_queue.py",
        'if p["insect_mode"] == "exclude" and insect:',
        "if False:",
        "analysis/test_review_queue.py",
    ),
    (
        "M20 stale lock never taken over",
        "analysis/labels.py",
        "if time.time() - os.path.getmtime(lock) > self.LOCK_STALE_S:",
        "if False:",
        "analysis/test_labels.py",
    ),
    (
        "M23 queue --out may overwrite the label store",
        "tools/bma_labels.py",
        '        if under(a.out, pa["labels"]):',
        "        if False:",
        "analysis/test_labels.py",
    ),
    (
        "M24 path guard is case/symlink sensitive (no realpath/normcase)",
        "tools/bma_labels.py",
        "    return os.path.normcase(os.path.realpath(p))",
        "    return os.path.abspath(p)",
        "analysis/test_labels.py",
    ),
    (
        "M25 add --labels-dir guard removed",
        "tools/bma_labels.py",
        '    sub = protected_hit(pa["labels"], pa)\n    if sub:\n        eprint("refusing to write under %s/" % sub)\n        return 2\n    events, _taxa',
        "    events, _taxa",
        "analysis/test_labels.py",
    ),
    (
        "M26 --out write error uncaught",
        "tools/bma_labels.py",
        '    except OSError as ex:\n        eprint("cannot write --out',
        '    except ZeroDivisionError as ex:\n        eprint("cannot write --out',
        "analysis/test_labels.py",
    ),
    ("M27 add: corrected_key speech guard removed", "tools/bma_labels.py", "L.is_speech_label(a.corrected_key)", "False", "analysis/test_labels.py"),
    (
        "M28 validator: corrected_key speech guard removed",
        "analysis/labels.py",
        '        if is_speech_label(rec["corrected_key"]):',
        "        if False:",
        "analysis/test_labels.py",
    ),
    ("M22 writer without the lock file", "analysis/labels.py", "        lock = self._lock()", "        lock = self.path + '.lock'", "analysis/test_labels.py"),
    (
        "E1 id drops the beam from the member text",
        "analysis/events.py",
        '";".join("%d:%d" % m for m in ms)',
        '";".join("%d" % m[0] for m in ms)',
        "analysis/test_events.py",
    ),
    ("E2 t quantised to 10 ms", "analysis/events.py", "return int(round(t * 1000))", "return int(round(t * 100)) * 10", "analysis/test_events.py"),
    (
        "E3 diff supersedes a split event",
        "analysis/events.py",
        "if len(homes) == 1 and None not in homes:",
        "if len(homes) >= 1 and None not in homes:",
        "analysis/test_events.py",
    ),
    ("E4 conservation check disabled", "analysis/events.py", "if self.input != self.reports + n_rej or", "if False or", "analysis/test_events.py"),
    ("E5 dedup keeps the duplicate window", "analysis/events.py", "            if dedup:", "            if False:", "analysis/test_events.py"),
    ("E6 alias chains not collapsed", "analysis/events.py", 'while tgt in d["superseded"]:', "while False:", "analysis/test_events.py"),
    ("E7 humans never hidden", "analysis/events.py", "if drop_human and is_human(o):", "if False:", "analysis/test_events.py"),
    ("E8 bird_filtered never rejected out of region", "analysis/events.py", 'if o["k"] not in kinds:', "if False:", "analysis/test_events.py"),
    ("E9 huge t crashes", "analysis/events.py", "except OverflowError:", "except ZeroDivisionError:", "analysis/test_events.py"),
    (
        "M21 partial last line not isolated by the writer",
        "analysis/labels.py",
        'if os.read(fd, 1) != b"\\n":\n                        prefix = b"\\n"',
        'if False:\n                        prefix = b"\\n"',
        "analysis/test_labels.py",
    ),
]


def run_test(root, test):
    p = subprocess.run([sys.executable, "-I", os.path.join(root, test)], capture_output=True, cwd=root)
    out = p.stdout.decode("utf-8", "replace").splitlines()
    return p.returncode, [l for l in out if l.startswith("FAIL")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="scratch directory (created if missing, a subfolder per run)")
    ap.add_argument("--only", default=None, help="run only the mutations whose name starts with this")
    a = ap.parse_args()
    os.makedirs(a.work, exist_ok=True)
    bad = 0
    base = os.path.join(a.work, "mut_base")
    shutil.rmtree(base, ignore_errors=True)
    shutil.copytree(
        BMA,
        base,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.bin*", "target", "node_modules", ".git", "board", "daemon", "dspd", "dante", "hook", "meter", "findings", "dms", "control"
        ),
    )
    for t in ("analysis/test_labels.py", "analysis/test_review_queue.py", "analysis/test_events.py"):
        rc, fl = run_test(base, t)
        print("baseline %s: exit %d, %d FAIL line(s)" % (t, rc, len(fl)))
        if rc != 0:
            return 1
    for name, rel, old, new, test in MUTATIONS:
        if a.only and not name.startswith(a.only):
            continue
        d = os.path.join(a.work, "mut_run")
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(base, d)
        p = os.path.join(d, rel)
        s = open(p, "rb").read().decode("utf-8")
        if s.count(old) != 1:
            print("BROKEN  %s: the text to mutate occurs %d times in %s" % (name, s.count(old), rel))
            bad += 1
            continue
        if name.startswith("M9"):
            s = s.replace("    frame_n = sum(len(v) for v in remaining.values())\n", "    frame_n = sum(len(v) for v in remaining.values())\n")
            s = s.replace("    audit_items = []\n", "    audit_items = []\n    frame_all = sum(len(v) for v in remaining.values())\n", 1)
        s = s.replace(old, new)
        open(p, "wb").write(s.encode("utf-8"))
        rc, fl = run_test(d, test)
        ok = rc != 0 and len(fl) > 0
        bad += 0 if ok else 1
        print("%s  %s  [%s exit %d, %d red]  %s" % ("RED  " if ok else "GREEN", name, os.path.basename(test), rc, len(fl), (fl[0][:150] if fl else "")))
    shutil.rmtree(os.path.join(a.work, "mut_run"), ignore_errors=True)
    print("%d mutation(s) stayed green or broke" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
