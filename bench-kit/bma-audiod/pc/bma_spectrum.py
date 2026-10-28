#!/usr/bin/env python3
"""bma_spectrum - magnitude-only spectrum rows for the dashboard (contracts/specs/spectrum-packet.md, BMAS 1.0.0).

    python bma_spectrum.py --listen 127.0.0.1:48410 --to 127.0.0.1:48520 [--detail]

Receives the bridge's 8-beam BMAA stream (an extra `--all-to 127.0.0.1:48410` target on dante_bridge.py), computes per beam
(and for the power mean of the 8 beams) band-power rows, and sends ONLY rows (BMAS datagrams: uint8 dB, no phase, no sample) to
the dashboard's loopback receiver. Audio never leaves this process and is never stored or logged. There is NO speech detection
here: rows are sent as soon as they are computed. Speech is handled by the analyzer's slower tagger (the dashboard's tag latch).

Both --listen and --to must be loopback (exit 2 otherwise); there is no flag to relax that.
Standard tier: 4800-sample Hann windows, no overlap, 10 rows/s, 46 bands. Detail tier (--detail): 2400-sample windows on a
960-sample hop, 50 rows/s, 100 bands. Python 3 + numpy."""
import argparse, ipaddress, math, socket, struct, sys, time

import numpy as np

RATE = 48000
SCALE = 1.0 / 8388608.0
MAGIC, VERSION = b"BMAS", 1
FLAG_GAP, FLAG_ZERO = 2, 4
DB_FLOOR, DB_STEP = -100.0, 0.5
TIERS = {0: {"name": "standard", "window": 4800, "hop": 4800, "n_bins": 46, "hop_ms": 100, "coarse_rows": 10},
         1: {"name": "detail", "window": 2400, "hop": 960, "n_bins": 100, "hop_ms": 20, "coarse_rows": 50}}
SLACK = 1024                              # samples the fastest beam may lead before a window is cut (the other beams arrive within this)
MAX_FILL_GAP = 16384                      # a hole up to this is zero-filled and marked; a longer one restarts the beam's buffer
BUF_CAP = 65536


def band_edges(tier):
    if tier == 0:
        e = [(50.0, 250.0)] + [(250.0 * 2 ** (k / 3), 250.0 * 2 ** ((k + 1) / 3)) for k in range(9)]
        return e + [(2000.0 * 2 ** (k / 12), 2000.0 * 2 ** ((k + 1) / 12)) for k in range(36)]
    e = [(250.0 * k, 250.0 * (k + 1)) for k in range(4)]
    return e + [(1000.0 * 2 ** (k / 24), 1000.0 * 2 ** ((k + 1) / 24)) for k in range(96)]


def quantize(db):
    """dB (array) -> uint8: round half even of (db + 100) * 2, clamped (np.rint rounds half to even)."""
    return np.clip(np.rint((np.asarray(db, dtype=np.float64) - DB_FLOOR) / DB_STEP), 0, 255).astype(np.uint8)


def to_db(p):
    return 10.0 * np.log10(np.maximum(p, 1e-30))


def hann(n):
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)       # periodic


class Bank:
    """Windowed one-sided power spectrum -> band powers for one tier. Power is mean-square re full scale (a full-scale sine is -3.01 dB)."""

    def __init__(self, tier):
        t = TIERS[tier]
        n = t["window"]
        self.tier, self.n, self.n_bins = tier, n, t["n_bins"]
        self.win = hann(n)
        k = np.arange(n // 2 + 1)
        two = np.full(len(k), 2.0)
        two[0] = 1.0
        if n % 2 == 0:
            two[-1] = 1.0
        self.scale = two / (float(n) ** 2 * float(np.mean(self.win ** 2)))
        freq = k * (RATE / n)
        m = np.zeros((self.n_bins, len(k)))
        for b, (lo, hi) in enumerate(band_edges(tier)):
            sel = (freq >= lo) & (freq < hi)
            if not sel.any():
                sel = np.zeros(len(k), dtype=bool)
                sel[int(np.argmin(np.abs(freq - math.sqrt(max(lo, 1e-9) * hi))))] = True
            m[b, sel] = 1.0
        self.m = m

    def band_power(self, x):
        """x: (beams, n) float samples -> (beams, n_bins) mean-square band power."""
        spec = np.fft.rfft(x * self.win, axis=1)
        return ((spec.real ** 2 + spec.imag ** 2) * self.scale) @ self.m.T


def build_row(tier, flags, beam, row, t_us, bins):
    t = TIERS[tier]
    return (MAGIC + bytes([VERSION, tier, flags & 0xFF, beam]) + struct.pack("<IQHH", row & 0xFFFFFFFF, t_us, t["hop_ms"], t["n_bins"])
            + bytes(bins))


# ------------------------------------------------------------------------------------------------ the buffers
class Chan:
    """One beam's contiguous sample run with zero-filled, marked holes. Samples are float32 and live only here, a few seconds."""

    def __init__(self):
        self.buf = np.zeros(BUF_CAP, dtype=np.float32)
        self.base, self.fill, self.last_u, self.holes, self.started = 0, 0, 0, [], False

    @property
    def end(self):
        return self.base + self.fill

    def unwrap(self, b):
        if not self.started:
            return b
        d = (b - self.last_u) & 0xFFFFFFFF
        return self.last_u + (d - (1 << 32) if d >= (1 << 31) else d)

    def push(self, u, cut, x):
        """-> 'ok' | 'dup' | 'reset' (a jump back by more than 1 s: the caller restarts the whole timeline)."""
        n = len(x)
        if not self.started:
            self.started, self.base, self.fill = True, u, 0
        end = self.end
        if u < end:
            if u + n <= end:
                return "dup" if u >= end - 48000 else "reset"
            x, u, n = x[end - u:], end, n - (end - u)
        elif u > end:
            gap = u - end
            if gap > MAX_FILL_GAP:
                self.base, self.fill, self.holes = u, 0, []
            else:
                self._room(gap + n)
                self.buf[self.fill:self.fill + gap] = 0.0
                self.fill += gap
                self.holes.append((end, u))
        elif cut:
            self.holes.append((u, u))                                # a marked boundary with no samples lost: windows must not span it
        self._room(n)
        self.buf[self.fill:self.fill + n] = x
        self.fill += n
        self.last_u = u
        return "ok"

    def _room(self, n):
        if self.fill + n <= BUF_CAP:
            return
        drop = max(self.fill + n - BUF_CAP, BUF_CAP // 2)
        drop = min(drop, self.fill)
        self.buf[:self.fill - drop] = self.buf[drop:self.fill]
        self.base += drop
        self.fill -= drop
        self.holes = [h for h in self.holes if h[1] > self.base]

    def get(self, s, e):
        if s < self.base or e > self.end:
            return None
        for a, b in self.holes:
            if (a < e and b > s) if b > a else (s < a < e):
                return None
        return self.buf[s - self.base:e - self.base]


# ------------------------------------------------------------------------------------------------ the engine
class Engine:
    """feed() BMAA blocks, get BMAS datagrams back (in sample-clock order, each row as soon as its window is complete)."""

    def __init__(self, detail=False, clock=time.time):
        self.tiers = [0, 1] if detail else [0]
        self.banks = {t: Bank(t) for t in self.tiers}
        self.chans = [Chan() for _ in range(8)]
        self.clock = clock
        self.bad = self.dups = self.resets = 0
        self.rows_out = {t: 0 for t in self.tiers}
        self._reset_timeline(0)

    def _reset_timeline(self, k_floor):
        self.s0 = None
        self.max_end = 0
        self.k = {t: k_floor for t in self.tiers}                      # next window per tier

    def feed(self, slot, block, flags, samples, now=None):
        """samples: int32 array of the BMAA block. -> list of BMAS datagrams that became due."""
        now = self.clock() if now is None else now
        ch = self.chans[slot]
        u = ch.unwrap(block)
        x = samples.astype(np.float32) * np.float32(SCALE)
        r = ch.push(u, bool(flags & 2), x)
        if r == "dup":
            self.dups += 1
            return []
        if r == "reset":
            self.resets += 1
            k = max(self.k.values()) if self.k else 0
            k = -(-k // 50) * 50 + 50
            for c in self.chans:
                c.__init__()
            self._reset_timeline(k)
            ch = self.chans[slot]
            ch.push(block, False, x)
            u = block
        if self.s0 is None:
            self.s0 = ch.base
        self.max_end = max(self.max_end, ch.end)
        return self._advance(now)

    def _frontier(self):
        return self.max_end - SLACK

    def _advance(self, now):
        f = self._frontier()
        out = []
        for t in self.tiers:
            tt = TIERS[t]
            while self.s0 + self.k[t] * tt["hop"] + tt["window"] <= f:
                s = self.s0 + self.k[t] * tt["hop"]
                if f - (s + tt["window"]) > 30 * RATE:                    # far behind (a long outage): skip ahead, keep alignment
                    self.k[t] = -(-((f - 10 * RATE - self.s0) // tt["hop"]) // 50) * 50
                    continue
                out += self._compute(t, self.k[t], s, now)
                self.k[t] += 1
                self.rows_out[t] += 1
        return out

    def _compute(self, t, k, s, now):
        tt = TIERS[t]
        e = s + tt["window"]
        wins = [c.get(s, e) for c in self.chans]
        valid = [i for i, w in enumerate(wins) if w is not None]
        rows = [(FLAG_GAP, bytes(tt["n_bins"]))] * 9
        if valid:
            x = np.stack([wins[i] for i in valid]).astype(np.float64)
            nonzero = x.any(axis=1)
            p = self.banks[t].band_power(x)
            p[~nonzero] = 0.0
            q = quantize(to_db(p))
            per = {}
            for j, i in enumerate(valid):
                per[i + 1] = (0, bytes(q[j])) if nonzero[j] else (FLAG_ZERO, bytes(tt["n_bins"]))
            mean = p.mean(axis=0)
            rows[0] = (0, bytes(quantize(to_db(mean)))) if nonzero.any() else (FLAG_ZERO, bytes(tt["n_bins"]))
            for b in range(1, 9):
                rows[b] = per.get(b, (FLAG_GAP, bytes(tt["n_bins"])))
        t_us = int(now * 1e6)
        return [build_row(t, fl, beam, k, t_us, bins) for beam, (fl, bins) in enumerate(rows)]


# ------------------------------------------------------------------------------------------------ BMAA input
def parse_bmaa(pkt):
    """-> (slot, flags, block, int32 samples) or None for any malformed datagram (counted by the caller). Reference: contracts/specs/bmaa.md."""
    if len(pkt) < 20 or pkt[:4] != b"BMAA" or pkt[4] != 1 or pkt[5] != 0:
        return None
    slot, flags = pkt[6], pkt[7]
    seq, block, n = struct.unpack_from("<III", pkt, 8)
    if slot > 7 or n < 1 or len(pkt) != 20 + 4 * n:
        return None
    x = np.frombuffer(pkt, dtype="<i4", count=n, offset=20)
    if x.min() < -8388608 or x.max() > 8388607:
        return None
    return slot, flags, block, x


def parse_loopback(addr):
    host, _, port = addr.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    if host != "localhost":
        try:
            ok = ipaddress.ip_address(host).is_loopback
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"{addr!r}: only loopback addresses are allowed")
    return ("127.0.0.1" if host == "localhost" else host), int(port)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--listen", default="127.0.0.1:48410", help="loopback HOST:PORT that receives the bridge's BMAA (--all-to target)")
    ap.add_argument("--to", default="127.0.0.1:48520", help="loopback HOST:PORT of the dashboard's --spectrum-port")
    ap.add_argument("--detail", action="store_true", help="also compute the detail tier (50 rows/s, 100 bands); the dashboard serves it to loopback clients only")
    ap.add_argument("--stats-s", type=float, default=30.0)
    a = ap.parse_args(argv)
    try:
        listen, to = parse_loopback(a.listen), parse_loopback(a.to)
    except ValueError as ex:
        print("bma_spectrum: " + str(ex), file=sys.stderr)
        return 2
    eng = Engine(detail=a.detail)
    rx = socket.socket(socket.AF_INET6 if ":" in listen[0] else socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    rx.bind(listen)
    rx.settimeout(1.0)
    tx = socket.socket(socket.AF_INET6 if ":" in to[0] else socket.AF_INET, socket.SOCK_DGRAM)
    print(f"bma_spectrum listening {a.listen} -> {a.to} detail={a.detail}", flush=True)
    n = sent = 0
    last = time.time()
    while True:
        try:
            p = rx.recv(2048)
        except socket.timeout:
            p = None
        if p is not None:
            r = parse_bmaa(p)
            if r is None:
                eng.bad += 1
            else:
                for d in eng.feed(r[0], r[2], r[1], r[3]):
                    try:
                        tx.sendto(d, to)
                        sent += 1
                    except OSError:
                        pass
                n += 1
        now = time.time()
        if now - last >= a.stats_s:
            print(f"stats: blocks={n} rows_sent={sent} bad={eng.bad} dups={eng.dups} resets={eng.resets}", flush=True)
            last = now


if __name__ == "__main__":
    sys.exit(main())
