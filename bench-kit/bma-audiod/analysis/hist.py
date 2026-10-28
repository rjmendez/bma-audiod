"""Bounded, never-raising history writers for the analyzer (stdlib only).

RotatingJsonl: append-only JSONL, size-capped, ONE previous generation (<name>.1), so the disk bound is 2 x cap.
Rotation is os.replace; if that fails (Windows or a 9p mount while a reader holds the file) it falls back to
copy + truncate, and if that fails too the file keeps growing and rotation is retried after another cap.
Nothing here raises: failures are counted (write_errors, rotate_fallbacks) and surface in status.json meta.
AtomicJson: whole-file JSON written through tmp + os.replace; an OSError leaves the previous file intact and is counted.
"""

import json, os, shutil, threading


def dumps(rec):
    return json.dumps(rec, separators=(",", ":"))


class RotatingJsonl:
    def __init__(self, path, cap_bytes, replace=os.replace):
        self.path, self.cap, self.replace = path, int(cap_bytes), replace
        self.f = None
        self.size = 0
        self.retry_at = None
        self.tail_checked = False
        self.torn_repaired = 0
        self.write_errors = 0
        self.rotate_fallbacks = 0
        self.lock = threading.Lock()

    def _open(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.f = open(self.path, "ab", buffering=0)
        self.size = os.path.getsize(self.path)
        if not self.tail_checked:
            self.tail_checked = True
            if self.size > 0:
                with open(self.path, "rb") as r:  # a torn last line (no newline) would swallow the next record
                    r.seek(-1, 2)
                    torn = r.read(1) != b"\n"
                if torn:
                    self.f.write(b"\n")
                    self.size += 1
                    self.torn_repaired += 1

    def _close(self):
        try:
            if self.f:
                self.f.close()
        except OSError:
            pass
        self.f = None

    def write(self, rec):
        """Append one record; True when it reached the file."""
        data = (dumps(rec) + "\n").encode("utf-8")
        with self.lock:
            try:
                if self.f is None:
                    self._open()
                limit = self.cap if self.retry_at is None else self.retry_at
                if self.size + len(data) > limit and self.size > 0:
                    self._rotate()
                self.f.write(data)
                self.size += len(data)
                return True
            except (OSError, ValueError, TypeError):
                self.write_errors += 1
                self._close()
                return False

    def _rotate(self):
        self._close()
        prev = self.path + ".1"
        try:
            self.replace(self.path, prev)
            self.retry_at = None
        except OSError:
            self.rotate_fallbacks += 1
            try:
                shutil.copyfile(self.path, prev)
                with open(self.path, "r+b") as f:
                    f.truncate(0)
                self.retry_at = None
            except OSError:
                self.write_errors += 1
                self.retry_at = os.path.getsize(self.path) + self.cap  # keep appending, retry after another cap
        self._open()
        if self.retry_at is None:
            self.size = os.path.getsize(self.path)

    def close(self):
        with self.lock:
            self._close()


class AtomicJson:
    def __init__(self, path, replace=os.replace):
        self.path, self.replace, self.errors = path, replace, 0

    def write(self, obj):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(obj, f, separators=(",", ":"))
            self.replace(tmp, self.path)
            return True
        except (OSError, TypeError, ValueError):
            self.errors += 1
            return False

    def read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
