#!/usr/bin/env python3
"""Mutation check for tools/loss_log.py and tools/loss_report.py: each mutation edits one behaviour in a COPY of tools/ + pc/history_log.py,
runs test_loss_log.py against it and requires a nonzero exit. The unmutated copy must exit 0 first.

    python -I mutation_loss_log.py [name-substring]        Exit 0 only if the baseline is green and every mutation was caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
LOG, REP = "loss_log.py", "loss_report.py"
M = [
    (LOG, "counters are cumulative, not deltas", "out[c] = cur[c] - p_cur[c]", "out[c] = cur[c]"),
    (LOG, "a counter going backwards is not a restart", "any(cur[c] < p_cur[c] for c in cur)", "False"),
    (LOG, "uptime going backwards is not a restart", " or (up is not None and p_up is not None and up < p_up)", ""),
    (LOG, "the restart baseline is not replaced", "self.prev[name] = (now, cur, up)\n", "if name not in self.prev: self.prev[name] = (now, cur, up)\n"),
    (
        LOG,
        "a stale tick drops the baseline",
        'out["reason"] = "stale"\n            return out',
        'out["reason"] = "stale"\n            self.prev.pop(name, None)\n            return out',
    ),
    (LOG, "age equal to --stale-s is stale", 'if out["age_s"] > self.stale_s:', 'if out["age_s"] >= self.stale_s:'),
    (LOG, "span_s is the nominal 30 s", 'out["span_s"] = round(now - t0, 3)', 'out["span_s"] = 30.0'),
    (LOG, "a missing file is reported unreadable", "except FileNotFoundError:", "except ZeroDivisionError:"),
    (
        LOG,
        "a deeply nested document escapes the parser guard",
        "except (ValueError, UnicodeDecodeError, RecursionError):",
        "except (ValueError, UnicodeDecodeError):",
    ),
    (LOG, "a bool counts as a counter", " and not isinstance(v, bool) and v >= 0 else None\n\n\ndef _gauge", " and v >= 0 else None\n\n\ndef _gauge"),
    (LOG, "a float counts as a counter", "return v if isinstance(v, int) and not", "return v if isinstance(v, (int, float)) and not"),
    (LOG, "a negative counter is accepted", "and v >= 0 else None\n\n\ndef _gauge", "else None\n\n\ndef _gauge"),
    (LOG, "the 1 MiB cap is not applied", "if len(raw) > MAX_STATUS_BYTES:", "if False:"),
    (LOG, "a future-dated file has a negative age", "max(0.0, now - mtime)", "now - mtime"),
    (
        LOG,
        "the record leaks the root path",
        'rec = {"k": "loss", "v": 1, "t": round(now, 3), "restart": False}',
        'rec = {"k": "loss", "v": 1, "t": round(now, 3), "restart": False, "root": self.root}',
    ),
    (LOG, "the top-level restart is never set", 'rec["restart"] = any(rec[n]["restart"] for n in SOURCES)', 'rec["restart"] = False'),
    (LOG, "no stall reset (catch-up burst)", "            nxt = mono()\n", "            pass\n"),
    (LOG, "the tick error line carries the message", "type(ex).__name__", "ex"),
    (LOG, "a failed tick stops the loop", "        except Exception as ex:", "        except ZeroDivisionError as ex:"),
    (
        LOG,
        "the sink reads the bridge counter",
        '"sink": ("sink_status.json", {"datagrams": ("datagrams",), "rx_errors": ("rx_errors",)}',
        '"sink": ("sink_status.json", {"datagrams": ("datagrams",), "rx_errors": ("bad",)}',
    ),
    (REP, "the worst window is the minimum", 'g > e["worst"]["gaps"]', 'g < e["worst"]["gaps"]'),
    (REP, "other record versions are accepted", 'r.get("v") == 1 and', "True and"),
    (REP, ".1 is read after the file", 'for p in (path + ".1", path):', 'for p in (path, path + ".1"):'),
    (REP, "gap % is gaps per 100 datagrams plus gaps", '(100.0 * e["gaps"] / e["datagrams"])', '(100.0 * e["gaps"] / (e["datagrams"] + e["gaps"]))'),
    (LOG, "the default --stale-s is the old 20 s", "STALE_S = 60.0 ", "STALE_S = 20.0 "),
    (REP, "loss % is not weighted by datagrams", 'e["lw"] += lp * s["datagrams"]', 'e["lw"] += lp * 1000'),
    (REP, "loss % is the gap events %", 'e["loss_pct"] = (e["lw"] / e["ld"]) if e["ld"] else None', 'e["loss_pct"] = e["gap_pct"]'),
    (REP, "null records are summed as zero and not counted as nulls", 'e["nulls"] += 1\n            continue', "continue"),
]


def run(d):
    p = subprocess.run([sys.executable, "-I", os.path.join(d, "tools", "test_loss_log.py")], capture_output=True, text=True, timeout=600)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2] or p.stderr.splitlines()[-1:]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="loss_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        os.makedirs(os.path.join(base, "pc"))
        shutil.copytree(HERE, os.path.join(base, "tools"), ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(HERE, "..", "pc", "history_log.py"), os.path.join(base, "pc"))
        rc, _ = run(base)
        print(f"baseline exit {rc} (must be 0)")
        if rc != 0:
            return 1
        for i, (f, name, a, b) in enumerate(M):
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m%d" % i)
            shutil.copytree(base, d)
            p = os.path.join(d, "tools", f)
            s = open(p, encoding="utf-8", newline="").read()
            if s.count(a) != 1:
                print(f"BAD   {name}: pattern found {s.count(a)} times")
                bad += 1
                continue
            open(p, "w", encoding="utf-8", newline="").write(s.replace(a, b))
            rc, fl = run(d)
            print(("RED   " if rc != 0 else "GREEN ") + name + ("  " + str(fl[:1]) if rc != 0 else "  <- NOT CAUGHT"))
            bad += rc == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{bad} mutation(s) not caught")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
