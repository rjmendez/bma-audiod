#!/usr/bin/env python3
"""Tests for bma_debug_rx.Out (the collector's JSONL writer and its 64 MB rotation).
Regression: on Windows os.replace() of a file that another process holds open raises PermissionError, and the cable
sink's tailer always holds board_debug.jsonl open, so the first rotation (at 64 MB, 2026-10-25 12:16) killed the collector's
writer and no board debug data was recorded for three hours. Rotation must fall back to copy + truncate and never stop."""

import json, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bma_debug_rx as R

n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + detail + "]"))
    if not cond:
        fails.append(name)


def lines(path):
    with open(path, "rb") as f:
        return [json.loads(x) for x in f.read().splitlines() if x]


def rec(i):
    return {"type": "meter", "i": i, "pad": "x" * (40 - len(str(i)))}  # every line has exactly the same length


L = len(json.dumps(rec(0), separators=(",", ":"))) + 1  # bytes per line including the newline
CAP = 10 * L - 1  # the 10th write passes the cap and rotates


class FailingReplace:
    """Recording stand-in for os.replace that fails the way Windows does while a reader holds the file open."""

    def __init__(self):
        self.calls = []

    def __call__(self, src, dst):
        self.calls.append((src, dst))
        raise PermissionError(13, "The process cannot access the file because it is being used by another process", src)


class RecordingReplace:
    def __init__(self):
        self.calls = []

    def __call__(self, src, dst):
        self.calls.append((src, dst))
        os.replace(src, dst)


def fresh():
    d = tempfile.mkdtemp(prefix="bma_rx_test_")
    return d, os.path.join(d, "board_debug.jsonl")


def default_src_port():
    """The --src-port default declared in bma_debug_rx.py main()."""
    import re

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bma_debug_rx.py"), encoding="utf8") as fh:
        m = re.search(r'--src-port", type=int, default=(\d+)', fh.read())
    return int(m.group(1)) if m else None


def run():
    # 1. no path: a no-op
    o = R.Out(None)
    o.write(rec(0))
    check("no --out path: write is a no-op and never raises", o.f is None and o.rotate_fallbacks == 0)

    # 2. normal rotation by rename (nothing holds the file)
    d, p = fresh()
    rr = RecordingReplace()
    o = R.Out(p, cap=CAP, replace=rr)
    for i in range(15):
        o.write(rec(i))
    o.f.close()
    old, new = lines(p + ".1"), lines(p)
    check("rename rotation: the full file moved to FILE.1 through the rename (one call, to FILE.1)", rr.calls == [(p, p + ".1")], str(rr.calls))
    check("rename rotation: no fallback was needed", o.rotate_fallbacks == 0)
    check(
        "rename rotation: every record is in exactly one of the two files, in order",
        [r["i"] for r in old + new] == list(range(15)),
        str([r["i"] for r in old + new]),
    )
    check(
        "rename rotation: FILE.1 holds records 0..9 (the 10th write passed the cap) and the new file holds 10..14",
        [r["i"] for r in old] == list(range(10)) and [r["i"] for r in new] == list(range(10, 15)),
        str(([r["i"] for r in old], [r["i"] for r in new])),
    )
    shutil.rmtree(d, ignore_errors=True)

    # 3. the Windows failure: rename refused while a reader holds the file; copy + truncate, collection continues
    d, p = fresh()
    fr = FailingReplace()
    o = R.Out(p, cap=CAP, replace=fr)
    reader = open(p, "rb")  # the sink's tailer: opened once, kept open
    for i in range(9):
        o.write(rec(i))
    reader.seek(0, 2)
    pos = reader.tell()
    o.write(rec(9))  # the 10th write passes the cap and triggers the rotation
    check("rename refused: the fallback ran exactly once", o.rotate_fallbacks == 1 and len(fr.calls) == 1, str((o.rotate_fallbacks, fr.calls)))
    check("rename refused: FILE.1 holds the full old content (records 0..9), nothing lost", [r["i"] for r in lines(p + ".1")] == list(range(10)))
    check("rename refused: the live file was truncated in place", os.path.getsize(p) == 0, str(os.path.getsize(p)))
    check("rename refused: a tailer holding the file sees the size drop below its position (it restarts at 0)", os.path.getsize(p) < pos)
    for i in range(10, 15):
        o.write(rec(i))
    o.f.close()
    reader.close()
    check(
        "rename refused: writing continued into the same path afterwards (the live file holds records 10..14, in order)",
        [r["i"] for r in lines(p)] == list(range(10, 15)),
        str([r["i"] for r in lines(p)]),
    )
    allrecs = [r["i"] for r in lines(p + ".1")] + [r["i"] for r in lines(p)]
    check("rename refused: across FILE.1 and the file every record appears exactly once", allrecs == list(range(15)), str(allrecs))
    shutil.rmtree(d, ignore_errors=True)

    # 4. real Windows behaviour: the genuine os.replace is refused while a handle is open, and Out still rotates
    if os.name == "nt":
        d, p = fresh()
        o = R.Out(p, cap=CAP)  # default replace = os.replace
        reader = open(p, "rb")
        try:
            os.replace(p + "", p + ".probe")
            refused = False
        except PermissionError:
            refused = True
        check("windows: renaming a file another handle holds open IS refused (the real failure this guards against)", refused)
        for i in range(15):
            o.write(rec(i))
        o.f.close()
        reader.close()
        allrecs = ([r["i"] for r in lines(p + ".1")] if os.path.exists(p + ".1") else []) + [r["i"] for r in lines(p)]
        check(
            "windows: with a reader holding the file, the real rotation fell back to copy + truncate and lost no record",
            o.rotate_fallbacks == 1 and allrecs == list(range(15)),
            str((o.rotate_fallbacks, allrecs)),
        )
        shutil.rmtree(d, ignore_errors=True)
    else:
        print("SKIP windows real-handle check (not Windows); the injected-failure checks above cover the logic")

    # 5. total failure: even the copy fails -> keep the file, keep writing, do not retry on every record
    d, p = fresh()
    fr = FailingReplace()
    o = R.Out(p, cap=CAP, replace=fr)
    os.mkdir(p + ".1")  # FILE.1 is a directory: copyfile cannot succeed
    for i in range(25):
        o.write(rec(i))
    o.f.close()
    check("total failure: every record is still in the live file (collection never stopped)", [r["i"] for r in lines(p)] == list(range(25)))
    check(
        "total failure: the rotation is not retried on every record: exactly 2 attempts in 25 records (at the 10th and again after another cap, the 20th)",
        len(fr.calls) == 2,
        str(len(fr.calls)),
    )
    os.rmdir(p + ".1")
    o2 = R.Out(p, cap=CAP, replace=RecordingReplace())  # positive twin: once FILE.1 is usable again, rotation works
    o2.write(rec(100))
    o2.f.close()
    check(
        "total failure twin: after the obstacle is removed a rotation succeeds and moves the big file to FILE.1",
        os.path.exists(p + ".1") and len(lines(p + ".1")) == 26 and lines(p) == [],
        str((os.path.exists(p + ".1"), len(lines(p)))),
    )
    shutil.rmtree(d, ignore_errors=True)

    # 6. fixed UDP source port (the board daemon never expires a lease while its media path is off, so a new ephemeral
    #    port per restart used up one of its 8 subscriber slots for good)
    import socket, subprocess

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", 0))
    port = probe.getsockname()[1]
    probe.close()
    a = R.open_sock(port)
    p1 = a.getsockname()[1]
    a.close()
    b = R.open_sock(port)  # a restarted collector
    p2 = b.getsockname()[1]
    check(
        "fixed source port: a restarted collector uses the same UDP source port, so the board daemon sees the same subscriber",
        p1 == p2 == port,
        str((p1, p2, port)),
    )
    c = R.open_sock(port)  # the port is taken now (b still open): a second collector
    c.sendto(b"", ("127.0.0.1", 9))  # an unbound UDP socket gets its ephemeral port at the first send (Windows raises on getsockname before)
    check(
        "fixed source port twin: with the port taken a second collector still gets a working socket, on a different port, instead of failing",
        c.getsockname()[1] not in (0, port),
        str(c.getsockname()),
    )
    check(
        "sockets have the 0.5 s receive timeout the main loop relies on", b.gettimeout() == 0.5 and c.gettimeout() == 0.5, str((b.gettimeout(), c.gettimeout()))
    )
    b.close()
    c.close()
    d = R.open_sock(0)
    d.sendto(b"", ("127.0.0.1", 9))
    check("port 0 asks for an ephemeral port", d.getsockname()[1] != 0, str(d.getsockname()))
    d.close()
    hp = subprocess.run(
        [sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bma_debug_rx.py"), "--help"], capture_output=True, text=True
    ).stdout
    check("the CLI offers --src-port, and its default is the fixed port 48010", "--src-port" in hp and default_src_port() == 48010, str(default_src_port()))
    # 7. the wiring: the real collector, started with --src-port, sends its BMAS subscription FROM that port (a fake board listens)
    fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fake.bind(("127.0.0.1", 0))
    fake.settimeout(8)
    fake_port = fake.getsockname()[1]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("0.0.0.0", 0))
    want = probe.getsockname()[1]
    probe.close()
    dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bma_debug_rx.py")
    proc = subprocess.Popen(
        [sys.executable, "-I", dbg, f"127.0.0.1:{fake_port}", "--secs", "4", "--quiet", "--src-port", str(want)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        data, addr = fake.recvfrom(256)
    except OSError as e:
        data, addr = b"", ("", -1)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        fake.close()
    check(
        "wiring: the collector's BMAS subscription arrives at the board from the --src-port it was started with",
        data[:4] == b"BMAS" and addr[1] == want,
        str((data[:8], addr, want)),
    )


def run_board_spec():
    """BMA_BOARD (MAC or IP) replaces the hard-coded board MAC: parsing, neighbour-table lookup, and the real CLI."""
    mac = "00-00-5e-00-53-01"
    check("board_spec: upper-case dashed MAC -> normalised", R.board_spec("00-00-5E-00-53-01") == ("mac", mac), str(R.board_spec("00-00-5E-00-53-01")))
    check("board_spec: colon MAC -> the same normal form", R.board_spec("00:00:5e:00:53:01") == ("mac", mac))
    check(
        "board_spec: IPv4 address -> ip (positive twin of the MAC cases)", R.board_spec(" 192.0.2.7 ") == ("ip", "192.0.2.7"), str(R.board_spec(" 192.0.2.7 "))
    )
    check("board_spec: mixed separators are not a MAC", R.board_spec("00-00:5e-00:53-01") == ("ip", "00-00:5e-00:53-01"))
    check("board_spec: unset / empty / blank -> None", R.board_spec(None) is None and R.board_spec("") is None and R.board_spec("  ") is None)
    arp = "\n".join(
        [
            "Interface: 192.0.2.1 --- 0x5",
            "  Internet Address      Physical Address      Type",
            "  192.0.2.7             00-00-5e-00-53-01     dynamic",
            "  192.0.2.9             00-00-5e-00-53-09     dynamic",
            "  192.0.2.77            00-00-5E-00-53-01     static",
            "",
        ]
    )
    check(
        "arp_candidates: exactly the addresses mapped to the MAC (stale and upper-case rows included)",
        R.arp_candidates(mac, arp) == ["192.0.2.7", "192.0.2.77"],
        str(R.arp_candidates(mac, arp)),
    )
    check("arp_candidates: another MAC finds only its own row (positive twin)", R.arp_candidates("00-00-5e-00-53-09", arp) == ["192.0.2.9"])
    check("arp_candidates: a MAC that is not in the table -> []", R.arp_candidates("00-00-5e-00-53-55", arp) == [])
    dbg = os.path.join(os.path.dirname(os.path.abspath(R.__file__)), "bma_debug_rx.py")
    base_env = {k: v for k, v in os.environ.items() if k != "BMA_BOARD"}
    r = subprocess.run(
        [sys.executable, "-I", dbg, "auto", "--secs", "1", "--quiet", "--history-dir", ""], env=base_env, capture_output=True, text=True, timeout=60
    )
    check(
        "CLI: 'auto' without BMA_BOARD fails loudly (exit 2, names the variable)",
        r.returncode == 2 and "auto needs BMA_BOARD" in r.stderr,
        f"{r.returncode} {r.stderr[-200:]}",
    )
    r = subprocess.run(
        [sys.executable, "-I", dbg, "auto", "--secs", "1", "--quiet", "--history-dir", "", "--src-port", "0"],
        env=dict(base_env, BMA_BOARD="192.0.2.1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(
        "CLI: 'auto' with BMA_BOARD=<ip> collects from that address on the board port (exit 1 = nothing received from a documentation address)",
        r.returncode == 1 and "collecting from 192.0.2.1:48000" in r.stdout and "received: {}" in r.stdout,
        f"{r.returncode} {r.stdout[-200:]} {r.stderr[-200:]}",
    )
    r = subprocess.run(
        [sys.executable, "-I", dbg, "auto", "--secs", "1", "--quiet", "--history-dir", "", "--src-port", "0"],
        env=dict(base_env, BMA_BOARD="00-00-5E-00-53-01"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(
        "CLI: 'auto' with BMA_BOARD=<mac> that no neighbour has -> keeps looking (exit 1 = nothing received, 'board not found; retrying')",
        r.returncode == 1 and "board not found; retrying" in r.stdout and "collecting from" not in r.stdout,
        f"{r.returncode} {r.stdout[-200:]} {r.stderr[-200:]}",
    )


run_board_spec()

run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
