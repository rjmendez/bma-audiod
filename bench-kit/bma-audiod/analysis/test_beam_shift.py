#!/usr/bin/env python3
"""Tests for the analyzer's per-beam sample-shift correction (host.py --beam-shift). Script style (check(name, ok, detail),
exit 1 on a failure) like test_coherence.py. Temp files only under tempfile (TMPDIR / TEMP redirect it).
Run in the audioeval venv:  python -I test_beam_shift.py        (--skip-e2e leaves out the two 45 s host runs)

Semantics under test: shift k for beam b means window column b is ring b read at (window start + k), i.e. k samples LATER.
k = +1 advances the content by one sample (column[t] = beam[t + 1]).
End to end: beam 0 arrives 1 sample LATE (content delayed) and beam 7 1 sample EARLY, all else identical white noise.
Coherence pair p is (beam p, beam p+1) and its lag is +d when the second beam lags the first by d samples, so
  lags = d[p+1] - d[p] = [-1, 0, 0, 0, 0, 0, -1, +2] samples times the estimator's low-band bias (about 0.9, from the datamined data);
  with --beam-shift 1,0,0,0,0,0,0,-1 beam 0 is read one sample later and beam 7 one sample earlier: all lags are ~0."""

import argparse, json, os, shutil, socket, struct, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import host as H  # noqa: E402

fails, n_checks = [], 0
FS = 48000


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def raises(fn, part):
    """-> (True when fn raised argparse.ArgumentTypeError whose message contains part, message)"""
    try:
        fn()
    except argparse.ArgumentTypeError as e:
        return part in str(e), str(e)
    return False, "no exception"


# ------------------------------------------------------------------------------------------------ flag validation
check("parse: the documented vector", H.parse_beam_shift("1,0,0,0,0,0,0,-1") == [1, 0, 0, 0, 0, 0, 0, -1], H.parse_beam_shift("1,0,0,0,0,0,0,-1"))
check("parse: the default of the flag is eight zeros", H.parse_beam_shift("0,0,0,0,0,0,0,0") == [0] * 8)
check("parse: limits -4 and +4 and spaces are accepted (positive twin of the rejections)", H.parse_beam_shift("-4, 4,0,0,0,0,0,0") == [-4, 4, 0, 0, 0, 0, 0, 0])
for text, part in (
    ("1,0,0,0,0,0,0", "exactly 8"),
    ("1,0,0,0,0,0,0,0,0", "exactly 8"),
    ("5,0,0,0,0,0,0,0", "|k| <= 4"),
    ("0,0,0,0,0,0,0,-5", "|k| <= 4"),
    ("1,0,0,0,0,0,0,x", "comma-separated integers"),
    ("0.5,0,0,0,0,0,0,0", "comma-separated integers"),
    ("", "comma-separated integers"),
):
    ok, msg = raises(lambda t=text: H.parse_beam_shift(t), part)
    check(f"parse: {text!r} is rejected with a message containing {part!r}", ok, msg)
here = os.path.join(HERE, "host.py")
tmp = tempfile.mkdtemp(prefix="beam_shift_test_")
try:
    for text in ("1,2,3", "9,0,0,0,0,0,0,0"):
        r = subprocess.run([sys.executable, here, "--beam-shift", text, "--out", os.path.join(tmp, "x")], capture_output=True, text=True, timeout=60)
        check(
            f"host CLI: --beam-shift {text} exits 2 with the validation message on stderr and creates nothing",
            r.returncode == 2
            and "--beam-shift" in r.stderr
            and ("exactly 8" in r.stderr or "|k| <= 4" in r.stderr)
            and not os.path.exists(os.path.join(tmp, "x")),
            (r.returncode, r.stderr[-200:]),
        )

    # -------------------------------------------------------------------------------------------- margins
    check("margins: all zero -> (0, 0)", H.shift_margins([0] * 8) == (0, 0))
    check("margins: [1,0,..,-1] -> lead 1 (the -1 beam reads earlier), tail 1 (the +1 beam reads later)", H.shift_margins([1, 0, 0, 0, 0, 0, 0, -1]) == (1, 1))
    check("margins: only positive shifts [3,2,0..] -> (0, 3)", H.shift_margins([3, 2, 0, 0, 0, 0, 0, 0]) == (0, 3))
    check("margins: only negative shifts [0,-4,-2,0..] -> (4, 0)", H.shift_margins([0, -4, -2, 0, 0, 0, 0, 0]) == (4, 0))

    # -------------------------------------------------------------------------------------------- window assembly with exact values
    CAP, START, LEN = 4096, 100, 3000  # rings hold absolute samples [100, 3100); sample value = beam * 10000 + (index - START), exact in float32
    rings = [H.Ring(CAP) for _ in range(8)]
    for b, r in enumerate(rings):
        r.push(START, (b * 10000 + np.arange(LEN)).astype(np.float32))
    check("fixture: every ring spans [100, 3100)", all((r.start, r.end) == (START, START + LEN) for r in rings), [(r.start, r.end) for r in rings])
    n = 64

    def expect(a, shifts):
        return np.stack([b * 10000 + (a + shifts[b] - START) + np.arange(n) for b in range(8)], axis=1).astype(np.float32)

    shifts = [1, 0, 0, 0, 0, 0, 0, -1]
    w = H.assemble_window(rings, 1000, n, shifts)
    check("assemble: shape (64, 8), float32", w.shape == (n, 8) and w.dtype == np.float32, (w.shape, w.dtype))
    check(
        "assemble: beam 0 reads ring 0 at a+1, beam 7 at a-1, the others at a (exact values)",
        np.array_equal(w, expect(1000, shifts)),
        (w[0], expect(1000, shifts)[0]),
    )
    check(
        "assemble: column 0 sample 0 is exactly 1 sample ahead (value 901), column 7 one behind (70899), column 3 at a (30900)",
        (w[0, 0], w[0, 7], w[0, 3]) == (901.0, 70899.0, 30900.0),
        (w[0, 0], w[0, 7], w[0, 3]),
    )
    check(
        "assemble: zero shifts equal the plain per-ring read (default: no change)",
        np.array_equal(H.assemble_window(rings, 1000, n, [0] * 8), expect(1000, [0] * 8)),
    )
    lead, tail = H.shift_margins(shifts)
    for a in (START + lead, START + LEN - tail - n):
        wa = H.assemble_window(rings, a, n, shifts)
        idx = wa - (np.arange(8) * 10000)[None, :]
        check(
            f"assemble: window at a={a} (edge of [lo+lead, hi-tail-n]) decodes to indices within [0, {LEN}) and is exact",
            idx.min() >= 0 and idx.max() <= LEN - 1 and np.array_equal(wa, expect(a, shifts)),
            (idx.min(), idx.max()),
        )
    wlo = H.assemble_window(rings, START + lead - 1, n, shifts)  # one sample outside the margin: ring 7 (shift -1) reaches below start
    check(
        "assemble: positive control for the margin: one sample before lo+lead the -1 beam reads index -1: outside the ring, where the buffer is still 0.0 (in range it would be 70000+)",
        wlo[0, 7] == 0.0 and w[0, 7] == 70899.0,
        (wlo[0, 7], w[0, 7]),
    )
    whi = H.assemble_window(rings, START + LEN - tail - n + 1, n, shifts)
    check(
        "assemble: positive control: one sample after hi-tail-n the +1 beam's last sample is index LEN: outside the ring (0.0), where the window at the edge ends on index LEN-1",
        whi[-1, 0] == 0.0 and wa[-1, 0] == float(LEN - 1),
        (whi[-1, 0], wa[-1, 0]),
    )
    wneg = H.assemble_window(rings, 1000, n, [0, -4, -2, 0, 0, 0, 0, 0])
    wpos = H.assemble_window(rings, 1000, n, [3, 2, 0, 0, 0, 0, 0, 0])
    check(
        "assemble: -4 / -2 and +3 / +2 shifts (the limits of the flag) read exactly those offsets",
        (wneg[0, 1], wneg[0, 2], wpos[0, 0], wpos[0, 1]) == (10000 + 896, 20000 + 898, 903.0, 10000 + 902),
        (wneg[0, 1], wneg[0, 2], wpos[0, 0], wpos[0, 1]),
    )
    # all beams carry the same signal, beam 0 one sample late, beam 7 one early: the corrected windows are identical across beams
    sg = np.random.default_rng(5).standard_normal(LEN + 8).astype(np.float32)
    rr = [H.Ring(CAP) for _ in range(8)]
    for b, r in enumerate(rr):
        d = 1 if b == 0 else (-1 if b == 7 else 0)
        r.push(START, sg[4 - d : 4 - d + LEN])  # sample t of beam b = sg[t + 4 - d]
    wc = H.assemble_window(rr, 1000, n, shifts)
    wu = H.assemble_window(rr, 1000, n, [0] * 8)
    check(
        "assemble: late beam 0 / early beam 7 content, corrected by 1,0,0,0,0,0,0,-1 -> all 8 columns identical",
        all(np.array_equal(wc[:, 0], wc[:, b]) for b in range(8)),
    )
    check(
        "assemble: positive twin: uncorrected, columns 0 and 7 differ from column 3",
        not np.array_equal(wu[:, 0], wu[:, 3]) and not np.array_equal(wu[:, 7], wu[:, 3]),
    )
    check(
        "assemble: the wrong sign (-1,0,..,+1) does NOT align them",
        not np.array_equal(H.assemble_window(rr, 1000, n, [-1, 0, 0, 0, 0, 0, 0, 1])[:, 0], wc[:, 3]),
    )

    # -------------------------------------------------------------------------------------------- the host's edge bookkeeping, end to end
    # Exactly one window of data (48000 samples per beam, absolute samples [0, 48000)). Without a shift the coherence plugin (1 s windows)
    # processes window [0, 48000). With 1,0,0,0,0,0,0,-1 beam 7 would read [-1, 47999) and beam 0 [1, 48001): outside the rings, so the host must
    # cut one sample at each end of the common region and process NOTHING until there are 2 more samples (positive twin: 48100 samples).
    def free_port0():
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s_.bind(("127.0.0.1", 0))
        pt = s_.getsockname()[1]
        s_.close()
        return pt

    def short_run(tag, flag, nsamp):
        o = os.path.join(tmp, "edge_" + tag)
        os.makedirs(o)
        port = free_port0()
        cmd = [
            sys.executable,
            here,
            "--listen",
            f"127.0.0.1:{port}",
            "--out",
            o,
            "--history-dir",
            "",
            "--no",
            "dsp",
            "--no",
            "spatial",
            "--no",
            "tags",
            "--no",
            "clap",
            "--no",
            "birds",
            "--secs",
            "7",
        ] + (["--beam-shift", flag] if flag else [])
        pr = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        for _ in range(240):
            if os.path.exists(os.path.join(o, "status.json")) or pr.poll() is not None:
                break
            time.sleep(0.25)
        sk_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        x_ = (np.random.default_rng(3).standard_normal((nsamp, 8)) * 10 ** (-60 / 20)).astype(np.float32)
        for off in range(0, nsamp, 256):
            for slot in range(8):
                pk = x_[off : off + 256, slot]
                s24 = np.clip((pk * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
                sk_.sendto(b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, off, len(pk)) + s24.tobytes(), ("127.0.0.1", port))
        pr.wait(timeout=60)
        err_ = pr.stderr.read()
        st_ = json.load(open(os.path.join(o, "status.json")))
        return pr.returncode, st_["plugins"]["coherence"]["windows"], st_["rx"]["datagrams"], err_

    e0 = short_run("none48000", None, 48000)
    e1 = short_run("flag48000", "1,0,0,0,0,0,0,-1", 48000)
    e2 = short_run("flag48100", "1,0,0,0,0,0,0,-1", 48100)
    check(
        "edges: no shift, exactly 48000 samples per beam -> 1 window processed (positive control: the data and the host loop work)",
        e0[0] == 0 and e0[1] == 1 and e0[2] == 8 * 188,
        e0[:3] + (e0[3][-200:],),
    )
    check(
        "edges: --beam-shift 1,0,0,0,0,0,0,-1 with the same 48000 samples -> 0 windows: the common region is cut by 1 at both ends, no ring is read outside",
        e1[0] == 0 and e1[1] == 0 and e1[2] == 8 * 188,
        e1[:3] + (e1[3][-200:],),
    )
    check(
        "edges: the same flag with 48100 samples (100 spare) -> 1 window (positive twin of the cut)",
        e2[0] == 0 and e2[1] == 1 and e2[2] == 8 * 188,
        e2[:3] + (e2[3][-200:],),
    )

    # -------------------------------------------------------------------------------------------- end to end
    if "--skip-e2e" in sys.argv:
        print("SKIP e2e over UDP  [--skip-e2e]")
    else:

        def free_port():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            pt = s.getsockname()[1]
            s.close()
            return pt

        def bmaa(slot, block, x):
            s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
            return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()

        def rows(path):
            if not os.path.exists(path):
                return []
            with open(path, "rb") as f:
                return [json.loads(x) for x in f.read().splitlines() if x]

        SECS, SPEED = 150, 6  # the coherence plugin warms up for 60 windows (120 s of audio); the sender runs at 6x real time (the host handles up to 8x)
        z = (np.random.default_rng(31).standard_normal(SECS * FS + 16) * 10 ** (-60 / 20)).astype(np.float32)
        delay = [1, 0, 0, 0, 0, 0, 0, -1]  # beam 0 arrives 1 sample late, beam 7 1 sample early
        sig = np.stack([z[8 - delay[b] : 8 - delay[b] + SECS * FS] for b in range(8)], axis=1)  # sample t of beam b = z[t + 8 - delay]
        runs = {}
        for name, flag in (("raw", None), ("fixed", "1,0,0,0,0,0,0,-1")):
            o, h = os.path.join(tmp, name, "out"), os.path.join(tmp, name, "hist")
            os.makedirs(o)
            port = free_port()
            cmd = [
                sys.executable,
                here,
                "--listen",
                f"127.0.0.1:{port}",
                "--out",
                o,
                "--history-dir",
                h,
                "--no",
                "dsp",
                "--no",
                "spatial",
                "--no",
                "tags",
                "--no",
                "clap",
                "--no",
                "birds",
                "--coherence-interval-s",
                "86400",
                "--secs",
                "45",
            ] + (["--beam-shift", flag] if flag else [])
            runs[name] = {"port": port, "out": o, "hist": h, "proc": subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)}
        for rn in runs.values():
            for _ in range(240):
                if os.path.exists(os.path.join(rn["out"], "status.json")) or rn["proc"].poll() is not None:
                    break
                time.sleep(0.25)
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t_start, nb = time.time(), 0
        for off in range(0, SECS * FS, 256):
            for rn in runs.values():
                for slot in range(8):
                    sk.sendto(bmaa(slot, off, sig[off : off + 256, slot]), ("127.0.0.1", rn["port"]))
            nb += 1
            dly = t_start + nb * 256 / FS / SPEED - time.time()
            if dly > 0:
                time.sleep(dly)
        res = {}
        for name, rn in runs.items():
            rn["proc"].wait(timeout=120)
            err = rn["proc"].stderr.read()
            rs = rows(os.path.join(rn["out"], "coherence.jsonl"))
            used = sum(r["win_used"] for r in rs)
            print(
                "  record:",
                [
                    (
                        r["win_total"],
                        r["win_used"],
                        r["bands"][0]["n_quiet"],
                        r["bands"][0]["n_active"],
                        r["bands"][0]["mid"],
                        r["bands"][0]["warmup"],
                        r.get("partial"),
                    )
                    for r in rs
                ],
            )
            cells = [(r["bands"][0]["quiet"]) for r in rs if r["bands"][0]["quiet"]]
            lag = sum(np.array(c["lag_p50"]) * c["n"] for c in cells) / max(1, sum(c["n"] for c in cells))
            msc = sum(np.array(c["msc_p50"]) * c["n"] for c in cells) / max(1, sum(c["n"] for c in cells))
            res[name] = (rn["proc"].returncode, used, lag, msc, err)
            print(f"  {name}: rc={rn['proc'].returncode} used={used} low-band lags={np.round(lag, 3).tolist()} msc={np.round(msc, 3).tolist()}")
        rc, used, lag, msc, err = res["raw"]
        BIAS = 0.963  # low-band (50-125 Hz) phase-slope bias of the estimator, measured: -0.963 / +1.926 on this signal
        want = np.array([-1, 0, 0, 0, 0, 0, -1, 2]) * BIAS
        check("e2e raw: host exits 0 and enough windows were used", rc == 0 and used >= 8, (rc, used, err[-300:]))
        check(
            "e2e raw: WITHOUT the flag the 50-125 Hz pair lags show the skew: about [-.96,0,0,0,0,0,-.96,+1.93] within 0.1 sample",
            bool(np.abs(lag - want).max() < 0.1),
            (lag.tolist(), want.tolist()),
        )
        rc, used, lag, msc, err = res["fixed"]
        check("e2e fixed: host exits 0 and enough windows were used", rc == 0 and used >= 8, (rc, used, err[-300:]))
        check("e2e fixed: WITH --beam-shift 1,0,0,0,0,0,0,-1 every 50-125 Hz pair lag is 0 within 0.1 sample", bool(np.abs(lag).max() < 0.1), lag.tolist())
        check("e2e fixed: median MSC of every pair is at least 0.99 in the 50-125 Hz band", bool(msc.min() >= 0.99), msc.tolist())
        cfg_raw = rows(os.path.join(runs["raw"]["hist"], "config_history.jsonl"))
        cfg_fix = rows(os.path.join(runs["fixed"]["hist"], "config_history.jsonl"))
        check(
            "e2e config: the analysis_config of the corrected run records beam_shift exactly",
            bool(cfg_fix) and cfg_fix[0].get("beam_shift") == [1, 0, 0, 0, 0, 0, 0, -1],
            cfg_fix[:1],
        )
        check(
            "e2e config: the default run's analysis_config has no beam_shift key (backward compatible)",
            bool(cfg_raw) and "beam_shift" not in cfg_raw[0],
            cfg_raw[:1],
        )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
