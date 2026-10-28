#!/usr/bin/env python3
"""Tests for the spectrum feed of bma_ui (GET /api/spectrum, the loopback UDP receiver, the ring, the tag latch, tier enforcement,
limits). Rows are built with the CONTRACT reference encoder (contracts/ref_binary.py) and the payloads are validated against the
contract schema. Same conventions as test_bma_ui.py: PASS/FAIL lines, a final 'N checks, 0 failure(s)' line, non-zero exit on failure.

    python test_bma_ui_spectrum.py     (env BMA_UI_DIR as for test_bma_ui.py; run with the contracts venv, which has jsonschema)"""
import base64, http.client, json, math, os, shutil, socket, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, os.path.join(UI_DIR, "..", "contracts"))
import bma_ui as U  # noqa: E402
import auth as A  # noqa: E402
import ref_binary as R  # noqa: E402
import jsonschema  # noqa: E402

fails, n_checks = [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4                                      # the dashboard token of the servers below that have the REAL login (ui/auth.py)
AUTH_HEADER = {"Authorization": "Basic " + base64.b64encode(b"owner:" + TOKEN.encode()).decode()}


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


SCH = json.load(open(os.path.join(UI_DIR, "..", "contracts", "schemas", "ui_http.schema.json"), encoding="utf-8"))
FIX = os.path.join(UI_DIR, "..", "contracts", "fixtures")


def schema_errors(name, obj):
    v = jsonschema.Draft202012Validator({"$schema": SCH["$schema"], "$ref": "#/$defs/" + name, "$defs": SCH["$defs"]})
    return [e.message for e in v.iter_errors(obj)]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_app(root, clk, enabled=True, detail=False):
    pr = os.path.join(root, "pipeline")
    os.makedirs(os.path.join(pr, "analysis"), exist_ok=True)
    os.makedirs(os.path.join(pr, "logs"), exist_ok=True)
    tf = os.path.join(root, "token")
    open(tf, "w").write("x\n")
    board = U.Board("http://127.0.0.1:1", tf, enabled=False)
    feed = U.SpectrumFeed(enabled=enabled, detail=detail, clock=clk) if enabled else None
    return U.App(pr, os.path.join(root, "reports"), os.path.join(root, "rollups"), board, clock=clk, log=lambda m: None, spectrum=feed)


def pkt(tier, beam, row, t_s, bins, flags=0):
    return R.build_spectrum(tier, flags, beam, row, int(t_s * 1e6), bins)


def fine_bins(row, tier=0, base=40):
    n = R.SPEC_TIERS[tier]["n_bins"]
    return [(base + row + 3 * i) % 200 for i in range(n)]


def feed_rows(feed, clk, first, count, tier=0, beams=(0,), step=None, base=40):
    """Feed rows first..first+count-1, advancing the clock by one hop per row (so the per-stream rate cap is respected)."""
    step = step or R.SPEC_TIERS[tier]["hop_ms"] / 1000.0
    ok = 0
    for row in range(first, first + count):
        for b in beams:
            ok += bool(feed.handle(pkt(tier, b, row, clk.t, fine_bins(row, tier, base)), "127.0.0.1"))
        clk.t += step
    return ok


def decode(row):
    return list(base64.b64decode(row["bins"]))


tmp = tempfile.mkdtemp(prefix="bma_ui_spec_")
try:
    # -------------------------------------------------------------------------------------------- packet parsing equals the reference
    ex = json.load(open(os.path.join(FIX, "spectrum_examples.json")))
    ok = True
    for v in ex["valid"]:
        t, fl, bm, row, tus, bins = U.parse_spectrum_packet(bytes.fromhex(v["hex"]))
        ok &= (t == ("standard", "detail")[v["tier"]] and fl == v["flags"] and bm == v["beam"] and row == v["row"] and tus == v["t_us"] and list(bins) == v["bins"])
    check("every VALID contract example parses to the same fields as the reference", ok)
    codes = []
    for v in ex["invalid"]:
        try:
            U.parse_spectrum_packet(bytes.fromhex(v["hex"]))
            codes.append(("accepted", v["code"]))
        except U.SpecError as e:
            codes.append((e.code, v["code"]))
    check("every INVALID contract example is rejected with the reference's code", all(a == b for a, b in codes), str([c for c in codes if c[0] != c[1]]))
    check("edges, coarse groups and quantization equal the reference for both tiers",
          all(U.spec_edges(t) == R.spectrum_band_edges(t) and U.spec_groups(t) == R.spectrum_coarse_groups(t) for t in (0, 1))
          and all(U.spec_quantize(g / 16.0) == R.quantize_db(g / 16.0) for g in range(-1700, 450)))
    c = ex["coarsen_example"]
    eq("spec_coarsen equals the contract's coarsen_example result", list(U.spec_coarsen(c["tier"], [bytes(r) for r in c["rows"]])), c["result"])
    fx = json.load(open(os.path.join(FIX, "ui_spectrum.json")))
    eq("edges_hz of the standard tier equal the contract fixture's edges_hz", U.SPEC_EDGES_HZ["standard"], fx["edges_hz"])

    # -------------------------------------------------------------------------------------------- states, payload, schema
    clk = Clock()
    app_off = make_app(os.path.join(tmp, "off"), clk, enabled=False)
    j = app_off.api_spectrum({}, "198.51.100.50", True)
    eq("disabled (no --spectrum-port): state 'disabled', no rows, n_bins 0, no edges", (j["state"], j["rows"], j["n_bins"], j["edges_hz"]), ("disabled", [], 0, []))
    eq("disabled payload validates against the schema", schema_errors("spectrum", j), [])
    app = make_app(os.path.join(tmp, "on"), clk, enabled=True, detail=True)
    feed = app.spectrum
    j = app.api_spectrum({}, "198.51.100.50", True)
    eq("enabled, nothing received: state 'no_data' and no rows", (j["state"], j["rows"], j["newest_idx"], j["newest_age_s"]), ("no_data", [], 0, None))
    eq("no_data payload validates", schema_errors("spectrum", j), [])
    feed_rows(feed, clk, 0, 5, beams=range(9))
    j = app.api_spectrum({}, "198.51.100.50", True)
    eq("after 5 rows: live, 5 rows, idx 1..5, 46 bins, next_since 5", (j["state"], [r["idx"] for r in j["rows"]], j["n_bins"], j["next_since"]), ("live", [1, 2, 3, 4, 5], 46, 5))
    eq("live payload validates against the schema (no errors)", schema_errors("spectrum", j), [])
    eq("row 3 bins are exactly what was sent (base64 round trip)", decode(j["rows"][2]), fine_bins(2))
    eq("t_s is the packet time of the row's window end", j["rows"][0]["t_s"], round(1_800_000_000.0, 3))
    j2 = app.api_spectrum({"beam": "4"}, "198.51.100.50", True)
    eq("beam=4 serves beam 4's own rows (5 rows), schema-valid", (len(j2["rows"]), j2["beam"], schema_errors("spectrum", j2)), (5, 4, []))
    check("negative twin: a payload with a base64 'bins' that is not base64 fails the schema", schema_errors("spectrum", dict(j, rows=[dict(j["rows"][0], bins="!!")])) != [])
    check("negative twin: a payload without 'rows' fails the schema", schema_errors("spectrum", {k: v for k, v in j.items() if k != "rows"}) != [])
    clk.t += 2.8
    eq("2.9 s after the newest row (arrival) it is still live", app.api_spectrum({}, "203.0.113.1", True)["state"], "live")
    clk.t += 0.2
    j = app.api_spectrum({}, "203.0.113.9", True)
    eq("3.1 s after the newest row: state 'stale', rows still listed, newest_age_s 3.1", (j["state"], len(j["rows"]), j["newest_age_s"]), ("stale", 5, 3.1))
    eq("stale payload validates", schema_errors("spectrum", j), [])
    st = app.spectrum.summary()
    eq("status.spectrum summary: enabled, detail enabled, stale, 45 rows received (5 rows x 9 beams)", (st["enabled"], st["detail_enabled"], st["state"], st["rows_received"]), (True, True, "stale", 45))
    eq("the summary validates against the status.spectrum schema", [e.message for e in jsonschema.Draft202012Validator({"$schema": SCH["$schema"], "$defs": SCH["$defs"], "$ref": "#/$defs/status/properties/spectrum"}).iter_errors(st)], [])

    # -------------------------------------------------------------------------------------------- ring bounds and cursor
    clk = Clock()
    app = make_app(os.path.join(tmp, "ring"), clk, enabled=True, detail=True)
    feed = app.spectrum
    eq("650 standard rows (beam 0, one hop apart) are all accepted", feed_rows(feed, clk, 0, 650), 650)
    ring = feed.rings["standard"]
    eq("the standard ring keeps exactly 600 rows (60 s) and its row index has the same size", (len(ring.slots), len(ring.by_row)), (600, 600))
    j = app.api_spectrum({}, "203.0.113.1", True)
    eq("no cursor: the newest 600 rows, idx 51..650", (len(j["rows"]), j["rows"][0]["idx"], j["rows"][-1]["idx"], j["newest_idx"]), (600, 51, 650, 650))
    j = app.api_spectrum({"since": "640"}, "192.0.2.1", True)
    eq("since=640: exactly rows 641..650", [r["idx"] for r in j["rows"]], list(range(641, 651)))
    j = app.api_spectrum({"since": "650"}, "192.0.2.2", True)
    eq("since=650 (nothing new): no rows, next_since stays 650", (j["rows"], j["next_since"]), ([], 650))
    j = app.api_spectrum({"since": "20"}, "192.0.2.3", True)
    eq("since=20 (older than the ring): rows_missed = 30 (idx 21..50 are gone) and the 600 retained rows are served",
       (j["rows_missed"], len(j["rows"]), j["rows"][0]["idx"]), (30, 600, 51))
    j = app.api_spectrum({"limit": "5"}, "192.0.2.4", True)
    eq("limit=5: the newest 5 rows (646..650), never more than the limit", [r["idx"] for r in j["rows"]], [646, 647, 648, 649, 650])
    j = app.api_spectrum({"since": "9999"}, "192.0.2.5", True)
    eq("a cursor from a newer (previous) server run restarts at the newest rows instead of stalling", (len(j["rows"]), j["rows"][-1]["idx"]), (600, 650))
    clk2 = Clock()
    app_d = make_app(os.path.join(tmp, "ringd"), clk2, enabled=True, detail=True)
    eq("1100 detail rows (20 ms apart) are all accepted", feed_rows(app_d.spectrum, clk2, 0, 1100, tier=1), 1100)
    eq("the detail ring keeps exactly 1000 rows (20 s)", len(app_d.spectrum.rings["detail"].slots), 1000)
    j = app_d.api_spectrum({"tier": "detail"}, "127.0.0.1", True)
    eq("detail request without limit: 500 rows (the detail cap), 100 bins, schema-valid", (len(j["rows"]), j["n_bins"], schema_errors("spectrum", j)), (500, 100, []))

    # -------------------------------------------------------------------------------------------- producer restart continues idx
    clk = Clock()
    app = make_app(os.path.join(tmp, "restart"), clk, enabled=True)
    feed = app.spectrum
    feed_rows(feed, clk, 0, 30)
    feed_rows(feed, clk, 0, 5)
    j = app.api_spectrum({}, "203.0.113.1", True)
    eq("a producer restart (row counter back to 0 while time moves on) continues idx: 1..30 then 31..35", [r["idx"] for r in j["rows"]], list(range(1, 36)))
    feed_rows(feed, clk, 36, 1)
    eq("a duplicate of a row already in the ring is dropped", feed.handle(pkt(0, 0, 3, clk.t, fine_bins(3))), False)
    eq("a late row of a window that was never opened (older time, lower row number) is dropped, not inserted out of order",
       (feed.handle(pkt(0, 0, 20, clk.t - 100, fine_bins(20))), len(feed.rings["standard"].slots)), (False, 36))

    # -------------------------------------------------------------------------------------------- rate caps (packets and requests)
    clk = Clock()
    app = make_app(os.path.join(tmp, "caps"), clk, enabled=True, detail=True)
    feed = app.spectrum
    acc = sum(bool(feed.handle(pkt(0, 0, r, clk.t, fine_bins(r)))) for r in range(100))
    eq("a flood of 100 standard rows in one instant: only the burst of 10 (1 s of rows) is accepted, 90 dropped and counted", (acc, feed.rows_dropped_rate), (10, 90))
    clk.t += 10
    acc = sum(bool(feed.handle(pkt(0, 0, r, clk.t, fine_bins(r)))) for r in range(100, 200))
    eq("after a pause the bucket refills only to the burst (10 more, not 100)", acc, 10)
    clk = Clock()
    app = make_app(os.path.join(tmp, "caps2"), clk, enabled=True, detail=True)
    feed = app.spectrum
    eq("paced at one hop (10 rows/s): 30 of 30 accepted", feed_rows(feed, clk, 0, 30), 30)
    eq("a flood of 100 detail rows in one instant: burst of 50 (1 s of rows) accepted", sum(bool(feed.handle(pkt(1, 0, r, clk.t, fine_bins(r, 1)))) for r in range(100)), 50)
    clk.t += 100
    eq("detail paced at 20 ms (50 rows/s) is accepted in full", feed_rows(feed, clk, 200, 100, tier=1), 100)
    clk.t += 100
    eq("detail at 100 rows/s (10 ms apart) is cut: fewer than 100 of 100 are accepted", feed_rows(feed, clk, 400, 100, tier=1, step=0.01) < 100, True)
    # a producer stall followed by a flush of the rows that piled up: up to 1 s of catch-up is kept, more is cut (the sustained cap is unchanged)
    for tier, rate in ((0, 10), (1, 50)):
        for stall_s, want_ok in ((0.3, int(rate * 0.3)), (1.0, rate), (2.0, rate)):
            clk = Clock()
            app = make_app(os.path.join(tmp, "stall%d_%g" % (tier, stall_s)), clk, enabled=True, detail=True)
            feed = app.spectrum
            paced = feed_rows(feed, clk, 0, 2 * rate, tier=tier)
            clk.t += stall_s
            n = int(rate * stall_s)
            flushed = sum(bool(feed.handle(pkt(tier, 0, 1000 + r, clk.t, fine_bins(r, tier)), "127.0.0.1")) for r in range(n))
            eq("%s tier: paced rows all accepted (%d), then a %g s stall and a flush of %d rows at once: %d accepted (%s)" % (("standard", "detail")[tier], 2 * rate, stall_s, n, want_ok, "all kept" if n <= rate else "beyond 1 s cut"),
               (paced, flushed), (2 * rate, want_ok))
    # request token bucket
    clk = Clock()
    app = make_app(os.path.join(tmp, "req"), clk, enabled=True)
    codes = []
    for i in range(12):
        try:
            app.api_spectrum({}, "198.51.100.7", True)
            codes.append(200)
        except U.Refuse as e:
            codes.append((e.code, e.name, e.extra))
    eq("requests: the first 10 (burst) are served, the 11th and 12th get 429 rate_limited with Retry-After 1", (codes[:10] == [200] * 10, codes[10], codes[11]),
       (True, (429, "rate_limited", {"Retry-After": "1"}), (429, "rate_limited", {"Retry-After": "1"})))
    app.api_spectrum({}, "198.51.100.8", True)
    check("positive twin: another client address is not affected by the first one's limit", True)
    clk.t += 1.0
    n = 0
    for i in range(7):
        try:
            app.api_spectrum({}, "198.51.100.7", True)
            n += 1
        except U.Refuse:
            pass
    eq("after 1 s the first address has 5 tokens back: 5 of the next 7 are served (sustained rate 5/s)", n, 5)
    codes = []
    for peer in ["::ffff:198.51.100.207"] * 5 + ["198.51.100.207"] * 5 + ["198.51.100.207", "::ffff:198.51.100.207"]:
        try:
            app.api_spectrum({}, peer, True)
            codes.append(200)
        except U.Refuse as e:
            codes.append(e.code)
    eq("an IPv4-mapped IPv6 peer and the plain IPv4 address are one client: 10 served, then 429 for both spellings", codes, [200] * 10 + [429, 429])
    lim = U.RequestLimiter(max_addrs=256)
    for i in range(300):
        lim.allow("10.1.%d.%d" % (i // 250, i % 250), 0.0)
    eq("at most 256 client addresses are tracked (least recently seen evicted)", len(lim.b), 256)

    # -------------------------------------------------------------------------------------------- tier enforcement (direct)
    clk = Clock()
    app = make_app(os.path.join(tmp, "tier"), clk, enabled=True, detail=True)
    feed_rows(app.spectrum, clk, 0, 5, tier=1)
    feed_rows(app.spectrum, clk, 0, 5, tier=0)

    def ask(q, peer, authed=True):
        try:
            return 200, app.api_spectrum(q, peer, authed)
        except U.Refuse as e:
            return e.code, e.name

    for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "198.51.100.50", "192.0.2.103", "203.0.113.5", "::ffff:198.51.100.50", "fd00::1", "not-an-address", ""):
        s, j = ask({"tier": "detail"}, peer)
        eq(f"detail tier: authenticated peer {peer!r} (loopback or not: the network no longer matters) + flag -> 200 with 100-bin rows", (s, j["state"], j["n_bins"], len(j["rows"]), schema_errors("spectrum", j)), (200, "live", 100, 5, []))
    s, j = ask({"tier": "standard"}, "198.51.100.50")
    eq("the standard tier is served to an authenticated LAN client", (s, len(j["rows"])), (200, 5))
    s, j = ask({}, "198.51.100.50")
    eq("the default tier is standard: 46 bins", (s, j["tier"], j["n_bins"]), (200, "standard", 46))
    s, j = ask({}, "198.51.100.51")
    check("a standard response never carries detail rows (tier standard, 46 bins, rows_per_s 10, hop 100 ms)", j["tier"] == "standard" and all(len(decode(r)) == 46 for r in j["rows"]) and j["rows_per_s"] == 10 and j["hop_ms"] == 100, str(j["tier"]))
    app_nd = make_app(os.path.join(tmp, "nodetail"), Clock(), enabled=True, detail=False)
    for peer in ("127.0.0.1", "198.51.100.50"):
        try:
            app_nd.api_spectrum({"tier": "detail"}, peer, True)
            r = None
        except U.Refuse as e:
            r = (e.code, e.name)
        eq(f"without --spectrum-detail the detail tier is refused for {peer} too: 403 spectrum_detail_disabled", r, (403, "spectrum_detail_disabled"))
    clk_d = Clock()
    app_nd.spectrum.handle(pkt(1, 0, 0, clk_d.t, fine_bins(0, 1)))
    eq("detail datagrams are not even stored when the flag is off", (len(app_nd.spectrum.rings["detail"].slots), app_nd.spectrum.detail_ignored), (0, 1))

    # -------------------------------------------------------------------------------------------- compare levels
    clk = Clock()
    app = make_app(os.path.join(tmp, "cmp"), clk, enabled=True)
    for beam in range(9):
        bins = [0] * 46
        if beam in (1, 4):
            for i in range(10, 22):
                bins[i] = 100                                         # -50 dB in the 12 bands of 2-4 kHz
        app.spectrum.handle(pkt(0, beam, 0, clk.t, bins))
    j = app.api_spectrum({"compare": "true", "band": "5", "beam": "0"}, "203.0.113.1", True)
    eq("compare, band 5 (2-4 kHz): levels = power sum of 12 bands at -50 dB = -39.2 dB -> 122 for beams 1 and 4; the other beams hold 12 bands at the -100 dB floor = -89.2 dB -> 22",
       j["rows"][0]["levels"], [122, 22, 22, 122, 22, 22, 22, 22])
    eq("compare payload validates", schema_errors("spectrum", j), [])
    j = app.api_spectrum({"compare": "false", "beam": "0"}, "203.0.113.2", True)
    check("positive twin: without compare there is no levels field", "levels" not in j["rows"][0])

    # -------------------------------------------------------------------------------------------- tag latch
    clk = Clock(1_800_000_100.0)
    app = make_app(os.path.join(tmp, "latch"), clk, enabled=True)
    feed = app.spectrum
    t_first = clk.t
    feed_rows(feed, clk, 0, 30, beams=(0, 1))                          # 3 blocks of 10 rows, 1 s each: row i is at t_first + 0.1 i
    before = app.api_spectrum({}, "203.0.113.1", True)
    rev0 = before["rev"]
    eq("before any tag: rev 0, no gated row", (rev0, before["gated_now"], any(r["gated"] for r in before["rows"])), (0, False, False))
    app._tag({"k": "tags", "t": t_first + 14.0, "beam": 1, "speech_p": 0.01, "speech_active": False})
    check("a tag with speech_active false does not touch the ring (positive twin of the latch)", feed.rings["standard"].rev == 0 and not feed.windows)
    app._tag({"k": "tags", "t": t_first + 1.95, "beam": 1, "speech_p": 0.9, "speech_active": True})          # window (t-5, t) covers rows at 0.0 .. 1.9 s
    j = app.api_spectrum({"since": "30", "rev": "0"}, "203.0.113.2", True)
    want0 = R.coarsen_block(0, [fine_bins(i) for i in range(0, 10)])
    want1 = R.coarsen_block(0, [fine_bins(i) for i in range(10, 20)])
    after = app.api_spectrum({}, "203.0.113.3", True)
    sel = {r["idx"]: r for r in after["rows"]}
    eq("a speech_active tag over (t-5, t) covering rows at 0.0..1.9 s gates blocks 0 and 1 (idx 1..20) and bumps rev to 1", (after["rev"], sorted(i for i, r in sel.items() if r["gated"])), (1, list(range(1, 21))))
    eq("the gated rows equal R.coarsen_block of their own block's fine rows (per block), flag 1", [decode(sel[i]) for i in range(1, 21)], [want0] * 10 + [want1] * 10)
    check("block 2 (idx 21..30, after the window) is unchanged and not gated (positive twin)", all(not sel[i]["gated"] and decode(sel[i]) == fine_bins(i - 1) for i in range(21, 31)))
    eq("a client that already has everything (since=30) receives exactly the rewritten rows 1..20 when it sends rev=0", [r["idx"] for r in j["rows"]], list(range(1, 21)))
    eq("... and nothing when it sends the new rev=1 (positive twin: no spurious rewrite)", app.api_spectrum({"since": "30", "rev": "1"}, "203.0.113.4", True)["rows"], [])
    eq("beam 2's rows (second stream) are gated too, same rule", [r["gated"] for r in app.api_spectrum({"beam": "1"}, "203.0.113.5", True)["rows"][10:20]], [True] * 10)
    eq("the schema accepts the revised payload", schema_errors("spectrum", after), [])
    app._tag({"k": "tags", "t": t_first + 11.0, "beam": 2, "speech_p": 0.9, "speech_active": True})
    eq("the same tag again changes nothing: rev stays 1 (idempotent)", feed.rings["standard"].rev, 1)
    # a row that arrives AFTER its tag is gated on arrival
    feed_rows(feed, clk, 30, 1, beams=(0,))
    app._tag({"k": "tags", "t": clk.t + 4.0, "beam": 1, "speech_p": 0.9, "speech_active": True})
    feed_rows(feed, clk, 31, 3, beams=(0,))
    last = app.api_spectrum({}, "203.0.113.6", True)["rows"][-1]
    check("a row arriving after its tag window opened is gated on arrival", last["gated"] is True, str(last["flags"]))
    # an old tag (backfill of an old file) does nothing
    rev_before, win_before = feed.rings["standard"].rev, len(feed.windows)
    app._tag({"k": "tags", "t": clk.t - 86400, "beam": 1, "speech_p": 0.9, "speech_active": True})
    eq("a day-old tag (tags.jsonl backfill at start) neither touches the ring nor is remembered as a latch window", (feed.rings["standard"].rev, len(feed.windows)), (rev_before, win_before))
    app._tag({"k": "tags", "t": clk.t, "beam": 1, "speech_p": 0.9, "speech_active": True})
    eq("positive twin: a current tag is remembered as a latch window", len(feed.windows), win_before + 1)
    # the real tailer wires the latch: a tags.jsonl line written to the file the dashboard reads
    clk = Clock(1_800_000_200.0)
    app = make_app(os.path.join(tmp, "latch2"), clk, enabled=True)
    t0 = clk.t
    feed_rows(app.spectrum, clk, 0, 20, beams=(0,))
    with open(app.paths["tags"], "ab") as f:
        f.write((json.dumps({"k": "tags", "t": t0 + 0.55, "beam": 3, "speech_p": 0.5, "speech_active": True, "top": [["Speech", 0.5]]}) + "\n").encode())
    app.tailers["tags"].backfill(1 << 20)
    j = app.api_spectrum({}, "203.0.113.1", True)
    eq("the tags.jsonl tailer drives the latch: block 0 (rows 1..10) gated, block 1 not", ([r["gated"] for r in j["rows"][:10]], [r["gated"] for r in j["rows"][10:]]), ([True] * 10, [False] * 10))

    # -------------------------------------------------------------------------------------------- privacy
    clk = Clock()
    app = make_app(os.path.join(tmp, "priv"), clk, enabled=True)
    feed_rows(app.spectrum, clk, 0, 100)
    j = app.api_spectrum({"compare": "true"}, "203.0.113.1", True)
    allowed = set(SCH["$defs"]["spectrum"]["properties"])
    check("the response has only the contract's fields (no samples, pcm or phase field anywhere)", set(j) <= allowed and all(set(r) <= {"idx", "t_s", "flags", "gated", "bins", "levels"} for r in j["rows"]), str(set(j) - allowed))
    check("every row carries exactly n_bins (46) one-byte values: 46 B per 4800 samples", all(len(decode(r)) == 46 for r in j["rows"]))
    per_s = len(j["rows"]) / (j["rows"][-1]["t_s"] - j["rows"][0]["t_s"] + 0.1)
    check("information rate in the payload: 10 rows/s x 46 values (<= 460 values/s per beam)", abs(per_s - 10.0) < 1e-6, str(per_s))
    pcm = b"BMAA" + bytes([1, 0, 0, 0]) + (1).to_bytes(4, "little") * 2 + (256).to_bytes(4, "little") + bytes(1024)
    n_slots = len(app.spectrum.rings["standard"].slots)
    eq("an audio (BMAA) datagram sent to the spectrum socket is rejected and counted as a bad packet, nothing stored",
       (app.spectrum.handle(pcm), app.spectrum.bad_packets, len(app.spectrum.rings["standard"].slots)), (False, 1, n_slots))
    n0 = app.spectrum.rows_received
    eq("a row from a non-loopback source address is dropped and counted (positive twin: the same bytes from 127.0.0.1 are accepted)",
       (app.spectrum.handle(pkt(0, 2, 5000, clk.t, fine_bins(1)), "198.51.100.9"), app.spectrum.foreign, app.spectrum.rows_received - n0), (False, 1, 0))
    clk.t += 1
    check("positive twin: the same datagram from 127.0.0.1 is accepted", app.spectrum.handle(pkt(0, 2, 5000, clk.t, fine_bins(1)), "127.0.0.1") is True)
    check("a gated datagram whose values differ inside a coarse group is rejected (the receiver trusts gated rows to be coarse)",
          app.spectrum.handle(pkt(0, 0, 6000, clk.t, fine_bins(6000)), "127.0.0.1") is True and app.spectrum.handle(bytes.fromhex(next(v["hex"] for v in ex["invalid"] if v["code"] == "gated_not_coarse"))) is False)

    # -------------------------------------------------------------------------------------------- the real socket
    clk = time.time
    feed = U.SpectrumFeed(enabled=True, detail=False)
    port = feed.start(0)
    try:
        eq("the receiver binds 127.0.0.1 only", feed.sock.getsockname()[0], "127.0.0.1")
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for beam in range(9):
            tx.sendto(pkt(0, beam, 0, time.time(), fine_bins(0)), ("127.0.0.1", port))
        end = time.time() + 5
        while time.time() < end and feed.rows_received < 9:
            time.sleep(0.02)
        eq("9 datagrams (one row, 9 beam streams) sent over real UDP are received into one ring slot", (feed.rows_received, len(feed.rings["standard"].slots)), (9, 1))
        tx.sendto(b"garbage", ("127.0.0.1", port))
        end = time.time() + 5
        while time.time() < end and feed.bad_packets < 1:
            time.sleep(0.02)
        eq("garbage over real UDP is counted, not stored", (feed.bad_packets, len(feed.rings["standard"].slots)), (1, 1))
    finally:
        feed.close()

    # -------------------------------------------------------------------------------------------- over HTTP: peer hook, headers, 422, flags
    import test_bma_ui as T  # noqa: E402  (Client helper)
    clk = Clock(time.time())
    app = make_app(os.path.join(tmp, "http"), clk, enabled=True, detail=True)
    feed_rows(app.spectrum, clk, 0, 5, tier=0)
    feed_rows(app.spectrum, clk, 0, 5, tier=1)
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), auth=A.Authenticator(TOKEN))     # the real login: every request below carries the right token
    port = srv.server_address[1]
    srv.allowed_hosts = U.allowed_hosts_for(port, ["ui.example"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class AuthClient(T.Client):
        def req(self, method, path, host="default", headers=None):
            return super().req(method, path, host=host, headers=dict(AUTH_HEADER, **(headers or {})))
    c = AuthClient(srv)
    s, h, j = c.get("/api/spectrum")
    eq("HTTP: GET /api/spectrum is 200 JSON, no-store, schema-valid", (s, h["content-type"], h["cache-control"], schema_errors("spectrum", j)), (200, "application/json", "no-store", []))
    check("HTTP: the CSP header is on the spectrum response too", "default-src 'self'" in h["content-security-policy"])
    s, h, j = c.get("/api/spectrum?tier=detail", host=f"127.0.0.1:{port}")
    eq("HTTP: detail tier, authenticated, flag on -> 200, 100 bins", (s, j and j["n_bins"]), (200, 100))
    orig = U.Handler.peer_ip
    U.Handler.peer_ip = lambda self: "198.51.100.50"                       # the client-address hook: this connection now looks like a LAN client
    try:
        s, h, j = c.get("/api/spectrum?tier=detail", host=f"ui.example:{port}", headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1"})
        eq("HTTP, LAN peer hook, Host ui.example, forwarding headers: authenticated detail is served (the network and headers play no part)", (s, j["tier"], j["n_bins"]), (200, "detail", 100))
        s, h, j = c.get("/api/spectrum?beam=0", host=f"ui.example:{port}")
        eq("HTTP, LAN peer hook: the standard tier is served (46 bins)", (s, j["tier"], j["n_bins"]), (200, "standard", 46))
        s, h, j = c.get("/api/spectrum?tier=standard", host=f"ui.example:{port}")
        eq("HTTP, same LAN peer: a second standard request is served (3 of the burst of 10 used)", s, 200)
        got = []
        for i in range(10):                                                # 7 tokens are left: then 429
            s, h, j = c.get("/api/spectrum", host=f"ui.example:{port}")
            got.append((s, h.get("retry-after")))
        eq("HTTP: after the burst the client gets 429 with Retry-After: 1 and a problem body", (got[-1], j["code"]), ((429, "1"), "rate_limited"))
    finally:
        U.Handler.peer_ip = orig
    for q in ("tier=bogus", "beam=9", "beam=-1", "limit=601", "limit=0", "since=abc", "compare=yes", "band=8", "x=1", "beam=1&beam=2", "tier=detail&limit=501"):
        s, h, j = c.get("/api/spectrum?" + q, host=f"127.0.0.1:{port}")
        eq(f"HTTP: ?{q} is 422 invalid_parameter", (s, j["code"]), (422, "invalid_parameter"))
    s, h, j = c.get("/api/spectrum?limit=600&beam=8&band=7&since=0&rev=0&compare=true", host=f"127.0.0.1:{port}")
    eq("HTTP: positive twin, every parameter at its bound is accepted", s, 200)
    s, h, b = c.req("POST", "/api/spectrum")
    eq("HTTP: POST is still 405", s, 405)
    s, h, j = c.get("/api/status")
    eq("HTTP: /api/status carries status.spectrum (enabled, detail_enabled, state live)", (j["spectrum"]["enabled"], j["spectrum"]["detail_enabled"], j["spectrum"]["state"]), (True, True, "live"))
    eq("UI_VERSION is 1.13.0", U.UI_VERSION, "1.13.0")
    eq("the two new assets are routed with the JavaScript content type (exact names, no wildcard)",
       (U.ASSETS["/static/tip.js"], U.ASSETS["/static/spectrum.js"]), (("tip.js", "text/javascript; charset=utf-8"), ("spectrum.js", "text/javascript; charset=utf-8")))
    eq("the asset list is exactly the old six plus the three of 1.7.0 (session.js, login.js, login.css) and the two of 1.8.0 (review.js, review.css)", sorted(U.ASSETS), ["/", "/index.html", "/static/app.css", "/static/app.js", "/static/login.css", "/static/login.js", "/static/review.css", "/static/review.js", "/static/session.js", "/static/spectrum.js", "/static/tip.js"])
    s, h, b = c.req("GET", "/static/tip.js?x=1")
    eq("an asset with a query string is still 404", s, 404)
    s, h, b = c.req("GET", "/static/tip.js/")
    eq("an asset with a trailing slash is still 404", s, 404)
    srv.shutdown()

    # -------------------------------------------------------------------------------------------- authentication gates the spectrum, not the network (ui-http 1.6.0)
    eq("spectrum_client_allowed: unauthenticated is refused for both tiers, with or without the detail flag (reason spectrum_auth_required)",
       [U.spectrum_client_allowed(False, t, f) for t in ("standard", "detail") for f in (False, True)], [(False, "spectrum_auth_required")] * 4)
    eq("spectrum_client_allowed: authenticated standard is allowed with or without the detail flag (positive twin)",
       [U.spectrum_client_allowed(True, "standard", f) for f in (False, True)], [(True, None)] * 2)
    eq("spectrum_client_allowed: authenticated detail needs the flag (spectrum_detail_disabled without it, allowed with it)",
       [U.spectrum_client_allowed(True, "detail", False), U.spectrum_client_allowed(True, "detail", True)], [(False, "spectrum_detail_disabled"), (True, None)])
    clk = Clock()
    app_a = make_app(os.path.join(tmp, "auth"), clk, enabled=True, detail=True)
    feed_rows(app_a.spectrum, clk, 0, 5, tier=0)
    feed_rows(app_a.spectrum, clk, 0, 5, tier=1)
    for tier in ("standard", "detail"):
        for peer in ("127.0.0.1", "::1", "198.51.100.50", ""):
            j = app_a.api_spectrum({"tier": tier, "beam": "3", "compare": "true"}, peer, False)
            eq(f"unauthenticated, {tier} tier, peer {peer!r} (even loopback): state auth_required, no rows, no bands, no cursor, no gate state",
               (j["state"], j["rows"], j["n_bins"], j["edges_hz"], j["rev"], j["newest_idx"], j["next_since"], j["rows_missed"], j["newest_age_s"], j["gated_now"], j["tier"], j["beam"]),
               ("auth_required", [], 0, [], 0, 0, 0, 0, None, False, tier, 3))
    j = app_a.api_spectrum({}, "198.51.100.50")
    eq("the default for the authenticated flag is False: api_spectrum(q, peer) alone answers auth_required", j["state"], "auth_required")
    ar_fx = json.load(open(os.path.join(FIX, "ui_spectrum_auth_required.json")))
    eq("the auth_required payload validates against the schema and equals the contract fixture ui_spectrum_auth_required.json (now_s aside)",
       (schema_errors("spectrum", j), dict(j, now_s=0)), ([], dict(ar_fx, now_s=0)))
    for peer in ("127.0.0.1", "198.51.100.50"):
        j = app_a.api_spectrum({}, peer, True)
        eq(f"authenticated standard, peer {peer!r}: 5 live rows (loopback and LAN alike)", (j["state"], len(j["rows"]), j["n_bins"]), ("live", 5, 46))
    app_nd2 = make_app(os.path.join(tmp, "nodetail2"), Clock(), enabled=True, detail=False)
    for peer in ("127.0.0.1", "198.51.100.50"):
        try:
            app_nd2.api_spectrum({"tier": "detail"}, peer, True)
            r = None
        except U.Refuse as e:
            r = (e.code, e.name)
        eq(f"authenticated detail without --spectrum-detail, peer {peer!r}: 403 spectrum_detail_disabled", r, (403, "spectrum_detail_disabled"))
    j = app_nd2.api_spectrum({"tier": "detail"}, "127.0.0.1", False)
    eq("unauthenticated detail without the flag: auth_required (the configuration is not revealed before sign-in)", j["state"], "auth_required")
    app_dis = make_app(os.path.join(tmp, "dis"), Clock(), enabled=False)
    eq("a disabled feed answers disabled even to an unauthenticated client (nothing to hide)", app_dis.api_spectrum({}, "198.51.100.50", False)["state"], "disabled")
    eq("status.spectrum describes the feed, never the client: state live, never auth_required", app_a.api_status({})["spectrum"]["state"], "live")
    # ---- request_authenticated and the Handler hook, over HTTP
    class FakeServer:
        pass

    class FakeHandler:
        pass
    fs_, fh_ = FakeServer(), FakeHandler()
    eq("request_authenticated: False with no auth layer, whatever the request attribute says",
       (U.request_authenticated(fh_), (setattr(fh_, "authenticated", True), setattr(fh_, "server", fs_), U.request_authenticated(fh_))[2]), (False, False))
    fs_.auth = A.Authenticator(TOKEN)
    eq("request_authenticated: True only with server.auth AND handler.authenticated is True; a truthy non-True value or False is not enough",
       [U.request_authenticated(fh_), (setattr(fh_, "authenticated", 1), U.request_authenticated(fh_))[1], (setattr(fh_, "authenticated", False), U.request_authenticated(fh_))[1]], [True, False, False])
    eq("Handler.authenticated defaults to False (the auth layer sets it per request)", U.Handler.authenticated, False)
    clk_h = Clock(time.time())
    app_h = make_app(os.path.join(tmp, "http_auth"), clk_h, enabled=True, detail=True)
    feed_rows(app_h.spectrum, clk_h, 0, 5, tier=0)
    feed_rows(app_h.spectrum, clk_h, 0, 5, tier=1)
    srv_h = U.Server(("127.0.0.1", 0), app_h, set(), U.load_assets())      # no login configured: server.auth is auth.OPEN
    port_h = srv_h.server_address[1]
    srv_h.allowed_hosts = U.allowed_hosts_for(port_h, ["ui.example"])
    threading.Thread(target=srv_h.serve_forever, daemon=True).start()
    ch = T.Client(srv_h)
    s, h, j = ch.get("/api/spectrum", host=f"127.0.0.1:{port_h}")
    eq("HTTP, server without a login (auth.OPEN), real loopback connection: 200 auth_required, no rows, schema-valid, no-store", (s, j["state"], j["rows"], schema_errors("spectrum", j), h["cache-control"]), (200, "auth_required", [], [], "no-store"))
    s, h, j = ch.get("/api/spectrum?tier=detail", host=f"127.0.0.1:{port_h}")
    eq("HTTP, server without a login: the detail tier is auth_required too", (s, j["state"], j["rows"]), (200, "auth_required", []))
    check("the server without a login holds the shared auth.OPEN object (open, never authenticated)", srv_h.auth is A.OPEN and A.OPEN.enabled is False)
    srv_h.auth = A.Authenticator(TOKEN)                                    # now the real login is on (the same server, the same route)
    s, h, j = ch.get("/api/spectrum", host=f"127.0.0.1:{port_h}")
    eq("HTTP, real login on, no credential: 401 unauthorized from the login gate, not a spectrum answer", (s, j["code"]), (401, "unauthorized"))
    orig_peer = U.Handler.peer_ip
    ah = {"headers": AUTH_HEADER}
    try:
        s, h, j = ch.get("/api/spectrum", host=f"127.0.0.1:{port_h}", **ah)
        eq("HTTP, authenticated (Basic, the real token), real loopback peer: 200 live, 5 rows of 46 bins", (s, j["state"], len(j["rows"]), j["n_bins"]), (200, "live", 5, 46))
        U.Handler.peer_ip = lambda self: "198.51.100.50"
        s, h, j = ch.get("/api/spectrum", host=f"ui.example:{port_h}", **ah)
        eq("HTTP, authenticated, simulated LAN peer: the same 5 live rows (the network no longer matters)", (s, j["state"], len(j["rows"])), (200, "live", 5))
        s, h, j = ch.get("/api/spectrum?tier=detail", host=f"ui.example:{port_h}", **ah)
        eq("HTTP, authenticated LAN peer, detail flag on, Host ui.example: 200 detail, 100 bins, 5 rows (no loopback or localhost requirement)", (s, j["tier"], j["n_bins"], len(j["rows"])), (200, "detail", 100, 5))
        app_h.spectrum.detail_enabled = False
        s, h, j = ch.get("/api/spectrum?tier=detail", host=f"ui.example:{port_h}", **ah)
        eq("HTTP, authenticated, detail flag off: 403 spectrum_detail_disabled", (s, j["code"]), (403, "spectrum_detail_disabled"))
        app_h.spectrum.detail_enabled = True
    finally:
        U.Handler.peer_ip = orig_peer
    srv_h.shutdown()
    # ---- flags
    a = U.make_parser().parse_args(["--spectrum-port", "48520", "--spectrum-detail"])
    eq("flags: --spectrum-port and --spectrum-detail only; there is no --spectrum-lan", (a.spectrum_port, a.spectrum_detail, hasattr(a, "spectrum_lan")), (48520, True, False))
    try:
        U.make_parser().parse_args(["--spectrum-port", "48520", "--spectrum-lan"])
        r = "accepted"
    except SystemExit as e:
        r = e.code
    eq("--spectrum-lan is refused by argparse (exit 2): the network is not a gate", r, 2)

    # -------------------------------------------------------------------------------------------- no audio is ever written
    AUDIO_EXT = (".wav", ".flac", ".mp3", ".ogg", ".opus", ".aac", ".m4a", ".aiff", ".aif", ".wma", ".pcm", ".raw", ".sph")
    AUDIO_MAGIC = (b"RIFF", b"fLaC", b"OggS", b"ID3", b"FORM", b"\xff\xfb", b".snd")

    def audio_like(top):
        found = []
        for d, _dirs, files in os.walk(top):
            for fn in files:
                pth = os.path.join(d, fn)
                with open(pth, "rb") as f:
                    head = f.read(4)
                if fn.lower().endswith(AUDIO_EXT) or any(head.startswith(m) for m in AUDIO_MAGIC):
                    found.append(os.path.relpath(pth, top))
        return sorted(found)
    work = os.path.join(tmp, "noaudio")
    os.makedirs(work)
    cwd0 = os.getcwd()
    os.chdir(work)                                                           # a stray relative-path write would land here, inside the scanned tree
    try:
        clk_n = Clock(time.time())
        app_n = make_app(os.path.join(work, "data"), clk_n, enabled=True, detail=True)
        feed_rows(app_n.spectrum, clk_n, 0, 40, tier=0, beams=tuple(range(9)))      # fine rows
        feed_rows(app_n.spectrum, clk_n, 0, 20, tier=1)
        app_n.spectrum.handle(pkt(0, 0, 100, clk_n.t, R.coarsen_block(0, [fine_bins(i) for i in range(10)]), flags=1), "127.0.0.1")
        app_n.spectrum.latch(clk_n.t - 5, clk_n.t)                                   # the tag latch rewrites rows in memory
        for q_ in ({}, {"tier": "detail"}, {"beam": "4", "compare": "true"}):
            app_n.api_spectrum(q_, "127.0.0.1", True)
            clk_n.t += 1
        app_n.start_backfill()
        app_n.housekeeping()
        app_n.api_status({})
        files = sorted(os.path.relpath(os.path.join(d, f), work) for d, _x, fs in os.walk(work) for f in fs)
        eq("after feeding 9 beams of fine and gated rows, the latch, every tier and a housekeeping pass: no audio-like file (extension or magic) exists in the data tree or the working directory",
           audio_like(work), [])
        check("the scan saw the dashboard's own output (the token file and the rollup directory contents are scanned), so the empty result is not an empty scan", len(files) >= 1, str(files))
        with open(os.path.join(work, "planted.dat"), "wb") as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        open(os.path.join(work, "planted2.wav"), "wb").write(b"x")
        eq("positive twin: the same scanner flags a planted RIFF/WAVE file by magic and a .wav by extension", audio_like(work), ["planted.dat", "planted2.wav"])
    finally:
        os.chdir(cwd0)

    # -------------------------------------------------------------------------------------------- flags
    for argv in (["--spectrum-detail"], ["--spectrum-port", "0"], ["--spectrum-port", "70000"], ["--spectrum-port", "x"]):
        try:
            U.main(argv)
            r = "returned"
        except SystemExit as e:
            r = e.code
        eq(f"flags {argv}: argparse refuses (exit 2)", r, 2)
    a = U.make_parser().parse_args([])
    eq("derived defaults: no --spectrum-port and no --spectrum-detail (the feature is off)", (a.spectrum_port, a.spectrum_detail), (None, False))
    a = U.make_parser().parse_args(["--spectrum-port", "48520", "--spectrum-detail"])
    eq("flags parse: --spectrum-port 48520 --spectrum-detail", (a.spectrum_port, a.spectrum_detail), (48520, True))
    app_b = U.build(U.make_parser().parse_args(["--no-board", "--pipeline-root", tmp, "--reports-dir", tmp, "--rollup-dir", tmp]))
    eq("build without the flag: feed disabled", (app_b.spectrum.enabled, app_b.spectrum.sock), (False, None))
    app_b = U.build(U.make_parser().parse_args(["--no-board", "--pipeline-root", tmp, "--reports-dir", tmp, "--rollup-dir", tmp, "--spectrum-port", "48520"]))
    eq("build with --spectrum-port: feed enabled, detail off (positive twin)", (app_b.spectrum.enabled, app_b.spectrum.detail_enabled), (True, False))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
