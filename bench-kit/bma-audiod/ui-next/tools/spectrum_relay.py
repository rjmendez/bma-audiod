#!/usr/bin/env python3
"""Spectrum relay: copies the rows a dashboard serves on GET /api/spectrum into BMAS datagrams for ANOTHER dashboard's --spectrum-port, so a second
(test) dashboard can show real rows without a second pipeline. Read only on the source (GET, the same call the dashboard page makes, one beam per
request, at most 4.5 requests/s, under the source's 5/s limit); the only thing it sends is UDP to a loopback port. Rows only: no audio exists in them.
Standard tier. Stdlib only. DEV TOOL ONLY: not installed, not started by the supervisor, in no default path; run by hand against a dashboard you may read.

    python spectrum_relay.py --source https://127.0.0.1:48500 --token-file <root>\\dashboard_token.txt --to 127.0.0.1:48521 [--insecure]
    python bma_ui.py --ui-next --port 48501 --spectrum-port 48521 --pipeline-root <a COPY of the data> ...        (then open /next/ on port 48501)"""
import argparse, base64, collections, ipaddress, json, socket, ssl, struct, sys, threading, time, urllib.error, urllib.request

MAGIC, VERSION, HOP_MS, N_BINS = b"BMAS", 1, 100, 46
POLL_S = 0.22                                                   # 9 beams in about 2 s: 4.5 requests/s
QUEUE_ROWS = 30                                                 # per beam; older rows are dropped (the far side accepts 10 rows/s per beam)
SEND_S = 0.1                                                    # one row per beam per tick: the far dashboard's own per-beam rate


def build_row(flags, beam, row, t_us, bins):
    return MAGIC + bytes([VERSION, 0, flags & 0xFF, beam]) + struct.pack("<IQHH", row & 0xFFFFFFFF, t_us, HOP_MS, N_BINS) + bytes(bins)


def datagrams(body, beam):
    """-> [BMAS datagram] for the rows of one /api/spectrum answer (state live or stale, standard tier); [] for any other answer."""
    if body.get("tier") != "standard" or body.get("n_bins") != N_BINS or body.get("state") not in ("live", "stale"):
        return []
    out = []
    for r in body["rows"]:
        bins = base64.b64decode(r["bins"])
        if len(bins) == N_BINS:
            out.append(build_row(r["flags"], beam, r["idx"], int(round(r["t_s"] * 1e6)), bins))
    return out


class Source:
    def __init__(self, base, token, insecure=False):
        self.base, self.auth = base.rstrip("/"), "Basic " + base64.b64encode(b"relay:" + token.encode()).decode()
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.cur = [[0, 0] for _ in range(9)]                    # per beam: since, rev

    def poll(self, beam):
        since, rev = self.cur[beam]
        q = "/api/spectrum?beam=%d&compare=false" % beam + ("&since=%d&rev=%d" % (since, rev) if since else "")
        req = urllib.request.Request(self.base + q, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=10, context=self.ctx) as r:
            body = json.loads(r.read())
        if body.get("state") in ("live", "stale"):
            self.cur[beam] = [body["next_since"], body["rev"]]
        return body


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", required=True, help="base URL of the dashboard to read (GET only)")
    ap.add_argument("--token-file", required=True, help="file holding its dashboard token (read only)")
    ap.add_argument("--to", default="127.0.0.1:48521", help="loopback HOST:PORT of the other dashboard's --spectrum-port")
    ap.add_argument("--insecure", action="store_true", help="do not verify the source's TLS certificate (a local self-signed one)")
    ap.add_argument("--secs", type=float, default=0.0, help="stop after N seconds (tests); 0 = run until stopped")
    a = ap.parse_args(argv)
    host, port = a.to.rsplit(":", 1)
    if not ipaddress.ip_address(host).is_loopback:
        print("--to must be a loopback address", file=sys.stderr)
        return 2
    with open(a.token_file, "rb") as f:
        token = f.read().decode().strip()
    src, tx, beam, t_end = Source(a.source, token, a.insecure), socket.socket(socket.AF_INET, socket.SOCK_DGRAM), 0, (time.time() + a.secs if a.secs else None)
    queues, sent, stop = [collections.deque(maxlen=QUEUE_ROWS) for _ in range(9)], [0], threading.Event()

    def sender():
        while not stop.is_set():
            for q in queues:
                try:
                    tx.sendto(q.popleft(), (host, int(port)))
                    sent[0] += 1
                except IndexError:
                    pass
            time.sleep(SEND_S)
    threading.Thread(target=sender, daemon=True).start()
    while t_end is None or time.time() < t_end:
        t0 = time.time()
        try:
            queues[beam].extend(datagrams(src.poll(beam), beam))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print("source: %s" % type(e).__name__, file=sys.stderr, flush=True)
            time.sleep(2)
        beam = (beam + 1) % 9
        time.sleep(max(0.0, POLL_S - (time.time() - t0)))
    time.sleep(SEND_S * 2)
    stop.set()
    print("sent %d rows" % sent[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
