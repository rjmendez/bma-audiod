#!/usr/bin/env python3
"""Fail when a commit after --base carries an AI attribution trailer (owner policy: no AI attribution).

    python -I scripts/check_commit_trailers.py [--repo DIR] [--base REV] [--head REV]

Scans every commit in BASE..HEAD of the repository that contains --repo (default: the current directory). A trailer line is
a violation when it starts with `Co-authored-by:` and names an AI (Claude, Anthropic, Copilot, ChatGPT, OpenAI, Codex, Gemini,
Cursor, Devin, bot noreply addresses of those vendors), or contains `Generated with` / `Generated-with` followed by an AI name.
Default --base is the master tip at the time of writing: older commits keep their trailers (history is not rewritten).
Merge with `gh pr merge --squash --body ""`: a squash merge otherwise appends a Co-authored-by line per PR commit author.
Exit 0 clean, 1 violations (one line each: short hash, line), 2 usage or git error."""

import argparse
import re
import subprocess
import sys

DEFAULT_BASE = "f64f9968e8ecd1a699ebf5bff56837a3d5f85660"
AI = r"(claude|anthropic|copilot|chatgpt|openai|codex|gemini|cursor|devin)"
CO_AUTHOR = re.compile(r"^\s*co-authored-by\s*:.*" + AI, re.I)
GENERATED = re.compile(r"generated[ -]with\b.*" + AI, re.I)


def violations(message):
    """-> the offending lines of one commit message, in order."""
    return [ln.strip() for ln in message.splitlines() if CO_AUTHOR.match(ln) or GENERATED.search(ln)]


def scan(repo, base, head="HEAD"):
    """-> [(short hash, line)] for every violating commit in base..head, oldest last (git log order)."""
    r = subprocess.run(["git", "-C", repo, "log", "--format=%H%x1f%B%x1e", f"{base}..{head}"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"git log exited {r.returncode}")
    out = []
    for rec in r.stdout.split("\x1e"):
        if "\x1f" not in rec:
            continue
        h, msg = rec.split("\x1f", 1)
        out += [(h.strip()[:12], ln) for ln in violations(msg)]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default=DEFAULT_BASE, help="exclusive lower bound (default: the master tip when this script was written)")
    ap.add_argument("--head", default="HEAD")
    a = ap.parse_args(argv)
    try:
        found = scan(a.repo, a.base, a.head)
    except (RuntimeError, OSError) as e:
        print(f"check_commit_trailers: {e}", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # a trailer line may hold an emoji; a cp1252 console must not crash the report
    for h, ln in found:
        print(f"{h} {ln}")
    print(f"{len(found)} AI attribution trailer(s) in {a.base[:12]}..{a.head}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
