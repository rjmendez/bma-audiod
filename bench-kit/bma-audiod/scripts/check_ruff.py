#!/usr/bin/env python3
"""Run ruff over the kit with its ruff.toml: `ruff check`, and `ruff format --check` with --format.

    python scripts/check_ruff.py [--root DIR] [--format]

Exit 0 clean, 1 findings (ruff's own output), 2 usage, 77 with a SKIP line when ruff is not installed (CI installs it from scripts/requirements-ci.txt)."""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys

BMA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ruff_cmd():
    """-> argv prefix that runs ruff, or None."""
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    exe = shutil.which("ruff")
    return [exe] if exe else None


def commands(prefix, fmt):
    cmds = [prefix + ["check", "."]]
    if fmt:
        cmds.append(prefix + ["format", "--check", "."])
    return cmds


def main(argv=None, prefix="auto", run=subprocess.run):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=BMA)
    ap.add_argument("--format", action="store_true", help="also fail on files `ruff format` would change")
    a = ap.parse_args(argv)
    prefix = ruff_cmd() if prefix == "auto" else prefix
    if prefix is None:
        print("SKIP: ruff is not installed (pip install -r scripts/requirements-ci.txt)")
        return 77
    rc = 0
    for cmd in commands(prefix, a.format):
        r = run(cmd, cwd=a.root)
        rc = rc or (1 if r.returncode else 0)
    return rc


if __name__ == "__main__":
    sys.exit(main())
