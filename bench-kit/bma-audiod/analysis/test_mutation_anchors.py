#!/usr/bin/env python3
"""Self-check of analysis/mutation_clips.py: every mutation anchor must still match EXACTLY ONCE in the file it mutates (an anchor that stops
applying silently proves nothing), and the harness must report a non-applying anchor loudly. Fast (no test_clips run).
Run:  python -I test_mutation_anchors.py"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mutation_clips as MC  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


check("anchor_problem: one occurrence applies (None)", MC.anchor_problem("a b a", "b") is None)
check("anchor_problem: zero occurrences is reported with the count", MC.anchor_problem("a b a", "z") == "mutation does not apply: 'z' found 0 times")
check("anchor_problem: two occurrences is reported with the count", MC.anchor_problem("a b a", "a") == "mutation does not apply: 'a' found 2 times")
check("summary: all caught -> exit 0", MC.summary(98, 0, 0) == ("98 mutations, 0 not caught, 0 did not apply", 0))
check("summary: one uncaught -> exit 1", MC.summary(98, 1, 0) == ("98 mutations, 1 not caught, 0 did not apply", 1))
check("summary: an anchor that did not apply -> exit 2 (even when everything else was caught)", MC.summary(98, 0, 1) == ("98 mutations, 0 not caught, 1 did not apply", 2))
name, caught, why = MC.run_one(("bogus", "clips.py", [("this text is not in clips.py", "x")], False))
check("run_one: a missing anchor returns (name, None, reason) without running the tests", (name, caught) == ("bogus", None) and why == ["mutation does not apply: 'this text is not in clips.py' found 0 times"], str((caught, why)))
texts = {}
bad = []
for name, fn, edits, _ in MC.M:
    t = texts.setdefault(fn, open(os.path.join(HERE, fn), encoding="utf-8").read())
    for old, _new in edits:
        w = MC.anchor_problem(t, old)
        if w:
            bad.append(f"{name}: {w}")
check(f"all {len(MC.M)} mutations apply: every anchor matches exactly once in its file", bad == [], "; ".join(bad))
print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
