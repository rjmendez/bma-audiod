#!/usr/bin/env python3
"""Tests of tools/spectrum_relay.py: a source dashboard (real bma_ui Server, login, rows over real UDP), the relay in a thread, a second SpectrumFeed on a
loopback UDP port. Every row the source serves must arrive in the second feed with identical bins and flags, per beam and row index.
    python test_spectrum_relay.py     (PASS/FAIL lines, 'N checks, M failure(s)', exit 1 on a failure)"""
import base64, os, socket, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for d in ("ui", "analysis", "contracts", os.path.join("ui-next", "tools")):
    sys.path.insert(0, os.path.join(ROOT, d))
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402
import ref_binary as RB  # noqa: E402
import spectrum_relay as SR  # noqa: E402

n, fails = 0, []


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [%s]" % detail), flush=True)
    if not ok:
        fails.append(name)


TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
ROWS, GATED_ROW = 20, 7


def bins_of(k, beam, gated):
    return [90] * 46 if gated else [(7 * k + 3 * beam + i) % 200 for i in range(46)]


# ---- pure functions
check("build_row equals the contract's reference encoder byte for byte", SR.build_row(2, 5, 1234, 1792951400100000, bins_of(1, 5, False)) == RB.build_spectrum(0, 2, 5, 1234, 1792951400100000, bins_of(1, 5, False)))
body = {"tier": "standard", "n_bins": 46, "state": "live", "rows": [{"idx": 9, "t_s": 1792951400.1, "flags": 0, "bins": base64.b64encode(bytes(bins_of(9, 3, False))).decode()}]}
check("datagrams: one live row becomes one BMAS datagram with its idx, flags, beam and time (microseconds)", SR.datagrams(body, 3) == [RB.build_spectrum(0, 0, 3, 9, 1792951400100000, bins_of(9, 3, False))])
check("datagrams: positive twin above; auth_required, disabled, a detail tier and a wrong bin count give nothing",
      [SR.datagrams(dict(body, state=s), 3) for s in ("auth_required", "disabled")] == [[], []] and SR.datagrams(dict(body, tier="detail"), 3) == [] and SR.datagrams(dict(body, n_bins=100), 3) == []
      and SR.datagrams(dict(body, rows=[dict(body["rows"][0], bins=base64.b64encode(b"\x01\x02").decode())]), 3) == [])
check("main: a non-loopback --to is refused (exit 2); positive twin: the loopback default parses", SR.main(["--source", "http://x", "--token-file", "x", "--to", "192.0.2.1:1"]) == 2)

# ---- end to end
tmp = tempfile.mkdtemp(prefix="relay_")
pr = os.path.join(tmp, "p")
os.makedirs(os.path.join(pr, "analysis"))
src_feed = U.SpectrumFeed(enabled=True)
src_port = src_feed.start(0)
app = U.App(pr, os.path.join(tmp, "r"), os.path.join(tmp, "ru"), U.Board("http://127.0.0.1:9", os.path.join(tmp, "nt"), enabled=False), log=lambda m: None, spectrum=src_feed)
srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), auth=A.Authenticator(TOKEN, log=lambda m: None))
port = srv.server_address[1]
srv.allowed_hosts = U.allowed_hosts_for(port, [])
threading.Thread(target=srv.serve_forever, daemon=True).start()
dst = U.SpectrumFeed(enabled=True)
dst_port = dst.start(0)
tf = os.path.join(tmp, "tok")
open(tf, "wb").write(TOKEN.encode())
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
t0 = time.time()
for k in range(ROWS):
    for b in range(9):
        tx.sendto(RB.build_spectrum(0, 1 if k == GATED_ROW else 0, b, k, int((t0 + 0.1 * k) * 1e6), bins_of(k, b, k == GATED_ROW)), ("127.0.0.1", src_port))
    time.sleep(0.1)
rc = []
th = threading.Thread(target=lambda: rc.append(SR.main(["--source", "http://127.0.0.1:%d" % port, "--token-file", tf, "--to", "127.0.0.1:%d" % dst_port, "--secs", "8"])))
th.start()
th.join(30)
time.sleep(0.5)
check("relay ran and stopped cleanly", rc == [0], str(rc))
ok_all, detail, gated = True, [], {}
for b in range(9):
    s_rows = src_feed.snapshot("standard", b, 0, 0, False, 4, 600)["rows"]
    d_rows = dst.snapshot("standard", b, 0, 0, False, 4, 600)["rows"]        # each feed numbers its own rows; the order is the row order
    same = [(a["bins"], a["flags"], a["gated"]) == (c["bins"], c["flags"], c["gated"]) for a, c in zip(s_rows, d_rows)]
    if len(s_rows) != ROWS or len(d_rows) != ROWS or not all(same):
        ok_all = False
        detail.append((b, len(s_rows), len(d_rows), same.count(True)))
    gated[b] = ([i for i, r in enumerate(s_rows) if r["gated"]], [i for i, r in enumerate(d_rows) if r["gated"]])
check("every beam: all %d rows arrive in order with identical bins, flags and gated bit (9 beams x %d rows)" % (ROWS, ROWS), ok_all, str(detail))
check("the gated row is row %d on both sides for every beam, and the only one (positive twin of the identical-rows check)" % GATED_ROW, all(v == ([GATED_ROW], [GATED_ROW]) for v in gated.values()), str(gated))
srv.shutdown()
src_feed.close()
dst.close()
print("\n%d checks, %d failure(s)" % (n, len(fails)))
sys.exit(1 if fails else 0)
