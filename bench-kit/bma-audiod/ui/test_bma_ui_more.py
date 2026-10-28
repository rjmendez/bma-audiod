#!/usr/bin/env python3
"""Second half of the bma_ui tests: seams between memory and rollups, rollup validation, out-of-region records, DST fall-back,
dead channels, hour boundaries, connection limits, 500 hygiene, token refresh, tailer failure handling. Each section builds its own
small tree so the numbers are easy to verify by hand. Same conventions as test_bma_ui.py (PASS/FAIL lines, a final
'N checks, 0 failure(s)' line, non-zero exit on failure).

    python test_bma_ui_more.py        (imports bma_ui.py from this directory, as test_bma_ui.py does)"""

import json, math, os, shutil, socket, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = HERE
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import bma_ui as U  # noqa: E402
import test_bma_ui as T  # noqa: E402  (helpers only: FakeBoard, Client, bands_rec, bird, jl, constants)

fails, n_checks = [], 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


H0, HC, HO, NOW = T.H0, T.HC, T.HO, T.NOW


def mini_tree(root):
    pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp, os.path.join(root, "rollups")):
        os.makedirs(d, exist_ok=True)
    return pr, rp, os.path.join(root, "rollups")


def beams_rollup(h, n, db, zf=0.0, lead=None, rel=None, nc=0):
    grid = [[db] * 8 for _ in range(8)]
    return {
        "k": "ui_rollup",
        "v": 1,
        "kind": "beams",
        "hour_s": h,
        "windows": n,
        "zero_frac_mean": zf,
        "power_mean_db": grid,
        "max_db": grid,
        "contrast": {"windows": nc, "leader_count": lead or [0] * 8, "mean_rel_ring_db_2_8khz": rel},
    }


def new_app(root, pr, rp, clock, board=None, log=None, window_h=48):
    board = board or U.Board("http://127.0.0.1:9", os.path.join(root, "no_token"), enabled=False)
    return U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=window_h, clock=clock, log=(log if log is not None else []).append)


def serve(app, **kw):
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), **kw)
    srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], [])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, T.Client(srv)


def raw_http(port, data, timeout=5.0):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(data)
    chunks = []
    try:
        while True:
            c = s.recv(65536)
            if not c:
                break
            chunks.append(c)
    except socket.timeout:
        pass
    s.close()
    return b"".join(chunks)


def section_rollup_validation(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    good = beams_rollup(HC, 5, -40.0)
    lines = [
        json.dumps(good).encode(),
        b'{"k":"ui_rollup","v":1,"kind":"beams","hour_s":1e999}',  # int(inf) -> OverflowError
        b'{"k":"ui_rollup","v":1,"kind":"beams","hour_s":NaN}',
        json.dumps(dict(good, hour_s=HC + 1)).encode(),  # not a multiple of 3600
        json.dumps(dict(good, hour_s=float(HO))).encode(),  # a float hour
        json.dumps(dict(good, hour_s=True)).encode(),
        b'{"k":"ui_rollup","v":1,"kind":"beams","hour_s":%d}' % HO,  # header only
        json.dumps({"k": "ui_rollup", "v": 1, "kind": "detections", "hour_s": HO, "species": "notalist"}).encode(),
        json.dumps({"k": "ui_rollup", "v": 1, "kind": "stream", "hour_s": HO}).encode(),  # bare stream
        json.dumps(dict(good, hour_s=HO, power_mean_db=[[0] * 8] * 7)).encode(),  # wrong grid
        json.dumps(dict(good, hour_s=HO, windows=0)).encode(),
    ]
    with open(os.path.join(rd, "rollup_20261025.jsonl"), "wb") as f:
        f.write(b"\n".join(lines) + b"\n")
    log = []
    app = new_app(root, pr, rp, lambda: HO + 600, log=log)
    app.start_backfill()
    eq(
        "rollup validation: only the one well-formed record is kept, ten malformed lines are counted as garbage, none is fatal",
        (sorted(app.rollups.recs), app.rollups.garbage, app.rollups.load_errors),
        ([("beams", HC)], 10, 0),
    )
    srv, c = serve(app)
    s, h, j = c.get(f"/api/beams/stats?since={HC}&until={HO + 3600}&band=5")
    eq(
        "rollup validation: stats over a range covering the bad hours answer 200 from the good hour (no 500)",
        (s, j["windows"], j["hours"], j["beams"][0]["power_mean_db"]),
        (200, 5, 1, -40.0),
    )
    s, h, j = c.get(f"/api/detections/species?since={HC}&until={HO + 3600}")
    eq("rollup validation: species over the same range answer 200", (s, j["species"]), (200, []))
    srv.shutdown()
    # a load that fails outright must not stop the tailers (a recording stub for the failing step)
    calls = []

    def boom():
        calls.append("load")
        raise RuntimeError("disk gone")

    jl_birds = os.path.join(pr, "analysis", "birds.jsonl")
    T.jl(jl_birds, [T.bird(HC + 100, 2, 0.8, "Alpha Wren", "Aus a")])
    log2 = []
    app2 = new_app(root, pr, rp, lambda: HO + 600, log=log2)
    app2.rollups.load = boom
    app2.start_backfill()
    eq(
        "rollup load failure: the stub ran once, the failure is counted and logged by class only",
        (calls, app2.rollups.load_errors, log2),
        (["load"], 1, ["rollup load failed: RuntimeError"]),
    )
    eq(
        "rollup load failure: the live tailers still ingested the bird and backfill is reported done",
        (len(app2.dets.snapshot()), app2.backfill_done),
        (1, True),
    )
    app3 = new_app(root, pr, rp, lambda: HO + 600)
    app3.start_backfill()
    eq("rollup load success (positive twin): no load error, same bird ingested", (app3.rollups.load_errors, len(app3.dets.snapshot())), (0, 1))
    shutil.rmtree(root, ignore_errors=True)


def section_seams(base):
    """memory vs rollup: same hour in both, they disagree on purpose; hour flooring; exact boundaries; weighting by n."""
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    bb = os.path.join(rp, "beam_bands.jsonl")
    T.jl(bb, [T.bands_rec(HC + 100, 0), T.bands_rec(HC + 101, 2), T.bands_rec(HC + 102, None)])
    with open(os.path.join(rd, "rollup_20261025.jsonl"), "wb") as f:  # disk says hour HC had 7 windows at -50 dB
        f.write(json.dumps(beams_rollup(HC, 7, -50.0)).encode() + b"\n")
        f.write(json.dumps(beams_rollup(H0, 7, -50.0)).encode() + b"\n")  # H0: nothing in memory
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app)
    s, h, j = c.get(f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5")
    eq(
        "seam: hour held in memory wins over a disagreeing rollup, no double count (3 windows, beam 1 -14.69 dB)",
        (j["windows"], j["hours"], j["beams"][0]["power_mean_db"]),
        (3, 1, -14.69),
    )
    s, h, j = c.get(f"/api/beams/stats?since={H0}&until={H0 + 3600}&band=5")
    eq("seam: positive twin - an hour NOT in memory comes from its rollup (7 windows at -50.0)", (j["windows"], j["beams"][0]["power_mean_db"]), (7, -50.0))
    s, h, j = c.get(f"/api/beams/stats?since={HC}&until={HC + 7200}&band=5")
    eq("seam: a range spanning a memory hour and the next (empty) hour keeps one hour", (j["windows"], j["hours"]), (3, 1))
    s, h, j = c.get(f"/api/beams/stats?since={HC + 1800}&until={HC + 2400}&band=5")
    eq(
        "hour rule: since in the middle of an hour still returns that whole hour, and says so (effective bounds)",
        (j["windows"], j["effective_since_s"], j["effective_until_s"]),
        (3, HC, HC + 3600),
    )
    s, h, j = c.get(f"/api/beams/stats?since={HC}&until={HC + 3700}&band=5")
    eq(
        "hour rule: until 100 s into the next hour extends the effective end to the end of that hour",
        (j["effective_since_s"], j["effective_until_s"]),
        (HC, HC + 7200),
    )
    s, h, j = c.get(f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5")
    eq("hour rule: positive twin - an until exactly on an hour start ends there", (j["effective_since_s"], j["effective_until_s"]), (HC, HC + 3600))
    srv.shutdown()
    eq(
        "hour_of: an exact hour start is its own hour, one second earlier is the previous hour",
        (U.hour_of(HO), U.hour_of(HO - 1), U.hour_of(HO + 3599)),
        (HO, HC, HO),
    )
    eq("ceil_hour: an exact hour is unchanged, one second past rounds up", (U.ceil_hour(HO), U.ceil_hour(HO + 1), U.ceil_hour(HO - 1)), (HO, HO + 3600, HO))
    shutil.rmtree(root, ignore_errors=True)

    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(HO - 1, 0, base=-30.0), T.bands_rec(HO, None, base=-50.0, hot=-50.0)])
    app = new_app(root, pr, rp, lambda: HO + 60)
    app.start_backfill()
    srv, c = serve(app)
    s, h, a = c.get(f"/api/beams/stats?since={HC}&until={HO}&band=5")
    s, h, b = c.get(f"/api/beams/stats?since={HO}&until={HO + 3600}&band=5")
    eq(
        "boundary: the window at HO-1 belongs to hour HC, the window at exactly HO to hour HO",
        (a["windows"], b["windows"], b["beams"][0]["power_mean_db"]),
        (1, 1, -50.0),
    )
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)

    # weighting: two rollup-only hours with different n
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    with open(os.path.join(rd, "rollup_20261025.jsonl"), "wb") as f:
        f.write(json.dumps(beams_rollup(H0, 1, -10.0, zf=0.0)).encode() + b"\n")
        f.write(json.dumps(beams_rollup(H0 + 3600, 3, -30.0, zf=0.4)).encode() + b"\n")
    app = new_app(root, pr, rp, lambda: NOW + 5 * 86400, window_h=1)
    app.start_backfill()
    srv, c = serve(app)
    s, h, j = c.get(f"/api/beams/stats?since={H0}&until={H0 + 7200}&band=5")
    want = round(10 * math.log10((1 * 10**-1.0 + 3 * 10**-3.0) / 4), 2)
    eq(
        "weighting: power mean of two hours is weighted by n (1 window at -10 dB, 3 at -30 dB) = %.2f, not the unweighted -12.97" % want,
        (j["windows"], j["beams"][0]["power_mean_db"], want),
        (4, want, -15.89),
    )
    eq("weighting: zero_frac_mean weighted by n too (0.3, not 0.2)", j["zero_frac_mean"], 0.3)
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_grace():
    bs = U.BeamStore(10 * 86400)
    bs.add(T.bands_rec(HC + 10, 0))
    eq("rollup grace: 60 s after the hour closed nothing is written yet", bs.closed_rollups(HC + 3600 + 60, {}), [])
    eq("rollup grace: 119 s after: still nothing", bs.closed_rollups(HC + 3600 + 119, {}), [])
    r = bs.closed_rollups(HC + 3600 + 120, {})
    eq(
        "rollup grace: exactly 120 s after the hour closed it is written (positive twin), once, with its window count",
        [(x["kind"], x["hour_s"], x["windows"]) for x in r],
        [("beams", HC, 1)],
    )
    eq("rollup grace: an hour already persisted is not produced again", bs.closed_rollups(HC + 9999, {("beams", HC): {}}), [])
    ds = U.DetStore(10 * 86400)
    ds.add(T.bird(HC + 10, 1, 0.5, "A", "A a"))
    eq(
        "rollup grace (detections): 119 s after nothing, 120 s after one record",
        (ds.closed_rollups(HC + 3719, {}, 0), len(ds.closed_rollups(HC + 3720, {}, 0))),
        ([], 1),
    )


def section_dead_beam():
    def window(overrides):
        db = [[-80.0] * 8 for _ in range(8)]
        for (b, k), v in overrides.items():
            db[b][k] = v
        return db

    a = U.BeamAgg()
    a.add(window({(2, 5): -200.0, (2, 6): -200.0}), 0.0)
    eq(
        "dead channel: a window with one beam at the -200 dB floor is not a contrast window (no leader, no bias sample)",
        (a.nc, a.lead, a.rel, a.n),
        (0, [0] * 8, [0.0] * 8, 1),
    )
    b = U.BeamAgg()
    b.add(window({(2, 5): -90.0, (2, 6): -90.0}), 0.0)
    ring_db = 10 * math.log10((7 * 2e-8 + 2e-9) / 8)
    lv0, lv2 = 10 * math.log10(2e-8), 10 * math.log10(2e-9)
    eq(
        "dead channel: positive twin - a live beam 10 dB down IS a contrast window; beam 1 leads (first of the tie); rel-to-ring exact",
        (b.nc, b.lead, round(b.rel[0], 2), round(b.rel[2], 2)),
        (1, [1, 0, 0, 0, 0, 0, 0, 0], round(lv0 - ring_db, 2), round(lv2 - ring_db, 2)),
    )
    c = U.BeamAgg()
    c.add(window({(0, 5): -77.0, (0, 6): -77.0}), 0.0)  # +3.0 dB nominal: one beam 3 dB above the rest
    d = U.BeamAgg()
    d.add(window({(0, 5): -77.5, (0, 6): -77.5}), 0.0)  # +2.5 dB: below the 3 dB threshold
    eq("contrast threshold: 3.0 dB spread counts, 2.5 dB does not", (c.nc, d.nc), (1, 0))
    e = U.BeamAgg()
    e.add(window({(0, 5): -78.0, (0, 6): -78.0}), 0.0)  # 2.0 dB
    f = U.BeamAgg()
    f.add(window({(0, 5): -76.9, (0, 6): -76.9}), 0.0)  # 3.1 dB
    eq("contrast threshold: 2.0 dB no, 3.1 dB yes", (e.nc, f.nc), (0, 1))


def section_birds(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T0 = HC + 1000
    real_filtered = {
        "k": "bird_filtered",
        "t": T0,
        "beam": 2,
        "level_dbfs": -60.2,
        "common": "Varied Thrush",
        "scientific": "Ixoreus naevius",
        "confidence": 0.2808,
        "in_region": False,
        "provenance": "model_opinion",
    }  # shape copied from the real birds.jsonl
    recs = [
        real_filtered,
        dict(real_filtered, t=T0 + 1, beam=3, confidence=0.4918),  # same call on another beam
        {
            "k": "bird",
            "t": T0 + 100,
            "beam": 5,
            "level_dbfs": -56.9,
            "common": "Engine",
            "scientific": "Engine",
            "confidence": 0.4394,
            "provenance": "model_opinion",
            "non_species": True,
        },
        {
            "k": "bird",
            "t": T0 + 200,
            "beam": 4,
            "level_dbfs": -61.0,
            "common": "Null Nuthatch",
            "scientific": "Sitta nulla",
            "confidence": 0.61,
            "provenance": "model_opinion",
        },
        T.bird(T0 + 300, 1, 0.9, "True Thrush", "Turdus verus"),
        {k: v for k, v in dict(real_filtered, t=T0 + 400, beam=6, common="Keyless Kite", scientific="Milvus nokey").items() if k != "in_region"},
        dict(real_filtered, t=T0 + 500, beam=7, in_region=True, common="Wrong Wren", scientific="Troglodytes wrong"),
    ]  # a filtered record claiming true: still out of region
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), recs)
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app)
    q = f"since={HC}&until={NOW}&min_conf=0"
    s, h, any_ = c.get(f"/api/detections/events?{q}&in_region=any&non_species=any")
    eq(
        "bird_filtered: the producer's out-of-region records are ingested (nothing rejected) and grouped by species+time (2 reports, beams 2 and 3, best beam 3)",
        (
            app.tailers["birds"].rejected,
            [(e["species"], e["reports"], e["beams"], e["best_beam"], e["in_region"]) for e in any_["events"] if e["species"] == "Varied Thrush"],
        ),
        (0, [("Varied Thrush", 2, [2, 3], 3, False)]),
    )
    s, h, out = c.get(f"/api/detections/events?{q}&in_region=false&non_species=any")
    eq(
        "in_region=false lists exactly the filtered species (a filtered record is never in region, whatever its field says)",
        sorted(e["species"] for e in out["events"]),
        ["Keyless Kite", "Varied Thrush", "Wrong Wren"],
    )
    s, h, dflt = c.get(f"/api/detections/events?{q}")
    eq(
        "default view: in-region and unknown-region species only, non-species left out (Null Nuthatch with no in_region key counts, True Thrush counts)",
        sorted(e["species"] for e in dflt["events"]),
        ["Null Nuthatch", "True Thrush"],
    )
    s, h, anyr = c.get(f"/api/detections/events?{q}&in_region=any")
    eq(
        "in_region=any (non-species still excluded): all five species rows",
        sorted(e["species"] for e in anyr["events"]),
        ["Keyless Kite", "Null Nuthatch", "True Thrush", "Varied Thrush", "Wrong Wren"],
    )
    eq("an event with no in_region information reports null", [e["in_region"] for e in dflt["events"] if e["species"] == "Null Nuthatch"], [None])
    s, h, ns = c.get(f"/api/detections/events?{q}&non_species=true")
    eq("non_species=true lists only the Engine event, flagged", [(e["species"], e["non_species"]) for e in ns["events"]], [("Engine", True)])
    s, h, nsa = c.get(f"/api/detections/events?{q}&non_species=any")
    eq("non_species=any: Engine joins the default two", sorted(e["species"] for e in nsa["events"]), ["Engine", "Null Nuthatch", "True Thrush"])
    s, h, sp = c.get(f"/api/detections/species?since={HC}&until={NOW}")
    eq("species summary default: the same two species, Engine excluded", sorted(x["species"] for x in sp["species"]), ["Null Nuthatch", "True Thrush"])
    s, h, spo = c.get(f"/api/detections/species?since={HC}&until={NOW}&in_region=false")
    eq(
        "species summary in_region=false: Varied Thrush has 1 event, 2 reports, beams 2 and 3",
        [(x["events"], x["reports"], x["beam_events"]) for x in spo["species"] if x["species"] == "Varied Thrush"],
        [(1, 2, [0, 1, 1, 0, 0, 0, 0, 0])],
    )
    s, h, spn = c.get(f"/api/detections/species?since={HC}&until={NOW}&non_species=true")
    eq("species summary non_species=true: Engine only, flagged", [(x["species"], x["non_species"]) for x in spn["species"]], [("Engine", True)])
    s, h, j = c.get(f"/api/detections/events?{q}&beam=2&in_region=any")
    eq(
        "beam filter matches any beam of the event, not just the best beam (Varied Thrush best beam is 3, beam=2 still finds it)",
        [e["species"] for e in j["events"]],
        ["Varied Thrush"],
    )
    s, h, j = c.get(f"/api/detections/events?{q}&beam=4&in_region=any")
    eq("beam filter: positive twin - beam 4 finds only the event that has beam 4", [e["species"] for e in j["events"]], ["Null Nuthatch"])
    s, h, j = c.get(f"/api/detections/events?since={T0 + 300}&until={T0 + 301}&min_conf=0")
    eq("since is inclusive: the event at exactly since is returned", [e["species"] for e in j["events"]], ["True Thrush"])
    s, h, j = c.get(f"/api/detections/events?since={T0 + 299}&until={T0 + 300}&min_conf=0")
    eq("until is exclusive: the event at exactly until is not returned", j["events"], [])
    s, h, j = c.get(f"/api/detections/events?since={T0 + 299}&until={T0 + 300.5}&min_conf=0")
    eq("until: positive twin - 0.5 s later it is returned", [e["species"] for e in j["events"]], ["True Thrush"])
    s, h, j = c.get(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0.6")
    eq(
        "min_conf compares the event's maximum confidence (Varied Thrush 0.4918, Keyless 0.2808, Wrong 0.2808 are below 0.6; 0.61 and 0.9 pass)",
        sorted(e["species"] for e in j["events"]),
        ["Null Nuthatch", "True Thrush"],
    )
    s, h, j = c.get(f"/api/detections/events?{q}&non_species=maybe")
    eq("non_species=maybe is a 422 naming the parameter", (s, j["param"]), (422, "non_species"))
    s, h, stt = c.get("/api/status")
    eq("status: nothing was rejected from birds.jsonl", [x["rejected_records"] for x in stt["sources"] if x["name"] == "birds"], [0])
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def ny_mktime(st):
    """Local wall clock of America/New_York on the 2026 fall-back day (clocks 02:00 EDT -> 01:00 EST on 2026-11-01), the way a C
    mktime resolves the repeated hour: to its first (EDT) reading."""
    import calendar

    wall = calendar.timegm(st)
    cut = calendar.timegm((2026, 11, 1, 2, 0, 0, 0, 0, 0))  # wall-clock 02:00 on the fall-back day
    return wall + (4 * 3600 if wall < cut else 5 * 3600)


def section_dst(base):
    eq("fallback: no previous stamp -> unchanged", U.resolve_fallback(1000.0, None), 1000.0)
    eq(
        "fallback: a stamp 3570 s before the previous one, resuming 30 s after it once shifted, is the repeated hour (+3600)",
        U.resolve_fallback(1000.0, 4570.0),
        4600.0,
    )
    eq("fallback: a plain duplicate (same stamp) is left alone", U.resolve_fallback(1000.0, 1000.0), 1000.0)
    eq("fallback: a normal later stamp is left alone", U.resolve_fallback(1030.0, 1000.0), 1030.0)
    eq("fallback: boundary - resuming exactly 600 s after the previous stamp still counts", U.resolve_fallback(1000.0, 4000.0), 4600.0)
    eq("fallback: boundary - 601 s after does not (an old line, not a repeated hour)", U.resolve_fallback(1000.0, 3999.0), 1000.0)

    def line(wall, loss):
        return f"{wall} stats: 2990 pkt/s loss {loss}% gaps=1 back=0 bad=0 blocks=5 sent=1 all_sent=1 state=ok nch=8 group=x\n"

    walls = ["2026-11-01 01:58:00", "2026-11-01 01:58:30", "2026-11-01 01:59:00", "2026-11-01 01:59:30"]  # EDT, first pass
    second = ["2026-11-01 01:%02d:%02d" % (m, sec) for m in range(60) for sec in (0, 30)]  # EST, the repeated hour
    walls += second + ["2026-11-01 02:00:00"]
    root = tempfile.mkdtemp(dir=base)
    path = os.path.join(root, "bridge.log")
    with open(path, "w") as f:
        for i, w in enumerate(walls):
            f.write(line(w, 1.0 + (i % 3) / 10))
    st = U.StreamStore()
    t = U.Tailer(path, st.add, parse=U.BridgeLogParser(mktime=ny_mktime))
    t.poll()
    got = [x[0] for x in st.samples]
    first = 1793512680.0  # 2026-11-01T05:58:00Z = 01:58:00 EDT
    want = [first + 30 * i for i in range(4)] + [first + 120 + 30 * i for i in range(120)] + [first + 120 + 3600]
    eq(
        "DST fall-back: all 125 samples across the repeated hour are kept (none dropped), 30 s apart, then 02:00 EST at 07:00Z",
        (len(got), got == want, got[-1] - first),
        (125, True, 3720.0),
    )
    eq("DST fall-back: the tailer saw 125 lines and rejected nothing", (t.lines, t.rejected, t.garbage), (125, 0, 0))
    # negative twin: the same repeated hour WITHOUT the fix (identity resolver) loses it, proving the test sees the defect
    st2 = U.StreamStore()
    p2 = U.BridgeLogParser(mktime=ny_mktime)
    raw = []
    for w in walls:
        raw.append(ny_mktime(time.strptime(w, "%Y-%m-%d %H:%M:%S")))
    kept = 0
    last = None
    for r in raw:
        if last is None or r > last:
            kept += 1
            last = r
    eq("DST fall-back: the raw wall-clock stamps alone would keep only 5 of 125 (what the old parser did)", kept, 5)
    shutil.rmtree(root, ignore_errors=True)


def section_reject_does_not_stall_accept(base):
    """Over-cap idle connections must not delay the accept loop (each used to cost it ~1 s while draining its 503)."""
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app, max_conns=1, conn_timeout=30.0)
    port = srv.server_address[1]
    hold = [socket.create_connection(("127.0.0.1", port), timeout=5)]  # holds the only slot, sends nothing
    time.sleep(0.3)
    idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(5)]  # five over-cap clients that never send a byte
    time.sleep(0.3)
    t0 = time.monotonic()
    over = raw_http(port, b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
    dt = time.monotonic() - t0
    eq(
        "503 for a real request behind 5 idle over-cap connections is prompt (< 0.5 s; was ~5 s with the inline drain)",
        (over.startswith(b"HTTP/1.1 503"), dt < 0.5),
        (True, True),
    )
    eq("all six over-cap connections were counted as refused", srv.refused, 6)
    for s in hold + idle:
        s.close()
    time.sleep(0.3)
    s2, h2, j2 = c.get("/api/status")
    eq("once the slot is free a normal request is served (positive twin)", (s2, j2["ui_version"]), (200, U.UI_VERSION))
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_http_limits(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app, max_conns=2, conn_timeout=1.0)
    port = srv.server_address[1]
    hold = []
    for _ in range(2):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % port)  # never finishes the request
        hold.append(s)
    time.sleep(0.3)
    over = raw_http(port, b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
    eq(
        "connection cap: the third connection gets an immediate 503 with Retry-After while two are stalled",
        (over.startswith(b"HTTP/1.1 503"), b"Retry-After: 5" in over, srv.refused),
        (True, True, 1),
    )
    time.sleep(1.8)
    closed = []
    for s in hold:
        s.settimeout(3)
        try:
            closed.append(s.recv(1024))
        except socket.timeout:
            closed.append(None)
        s.close()
    eq("connection timeout: both stalled connections were closed by the server after ~1 s (recv returns EOF, not a hang)", closed, [b"", b""])
    s2, h2, j2 = c.get("/api/status")
    eq("after the stalled clients are gone a normal request is served again (slots released) - positive twin", (s2, j2["ui_version"]), (200, U.UI_VERSION))
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_http_hygiene(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    log = []
    app = new_app(root, pr, rp, lambda: NOW, log=log)
    app.start_backfill()
    srv, c = serve(app)
    port = srv.server_address[1]
    no_host = raw_http(port, b"GET /api/status HTTP/1.0\r\n\r\n")
    eq("HTTP/1.0 request without a Host header is refused 403", no_host.split(b"\r\n")[0], b"HTTP/1.1 403 Forbidden")
    with_host = raw_http(port, b"GET /api/status HTTP/1.0\r\nHost: 127.0.0.1:%d\r\n\r\n" % port)
    eq("positive twin: the same request with a valid Host is 200", with_host.split(b"\r\n")[0], b"HTTP/1.1 200 OK")
    calls = []
    real = app.api_status

    def exploding(q):
        calls.append(dict(q))
        raise RuntimeError("SECRET-EXC-TEXT /d/private/path")

    app.api_status = exploding
    s, h, b = c.req("GET", "/api/status")
    eq("500: the route stub ran once", calls, [{}])
    check(
        "500: body is the fixed problem document and carries no exception text, class or path",
        s == 500 and b"SECRET-EXC-TEXT" not in b and b"RuntimeError" not in b and json.loads(b)["detail"] == "the request could not be served",
        f"{s} {b[:120]!r}",
    )
    check(
        "500: the log line has method-free path + class only, no message",
        "500 /api/status RuntimeError" in log and all("SECRET-EXC-TEXT" not in l for l in log),
        str(log),
    )
    app.api_status = real
    s, h, b = c.req("GET", "/api/status")
    eq("500 twin: restoring the route gives 200", s, 200)
    s, h, b = c.req("GET", "/api/status")
    rawh = raw_http(port, b"HEAD /api/status HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n" % port)
    head_part, _, head_body = rawh.partition(b"\r\n\r\n")
    clen = [int(x.split(b":")[1]) for x in head_part.split(b"\r\n") if x.lower().startswith(b"content-length")]
    eq(
        "HEAD: 200, a Content-Length equal to GET's, and not one body byte on the wire",
        (head_part.split(b"\r\n")[0], clen == [int(h["content-length"])], head_body),
        (b"HTTP/1.1 200 OK", True, b""),
    )
    eq(
        "headers: X-Frame-Options and Referrer-Policy exact",
        (h.get("x-frame-options"), h.get("referrer-policy"), h.get("cross-origin-resource-policy")),
        ("DENY", "no-referrer", "same-origin"),
    )
    s, h, b = c.req("GET", "/static/app.js?x=1")
    eq("an asset with a query string is a 404", s, 404)
    s, h, b = c.req("GET", "/static/app.js")
    eq("positive twin: the asset without a query is 200", s, 200)
    s, h, b = c.req("GET", "/api/beams/stats?band=99")
    eq("a 422 is never cached and says its language", (s, h.get("cache-control"), h.get("content-language")), (422, "no-store", "en"))
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_token_refresh(base):
    root = tempfile.mkdtemp(dir=base)
    fb = T.FakeBoard()
    tf = os.path.join(root, "token")
    open(tf, "w").write("OLD-TOKEN-1111\n")
    b = U.Board(f"http://127.0.0.1:{fb.port}", tf, timeout=3.0)
    ok, body, err = b.get("info")
    eq("token: first call succeeds with the file's token", (ok, err, fb.log[-1][2]), (True, None, "Bearer OLD-TOKEN-1111"))
    fb.auth_fail = True
    ok, body, err = b.get("info")
    eq("token: a 401 is reduced to its code and drops the cached token", (ok, err, b.token), (False, "http_401_unauthorized", None))
    open(tf, "w").write("NEW-TOKEN-2222\n")
    fb.auth_fail = False
    ok, body, err = b.get("info")
    eq("token: after the 401 the file is re-read and the NEW token is sent", (ok, fb.log[-1][2]), (True, "Bearer NEW-TOKEN-2222"))
    eq("token: board contact stayed GET-only throughout", {m for (m, p, a) in fb.log}, {"GET"})
    fb.close()
    shutil.rmtree(root, ignore_errors=True)


def section_tailer_failures(base):
    root = tempfile.mkdtemp(dir=base)
    # a directory where the file should be
    d = os.path.join(root, "birds.jsonl")
    os.makedirs(d)
    got = []
    t = U.Tailer(d, lambda o: got.append(o) or True)
    t.backfill(1 << 20)
    eq("tailer: a directory is refused without an exception, recorded as last_error", (t.last_error, t.key, t.offset), ("not_a_regular_file", None, 0))
    eq("tailer: polling a directory reads nothing and reports False", (t.poll(), got), (False, []))
    # a transient read failure during backfill resumes at the END, never from byte 0
    p = os.path.join(root, "big.jsonl")
    with open(p, "wb") as f:
        for i in range(500):
            f.write(b'{"i":%d,"pad":"%s"}\n' % (i, b"x" * 50))
    size = os.path.getsize(p)
    seen = []
    t2 = U.Tailer(p, lambda o: seen.append(o["i"]) or True)
    attempts = []

    def failing_read(path, start, limit):
        attempts.append((start, limit))
        raise PermissionError(13, "sharing violation")

    t2._read_file = failing_read
    t2.backfill(4000)
    eq(
        "tailer: a read failure during backfill is recorded (class only) and leaves the offset at the end of the file",
        (t2.last_error, t2.offset, attempts[0][1] > 0),
        ("PermissionError", size, True),
    )
    del t2._read_file  # the file becomes readable again
    open(p, "ab").write(b'{"i":500,"pad":""}\n')
    t2.poll()
    eq("tailer: after the failure only the NEW line is ingested, not the 500 old ones (no whole-history replay)", (seen, t2.last_error), ([500], None))
    # a backfill that raises something other than OSError (a recording stub for the tailer) must not stop the other sources
    pr0, rp0, rd0 = mini_tree(tempfile.mkdtemp(dir=base))
    T.jl(os.path.join(pr0, "analysis", "birds.jsonl"), [T.bird(HC + 100, 2, 0.8, "Alpha Wren", "Aus a")])
    log0, stubbed = [], []
    app0 = new_app(os.path.dirname(pr0), pr0, rp0, lambda: NOW, log=log0)

    def bad_backfill(nbytes, rot):
        stubbed.append((nbytes, rot))
        raise ValueError("boom")

    app0.tailers["tags"].backfill = bad_backfill
    app0.start_backfill()
    eq(
        "app: a tailer whose backfill raises is recorded and logged by class; the birds source still loaded",
        (len(stubbed), app0.tailers["tags"].last_error, log0, len(app0.dets.snapshot()), app0.backfill_done),
        (1, "ValueError", ["backfill tags failed: ValueError"], 1, True),
    )
    # the same through the App: birds.jsonl is a directory
    pr, rp, rd = mini_tree(tempfile.mkdtemp(dir=base))
    os.makedirs(os.path.join(pr, "analysis", "birds.jsonl"))
    app = new_app(os.path.dirname(pr), pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app)
    s, h, j = c.get("/api/status")
    eq(
        "app: a non-file birds source does not stop the backfill and shows its error in the status",
        (s, j["ingest"]["backfill_done"], [x["last_error"] for x in j["sources"] if x["name"] == "birds"]),
        (200, True, ["not_a_regular_file"]),
    )
    srv.shutdown()
    # positive twin: a regular file reports no error
    eq("tailer: a regular file has no error", (U.Tailer(p, lambda o: True).last_error,), (None,))
    shutil.rmtree(root, ignore_errors=True)


def section_live(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(NOW - 3, 0), T.bands_rec(NOW - 2, 2), T.bands_rec(NOW - 1, None)])
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app)
    s, h, lv = c.get("/api/beams/live")
    bd = lv["bands"][5]
    eq("live: ring power mean is the power mean over the 8 beams (-20.34 dB, NOT the arithmetic dB mean -23.33 or -20.0)", bd["ring_power_mean_db"], -20.34)
    eq(
        "live: rel_ring_db per beam (beam 1 +5.65, beam 2 -9.66, beam 3 +5.65)",
        (bd["rel_ring_db"][0], bd["rel_ring_db"][1], bd["rel_ring_db"][2]),
        (5.65, -9.66, 5.65),
    )
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def raw_bird(t, beam, conf, common, sci, flag=None):
    """A bird record exactly as an OLD producer wrote it: no in_region key, and no non_species key unless flag is given."""
    r = {"k": "bird", "t": t, "beam": beam, "level_dbfs": -60.0, "common": common, "scientific": sci, "confidence": conf, "provenance": "model_opinion"}
    if flag is not None:
        r["non_species"] = flag
    return r


def det_rollup(h, rows):
    sp = [
        {
            "scientific": sci,
            "common": com,
            "events": n,
            "reports": n,
            "max_confidence": 0.9,
            "beam_events": [n, 0, 0, 0, 0, 0, 0, 0],
            "in_region": True,
            **({} if flag is None else {"non_species": flag}),
        }
        for sci, com, n, flag in rows
    ]
    return {"k": "ui_rollup", "v": 1, "kind": "detections", "hour_s": h, "species": sp}


def section_taxa_table():
    tx = U.Taxa(U.TAXA_PATH)
    eq("taxa.json loads without error", tx.error, None)
    eq("taxa.json: exact genus count (1798 bird + 11 amphibian + 19 insect + 6 mammal)", len(tx.genus), 1834)
    rows = [
        (("Amblycorypha longinicta", "Common Virtuoso Katydid", None), ("insect", "genus", False)),
        (("Gryllus assimilis", "Gryllus assimilis", True), ("insect", "genus", False)),  # the producer's scientific == common false positive
        (("Miogryllus saussurei", "Miogryllus saussurei", True), ("insect", "genus", False)),
        (("Oecanthus pini", "Pine Tree Cricket", None), ("insect", "genus", False)),
        (("Apis mellifera", "Honey Bee", None), ("insect", "genus", False)),
        (("Gastrophryne carolinensis", "Eastern Narrow-mouthed Toad", None), ("amphibian", "genus", False)),
        (("Lithobates catesbeianus", "American Bullfrog", None), ("amphibian", "genus", False)),
        (("Pseudacris triseriata", "Striped Chorus Frog", None), ("amphibian", "genus", False)),
        (("Odocoileus virginianus", "White-tailed Deer", None), ("mammal", "genus", False)),
        (("Tamias striatus", "Eastern Chipmunk", None), ("mammal", "genus", False)),
        (("Sciurus carolinensis", "Eastern Gray Squirrel", None), ("mammal", "genus", False)),
        (("Canis latrans", "Coyote", None), ("mammal", "genus", False)),
        (("Dog", "Dog", True), ("mammal", "explicit", False)),  # a producer flag does not hide it: Dog is shown under Mammal
        (("Dog", "Dog", None), ("mammal", "explicit", False)),  # and without a flag it is the same
        (("Engine", "Engine", None), ("mechanical", "explicit", True)),  # an OLD record without the flag
        (("Human vocal", "Human vocal", None), ("mechanical", "explicit", True)),
        (("Siren", "Siren", False), ("mechanical", "explicit", True)),  # a (wrong) false flag does not un-mechanical it
        (("Melanerpes carolinus", "Red-bellied Woodpecker", None), ("bird", "genus", False)),
        (("Strix varia", "Barred Owl", None), ("bird", "genus", False)),
        (("AMBLYCORYPHA longinicta", "x", None), ("insect", "genus", False)),  # genus match is case-insensitive
        (("Engine", "ENGINE", None), ("mechanical", "explicit", True)),
        (("Zzyzx fictus", "Mystery Thing", None), ("other-bio", "unmapped", False)),  # unknown: filed under other-bio, never dropped
        (("Zzyzx fictus", "Mystery Thing", True), ("other-bio", "unmapped", True)),  # an unmapped label keeps the producer's flag
        (("Zzyzx", "Lone Word", None), ("other-bio", "unmapped", False)),  # a one-word unknown scientific name has no genus
        (("", "Orphan", None), ("other-bio", "unmapped", False)),
    ]
    for (sci, com, flag), want in rows:
        eq("taxon %r/%r flag=%r" % (sci, com, flag), tx.classify(sci, com, flag), want)
    cov = json.load(open(os.path.join(HERE, "..", "classify", "birdnet_coverage.json"), encoding="utf-8"))
    import make_taxa as MT

    nb = {g: grp for grp, gs in MT.NON_BIRD.items() for g in gs}
    got, want = {}, {}
    for lab in cov["uncovered"]:
        sci, com = lab.split("_", 1)
        g = tx.classify(sci, com, None)
        got[lab] = g[1] != "unmapped"
        want[lab] = True
        if sci.split()[0] in nb:
            eq("coverage label %s is %s" % (lab, nb[sci.split()[0]]), g[0], nb[sci.split()[0]])
    eq(
        "every one of the %d BirdNET labels the location model cannot score (insects, frogs, mammals, rare birds) is mapped, none falls to other-bio"
        % len(cov["uncovered"]),
        got,
        want,
    )
    eq(
        "taxa.json genus counts per group",
        {g: sum(1 for v in tx.genus.values() if v == g) for g in U.GROUP_ORDER},
        {"bird": 1798, "insect": 19, "amphibian": 11, "mammal": 6, "other-bio": 0, "mechanical": 0},
    )
    eq("no group name in taxa.json mentions an aircraft (aircraft tracks are out of scope)", [g for g in U.GROUP_ORDER if "air" in g], [])
    with open(os.path.join(HERE, "static", "app.js"), encoding="utf-8") as f:
        js = f.read().lower()
    check("app.js has no aircraft or ADS-B feature", "aircraft" not in js and "adsb" not in js and "ads-b" not in js)


def section_taxa_api(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T0 = HO + 100
    recs = [
        raw_bird(T0, 3, 0.9, "Common Virtuoso Katydid", "Amblycorypha longinicta"),
        raw_bird(T0 + 10, 3, 0.8, "Common Virtuoso Katydid", "Amblycorypha longinicta"),
        raw_bird(T0 + 20, 3, 0.7, "Common Virtuoso Katydid", "Amblycorypha longinicta"),
        raw_bird(T0 + 15, 4, 0.75, "Common Virtuoso Katydid", "Amblycorypha longinicta"),  # same bucket as +10, another beam
        raw_bird(T0 + 30, 4, 0.6, "Gryllus assimilis", "Gryllus assimilis", True),
        raw_bird(T0 + 40, 5, 0.6, "Eastern Narrow-mouthed Toad", "Gastrophryne carolinensis"),
        raw_bird(T0 + 50, 6, 0.6, "White-tailed Deer", "Odocoileus virginianus"),
        raw_bird(T0 + 60, 7, 0.6, "Dog", "Dog", True),
        raw_bird(T0 + 70, 1, 0.6, "Engine", "Engine"),  # old record: no flag
        raw_bird(T0 + 80, 2, 0.6, "Human vocal", "Human vocal", True),
        raw_bird(T0 + 90, 8, 0.6, "Red-bellied Woodpecker", "Melanerpes carolinus"),
        raw_bird(T0 + 100, 1, 0.6, "Mystery Thing", "Zzyzx fictus"),
        raw_bird(T0 + 110, 2, 0.6, "Nonbio Unknown", "Nox nonbio", True),
    ]
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), recs)
    r1, r2, r3 = HO - 3600, HO - 7200, HO - 10800
    kat = ("Amblycorypha longinicta", "Common Virtuoso Katydid", 100, None)
    hours = {
        r1: [
            kat,
            ("Gastrophryne carolinensis", "Eastern Narrow-mouthed Toad", 2, None),
            ("Engine", "Engine", 4, None),
            ("Zzyzx fictus", "Mystery Thing", 1, None),
            ("Oecanthus pini", "Pine Tree Cricket", 60, None),
            ("Oecanthus niveus", "Narrow-winged Tree Cricket", 59, None),
            ("Gryllus rubens", "Southeastern Field Cricket", 200, None),
            ("Gryllus fultoni", "Southern Wood Cricket", 100, None),
        ],
        r2: [
            kat,
            ("Oecanthus pini", "Pine Tree Cricket", 60, None),
            ("Oecanthus niveus", "Narrow-winged Tree Cricket", 60, None),
            ("Gryllus fultoni", "Southern Wood Cricket", 100, None),
        ],
        r3: [kat, ("Oecanthus pini", "Pine Tree Cricket", 60, None), ("Oecanthus niveus", "Narrow-winged Tree Cricket", 60, None)],
    }
    with open(os.path.join(rd, "rollup_20261025.jsonl"), "wb") as f:  # one line per (kind, hour): the first line of an hour wins
        for h in sorted(hours):
            f.write(json.dumps(det_rollup(h, hours[h])).encode() + b"\n")
    app = new_app(root, pr, rp, lambda: NOW, window_h=1)
    app.start_backfill()
    app.housekeeping()
    srv, c = serve(app)
    q = f"since={HO}&until={NOW}&min_conf=0"
    names = lambda j: sorted(e["species"] for e in j["events"])  # noqa: E731
    s, h, ev = c.get(f"/api/detections/events?{q}")
    eq(
        "default view: the engine record with NO flag is excluded, Human vocal and the flagged unmapped class too; the cricket the producer mis-flagged IS shown and so is Dog (a mammal)",
        names(ev),
        ["Common Virtuoso Katydid"] * 4
        + ["Dog", "Eastern Narrow-mouthed Toad", "Gryllus assimilis", "Mystery Thing", "Red-bellied Woodpecker", "White-tailed Deer"],
    )
    eq(
        "event groups on the default view",
        sorted({(e["species"], e["group"]) for e in ev["events"]}),
        [
            ("Common Virtuoso Katydid", "insect"),
            ("Dog", "mammal"),
            ("Eastern Narrow-mouthed Toad", "amphibian"),
            ("Gryllus assimilis", "insect"),
            ("Mystery Thing", "other-bio"),
            ("Red-bellied Woodpecker", "bird"),
            ("White-tailed Deer", "mammal"),
        ],
    )
    s, h, ev = c.get(f"/api/detections/events?{q}&non_species=any")
    eq(
        "non_species=any (positive twin): Engine and the flagged unmapped class are there, Human vocal is NOT (hidden from every view, 1.6.0)",
        sorted(set(names(ev)) - set(names(c.get(f"/api/detections/events?{q}")[2]))),
        ["Engine", "Nonbio Unknown"],
    )
    eq(
        "groups of the non-species classes: Engine mechanical, the flagged unknown other-bio; Dog and Human vocal are NOT among them",
        sorted((e["species"], e["group"], e["non_species"]) for e in ev["events"] if e["non_species"]),
        [("Engine", "mechanical", True), ("Nonbio Unknown", "other-bio", True)],
    )
    eq(
        "group=insect returns exactly the katydid x4 and the cricket",
        (c.get(f"/api/detections/events?{q}&group=insect")[2]["total"], names(c.get(f"/api/detections/events?{q}&group=insect")[2])),
        (5, ["Common Virtuoso Katydid"] * 4 + ["Gryllus assimilis"]),
    )
    eq("group=amphibian: the toad only", names(c.get(f"/api/detections/events?{q}&group=amphibian")[2]), ["Eastern Narrow-mouthed Toad"])
    eq("group=mammal (non_species default): the deer and Dog", names(c.get(f"/api/detections/events?{q}&group=mammal")[2]), ["Dog", "White-tailed Deer"])
    eq(
        "group=mammal with non_species=any: the deer and Dog",
        names(c.get(f"/api/detections/events?{q}&group=mammal&non_species=any")[2]),
        ["Dog", "White-tailed Deer"],
    )
    eq("group=mechanical needs non_species=any: with the default it is empty", c.get(f"/api/detections/events?{q}&group=mechanical")[2]["total"], 0)
    eq(
        "group=mechanical&non_species=any returns exactly Engine (Human vocal is hidden)",
        names(c.get(f"/api/detections/events?{q}&group=mechanical&non_species=any")[2]),
        ["Engine"],
    )
    s, h, bio = c.get(f"/api/detections/events?{q}&group=bio&non_species=any")
    eq(
        "group=bio with non_species=any: every biological group (Dog included), no mechanical class",
        names(bio),
        ["Common Virtuoso Katydid"] * 4
        + ["Dog", "Eastern Narrow-mouthed Toad", "Gryllus assimilis", "Mystery Thing", "Nonbio Unknown", "Red-bellied Woodpecker", "White-tailed Deer"],
    )
    eq(
        "group=any is the default and echoed",
        (c.get(f"/api/detections/events?{q}")[2]["group"], c.get(f"/api/detections/events?{q}&group=bio")[2]["group"]),
        ("any", "bio"),
    )
    s, h, bad = c.get(f"/api/detections/events?{q}&group=birds")
    eq(
        "group=birds -> 422 naming the parameter and the allowed values",
        (s, bad["param"], bad["detail"]),
        (422, "group", "expected one of bird, insect, amphibian, mammal, other-bio, mechanical, bio, any"),
    )
    eq(
        "group on species and density is validated the same way",
        (c.get(f"/api/detections/species?{q}&group=x")[0], c.get(f"/api/detections/density?{q}&group=x")[0]),
        (422, 422),
    )
    # ---- species summary: live + rollups
    s, h, sp = c.get(f"/api/detections/species?since={r3}&until={NOW}")
    by = {x["scientific"]: x for x in sp["species"]}
    eq("species default view: events merge memory (4 katydid events) with the three katydid rollup hours (300)", by["Amblycorypha longinicta"]["events"], 304)
    eq(
        "species groups: rollup-sourced rows get the same group as live ones",
        {
            k: by[k]["group"]
            for k in (
                "Amblycorypha longinicta",
                "Gastrophryne carolinensis",
                "Odocoileus virginianus",
                "Zzyzx fictus",
                "Oecanthus pini",
                "Melanerpes carolinus",
            )
        },
        {
            "Amblycorypha longinicta": "insect",
            "Gastrophryne carolinensis": "amphibian",
            "Odocoileus virginianus": "mammal",
            "Zzyzx fictus": "other-bio",
            "Oecanthus pini": "insect",
            "Melanerpes carolinus": "bird",
        },
    )
    eq(
        "the old Engine rollup row (no non_species key) is excluded by default (not a species), positive twin: shown with non_species=any as mechanical",
        (
            "Engine" in by,
            c.get(f"/api/detections/species?since={r3}&until={NOW}&non_species=any")[2]["species"]
            and [
                (x["group"], x["events"])
                for x in c.get(f"/api/detections/species?since={r3}&until={NOW}&non_species=any")[2]["species"]
                if x["scientific"] == "Engine"
            ],
        ),
        (False, [("mechanical", 5)]),
    )
    eq(
        "species.groups lists all six groups in order, zeros included",
        [g["group"] for g in sp["groups"]],
        ["bird", "insect", "amphibian", "mammal", "other-bio", "mechanical"],
    )
    eq(
        "species.groups counts (default view): bird 1 species/1 event; insect 6 species; amphibian 1 species/3 events; mammal the deer and Dog; other-bio the unknown; mechanical zero",
        [(g["group"], g["species"], g["events"]) for g in sp["groups"]],
        [("bird", 1, 1), ("insect", 6, 304 + 1 + 180 + 179 + 200 + 200), ("amphibian", 1, 3), ("mammal", 2, 2), ("other-bio", 1, 2), ("mechanical", 0, 0)],
    )
    s, h, spg = c.get(f"/api/detections/species?since={r3}&until={NOW}&group=amphibian")
    eq(
        "group=amphibian narrows the species list but groups[] still counts every group (the chip counts do not collapse)",
        ([x["species"] for x in spg["species"]], spg["events_total"], [g["events"] for g in spg["groups"]]),
        (["Eastern Narrow-mouthed Toad"], 3, [g["events"] for g in sp["groups"]]),
    )
    eq(
        "unclassified: the unknown label is listed with its events (live 1 + rollup 1), unclassified_total counts them; known labels never appear",
        (sp["unclassified"], sp["unclassified_total"]),
        ([{"species": "Mystery Thing", "scientific": "Zzyzx fictus", "events": 2}], 2),
    )
    ch = {x["scientific"]: (x["events_per_active_hour"], x["active_hours"], x["chorus"]) for x in sp["species"]}
    eq("chorus: the katydid (304 events over 4 active hours = 76.0/h) is flagged", ch["Amblycorypha longinicta"], (76.0, 4, True))
    eq("chorus boundary: 60.0 events/h over exactly 3 hours IS a chorus", ch["Oecanthus pini"], (60.0, 3, True))
    eq("chorus boundary: 59.7 events/h over 3 hours is not", ch["Oecanthus niveus"], (59.7, 3, False))
    eq(
        "chorus needs 3 active hours: 100/h over 2 hours is not, 200 in one hour is not",
        (ch["Gryllus fultoni"], ch["Gryllus rubens"]),
        ((100.0, 2, False), (200.0, 1, False)),
    )
    eq("a quiet species is no chorus (woodpecker 1 event)", ch["Melanerpes carolinus"], (1.0, 1, False))
    # ---- density
    s, h, dn = c.get(f"/api/detections/density?{q}&bucket_s=60")
    ev_all = c.get(f"/api/detections/events?{q}")[2]
    eq(
        "density: total equals the events endpoint total and the sum of the cell counts (no event lost or counted twice)",
        (dn["total"], sum(r["events"] for r in dn["rows"]), ev_all["total"]),
        (10, 10, 10),
    )
    kat = [(r["t_s"], r["best_beam"], r["events"], r["group"]) for r in dn["rows"] if r["scientific"] == "Amblycorypha longinicta"]
    eq(
        "density: katydid events at +100 s and +110 s share bucket [+60,+120) on beam 3 (2 events); the one at +115 s on beam 4 is its own cell (cells are per beam); the one at exactly +120 s opens the next bucket (boundary)",
        kat,
        [(HO + 60, 3, 2, "insect"), (HO + 60, 4, 1, "insect"), (HO + 120, 3, 1, "insect")],
    )
    eq(
        "density: n_rows, bucket, data extent and window fields",
        (dn["n_rows"], dn["bucket_s"], dn["data_first_s"], dn["data_last_s"], dn["group"], dn["non_species"]),
        (len(dn["rows"]), 60, T0, T0 + 100, "any", "false"),
    )
    eq(
        "density carries the slot-mapping boundary and the in-memory window start, like events",
        (dn["slot_mapping_reliable_from_s"], dn["window_start_s"], dn["truncated_by_window"]),
        (app.slot_fix_s, round(app.dets.cutoff_s, 3), False),
    )
    s, h, dg = c.get(f"/api/detections/density?{q}&bucket_s=60&group=insect")
    eq(
        "density with group=insect: only insect cells (positive twin of the group filter)",
        (sorted({r["group"] for r in dg["rows"]}), dg["total"]),
        (["insect"], 5),
    )
    s, h, d0 = c.get(f"/api/detections/density?since={HO}&until={HO + 600}")
    eq("density default bucket: 600 s / 200 rounds up to the 60 s minimum", d0["bucket_s"], 60)
    s, h, d1 = c.get(f"/api/detections/density?since={HO - 86400 * 2}&until={HO}")
    eq(
        "density default bucket for 48 h: 172800 / 200 = 864 s rounds up to 900 s; empty result has null extent",
        (d1["bucket_s"], d1["total"], d1["data_first_s"], d1["data_last_s"], d1["rows"]),
        (900, 0, None, None, []),
    )
    eq(
        "density bucket_s=59 -> 422 naming the bound",
        (lambda r: (r[0], r[2]["param"], r[2]["detail"]))(c.get(f"/api/detections/density?{q}&bucket_s=59")),
        (422, "bucket_s", "expected an integer in [60, 86400]"),
    )
    eq(
        "density bucket_s=60 and 86400 are accepted (positive twin)",
        (c.get(f"/api/detections/density?{q}&bucket_s=60")[0], c.get(f"/api/detections/density?{q}&bucket_s=86400")[0]),
        (200, 200),
    )
    eq(
        "density range over 90 days -> 422",
        (lambda r: (r[0], r[2]["detail"]))(c.get(f"/api/detections/density?since={NOW - 91 * 86400}&until={NOW}")),
        (422, "range is limited to 90 days"),
    )
    eq(
        "density has no limit parameter (it aggregates instead): limit=5 is a 422, positive twin bucket_s=60 is 200",
        (c.get(f"/api/detections/density?{q}&limit=5")[0], c.get(f"/api/detections/density?{q}&bucket_s=60")[0]),
        (422, 200),
    )
    eq(
        "density answers HEAD with the headers of GET and no body, and refuses POST",
        (c.req("HEAD", f"/api/detections/density?{q}")[0], c.req("HEAD", f"/api/detections/density?{q}")[2], c.req("POST", "/api/detections/density")[0]),
        (200, b"", 405),
    )
    s, h, st = c.get("/api/status")
    eq(
        "status.taxa: loaded, 1834 genera, no error, 2 unclassified events in the last 24 h (the unknown label and the flagged unknown class)",
        st["taxa"],
        {"loaded": True, "n_genera": 1834, "error": None, "unclassified_events_24h": 2},
    )
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_taxa_broken(base):
    for label, content in (
        ("missing", None),
        ("corrupt", b"{not json"),
        ("wrong version", b'{"version":2,"genera":{},"explicit_labels":{}}'),
        ("unknown group name", b'{"version":1,"genera":{"mollusc":["x"]},"explicit_labels":{}}'),
    ):
        root = tempfile.mkdtemp(dir=base)
        pr, rp, rd = mini_tree(root)
        T.jl(os.path.join(pr, "analysis", "birds.jsonl"), [raw_bird(HO + 100, 3, 0.9, "Common Virtuoso Katydid", "Amblycorypha longinicta")])
        tp = os.path.join(root, "taxa.json")
        if content is not None:
            open(tp, "wb").write(content)
        app = U.App(pr, rp, rd, U.Board("http://127.0.0.1:9", os.path.join(root, "no_token"), enabled=False), clock=lambda: NOW, log=[].append, taxa_path=tp)
        app.start_backfill()
        srv, c = serve(app)
        s, h, st = c.get("/api/status")
        eq(
            "taxa %s: status says not loaded with a loud error and counts the event as unclassified" % label,
            (st["taxa"]["loaded"], st["taxa"]["n_genera"], bool(st["taxa"]["error"]), st["taxa"]["unclassified_events_24h"]),
            (False, 0, True, 1),
        )
        s, h, ev = c.get(f"/api/detections/events?since={HO}&until={NOW}&min_conf=0")
        eq(
            "taxa %s: the detection is not dropped, it is filed under other-bio" % label,
            [(e["species"], e["group"]) for e in ev["events"]],
            [("Common Virtuoso Katydid", "other-bio")],
        )
        s, h, sp = c.get(f"/api/detections/species?since={HO}&until={NOW}")
        eq(
            "taxa %s: species lists it in unclassified" % label,
            (sp["unclassified_total"], [u["species"] for u in sp["unclassified"]]),
            (1, ["Common Virtuoso Katydid"]),
        )
        srv.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    tp = os.path.join(root, "taxa.json")
    open(tp, "wb").write(b'{"version":1,"genera":{"insect":["amblycorypha"]},"explicit_labels":{}}')
    t1 = U.Taxa(tp)
    eq(
        "a small valid taxa file loads (positive twin of the broken ones)",
        (t1.error, t1.classify("Amblycorypha longinicta", "k", None)),
        (None, ("insect", "genus", False)),
    )
    shutil.rmtree(root, ignore_errors=True)


def section_event_detail(base):
    """GET /api/detections/event?id=: one event with every report (the per-beam confidence / level trace)."""
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T0 = HO + 10
    rec = lambda t, beam, conf, lv, com, sci: {
        "k": "bird",
        "t": t,
        "beam": beam,
        "level_dbfs": lv,
        "common": com,
        "scientific": sci,
        "confidence": conf,
        "provenance": "model_opinion",
    }  # noqa: E731
    recs = [
        rec(T0 + 2, 3, 0.6, -52.5, "Common Virtuoso Katydid", "Amblycorypha longinicta"),
        rec(T0, 3, 0.5, -50.0, "Common Virtuoso Katydid", "Amblycorypha longinicta"),
        rec(T0 + 1, 4, 0.8, -45.25, "Common Virtuoso Katydid", "Amblycorypha longinicta"),
        rec(T0 + 100, 7, 0.9, None, "Eastern Narrow-mouthed Toad", "Gastrophryne carolinensis"),
    ]
    recs += [
        rec(T0 + 300, 2, 0.7, -40.0, "Alpha Wren", "Alphus wrenus"),
        rec(T0 + 300, 6, 0.6, -41.0, "Beta Wren", "Betus wrenus"),  # two species starting in the same millisecond
        rec(T0 + 400, 5, 0.5, -42.0, "Tie Caller", "Tieus callerus"),
        rec(T0 + 400, 2, 0.6, -43.0, "Tie Caller", "Tieus callerus"),
    ]  # same t on two beams, arriving 5 then 2
    recs += [
        rec(T0 + 200 + 0.001 * i, 1 + i % 8, 0.5, -60.0, "Bulk Caller", "Bulkus callerus") for i in range(600)
    ]  # one event of 600 reports: the trace is capped at 500
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), recs)
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app)
    q = f"since={HO}&until={NOW}&min_conf=0&in_region=any&non_species=any"
    ids = {e["species"]: e["id"] for e in c.get(f"/api/detections/events?{q}")[2]["events"]}
    kid = "%d-amblycorypha-longinicta" % int(T0 * 1000)
    eq("the id of an event is its start in ms and the slug of its species key (the detail endpoint is keyed on it)", ids["Common Virtuoso Katydid"], kid)
    s, h, d = c.get(f"/api/detections/event?id={kid}")
    ev = c.get(f"/api/detections/events?{q}&species=amblycorypha+longinicta")[2]["events"][0]
    eq("detail: status 200 and the event object is exactly the one the list gives", (s, d["event"]), (200, ev))
    eq(
        "detail reports: all 3 reports in time order (the file had them out of order) with beam, azimuth, confidence and level, exact values",
        d["reports"],
        [
            {"t_s": T0, "beam": 3, "beam_azimuth_deg": 0, "confidence": 0.5, "level_dbfs": -50.0},
            {"t_s": T0 + 1, "beam": 4, "beam_azimuth_deg": 315, "confidence": 0.8, "level_dbfs": -45.2},
            {"t_s": T0 + 2, "beam": 3, "beam_azimuth_deg": 0, "confidence": 0.6, "level_dbfs": -52.5},
        ],
    )
    eq(
        "detail carries the frame label, the slot-mapping boundary and trace_truncated false",
        (d["frame"], d["slot_mapping_reliable_from_s"], d["trace_truncated"]),
        (app.calibration()["frame"], app.slot_fix_s, False),
    )
    eq(
        "detail event: provenance model_opinion, group insect, 3 reports, beams 3 and 4",
        (d["event"]["provenance"], d["event"]["group"], d["event"]["reports"], d["event"]["beams"]),
        ("model_opinion", "insect", 3, [3, 4]),
    )
    s, h, t = c.get(f"/api/detections/event?id={ids['Eastern Narrow-mouthed Toad']}")
    eq(
        "a report without a level keeps level_dbfs null (not 0, not dropped)",
        t["reports"],
        [{"t_s": T0 + 100, "beam": 7, "beam_azimuth_deg": 180, "confidence": 0.9, "level_dbfs": None}],
    )
    s, h, b = c.get(f"/api/detections/event?id={ids['Bulk Caller']}")
    eq(
        "a 600-report event: 600 reports counted, 500 kept in the trace and said so (trace_truncated), the first 500 in time order",
        (b["event"]["reports"], len(b["reports"]), b["trace_truncated"], b["reports"][0]["t_s"], b["reports"][-1]["t_s"]),
        (600, 500, True, T0 + 200, round(T0 + 200.499, 3)),
    )
    eq(
        "same-t reports on different beams come back in beam order, not arrival order (file had beam 5 first)",
        [(r["t_s"], r["beam"]) for r in c.get(f"/api/detections/event?id={ids['Tie Caller']}")[2]["reports"]],
        [(T0 + 400, 2), (T0 + 400, 5)],
    )
    ms300 = int((T0 + 300) * 1000)
    aw, bw = c.get(f"/api/detections/event?id={ms300}-alphus-wrenus"), c.get(f"/api/detections/event?id={ms300}-betus-wrenus")
    eq(
        "two species starting in the same ms: each id returns its own event (positive twin of the wrong-species 404)",
        (aw[0], aw[2]["event"]["species"], aw[2]["event"]["best_beam"], bw[0], bw[2]["event"]["species"], bw[2]["event"]["best_beam"]),
        (200, "Alpha Wren", 2, 200, "Beta Wren", 6),
    )
    s, h, ws = c.get(f"/api/detections/event?id={ms300}-wrong-species")
    eq("a real start time with a wrong species slug -> 404 not_found, never another species' event", (s, ws["code"]), (404, "not_found"))
    s, h, ws = c.get(f"/api/detections/event?id={int(T0 * 1000) + 1}-amblycorypha-longinicta")
    eq("the right slug at the wrong start time -> 404 too", (s, ws["code"]), (404, "not_found"))
    s, h, nf = c.get(f"/api/detections/event?id=1-no-such-event")
    eq(
        "a well-formed id that is not in memory -> 404 not_found with a detail, not 200 and not 500",
        (s, nf["code"], nf["detail"]),
        (404, "not_found", "no such event in the in-memory window"),
    )
    s, h, m = c.get("/api/detections/event")
    eq("no id -> 422 naming the parameter", (s, m["param"], m["detail"]), (422, "id", "required: the id of an event from /api/detections/events"))
    for bad in ("..%2Fx", "ABC-def", "12", "1-" + "a" * 61, "1-a&id=2"):
        s, h, m = c.get(f"/api/detections/event?id={bad}")
        eq("malformed id %r -> 422 (never looked up)" % bad, (s, m["param"]), (422, "id"))
    s, h, m = c.get("/api/detections/event?id=%D9%A1-x")
    eq("an id starting with a non-ASCII digit (Arabic-Indic one) is malformed -> 422, not a 404 lookup", (s, m["param"]), (422, "id"))
    eq(
        "ASCII digit twin: 1-x is well-formed -> 404 (unknown), and the real id is 200",
        (c.get("/api/detections/event?id=1-x")[0], c.get(f"/api/detections/event?id={kid}")[0]),
        (404, 200),
    )
    eq("an id of exactly 60 slug characters is well-formed (boundary twin): 404, not 422", c.get("/api/detections/event?id=1-" + "a" * 60)[0], 404)
    eq("another parameter is 422", c.get(f"/api/detections/event?id={kid}&limit=1")[0], 422)
    eq(
        "HEAD answers like GET without a body, POST is 405",
        (
            c.req("HEAD", f"/api/detections/event?id={kid}")[0],
            c.req("HEAD", f"/api/detections/event?id={kid}")[2],
            c.req("POST", f"/api/detections/event?id={kid}")[0],
        ),
        (200, b"", 405),
    )
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_rollup_has_no_group():
    ds = U.DetStore(10 * 86400, U.Taxa(U.TAXA_PATH))
    ds.add(raw_bird(HC + 10, 3, 0.9, "Common Virtuoso Katydid", "Amblycorypha longinicta"))
    ds.add(raw_bird(HC + 20, 1, 0.9, "Engine", "Engine"))
    ds.add(raw_bird(HC + 30, 1, 0.9, "Gryllus assimilis", "Gryllus assimilis", True))
    ds.add(raw_bird(HC + 31, 2, 0.9, "Gryllus assimilis", "Gryllus assimilis", True))  # a second, flagged report merges into the same event
    rows = ds.closed_rollups(HC + 3600 + 130, {}, 0)[0]["species"]
    eq(
        "rollup lines keep their append-only shape: exactly the 1.1.0 keys, no group",
        sorted({k for r in rows for k in r}),
        ["beam_events", "common", "events", "in_region", "max_confidence", "non_species", "reports", "scientific"],
    )
    eq(
        "rollup non_species is the effective class: Engine true (old record), the mis-flagged cricket false",
        [(r["common"], r["non_species"]) for r in rows],
        [("Common Virtuoso Katydid", False), ("Engine", True), ("Gryllus assimilis", False)],
    )


def section_steerable(base):
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(HC + 100, 0)])
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    srv, c = serve(app)
    st = [c.get(f"/api/beams/stats?since={HC}&until={HC + 3600}&band={b}")[2]["steerable"] for b in range(8)]
    hm = [c.get(f"/api/beams/heatmap?since={HC}&until={HC + 600}&band={b}&bucket_s=300")[2]["steerable"] for b in range(8)]
    eq("beams_stats: only the 50-125 and 125-250 Hz bands are not steerable", st, [False, False, True, True, True, True, True, True])
    eq("beams_heatmap: the same flags", hm, [False, False, True, True, True, True, True, True])
    eq("NOT_STEERABLE_BANDS is derived from the band edges (upper edge <= 250 Hz)", U.NOT_STEERABLE_BANDS, (0, 1))
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def section_human_hidden(base):
    """ui-http 1.6.0: human voices are hidden from every view, with and without non_species=any; the only trace is the status count."""
    import make_taxa as MT

    tx = U.load_taxa()
    eq(
        "HUMAN_LABELS is exactly the three human labels, and ui/taxa.json and make_taxa.py name the same three as mechanical (one list, three places agree)",
        (sorted(U.HUMAN_LABELS), sorted(k for k in tx.explicit if k.startswith("human ")), sorted(k for k in MT.EXPLICIT if k.startswith("human "))),
        (["human non-vocal", "human vocal", "human whistle"],) * 3,
    )
    eq(
        "is_human_label: case-insensitive on the common or the scientific label, whitespace trimmed",
        [
            U.is_human_label("Human vocal"),
            U.is_human_label("HUMAN WHISTLE"),
            U.is_human_label("x", " human non-vocal "),
            U.is_human_label("Human NON-vocal", ""),
        ],
        [True] * 4,
    )
    eq(
        "is_human_label positive twin: a species, Engine and a label that merely contains 'human' are not hidden",
        [
            U.is_human_label("Red-bellied Woodpecker", "Melanerpes carolinus"),
            U.is_human_label("Engine", "Engine"),
            U.is_human_label("Humanoid Owl", "Bubo humanus"),
        ],
        [False] * 3,
    )
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    T0 = HO + 100
    wren = ("Red-bellied Woodpecker", "Melanerpes carolinus")
    recs = [
        raw_bird(T0, 3, 0.9, *wren),
        raw_bird(T0 + 1, 3, 0.8, *wren),
        raw_bird(T0 + 30, 3, 0.7, *wren),  # 2 events
        raw_bird(T0 + 5, 1, 0.9, "Human vocal", "Human vocal", True),
        raw_bird(T0 + 6, 1, 0.9, "Human vocal", "Human vocal", True),
        raw_bird(T0 + 7, 1, 0.9, "Human vocal", "Human vocal", True),
        raw_bird(T0 + 60, 1, 0.9, "Human vocal", "Human vocal", True),  # 2 events (gap 53 s)
        raw_bird(T0 + 10, 2, 0.9, "HUMAN WHISTLE", "Human whistle", True),  # 1 event, upper case
        raw_bird(T0 + 20, 2, 0.9, "Human non-vocal", "Human non-vocal"),  # 1 event, no flag (old record)
        raw_bird(T0 + 40, 4, 0.9, "Engine", "Engine", True),
    ]  # kept: a mechanical class
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), recs)
    r1 = HO - 3600
    with open(os.path.join(rd, "rollup_20261025.jsonl"), "wb") as f:  # written by an older version: it names two human labels
        f.write(
            json.dumps(
                det_rollup(
                    r1,
                    [
                        (wren[1], wren[0], 3, None),
                        ("Engine", "Engine", 2, True),
                        ("Human vocal", "Human vocal", 5, True),
                        ("Human whistle", "Human whistle", 1, True),
                    ],
                )
            ).encode()
            + b"\n"
        )
    app = new_app(root, pr, rp, lambda: NOW, window_h=1)
    app.start_backfill()
    app.housekeeping()
    srv, c = serve(app)
    q = f"since={HO}&until={NOW}&min_conf=0&in_region=any"
    names = lambda j: sorted(e["species"] for e in j["events"])  # noqa: E731
    wr = [wren[0]] * 2
    eq("events, default view: the 2 woodpecker events (positive twin: a normal species is unaffected)", names(c.get(f"/api/detections/events?{q}")[2]), wr)
    eq(
        "events, non_species=any: the woodpeckers and Engine, and no human label (Engine proves non_species=any is really on)",
        names(c.get(f"/api/detections/events?{q}&non_species=any")[2]),
        ["Engine"] + wr,
    )
    eq("events, non_species=true (only the non-species classes): Engine alone", names(c.get(f"/api/detections/events?{q}&non_species=true")[2]), ["Engine"])
    eq(
        "events, group=mechanical&non_species=any: Engine alone (the group keeps Engine, Siren ...)",
        names(c.get(f"/api/detections/events?{q}&group=mechanical&non_species=any")[2]),
        ["Engine"],
    )
    for sp in ("human vocal", "Human Whistle", "human non-vocal"):
        eq(
            f"events, species={sp!r} with non_species=any: no event",
            c.get(f"/api/detections/events?{q}&non_species=any&species={sp.replace(' ', '%20')}")[2]["total"],
            0,
        )
    s, h, dn = c.get(f"/api/detections/density?{q}&non_species=any")
    eq(
        "density, non_species=any: 3 events in the cells (2 woodpeckers, Engine), no human cell",
        (dn["total"], sorted({r["species"] for r in dn["rows"]}), sum(r["events"] for r in dn["rows"])),
        (3, ["Engine", wren[0]], 3),
    )
    s, h, sp = c.get(f"/api/detections/species?since={HO - 7200}&until={NOW}&in_region=any&non_species=any")
    eq(
        "species summary (memory + the old rollup, non_species=any): woodpecker 2 + 3, Engine 1 + 2, nothing human; the old rollup's 5 + 1 human events are skipped",
        ([(r["species"], r["events"]) for r in sp["species"]], sp["events_total"]),
        ([(wren[0], 5), ("Engine", 3)], 8),
    )
    eq(
        "species chips: the mechanical chip counts Engine only (3), the bird chip the woodpecker (5), no other group has an event",
        {g["group"]: g["events"] for g in sp["groups"] if g["events"]},
        {"bird": 5, "mechanical": 3},
    )
    s, h, sp0 = c.get(f"/api/detections/species?since={HO - 7200}&until={NOW}&in_region=any")
    eq(
        "species summary, default non_species: the woodpecker only (positive twin of the any view)",
        [(r["species"], r["events"]) for r in sp0["species"]],
        [(wren[0], 5)],
    )
    s, h, bio = c.get(f"/api/detections/events?{q}")
    eid = bio["events"][0]["id"]
    s, h, one = c.get(f"/api/detections/event?id={eid}")
    eq("event detail: a woodpecker event is served (positive twin)", (s, one["event"]["species"]), (200, wren[0]))
    s, h, gone = c.get(f"/api/detections/event?id={int((T0 + 5) * 1000)}-human-vocal")
    eq("event detail: the hidden Human vocal event id is a 404 not_found", (s, gone["code"]), (404, "not_found"))
    s, h, st = c.get("/api/status")
    eq(
        "status.human_hidden_24h counts the hidden EVENTS: 2 Human vocal (3 reports merged + 1), 1 whistle, 1 non-vocal = 4 (a count, no label, no time)",
        st["human_hidden_24h"],
        4,
    )
    eq("status: events_in_memory counts only the 3 visible events (positive twin)", st["ingest"]["events_in_memory"], 3)
    eq("status: last_event is Engine, the newest visible event, not a later human one", st["last_event"]["species"], "Engine")
    check(
        "no response body carries a human label anywhere (events any, density any, species any)",
        all(b"human" not in json.dumps(j).lower().encode() for j in (c.get(f"/api/detections/events?{q}&non_species=any")[2], dn, sp)),
    )
    eq(
        "the status payload names no human label either: the only human_ key is the integer count",
        sorted(k for k in st if "human" in k.lower()) + [type(st["human_hidden_24h"]).__name__],
        ["human_hidden_24h", "int"],
    )
    srv.shutdown()
    shutil.rmtree(root, ignore_errors=True)
    # ---- the rollup files written by the dashboard
    root = tempfile.mkdtemp(dir=base)
    pr, rp, rd = mini_tree(root)
    Tc = HC + 1000
    T.jl(
        os.path.join(pr, "analysis", "birds.jsonl"),
        [
            raw_bird(Tc, 3, 0.9, *wren),
            raw_bird(Tc + 5, 1, 0.9, "Human vocal", "Human vocal", True),
            raw_bird(Tc + 60, 1, 0.9, "Human whistle", "Human whistle", True),
            raw_bird(Tc + 90, 4, 0.9, "Engine", "Engine", True),
        ],
    )
    app = new_app(root, pr, rp, lambda: NOW)
    app.start_backfill()
    app.housekeeping()
    blobs = {fn: open(os.path.join(rd, fn), "rb").read().lower() for fn in os.listdir(rd)}
    det = [json.loads(l) for b in blobs.values() for l in b.splitlines() if b'"kind":"detections"' in l]
    eq(
        "rollup files: the closed hour's detections line lists exactly the woodpecker and Engine",
        sorted(r["common"].lower() for d in det for r in d["species"]),
        ["engine", wren[0].lower()],
    )
    check(
        "rollup files: no byte sequence 'human' in any file under the rollup directory",
        bool(blobs) and all(b"human" not in b for b in blobs.values()),
        str(sorted(blobs)),
    )
    eq("rollup run: the 2 hidden events are still only a status count", app.api_status({})["human_hidden_24h"], 2)
    shutil.rmtree(root, ignore_errors=True)


def section_status_voice_hidden(base):
    """/api/status `latest.tags` / `latest.clap` carry no human-voice label; the guard list is the tagger's own."""
    import ast

    root = tempfile.mkdtemp(dir=base, prefix="voice_")
    pr, rp, _ = mini_tree(root)
    t = NOW - 5.0
    with open(os.path.join(pr, "analysis", "tags.jsonl"), "w", newline="") as f:
        f.write(
            json.dumps({"k": "tags", "t": t, "beam": 1, "speech_p": 0.9, "speech_active": True, "top": [["Speech", 0.9], ["Bird", 0.4], ["Human vocal", 0.2]]})
            + chr(10)
        )
    with open(os.path.join(pr, "analysis", "clap.jsonl"), "w", newline="") as f:
        f.write(json.dumps({"k": "clap", "t": t, "beam": 1, "top": [["Singing", 0.6], ["wind blowing", 0.3]]}) + chr(10))
    app = new_app(root, pr, rp, lambda: NOW)
    for name in ("tags", "clap"):
        app.tailers[name].backfill(1 << 20)
    srv, c = serve(app)
    s, h, j = c.get("/api/status")
    eq(
        "status: a tags row [Speech .9, Bird .4, Human vocal .2] serves only Bird; clap Singing is dropped, wind blowing stays",
        (s, j["latest"]["tags"], j["latest"]["clap"]),
        (200, [["Bird", 0.4]], [["wind blowing", 0.3]]),
    )
    srv.shutdown()
    root2 = tempfile.mkdtemp(dir=base, prefix="voice2_")
    pr, rp, _ = mini_tree(root2)
    with open(os.path.join(pr, "analysis", "tags.jsonl"), "w", newline="") as f:
        f.write(json.dumps({"k": "tags", "t": t, "beam": 1, "speech_p": 0.0, "speech_active": False, "top": [["Bird", 0.4], ["Wind", 0.2]]}) + chr(10))
    app = new_app(root2, pr, rp, lambda: NOW)
    app.tailers["tags"].backfill(1 << 20)
    srv, c = serve(app)
    s, h, j = c.get("/api/status")
    eq(
        "status: positive twin - a tags row with no voice label is served unchanged",
        (s, j["latest"]["tags"], j["latest"]["clap"]),
        (200, [["Bird", 0.4], ["Wind", 0.2]], None),
    )
    srv.shutdown()
    src = open(os.path.join(HERE, "..", "analysis", "plugins.py"), encoding="utf-8").read()
    tagger = next(
        ast.literal_eval(n.value) for n in ast.parse(src).body if isinstance(n, ast.Assign) and any(getattr(x, "id", "") == "SPEECH_LABELS" for x in n.targets)
    )
    eq("the UI guard's speech list equals the tagger's plugins.SPEECH_LABELS (14 labels)", (set(U.L.SPEECH_LABELS), len(tagger)), (set(tagger), 14))
    eq(
        "visible_top: a non-list is None, junk entries are dropped, a Human prefix label is dropped",
        (U.visible_top("x"), U.visible_top([None, ["Human whistle", 1], ["Rain", 0.1], 3])),
        (None, [["Rain", 0.1]]),
    )
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(root2, ignore_errors=True)


def main():
    base = tempfile.mkdtemp(prefix="bma_ui_more_")
    try:
        section_rollup_validation(base)
        section_seams(base)
        section_grace()
        section_dead_beam()
        section_birds(base)
        section_dst(base)
        section_http_limits(base)
        section_reject_does_not_stall_accept(base)
        section_http_hygiene(base)
        section_token_refresh(base)
        section_tailer_failures(base)
        section_live(base)
        section_taxa_table()
        section_taxa_api(base)
        section_taxa_broken(base)
        section_human_hidden(base)
        section_rollup_has_no_group()
        section_steerable(base)
        section_event_detail(base)
        section_status_voice_hidden(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"\n{n_checks} checks, {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
