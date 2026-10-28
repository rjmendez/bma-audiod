#!/usr/bin/env python3
"""Hermetic tests for the dashboard's advisory artifact flags (ui-http 1.11.0, contract artifact-gate): the optional `artifact_flags`
object on insect / mechanical events, the `artifact` query parameter (any | none | only) on events, density and species, the counts
that make a hidden event visible, the status block, and every failure mode (no ambient file, an old record, no transients, a gate that
is off). Synthetic pipeline files in temp dirs, a fixed or mutable clock, the real App; one real HTTP round trip for the parameter check.

    python -I test_artifact_ui.py
bma_ui.py and artifact_gate.py are imported from this checkout (the mutation runs copy ui/ and analysis/artifact_gate.py and run the copy)."""

import json, os, shutil, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402
import test_bma_ui as T  # noqa: E402

fails, n_checks = [], 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


H0 = T.H0  # an exact UTC hour start
KATY = ("Common Virtuoso Katydid", "Amblycorypha longinicta")
WREN = ("Alpha Wren", "Aus a")
ENGINE = ("Engine", "Engine")


def rep(t, common_sci, beam=1, conf=0.8):
    return {
        "k": "bird",
        "t": t,
        "beam": beam,
        "level_dbfs": -60.0,
        "common": common_sci[0],
        "scientific": common_sci[1],
        "confidence": conf,
        "provenance": "model_opinion",
        "in_region": True,
    }


def amb(hour, temp):
    return {"k": "ambient_temp", "v": 1, "t_hour_utc": hour, "temp_c": temp, "source": "open-meteo", "model_grid_km": None}


def dsp_rec(t):
    return {"k": "dsp", "t": t, "rms_dbfs": [-70.0] * 8}


def trans(t, spread):
    return {"k": "transient", "t": t, "excess_db": 10.0, "level_db": -40.0, "loudest_beam": 2, "beam_spread_db": spread}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_app(root, birds, ambient=None, dsp=None, gate=True, now=None, rollups=None):
    pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "history"), rp):
        os.makedirs(d, exist_ok=True)
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), birds)
    if ambient is not None:
        T.jl(os.path.join(pr, "history", "ambient_temp.jsonl"), ambient)
    if dsp is not None:
        T.jl(os.path.join(pr, "analysis", "dsp.jsonl"), dsp)
    rd = os.path.join(root, "rollups")
    if rollups:
        os.makedirs(rd, exist_ok=True)
        T.jl(os.path.join(rd, "rollup_20261022.jsonl"), rollups)
    clock = Clock(now if now is not None else H0 + 6 * 3600)
    app = U.App(pr, rp, rd, U.Board("", "", enabled=False), window_h=48, clock=clock, log=lambda m: None, slot_fix_at=0, artifact_gate=gate)
    app.start_backfill()
    return app, clock, pr


def span(now):
    return {"since": str(int(now - 7 * 86400)), "until": str(int(now) + 1), "min_conf": "0", "in_region": "any", "limit": "500"}


# ---------------------------------------------------------------- the main scene: four hours at 2.0, 12.0, 5.0 (the floor) and 4.9 C
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    TCOLD, TWARM, TFLOOR, TJUST = H0 + 1800, H0 + 3600 + 1800, H0 + 7200 + 1800, H0 + 10800 + 1800
    birds = [
        rep(TCOLD, KATY),
        rep(TWARM, KATY),
        rep(TFLOOR, KATY),
        rep(TJUST, KATY),
        rep(TCOLD + 100, WREN),
        rep(TCOLD + 200, ENGINE),
        {
            "k": "bird",
            "t": TCOLD + 300,
            "beam": 2,
            "level_dbfs": -60.0,
            "common": "Human vocal",
            "scientific": "Homo sapiens",
            "confidence": 0.9,
            "provenance": "model_opinion",
            "in_region": True,
        },
        {
            "k": "bird",
            "t": TCOLD + 400,
            "beam": 2,
            "level_dbfs": -60.0,
            "common": "Human non-vocal",
            "scientific": "Human non-vocal",
            "confidence": 0.9,
            "provenance": "model_opinion",
            "in_region": True,
        },
    ]
    app, clock, pr = make_app(root, birds, ambient=[amb(H0, 2.0), amb(H0 + 3600, 12.0), amb(H0 + 7200, 5.0), amb(H0 + 10800, 4.9)])
    az = app.calibration()["beam_azimuth_deg"]
    ev = app.api_events(dict(span(NOW)))
    by_t = {e["t_start_s"]: e for e in ev["events"]}
    eq(
        "events: the four katydid events and the wren are listed (the Engine is a non-species class, off by default); the two human-voice labels are not (5 events)",
        ev["total"],
        5,
    )
    cold = by_t[TCOLD]["artifact_flags"]
    eq(
        "cold katydid (2.0 C): flags exactly ['cold_insect'], level likely, evidence temp 2.0 age 0 floor 5.0, no transient feed so burst fields null",
        cold,
        {
            "v": 1,
            "flags": ["cold_insect"],
            "level": "likely",
            "evidence": {"temp_c": 2.0, "temp_age_s": 0, "floor_c": 5.0, "burst_n": None, "burst_spread_db": None, "window_s": None},
        },
    )
    eq(
        "positive twin: the katydid at 12.0 C has no flag at all and level none",
        by_t[TWARM]["artifact_flags"],
        {
            "v": 1,
            "flags": [],
            "level": "none",
            "evidence": {"temp_c": 12.0, "temp_age_s": 0, "floor_c": 5.0, "burst_n": None, "burst_spread_db": None, "window_s": None},
        },
    )
    eq(
        "boundary: exactly 5.0 C is not flagged (strict floor)",
        (by_t[TFLOOR]["artifact_flags"]["flags"], by_t[TFLOOR]["artifact_flags"]["evidence"]["temp_c"]),
        ([], 5.0),
    )
    eq("boundary twin: 4.9 C is flagged", (by_t[TJUST]["artifact_flags"]["flags"], by_t[TJUST]["artifact_flags"]["level"]), (["cold_insect"], "likely"))
    wren = next(e for e in ev["events"] if e["species"] == "Alpha Wren")
    eq("a bird event has NO artifact_flags key at all (group other-bio)", ("artifact_flags" in wren, wren["group"]), (False, "other-bio"))
    eng = next(e for e in app.api_events(dict(span(NOW), non_species="any"))["events"] if e["species"] == "Engine")
    eq(
        "a mechanical event carries the informational mechanical_label only, level none (never an artifact verdict)",
        (eng["artifact_flags"]["flags"], eng["artifact_flags"]["level"], eng["group"]),
        (["mechanical_label"], "none", "mechanical"),
    )
    check(
        "human-voice labels stay hidden everywhere: no label, scientific name or id of one in events, density or species",
        not any(
            "uman" in json.dumps(x) or "Homo" in json.dumps(x)
            for x in (
                app.api_events(dict(span(NOW), non_species="any")),
                app.api_density(dict(span(NOW), non_species="any")),
                app.api_species({"since": str(H0), "until": str(NOW), "non_species": "any", "in_region": "any"}),
            )
        ),
    )
    eq("human_hidden_24h counts the two hidden events and nothing else", app.api_status({})["human_hidden_24h"], 2)

    # default = explicit any: nothing changes unless the toggle is used
    eq(
        "artifact=any answers exactly what the request without the parameter answers (apart from the echo)",
        dict(app.api_events(dict(span(NOW), artifact="any")), artifact=None),
        dict(ev, artifact=None),
    )
    eq("the default echo is 'any'", ev["artifact"], "any")
    none_ev = app.api_events(dict(span(NOW), artifact="none"))
    eq(
        "artifact=none leaves out exactly the two flagged katydid events (cold 2.0 and 4.9): 3 remain (the 12.0 C and 5.0 C katydids and the wren)",
        sorted((e["species"], e["t_start_s"]) for e in none_ev["events"]),
        sorted([(KATY[0], TWARM), (KATY[0], TFLOOR), ("Alpha Wren", TCOLD + 100)]),
    )
    only_ev = app.api_events(dict(span(NOW), artifact="only"))
    eq("artifact=only keeps just the two flagged events", sorted(e["t_start_s"] for e in only_ev["events"]), sorted([TCOLD, TJUST]))
    eq(
        "none + only partition the flagged view: 3 + 2 listed, total echoes",
        (none_ev["total"], only_ev["total"], none_ev["artifact"], only_ev["artifact"]),
        (3, 2, "none", "only"),
    )
    check(
        "a mechanical event (level none) is not 'flagged': artifact=none keeps the Engine row when non_species=any",
        [e["species"] for e in app.api_events(dict(span(NOW), artifact="none", non_species="any"))["events"] if e["species"] == "Engine"] == ["Engine"],
    )

    sp_all = app.api_species({"since": str(H0), "until": str(NOW)})
    row = next(s for s in sp_all["species"] if s["species"] == KATY[0])
    eq(
        "species (default): the katydid row counts all 4 events and shows 2 flagged; the response counts 2 flagged in total, 0 unscored",
        (row["events"], row["artifact_events"], sp_all["artifact_events_total"], sp_all["artifact_unscored_events"], sp_all["artifact"]),
        (4, 2, 2, 0, "any"),
    )
    sp_none = app.api_species({"since": str(H0), "until": str(NOW), "artifact": "none"})
    row2 = next(s for s in sp_none["species"] if s["species"] == KATY[0])
    eq(
        "species artifact=none: the katydid row falls to 2 events, 0 flagged, but the 2 hidden events are still counted in artifact_events_total",
        (row2["events"], row2["artifact_events"], sp_none["artifact_events_total"], sp_none["events_total"]),
        (2, 0, 2, sp_all["events_total"] - 2),
    )
    sp_only = app.api_species({"since": str(H0), "until": str(NOW), "artifact": "only"})
    eq(
        "species artifact=only: only the katydid row with its 2 flagged events",
        [(s["species"], s["events"], s["artifact_events"]) for s in sp_only["species"]],
        [(KATY[0], 2, 2)],
    )
    wr = next(s for s in sp_all["species"] if s["species"] == "Alpha Wren")
    eq(
        "species: a bird row has artifact_events 0 and is untouched by the toggle (same events in none mode)",
        (wr["artifact_events"], next(s for s in sp_none["species"] if s["species"] == "Alpha Wren")["events"], wr["events"]),
        (0, 1, 1),
    )
    dn = app.api_density(dict(span(NOW), artifact="none", non_species="any", bucket_s="3600"))
    eq("density honours artifact=none: 4 events (unflagged katydids 2, wren, engine) and echoes the mode", (dn["total"], dn["artifact"]), (4, "none"))
    detail = app.api_event({"id": "%d-amblycorypha-longinicta" % (TCOLD * 1000)})
    eq("event detail carries the same artifact_flags as the list", detail["event"]["artifact_flags"], cold)
    st = app.api_status({})["artifact_gate"]
    eq(
        "status.artifact_gate: enabled, floor 5.0, 3 h max age, 30 s window, 20 transients, 4 ambient records from H0 to H0+3h, 0 skipped, 0 transients read, 5 events scored (4 katydid + the engine), 2 flagged",
        (
            st["enabled"],
            st["available"],
            st["floor_c"],
            st["temp_max_age_s"],
            st["burst_window_s"],
            st["burst_min_transients"],
            st["ambient"]["records"],
            st["ambient"]["first_hour_s"],
            st["ambient"]["newest_hour_s"],
            st["ambient"]["skipped"],
            st["transients"]["in_memory"],
            st["events_scored_24h"],
            st["events_flagged_24h"],
        ),
        (True, True, 5.0, 10800, 30.0, 20, 4, H0, H0 + 10800, 0, 0, 5, 2),
    )

    def keys_of(o):
        return {k for d in ([o] if isinstance(o, dict) else []) for k in d} | {
            k for v in (o.values() if isinstance(o, dict) else o if isinstance(o, list) else []) for k in keys_of(v)
        }

    check(
        "no coordinate key and no data-directory path anywhere in the served artifact data (events, species, status)",
        not (keys_of([ev, sp_all, st]) & {"lat", "lon", "latitude", "longitude", "path"}) and pr not in json.dumps([ev, sp_all, st]),
    )

    # one real HTTP round trip: the parameter is validated like every other one
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
    srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], [])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = T.Client(srv)
    s, _h, j = c.get("/api/detections/species?since=%d&until=%d&artifact=none" % (H0, NOW))
    eq("HTTP: artifact=none is accepted (200) and echoed", (s, (j or {}).get("artifact")), (200, "none"))
    s, _h, j = c.get("/api/detections/species?since=%d&until=%d&artifact=bogus" % (H0, NOW))
    eq("HTTP: an unknown artifact mode is 422 invalid_parameter naming the parameter", (s, j["code"], j["param"]), (422, "invalid_parameter", "artifact"))
    s, _h, j = c.get("/api/detections/events?since=%d&until=%d&artifact=none&artifact=only" % (H0, NOW))
    eq("HTTP: a repeated artifact parameter is 422", s, 422)
    srv.shutdown()
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- the reader rule: nearest past hour, at most 3 h old
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 8 * 3600
    t3, t4 = H0 + 3 * 3600 + 100, H0 + 4 * 3600 + 100
    app, clock, pr = make_app(root, [rep(t3, KATY), rep(t4, KATY)], ambient=[amb(H0, 2.0)], now=NOW)
    by_t = {e["t_start_s"]: e["artifact_flags"] for e in app.api_events(dict(span(NOW)))["events"]}
    eq(
        "a record exactly 3 h old (age 10800 s) is used: cold_insect with temp_age_s 10800",
        (by_t[t3]["flags"], by_t[t3]["evidence"]["temp_age_s"]),
        (["cold_insect"], 10800),
    )
    eq(
        "a record 4 h old is not used: temp_unknown, level none, temp null (no judgement, never an error)",
        (by_t[t4]["flags"], by_t[t4]["level"], by_t[t4]["evidence"]["temp_c"], by_t[t4]["evidence"]["temp_age_s"]),
        (["temp_unknown"], "none", None, None),
    )
    eq(
        "temp_unknown does not count as flagged: artifact=none keeps it",
        [e["t_start_s"] for e in app.api_events(dict(span(NOW), artifact="none"))["events"]],
        [t4],
    )
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- no ambient file: temp_unknown, nothing hidden, nothing raises
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    app, clock, pr = make_app(root, [rep(H0 + 100, KATY), rep(H0 + 200, WREN)], ambient=None, now=NOW)
    evs = app.api_events(dict(span(NOW)))["events"]
    katy = next(e for e in evs if e["species"] == KATY[0])
    eq(
        "no ambient file: the katydid reads temp_unknown (level none) and the event is listed",
        (katy["artifact_flags"]["flags"], katy["artifact_flags"]["level"]),
        (["temp_unknown"], "none"),
    )
    eq(
        "no ambient file: artifact=none hides nothing (both events remain), artifact=only is empty",
        (len(app.api_events(dict(span(NOW), artifact="none"))["events"]), len(app.api_events(dict(span(NOW), artifact="only"))["events"])),
        (2, 0),
    )
    st = app.api_status({})["artifact_gate"]
    eq(
        "no ambient file: status says so (0 records, null newest hour and age) with the gate still enabled",
        (st["enabled"], st["ambient"]["records"], st["ambient"]["newest_hour_s"], st["ambient"]["newest_age_s"]),
        (True, 0, None, None),
    )
    # a corrupt ambient file is the same as none, and a late file is picked up (the clock moves past the 30 s re-check)
    os.makedirs(os.path.join(pr, "history"), exist_ok=True)
    with open(os.path.join(pr, "history", "ambient_temp.jsonl"), "wb") as f:
        f.write(b'not json\n{"k":"ambient_temp","v":1,"t_hour_utc":7,"temp_c":1.0}\n[1,2]\n')
    clock.t = NOW + 60
    st = app.api_status({})["artifact_gate"]
    eq(
        "a corrupt ambient file (garbage, a record off the hour grid, a non-object) reads as no data and does not raise: 0 records, 2 skipped records (the unparsable line is not a record)",
        (st["ambient"]["records"], st["ambient"]["skipped"]),
        (0, 2),
    )
    T.jl(os.path.join(pr, "history", "ambient_temp.jsonl"), [amb(H0, 3.0)])
    clock.t = NOW + 120
    katy = next(e for e in app.api_events(dict(span(NOW), since=str(H0)))["events"] if e["species"] == KATY[0])
    eq(
        "a temperature record that appears later is used at the next re-check: the same event becomes cold_insect (the cached verdict is not stale)",
        (katy["artifact_flags"]["flags"], katy["artifact_flags"]["evidence"]["temp_c"]),
        (["cold_insect"], 3.0),
    )
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- bursts: the +-30 s window of dsp transients, only when covered
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    E1, E2, E3, E4, E5 = H0 + 1000, H0 + 2000, H0 + 3000, H0 + 4000, H0 + 5000
    dsp = [dsp_rec(H0 + 500), dsp_rec(H0 + 6000)]  # the span the dsp file covers
    dsp += [trans(E1 - 10 + i, 1.0) for i in range(20)]  # 20 at spread 1.0: the burst
    dsp += [trans(E2 - 10 + i, 1.0) for i in range(19)]  # 19: one short
    dsp += [trans(E3 - 10 + i, 3.0 if i % 2 else 3.2) for i in range(20)]  # 20, median 3.1 dB: too spread
    dsp += [trans(E4 - 10 + i, 3.0) for i in range(20)]  # 20, median exactly 3.0: still a burst (<= 3.0)
    app, clock, pr = make_app(
        root, [rep(E1, KATY), rep(E2, KATY), rep(E3, KATY), rep(E4, KATY), rep(E5, KATY)], ambient=[amb(H0, 20.0), amb(H0 + 3600, 20.0)], dsp=dsp, now=NOW
    )
    by_t = {e["t_start_s"]: e["artifact_flags"] for e in app.api_events(dict(span(NOW)))["events"]}
    eq(
        "20 transients at 1.0 dB within +-30 s on a warm hour: burst_coincident, level suspect (never likely), burst_n 20, spread 1.0, window 30.0",
        (by_t[E1]["flags"], by_t[E1]["level"], by_t[E1]["evidence"]["burst_n"], by_t[E1]["evidence"]["burst_spread_db"], by_t[E1]["evidence"]["window_s"]),
        (["burst_coincident"], "suspect", 20, 1.0, 30.0),
    )
    eq("19 transients: no burst flag (boundary), burst_n 19", (by_t[E2]["flags"], by_t[E2]["evidence"]["burst_n"]), ([], 19))
    eq("20 transients with median spread 3.1 dB: no burst flag", (by_t[E3]["flags"], by_t[E3]["evidence"]["burst_spread_db"]), ([], 3.1))
    eq("boundary twin: median spread exactly 3.0 dB is a burst", (by_t[E4]["flags"], by_t[E4]["evidence"]["burst_spread_db"]), (["burst_coincident"], 3.0))
    eq("an event in a covered quiet stretch has burst_n 0 (a real zero, the dsp file covers it)", (by_t[E5]["flags"], by_t[E5]["evidence"]["burst_n"]), ([], 0))
    eq(
        "artifact=only returns exactly the two burst events",
        sorted(e["t_start_s"] for e in app.api_events(dict(span(NOW), artifact="only"))["events"]),
        [E1, E4],
    )
    st = app.api_status({})["artifact_gate"]
    eq(
        "status transients: 79 read (20 + 19 + 20 + 20), the span read starts at the first dsp record H0+500 and ends at the last, H0+6000",
        (st["transients"]["in_memory"], st["transients"]["first_t_s"], st["transients"]["last_t_s"]),
        (79, float(H0 + 500), float(H0 + 6000)),
    )
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- burst evidence needs coverage: outside the span read it is null, not a false "no burst"
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    EA, EB, EC = H0 + 100, H0 + 1000, H0 + 5990
    dsp = [dsp_rec(H0 + 500), dsp_rec(H0 + 6000)] + [trans(EB - 10 + i, 1.0) for i in range(20)]
    app, clock, pr = make_app(root, [rep(EA, KATY), rep(EB, KATY), rep(EC, KATY)], ambient=[amb(H0, 20.0), amb(H0 + 3600, 20.0)], dsp=dsp, now=NOW)
    by_t = {e["t_start_s"]: e["artifact_flags"]["evidence"] for e in app.api_events(dict(span(NOW)))["events"]}
    eq(
        "an event before the first dsp record read (window starts before it): burst_n null, window null",
        (by_t[EA]["burst_n"], by_t[EA]["window_s"]),
        (None, None),
    )
    eq("positive twin: a covered event of the same file has burst_n 20", by_t[EB]["burst_n"], 20)
    eq("an event whose +-30 s window runs past the last dsp record: burst_n null (the evidence is not complete yet)", by_t[EC]["burst_n"], None)
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- the gate off: no field anywhere
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    app, clock, pr = make_app(
        root, [rep(H0 + 100, KATY)], ambient=[amb(H0, 2.0)], dsp=[dsp_rec(H0 + 50), trans(H0 + 90, 1.0), dsp_rec(H0 + 150)], gate=False, now=NOW
    )
    eq("--no-artifact-gate: the dsp transients are not even read into memory (0 held although the file has one)", app.trans.summary()["in_memory"], 0)
    ev = app.api_events(dict(span(NOW)))
    check("--no-artifact-gate: no artifact_flags key on any event", all("artifact_flags" not in e for e in ev["events"]) and ev["total"] == 1)
    eq(
        "--no-artifact-gate: artifact=only matches nothing and none matches all (nothing is flagged), no error",
        (len(app.api_events(dict(span(NOW), artifact="only"))["events"]), len(app.api_events(dict(span(NOW), artifact="none"))["events"])),
        (0, 1),
    )
    eq(
        "--no-artifact-gate: status says disabled but available, feeds null",
        app.api_status({})["artifact_gate"],
        {"enabled": False, "available": True, "ambient": None, "transients": None, "events_scored_24h": None, "events_flagged_24h": None},
    )
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- rollup-sourced events are never scored: counted as unscored, not hideable
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    OLD = ((NOW - 60 * 3600) // 3600) * 3600  # an hour older than the 48 h in-memory window
    roll = [
        {
            "k": "ui_rollup",
            "v": 1,
            "kind": "detections",
            "hour_s": OLD,
            "species": [
                {
                    "scientific": KATY[1],
                    "common": KATY[0],
                    "events": 7,
                    "reports": 9,
                    "max_confidence": 0.9,
                    "beam_events": [1] * 8,
                    "in_region": True,
                    "non_species": False,
                },
                {
                    "scientific": WREN[1],
                    "common": WREN[0],
                    "events": 3,
                    "reports": 3,
                    "max_confidence": 0.9,
                    "beam_events": [1] * 8,
                    "in_region": True,
                    "non_species": False,
                },
            ],
        }
    ]
    app, clock, pr = make_app(root, [rep(H0 + 100, KATY)], ambient=[amb(H0, 2.0)], now=NOW, rollups=roll)
    p = {"since": str(OLD), "until": str(NOW)}
    sp = app.api_species(p)
    k = next(s for s in sp["species"] if s["species"] == KATY[0])
    eq(
        "rollup + live: the katydid row counts 7 rollup events + 1 live event, 1 flagged (the live one), 7 unscored",
        (k["events"], k["artifact_events"], sp["artifact_events_total"], sp["artifact_unscored_events"], k["bounds_exact"]),
        (8, 1, 1, 7, False),
    )
    spn = app.api_species(dict(p, artifact="none"))
    k = next(s for s in spn["species"] if s["species"] == KATY[0])
    eq(
        "artifact=none cannot leave rollup events out: 7 remain (the live flagged one goes), and they are reported as unscored",
        (k["events"], spn["artifact_unscored_events"], spn["artifact_events_total"]),
        (7, 7, 1),
    )
    spo = app.api_species(dict(p, artifact="only"))
    eq(
        "artifact=only: no rollup row (they are never flagged), just the live flagged event",
        [(s["species"], s["events"]) for s in spo["species"]],
        [(KATY[0], 1)],
    )
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------- the verdict cache: a final verdict is computed once, and a changed ambient file invalidates it
root = tempfile.mkdtemp(prefix="bma_artifact_ui_")
try:
    NOW = H0 + 6 * 3600
    EV = H0 + 1000
    app, clock, pr = make_app(root, [rep(EV, KATY)], ambient=[amb(H0, 2.0)], dsp=[dsp_rec(H0 + 500), dsp_rec(H0 + 6000)], now=NOW)
    calls = []
    real = U.AG.evaluate
    U.AG.evaluate = lambda **kw: (calls.append(kw["t"]), real(**kw))[1]  # a recording wrapper: the real gate still answers
    try:
        first = app.api_events(dict(span(NOW)))["events"][0]["artifact_flags"]
        n1 = len(calls)
        again = app.api_events(dict(span(NOW)))["events"][0]["artifact_flags"]
        eq(
            "a covered, final verdict is computed once: the second request calls the gate 0 more times and returns the same object",
            (n1, len(calls) - n1, again == first, first["flags"]),
            (1, 0, True, ["cold_insect"]),
        )
        T.jl(os.path.join(pr, "history", "ambient_temp.jsonl"), [amb(H0, 15.0)])  # the last record of an hour wins
        clock.t = NOW + 60
        after = app.api_events(dict(span(NOW)))["events"][0]["artifact_flags"]
        eq(
            "a changed ambient file invalidates the cache: the same event is now 15.0 C, no flag, level none, one more gate call",
            (after["flags"], after["level"], after["evidence"]["temp_c"], len(calls) - n1),
            ([], "none", 15.0, 1),
        )
    finally:
        U.AG.evaluate = real
finally:
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
