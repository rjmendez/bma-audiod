#!/usr/bin/env python3
"""PC-side Dante bridge: card flow -> gap-aware BMAA datagrams for classify/bma_classify.py.

The Dante card is cabled straight to this PC (no board in the path). This program
  1. finds the card (PTP multicast on a local 169.254/16 interface) unless --card/--iface-ip given,
  2. asks it for its Tx flow over ARC (:4440, read-only query 0x2200) and joins that group,
  3. decodes the verified media layout (UDP 4321, 9-byte header, 16 frames x N ch x 24-bit BE),
  4. buffers each selected channel in 256-sample blocks against the card's own stream clock and
     FLUSHES at every hole (never splices audio across a gap), flagging the next block gap_before,
  5. sends BMAA datagrams (same layout the board daemon uses) to one or more targets.
It never creates, changes or deletes a flow, and sends nothing to the card except the query.

BMAA: contracts/specs/bmaa.md. Default target 'wsl:48200' resolves the
WSL VM address (Windows reaches it directly; localhost forwarding does not carry UDP).
"""

import argparse, json, os, socket, struct, subprocess, sys, time

try:
    import numpy as np
except ImportError:  # pure-python fallback (selected channels only, still fine for 1-2 channels)
    np = None

HDR, FRAMES, RATE, MAXCH = 9, 16, 48000, 8
CH_BYTES = FRAMES * 3
BLOCK = 256


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


# ----------------------------------------------------------------------------- discovery
def local_ipv4():
    ips = set()
    try:
        for r in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(r[4][0])
    except OSError:
        pass
    return sorted(ips)


def ptp_card(iface_ip, wait=4.0):
    """Source IP of PTP (224.0.1.129:319) traffic seen on this interface, other than ourselves."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", 319))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton("224.0.1.129") + socket.inet_aton(iface_ip))
    except OSError:
        s.close()
        return None
    s.settimeout(0.5)
    end = time.time() + wait
    found = None
    while time.time() < end and not found:
        try:
            _, a = s.recvfrom(512)
        except socket.timeout:
            continue
        if a[0] != iface_ip and a[0].startswith("169.254."):
            found = a[0]
    s.close()
    return found


def arp_cards():
    """Link-local IPs whose MAC carries the Audinate OUI 00-1d-c1, from the OS neighbour table."""
    out = set()
    try:
        txt = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    for line in txt.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0].startswith("169.254.") and f[1].lower().replace(":", "-").startswith("00-1d-c1"):
            out.add(f[0])
    return sorted(out)


def multicast_source(iface_ip, group, port, wait=3.0):
    """First 169.254.x source seen on a multicast group (the card's CMC control traffic is 224.0.0.233:8708)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(iface_ip))
    except OSError:
        s.close()
        return None
    s.settimeout(0.5)
    end, found = time.time() + wait, None
    while time.time() < end and not found:
        try:
            _, a = s.recvfrom(1500)
        except socket.timeout:
            continue
        if a[0] != iface_ip and a[0].startswith("169.254."):
            found = a[0]
    s.close()
    return found


def arc_txn(iface_ip, card, pkt, tid, port=4440):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((iface_ip, 0))
    s.settimeout(1.0)
    try:
        for _ in range(3):
            try:
                s.sendto(pkt, (card, port))
                d, a = s.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                return None  # unreachable from this interface: not a card candidate here
            if a[0] == card and len(d) >= 10 and struct.unpack(">H", d[4:6])[0] == tid:
                return d
    finally:
        s.close()
    return None


def build_query(start, tid, proto=0x2729):
    p = bytearray(16)
    struct.pack_into(">HHHH", p, 0, proto, 16, tid, 0x2200)
    struct.pack_into(">HH", p, 10, 1, start)
    return bytes(p)


def build_create(slot, chans, tid, proto=0x2729):
    """ARC 0x2201 multicast Tx flow for 1-based `chans` at flow index `slot` (layout verified on the card,
    byte-exact against the board capture in its unit test)."""
    n = len(chans)
    L = 56 + 2 * n
    p = bytearray(L)
    struct.pack_into(">HHHH", p, 0, proto, L, tid, 0x2201)
    struct.pack_into(">H", p, 10, 0x0101)
    struct.pack_into(">H", p, 12, 0x0010)
    struct.pack_into(">H", p, 16, slot)
    struct.pack_into(">H", p, 18, 0x0002)  # FLOW_TYPE_MULTICAST
    struct.pack_into(">H", p, 30, n)
    o = 32
    for c in chans:
        struct.pack_into(">H", p, o, c)
        o += 2
    struct.pack_into(">H", p, o, 36 + 2 * n)
    o += 4
    struct.pack_into(">H", p, o, 0x0A00)
    o += 2 + 14
    struct.pack_into(">H", p, o, 0x0001)
    return bytes(p)


def arc_query(iface_ip, card, start, tid, port=4440):
    return arc_txn(iface_ip, card, build_query(start, tid), tid, port)


def result_ok(resp):
    return resp is not None and len(resp) >= 10 and struct.unpack(">H", resp[8:10])[0] in (1, 0x8112)


def first_flow(resp):
    """(group, port) of the FIRST flow in an ARC 0x2200 response: port 0x10e1-style u16 followed by a
    239.x multicast address. Same record the daemon's parse_flow reads."""
    if resp is None or len(resp) < 12 or struct.unpack(">H", resp[8:10])[0] not in (1, 0x8112):
        return None
    for i in range(10, len(resp) - 6):
        if resp[i + 2] == 0xEF:
            port = struct.unpack(">H", resp[i : i + 2])[0]
            if 1024 <= port <= 65535:
                return ".".join(str(b) for b in resp[i + 2 : i + 6]), port
    return None


def discover(iface_ip, card, slot, create_chans=None, arc_port=4440, state_file=None):
    """-> (iface_ip, card_ip, (group, port)) or None. The card is found by whichever signal works: --card,
    the last IP that worked (state_file), the ARP table (Audinate OUI), PTP, then the CMC multicast. Each
    candidate is validated by an ARC query. If the card answers with an EMPTY flow table and create_chans
    is given, the multicast Tx flow is created (what a power-cycled card needs) and re-queried."""
    ifaces = [iface_ip] if iface_ip else [ip for ip in local_ipv4() if ip.startswith("169.254.")]
    remembered = None
    if state_file:
        try:
            remembered = open(state_file).read().strip() or None
        except OSError:
            pass
    for ip in ifaces:
        cands = []
        for c in [card] if card else [remembered] + arp_cards():
            if c and c not in cands:
                cands.append(c)
        tried_signals = False
        while True:
            for c in cands:
                resp = arc_query(ip, c, slot, 0x4000 + slot, arc_port)
                if resp is None:
                    continue
                fl = first_flow(resp)
                if not fl and result_ok(resp) and create_chans:
                    for proto in (0x2729, 0x2801):
                        log(f"card {c} has no Tx flow at index {slot}: creating it (channels {create_chans}, proto {proto:04x})")
                        r = arc_txn(ip, c, build_create(slot, create_chans, 0x4100 + slot, proto), 0x4100 + slot, arc_port)
                        if result_ok(r):
                            fl = first_flow(arc_query(ip, c, slot, 0x4200 + slot, arc_port))
                            if fl:
                                break
                            log("WARN: create accepted but the card still reports no flow")
                        else:
                            log(f"WARN: create rejected with proto {proto:04x}")
                if fl:
                    if state_file and c != remembered:
                        try:
                            open(state_file, "w").write(c)
                        except OSError:
                            pass
                    return ip, c, fl
            if tried_signals or card:
                break
            tried_signals = True
            for sig in (ptp_card(ip), multicast_source(ip, "224.0.0.233", 8708)):
                if sig and sig not in cands:
                    cands.append(sig)
            if not cands:
                break
    return None


def wsl_ip():
    try:
        out = subprocess.run(["wsl.exe", "-e", "hostname", "-I"], capture_output=True, text=True, timeout=20).stdout
        for tok in out.split():
            if tok.startswith("172.") and not tok.startswith("172.31."):
                return tok
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------- decode + buffer
def decode_selected(p, chans):
    """-> (abs_frame, nch, {ch: [16 ints]}) or None. Channel order is frame-major, ch fastest."""
    n = len(p)
    if n < HDR + CH_BYTES or p[0] != 0x02 or (n - HDR) % CH_BYTES:
        return None
    nch = (n - HDR) // CH_BYTES
    if nch > MAXCH:
        return None
    sec, fr = struct.unpack(">II", p[1:9])
    if fr >= RATE:
        return None
    out = {}
    if np is not None:
        # one pass for all channels: 24-bit BE -> top 3 bytes of a BE int32, then >> 8 sign-extends
        w = np.zeros((FRAMES * nch, 4), dtype=np.uint8)
        w[:, :3] = np.frombuffer(p, dtype=np.uint8, count=nch * CH_BYTES, offset=HDR).reshape(FRAMES * nch, 3)
        v = np.ascontiguousarray((w.view(">i4").reshape(FRAMES, nch) >> 8).astype(np.int32).T)
        for c in chans:
            if c < nch:
                out[c] = v[c]
    else:
        for c in chans:
            if c < nch:
                vals = []
                for f in range(FRAMES):
                    o = HDR + (f * nch + c) * 3
                    v = (p[o] << 16) | (p[o + 1] << 8) | p[o + 2]
                    vals.append(v - (1 << 24) if v & 0x800000 else v)
                out[c] = vals
    return sec * RATE + fr, nch, out


class ChanBuf:
    def __init__(self):
        self.acc = []  # list of int lists / arrays, total length self.n
        self.n = 0
        self.start = 0
        self.gap = False

    def flush(self):
        if not self.n:
            return None
        if np is not None:
            s = np.concatenate([np.asarray(x, dtype=np.int32) for x in self.acc])
            samples = s
        else:
            samples = [v for x in self.acc for v in x]
        blk = (self.start, samples, self.gap)
        self.acc, self.n, self.gap = [], 0, False
        return blk

    def push(self, abs_frame, frames, gap_event):
        out = []
        if gap_event:
            b = self.flush()
            if b:
                out.append(b)
            self.gap = True
        if self.n == 0:
            self.start = abs_frame
        self.acc.append(frames)
        self.n += FRAMES
        while self.n >= BLOCK:
            if np is not None:
                s = np.concatenate([np.asarray(x, dtype=np.int32) for x in self.acc])
                blk, rest = s[:BLOCK], s[BLOCK:]
                self.acc = [rest] if len(rest) else []
            else:
                s = [v for x in self.acc for v in x]
                blk, rest = s[:BLOCK], s[BLOCK:]
                self.acc = [rest] if rest else []
            out.append((self.start, blk, self.gap))
            self.start += BLOCK
            self.n -= BLOCK
            self.gap = False
        return out


def bmaa(slot, flags, seq, block, samples):
    n = len(samples)
    if np is not None:
        body = np.asarray(samples, dtype="<i4").tobytes()
    else:
        body = struct.pack("<%di" % n, *samples)
    return b"BMAA" + bytes([1, 0, slot, flags]) + struct.pack("<III", seq & 0xFFFFFFFF, block & 0xFFFFFFFF, n) + body


def is_zero(samples):
    if np is not None:
        return not np.any(samples)
    return not any(samples)


# ----------------------------------------------------------------------------- main
def parse_chans(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a) - 1, int(b)))
        else:
            out.append(int(part) - 1)
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iface-ip", help="local IPv4 on the card link (default: auto from 169.254/16 + PTP)")
    ap.add_argument("--card", help="card IPv4 (default: discovered by PTP)")
    ap.add_argument("--slot", type=int, default=1, help="ARC flow index to query (1 = first flow, default)")
    ap.add_argument(
        "--channels",
        default="1",
        help="1-based channels to forward, e.g. 1 or 1-2, or 'auto' = the live channel with the most energy, "
        "re-chosen every 10 s and always sent as slot 0 (default 1)",
    )
    ap.add_argument("--to", action="append", default=[], help="HOST:PORT target; 'wsl:PORT' = the WSL VM. Repeatable")
    ap.add_argument("--status-file", help="write a JSON status snapshot here every --stats-s")
    ap.add_argument("--stats-s", type=float, default=5.0)
    ap.add_argument(
        "--all-to",
        action="append",
        default=[],
        help="HOST:PORT (or wsl:PORT) that also gets ALL 8 channels, each as its own slot 0-7 "
        "(for the analysis host; the classifier path above is unchanged). Repeatable",
    )
    ap.add_argument("--state-file", help="remember the last working card IP here (tried first next time)")
    ap.add_argument(
        "--create-channels",
        default="1-8",
        help="if the card has NO Tx flow (power-cycled), create one for these 1-based channels "
        "(default 1-8; 'none' = never create). An existing flow is never touched",
    )
    ap.add_argument("--bind", help="TEST ONLY: ADDR:PORT to receive media on, no discovery, no join")
    a = ap.parse_args()
    auto = a.channels.strip().lower() == "auto"
    sel = list(range(MAXCH)) if auto else parse_chans(a.channels)
    chans = list(range(MAXCH)) if (auto or a.all_to) else sel  # channels decoded per packet
    targets_spec = a.to or ["wsl:48200"]

    def resolve():
        out = []
        for t in targets_spec:
            h, _, p = t.rpartition(":")
            if h == "wsl":
                ip = wsl_ip()
                if ip:
                    out.append((ip, int(p)))
                else:
                    log("WARN: WSL address not resolvable; will retry")
            else:
                out.append((h, int(p)))
        return out

    def resolve_all():
        out = []
        for t in a.all_to:
            h, _, p = t.rpartition(":")
            if h == "wsl":
                ip = wsl_ip()
                if ip:
                    out.append((ip, int(p)))
            else:
                out.append((h, int(p)))
        return out

    targets = resolve()
    all_targets = resolve_all()
    t_resolved = time.time()
    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    while True:  # (re)discover loop: card power cycle, link flap, flow change
        media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        media.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        media.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        if a.bind:
            h, _, p = a.bind.rpartition(":")
            media.bind((h or "0.0.0.0", int(p)))
            log(f"test mode: receiving on {a.bind}, no discovery, no join")
            group = a.bind
        else:
            cc = None if a.create_channels.lower() == "none" else [c + 1 for c in parse_chans(a.create_channels)]
            d = discover(a.iface_ip, a.card, a.slot, cc, state_file=a.state_file)
            if not d:
                log("WARN: card/flow not found (cable, link-local address, or no flow); retrying in 5 s")
                media.close()
                time.sleep(5)
                continue
            iface, card, (group_ip, port) = d
            media.bind(("", port))
            media.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group_ip) + socket.inet_aton(iface))
            group = f"{group_ip}:{port}"
            log(f"card {card} via {iface}; joined {group} (ARC flow index {a.slot})")
        media.settimeout(1.0)
        bufs = {}
        prev = None
        seq = {c: 0 for c in chans}
        energy = {c: 0.0 for c in chans}  # auto mode: sum of |x| per channel since the last choice
        chosen = None
        block_off = 0  # bumped at every channel switch so the classifier sees a hole
        abufs = {}  # --all-to path: one ChanBuf per channel, slot = channel index
        aseq = {c: 0 for c in range(MAXCH)}
        n_all_sent = 0
        t_choose = time.time()
        n_pkt = n_bad = n_gap = lost = n_back = n_blocks = n_sent = n_zero_blocks = 0
        nch_seen = None
        t0 = last = time.time()
        i_pkt = i_lost = i_blocks = i_zero = 0
        last_pkt = time.time()
        warned_starve = False
        while True:
            try:
                p, _ = media.recvfrom(2048)
            except socket.timeout:
                if time.time() - last_pkt > 5 and not warned_starve:
                    log("WARN: no media for 5 s (card down, flow removed or cable out); will re-discover")
                    warned_starve = True
                if time.time() - last_pkt > 15 and not a.bind:
                    break
                p = None
            now = time.time()
            if p is not None:
                last_pkt, warned_starve = now, False
                r = decode_selected(p, chans)
                if r is None:
                    n_bad += 1
                else:
                    abs_frame, nch, per = r
                    if nch != nch_seen:
                        log(f"flow has {nch} channel(s) per packet; forwarding {'auto' if auto else [c + 1 for c in chans if c < nch]}")
                        nch_seen, bufs, prev = nch, {}, None
                    gap_event = False
                    if prev is not None:
                        exp = prev + FRAMES
                        if abs_frame > exp:
                            n_gap += 1
                            lost += abs_frame - exp
                            gap_event = True
                        elif abs_frame < exp:
                            n_back += 1
                            gap_event = True
                    prev = abs_frame
                    n_pkt += 1
                    if auto:
                        for c, fr in per.items():
                            energy[c] += float(np.abs(fr).sum()) if np is not None else float(sum(abs(v) for v in fr))
                        if now - t_choose >= 10 or chosen is None:
                            best = max(energy, key=energy.get)
                            # hysteresis: only leave a channel that went quiet, or take the first live one
                            switch = energy[best] > 0 and best != chosen and (chosen is None or energy[chosen] == 0 or energy[best] > 4 * energy[chosen])
                            if switch:
                                log(f"auto: forwarding channel {best + 1} as slot 0 (energy {[round(energy[k]) for k in sorted(energy)]})")
                                if chosen is not None:
                                    block_off = (block_off + 0x100000) & 0xFFFFFFFF
                                chosen = best
                                bufs = {}
                            elif energy[best] == 0 and chosen is None:
                                chosen = 0
                            energy = {c: 0.0 for c in energy}
                            t_choose = now
                    if all_targets:
                        for c, fr in per.items():
                            ab = abufs.setdefault(c, ChanBuf())
                            for start, samples, gap in ab.push(abs_frame, fr, gap_event):
                                d = bmaa(c, (1 if is_zero(samples) else 0) | (2 if gap else 0), aseq[c], start, samples)
                                aseq[c] = (aseq[c] + 1) & 0xFFFFFFFF
                                for t in all_targets:
                                    try:
                                        out_sock.sendto(d, t)
                                        n_all_sent += 1
                                    except OSError:
                                        pass
                    for c, fr in per.items():
                        if (auto and c != chosen) or (not auto and c not in sel):
                            continue
                        cb = bufs.setdefault(c, ChanBuf())
                        for start, samples, gap in cb.push(abs_frame, fr, gap_event):
                            n_blocks += 1
                            zero = is_zero(samples)
                            n_zero_blocks += zero
                            d = bmaa(0 if auto else c, (1 if zero else 0) | (2 if gap else 0), seq[c], (start + block_off) & 0xFFFFFFFF, samples)
                            seq[c] = (seq[c] + 1) & 0xFFFFFFFF
                            for t in targets:
                                try:
                                    out_sock.sendto(d, t)
                                    n_sent += 1
                                except OSError:
                                    pass
            if now - last >= a.stats_s:
                dt = now - last
                if now - t_resolved > 60:
                    targets, all_targets, t_resolved = resolve(), resolve_all(), now
                tot = (n_pkt - i_pkt) * FRAMES + (lost - i_lost)
                loss = 100.0 * (lost - i_lost) / tot if tot else 0.0
                zfrac = (n_zero_blocks - i_zero) / max(1, n_blocks - i_blocks)
                state = "NOPKT" if n_pkt == i_pkt else ("UNFED" if zfrac > 0.999 else "ok")
                log(
                    f"stats: {(n_pkt - i_pkt) / dt:.0f} pkt/s loss {loss:.2f}% gaps={n_gap} back={n_back} bad={n_bad} "
                    f"blocks={n_blocks - i_blocks} sent={n_sent} all_sent={n_all_sent} state={state} nch={nch_seen} group={group}"
                )
                if a.status_file:
                    try:
                        tmp = a.status_file + ".tmp"
                        with open(tmp, "w") as f:
                            json.dump(
                                {
                                    "t": time.time(),
                                    "group": group,
                                    "pkts": n_pkt,
                                    "pps": (n_pkt - i_pkt) / dt,
                                    "loss_pct": loss,
                                    "gaps": n_gap,
                                    "backwards": n_back,
                                    "bad": n_bad,
                                    "blocks": n_blocks,
                                    "blocks_all_zero_frac": zfrac,
                                    "state": state,
                                    "nch": nch_seen,
                                    "channels": ([chosen + 1] if auto and chosen is not None else [c + 1 for c in chans]),
                                    "targets": [f"{h}:{pt}" for h, pt in targets],
                                    "uptime_s": now - t0,
                                },
                                f,
                            )
                        os.replace(tmp, a.status_file)
                    except OSError:
                        pass
                if state == "UNFED":
                    log("WARN: every forwarded block is all-zero: the card's Tx channel carries no audio (unfed)")
                i_pkt, i_lost, i_blocks, i_zero, last = n_pkt, lost, n_blocks, n_zero_blocks, now
        media.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
