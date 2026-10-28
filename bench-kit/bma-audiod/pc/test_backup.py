#!/usr/bin/env python3
"""Tests for pc/backup_data.ps1: the REAL script runs under Windows PowerShell against temp data dirs (no stubs). Exact file sets, bytes, retention.

python test_backup.py        (exit 77 with SKIP when powershell is missing)"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "backup_data.ps1")
PS = shutil.which("powershell") or shutil.which("pwsh")
if not PS:
    print("SKIP: powershell not found")
    sys.exit(77)
n, fails = 0, []


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + str(detail)[:300] + "]"))
    if not ok:
        fails.append(name)


def put(root, rel, data=b"x"):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def run(root, *extra):
    r = subprocess.run([PS, "-NoProfile", "-File", SCRIPT, "-Root", root] + list(extra), capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def tree(d):
    out = []
    for dp, _, fs in os.walk(d):
        out += [os.path.relpath(os.path.join(dp, f), d).replace("\\", "/") for f in fs]
    return sorted(out)


def fixture(root):
    put(root, "labels/labels.jsonl", b"L1\n")
    put(root, "labels/labels.jsonl.lock", b"")
    put(root, "history/loss.jsonl", b"AB\n")
    put(root, "history/ambient_temp.jsonl", b"C\n")
    put(root, "history/daily_summary.json", b"{}")
    put(root, "analysis/clips/index.jsonl", b"IDX\n")
    put(root, "analysis/clips/20261027/a-b1.json", b"{}")
    put(root, "analysis/clips/20261027/a-b1.png", b"PNG")
    put(root, "analysis/clips/20261027/a-b1.flac", b"AUDIO")
    put(root, "analysis/clips/20261027/b-b2.wav", b"AUDIO")
    put(root, "analysis/clips/20261027/c-b3.opus", b"AUDIO")
    put(root, "analysis/dsp.jsonl", b"BIG")
    put(root, "analysis/novelty_state.pkl", b"NOV")
    put(root, "analysis/other_state.pkl", b"NO")
    put(root, "reports/summary.json", b"{}")
    put(root, "analyzer_args.txt", b"--x\n")
    put(root, "bma.local.ps1", b"$BMA_BOARD='h'")
    put(root, "dashboard_token.txt", b"SECRETTOKEN")
    put(root, "tls/key.pem", b"KEY")


with tempfile.TemporaryDirectory(prefix="bma_bk_") as tmp:
    root = os.path.join(tmp, "data")
    fixture(root)
    rc, out, err = run(root, "-Day", "20261027")
    got = tree(os.path.join(root, "backup", "20261027"))
    want = sorted(
        [
            "labels/labels.jsonl",
            "analyzer_args.txt",
            "history/loss.jsonl",
            "history/ambient_temp.jsonl",
            "history/daily_summary.json",
            "analysis/novelty_state.pkl",
            "analysis/clips/index.jsonl",
            "analysis/clips/20261027/a-b1.json",
            "analysis/clips/20261027/a-b1.png",
        ]
    )
    check("copies exactly the allowlisted files", got == want, got)

    # a root in 8.3 short form: Get-ChildItem returns long names, Resolve-Path keeps the short one
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(root, buf, 1024) if os.name == "nt" else 0
    short = buf.value if n else root
    if short.lower() == root.lower():
        print("SKIP 8.3 root: no short name for the temp directory on this volume")
    else:
        shutil.rmtree(os.path.join(root, "backup"))
        rc_s, out_s, err_s = run(short, "-Day", "20261027")
        got_s = tree(os.path.join(root, "backup", "20261027"))
        check("a root given as an 8.3 short path copies the same relative paths", (rc_s, got_s) == (0, want), (rc_s, got_s, err_s))
    check(
        "exit 0 and a counts-only summary line", (rc, err) == (0, "") and out == "backup 20261027: 9 files, 26 bytes, 0 old removed, 0 failed", (rc, out, err)
    )
    check("a copied file has the source bytes", open(os.path.join(root, "backup", "20261027", "labels", "labels.jsonl"), "rb").read() == b"L1\n")
    flat = " ".join(got)
    check("no audio file (flac, wav, opus) in the backup", not any(x in flat for x in (".flac", ".wav", ".opus")), got)
    check(
        "the dashboard token, bma.local.ps1 and tls are not in the backup", not any(x in flat for x in ("dashboard_token", "tls/", "key.pem", "local.ps1")), got
    )
    check(
        "the label lock file, analysis/dsp.jsonl, another .pkl and a .json outside history are not copied",
        not ({"labels/labels.jsonl.lock", "analysis/dsp.jsonl", "analysis/other_state.pkl", "reports/summary.json"} & set(got)),
        got,
    )
    check(
        "history/daily_summary.json and analysis/novelty_state.pkl are copied with the source bytes",
        open(os.path.join(root, "backup", "20261027", "history", "daily_summary.json"), "rb").read() == b"{}"
        and open(os.path.join(root, "backup", "20261027", "analysis", "novelty_state.pkl"), "rb").read() == b"NOV",
        got,
    )
    check(
        "the last-backup marker holds the day and a newline, written beside the dated folders",
        open(os.path.join(root, "backup", "last_backup.txt"), "rb").read() == b"20261027\n",
        os.listdir(os.path.join(root, "backup")),
    )
    check(
        "the source files are untouched",
        open(os.path.join(root, "dashboard_token.txt"), "rb").read() == b"SECRETTOKEN"
        and os.path.exists(os.path.join(root, "analysis/clips/20261027/a-b1.flac")),
    )
    # a rerun the same day overwrites the same folder with the changed source
    put(root, "labels/labels.jsonl", b"L1\nL2\n")
    rc, out, _ = run(root, "-Day", "20261027")
    check(
        "a rerun the same day refreshes the same folder",
        rc == 0
        and open(os.path.join(root, "backup", "20261027", "labels", "labels.jsonl"), "rb").read() == b"L1\nL2\n"
        and sorted(os.listdir(os.path.join(root, "backup"))) == ["20261027", "last_backup.txt"],
        out,
    )

    # retention: 14 days kept, the 15th and older removed; non-date folders and files untouched
    bk = os.path.join(root, "backup")
    for d in ("20261012", "20261013", "20261014", "20261001", "20260101"):
        put(bk, d + "/labels/labels.jsonl", b"o")
    put(bk, "notes/keep.txt", b"k")
    put(bk, "20261012x/keep.txt", b"k")
    put(bk, "20261028/labels/labels.jsonl", b"future")
    rc, out, _ = run(root, "-Day", "20261027")
    left = sorted(os.listdir(bk))
    check(
        "retention: 20261014..20261027 kept (14 days), older dated folders removed",
        left == sorted(["20261014", "20261027", "20261028", "20261012x", "notes", "last_backup.txt"]),
        left,
    )
    check("retention summary counts 4 removed", out == "backup 20261027: 9 files, 29 bytes, 4 old removed, 0 failed", out)
    check(
        "non-date folders and later-dated folders keep their files",
        os.path.exists(os.path.join(bk, "notes", "keep.txt")) and os.path.exists(os.path.join(bk, "20261028", "labels", "labels.jsonl")),
    )

    # missing sources: an empty data dir is a valid run with 0 files
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    rc, out, err = run(empty, "-Day", "20261027")
    check(
        "an empty data dir: exit 0, 0 files, empty dated folder not required",
        rc == 0 and out == "backup 20261027: 0 files, 0 bytes, 0 old removed, 0 failed",
        (rc, out, err),
    )
    # a copy that cannot be made is counted, exits 1, and does not stop the other files
    blocked = os.path.join(tmp, "blocked")
    fixture(blocked)
    put(blocked, "backup/20261027/labels", b"a file where the labels folder must go")
    rc, out, err = run(blocked, "-Day", "20261027")
    check(
        "a blocked copy: exit 1, 8 of 9 files copied, 1 failed",
        rc == 1
        and out == "backup 20261027: 8 files, 23 bytes, 0 old removed, 1 failed"
        and os.path.exists(os.path.join(blocked, "backup", "20261027", "analyzer_args.txt")),
        (rc, out, err),
    )
    check(
        "a run with a failed copy writes no marker (positive twin: the clean run above wrote it)",
        not os.path.exists(os.path.join(blocked, "backup", "last_backup.txt")),
        os.listdir(os.path.join(blocked, "backup")),
    )
    # hourly labels-only run
    lo = os.path.join(tmp, "lo")
    fixture(lo)
    rc, out, err = run(lo, "-Day", "20261027", "-LabelsOnly")
    check(
        "-LabelsOnly: exactly labels/labels.jsonl is copied, exit 0, its own summary line",
        (rc, err) == (0, "")
        and tree(os.path.join(lo, "backup", "20261027")) == ["labels/labels.jsonl"]
        and out == "backup 20261027 labels: 1 files, 3 bytes, 0 failed",
        (rc, out, err, tree(os.path.join(lo, "backup"))),
    )
    check(
        "-LabelsOnly writes no marker and removes no old folder (an old dated folder stays; positive twin: a full run removes it)",
        not os.path.exists(os.path.join(lo, "backup", "last_backup.txt")),
        os.listdir(os.path.join(lo, "backup")),
    )
    put(lo, "backup/20240101/labels/labels.jsonl", b"o")
    run(lo, "-Day", "20261027", "-LabelsOnly")
    kept = os.path.isdir(os.path.join(lo, "backup", "20240101"))
    run(lo, "-Day", "20261027")
    check(
        "-LabelsOnly keeps an old dated folder; the full run then removes it",
        kept and not os.path.isdir(os.path.join(lo, "backup", "20240101")),
        (kept, os.listdir(os.path.join(lo, "backup"))),
    )
    put(lo, "labels/labels.jsonl", b"L1\nL2\n")
    run(lo, "-Day", "20261027", "-LabelsOnly")
    check(
        "-LabelsOnly refreshes the labels copy within the day",
        open(os.path.join(lo, "backup", "20261027", "labels", "labels.jsonl"), "rb").read() == b"L1\nL2\n",
    )
    # usage errors
    rc, _, err = run(root, "-Day", "2026-10-27")
    check("a malformed -Day is exit 2", (rc, err) == (2, "Day must be yyyyMMdd"), (rc, err))
    rc, _, err = run(os.path.join(tmp, "nope"), "-Day", "20261027")
    check("a missing root is exit 2", (rc, err) == (2, "Root is not a directory"), (rc, err))
    rc, _, err = run(root, "-Day", "20261027", "-Keep", "0")
    check("-Keep 0 is exit 2", (rc, err) == (2, "Keep must be >= 1"), (rc, err))
    # Keep=1 positive twin: only today stays
    rc, out, _ = run(root, "-Day", "20261027", "-Keep", "1")
    check(
        "-Keep 1 leaves only the day's folder among dated folders at or before it",
        "20261014" not in os.listdir(bk) and "20261027" in os.listdir(bk),
        os.listdir(bk),
    )
    # default Day = today (local)
    import datetime

    rc, out, _ = run(root)
    today = datetime.date.today().strftime("%Y%m%d")
    check("no -Day: today's local date", rc == 0 and out.startswith("backup " + today + ":") and os.path.isdir(os.path.join(bk, today)), out)

print("\n%d checks, %d failure(s)" % (n, len(fails)))
sys.exit(1 if fails else 0)
