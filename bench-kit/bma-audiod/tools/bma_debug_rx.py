#!/usr/bin/env python3
"""Collect the board's low-bandwidth debug streams from bma-audiod: FPGA meters (BMAM), DSP-monitor lines and
tagged housekeeping lines (BMAD). The PC connects to the board; nothing is forwarded, no telnet data.

  bma_debug_rx.py BOARD_IP[:48000] | auto  [--secs N (0 = run forever)] [--out FILE.jsonl] [--quiet]

`auto` takes the board from $BMA_BOARD: a MAC (e.g. 00-00-5E-00-53-01) is looked up in the OS neighbour table and
confirmed with a BMAS status reply, an IP address is used as it is. It re-discovers if the stream goes silent (the board's DHCP address changes at every boot).
--out appends one JSON object per record (rotated at 64 MB to FILE.1); a summary table prints at exit.

Monitor line format (board monitor): "<t> <dsp> <slot> <mean-square> <peak>" over 24-bit words;
slot 4 is the reference channel and its mean-square field is not on the same scale.
Lines tagged "@health {json}" are bma_health.sh records; temperatures are decoded here (float32 BE words).
"""

import argparse, collections, json, math, os, re, shutil, socket, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # -I (isolated mode) does not add the script directory
sys.path.insert(1, os.path.join(HERE, "..", "pc"))  # in the repo history_log.py lives in pc/; pinned app\ is flat
from history_log import RotatingJsonl, load_tail, HISTORY_DIR  # noqa: E402

FS = 8388608.0
MAC_RE = re.compile(r"^[0-9a-f]{2}([:-])(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}$", re.I)


def board_spec(spec):
    """BMA_BOARD value -> ("mac", "xx-xx-xx-xx-xx-xx") | ("ip", address) | None (unset or empty)."""
    spec = (spec or "").strip()
    if not spec:
        return None
    if MAC_RE.match(spec):
        return "mac", spec.lower().replace(":", "-")
    return "ip", spec


def f32(hexword):
    try:
        return round(struct.unpack(">f", bytes.fromhex(hexword))[0], 2)
    except (ValueError, struct.error, TypeError):
        return None


def parse_meter(d):
    nsel, flags = d[5], d[6]
    seq, ms = struct.unpack("<II", d[8:16])
    vals = {}
    for i in range(nsel):
        sel, val = struct.unpack("<HH", d[16 + 4 * i : 20 + 4 * i])
        vals[f"{sel:02x}"] = f"{val:04x}"
    return {"type": "meter", "seq": seq, "ms": ms, "zero": bool(flags & 1), "vals": vals}


def parse_monitor(d):
    seq = struct.unpack("<I", d[8:12])[0]
    body = d[12:].decode("utf-8", "replace")
    out = []
    for l in body.split("\n") if body else []:
        rec = {"type": "monitor", "seq": seq, "line": l}
        if l.startswith("@health "):
            try:
                h = json.loads(l[8:])
                h["type"] = "health"
                h["seq"] = seq
                h["temp_local_c"] = f32(h.get("temp_local_hex", ""))
                h["temp_remote_c"] = f32(h.get("temp_remote_hex", ""))
                rec = h
            except ValueError:
                rec["type"] = "health_unparsed"
        else:
            f = l.split()
            if len(f) == 5 and all(x.lstrip("-").isdigit() for x in f):
                t, dsp, slot, ms, pk = map(int, f)
                rec.update(
                    t=t,
                    dsp=dsp,
                    slot=slot,
                    meansq=ms,
                    peak=pk,
                    peak_dbfs=(20 * math.log10(pk / FS) if pk > 0 else None),
                    rms_dbfs=(10 * math.log10(ms / FS**2) if ms > 0 and slot != 4 else None),
                )
        out.append(rec)
    return out


def arp_candidates(mac, txt=None):
    """IPv4 addresses the OS neighbour table maps to `mac` (stale entries included). `txt` = the `arp -a` output."""
    if txt is None:
        try:
            txt = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return []
    ips = []
    for line in txt.splitlines():
        f = line.split()
        if len(f) >= 2 and f[1].lower().replace(":", "-") == mac:
            ips.append(f[0])
    return ips


def probe(ip, port, timeout=1.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(b"BMAS\x01\x03\x00\x00", (ip, port))
        d, a = s.recvfrom(4096)
        return d[:4] == b"BMAS" and a[0] == ip
    except OSError:
        return False
    finally:
        s.close()


def discover(port, mac):
    for ip in arp_candidates(mac):
        if probe(ip, port):
            return ip
    return None


def open_sock(port):
    """UDP socket for the BMAS subscription, bound to a FIXED source port. The board daemon keys subscribers by (ip, port)
    and, with its media path disabled (the default here), only expires leases while serving media, i.e. never: every
    restart on a new ephemeral port permanently used up one of its 8 slots, and when they were gone every new subscriber
    was refused (2026-10-25). Reusing the same port renews our own slot. If the port is taken (a second collector) fall
    back to an ephemeral one with a warning so collection still works."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        s.bind(("0.0.0.0", port))
    except OSError as e:
        print(f"bma_debug_rx: cannot bind UDP {port} ({e}); using an ephemeral port: restarts will use up board subscriber slots", file=sys.stderr)
    return s


class Out:
    def __init__(self, path, cap=64 << 20, replace=os.replace):
        self.path, self.cap, self.replace = path, cap, replace
        self.limit = cap  # rotate when the file passes this size
        self.rotate_fallbacks = 0  # rotations that could not rename and copied + truncated instead
        self.f = open(path, "a", buffering=1) if path else None

    def write(self, rec):
        if not self.f:
            return
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if self.f.tell() > self.limit:
            self._rotate()

    def _rotate(self):
        """Move the full file to FILE.1 and start a new one. Collection must never stop: on Windows the rename fails
        while any other process holds the file open (the cable sink's tailer always does), so fall back to copying the
        file to FILE.1 and truncating it in place (a tailer sees the size drop and restarts at 0); if even that fails keep
        appending and try again after another `cap` bytes."""
        self.f.close()
        try:
            self.replace(self.path, self.path + ".1")
            self.limit = self.cap
        except OSError:
            self.rotate_fallbacks += 1
            try:
                shutil.copyfile(self.path, self.path + ".1")
                with open(self.path, "r+b") as t:
                    t.truncate(0)
                self.limit = self.cap
            except OSError as e:
                print(f"bma_debug_rx: cannot rotate {self.path}: {e}; keeping the file", file=sys.stderr)
                self.limit = os.path.getsize(self.path) + self.cap
        self.f = open(self.path, "a", buffering=1)


BOARD_HISTORY_CAP = 16 << 20  # board_history.jsonl; with the one backup 32 MB, about 0.26 MB/day
REBOOT_UP_DROP_S = 1.0  # uptime going backwards by more than this is a reboot
RESTART_BOOT_T_TOL_S = 120.0  # after a collector restart: boot_t further than this from the stored one is a reboot
REBOOT_CONFIRM_GAP_S = 1.0  # a suspected reboot is real only when a second sample comes at least this much later in host_t ...
REBOOT_CONFIRM_BOOT_T_TOL_S = 5.0  # ... with the same boot_t (a replayed backlog advances uptime faster than host time)


class BoardHistory:
    """Board temperature + boot history from the @health records the collector ALREADY receives (about one per minute): no
    board write, no new socket. Each health record gives a board_sample (uptime, derived boot_t = host_t - up, temperatures,
    load, free memory); the first sample of a boot is preceded by a board_boot event so reboots are real events.

    Reboot rule (uptime is the only reliable signal; the board clock drifts against the PC by seconds per day, so boot_t must
    never be compared exactly): a new boot when `up` is lower than the previous `up` by more than 1 s, or, on the first
    sample after the collector started, when boot_t differs from the last stored boot_t (read back from the history file) by
    more than 120 s. boot_id = int(boot_t) at the first sample of the boot and then stays fixed for that boot.

    Replayed backlog (the board resends old health records in a burst: host_t ms apart, uptime lower than the last real one):
    a suspected reboot is held until a second sample arrives >= 1 s later in host_t with the same boot_t (+-5 s); held samples
    that are never confirmed are dropped, not written. A sample whose host_t equals the previous one is dropped."""

    def __init__(self, path, cap=BOARD_HISTORY_CAP, replace=os.replace):
        self.log = RotatingJsonl(path, cap, replace=replace)
        self.boot_id = self.boot_t = self.last_up = self.last_t = None
        self.restored = False
        self.skipped = 0  # health records without a usable uptime or host_t (not written)
        self.boots = 0
        self.dropped = 0  # replayed or duplicate samples (not written)
        self.pending = None  # first sample of a suspected reboot: (record, boot_t)
        for r in reversed(load_tail(path, 50)):  # state survives a collector restart
            if not isinstance(r, dict) or r.get("k") != "board_sample":
                continue
            vals = [r.get(x) for x in ("boot_id", "boot_t", "up", "t")]
            if all(self._num(v) for v in vals) and vals[2] >= 0:  # a damaged record is skipped, never crashes the collector
                self.boot_id, self.boot_t, self.last_up, self.last_t = vals
                self.restored = True
                break

    @staticmethod
    def _num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")

    def on_health(self, r):
        up, host_t = r.get("up"), r.get("host_t")
        if not (self._num(up) and self._num(host_t) and up >= 0):
            self.skipped += 1
            return None
        boot_t = round(host_t - up, 3)
        prev_t = self.pending[0].get("host_t") if self.pending else self.last_t
        if prev_t is not None and host_t == prev_t:
            self.dropped += 1
            return None
        suspect = self.boot_id is not None and (
            (self.last_up is not None and up < self.last_up - REBOOT_UP_DROP_S) or (self.restored and abs(boot_t - self.boot_t) > RESTART_BOOT_T_TOL_S)
        )
        if self.pending:
            first, first_bt = self.pending
            if host_t - first["host_t"] >= REBOOT_CONFIRM_GAP_S and abs(boot_t - first_bt) <= REBOOT_CONFIRM_BOOT_T_TOL_S:
                self.pending = None
                self._commit(first, first_bt, True)
                return self._commit(r, boot_t, False)
            self.dropped += 1
            if host_t - first["host_t"] >= REBOOT_CONFIRM_GAP_S:  # later, but another boot_t: a new suspicion
                self.pending = (r, boot_t) if suspect else None
                if not suspect:
                    return self._commit(r, boot_t, False)
                return None
            if not suspect:  # the burst is over; its samples are dropped
                self.pending = None
                return self._commit(r, boot_t, False)
            return None
        if not suspect:
            return self._commit(r, boot_t, self.boot_id is None)
        self.pending = (r, boot_t)
        return None

    def _commit(self, r, boot_t, new):
        up, host_t = r["up"], r["host_t"]
        self.restored = False
        if new:
            prev = (self.boot_id, self.last_up, self.last_t)
            self.boot_id = int(boot_t)
            self.boots += 1
            self.log.write(
                {
                    "k": "board_boot",
                    "v": 1,
                    "t": host_t,
                    "boot_id": self.boot_id,
                    "boot_t": boot_t,
                    "prev_boot_id": prev[0],
                    "prev_last_up": prev[1],
                    "gap_s": None if prev[2] is None else round(host_t - prev[2], 3),
                }
            )
        self.boot_t, self.last_up, self.last_t = boot_t, up, host_t
        try:
            load1 = float(str(r.get("load", "")).split()[0])
        except (ValueError, IndexError):
            load1 = None
        rec = {
            "k": "board_sample",
            "v": 1,
            "t": host_t,
            "up": up,
            "boot_t": boot_t,
            "boot_id": self.boot_id,
            "temp_local_c": r.get("temp_local_c"),
            "temp_remote_c": r.get("temp_remote_c"),
            "load1": load1,
            "memfree_kb": r.get("memfree_kb"),
            "seq": r.get("seq"),
        }
        self.log.write(rec)
        return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", help="BOARD_IP[:PORT] or 'auto' (the board named by $BMA_BOARD: a MAC or an IP address)")
    ap.add_argument("--secs", type=float, default=60.0, help="0 = run until killed")
    ap.add_argument("--out")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--history-dir",
        default=HISTORY_DIR,
        help="directory of the run-history files: board_history.jsonl gets a board_sample per health record and a "
        "board_boot per reboot (metadata only); '' to disable",
    )
    ap.add_argument("--src-port", type=int, default=48010, help="fixed UDP source port (see open_sock); 0 = ephemeral")
    a = ap.parse_args()
    auto = a.board == "auto"
    host, _, port = ("" if auto else a.board).partition(":")
    port = int(port or 48000)
    mac = None
    if auto:
        spec = board_spec(os.environ.get("BMA_BOARD"))
        if spec is None:
            ap.error("auto needs BMA_BOARD (the board's MAC or IP address)")
        kind, val = spec
        if kind == "ip":
            auto, host = False, val
        else:
            mac = val
    out = Out(a.out)
    bhist = BoardHistory(os.path.join(a.history_dir, "board_history.jsonl")) if a.history_dir else None
    health_unparsed = 0
    s = open_sock(a.src_port)
    sub = b"BMAS\x01\x01\x00\x03"  # no audio, meters + monitor lines (+ tagged housekeeping)
    dst = None
    last_sub = last_rx = 0.0
    counts = collections.Counter()
    agg = collections.defaultdict(lambda: {"n": 0, "ms": 0.0, "pk": 0})
    last = {"meter": None, "health": None}
    seqs, lost = {}, collections.Counter()
    t_end = time.time() + a.secs if a.secs else None
    warned = False
    while t_end is None or time.time() < t_end:
        now = time.time()
        if dst is None or (auto and now - last_rx > 20):
            ip = host if not auto else discover(port, mac)
            if ip is None:
                if not warned:
                    print(time.strftime("%H:%M:%S ") + "board not found; retrying", flush=True)
                    warned = True
                time.sleep(5)
                continue
            if dst != (ip, port):
                print(time.strftime("%H:%M:%S ") + f"collecting from {ip}:{port}", flush=True)
            dst, warned, last_rx = (ip, port), False, now
            seqs.clear()
        if now - last_sub > 4:
            s.sendto(sub, dst)
            last_sub = now
        try:
            d, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        last_rx = time.time()
        magic = d[:4]
        recs = []
        if magic in (b"BMAM", b"BMAD") and len(d) >= 12:
            sq = struct.unpack("<I", d[8:12])[0]
            m = magic.decode()
            if m in seqs and sq != (seqs[m] + 1) & 0xFFFFFFFF:
                lost[m] += (sq - seqs[m] - 1) & 0xFFFFFFFF
            seqs[m] = sq
        if magic == b"BMAM":
            r = parse_meter(d)
            counts["meter"] += 1
            last["meter"] = r
            recs = [r]
        elif magic == b"BMAD":
            recs = parse_monitor(d)
            for r in recs:
                counts[r["type"]] += 1
                if r["type"] == "health":
                    last["health"] = r
                    r["host_t"] = round(last_rx, 3)  # set before the loop below too; the history needs it now
                    if bhist:
                        bhist.on_health(r)
                elif r["type"] == "health_unparsed":
                    health_unparsed += 1
                if r["type"] == "monitor" and "dsp" in r:
                    g = agg[(r["dsp"], r["slot"])]
                    g["n"] += 1
                    g["pk"] = max(g["pk"], r["peak"])
                    if r["slot"] != 4:
                        g["ms"] += r["meansq"]
        elif magic == b"BMAS":
            counts["status"] += 1
            recs = [{"type": "status", "json": d[4:].decode("utf-8", "replace")}]
        else:
            counts["unrecognised"] += 1
            continue
        for r in recs:
            r["host_t"] = round(last_rx, 3)
            out.write(r)
    try:
        s.sendto(b"BMAS\x01\x02\x00\x00", dst)
    except (OSError, TypeError):
        pass
    if bhist:
        print(
            f"board history: {bhist.boots} boot event(s), {bhist.skipped} health record(s) without uptime, {health_unparsed} unparsed, "
            f"writer {bhist.log.counters()}"
        )
    print("received:", dict(counts), "| missing datagrams by seq gap:", dict(lost) or "none")
    if last["meter"]:
        print("last meters:", last["meter"]["vals"], "(all zero = DSP pipeline idle)" if last["meter"]["zero"] else "")
    h = last["health"]
    if h:
        print(
            f"last health: up {h.get('up')} s, load {h.get('load')}, DSP0 temp local {h.get('temp_local_c')} C "
            f"remote {h.get('temp_remote_c')} C, safety mute EA={h.get('fpga', {}).get('ea')}, d_state={h.get('d_state')}, "
            f"dmesg_err={h.get('dmesg_err')}"
        )
    if agg and not a.quiet:
        print(f"{'dsp':>3} {'slot':>4} {'n':>4} {'rms dBFS':>9} {'peak dBFS':>10}")
        for (dsp, slot), g in sorted(agg.items()):
            rms = f"{10 * math.log10(g['ms'] / g['n'] / FS**2):9.1f}" if slot != 4 and g["ms"] > 0 else "      n/a"
            pk = f"{20 * math.log10(g['pk'] / FS):10.1f}" if g["pk"] > 0 else "      -inf"
            print(f"{dsp:>3} {slot:>4} {g['n']:>4} {rms} {pk}{'  (reference)' if slot == 4 else ''}")
    return 0 if (counts["meter"] or counts["monitor"] or counts["health"]) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
