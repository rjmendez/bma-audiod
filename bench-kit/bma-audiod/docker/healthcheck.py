#!/usr/bin/env python3
"""Container healthchecks, standard library only.
  healthcheck.py fresh PATH SECONDS   PATH (relative to $BMA_DATA_DIR) was modified within SECONDS
  healthcheck.py udp PORT             some socket in this network namespace is bound to UDP PORT
  healthcheck.py http PORT            GET http://127.0.0.1:PORT/ answers 200
Exit 0 = healthy, 1 = not (the reason is on stderr), 2 = bad usage."""

import os, sys, time, urllib.request


def fresh(rel, secs):
    p = os.path.join(os.environ.get("BMA_DATA_DIR", "/data"), rel)
    try:
        age = time.time() - os.stat(p).st_mtime
    except OSError as e:
        return f"{p}: {e.strerror}"
    return None if age <= secs else f"{p} is {age:.0f}s old (limit {secs:g}s)"


def udp(port, tables=("/proc/net/udp", "/proc/net/udp6")):
    for tbl in tables:
        try:
            with open(tbl) as f:
                next(f)
                for line in f:
                    if int(line.split()[1].rsplit(":", 1)[1], 16) == port:
                        return None
        except OSError:
            continue
    return f"nothing bound to udp/{port}"


def http(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            return None if r.status == 200 else f"status {r.status}"
    except Exception as e:  # noqa: BLE001 - any failure is "unhealthy"; the text says why
        return f"{type(e).__name__}: {e}"


def main(argv):
    try:
        mode = argv[1]
        err = {"fresh": lambda: fresh(argv[2], float(argv[3])), "udp": lambda: udp(int(argv[2])), "http": lambda: http(int(argv[2]))}[mode]()
    except (IndexError, KeyError, ValueError):
        print(__doc__, file=sys.stderr)
        return 2
    if err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
