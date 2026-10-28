#!/usr/bin/env python3
"""Tests for scripts/check_ruff.py: command lines with a recording stub, exit codes, and (where ruff is installed) the real ruff on scratch trees.
Run: python scripts/test_check_ruff.py   (the real-ruff checks are skipped, and said so, when ruff is not installed)"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("check_ruff", os.path.join(HERE, "check_ruff.py"))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

fails, n = [], 0


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


class Run:
    def __init__(self, *codes):
        self.codes, self.calls = list(codes), []

    def __call__(self, cmd, cwd=None):
        self.calls.append((cmd, cwd))
        return type("R", (), {"returncode": self.codes.pop(0)})()


R = ["ruff"]
eq("commands without --format: check only", C.commands(R, False), [["ruff", "check", "."]])
eq("commands with --format: check then format --check", C.commands(R, True), [["ruff", "check", "."], ["ruff", "format", "--check", "."]])
r = Run(0)
eq("clean check: exit 0, one call in the root", (C.main(["--root", "X"], R, r), r.calls), (0, [(["ruff", "check", "."], "X")]))
r = Run(1)
eq("findings: exit 1", C.main(["--root", "X"], R, r), 1)
r = Run(0, 1)
eq("--format: a clean check and an unformatted tree is exit 1, both commands ran", (C.main(["--root", "X", "--format"], R, r), len(r.calls)), (1, 2))
r = Run(1, 0)
eq("--format: findings are not hidden by a clean format check", C.main(["--root", "X", "--format"], R, r), 1)
r = Run(0, 0)
eq("--format twin: both clean is exit 0", C.main(["--root", "X", "--format"], R, r), 0)
r = Run()
eq("ruff not installed: exit 77 and nothing runs", (C.main(["--root", "X"], None, r), r.calls), (77, []))

if C.ruff_cmd() is None:
    print("SKIP real-ruff checks: ruff is not installed")
else:
    import subprocess

    d = tempfile.mkdtemp()
    open(os.path.join(d, "ruff.toml"), "w").write('line-length = 160\n[lint]\nselect = ["E9", "F82"]\n')
    open(os.path.join(d, "ok.py"), "w").write("x = 1\nprint(x)\n")
    eq("real ruff: a clean tree is exit 0", C.main(["--root", d]), 0)
    open(os.path.join(d, "bad.py"), "w").write("print(undefined_name_zz)\n")
    eq("real ruff: an undefined name is exit 1", C.main(["--root", d]), 1)
    os.remove(os.path.join(d, "bad.py"))
    open(os.path.join(d, "fmt.py"), "w").write("x   =   1\n")
    eq("real ruff: --format fails on an unformatted file", C.main(["--root", d, "--format"]), 1)
    eq("real ruff twin: the same file passes without --format", C.main(["--root", d]), 0)
    subprocess.run(C.ruff_cmd() + ["format", "."], cwd=d, capture_output=True)
    eq("real ruff twin: after formatting, --format passes", C.main(["--root", d, "--format"]), 0)
    eq("the kit's own tree passes", C.main([]), 0)

print(f"\n{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
