#!/usr/bin/env python3
"""CPU and loss benchmark for the spectrum path at the card's real rate: 3000 packets/s x 8 channels, 16 frames per packet.

Replays a synthetic 8-channel Dante flow (fake_dante_sender.py) into dante_bridge.py --bind and measures, per configuration, the
bridge's CPU time and what it reports (pkt/s, loss, gaps, blocks sent), and the CPU of every other process involved:

  baseline   : bridge with the two existing --all-to targets (analysis host, cable sink), each a null UDP counter
  +target    : the same plus one more --all-to target on loopback (a null counter): the bridge-side cost of the spectrum feed
  +spectrum  : the same with bma_spectrum.py (standard tier) as that extra target, rows going to a null counter
  +detail    : as +spectrum with --detail

    python -I bench_spectrum.py [--secs 20] [--reps 3] [--python PATH]

Needs numpy and psutil. Synthetic audio only; nothing here touches the card, the live pipeline or any live port (random loopback ports)."""

import argparse, os, socket, struct, subprocess, sys, tempfile, time, wave

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


NULL_SINK = "import socket,sys\ns=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\ns.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4<<20)\ns.bind(('127.0.0.1',int(sys.argv[1])))\nn=0\nwhile True:\n    s.recv(65535)\n    n+=1\n"


def make_wav(path, secs):
    import numpy as np

    n = int(secs * 48000)
    t = np.arange(n) / 48000
    x = 0.1 * np.sin(2 * np.pi * 1100 * t) + 0.05 * np.random.default_rng(1).standard_normal(n)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def run_cfg(py, cfg, secs, wav):
    procs, ports = [], {}

    def spawn(args, **kw):
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
        procs.append(p)
        return p

    try:
        sinks = [free_port() for _ in range(2)]
        for p in sinks:
            spawn([py, "-I", "-c", NULL_SINK, str(p)])
        targets = list(sinks)
        spec = None
        if cfg != "baseline":
            extra = free_port()
            targets.append(extra)
            if cfg == "+target":
                spawn([py, "-I", "-c", NULL_SINK, str(extra)])
            else:
                out = free_port()
                spawn([py, "-I", "-c", NULL_SINK, str(out)])
                spec = spawn(
                    [py, "-I", os.path.join(HERE, "bma_spectrum.py"), "--listen", f"127.0.0.1:{extra}", "--to", f"127.0.0.1:{out}"]
                    + (["--detail"] if cfg == "+detail" else [])
                )
        media = free_port()
        cls_sink = free_port()
        spawn([py, "-I", "-c", NULL_SINK, str(cls_sink)])
        time.sleep(1.0)
        bridge = spawn(
            [
                py,
                "-I",
                os.path.join(HERE, "dante_bridge.py"),
                "--bind",
                f"127.0.0.1:{media}",
                "--channels",
                "auto",
                "--to",
                f"127.0.0.1:{cls_sink}",
                "--stats-s",
                "5",
            ]
            + sum([["--all-to", f"127.0.0.1:{p}"] for p in targets], [])
        )
        time.sleep(1.5)
        sender = spawn([py, "-I", os.path.join(HERE, "fake_dante_sender.py"), wav, f"127.0.0.1:{media}", "--nch", "8", "--secs", str(secs)])
        pb = psutil.Process(bridge.pid)
        ps = psutil.Process(spec.pid) if spec else None
        b0 = pb.cpu_times()
        s0 = ps.cpu_times() if ps else None
        t0 = time.perf_counter()
        sender.wait()
        wall = time.perf_counter() - t0
        b1 = pb.cpu_times()
        s1 = ps.cpu_times() if ps else None
        time.sleep(1.0)
        bridge.terminate()
        out = bridge.communicate(timeout=10)[0]
        stats = [ln for ln in out.splitlines() if "stats:" in ln]
        res = {"cfg": cfg, "wall": wall, "bridge_cpu_s": (b1.user + b1.system) - (b0.user + b0.system), "stats": stats}
        if ps:
            res["spec_cpu_s"] = (s1.user + s1.system) - (s0.user + s0.system)
        return res
    finally:
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass
        for p in procs:
            try:
                p.communicate(timeout=5)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--secs", type=float, default=20.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--configs", default="baseline,+target,+spectrum,+detail")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="bma_bench_") as d:
        wav = os.path.join(d, "in.wav")
        make_wav(wav, a.secs + 2)
        rows = {}
        for rep in range(a.reps):
            for cfg in a.configs.split(","):
                r = run_cfg(a.python, cfg, a.secs, wav)
                rows.setdefault(cfg, []).append(r)
                last = r["stats"][-1] if r["stats"] else "(no stats line)"
                print(
                    f"rep {rep} {cfg:10s} wall {r['wall']:.2f}s bridge cpu {r['bridge_cpu_s']:.2f}s ({100 * r['bridge_cpu_s'] / r['wall']:.1f}% of one core)"
                    + (f"  spectrum cpu {r['spec_cpu_s']:.2f}s ({100 * r['spec_cpu_s'] / r['wall']:.1f}%)" if "spec_cpu_s" in r else "")
                    + "\n    "
                    + last,
                    flush=True,
                )
    print("\nsummary (mean over reps):")
    for cfg, rs in rows.items():
        bc = sum(r["bridge_cpu_s"] / r["wall"] for r in rs) / len(rs) * 100
        sc = sum(r.get("spec_cpu_s", 0) / r["wall"] for r in rs) / len(rs) * 100
        print(f"  {cfg:10s} bridge {bc:5.1f}% of one core" + (f", spectrum process {sc:5.1f}%" if cfg.startswith("+s") or cfg == "+detail" else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
