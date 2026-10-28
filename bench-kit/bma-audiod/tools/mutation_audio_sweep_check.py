#!/usr/bin/env python3
"""Mutation check for tools/audio_sweep_check.py: each mutation edits one behaviour in a COPY and test_audio_sweep_check.py must then exit 1.
The unmutated copy must exit 0 first.   python mutation_audio_sweep_check.py [name-substring]"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    ("flac is not an audio extension", 'EXTS = (".wav", ".flac", ".opus")', 'EXTS = (".wav", ".opus")'),
    ("opus is not an audio extension", 'EXTS = (".wav", ".flac", ".opus")', 'EXTS = (".wav", ".flac")'),
    ("wav is not an audio extension", 'EXTS = (".wav", ".flac", ".opus")', 'EXTS = (".flac", ".opus")'),
    ("png counts as audio", 'EXTS = (".wav", ".flac", ".opus")', 'EXTS = (".wav", ".flac", ".opus", ".png")'),
    ("the extension match is case sensitive", "os.path.splitext(f)[1].lower() not in EXTS", "os.path.splitext(f)[1] not in EXTS"),
    ("exactly the age limit is listed", "if st.st_mtime < cutoff:", "if st.st_mtime <= cutoff:"),
    ("the limit is 25 hours", "cutoff = now - age_h * 3600.0", "cutoff = now - (age_h + 1) * 3600.0"),
    ("a future file is listed", "if st.st_mtime < cutoff:", "if abs(now - st.st_mtime) > age_h * 3600.0:"),
    (
        "the extra roots are not scanned",
        'for label, top in [("root", root)] + [("extra%d" % (i + 1), x) for i, x in enumerate(extras)]:',
        'for label, top in [("root", root)]:',
    ),
    ("linked directories are followed", "os.walk(top, followlinks=False, onerror=onerr)", "os.walk(top, followlinks=True, onerror=onerr)"),
    ("newest first", "found.sort(key=lambda r: (r[0], r[2]))", "found.sort(key=lambda r: (-r[0], r[2]))"),
    ("the byte total counts only the printed lines", "sum(r[1] for r in found)", "sum(r[1] for r in found[:max_lines])"),
    ("the line cap is ignored", "for mt, size, name in found[:max_lines]:", "for mt, size, name in found:"),
    ("found files give exit 0", "return 1 if found else 0", "return 0"),
    (
        "a file is deleted",
        "                found.append((st.st_mtime, st.st_size, label",
        "                os.remove(p); found.append((st.st_mtime, st.st_size, label",
    ),
]


def run(d):
    p = subprocess.run([sys.executable, "-I", os.path.join(d, "test_audio_sweep_check.py")], capture_output=True, text=True, timeout=300)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_as_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        os.makedirs(base)
        for f in ("audio_sweep_check.py", "test_audio_sweep_check.py"):
            shutil.copy(os.path.join(HERE, f), base)
        rc, _ = run(base)
        print("baseline exit %d (must be 0)" % rc)
        if rc != 0:
            return 1
        for name, a, b in M:
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, "audio_sweep_check.py")
            s = open(p, newline="", encoding="utf-8").read()
            if s.count(a) != 1:
                print("BROKEN mutation (pattern found %d times): %s" % (s.count(a), name))
                bad += 1
                continue
            open(p, "w", newline="", encoding="utf-8").write(s.replace(a, b))
            rc, fl = run(d)
            print(("RED  " if rc == 1 else "NOT RED (exit %d) " % rc) + name + ("  <- " + fl[0][:110] if fl else ""))
            bad += rc != 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
