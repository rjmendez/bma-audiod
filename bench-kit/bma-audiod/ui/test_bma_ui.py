#!/usr/bin/env python3
"""Hermetic tests for bma_ui (synthetic JSONL in temp dirs, a recording fake dspd on a loopback port, the real HTTP
server on 127.0.0.1:0). Covers aggregation (power-mean dB, relative-to-ring, leader share, event grouping, in_region
exclusion), rollups (idempotent, crash-safe, history from rollups), tailing (rotation, truncation, partial, garbage),
parameter validation, 405, Host allowlist, token secrecy, traversal, CSP and asset types, and the contract schemas.

    python test_bma_ui.py      (needs jsonschema for the schema checks)
bma_ui.py is imported from this directory (the mutation scripts copy the whole directory and run the copy)."""

import http.client, json, math, os, shutil, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = HERE
sys.path.insert(0, UI_DIR)
import bma_ui as U  # noqa: E402

fails, n_checks = [], 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


TOKEN = "TOK-9f3a7c21-secret-value"
H0 = 1792940400  # an exact UTC hour start (2026-10-25 15:00:00Z)
HC = H0 + 2 * 3600  # the closed hour with beam data
HO = H0 + 3 * 3600  # the open hour
NOW = HO + 600
EDGES = [[50, 125], [125, 250], [250, 500], [500, 1000], [1000, 2000], [2000, 4000], [4000, 8000], [8000, 16000]]


def jl(path, recs, raw=False):
    with open(path, "ab") as f:
        for r in recs:
            f.write(r if raw else (json.dumps(r, separators=(",", ":")) + "\n").encode())


def bands_rec(t, lead_beam=None, base=-30.0, hot=-10.0, zf=0.0, extra=None):
    db = [[base] * 8 for _ in range(8)]
    if lead_beam is not None:
        db[lead_beam][5] = db[lead_beam][6] = hot
    for (b, k), v in (extra or {}).items():
        db[b][k] = v
    return {"k": "beam_bands", "v": 1, "idx": int(t) % 4000000000, "t_us": int(t * 1e6), "n": 48000, "zero_frac": zf, "edges_hz": EDGES, "db": db}


def bird(t, beam, conf, common, sci, level=-60.0, in_region=True, non=False, filtered=False):
    if filtered:  # the producer's real out-of-region record (analysis/plugins.py)
        return {
            "k": "bird_filtered",
            "t": t,
            "beam": beam,
            "level_dbfs": level,
            "common": common,
            "scientific": sci,
            "confidence": conf,
            "in_region": False,
            "provenance": "model_opinion",
        }
    return {
        "k": "bird",
        "t": t,
        "beam": beam,
        "level_dbfs": level,
        "common": common,
        "scientific": sci,
        "confidence": conf,
        "provenance": "model_opinion",
        "in_region": in_region,
        "non_species": non,
    }


def make_tree(root):
    """pipeline root + reports dir with: 3 band windows in the closed hour and the same 3 in the open hour (beam 1 leads,
    beam 3 leads, nobody), birds with grouping edge cases, bridge log + status files."""
    pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
        os.makedirs(d, exist_ok=True)
    for base in (HC + 100, HO + 100):
        jl(os.path.join(rp, "beam_bands.jsonl"), [bands_rec(base, 0), bands_rec(base + 1, 2), bands_rec(base + 2, None)])
    # live window (last 60 s of NOW): the same triple, with a different zero_frac
    jl(os.path.join(rp, "beam_bands.jsonl"), [bands_rec(NOW - 3, 0, zf=0.5), bands_rec(NOW - 2, 2, zf=0.0), bands_rec(NOW - 1, None, zf=0.0)])
    T = HC + 1000
    reps = [
        bird(T, 2, 0.5, "Alpha Wren", "Aus a", level=-61.0),
        bird(T + 1, 3, 0.8, "Alpha Wren", "Aus a", level=-58.0),
        bird(T + 2, 2, 0.6, "Alpha Wren", "Aus a", level=-62.0),
        bird(T + 1, 4, 0.7, "Bravo Owl", "Bus b"),
        bird(T + 20, 1, 0.5, "Charlie Jay", "Cus c"),
        bird(T + 23.0, 1, 0.6, "Charlie Jay", "Cus c"),  # gap exactly 3.0: one event
        bird(T + 40, 5, 0.5, "Delta Dove", "Dus d"),
        bird(T + 43.01, 5, 0.6, "Delta Dove", "Dus d"),  # gap 3.01: two events
        bird(T + 60, 6, 0.9, "Echo Elsewhere", "Eus e", filtered=True),
        bird(HO + 200, 7, 0.4, "Alpha Wren", "Aus a"),
    ]
    jl(os.path.join(pr, "analysis", "birds.jsonl"), reps)
    jl(os.path.join(pr, "analysis", "birds.jsonl"), [b"not json at all\n", b'{"k":"bird","t":"x"}\n'], raw=True)
    with open(os.path.join(pr, "logs", "bridge.log"), "w") as f:
        import time as _t

        for i, loss in enumerate((0.5, 1.5, 2.5)):
            ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(HC + 60 + 30 * i))
            f.write(f"{ts} stats: 2990 pkt/s loss {loss}% gaps={100 + i * 10} back=0 bad=0 blocks=5 sent=1 all_sent=1 state=ok nch=8 group=x\n")
        f.write("2026-10-25 00:00:00 starting something else\n")
    for fn, obj in (
        ("bridge_status.json", {"t": NOW - 5, "state": "ok", "loss_pct": 1.2, "gaps": 7, "pkts": 99, "pps": 2990.0, "uptime_s": 100.0, "nch": 8}),
        ("sink_status.json", {"t": NOW - 5, "audio_output": False, "playing": False, "holes": 3, "data_dropped": 0, "underruns": 0}),
    ):
        json.dump(obj, open(os.path.join(pr, fn), "w"))
    json.dump(
        {
            "t": NOW - 5,
            "device": "cuda:0",
            "uptime_s": 50.0,
            "rx": {"datagrams": 1, "bad": 0, "gaps": 0},
            "plugins": {"birds": {"enabled": True, "records": 4, "errors": 0, "dropped": 0, "last_t": NOW - 9, "last_error": None}},
        },
        open(os.path.join(pr, "analysis", "status.json"), "w"),
    )
    for rel, age in (
        ("beam_bands.jsonl", 10),
        ("analysis/birds.jsonl", 50),
        ("logs/bridge.log", 1000),
        ("bridge_status.json", 5),
        ("sink_status.json", 5),
        ("analysis/status.json", 5),
    ):
        pth = os.path.join(rp if rel == "beam_bands.jsonl" else pr, *rel.split("/"))
        os.utime(pth, (NOW - age, NOW - age))
    return pr, rp


class FakeBoard:
    """Recording stub of dspd: remembers (method, path, authorization) of every request and answers fixed bodies."""

    def __init__(self):
        self.log, self.calibration, self.fail_network, self.auth_fail = [], None, True, False
        self.state = {"beams": {"applied": False, "pattern_ref": None}}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                outer.log.append((self.command, self.path, self.headers.get("Authorization")))
                p = self.path.split("?")[0]
                code, body = 200, {}
                if outer.auth_fail:
                    code, body = 401, {"code": "unauthorized"}
                elif p == "/v1/info":
                    body = {"api_version": "1.3.0", "uptime_s": 1000}
                elif p == "/v1/state":
                    body = outer.state
                elif p == "/v1/telemetry/health":
                    body = {"entries": [{"temp_local_c": 41.0, "temp_remote_c": 42.5, "memfree_kb": 6080, "load": "0.1 0.2 0.3"}]}
                elif p == "/v1/telemetry/monitor":
                    body = {"file": {"stale": False}, "cells_expected": 42, "samples": [{"dsp": 4, "slot": 0, "ms": 1, "mean_square": 10769386, "peak": 11247}]}
                elif p == "/v1/telemetry/network":
                    if outer.fail_network:
                        code, body = 500, {"code": "boom_" + TOKEN, "detail": "echo " + TOKEN}
                    else:
                        body = {"interface": "eth0", "per_s": {"rx": {"bytes": 1}, "tx": {"bytes": 2}}}
                elif p == "/v1/dsps":
                    body = {"dsps": [{"dsp": 0, "role": "host", "observed_version": "1.0.1.49"}]}
                elif p == "/v1/calibration":
                    if outer.calibration is None:
                        code, body = 404, {"code": "calibration_empty"}
                    else:
                        body = {"profile": outer.calibration}
                else:
                    code = 404
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _do

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


SLOT_FIX = HC + 1001  # fixed so fixtures and tests do not depend on the host time zone; Bravo Owl starts exactly here


def build_app(root, pr, rp, board, clock, log, window_h=48, slot_fix_at=SLOT_FIX):
    tf = os.path.join(root, "token")
    open(tf, "w").write(TOKEN + "\n")
    b = U.Board(f"http://127.0.0.1:{board.port}", tf, timeout=3.0)
    app = U.App(pr, rp, os.path.join(root, "rollups"), b, window_h=window_h, clock=clock, log=log.append, slot_fix_at=slot_fix_at)
    app.start_backfill()
    b.refresh(["info", "state", "monitor", "health", "network", "dsps", "calibration"], now=clock())
    return app


class Client:
    def __init__(self, srv):
        self.port = srv.server_address[1]

    def req(self, method, path, host="default", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = dict(headers or {})
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", f"127.0.0.1:{self.port}" if host == "default" else host)
        for k, v in h.items():
            c.putheader(k, v)
        c.putheader("Content-Length", "0")
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, body

    def get(self, path, **kw):
        s, h, b = self.req("GET", path, **kw)
        try:
            j = json.loads(b) if b else None
        except ValueError:
            j = None
        return s, h, j


def main():
    root = tempfile.mkdtemp(prefix="bma_ui_test_")
    log = []
    board = FakeBoard()
    try:
        pr, rp = make_tree(root)
        app = build_app(root, pr, rp, board, lambda: NOW, log)
        srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
        port = srv.server_address[1]
        srv.allowed_hosts = U.allowed_hosts_for(port, ["ui.example"])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = Client(srv)
        all_bodies = []

        def api(path, **kw):
            s, h, j = c.get(path, **kw)
            all_bodies.append((path, json.dumps(j), json.dumps(h)))
            return s, h, j

        # ------------------------------------------------------------------ event grouping
        s, h, ev = api(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&limit=100&min_conf=0")
        eq("events: 200", s, 200)
        by = {}
        for e in ev["events"]:
            by.setdefault(e["scientific"], []).append(e)
        a = [e for e in by["Aus a"] if e["t_start_s"] < HO]
        eq("grouping: Alpha Wren T, T+1, T+2 across beams 2,3 is ONE event", len(a), 1)
        eq(
            "grouping: that event keeps the best-confidence beam, the beam set, 3 reports, max conf and max level",
            (a[0]["best_beam"], a[0]["beams"], a[0]["reports"], a[0]["max_confidence"], a[0]["max_level_dbfs"], a[0]["t_start_s"], a[0]["t_end_s"]),
            (3, [2, 3], 3, 0.8, -58.0, HC + 1000, HC + 1002),
        )
        eq("grouping: the same species in the next hour is a separate event", len(by["Aus a"]), 2)
        eq("grouping: a different species at T+1 is its own event (species boundary)", (len(by["Bus b"]), by["Bus b"][0]["beams"]), (1, [4]))
        eq("grouping: gap of exactly 3.0 s joins (Charlie Jay 2 reports, 1 event)", (len(by["Cus c"]), by["Cus c"][0]["reports"]), (1, 2))
        eq("grouping: gap of 3.01 s splits (Delta Dove 2 events)", len(by["Dus d"]), 2)
        eq("events: newest first", [e["t_start_s"] for e in ev["events"]], sorted([e["t_start_s"] for e in ev["events"]], reverse=True))
        eq("events: total counts every event (in_region=any): Aus a x2, Bus b, Cus c, Dus d x2, Eus e = 7", ev["total"], 7)
        s, h, ev_def = api(f"/api/detections/events?since={HC}&until={NOW}&min_conf=0")
        eq("in_region default excludes the out-of-region species from counts: 6", ev_def["total"], 6)
        check("in_region default: Echo Elsewhere is absent", all(e["scientific"] != "Eus e" for e in ev_def["events"]))
        s, h, ev_out = api(f"/api/detections/events?since={HC}&until={NOW}&in_region=false&min_conf=0")
        eq(
            "in_region=false shows only the out-of-region event, viewable",
            [(e["species"], e["in_region"]) for e in ev_out["events"]],
            [("Echo Elsewhere", False)],
        )
        s, h, ev_f = api(f"/api/detections/events?since={HC}&until={NOW}&species=alpha%20wren&beam=3&min_conf=0.75")
        eq("filters: species + beam + min_conf select exactly the Alpha Wren event", [e["best_beam"] for e in ev_f["events"]], [3])
        s, h, ev_p = api(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=2&offset=2")
        eq("paging: limit 2 offset 2 of 7", (len(ev_p["events"]), ev_p["total"], ev_p["limit"], ev_p["offset"]), (2, 7, 2, 2))
        s, h, sp = api(f"/api/detections/species?since={HC}&until={NOW}")
        names = {x["scientific"]: x for x in sp["species"]}
        eq(
            "species summary: Aus a events 2, reports 4, max conf 0.8, per-beam events",
            (names["Aus a"]["events"], names["Aus a"]["reports"], names["Aus a"]["max_confidence"], names["Aus a"]["beam_events"]),
            (2, 4, 0.8, [0, 1, 1, 0, 0, 0, 1, 0]),
        )
        eq("species summary: hourly counts of Aus a", names["Aus a"]["hourly_counts"], [{"hour_s": HC, "events": 1}, {"hour_s": HO, "events": 1}])
        check("species summary: out-of-region species excluded by default", "Eus e" not in names)
        s, h, sp_any = api(f"/api/detections/species?since={HC}&until={NOW}&in_region=any")
        eq("species summary: in_region=any includes it, flagged false", [x["in_region"] for x in sp_any["species"] if x["scientific"] == "Eus e"], [False])
        eq("species summary: first_seen/last_seen exact for Aus a", (names["Aus a"]["first_seen_s"], names["Aus a"]["last_seen_s"]), (HC + 1000, HO + 200))

        # ------------------------------------------------------------------ beams
        s, h, st = api(f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5")
        b = st["beams"]
        eq("stats: 3 windows, 2 with >=3 dB contrast", (st["windows"], st["windows_with_contrast"]), (3, 2))
        eq("stats: beam 1 power-mean in 2-4 kHz over {-10,-30,-30} dB = -14.69 dB (NOT the dB average -23.33)", b[0]["power_mean_db"], -14.69)
        eq("stats: beam 3 equals beam 1 (symmetric), beam 2 = -30.0", (b[2]["power_mean_db"], b[1]["power_mean_db"]), (-14.69, -30.0))
        eq("stats: max_db beam 1 = -10.0, beam 2 = -30.0", (b[0]["max_db"], b[1]["max_db"]), (-10.0, -30.0))
        eq("stats: ring power mean = -20.34 dB", st["ring_power_mean_db"], -20.34)
        eq("stats: relative-to-ring beam 1 +5.65, beam 2 -9.66", (b[0]["rel_ring_db"], b[1]["rel_ring_db"]), (5.65, -9.66))
        eq("stats: leader share 0.5 / 0 / 0.5 over the 2 contrast windows", (b[0]["leader_share"], b[1]["leader_share"], b[2]["leader_share"]), (0.5, 0.0, 0.5))
        eq(
            "stats: 2-8 kHz mean relative-to-ring over contrast windows: beam 1 -1.26, beam 2 -11.26",
            (b[0]["mean_rel_ring_db_2_8khz"], b[1]["mean_rel_ring_db_2_8khz"]),
            (-1.26, -11.26),
        )
        eq("stats: zero_frac_mean 0.0 for the closed hour", st["zero_frac_mean"], 0.0)
        eq("stats: hourly row", (st["hourly"][0]["hour_s"], st["hourly"][0]["windows"], st["hourly"][0]["leader_share"][:3]), (HC, 3, [0.5, 0.0, 0.5]))
        eq(
            "stats: azimuths carry the uncalibrated array-frame label",
            (st["frame"], [x["azimuth_deg"] for x in b]),
            ("array frame (uncalibrated)", [90, 45, 0, 315, 270, 225, 180, 135]),
        )
        s, h, st2 = api(f"/api/beams/stats?since={H0}&until={H0 + 3600}&band=5")
        eq("stats: an hour with no data is an honest empty result (no beams, 0 windows)", (st2["windows"], st2["beams"]), (0, []))
        s, h, lv = api("/api/beams/live")
        bd = lv["bands"][5]
        eq("live: 3 windows in the last 60 s", lv["windows"], 3)
        eq(
            "live: band 2-4 kHz power means and leader (beam 1 and beam 3 tie at -14.69: first wins)",
            (bd["power_mean_db"][0], bd["power_mean_db"][1], bd["leader_beam"]),
            (-14.69, -30.0, 1),
        )
        eq("live: contrast = max - min = 15.31 dB", bd["contrast_db"], 15.31)
        eq("live: mean zero_frac over the 3 windows = 0.1667", lv["zero_frac_mean"], 0.1667)
        s, h, hm = api(f"/api/beams/heatmap?since={HO}&until={HO + 600}&band=5&bucket_s=60")
        eq("heatmap: 10 buckets, 8 beams", (hm["n_buckets"], len(hm["values_power_mean_db"])), (10, 8))
        eq(
            "heatmap: bucket 1 (first 60 s of the open hour, windows at +100..+102 s) lands in bucket index 1: beam 1 -14.69, beam 2 -30.0",
            (hm["values_power_mean_db"][0][1], hm["values_power_mean_db"][1][1]),
            (-14.69, -30.0),
        )
        eq("heatmap: buckets without windows are null, not zero", hm["values_power_mean_db"][0][0], None)
        s, h, hmh = api(f"/api/beams/heatmap?since={HC}&until={HC + 3600}&band=5&bucket_s=3600")
        eq("heatmap: hourly basis for old ranges", (hmh["basis"], hmh["values_power_mean_db"][0][0]), ("hourly", -14.69))
        s, h, hmbad = api(f"/api/beams/heatmap?since={NOW - 10 * 86400}&until={NOW}&band=5&bucket_s=300")
        eq("heatmap: fine buckets before the in-memory window are refused (422)", s, 422)

        # ------------------------------------------------------------------ status / system / calibration
        s, h, stt = api("/api/status")
        eq("status: bridge summary", (stt["bridge"]["state"], stt["bridge"]["loss_pct"], stt["bridge"]["age_s"]), ("ok", 1.2, 5.0))
        src = {x["name"]: x for x in stt["sources"]}
        eq(
            "status: beam_bands present, age 10 s <= 15 s -> not stale",
            (src["beam_bands"]["present"], src["beam_bands"]["age_s"], src["beam_bands"]["stale"]),
            (True, 10.0, False),
        )
        eq("status: positive twin - bridge_log aged 1000 s > 90 s -> stale", (src["bridge_log"]["age_s"], src["bridge_log"]["stale"]), (1000.0, True))
        eq("status: any_stale reflects it", stt["any_stale"], True)
        eq("status: garbage lines and rejected records are counted, never fatal", (src["birds"]["garbage_lines"], src["birds"]["rejected_records"]), (1, 1))
        eq("status: event-driven source has stale null", src["birds"]["stale"], None)
        eq("status: missing source is present false and stale true", (src["clap"]["present"], src["clap"]["stale"]), (False, True))
        eq("status: last event is the Alpha Wren at HO+200", (stt["last_event"]["species"], stt["last_event"]["t_s"]), ("Alpha Wren", HO + 200))
        eq(
            "status: board summary from the stub",
            (stt["board"]["reachable"], stt["board"]["api_version"], stt["board"]["temp_local_c"], stt["board"]["pattern_ref"]),
            (True, "1.3.0", 41.0, None),
        )
        s, h, sy = api(f"/api/system?since={HC - 3600}")
        eq(
            "system: 3 stream samples with exact loss values, mean 1.5",
            ([x["loss_pct"] for x in sy["stream"]["samples"]], sy["stream"]["loss_pct_mean"]),
            ([0.5, 1.5, 2.5], 1.5),
        )
        eq(
            "system: monitor values are raw and unit-less",
            (sy["board"]["monitor"]["samples"][0]["mean_square_raw"], "UNKNOWN" in sy["board"]["monitor"]["note"]),
            (10769386, True),
        )
        check("system: no dB field name on monitor samples", all("db" not in k for k in sy["board"]["monitor"]["samples"][0]))
        eq("system: memory carries the page-cache caveat", "page cache" in sy["board"]["memory"]["note"], True)
        eq("system: DSP versions", sy["board"]["dsps"], [{"dsp": 0, "role": "host", "observed_version": "1.0.1.49"}])
        s, h, cal = api("/api/calibration")
        eq(
            "calibration: none stored -> uncalibrated array frame",
            (cal["calibrated"], cal["frame"], cal["beam_azimuth_deg"]),
            (False, "array frame (uncalibrated)", [90, 45, 0, 315, 270, 225, 180, 135]),
        )
        eq(
            "calibration: uncalibrated -> frame_kind array, not provisional, no panel-up azimuth",
            (cal["frame_kind"], cal["provisional"], cal["panel_up_dsp_azimuth_deg"]),
            ("array", False, None),
        )
        check("calibration: the fallback note no longer claims a vendor pattern", "vendor pattern" not in cal["note"] and "105" not in cal["note"], cal["note"])
        s, h, lv0 = api("/api/beams/live")
        eq("live: uncalibrated -> frame_kind array", lv0["frame_kind"], "array")
        board.calibration = {
            "beam_azimuth_deg": [10, 55, 100, 145, 190, 235, 280, 325],
            "north_reference": "compass",
            "method": "speaker",
            "measured_at": "2026-10-25T14:03:00Z",
            "notes": "SECRET-NOTE 12.345,-67.89",
            "source": "owner",
        }
        app.board.refresh(["calibration"], now=NOW)
        s, h, cal2 = api("/api/calibration")
        eq(
            "calibration: stored compass profile -> compass frame with measured_at and its azimuths, no panel-up azimuth",
            (
                cal2["calibrated"],
                cal2["frame"],
                cal2["frame_kind"],
                cal2["north_reference"],
                cal2["panel_up_dsp_azimuth_deg"],
                cal2["provisional"],
                cal2["beam_azimuth_deg"],
            ),
            (True, "calibrated compass frame 2026-10-25T14:03:00Z", "compass", "compass", None, True, [10, 55, 100, 145, 190, 235, 280, 325]),
        )
        check(
            "calibration: the compass note says compass bearings and never mentions the panel",
            "compass bearings" in cal2["note"] and "panel" not in cal2["note"],
            cal2["note"],
        )
        s, h, lvc = api("/api/beams/live")
        eq("live: a compass profile is frame_kind compass", lvc["frame_kind"], "compass")
        board.calibration = {"beam_azimuth_deg": [10, 55, 100, 145, 190, 235, 280, 325], "north_reference": "array", "measured_at": "2026-10-25T14:03:00Z"}
        app.board.refresh(["calibration"], now=NOW)
        s, h, cala = api("/api/calibration")
        eq(
            "calibration: north_reference array (positive twin of compass) -> panel frame, kind panel, panel-up 15",
            (cala["frame"], cala["frame_kind"], cala["north_reference"], cala["panel_up_dsp_azimuth_deg"]),
            ("calibrated panel frame 2026-10-25T14:03:00Z", "panel", "array", 15),
        )
        board.calibration = {
            "beam_azimuth_deg": [10, 55, 100, 145, 190, 235, 280, 325],
            "north_reference": "compass",
            "method": "speaker",
            "measured_at": "2026-10-25T14:03:00Z",
            "notes": "SECRET-NOTE 12.345,-67.89",
            "source": "owner",
        }
        app.board.refresh(["calibration"], now=NOW)
        s, h, cal2 = api("/api/calibration")
        check("calibration: free-text notes/source are not exposed", "SECRET-NOTE" not in json.dumps(cal2))
        s, h, st3 = api(f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5")
        eq("calibration: azimuths flow into stats", st3["beams"][0]["azimuth_deg"], 10)
        board.calibration = None
        app.board.refresh(["calibration"], now=NOW)
        s, h, cal3 = api("/api/calibration")
        eq("calibration: removed again -> back to uncalibrated", cal3["calibrated"], False)
        # the panel-frame profile stored on the board (array mounted upright): azimuths are used as given and the frame is labelled
        PANEL = [75, 30, 345, 300, 255, 210, 165, 120]
        board.calibration = {"beam_azimuth_deg": PANEL, "measured_at": "2026-10-25T17:47:00Z"}
        app.board.refresh(["calibration"], now=NOW)
        s, h, calp = api("/api/calibration")
        eq(
            "panel calibration: frame, kind, provisional flag, DSP azimuth of panel up, azimuths as stored",
            (calp["calibrated"], calp["frame"], calp["frame_kind"], calp["provisional"], calp["panel_up_dsp_azimuth_deg"], calp["beam_azimuth_deg"]),
            (True, "calibrated panel frame 2026-10-25T17:47:00Z", "panel", True, 15, PANEL),
        )
        s, h, evp = api(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=100")
        eq(
            "panel calibration: every event azimuth is the stored entry of its best beam (exact list, oldest first)",
            sorted((e["t_start_s"], e["species"], e["best_beam"], e["best_beam_azimuth_deg"]) for e in evp["events"]),
            [
                (HC + 1000, "Alpha Wren", 3, 345),
                (HC + 1001, "Bravo Owl", 4, 300),
                (HC + 1020, "Charlie Jay", 1, 75),
                (HC + 1040, "Delta Dove", 5, 255),
                (HC + 1043.01, "Delta Dove", 5, 255),
                (HC + 1060, "Echo Elsewhere", 6, 210),
                (HO + 200, "Alpha Wren", 7, 165),
            ],
        )
        s, h, stp = api(f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5")
        eq("panel calibration: stats azimuth_deg", [x["azimuth_deg"] for x in stp["beams"]], PANEL)
        s, h, lvp = api("/api/beams/live")
        eq(
            "panel calibration: live azimuths, calibrated flag, frame kind",
            (lvp["beam_azimuth_deg"], lvp["calibrated"], lvp["frame_kind"]),
            (PANEL, True, "panel"),
        )
        s, h, hmp = api(f"/api/beams/heatmap?since={HO}&until={HO + 600}&band=5&bucket_s=60")
        eq(
            "panel calibration: heatmap azimuths and frame label",
            (hmp["beam_azimuth_deg"], hmp["frame"]),
            (PANEL, "calibrated panel frame 2026-10-25T17:47:00Z"),
        )
        board.calibration = None
        app.board.refresh(["calibration"], now=NOW)
        s, h, lv1 = api("/api/beams/live")
        eq(
            "panel calibration removed: live is back to the array fallback",
            (lv1["beam_azimuth_deg"], lv1["frame_kind"]),
            ([90, 45, 0, 315, 270, 225, 180, 135], "array"),
        )

        # ------------------------------------------------------------------ beam-slot mapping boundary (data before it is unreliable)
        s, h, evs = api(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=100")
        flags = {(e["species"], e["t_start_s"]): e["slot_mapping_unreliable"] for e in evs["events"]}
        eq(
            "slot fix: reliable-from is exposed on events, status and heatmap",
            (evs["slot_mapping_reliable_from_s"], api("/api/status")[2]["slot_mapping_reliable_from_s"], hmp["slot_mapping_reliable_from_s"]),
            (SLOT_FIX, SLOT_FIX, SLOT_FIX),
        )
        eq("slot fix: an event 1 s before the boundary is unreliable", flags[("Alpha Wren", HC + 1000)], True)
        eq("slot fix: an event starting exactly at the boundary is reliable (positive twin of the strict comparison)", flags[("Bravo Owl", HC + 1001)], False)
        eq("slot fix: later events and the next hour are reliable", (flags[("Charlie Jay", HC + 1020)], flags[("Alpha Wren", HO + 200)]), (False, False))
        eq("slot fix: exactly one event is unreliable", sum(1 for v in flags.values() if v), 1)
        app.slot_fix_s = HC + 1000.0
        s, h, evs3 = api(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=100")
        eq("slot fix: boundary at the first event's own start: nothing is unreliable", sum(1 for e in evs3["events"] if e["slot_mapping_unreliable"]), 0)
        app.slot_fix_s = SLOT_FIX
        eq("slot fix: default is the absolute instant 2026-10-25T21:47:00Z (17:47 EDT), whatever the host time zone", U.default_slot_fix_s(), 1792964820.0)
        eq("slot fix: CLI ISO value with Z parses to the exact epoch (17:47Z)", U.parse_slot_fix("2026-10-25T17:47:00Z"), 1792950420.0)
        eq("slot fix: CLI ISO value with an offset parses to the exact epoch (19:47+02:00)", U.parse_slot_fix("2026-10-25T19:47:00+02:00"), 1792950420.0)
        eq(
            "slot fix: a naive CLI time is read as host local time",
            U.parse_slot_fix("2026-10-25T17:47:00"),
            __import__("time").mktime((2026, 10, 25, 17, 47, 0, 0, 0, -1)),
        )
        # the --slot-fix-at flag reaches the App through the real parser and build()
        cli_base = ["--no-board", "--pipeline-root", pr, "--reports-dir", rp, "--rollup-dir", os.path.join(root, "rollups_cli")]
        cli_app = U.build(U.make_parser().parse_args(cli_base + ["--slot-fix-at", "2026-10-25T17:47:00Z"]))
        eq("slot fix: --slot-fix-at reaches App.slot_fix_s through build()", cli_app.slot_fix_s, 1792950420.0)
        cli_def = U.build(U.make_parser().parse_args(cli_base))
        eq("slot fix: without the flag App.slot_fix_s is the default instant (positive twin)", cli_def.slot_fix_s, 1792964820.0)
        import contextlib, io

        errbuf = io.StringIO()
        try:
            with contextlib.redirect_stderr(errbuf):
                U.make_parser().parse_args(cli_base + ["--slot-fix-at", "not-a-time"])
            rej = None
        except SystemExit as ex:
            rej = ex.code
        eq(
            "slot fix: an invalid --slot-fix-at value is rejected with exit code 2 and names the flag",
            (rej, "--slot-fix-at: invalid parse_slot_fix value: 'not-a-time'" in errbuf.getvalue()),
            (2, True),
        )

        # ------------------------------------------------------------------ validation (422 + positive twins)
        bad = [
            ("since=abc", "since"),
            (f"since={NOW}&until={NOW - 5}", "since"),
            (f"since=1&until={NOW}", "since"),
            ("limit=0", "limit"),
            ("limit=501", "limit"),
            ("min_conf=1.5", "min_conf"),
            ("beam=9", "beam"),
            ("beam=0", "beam"),
            ("in_region=maybe", "in_region"),
            ("offset=-1", "offset"),
            ("species=" + "x" * 101, "species"),
            ("bogus=1", "bogus"),
            ("limit=5&limit=6", "limit"),
            ("until=2026-13-45T00:00:00Z", "until"),
        ]
        for qs, param in bad:
            s, h, j = api("/api/detections/events?" + qs)
            check(
                f"422 events?{qs[:40]}",
                s == 422
                and h["content-type"].startswith("application/problem+json")
                and j["param"] == param
                and j["status"] == 422
                and j["code"] == "invalid_parameter",
                f"{s} {j}",
            )
        good = [
            f"since={HC}&until={NOW}",
            f"since=2026-10-25T15:00:00Z&until=2026-10-25T18:00:00Z",
            "limit=1",
            "limit=500",
            "min_conf=1",
            "min_conf=0",
            "beam=8",
            "beam=1",
            "in_region=any",
            "offset=0",
            "species=" + "x" * 100,
        ]
        for qs in good:
            s, h, j = api("/api/detections/events?" + qs)
            check(f"200 events?{qs[:40]} (positive twin)", s == 200 and h["content-type"] == "application/json", str(s))
        for path, qs in (
            ("/api/beams/stats", "band=8"),
            ("/api/beams/stats", "band=-1"),
            ("/api/beams/heatmap", "bucket_s=9"),
            ("/api/beams/heatmap", "bucket_s=86401"),
            ("/api/beams/heatmap", f"since={NOW - 100 * 86400}&until={NOW}&bucket_s=3600"),
            ("/api/system", f"since={NOW + 10}"),
            ("/api/status", "x=1"),
            ("/api/beams/live", "band=1"),
            ("/api/detections/species", "limit=1"),
        ):
            s, h, j = api(path + "?" + qs)
            check(f"422 {path}?{qs[:40]}", s == 422 and j["code"] == "invalid_parameter", f"{s}")
        for path, qs in (
            ("/api/beams/stats", "band=7"),
            ("/api/beams/stats", "band=0"),
            ("/api/beams/heatmap", "bucket_s=10&since=%d&until=%d" % (HO, HO + 600)),
            ("/api/beams/heatmap", f"bucket_s=86400&since={NOW - 40 * 86400}&until={NOW}"),
            ("/api/system", f"since={NOW - 100}"),
        ):
            s, h, j = api(path + "?" + qs)
            check(f"200 {path}?{qs[:40]} (positive twin)", s == 200, f"{s} {j}")
        s, h, j = api(f"/api/beams/heatmap?since={NOW - 3 * 86400}&until={NOW}&bucket_s=60")
        eq("422 heatmap: more than 2000 buckets named in the detail", (s, "2000" in j["detail"]), (422, True))
        s, h, j = api(f"/api/beams/heatmap?since={HO}&until={HO + 3600}&bucket_s=3")
        eq("422 body is problem+json with a type urn", j["type"], "urn:bma-ui:problem:invalid_parameter")

        # ------------------------------------------------------------------ HTTP surface
        for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            s, h, b = c.req(m, "/api/status")
            check(f"{m} -> 405 with Allow: GET, HEAD", s == 405 and h.get("allow") == "GET, HEAD", f"{s} {h.get('allow')}")
        s, h, b = c.req("POST", "/")
        eq("POST on the page is 405 too", s, 405)
        s, h, b = c.req("HEAD", "/api/status")
        s2, h2, b2 = c.req("GET", "/api/status")
        check(
            "HEAD: 200, no body, same content-length as GET (positive twin of 405)",
            s == 200 and b == b"" and abs(int(h["content-length"]) - int(h2["content-length"])) < 40,
            f"{s} {len(b)}",
        )
        for host in ("evil.example", "evil.example:%d" % port, "127.0.0.1", "localhost", "127.0.0.1:1", "ui.example"):
            s, h, j = api("/api/status", host=host)
            check(f"Host {host!r} rejected 403", s == 403 and j["code"] == "host_not_allowed", f"{s}")
        for host in ("127.0.0.1:%d" % port, "localhost:%d" % port, "ui.example:%d" % port, "LOCALHOST:%d" % port):
            s, h, j = api("/api/status", host=host)
            check(f"Host {host!r} accepted (positive twin)", s == 200, f"{s}")
        eq("Host rejection also applies to POST (403 before 405)", c.req("POST", "/api/status", host="evil.example")[0], 403)
        for p in (
            "/../etc/passwd",
            "/static/../bma_ui.py",
            "/static/%2e%2e/bma_ui.py",
            "/bma_ui.py",
            "/static/",
            "/static/app.js/",
            "/static/APP.JS",
            "/api/status/",
            "/API/status",
            "/api/%73tatus",
            "//etc/passwd",
            "/static/..%5cbma_ui.py",
            "/static/app.js%00.png",
            "/index.htm",
            "/test_bma_ui.py",
            "/static/index.html",
        ):
            s, h, b = c.req("GET", p)
            check(f"traversal/unknown {p!r} -> 404, no file content", s == 404 and b"import" not in b and b"def " not in b, f"{s}")
        for p, ctype, marker in (
            ("/", "text/html; charset=utf-8", b"<!DOCTYPE html>"),
            ("/index.html", "text/html; charset=utf-8", b"<!DOCTYPE html>"),
            ("/static/app.js", "text/javascript; charset=utf-8", b"use strict"),
            ("/static/app.css", "text/css; charset=utf-8", b"--bg"),
            ("/static/tip.js", "text/javascript; charset=utf-8", b"use strict"),
            ("/static/spectrum.js", "text/javascript; charset=utf-8", b"use strict"),
        ):
            s, h, b = c.req("GET", p)
            check(f"asset {p} 200 with {ctype}", s == 200 and h["content-type"] == ctype and marker in b, f"{s} {h.get('content-type')}")
        for p in ("/", "/static/app.js", "/api/status", "/api/nope", "/api/status?x=1"):
            s, h, b = c.req("GET", p)
            csp = h.get("content-security-policy", "")
            check(
                f"security headers on {p} ({s})",
                "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and h.get("x-content-type-options") == "nosniff",
                csp,
            )
        s, h, b = c.req("GET", "/api/status")
        eq("API responses are no-store", h.get("cache-control"), "no-store")
        s, h, b = c.req("GET", "/api/nope")
        eq("unknown API path is a problem+json 404", (s, h["content-type"]), (404, "application/problem+json"))
        s, h, b = c.req("GET", "/api/detections/events?since=%ff")
        eq("invalid percent-encoding is a 422, not a crash", s, 422)

        # ------------------------------------------------------------------ the token
        methods = [m for (m, p, a) in board.log]
        eq("board contact is GET only", set(methods), {"GET"})
        check(
            "board requests carry the bearer token (so the stub test is meaningful)", all(a == "Bearer " + TOKEN for (m, p, a) in board.log), str(board.log[:2])
        )
        s, h, stt = api("/api/status")
        check(
            "the stub returned an error echoing the token for /v1/telemetry/network (test is meaningful)",
            any(p.startswith("/v1/telemetry/network") for (_, p, _) in board.log),
        )
        eq("a board error is reduced to a code", app.board.snap("network")["error"], "http_500_boom_")
        leaked = [p for (p, body, hdr) in all_bodies if TOKEN in body or TOKEN in hdr or "9f3a7c21" in body]
        eq("token (and its tail) appears in no API response or header", leaked, [])
        for p in ("/", "/static/app.js", "/static/app.css", "/api/nope"):
            s, h, b = c.req("GET", p)
            check(f"token absent from {p}", TOKEN.encode() not in b and TOKEN not in json.dumps(h))
        check("token absent from every captured log line", all(TOKEN not in l and "9f3a7c21" not in l for l in log) and len(log) > 5, f"{len(log)} lines")
        check(
            "log lines carry method, path and code but no query string",
            any(l.startswith("GET /api/detections/events 422") for l in log) and all("?" not in l for l in log),
        )

        # ------------------------------------------------------------------ rollups
        rdir = os.path.join(root, "rollups")
        files = sorted(os.listdir(rdir))
        eq("rollup file is named by the UTC day of the hours", files, ["rollup_20261025.jsonl"])
        lines = [json.loads(l) for l in open(os.path.join(rdir, files[0]), "rb").read().splitlines()]
        keys = sorted((r["kind"], r["hour_s"]) for r in lines)
        eq(
            "rollups: one record per (kind, hour) for the CLOSED hours (beams, detections for HC; stream for HC); the open hour is not written",
            keys,
            [("beams", HC), ("detections", HC), ("stream", HC)],
        )
        rb = [r for r in lines if r["kind"] == "beams"][0]
        eq(
            "rollup beams: windows, power-mean dB band 5 beam 1, max, contrast",
            (rb["windows"], rb["power_mean_db"][0][5], rb["max_db"][0][5], rb["contrast"]["windows"], rb["contrast"]["leader_count"][:3]),
            (3, -14.69, -10.0, 2, [1, 0, 1]),
        )
        eq("rollup beams: 2-8 kHz mean relative-to-ring", rb["contrast"]["mean_rel_ring_db_2_8khz"][:2], [-1.26, -11.26])
        rd = [r for r in lines if r["kind"] == "detections"][0]
        sp_a = [x for x in rd["species"] if x["scientific"] == "Aus a"][0]
        eq(
            "rollup detections: Alpha Wren row",
            (sp_a["events"], sp_a["reports"], sp_a["max_confidence"], sp_a["beam_events"], sp_a["in_region"]),
            (1, 3, 0.8, [0, 1, 1, 0, 0, 0, 0, 0], True),
        )
        eq("rollup detections: out-of-region species keeps its flag", [x["in_region"] for x in rd["species"] if x["scientific"] == "Eus e"], [False])
        rs = [r for r in lines if r["kind"] == "stream"][0]
        eq(
            "rollup stream: samples, mean/min/max loss, gaps delta",
            (rs["samples"], rs["loss_pct_mean"], rs["loss_pct_min"], rs["loss_pct_max"], rs["gaps_delta"]),
            (3, 1.5, 0.5, 2.5, 20),
        )
        before = open(os.path.join(rdir, files[0]), "rb").read()
        app.housekeeping()
        app.housekeeping()
        eq("rollups are idempotent: housekeeping twice appends nothing", open(os.path.join(rdir, files[0]), "rb").read(), before)
        app2 = build_app(root, pr, rp, board, lambda: NOW, [])
        eq("restart: same data, same rollup dir -> file unchanged (nothing rewritten)", open(os.path.join(rdir, files[0]), "rb").read(), before)
        eq("restart: the persisted rollups were loaded", len(app2.rollups.recs), 3)
        # an hour that only becomes closed later is written exactly once
        app3 = build_app(root, pr, rp, board, lambda: HO + 3600 + 130, [])
        lines3 = [json.loads(l) for l in open(os.path.join(rdir, files[0]), "rb").read().splitlines()]
        eq(
            "later clock: the formerly open hour is now rolled up (beams + detections + stream? stream has no samples there)",
            sorted((r["kind"], r["hour_s"]) for r in lines3),
            [("beams", HC), ("beams", HO), ("detections", HC), ("detections", HO), ("stream", HC)],
        )
        app3.housekeeping()
        eq("and still exactly once after another pass", len(open(os.path.join(rdir, files[0]), "rb").read().splitlines()), 5)
        # crash safety: a torn last line, then a new batch
        with open(os.path.join(rdir, files[0]), "ab") as f:
            f.write(b'{"k":"ui_rollup","v":1,"kind":"beams","hour_s":99')
        rs2 = U.RollupStore(rdir)
        rs2.load()
        eq("torn tail line is skipped and counted on load, complete records survive", (len(rs2.recs), rs2.garbage), (5, 1))
        nr = {
            "k": "ui_rollup",
            "v": 1,
            "kind": "stream",
            "hour_s": HO,
            "samples": 1,
            "loss_pct_mean": 1.0,
            "loss_pct_min": 1.0,
            "loss_pct_max": 1.0,
            "gaps_delta": 0,
            "state_counts": {"ok": 1},
        }
        eq("write after a torn tail reports 1 written", rs2.write([nr, nr]), 1)
        rs3 = U.RollupStore(rdir)
        rs3.load()
        eq("after the torn tail a new batch is intact: 6 records, still 1 garbage line, duplicate key not doubled", (len(rs3.recs), rs3.garbage), (6, 1))
        eq("write of an existing key writes nothing", rs3.write([nr]), 0)
        # history from rollups after the fine data has aged out
        late = NOW + 5 * 86400
        app4 = build_app(root, pr, rp, board, lambda: late, [])
        srv4 = U.Server(("127.0.0.1", 0), app4, set(), U.load_assets())
        srv4.allowed_hosts = U.allowed_hosts_for(srv4.server_address[1], [])
        threading.Thread(target=srv4.serve_forever, daemon=True).start()
        c4 = Client(srv4)
        eq("aged-out fine data: nothing in memory", app4.beams.size(), 0)
        s, h, j = c4.get(f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5")
        eq(
            "history: beam stats for the aged-out hour come from the rollup (same exact values)",
            (j["windows"], j["beams"][0]["power_mean_db"], j["beams"][0]["leader_share"], j["beams"][0]["rel_ring_db"]),
            (3, -14.69, 0.5, 5.65),
        )
        s, h, j = c4.get(f"/api/detections/species?since={HC}&until={HC + 3600}")
        names4 = {x["scientific"]: x for x in j["species"]}
        eq(
            "history: species summary from rollups (Aus a 1 event, 3 reports, bounds approximate)",
            (names4["Aus a"]["events"], names4["Aus a"]["reports"], names4["Aus a"]["bounds_exact"]),
            (1, 3, False),
        )
        s, h, j = c4.get(f"/api/system?since={late - 86400 * 6}")
        eq(
            "history: system stream hourly rows come from the rollups (incl. the HO row written by the torn-tail test)",
            [(x["hour_s"], x["loss_pct_mean"]) for x in j["stream"]["hourly"]],
            [(HC, 1.5), (HO, 1.0)],
        )
        s, h, j = c4.get(f"/api/detections/events?since={HC}&until={HC + 3600}&in_region=any")
        eq("events older than the window are flagged, not silently empty", (j["total"], j["truncated_by_window"]), (0, True))
        srv4.shutdown()
        srv.shutdown()

        # ------------------------------------------------------------------ tailing
        td = os.path.join(root, "tail")
        os.makedirs(td)
        got = []
        tp = os.path.join(td, "f.jsonl")
        t = U.Tailer(tp, lambda o: got.append(o) or True)
        eq("tail: missing file polls as absent, no error", t.poll(), False)
        open(tp, "wb").write(b'{"a":1}\n{"a":2}\n{"a":3')
        t.poll()
        eq("tail: partial last line is held back", [o["a"] for o in got], [1, 2])
        open(tp, "ab").write(b'}\nGARBAGE\n{"a":4}\n')
        t.poll()
        eq("tail: the partial line completes on the next poll, garbage is counted and skipped", ([o["a"] for o in got], t.garbage), ([1, 2, 3, 4], 1))
        t.poll()
        eq("tail: polling an unchanged file reads nothing twice", [o["a"] for o in got], [1, 2, 3, 4])
        open(tp, "wb").write(b'{"a":9}\n')
        t.poll()
        eq("tail: truncation restarts from 0", ([o["a"] for o in got][-1], t.resets), (9, 1))
        open(tp, "ab").write(b'{"a":10}\n')
        t.poll()
        os.replace(tp, tp + ".1")
        open(tp + ".1", "ab").write(b'{"a":11}\n')  # written to the old file after our last poll
        open(tp, "wb").write(b'{"a":12}\n')
        t.poll()
        eq("tail: rotation finishes the old file from .1, then reads the new file from 0", [o["a"] for o in got][-3:], [10, 11, 12])
        eq("tail: rotation counted", t.resets, 2)
        t2got = []
        tp2 = os.path.join(td, "g.jsonl")
        with open(tp2, "wb") as f:
            for i in range(1000):
                f.write(b'{"i":%d,"pad":"%s"}\n' % (i, b"x" * 100))
        t2 = U.Tailer(tp2, lambda o: t2got.append(o["i"]) or True)
        t2.backfill(5000)
        check(
            "backfill reads only a bounded tail, starting at a full line, ending at EOF",
            0 < len(t2got) < 60 and t2got[-1] == 999 and t2got == list(range(t2got[0], 1000)),
            f"{len(t2got)}",
        )
        open(tp2, "ab").write(b'{"i":1000,"pad":""}\n')
        t2.poll()
        eq("after backfill the next poll continues without duplicates", t2got[-2:], [999, 1000])
        t3 = U.Tailer(os.path.join(td, "h.jsonl"), lambda o: o.get("ok") is True)
        open(os.path.join(td, "h.jsonl"), "wb").write(b'{"ok":true}\n{"ok":false}\n[1,2]\n{"ok":true}\n')
        t3.poll()
        eq("tail: records the handler rejects are counted (rejected), garbage separate", (t3.lines, t3.rejected, t3.garbage), (4, 2, 0))

        # ------------------------------------------------------------------ bulk shape guards
        bs = U.BeamStore(3600)
        bs.set_cutoff(0)
        for i in range(5):
            bs.add(bands_rec(1000 + i * 1000))
        bs.housekeeping(1000 + 4 * 1000 + 100)
        eq("eviction keeps only records inside the window (3600 s before the newest clock)", bs.size(), 4)
        eq(
            "a malformed beam_bands record is rejected (db of wrong shape)",
            bs.add({"k": "beam_bands", "v": 1, "t_us": 5, "zero_frac": 0, "db": [[0] * 8] * 7}),
            False,
        )
        eq("a beam_bands record with unknown major version is rejected", bs.add(dict(bands_rec(9999), v=2)), False)

        # ------------------------------------------------------------------ contracts
        try:
            import jsonschema  # noqa: F401

            have = True
        except ImportError:
            have = False
        check("jsonschema is available (the schema checks are mandatory)", have)
        if have:
            sys.path.insert(0, os.path.normpath(os.path.join(UI_DIR, "..", "contracts")))
            import check_contracts as C

            sch = json.load(open(os.path.join(UI_DIR, "..", "contracts", "schemas", "ui_http.schema.json"), encoding="utf-8"))
            val = lambda name, obj: [
                e.message for e in C.errors(C.Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/" + name, "$defs": sch["$defs"]}), obj)
            ]
            srv5 = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
            srv5.allowed_hosts = U.allowed_hosts_for(srv5.server_address[1], [])
            threading.Thread(target=srv5.serve_forever, daemon=True).start()
            c5 = Client(srv5)
            for name, path in (
                ("status", "/api/status"),
                ("events", f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0"),
                ("density", f"/api/detections/density?since={HC}&until={NOW}&in_region=any&min_conf=0"),
                ("species", f"/api/detections/species?since={HC}&until={NOW}"),
                ("beams_live", "/api/beams/live"),
                ("beams_stats", f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5"),
                ("beams_heatmap", f"/api/beams/heatmap?since={HO}&until={HO + 600}&band=5&bucket_s=60"),
                ("system", f"/api/system?since={HC - 3600}"),
                ("calibration", "/api/calibration"),
            ):
                s, h, j = c5.get(path)
                eq(f"schema: live {name} payload validates", (s, val(name, j)), (200, []))
            s, h, j = c5.get("/api/beams/stats?band=99")
            eq("schema: a real 422 validates as problem", (s, val("problem", j)), (422, []))
            s, h, j = c5.get("/api/beams/live")
            j["bands"][0]["band"] = 9
            check("schema: negative twin - a band index of 9 is rejected", val("beams_live", j) != [])
            s, h, j = c5.get("/api/status")
            del j["any_stale"]
            check("schema: negative twin - a missing required field is rejected", val("status", j) != [])
            s, h, j = c5.get(f"/api/detections/density?since={HC}&until={NOW}&in_region=any&min_conf=0")
            j["rows"][0]["group"] = "fish"
            check("schema: negative twin - a density row with a group outside the enum is rejected", val("density", j) != [])
            s, h, j = c5.get(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=1")
            s, h, j = c5.get("/api/detections/event?id=" + j["events"][0]["id"])
            eq("schema: live event-detail payload validates (200, no errors)", (s, val("event", j)), (200, []))
            j["reports"][0]["level_dbfs"] = "loud"
            check("schema: negative twin - a report level that is a string is rejected", val("event", j) != [])
            s, h, j = c5.get(
                "/api/detections/event?id=" + c5.get(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=1")[2]["events"][0]["id"]
            )
            del j["trace_truncated"]
            check("schema: negative twin - an event detail without trace_truncated is rejected", val("event", j) != [])
            s, h, j = c5.get(
                "/api/detections/event?id=" + c5.get(f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&limit=1")[2]["events"][0]["id"]
            )
            j["reports"][0]["confidence"] = 1.5
            check("schema: negative twin - a report confidence above 1 is rejected", val("event", j) != [])
            s, h, j = c5.get(f"/api/detections/species?since={HC}&until={NOW}")
            del j["groups"]
            check("schema: negative twin - species without groups[] is rejected", val("species", j) != [])
            srv5.shutdown()
    finally:
        board.close()
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{n_checks} checks, {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
