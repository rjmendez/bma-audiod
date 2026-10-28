#!/usr/bin/env python3
"""Mutation run for the live WebSocket (ui/ws.py, the /api/v2/live route and SpectrumFeed.live_rows): each mutant changes one rule (the file is restored
afterwards) and test_ui_ws.py must go red. Exit 0 only when every mutant is KILLED.

    python mutation_ws.py [name-substring]"""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
SUITE = "test_ui_ws.py"
WS, UI = "ws.py", "bma_ui.py"

MUTANTS = [
    ("accept key: another GUID", WS, 'GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"', 'GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B12"'),
    ("origin: any http(s) origin passes", WS, " and u.netloc.lower() == host.strip().lower()", ""),
    ("client frames may be unmasked", WS, "    if not b1 & 0x80:\n        raise ProtocolError", "    if False:\n        raise ProtocolError"),
    ("RSV bits are not checked", WS, "    if b0 & 0x70:", "    if False:"),
    ("fragments are accepted", WS, "    if not b0 & 0x80:", "    if False:"),
    ("oversize client frames are accepted", WS, "    if n >= 126:", "    if False:"),
    ("data frames from the client are accepted", WS, "    if op not in (OP_CLOSE, OP_PING, OP_PONG):", "    if False:"),
    ("pong answers with an empty payload", WS, "_send(sock, frame(OP_PONG, payload), write_timeout_s)", "_send(sock, frame(OP_PONG, b''), write_timeout_s)"),
    ("the server never pings", WS, "            if now - last_ping >= ping_s:", "            if False:"),
    ("no pong watchdog", WS, "            if now - last_pong > pong_timeout_s:", "            if False:"),
    ("no write timeout", WS, "    sock.settimeout(write_timeout_s)\n    try:", "    sock.settimeout(None)\n    try:"),
    ("no connection cap", WS, "            if self.n >= self.limit:", "            if False:"),
    ("close is not answered", WS, "                            _send(sock, close_frame(CLOSE_NORMAL if code < 1000 else code), write_timeout_s)\n", "                            pass\n"),
    ("no hello frame", WS, "        _send(sock, frame(OP_TEXT, json.dumps(HELLO, separators=(\",\", \":\")).encode()), write_timeout_s)\n", ""),
    ("no reset frame", WS, "            if reset:\n                _send", "            if False:\n                _send"),
    ("absent beam not flagged", WS, "            fl |= F_MISSING\n", "            pass\n"),
    ("seq is not the row index", WS, "idx & 0xFFFFFFFF, t_us, fl, 0)", "0, t_us, fl, 0)"),
    ("upgrade checks skipped", UI, "        bad = WS.upgrade_problem(self.headers.get)", "        bad = None"),
    ("no login on the upgrade", UI, "        if not request_authenticated(self):\n            return self._problem(401, \"unauthorized\", \"Authentication required\", \"the live spectrum needs a login\", head)",
     "        if False:\n            return None"),
    ("no Origin check", UI, "        if origin is not None and not WS.origin_ok(origin, self.headers.get(\"Host\")):", "        if False:"),
    ("route served without --ui-next", UI, "if path == \"/api/v2/live\" and self.server.next_assets and WS:", "if path == \"/api/v2/live\" and WS:"),
    ("slot not released at the end", UI, "        finally:\n            self.server.ws_gate.release()", "        finally:\n            pass"),
    ("a query string is accepted", UI, "            parse_query(parts.query, set())", "            pass"),
    ("an incomplete row is never held back", UI, "None in sl.bins and now - sl.arr < self.LIVE_PARTIAL_S", "False"),
    ("a revision change is not a reset", UI, "            reset = (rev is not None and rev != ring.rev) or since > ring.newest_idx", "            reset = False"),
]


def run_one(m):
    name, f, old, new = m
    p = os.path.join(HERE, f)
    orig = open(p, newline="", encoding="utf-8").read()
    if orig.count(old) != 1:
        return "BAD-MUTANT (pattern found %d times in %s)" % (orig.count(old), f)
    try:
        open(p, "w", newline="", encoding="utf-8").write(orig.replace(old, new))
        r = subprocess.run([PY, os.path.join(HERE, SUITE)], capture_output=True, text=True, timeout=300)
        fl = [ln for ln in r.stdout.splitlines() if ln.startswith("FAIL")]
        return ("KILLED (%d red: %s)" % (len(fl), fl[0][5:90])) if r.returncode != 0 and fl else ("KILLED (exit %d)" % r.returncode if r.returncode else "SURVIVED")
    except subprocess.TimeoutExpired:
        return "KILLED (hang: the suite did not finish)"
    finally:
        open(p, "w", newline="", encoding="utf-8").write(orig)


def main():
    sel = sys.argv[1] if len(sys.argv) > 1 else ""
    bad = 0
    for m in MUTANTS:
        if sel in m[0]:
            res = run_one(m)
            print("%-44s %s" % (m[0], res), flush=True)
            bad += not res.startswith("KILLED")
    print("%d mutant(s) not killed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
