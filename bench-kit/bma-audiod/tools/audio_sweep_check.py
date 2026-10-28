#!/usr/bin/env python3
"""audio_sweep_check: lists the audio files (*.wav, *.flac, *.opus) older than --age-h under the data root and every --extra directory.
Read only: it never deletes, moves or touches a file. Standard library only.

    python -I audio_sweep_check.py --root DIR [--extra DIR ...] [--age-h 24] [--max-lines 50]

The pipeline keeps no audio by default and clip audio expires after 24 h, so any file listed here is scratch or an old clip that was not swept.
Output: `N audio files older than H h, B bytes` (+ `, U unreadable` when entries could not be read), then the oldest first, one line each:
`<age h> h  <bytes> B  <root|extraN>:<relative path>`, at most --max-lines of them and then `... M more`.
A file is listed when its age is greater than --age-h (exactly --age-h is not listed); a future mtime is never listed.
Exit code: 0 none found, 1 some found, 2 usage error."""
import argparse, os, sys, time

EXTS = (".wav", ".flac", ".opus")


def scan(label, top, cutoff, now, found, stats):
    def onerr(e):
        stats["unreadable"] += 1
    for dp, _, fs in os.walk(top, followlinks=False, onerror=onerr):
        for f in fs:
            if os.path.splitext(f)[1].lower() not in EXTS:
                continue
            p = os.path.join(dp, f)
            try:
                st = os.stat(p, follow_symlinks=False)
            except OSError:
                stats["unreadable"] += 1
                continue
            if st.st_mtime < cutoff:
                found.append((st.st_mtime, st.st_size, label + ":" + os.path.relpath(p, top).replace("\\", "/")))


def check(root, extras, age_h, now):
    """-> (found [(mtime, size, label:relpath)] oldest first, unreadable count)"""
    found, stats = [], {"unreadable": 0}
    cutoff = now - age_h * 3600.0
    for label, top in [("root", root)] + [("extra%d" % (i + 1), x) for i, x in enumerate(extras)]:
        scan(label, top, cutoff, now, found, stats)
    found.sort(key=lambda r: (r[0], r[2]))
    return found, stats["unreadable"]


def render(found, unreadable, age_h, now, max_lines):
    hh = ("%g" % age_h)
    head = "%d audio files older than %s h, %d bytes" % (len(found), hh, sum(r[1] for r in found))
    if unreadable:
        head += ", %d unreadable" % unreadable
    out = [head]
    for mt, size, name in found[:max_lines]:
        out.append("%.1f h  %d B  %s" % ((now - mt) / 3600.0, size, name))
    if len(found) > max_lines:
        out.append("... %d more" % (len(found) - max_lines))
    return "\n".join(out) + "\n"


def main(argv=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True, help="pipeline data directory")
    ap.add_argument("--extra", action="append", default=[], help="another directory to scan (repeatable)")
    ap.add_argument("--age-h", type=float, default=24.0, help="list files older than this many hours (default 24)")
    ap.add_argument("--max-lines", type=int, default=50, help="file lines to print (default 50)")
    a = ap.parse_args(argv)
    if a.age_h < 0 or a.max_lines < 0:
        sys.stderr.write("--age-h and --max-lines must not be negative\n")
        return 2
    for d in [a.root] + a.extra:
        if not os.path.isdir(d):
            sys.stderr.write("not a directory: %s\n" % ("--root" if d == a.root else "--extra"))
            return 2
    now = time.time() if now is None else now
    found, unreadable = check(a.root, a.extra, a.age_h, now)
    sys.stdout.write(render(found, unreadable, a.age_h, now, a.max_lines))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
