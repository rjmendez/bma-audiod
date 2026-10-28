#!/usr/bin/env python3
"""Tests for bma_debug_rx.BoardHistory (board temperature + boot history from the @health records the collector already
receives). Scripted health series with exact numbers; temp directories only. Run:  python -I test_board_history.py"""

import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_debug_rx as R  # noqa: E402

n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def lines(path):
    with open(path, "rb") as f:
        return [json.loads(x) for x in f.read().splitlines() if x]


def health(host_t, up, seq, tl=40.0, tr=41.38, load="0.32 0.41 0.44", mem=6372):
    return {"type": "health", "k": "health", "seq": seq, "up": up, "load": load, "memfree_kb": mem, "temp_local_c": tl, "temp_remote_c": tr, "host_t": host_t}


def kinds(rows):
    return [r["k"] for r in rows]


tmp = tempfile.mkdtemp(prefix="board_hist_test_")
try:
    # ---- monotone uptime: exactly one board_boot, at the first sample
    p = os.path.join(tmp, "a", "board_history.jsonl")
    h = R.BoardHistory(p)
    T0 = 1793000000.0
    for i in range(4):
        h.on_health(health(T0 + 60 * i, 5000.0 + 60 * i, i))
    rows = lines(p)
    check("monotone uptime: one board_boot then 4 board_sample", kinds(rows) == ["board_boot"] + ["board_sample"] * 4, kinds(rows))
    check(
        "first boot event exact",
        rows[0]
        == {
            "k": "board_boot",
            "v": 1,
            "t": 1793000000.0,
            "boot_id": 1792995000,
            "boot_t": 1792995000.0,
            "prev_boot_id": None,
            "prev_last_up": None,
            "gap_s": None,
        },
        rows[0],
    )
    check(
        "first sample exact (temperatures, load1, memfree, seq, boot_id)",
        rows[1]
        == {
            "k": "board_sample",
            "v": 1,
            "t": 1793000000.0,
            "up": 5000.0,
            "boot_t": 1792995000.0,
            "boot_id": 1792995000,
            "temp_local_c": 40.0,
            "temp_remote_c": 41.38,
            "load1": 0.32,
            "memfree_kb": 6372,
            "seq": 0,
        },
        rows[1],
    )
    check("boot_id stays frozen across the boot", {r["boot_id"] for r in rows[1:]} == {1792995000})

    # ---- reboot: uptime falls from 5180 to 12
    n_before = len(lines(p))
    check(
        "a single sample with the uptime fallen to 12.0 is held, not written (a replay looks the same until a second sample confirms it)",
        h.on_health(health(T0 + 300.0, 12.0, 4)) is None and len(lines(p)) == n_before and kinds(lines(p)).count("board_boot") == 1,
        kinds(lines(p)),
    )
    h.on_health(health(T0 + 360.0, 72.0, 5))
    rows = lines(p)
    check(
        "uptime drop confirmed by a second sample 60 s later with the same boot_t: a second board_boot precedes both samples",
        kinds(rows)[-3:] == ["board_boot", "board_sample", "board_sample"] and kinds(rows).count("board_boot") == 2,
        kinds(rows),
    )
    check(
        "second boot event exact (prev_last_up 5180.0 = the last uptime before the reboot, gap 120 s)",
        rows[-3]
        == {
            "k": "board_boot",
            "v": 1,
            "t": 1793000300.0,
            "boot_id": 1793000288,
            "boot_t": 1793000288.0,
            "prev_boot_id": 1792995000,
            "prev_last_up": 5180.0,
            "gap_s": 120.0,
        },
        rows[-3],
    )
    check(
        "the new samples carry the new boot_id, the held one first (up 12.0, 72.0)",
        [(r_["boot_id"], r_["up"]) for r_ in rows[-2:]] == [(1793000288, 12.0), (1793000288, 72.0)],
        rows[-2:],
    )
    check("boots counted", h.boots == 2, h.boots)

    # ---- uptime jitter of 0.5 s backwards is NOT a reboot (positive twin of the drop)
    p2 = os.path.join(tmp, "jit.jsonl")
    h2 = R.BoardHistory(p2)
    h2.on_health(health(T0, 1000.0, 0))
    h2.on_health(health(T0 + 60, 1059.5, 1))
    h2.on_health(health(T0 + 120, 1059.0, 2))
    check("a 0.5 s uptime step back is not a reboot", kinds(lines(p2)) == ["board_boot", "board_sample", "board_sample", "board_sample"], kinds(lines(p2)))

    # ---- reboot threshold boundary: a 5 s drop IS a reboot (down from 400 s too), a 0.5 s drop is not
    for name, up0, up1, want in (
        ("5 s drop", 1000.0, 995.0, 2),
        ("400 s -> 20 s (reboot seen 20 s after boot)", 400.0, 20.0, 2),
        ("0.5 s drop", 1000.0, 999.5, 1),
    ):
        pb = os.path.join(tmp, "thr_%d_%d.jsonl" % (up0, up1 * 10))
        hb = R.BoardHistory(pb)
        hb.on_health(health(T0, up0, 0))
        hb.on_health(health(T0 + 60, up1, 1))
        hb.on_health(health(T0 + 120, up1 + 60, 2))
        check("threshold: " + name + " -> %d board_boot" % want, kinds(lines(pb)).count("board_boot") == want, kinds(lines(pb)))

    # ---- a damaged history tail must not crash the collector or poison its state
    good = json.dumps({"k": "board_sample", "v": 1, "t": T0, "up": 50.0, "boot_t": T0 - 50.0, "boot_id": int(T0 - 50.0)})
    for name, bad in (
        ("a bare number", "5"),
        ("a list", "[1,2]"),
        ("a sample missing keys", '{"k":"board_sample","v":1,"t":1.0}'),
        ("a sample with a string boot_t", json.dumps({"k": "board_sample", "t": T0, "up": 9.0, "boot_t": "x", "boot_id": 1})),
    ):
        pd = os.path.join(tmp, "dmg_%s.jsonl" % name.split()[1])
        with open(pd, "w") as f:
            f.write(good + "\n" + bad + "\n")
        hd = R.BoardHistory(pd)
        check(
            "damaged tail (" + name + "): skipped, state restored from the last valid sample",
            (hd.boot_id, hd.last_up, hd.restored) == (int(T0 - 50.0), 50.0, True),
            (hd.boot_id, hd.last_up),
        )
        rec = hd.on_health({"up": 110.0, "host_t": T0 + 60})
        check("damaged tail (" + name + "): next health is the same boot", rec is not None and rec["boot_id"] == int(T0 - 50.0) and hd.boots == 0, rec)
    pe = os.path.join(tmp, "dmg_only.jsonl")
    with open(pe, "w") as f:
        f.write("5\n")
    he = R.BoardHistory(pe)
    check(
        "only damaged lines: fresh state, first health starts a boot",
        he.boot_id is None and he.restored is False and he.on_health({"up": 5.0, "host_t": T0}) is not None and he.boots == 1,
        (he.boot_id, he.boots),
    )

    # ---- torn tail (no final newline): the first record after a restart is not glued onto the fragment
    pt = os.path.join(tmp, "torn.jsonl")
    with open(pt, "wb") as f:
        f.write(good.encode() + b'\n{"k":"board_sample","t":')
    ht = R.BoardHistory(pt)
    ht.on_health(health(T0 + 60, 5000.0, 0))  # different boot, confirmed by the next sample: board_boot then board_sample
    ht.on_health(health(T0 + 120, 5060.0, 1))
    raw = [l for l in open(pt, "rb").read().split(b"\n") if l]
    parsed = []
    for l in raw:
        try:
            parsed.append(json.loads(l)["k"])
        except ValueError:
            parsed.append("TORN")
    check(
        "torn tail: fragment isolated on its own line, board_boot and board_sample both survive",
        parsed == ["board_sample", "TORN", "board_boot", "board_sample", "board_sample"],
        parsed,
    )
    pn = os.path.join(tmp, "clean.jsonl")
    with open(pn, "wb") as f:
        f.write(good.encode() + b"\n")
    R.BoardHistory(pn).on_health(health(T0 + 60, 110.0, 0))
    check("positive twin: a newline-terminated file gets no blank line", b"\n\n" not in open(pn, "rb").read(), open(pn, "rb").read())

    # ---- board clock drift: boot_t moves 8 s over a day, no new boot (positive twin of the reboot tests)
    p3 = os.path.join(tmp, "drift.jsonl")
    h3 = R.BoardHistory(p3)
    h3.on_health(health(T0, 100000.0, 0))
    h3.on_health(health(T0 + 86400, 100000.0 + 86400 - 8, 1))  # a day later the board's uptime is 8 s short of wall time: boot_t +8 s
    rows3 = lines(p3)
    check(
        "drift: boot_t drifted by +8 s over a day, still one boot",
        kinds(rows3) == ["board_boot", "board_sample", "board_sample"]
        and rows3[2]["boot_t"] == rows3[1]["boot_t"] + 8.0
        and rows3[2]["boot_id"] == rows3[1]["boot_id"],
        rows3,
    )

    # ---- collector restart: state recovered from the tail of the file
    h4 = R.BoardHistory(p)  # same file as the first test, last sample up 72.0 at T0+360
    check("restart: state restored from the history file", (h4.boot_id, h4.last_up) == (1793000288, 72.0), (h4.boot_id, h4.last_up))
    h4.on_health(health(T0 + 3000.0, 2712.0, 0))  # same boot (boot_t 1793000288), collector was down 45 min
    rows = lines(p)
    check(
        "restart on the same boot: no new board_boot, boot_id unchanged",
        kinds(rows).count("board_boot") == 2 and rows[-1]["boot_id"] == 1793000288 and rows[-1]["up"] == 2712.0,
        kinds(rows),
    )
    h5 = R.BoardHistory(p)
    h5.on_health(health(T0 + 6000.0, 100.0, 0))
    h5.on_health(health(T0 + 6060.0, 160.0, 1))  # the board rebooted while the collector was down: up larger than before? here smaller
    check("restart after a reboot (uptime lower than stored): new boot", kinds(lines(p)).count("board_boot") == 3, kinds(lines(p)))
    h6 = R.BoardHistory(p)
    h6.on_health(health(T0 + 90000.0, 98000.0, 0))
    h6.on_health(health(T0 + 90030.0, 98030.0, 1))  # rebooted twice while down: up higher than stored, boot_t 8000 s off
    rows = lines(p)
    check(
        "restart after a reboot that left a HIGHER uptime: boot_t differs by more than 120 s -> new boot",
        kinds(rows).count("board_boot") == 4 and rows[-3]["boot_id"] == int(T0 + 90000.0 - 98000.0),
        rows[-3],
    )
    h7 = R.BoardHistory(p)
    h7.on_health(health(T0 + 90060.0, 98060.0 + 100.0, 1))  # restart with only 100 s of boot_t difference: tolerated (drift), same boot
    check("restart with boot_t 100 s off (below the 120 s tolerance): same boot", kinds(lines(p)).count("board_boot") == 4, kinds(lines(p)).count("board_boot"))

    # ---- null temperatures are kept as nulls; records without a usable uptime are counted, not written
    p8 = os.path.join(tmp, "null.jsonl")
    h8 = R.BoardHistory(p8)
    h8.on_health(health(T0, 7.0, 0, tl=None, tr=None, load="bad", mem=None))
    h8.on_health({"type": "health", "host_t": T0 + 60, "seq": 1})
    h8.on_health({"type": "health", "up": "7", "host_t": T0 + 60, "seq": 2})
    h8.on_health({"type": "health", "up": True, "host_t": T0 + 60, "seq": 3})
    h8.on_health({"type": "health", "up": -5.0, "host_t": T0 + 60, "seq": 4})
    r8 = lines(p8)
    check(
        "null temperatures and a bad load string are written as null",
        r8[1]["temp_local_c"] is None and r8[1]["temp_remote_c"] is None and r8[1]["load1"] is None and r8[1]["memfree_kb"] is None,
        r8[1],
    )
    check(
        "4 records with a missing, string, boolean or negative (-5) uptime are counted as skipped and not written",
        h8.skipped == 4 and len(r8) == 2,
        (h8.skipped, len(r8)),
    )
    h8.on_health(health(T0 + 120, 0.0, 5))
    h8.on_health(health(T0 + 180, 60.0, 6))
    check(
        "positive twin: uptime 0.0 is accepted (a reboot after up 7.0, confirmed by the next sample: board_boot + 2 samples)",
        h8.skipped == 4 and kinds(lines(p8))[2:] == ["board_boot", "board_sample", "board_sample"] and [r_["up"] for r_ in lines(p8)[-2:]] == [0.0, 60.0],
        (h8.skipped, len(lines(p8))),
    )

    # ---- the writer rotates through the shared helper (a refused rename does not stop collection)
    class Refuse:
        def __call__(self, src, dst):
            raise PermissionError(13, "in use", src)

    p9 = os.path.join(tmp, "rot.jsonl")
    h9 = R.BoardHistory(p9, cap=700, replace=Refuse())
    for i in range(12):
        h9.on_health(health(T0 + 60 * i, 100.0 + 60 * i, i))
    check(
        "board history rotation with a refused rename: copy + truncate, counted, every write succeeded",
        h9.log.rotate_fallbacks >= 1 and h9.log.write_errors == 0 and os.path.exists(p9 + ".1"),
        h9.log.counters(),
    )
    # ---- replayed backlog (shape of the real history/board_history.jsonl: after real sample up 140195.03 the board resent old health
    #      records in a burst, host_t 0.3-2 ms apart, uptime 111838..., seq continuing; then real samples resumed)
    REAL = [(1793038039.526, 140082.93, 11941), (1793038097.363, 140140.67, 11942), (1793038151.695, 140195.03, 11943)]
    BURST = [
        (1793038152.048, 111838.63),
        (1793038152.050, 111897.70),
        (1793038152.051, 111957.73),
        (1793038152.052, 112016.12),
        (1793038152.052, 112073.94),
        (1793038152.053, 112133.85),
        (1793038152.053, 112194.24),
        (1793038152.054, 112248.46),
    ]
    p10 = os.path.join(tmp, "replay.jsonl")
    h10 = R.BoardHistory(p10)
    for t_, u_, q_ in REAL:
        h10.on_health(health(t_, u_, q_))
    for i, (t_, u_) in enumerate(BURST):
        h10.on_health(health(t_, u_, 11944 + i))
    h10.on_health(health(1793038211.700, 140255.03, 11999))
    rows10 = lines(p10)
    check(
        "replay burst (8 samples 0-2 ms apart, uptime 28000 s lower): no board_boot, only the 3 real samples and the next real one are written",
        kinds(rows10) == ["board_boot"] + ["board_sample"] * 4
        and [r_["up"] for r_ in rows10[1:]] == [140082.93, 140140.67, 140195.03, 140255.03]
        and h10.boots == 1,
        kinds(rows10),
    )
    check(
        "replay burst: all 8 held samples are counted as dropped (the first held, 7 more inside the 1 s window), none written, boot_id unchanged",
        h10.dropped == 8 and {r_["boot_id"] for r_ in rows10[1:]} == {1792897956},
        (h10.dropped, {r_["boot_id"] for r_ in rows10[1:]}),
    )
    # positive twin: the same drop as a REAL reboot (second sample 58 s later, same boot_t) is a boot
    p11 = os.path.join(tmp, "realreboot.jsonl")
    h11 = R.BoardHistory(p11)
    for t_, u_, q_ in REAL:
        h11.on_health(health(t_, u_, q_))
    h11.on_health(health(1793038212.0, 20.0, 12000))
    h11.on_health(health(1793038270.0, 78.0, 12001))
    rows11 = lines(p11)
    check(
        "twin: uptime falling to 20.0 and confirmed 58 s later with the same boot_t IS one board_boot (boot_id 1793038192, prev_last_up 140195.03)",
        kinds(rows11).count("board_boot") == 2 and rows11[-3]["boot_id"] == 1793038192 and rows11[-3]["prev_last_up"] == 140195.03 and h11.dropped == 0,
        rows11[-3:],
    )
    # a replay that is slow (second sample 1.5 s later, uptime advanced 60 s: boot_t moved by 58.5 s) is still not a boot
    p12 = os.path.join(tmp, "slowreplay.jsonl")
    h12 = R.BoardHistory(p12)
    for t_, u_, q_ in REAL:
        h12.on_health(health(t_, u_, q_))
    h12.on_health(health(1793038152.0, 111838.63, 1))
    h12.on_health(health(1793038153.5, 111898.63, 2))
    check(
        "slow replay: second sample 1.5 s later but uptime +60 s (boot_t differs by 58.5 s) does not confirm; the first is dropped, the second becomes the new suspect",
        kinds(lines(p12)).count("board_boot") == 1 and h12.dropped == 1 and h12.pending is not None and h12.pending[0]["up"] == 111898.63,
        (h12.dropped, h12.pending),
    )
    # spacing: a same-boot_t sample only 0.5 s later does not confirm; one 1.0 s after the first does
    p14 = os.path.join(tmp, "spacing.jsonl")
    h14 = R.BoardHistory(p14)
    for t_, u_, q_ in REAL:
        h14.on_health(health(t_, u_, q_))
    h14.on_health(health(1793038300.0, 100.0, 1))
    h14.on_health(health(1793038300.5, 100.5, 2))
    check(
        "0.5 s after the first suspect with the same boot_t: not confirmed (1 board_boot, 1 dropped)",
        kinds(lines(p14)).count("board_boot") == 1 and h14.dropped == 1,
        (h14.dropped, kinds(lines(p14))),
    )
    h14.on_health(health(1793038301.0, 101.0, 3))
    check(
        "twin: exactly 1.0 s after the first with the same boot_t: confirmed, board_boot at the first sample's t",
        kinds(lines(p14)).count("board_boot") == 2 and [r_["t"] for r_ in lines(p14) if r_["k"] == "board_boot"][-1] == 1793038300.0,
        kinds(lines(p14)),
    )
    # a sample with the same host_t as the previous one is dropped; the next distinct one is written
    p13 = os.path.join(tmp, "dup.jsonl")
    h13 = R.BoardHistory(p13)
    h13.on_health(health(T0, 5000.0, 0))
    check(
        "same host_t as the previous sample: dropped (None, nothing written, dropped == 1)",
        h13.on_health(health(T0, 5000.5, 1)) is None and len(lines(p13)) == 2 and h13.dropped == 1,
        (len(lines(p13)), h13.dropped),
    )
    check(
        "twin: a different host_t 0.001 s later is written", h13.on_health(health(T0 + 0.001, 5000.5, 2)) is not None and len(lines(p13)) == 3, len(lines(p13))
    )
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
