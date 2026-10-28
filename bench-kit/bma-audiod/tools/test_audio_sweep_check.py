#!/usr/bin/env python3
"""Tests for tools/audio_sweep_check.py on scratch directories with files of exact ages (os.utime). No stubs.   python -I test_audio_sweep_check.py"""

import contextlib, io, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import audio_sweep_check as A  # noqa: E402

n_checks, fails = 0, []
NOW = 1793100000.0
H = 3600.0


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail)[:400] + "]"))
    if not cond:
        fails.append(name)


def put(base, rel, size, age_h):
    p = os.path.join(base, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"a" * size)
    t = NOW - age_h * H
    os.utime(p, (t, t))
    return p


def run(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = A.main(argv, now=NOW)
    return rc, buf.getvalue(), err.getvalue()


with tempfile.TemporaryDirectory() as d:
    root, extra = os.path.join(d, "root"), os.path.join(d, "scratch")
    os.makedirs(root)
    os.makedirs(extra)
    old_wav = put(root, "analysis/clips/20261026/a.wav", 100, 30.0)
    old_flac = put(root, "analysis/clips/20261026/b.flac", 200, 50.5)
    old_opus_up = put(root, "c.OPUS", 300, 24.5)
    put(root, "fresh.wav", 10, 1.0)
    put(root, "edge_exact.wav", 10, 24.0)
    put(root, "edge_under.wav", 10, 23.99)
    put(root, "notes.txt", 10, 99.0)
    put(root, "analysis/clips/20261026/a.png", 10, 99.0)
    put(root, "future.wav", 10, -5.0)
    old_extra = put(extra, "sub/dir/rec.wav", 400, 100.0)
    before = {p: (os.path.getsize(p), os.stat(p).st_mtime) for p in (old_wav, old_flac, old_opus_up, old_extra)}

    rc, out, err = run(["--root", root, "--extra", extra])
    want = (
        "4 audio files older than 24 h, 1000 bytes\n"
        "100.0 h  400 B  extra1:sub/dir/rec.wav\n"
        "50.5 h  200 B  root:analysis/clips/20261026/b.flac\n"
        "30.0 h  100 B  root:analysis/clips/20261026/a.wav\n"
        "24.5 h  300 B  root:c.OPUS\n"
    )
    check(
        "lists exactly the wav/flac/opus files older than 24 h (upper-case extension included, extra root labelled), oldest first, exact text",
        out == want and err == "",
        (out, err),
    )
    check("exit code 1 when something is found", rc == 1, rc)
    check(
        "exactly 24.0 h old is not listed and 23.99 h is not; positive twin: 24.5 h is (c.OPUS above)",
        "edge_exact" not in out and "edge_under" not in out and "c.OPUS" in out,
    )
    check("a non-audio file, a png and a future-dated wav are never listed", not any(x in out for x in ("notes.txt", "a.png", "future.wav")), out)
    check(
        "nothing is deleted or touched: every listed file keeps its size and mtime and still exists",
        all(os.path.exists(p) and (os.path.getsize(p), os.stat(p).st_mtime) == v for p, v in before.items()),
    )

    rc, out, _ = run(["--root", root])
    check(
        "without --extra the scratch directory is not scanned (positive twin of the extra root)",
        rc == 1 and out.splitlines()[0] == "3 audio files older than 24 h, 600 bytes" and "extra1" not in out,
        out,
    )
    rc, out, _ = run(["--root", root, "--age-h", "48"])
    check(
        "--age-h 48: only the files older than 48 h (50.5 h flac)",
        out.splitlines() == ["1 audio files older than 48 h, 200 bytes", "50.5 h  200 B  root:analysis/clips/20261026/b.flac"],
        out,
    )
    rc, out, _ = run(["--root", root, "--extra", extra, "--max-lines", "2"])
    check(
        "--max-lines 2: the totals still count all four, two lines are printed, then `... 2 more`",
        out.splitlines() == want.splitlines()[:3] + ["... 2 more"],
        out,
    )

    clean = os.path.join(d, "clean")
    os.makedirs(clean)
    put(clean, "x.wav", 10, 2.0)
    put(clean, "y.txt", 10, 200.0)
    rc, out, _ = run(["--root", clean])
    check("nothing old: header with 0 files and 0 bytes, exit 0", (rc, out) == (0, "0 audio files older than 24 h, 0 bytes\n"), (rc, out))

    # a link to a directory is not followed (a loop or a scan outside the root would otherwise be possible)
    outside = os.path.join(d, "outside")
    put(outside, "o.wav", 10, 500.0)
    linkroot = os.path.join(d, "linkroot")
    os.makedirs(linkroot)
    try:
        os.symlink(outside, os.path.join(linkroot, "link"), target_is_directory=True)
        linked = True
    except (OSError, NotImplementedError):
        linked = False
    if linked:
        rc, out, _ = run(["--root", linkroot])
        check("a symlinked directory is not followed", (rc, out) == (0, "0 audio files older than 24 h, 0 bytes\n"), (rc, out))
    else:
        print("SKIP symlink check: this account cannot create symlinks")

    rc, out, err = run(["--root", os.path.join(d, "nope")])
    check(
        "a missing --root is exit 2 with one stderr line naming the option only (no path)",
        (rc, out, err) == (2, "", "not a directory: --root\n"),
        (rc, out, err),
    )
    rc, out, err = run(["--root", root, "--extra", os.path.join(d, "nope")])
    check("a missing --extra is exit 2", (rc, out, err) == (2, "", "not a directory: --extra\n"), (rc, out, err))
    rc, out, err = run(["--root", root, "--age-h", "-1"])
    check("a negative --age-h is exit 2", (rc, out, err) == (2, "", "--age-h and --max-lines must not be negative\n"), (rc, out, err))

    cli = os.path.join(d, "cli")
    f_ = put(cli, "ancient.flac", 5, 0.0)
    os.utime(f_, (1000000000.0, 1000000000.0))
    r_ = put(cli, "recent.wav", 7, 0.0)
    os.utime(r_, None)  # the subprocess uses the real clock: "just now" must be the real now, not the fixed NOW
    p = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "audio_sweep_check.py"), "--root", cli, "--max-lines", "0"], capture_output=True, text=True, timeout=60
    )
    check(
        "subprocess with the real clock: an audio file from 2001 is found, a file written just now is not; exit 1, exact header",
        (p.returncode, p.stdout, p.stderr) == (1, "1 audio files older than 24 h, 5 bytes\n... 1 more\n", ""),
        (p.returncode, p.stdout, p.stderr),
    )

print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
sys.exit(1 if fails else 0)
