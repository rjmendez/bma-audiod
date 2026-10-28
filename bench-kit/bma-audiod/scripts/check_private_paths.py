#!/usr/bin/env python3
"""Fail when a tracked file contains a private, machine-specific value.

Scans the files git would commit: `git ls-files` plus untracked files that are
not ignored, of the repository that contains --root (default: the current
directory). A symlink is scanned as its own path with its target string as content
(never followed). Gitignored local override files are never reported.
Categories: drive-path, wsl-mount, home-path, lan-ip, link-local (169.254/16:
the unit's own Dante-link addresses, allowlisted per file with a reason), mac,
hostname, token-file, distro, coords, job-dir (and `name` for the optional
local names file, see below).

Allowlist (TOML, default scripts/private_paths_allowlist.toml next to this
script): every entry needs `path` (fnmatch glob, repo-relative, '/'-separated)
and `reason`. Either `skip = true` (file is not scanned: binary or third-party)
or `categories` + `count` (the exact number of matches the entry covers). The
list can only shrink: a wrong count or an entry that matches nothing fails.

Optional private names: one regex per line in `private_names.txt` next to this
script (gitignored). Matches are reported as category `name`. The repository
itself carries no names.

Usage: python -I scripts/check_private_paths.py [--root DIR] [--allowlist FILE]
Exit 0 clean, 1 findings, 2 usage/allowlist error.
"""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))

_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"

# (category, compiled regex). Patterns are written so that this file does not
# match itself.
PATTERNS = [
    ("drive-path", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\{1,2}|/)(?=[\w.$%{(~-]{2})")),
    ("wsl-mount", re.compile(r"/mnt/[a-z](?:/|\b)")),
    ("home-path", re.compile(r"/home/[A-Za-z][\w.-]*/|/Us[e]rs/(?!<)|~/(?:development|\.venvs)|\$HOME/\.venvs")),
    (
        "lan-ip",
        re.compile(
            rf"(?<![\d.])(?:10\.{_OCTET}\.{_OCTET}\.{_OCTET}"
            rf"|172\.(?:1[6-9]|2\d|3[01])\.{_OCTET}\.{_OCTET}"
            rf"|192\.168\.{_OCTET}\.{_OCTET}"
            rf"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.{_OCTET}\.{_OCTET})(?![\d]|\.\d)"
        ),
    ),
    ("link-local", re.compile(rf"(?<![\d.])169\.254\.{_OCTET}\.{_OCTET}(?![\d]|\.\d)")),
    ("mac", re.compile(r"(?<![0-9A-Za-z:-])[0-9A-Fa-f]{2}(?P<sep>[:-])(?:[0-9A-Fa-f]{2}(?P=sep)){4}[0-9A-Fa-f]{2}(?![0-9A-Za-z:-])")),
    ("hostname", re.compile(r"\.ts\.net\b|DESKTOP-[A-Z0-9]{5,}|\b[a-z0-9-]+\.lab\b|\b[a-z0-9-]+\.local\b(?![.\w-])")),
    ("token-file", re.compile(r"\.bma_dspd_tok(?:en)")),
    ("distro", re.compile(r"\bUbuntu(?:Cap)\b")),
    ("coords", re.compile(r"""["']?(?:lat|lon|lng)["']?\s*[:=]\s*-?(?!0+\.0+\b)\d+\.\d+""")),
    ("job-dir", re.compile(r"\.claude[\\/]jobs")),
]

# RFC 7042 documentation block, IANA multicast, all-zero / all-ones.
_MAC_OK = re.compile(
    r"^(?:00[:-]00[:-]5e[:-]00[:-]53[:-][0-9a-f]{2}|01[:-]00[:-]5e[:-][0-9a-f:-]{8}"
    r"|(?:00[:-]){5}00|(?:ff[:-]){5}ff)$",
    re.I,
)


def load_names(path):
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(("name", re.compile(ln, re.I)))
    return out


def scan_text(text, patterns):
    """Yield (lineno, category, match_text)."""
    for n, line in enumerate(text.splitlines(), 1):
        for cat, rx in patterns:
            for m in rx.finditer(line):
                if cat == "mac" and _MAC_OK.match(m.group(0)):
                    continue
                yield n, cat, m.group(0)


def tracked_files(root):
    r = subprocess.run(
        ["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--full-name", ":/"], capture_output=True, check=True
    )
    top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, check=True, text=True).stdout.strip()
    return top, [p for p in r.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def load_allowlist(path):
    if not os.path.isfile(path):
        return []
    with open(path, "rb") as f:
        entries = tomllib.load(f).get("allow", [])
    for e in entries:
        if "path" not in e or not e.get("reason"):
            raise ValueError(f"allowlist entry needs path and reason: {e}")
        if not e.get("skip") and ("categories" not in e or "count" not in e):
            raise ValueError(f"allowlist entry needs skip or categories+count: {e['path']}")
    return entries


def check(root, allowlist, patterns):
    """Return (findings, problems): finding lines and allowlist problems."""
    top, files = tracked_files(root)
    entries = allowlist
    used = [0] * len(entries)
    counts = [0] * len(entries)
    findings = []
    for rel in sorted(files):
        full = os.path.join(top, rel)
        link = os.path.islink(full)
        if not link and not os.path.isfile(full):
            continue
        skip_idx = [i for i, e in enumerate(entries) if e.get("skip") and fnmatch.fnmatchcase(rel, e["path"])]
        if skip_idx:
            for i in skip_idx:
                used[i] += 1
            continue
        if link:
            data = os.fsencode(os.readlink(full))
        else:
            with open(full, "rb") as f:
                data = f.read()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", "replace")
        cnt_idx = [i for i, e in enumerate(entries) if not e.get("skip") and fnmatch.fnmatchcase(rel, e["path"])]
        for n, cat, m in scan_text(text, patterns):
            owner = next((i for i in cnt_idx if cat in entries[i]["categories"] or "*" in entries[i]["categories"]), None)
            if owner is None:
                findings.append(f"{rel}:{n}: [{cat}] {m}")
            else:
                counts[owner] += 1
                used[owner] += 1
    problems = []
    for i, e in enumerate(entries):
        if used[i] == 0:
            problems.append(f"allowlist: {e['path']}: entry matches nothing (remove it)")
        elif not e.get("skip") and counts[i] != e["count"]:
            problems.append(f"allowlist: {e['path']}: count is {counts[i]}, entry says {e['count']}")
    return findings, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--allowlist", default=os.path.join(HERE, "private_paths_allowlist.toml"))
    ap.add_argument("--names", default=os.path.join(HERE, "private_names.txt"))
    a = ap.parse_args(argv)
    try:
        entries = load_allowlist(a.allowlist)
    except (ValueError, tomllib.TOMLDecodeError) as e:
        print(f"check_private_paths: bad allowlist: {e}", file=sys.stderr)
        return 2
    findings, problems = check(a.root, entries, PATTERNS + load_names(a.names))
    for line in findings + problems:
        print(line)
    if findings or problems:
        print(f"check_private_paths: {len(findings)} finding(s), {len(problems)} allowlist problem(s)")
        return 1
    print("check_private_paths: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
