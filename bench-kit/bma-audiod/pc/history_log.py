#!/usr/bin/env python3
"""Shared writer for the pipeline's metadata HISTORY files (contract: run-history, contracts/specs/run-history.md).

RotatingJsonl: append-only JSONL, one compact JSON object per line, size-capped with ONE backup (FILE.1), so the disk use
is bounded by 2 x cap. It never raises into the caller (history is best effort and must not stop a producer) and it counts
what went wrong instead. Rotation order: os.replace; if that fails (Windows refuses to rename a file another process has
open: the cable sink's tailer, the UI, a WSL reader on a mounted drive) copy to FILE.1 and truncate in place; if that fails too keep
appending and try again after another `cap` bytes. Same pattern as tools/bma_debug_rx.py Out._rotate.

Metadata only: nothing in here ever carries audio. Standard library only (the sink venv has nothing else).
"""
import json, os, shutil, threading

DATA_DIR = os.environ.get("BMA_DATA_DIR") or "data"      # root of analysis/, history/, logs/, reports/
HISTORY_DIR = os.path.join(DATA_DIR, "history")


def dumps(rec):
    return json.dumps(rec, separators=(",", ":"), allow_nan=False)


class RotatingJsonl:
    def __init__(self, path, cap_bytes, replace=os.replace, check_every=1):
        self.path, self.cap, self.replace, self.check_every = path, int(cap_bytes), replace, max(1, int(check_every))
        self.limit = self.cap               # rotate once the file is larger than this
        self.since = 0
        self.tail_checked = False           # the first append after startup isolates a torn last line
        self.torn_repaired = 0
        self.lock = threading.Lock()
        self.writes = 0
        self.write_errors = 0               # records that could not be written (dropped, counted)
        self.rotations = 0                  # rotations by rename
        self.rotate_fallbacks = 0           # rotations that had to copy + truncate (or could not rotate at all)

    def write(self, rec):
        """Append one record (a dict). Returns True when the line reached the file."""
        try:
            line = (dumps(rec) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            self.write_errors += 1
            return False
        return self._append(line)

    def write_line(self, line):
        """Append one already-serialised JSON line (bytes, no newline)."""
        return self._append(bytes(line) + b"\n")

    def _append(self, line):
        with self.lock:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                if not self.tail_checked:
                    line = self._isolate_torn_tail(line)
                with open(self.path, "ab") as f:                # opened per write: nothing stays open to block a rename
                    f.write(line)
                    size = f.tell()
                self.tail_checked = True
                self.writes += 1
                self.since += 1
                if self.since >= self.check_every:
                    self.since = 0
                    if size > self.limit:
                        self._rotate()
                return True
            except OSError:
                self.write_errors += 1
                return False

    def _isolate_torn_tail(self, line):
        """A process that died mid-line leaves a file without a final newline; appending directly would glue the next record
        onto the fragment and lose it. Prefix a newline once so the fragment stays an isolated (skipped) line."""
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                if f.tell() > 0:
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        self.torn_repaired += 1
                        return b"\n" + line
        except OSError:
            pass                                                    # absent file: nothing to repair
        return line

    def _rotate(self):
        try:
            self.replace(self.path, self.path + ".1")
            self.rotations += 1
            self.limit = self.cap
            return
        except OSError:
            self.rotate_fallbacks += 1
        try:
            shutil.copyfile(self.path, self.path + ".1")
            with open(self.path, "r+b") as t:
                t.truncate(0)
            self.limit = self.cap
        except OSError:
            try:
                self.limit = os.path.getsize(self.path) + self.cap      # keep appending, retry after another cap
            except OSError:
                self.limit = self.cap

    def counters(self):
        return {"writes": self.writes, "write_errors": self.write_errors, "rotations": self.rotations,
                "rotate_fallbacks": self.rotate_fallbacks}


def load_tail(path, n=1, block=1 << 16):
    """The last n complete, parseable records of a JSONL file (oldest first); [] if the file is absent or empty. A leading
    fragment (read started mid-line) and a trailing line without newline (mid-write) are dropped."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos, data = f.tell(), b""
            while pos > 0 and data.count(b"\n") <= n + 1:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
    except OSError:
        return []
    lines = data.split(b"\n")
    if pos > 0:
        lines = lines[1:]
    if lines and lines[-1] != b"":
        lines = lines[:-1]
    out = []
    for l in lines:
        if not l.strip():
            continue
        try:
            out.append(json.loads(l.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            pass
    return out[-n:]
