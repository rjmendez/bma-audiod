#!/usr/bin/env python3
"""Run every analysis/test_*.py suite, one after another, each in its own `python -I` process, and exit 1 if any failed.
    python run_tests.py                       # all suites, with THIS interpreter (use the audioeval venv: most need numpy)
    python run_tests.py test_beam_agreement   # only the named suites (the stdlib-only one runs anywhere)
This is the one entry point to wire into whatever recurring runner the checkout gets: the repo has no CI of its own, so an
unlisted suite is a suite nobody runs. A new test_*.py is picked up by name, nothing to register. Suites run sequentially:
several write temp files and bind loopback ports."""

import glob, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def suites(names):
    found = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(HERE, "test_*.py")))
    if not names:
        return found, []
    return [n for n in found if n in names], [n for n in names if n not in found]


def main(argv=None):
    names = list(sys.argv[1:] if argv is None else argv)
    todo, unknown = suites(names)
    if unknown:
        print("unknown suite(s): " + ", ".join(unknown), file=sys.stderr)
        return 2
    results = []
    for n in todo:
        print("== " + n, flush=True)
        rc = subprocess.run([sys.executable, "-I", os.path.join(HERE, n + ".py")], cwd=HERE).returncode
        results.append((n, rc))
    print()
    print("suite exit codes:")
    for n, rc in results:
        print("  %-28s %d" % (n, rc))
    return 1 if any(rc for _, rc in results) else 0


if __name__ == "__main__":
    sys.exit(main())
