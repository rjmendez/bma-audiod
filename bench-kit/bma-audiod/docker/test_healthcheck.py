#!/usr/bin/env python3
"""Tests for docker/healthcheck.py (the container healthchecks): the real functions against real files, a real UDP
socket and a real local HTTP server. Hermetic (temp dir, loopback). Run:  python test_healthcheck.py  (exit 0 = pass)."""

import http.server, os, socket, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import healthcheck as H  # noqa: E402

n, fails = 0, []


def check(name, got, want):
    global n
    n += 1
    if got != want:
        fails.append(name)
        print(f"FAIL {name}: got {got!r}, want {want!r}")


tmp = tempfile.mkdtemp()
os.environ["BMA_DATA_DIR"] = tmp

# fresh: a file modified now is healthy; the same file 100 s old is not, with the exact reason; a missing file says so
p = os.path.join(tmp, "status.json")
open(p, "w").close()
check("fresh: new file ok", H.fresh("status.json", 60), None)
old = time.time() - 100
os.utime(p, (old, old))
msg = H.fresh("status.json", 60)
check("fresh: old file names path and limit", msg is not None and msg.startswith(p + " is 10") and msg.endswith("s old (limit 60s)"), True)
check("fresh: same old file passes with a 200 s limit", H.fresh("status.json", 200), None)
check("fresh: missing file", H.fresh("nope.json", 60), f"{os.path.join(tmp, 'nope.json')}: No such file or directory")

# udp: a fake /proc/net/udp row pins the hex port decoding exactly (48200 = 0xBC48); the neighbours are not matched
tbl = os.path.join(tmp, "udp")
with open(tbl, "w") as f:
    f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops\n")
    f.write("   0: 00000000:BC48 00000000:0000 07 00000000:00000000 00:00000000 00000000 10001 0 12345 2 0000000000000000 0\n")
check("udp: 48200 bound", H.udp(48200, (tbl,)), None)
check("udp: 48201 not bound", H.udp(48201, (tbl,)), "nothing bound to udp/48201")
check("udp: 48199 not bound", H.udp(48199, (tbl,)), "nothing bound to udp/48199")
check("udp: missing table", H.udp(48200, (os.path.join(tmp, "absent"),)), "nothing bound to udp/48200")
# and against the kernel's real table: a socket this process binds is found, an unused port is not
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
check("udp: real socket found", H.udp(port), None)
s.close()
check("udp: closed socket gone", H.udp(port), f"nothing bound to udp/{port}")


# http: 200 is healthy; 503 is not, with the status in the reason
class Handler(http.server.BaseHTTPRequestHandler):
    code = 200

    def do_GET(self):
        self.send_response(type(self).code)
        self.end_headers()

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
hp = srv.server_address[1]
check("http: 200 ok", H.http(hp), None)
Handler.code = 204  # a success status that is not 200 is not "answers 200"
check("http: 204 is unhealthy and says why", H.http(hp), "status 204")
Handler.code = 503
msg = H.http(hp)
check("http: 503 is unhealthy and says why", msg is not None and "503" in msg, True)
srv.shutdown()
srv.server_close()
msg = H.http(hp)
check("http: nothing listening is unhealthy", msg is not None and msg.startswith("URLError"), True)

# main(): exit codes
check("main: healthy -> 0", H.main(["hc", "fresh", "status.json", "200"]), 0)
check("main: unhealthy -> 1", H.main(["hc", "fresh", "status.json", "60"]), 1)
check("main: no mode -> 2", H.main(["hc"]), 2)
check("main: unknown mode -> 2", H.main(["hc", "bogus"]), 2)
check("main: bad number -> 2", H.main(["hc", "udp", "notaport"]), 2)

print(f"\n{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
