#!/usr/bin/env python3
"""Mutation check for feature_state.py: each mutation edits one behaviour in a COPY of tools/, runs test_feature_state.py against it and
requires a non-zero exit. The unmutated copy must pass first.

    python mutation_feature_state.py [name-substring]        Exit 0 only if the baseline is green and every mutation was caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    ("a flag named in a comment counts as passed", """if not ln.lstrip().startswith("#"))""", "if True)"),
    (
        "rows are not sorted OFF first",
        """    order = {OFF: 0, UNKNOWN: 1, ON: 2}
    rows = sorted(rows""",
        """    order = {OFF: 2, UNKNOWN: 1, ON: 0}
    rows = sorted(rows""",
    ),
    ("a comment line of allowed_users.txt counts as a login", 'if ln.strip() and not ln.strip().startswith("#")]', "if ln.strip()]"),
    ("passkeys need only the marker", 'if c.exists("enable_passkeys.txt") and c.exists("tls", "tls_name.txt"):', 'if c.exists("enable_passkeys.txt"):'),
    ("a switch takes the next flag as its value", """return "" if nxt.startswith("--") else nxt""", "return nxt"),
    ("a detector failure leaks the exception text", '''f"detector failed: {type(e).__name__}"''', '''f"detector failed: {type(e).__name__}: {e}"'''),
    ("the exit code is 1", "    return 0\n\n\nif __name__", "    return 1\n\n\nif __name__"),
    ("--write writes nothing", "        if a.write:", "        if False:"),
    ("the running analyzer's audio state is ignored", '        if cl["audio"]:', "        if False:"),
    ("a --no line is ignored", "        if name in c.removed():", "        if False:"),
    ("a broken status.json is trusted", "        except ValueError:\n            return {}", "        except ValueError:\n            return {'plugins': {}}"),
    ("pending is not warned about", 'for cell in ("pending", "N"):', 'for cell in ("N",):'),
    ("N is not warned about", 'for cell in ("pending", "N"):', 'for cell in ("pending",):'),
    ("an OFF feature is warned about", 'if r["state"] == ON and approved.get', 'if r["state"] != UNKNOWN and approved.get'),
    ("the Approved column is the wrong one", 'col = cells.index("Approved")', 'col = cells.index("Approved") - 1'),
    ("the warnings are not printed", "    out += list(warnings)", "    out += []"),
    ("the supervisor start line is not read", 'ups = [ln for ln in lines if " supervisor up" in ln]', "ups = []"),
]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_fs_mut_")
    bad = 0

    def run(d):
        p = subprocess.run([sys.executable, "-I", os.path.join(d, "test_feature_state.py")], capture_output=True, text=True, timeout=300)
        return p.returncode, [l for l in (p.stdout + p.stderr).splitlines() if l.startswith("FAIL") or "Error" in l][:2]

    try:
        base = os.path.join(tmp, "base")
        shutil.copytree(HERE, base, ignore=shutil.ignore_patterns("__pycache__"))
        rc, _ = run(base)
        print(f"baseline exit {rc} (must be 0)")
        if rc != 0:
            return 1
        for name, a, b in M:
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, "feature_state.py")
            s = open(p, newline="", encoding="utf-8").read()
            if s.count(a) != 1:
                print(f"BROKEN mutation (pattern found {s.count(a)} times): {name}")
                bad += 1
                continue
            open(p, "w", newline="", encoding="utf-8").write(s.replace(a, b))
            rc, fl = run(d)
            print(("RED  " if rc != 0 else "NOT RED ") + name + ("  <- " + fl[0][:110] if fl else ""))
            bad += rc == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
