#!/usr/bin/env python3
"""Tests for reliability_report.py on synthetic trees (exact values, boundary twins, no live data, nothing outside temp).
Run:  python -I test_reliability_report.py"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reliability_report as R  # noqa: E402

fails, n_checks = [], 0
T0 = 1_800_000_000          # a multiple of 3600; UTC hour of day 8
TAXA = {"version": 1, "explicit_labels": {"engine": {"group": "mechanical", "non_species": True}, "ghost bird": {"group": "bird", "non_species": True}},
        "genera": {"bird": ["aaa", "bbb", "ccc", "ddd"], "insect": ["iii"], "amphibian": ["fff"]}}
SCRIPT = os.path.join(HERE, "reliability_report.py")


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def row(t, beam, conf, sci, common=None, k="bird", in_region=None, **kw):
    r = {"k": k, "t": t, "beam": beam, "level_dbfs": -50.0, "common": common or sci, "scientific": sci, "confidence": conf,
         "provenance": "model_opinion"}
    if in_region is not None:
        r["in_region"] = in_region
    r.update(kw)
    return r


TMP = tempfile.mkdtemp(prefix="relrep_all_")


def tree(rows, ambient=None, raw_birds=None):
    d = tempfile.mkdtemp(prefix="relrep_", dir=TMP)
    os.makedirs(os.path.join(d, "analysis"))
    os.makedirs(os.path.join(d, "history"))
    if rows is not None:
        with open(os.path.join(d, "analysis", "birds.jsonl"), "wb") as f:
            f.write(raw_birds if raw_birds is not None else b"".join(json.dumps(r).encode() + b"\n" for r in rows))
    if ambient is not None:
        with open(os.path.join(d, "history", "ambient_temp.jsonl"), "wb") as f:
            f.write(b"".join(json.dumps(a).encode() + b"\n" for a in ambient))
    with open(os.path.join(d, "taxa.json"), "w") as f:
        json.dump(TAXA, f)
    return d


def run(d, since=None):
    return R.analyse(d, since, os.path.join(d, "taxa.json"))


def cli(d, *args):
    return subprocess.run([sys.executable, "-I", SCRIPT, "--data-dir", d, "--taxa", os.path.join(d, "taxa.json")] + list(args), capture_output=True)


def amb(h, temp):
    return {"k": "ambient_temp", "v": 1, "t_hour_utc": h, "temp_c": temp, "source": "open-meteo", "model_grid_km": None}


def events_rows(sci, specs, t0=T0, step=100.0, **kw):
    """specs = [(conf, [beams]), ...]: one event each, `step` seconds apart, one row per beam at the same time."""
    out = []
    for i, (c, beams) in enumerate(specs):
        out += [row(t0 + i * step, b, c, sci, **kw) for b in beams]
    return out


try:
    # ---- events: gap rule
    e1 = R.build_events([row(0.0, 1, 0.6, "Aaa one"), row(3.0, 2, 0.9, "Aaa one")])
    e2 = R.build_events([row(0.0, 1, 0.6, "Aaa one"), row(3.001, 2, 0.9, "Aaa one")])
    check("events: rows exactly 3.0 s apart are ONE event with max conf 0.9 and beams [1, 2]", e1 == [{"t": 0.0, "conf": 0.9, "beams": [1, 2], "n_rows": 2}], str(e1))
    check("events: rows 3.001 s apart are TWO events (twin of the 3.0 s case)", len(e2) == 2 and e2[1]["beams"] == [2] and e2[0]["beams"] == [1], str(e2))
    chain = R.build_events([row(0.0, 1, 0.5, "Aaa one"), row(2.5, 1, 0.5, "Aaa one"), row(5.0, 3, 0.5, "Aaa one")])
    check("events: a chain of 2.5 s gaps stays one event (gap is between consecutive rows)", len(chain) == 1 and chain[0]["beams"] == [1, 3], str(chain))
    check("events: unsorted input is sorted first", R.build_events([row(10, 1, .5, "A"), row(0, 2, .5, "A")])[0]["t"] == 0)

    # ---- exact statistics of one species: event 1 has two rows 2 s apart, events 2..4 in UTC hours 8, 9, 10
    rows = [row(T0, 1, 0.8, "Aaa alpha", "Alpha Bird"), row(T0 + 2, 2, 0.9, "Aaa alpha", "Alpha Bird"),
            row(T0 + 100, 3, 0.85, "Aaa alpha", "Alpha Bird"),
            row(T0 + 3600 + 100, 1, 0.5, "Aaa alpha", "Alpha Bird")] + \
           [row(T0 + 7200 + 100, b, 0.3, "Aaa alpha", "Alpha Bird") for b in (1, 2, 3)]
    d = tree(rows)
    s = run(d)["species"]["Aaa alpha"]
    exp = {"events": 4, "events_high": 2, "share_high": 0.5, "median_conf": 0.675, "max_conf": 0.9, "mean_beams": 1.75,
           "share_multibeam": 0.5, "share_multibeam_high": 0.5, "share_multibeam_high_lower": 0.1209, "hours_active": 3, "temp_known": None, "share_below_5c": None,
           "share_below_10c": None}
    check("stats: exact numbers of a known species", s["stats"] == exp, str(s["stats"]))
    check("stats: common name and group from the taxonomy", s["common"] == "Alpha Bird" and s["group"] == "bird", str((s["common"], s["group"])))
    shutil.rmtree(d)

    d = tree(events_rows("Aaa edge", [(0.85, [1]), (0.8499, [1])]))
    st = run(d)["species"]["Aaa edge"]["stats"]
    check("high: 0.85 is high, 0.8499 is not -> exactly 1 of 2", st["events_high"] == 1 and st["share_high"] == 0.5, str(st))
    shutil.rmtree(d)

    # ---- verdict boundaries
    def verdict(specs, sci="Aaa v"):
        d = tree(events_rows(sci, specs))
        try:
            return run(d)["species"][sci]["verdict"]
        finally:
            shutil.rmtree(d)

    two = lambda n, c=0.95: [(c, [1, 2])] * n
    check("verdict: 9 events -> too few", verdict(two(9)) == R.V_FEW)
    check("verdict: 10 events (5+ high, all multi-beam) -> trustworthy at 0.85 (twin of 9)", verdict(two(10)) == R.V_TRUST)
    check("verdict: 10 events, 4 high -> needs second beam", verdict(two(4) + [(0.5, [1, 2])] * 6) == R.V_BEAM)
    check("verdict: 10 events, 5 high -> trustworthy (twin of 4 high; 5 of 10 events multi-beam is exactly the species-wide minimum)", verdict(two(5) + [(0.5, [1])] * 5) == R.V_TRUST)
    check("verdict: 5 high, 3 multi-beam (0.6) -> needs second beam", verdict([(0.9, [1, 2])] * 3 + [(0.9, [1])] * 2 + [(0.5, [1])] * 5) == R.V_BEAM)
    check("verdict: 40 high, 32 multi-beam (0.8, the point threshold, lower bound 0.678) -> trustworthy", verdict([(0.9, [1, 2])] * 32 + [(0.9, [1])] * 8) == R.V_TRUST)
    check("verdict: 40 high, 31 multi-beam (0.775, just under the point threshold) -> needs second beam", verdict([(0.9, [1, 2])] * 31 + [(0.9, [1])] * 9) == R.V_BEAM)
    check("verdict: 10 events none high -> 'no high-confidence events', not 'needs second beam'", verdict([(0.5, [1, 2])] * 10) == R.V_NOHIGH)
    sp_ = {}
    d = tree(events_rows("Aaa none", [(0.5, [1, 2])] * 10) + events_rows("Aaa one", [(0.9, [1, 2])] + [(0.5, [1, 2])] * 9))
    sp_ = run(d)["species"]
    shutil.rmtree(d)
    check("verdict detail: no high events names the count and the median, exact", sp_["Aaa none"]["detail"] == "none of 10 events reaches 0.85 (median 0.50)", sp_["Aaa none"]["detail"])
    check("verdict detail: ONE high event reads '1 event', not '1 events' (twin of the zero case)", (sp_["Aaa one"]["verdict"], sp_["Aaa one"]["detail"]) == (R.V_BEAM, "only 1 event at >= 0.85"), str(sp_["Aaa one"]))
    check("suggest: no high-confidence events -> (0.85, 2)", R.suggest(R.V_NOHIGH) == (0.85, 2))
    # borderline: point estimate passes (0.8) but the lower bound does not; twin with the same share on twice the events
    check("verdict: 10 high, 8 multi-beam (0.8 but lower bound 0.54) -> needs second beam", verdict([(0.9, [1, 2])] * 8 + [(0.9, [1])] * 2) == R.V_BEAM)
    check("verdict: 20 high, 16 multi-beam (same 0.8, lower bound 0.68) -> trustworthy (twin)", verdict([(0.9, [1, 2])] * 16 + [(0.9, [1])] * 4) == R.V_TRUST)
    d = tree(events_rows("Aaa lb", [(0.9, [1, 2])] * 8 + [(0.9, [1])] * 2))
    s_lb = run(d)["species"]["Aaa lb"]
    shutil.rmtree(d)
    check("verdict detail: lower-bound case names share, count and both bounds, min_beams 2",
          s_lb["detail"] == "80% of 10 events at >= 0.85 are multi-beam, lower bound 54% < 60%" and s_lb["min_beams"] == 2 and s_lb["stats"]["share_multibeam_high_lower"] == 0.5408, str(s_lb))
    # a species trusted on its few loud events but mostly single-beam overall (Barn Owl pattern)
    owl = [(0.9, [1, 2])] * 5 + [(0.5, [1])] * 6
    check("verdict: 5 high all multi-beam, but only 5 of 11 events (45%) multi-beam -> needs second beam", verdict(owl) == R.V_BEAM)
    check("verdict: the same with 6 of 11 events (55%) multi-beam -> trustworthy (twin)", verdict([(0.9, [1, 2])] * 5 + [(0.5, [1, 2])] + [(0.5, [1])] * 5) == R.V_TRUST)
    d = tree(events_rows("Aaa owl", owl))
    check("verdict detail: the species-wide rule names the share, count and need", run(d)["species"]["Aaa owl"]["detail"] == "only 45% of all 11 events are multi-beam, need 50%", run(d)["species"]["Aaa owl"]["detail"])
    shutil.rmtree(d)

    check("verdict: 7 high, 5 multi-beam (0.714, just under 0.8) -> needs second beam", verdict([(0.9, [1, 2])] * 5 + [(0.9, [1])] * 2 + [(0.5, [1])] * 3) == R.V_BEAM)

    # ---- temperature plausibility
    def temp_case(sci, temps):
        """one event per temperature, each in its own UTC hour with a record for that hour."""
        rows, ambient = [], []
        for i, tc in enumerate(temps):
            t = T0 + i * 3600 + 10
            rows += [row(t, 1, 0.9, sci), row(t, 2, 0.9, sci)]
            ambient.append(amb(T0 + i * 3600, tc))
        d = tree(rows, ambient)
        try:
            s = run(d)["species"][sci]
            return s["verdict"], s["stats"]
        finally:
            shutil.rmtree(d)

    v, st = temp_case("Iii cold", [4.9] * 5 + [10.0] * 5)
    check("insect: 5 of 10 below 10 C (exact 0.5) -> suspect; 10.0 itself is not below; exact shares",
          v == R.V_TEMP and st["share_below_10c"] == 0.5 and st["share_below_5c"] == 0.5 and st["temp_known"] == 10, str((v, st)))
    v, st = temp_case("Iii warm", [4.9] * 4 + [10.0] * 6)
    check("insect: 4 of 10 below 10 C (0.4) -> trustworthy (twin)", v == R.V_TRUST and st["share_below_10c"] == 0.4, str((v, st)))
    # 12 events: 9 with a known (cold) temperature, 3 whose hour has no record within 3 h
    rows12 = []
    for i in range(12):
        t = T0 + (i * 3600 if i < 9 else 20 * 3600 + i * 3600) + 10
        rows12 += [row(t, 1, 0.9, "Iii part"), row(t, 2, 0.9, "Iii part")]
    d = tree(rows12, [amb(T0 + i * 3600, 4.9) for i in range(9)])
    s9 = run(d)["species"]["Iii part"]
    check("insect: 9 known cold temperatures among 12 events -> no temperature verdict (needs 10 known)", s9["stats"]["temp_known"] == 9 and s9["stats"]["share_below_10c"] == 1.0 and s9["verdict"] == R.V_TRUST, str(s9))
    shutil.rmtree(d)
    v, st = temp_case("Iii nine", [4.9] * 9)
    check("insect: only 9 known temperatures -> no temperature verdict even at 100% cold", v == R.V_FEW and st["share_below_10c"] == 1.0, str((v, st)))
    v, st = temp_case("Fff cold", [4.9] * 8 + [5.0] * 2)
    check("amphibian: 8 of 10 below 5 C (0.8) -> suspect; 5.0 is not below", v == R.V_TEMP and st["share_below_5c"] == 0.8 and st["share_below_10c"] == 1.0, str((v, st)))
    v, st = temp_case("Fff mild", [4.9] * 7 + [5.0] * 3)
    check("amphibian: 7 of 10 below 5 C (0.7) -> trustworthy: gentler than insects (twin)", v == R.V_TRUST and st["share_below_5c"] == 0.7, str((v, st)))
    v, st = temp_case("Fff ten", [9.0] * 10)
    check("amphibian: 100% under 10 C but none under 5 C is NOT flagged (only the 5 C rule applies)", v == R.V_TRUST and st["share_below_10c"] == 1.0 and st["share_below_5c"] == 0.0, str((v, st)))
    v, st = temp_case("Aaa bird", [0.0] * 10)
    check("bird group: temperature is not evaluated (no shares, not flagged)", v == R.V_TRUST and st["temp_known"] is None and st["share_below_10c"] is None, str((v, st)))

    # ---- ambient reader rule: 4 h old unknown, 2 h old used
    A = R.Ambient([amb(T0, 7.5)])
    check("ambient: event in the record's own hour uses it", A.at(T0 + 1800) == 7.5)
    check("ambient: 2 h old record is used", A.at(T0 + 2 * 3600 + 5) == 7.5)
    check("ambient: record 3 h older than the event's hour is used (boundary)", A.at(T0 + 3 * 3600 + 3599) == 7.5)
    check("ambient: 4 h old record is unknown (twin of the 2 h case)", A.at(T0 + 4 * 3600) is None)
    check("ambient: an event before the first record is unknown (never a later hour)", A.at(T0 - 1) is None)
    check("ambient: nearest PAST record wins over an older one, no interpolation", R.Ambient([amb(T0, 1.0), amb(T0 + 3600, 9.0)]).at(T0 + 3600 + 5) == 9.0)
    check("ambient: duplicate hour, last line wins", R.Ambient([amb(T0, 1.0), amb(T0, 2.0)]).at(T0) == 2.0)
    check("ambient: out-of-range, NaN, bool and non-int hours are skipped",
          R.Ambient([amb(T0, 61.0), amb(T0, float("nan")), dict(amb(T0, 5.0), t_hour_utc=True), dict(amb(T0, 5.0), t_hour_utc=1.5)]).hours == [])
    check("ambient: -90 and 60 are accepted (range ends)", R.Ambient([amb(T0, -90.0), amb(T0 + 3600, 60.0)]).temps == [-90.0, 60.0])
    check("ambient: missing history dir -> unknown", R.Ambient.load(os.path.join(TMP, "nope")).at(T0) is None)
    d = tree([], [amb(T0 - 3600, 3.0)])
    with open(os.path.join(d, "history", "ambient_temp.jsonl.1"), "wb") as f:
        f.write(json.dumps(amb(T0 - 7200, 1.0)).encode() + b"\n{bad json\n")
    with open(os.path.join(d, "history", "ambient_temp.jsonl"), "ab") as f:
        f.write(b"{not json")      # a writer that died mid-line
    L = R.Ambient.load(os.path.join(d, "history"))
    check("ambient: reads .1 then current, skips a torn last line", L.hours == [T0 - 7200, T0 - 3600] and L.temps == [1.0, 3.0], str((L.hours, L.temps)))
    shutil.rmtree(d)
    for older, newer, want in (("ambient_temp.jsonl.1", "ambient_temp.jsonl", 2.0), ("ambient_temp.jsonl", "ambient_temp.jsonl.1", 1.0)):
        d = tree([])
        for name, val in ((older, 1.0), (newer, 2.0)):
            with open(os.path.join(d, "history", name), "wb") as f:
                f.write(json.dumps(amb(T0, val)).encode() + b"\n")
        got = R.Ambient.load(os.path.join(d, "history")).at(T0)
        check("ambient: the same hour in .1 (%g) and in the current file (%g) -> the current file's value wins" % (
            1.0 if older.endswith(".1") else 2.0, 2.0 if older.endswith(".1") else 1.0),
            got == (2.0 if older.endswith(".1") else 1.0), str(got))
        shutil.rmtree(d)

    def aged(age_h):
        rows = []
        for i in range(10):
            t = T0 + age_h * 3600 + i * 10
            rows += [row(t, 1, 0.9, "Iii age"), row(t, 2, 0.9, "Iii age")]
        d = tree(rows, [amb(T0, 2.0)])
        try:
            return run(d)["species"]["Iii age"]
        finally:
            shutil.rmtree(d)

    s4, s2 = aged(4), aged(2)
    check("e2e: events 4 h after the last record have no temperature (known 0, not flagged)", s4["stats"]["temp_known"] == 0 and s4["verdict"] == R.V_TRUST, str(s4["stats"]))
    check("e2e: events 2 h after the record use it (10 known, all below 5 C, flagged)", s2["stats"]["temp_known"] == 10 and s2["verdict"] == R.V_TEMP, str(s2["stats"]))

    # ---- confusions
    def conf_case(n_reports, n_partner, conf=0.7, dt=1.0, partner_sci="Ccc near", partner_extra=(), partner_in_region=None,
                  far_in_region=False, far_k="bird_filtered", taxa_ok=True, partner_kw=None, spacing=1000):
        """n_reports filtered 'Ccc far' reports `spacing` s apart; the first n_partner have an in-region partner `dt` s later."""
        rows = []
        for i in range(n_reports):
            t = T0 + i * spacing
            rows.append(row(t, 1, conf, "Ccc far", "Far Bird", k=far_k, in_region=far_in_region))
            if i < n_partner:
                rows.append(row(t + dt, 2, 0.4, partner_sci, "Near Bird", in_region=partner_in_region, **(partner_kw or {})))
        rows += list(partner_extra)
        d = tree(rows)
        try:
            return R.analyse(d, None, os.path.join(d, "taxa.json" if taxa_ok else "no_such_taxa.json"))
        finally:
            shutil.rmtree(d)

    def cand(rep):
        return rep["confusion_candidates"]["Ccc far"]

    c = cand(conf_case(10, 6))
    check("confusion: 10 reports, 6 with the partner (share 0.6) qualifies, exact counts",
          c["qualifies"] and c["reports"] == 10 and c["co_occurring"] == 6 and c["share"] == 0.6 and c["partner_label"] == "Near Bird", str(c))
    c = cand(conf_case(10, 5))
    check("confusion: share 0.5 does not qualify (reason names the numbers)", not c["qualifies"] and c["reason"] == "best partner in 5 of 10 reports (50%), need 60%", str(c))
    c = cand(conf_case(9, 9))
    check("confusion: 9 reports do not qualify even at 100% co-occurrence", not c["qualifies"] and c["reason"] == "9 reports, need 10", str(c))
    c = cand(conf_case(10, 10))
    check("confusion: 10 reports at 100% qualifies (twin of 9)", c["qualifies"] and c["share"] == 1.0, str(c))
    check("confusion: reports below 0.7 are not counted (0.6999 -> no candidate)", "Ccc far" not in conf_case(10, 10, conf=0.6999)["confusion_candidates"])
    check("confusion: reports at exactly 0.7 are counted (twin)", "Ccc far" in conf_case(10, 10, conf=0.7)["confusion_candidates"])
    check("confusion: partner 5.0 s after counts", cand(conf_case(10, 10, dt=5.0))["co_occurring"] == 10)
    check("confusion: partner 5.001 s after does not", cand(conf_case(10, 10, dt=5.001))["co_occurring"] == 0)
    check("confusion: partner 5.0 s BEFORE counts", cand(conf_case(10, 10, dt=-5.0))["co_occurring"] == 10)
    check("confusion: a partner of ANOTHER genus and group (insect) is not named", cand(conf_case(10, 10, partner_sci="Iii near"))["co_occurring"] == 0)
    check("confusion: a partner of the same genus but a row-level group 'insect' is not named (row group beats the taxonomy)",
          cand(conf_case(10, 10, partner_kw={"group": "insect"}))["co_occurring"] == 0)
    check("confusion: twin, the same row with group 'bird' is named", cand(conf_case(10, 10, partner_kw={"group": "bird"}))["co_occurring"] == 10)
    check("confusion: a partner of the same GROUP (bird) but another genus is not named (Great Spotted Woodpecker / Trumpeter Swan case)",
          cand(conf_case(10, 10, partner_sci="Bbb near"))["co_occurring"] == 0)
    check("confusion: twin, the same genus is named", cand(conf_case(10, 10, partner_sci="Ccc near"))["partner"] == "Ccc near")
    check("confusion: a partner row flagged non_species at row level is not named", cand(conf_case(10, 10, partner_kw={"non_species": True}))["co_occurring"] == 0)
    check("confusion: twin, the same partner without the flag is named", cand(conf_case(10, 10))["co_occurring"] == 10)
    check("confusion: with NO taxonomy (group None on both sides) nothing is paired, although the genus is the same",
          cand(conf_case(10, 10, taxa_ok=False))["co_occurring"] == 0 and cand(conf_case(10, 10, taxa_ok=False))["partner"] is None)
    check("confusion: a filtered row of k=bird_filtered WITHOUT an in_region key is out-of-region",
          cand(conf_case(10, 10, far_in_region=None))["co_occurring"] == 10)
    check("confusion: twin, the same rows as k=bird without in_region are in-region (no candidate)", "Ccc far" not in conf_case(10, 10, far_in_region=None, far_k="bird")["confusion_candidates"])
    check("confusion: k=bird with in_region false is out-of-region (candidate)", cand(conf_case(10, 10, far_k="bird"))["reports"] == 10)

    def tie_case(zulu_all):
        """10 reports; 'Ccc zulu' is seen first (report 0 has only it), then 'Ccc alpha' joins in report 1; both are in every later report."""
        rows = []
        for i in range(10):
            t = T0 + i * 1000
            rows.append(row(t, 1, 0.9, "Ccc far", "Far Bird", k="bird_filtered", in_region=False))
            if i != 1 or zulu_all:
                rows.append(row(t + 1, 2, 0.4, "Ccc zulu", "Zulu Bird"))
            if i != 0:
                rows.append(row(t + 2, 3, 0.4, "Ccc alpha", "Alpha Bird"))
        d = tree(rows)
        try:
            return run(d)["confusion_candidates"]["Ccc far"]
        finally:
            shutil.rmtree(d)

    tc = tie_case(zulu_all=False)
    check("confusion: two partners with 9 of 10 each -> the alphabetically first wins, whatever the order of appearance",
          (tc["partner"], tc["co_occurring"], tc["share"]) == ("Ccc alpha", 9, 0.9), str(tc))
    tc = tie_case(zulu_all=True)
    check("confusion: twin, the partner with more reports (10 of 10) wins over the alphabetically first one",
          (tc["partner"], tc["co_occurring"], tc["share"]) == ("Ccc zulu", 10, 1.0), str(tc))
    # base-rate control: the partner calls all the time (every 4 s over the whole span), so it is near any time
    always = [row(T0 - 3000 + 4 * i, 2, 0.4, "Ccc near", "Near Bird") for i in range(0, 4000)]
    c = cand(conf_case(10, 0, partner_extra=always))
    check("confusion: a partner heard all the time (null share 1.0 vs real 1.0) does NOT qualify, exact reason",
          not c["qualifies"] and c["null_share"] == 1.0 and c["reason"] == "partner also near 100% of the reports shifted by +-600 s (base rate), real 100%", str(c))
    c = cand(conf_case(10, 10))
    check("confusion: twin, the same partner heard only with the reports (null share 0.0) qualifies", c["qualifies"] and c["null_share"] == 0.0, str(c))
    shifted = [row(T0 + i * 1000 + 600, 2, 0.4, "Ccc near", "Near Bird") for i in range(10)]
    c = cand(conf_case(10, 10, partner_extra=shifted))
    check("confusion: null share 0.5 is exactly half of the real share 1.0 -> qualifies (boundary)", c["qualifies"] and c["null_share"] == 0.5 and c["share"] == 1.0, str(c))
    c = cand(conf_case(10, 10, partner_extra=shifted + [row(T0 - 600, 2, 0.4, "Ccc near", "Near Bird")]))
    check("confusion: one more shifted hit (null 0.55 > 0.5) -> does not qualify (twin of the boundary)", not c["qualifies"] and c["null_share"] == 0.55, str(c))
    engines = [row(T0 + i * 1000 + 1, 2, 0.9, "Engine", "Engine", in_region=True) for i in range(10)]
    check("confusion: a non-species partner (engine) is not named", cand(conf_case(10, 0, partner_extra=engines))["co_occurring"] == 0)
    ghosts = [row(T0 + i * 1000 + 1, 2, 0.9, "Ghost bird", "Ghost bird", in_region=True) for i in range(10)]
    check("confusion: a non-species partner of the SAME group (ghost bird) is not named", cand(conf_case(10, 0, partner_extra=ghosts))["co_occurring"] == 0)
    check("confusion: a real species partner of that group is named (twin)", cand(conf_case(10, 10, partner_extra=ghosts))["partner_label"] == "Near Bird")
    check("confusion: an out-of-region partner does not count (not in-region evidence)", cand(conf_case(10, 10, partner_in_region=False))["co_occurring"] == 0)
    rep = conf_case(10, 10)
    check("confusion: the verdict of the out-of-region species is 'confusable with Near Bird'", rep["species"]["Ccc far"]["verdict"] == "confusable with Near Bird", rep["species"]["Ccc far"]["verdict"])
    check("confusion: the unqualified twin has no 'confusable' verdict (all 10 events geofilter-rejected -> out of region)", conf_case(10, 5)["species"]["Ccc far"]["verdict"] == R.V_OOR)
    want = {"Far Bird": {"likely_as": "Near Bird", "scientific": "Ccc far", "likely_as_scientific": "Ccc near",
                         "evidence": {"reports": 10, "co_occurring": 10, "share": 1.0, "min_confidence": 0.7, "window_s": 5.0},
                         "source": "co-occurrence within 5 s"}}
    doc = R.confusions_document(rep["confusion_candidates"])
    check("confusions doc: exact content", doc == want, str(doc))
    check("confusions doc: unqualified entries are absent", R.confusions_document(conf_case(10, 5)["confusion_candidates"]) == {})

    # ---- exclusions and robustness
    d = tree(events_rows("Engine", [(0.9, [1, 2])] * 12) + events_rows("Human vocal", [(0.9, [1])] * 12, common="Human vocal") + events_rows("Aaa real", [(0.9, [1, 2])] * 10))
    rep = run(d)
    check("exclusion: engine and human labels are not in the species table; the real species is", sorted(rep["species"]) == ["Aaa real"] and rep["skipped_non_species_rows"] == 12 * 2 + 12, str((sorted(rep["species"]), rep["skipped_non_species_rows"])))
    shutil.rmtree(d)
    d = tree(None)
    rep = run(d)
    check("missing birds.jsonl: empty report, no exception", rep["species"] == {} and rep["rows"] == 0)
    shutil.rmtree(d)
    d = tree([], raw_birds=b"")
    check("empty birds.jsonl: empty report", run(d)["species"] == {})
    shutil.rmtree(d)
    junk = b'garbage\n{"k":"bird","t":"x","beam":1,"confidence":0.9,"scientific":"Aaa j"}\n[1]\n' + json.dumps(row(T0, 1, 0.9, "Aaa ok")).encode() + b"\n{torn"
    d = tree([], raw_birds=junk)
    rep = run(d)
    check("garbage, wrong types, torn last line are skipped; the good row counts", sorted(rep["species"]) == ["Aaa ok"] and rep["rows"] == 1, str(list(rep["species"])))
    shutil.rmtree(d)
    d = tree(events_rows("Aaa old", [(0.9, [1])], t0=T0) + events_rows("Aaa new", [(0.9, [1])], t0=T0 + 1000))
    cut = R.parse_since("2027-01-15T08:00:00+00:00")   # T0 is 2027-01-15T08:00:00Z
    check("since: rows before the cut are ignored", sorted(run(d, since=cut + 500)["species"]) == ["Aaa new"])
    check("since: a row exactly at the cut is kept (twin)", sorted(run(d, since=cut)["species"]) == ["Aaa new", "Aaa old"])
    check("since: naive ISO is UTC and equals the explicit +00:00 form", R.parse_since("2027-01-15T08:00:00") == cut == T0, str((cut, T0)))
    shutil.rmtree(d)

    # ---- out of region: reported, and never 'trustworthy'
    mixed = [row(T0 + i * 100, b, 0.9, "Ddd mix", k=("bird_filtered" if i < 5 else "bird"), in_region=(False if i < 5 else None)) for i in range(10) for b in (1, 2)]
    pure = [row(T0 + i * 100, b, 0.9, "Bbb pure", k="bird_filtered", in_region=False) for i in range(10) for b in (1, 2)]
    home = [row(T0 + i * 100, b, 0.9, "Aaa home") for i in range(10) for b in (1, 2)]
    d = tree(mixed + pure + home)
    oo = run(d)["species"]
    shutil.rmtree(d)
    check("out of region: some rows filtered (5 of 10) -> flag true, all-flag false, and a normal 'trustworthy' (twin of the all-filtered case)",
          (oo["Ddd mix"]["out_of_region"], oo["Ddd mix"]["all_out_of_region"], oo["Ddd mix"]["verdict"]) == (True, False, R.V_TRUST), str(oo["Ddd mix"]))
    check("out of region: every row filtered -> both flags true and verdict 'out of region' (never trustworthy), exact detail",
          (oo["Bbb pure"]["out_of_region"], oo["Bbb pure"]["all_out_of_region"], oo["Bbb pure"]["verdict"], oo["Bbb pure"]["detail"]) ==
          (True, True, R.V_OOR, "all 10 events are geofilter-rejected reports"), str(oo["Bbb pure"]))
    check("out of region: an in-region species has both flags false", (oo["Aaa home"]["out_of_region"], oo["Aaa home"]["all_out_of_region"]) == (False, False), str(oo["Aaa home"]))
    check("out of region: suggested rule is (0.95, 2) like the other doubtful verdicts", (oo["Bbb pure"]["min_confidence"], oo["Bbb pure"]["min_beams"]) == (0.95, 2))
    d = tree(events_rows("Aaa few", [(0.9, [1, 2])] * 9, k="bird_filtered", in_region=False))
    check("out of region: fewer than 10 events keeps 'too few events' (the earlier rule wins)", run(d)["species"]["Aaa few"]["verdict"] == R.V_FEW)
    shutil.rmtree(d)

    # ---- the taxonomy table: default fallback inside analyse(); missing table
    d = tree(events_rows("Melanerpes aurifrons", [(0.9, [1, 2])] * 10, k="bird_filtered", in_region=False))
    rep0 = R.analyse(d)
    check("analyse(): with no taxa path it falls back to the repository table (ui/taxa.json): taxa ok, Melanerpes is a bird",
          rep0["taxa_ok"] is True and rep0["species"]["Melanerpes aurifrons"]["group"] == "bird", str((rep0["taxa_ok"], rep0["species"])))
    rep1 = R.analyse(d, None, os.path.join(d, "no_such_taxa.json"))
    check("analyse(): an explicit missing table is reported as taxa_ok False and group None (no silent bird)",
          rep1["taxa_ok"] is False and rep1["species"]["Melanerpes aurifrons"]["group"] is None, str((rep1["taxa_ok"], rep1["species"])))
    shutil.rmtree(d)

    # ---- suggested rules
    check("suggest: trustworthy -> (0.85, 1)", R.suggest(R.V_TRUST) == (0.85, 1))
    check("suggest: needs second beam / too few -> (0.85, 2)", R.suggest(R.V_BEAM) == (0.85, 2) and R.suggest(R.V_FEW) == (0.85, 2))
    check("suggest: temperature / confusable -> (0.95, 2)", R.suggest(R.V_TEMP) == (0.95, 2) and R.suggest("confusable with X") == (0.95, 2))
    saved = R.HIGH_CONF
    R.HIGH_CONF = 0.2
    try:
        check("suggest: never below the 0.5 floor (HIGH_CONF forced to 0.2 still gives 0.5)", R.suggest(R.V_TRUST) == (0.5, 1))
    finally:
        R.HIGH_CONF = saved

    d = tree(events_rows("Aaa js", [(0.9, [1, 2])] * 10))
    out = cli(d, "--json")
    js = json.loads(out.stdout)
    check("cli --json: exit 0, keys version/advice_only/species", out.returncode == 0 and sorted(js) == ["advice_only", "species", "version"] and js["advice_only"] is True and js["version"] == 1, str(out))
    sp = js["species"]["Aaa js"]
    check("cli --json: per-species keys are exactly all_out_of_region, common, detail, group, min_beams, min_confidence, out_of_region, stats, verdict",
          sorted(sp) == ["all_out_of_region", "common", "detail", "group", "min_beams", "min_confidence", "out_of_region", "stats", "verdict"], str(sorted(sp)))
    check("cli --json: values (0.85 minimum, 1 beam, trustworthy)", (sp["min_confidence"], sp["min_beams"], sp["verdict"]) == (0.85, 1, R.V_TRUST))
    out2 = cli(d, "--json")
    check("cli --json: byte-identical on a second run, LF only, trailing newline", out.stdout == out2.stdout and b"\r" not in out.stdout and out.stdout.endswith(b"}\n"))
    txt = cli(d)
    check("cli text: exit 0 and the species line carries the verdict", txt.returncode == 0 and b"Aaa js" in txt.stdout and b"trustworthy at 0.85" in txt.stdout, txt.stdout[:300])
    bad = cli(d, "--since", "notatime")
    check("cli: a bad --since is a usage error (exit 2)", bad.returncode == 2 and b"--since" in bad.stderr, str(bad))
    emp = tempfile.mkdtemp(prefix="relrep_", dir=TMP)
    e = subprocess.run([sys.executable, "-I", SCRIPT, "--data-dir", emp, "--json", "--taxa", os.path.join(d, "taxa.json")], capture_output=True)
    check("cli: a data dir with nothing in it -> exit 0, empty species", e.returncode == 0 and json.loads(e.stdout)["species"] == {}, str(e))

    keep = os.path.join(d, "keep.json")
    with open(keep, "wb") as f:
        f.write(b'{"Golden-fronted Woodpecker": {"likely_as": "x"}}\n')
    kept = open(keep, "rb").read()
    nt = subprocess.run([sys.executable, "-I", SCRIPT, "--data-dir", d, "--taxa", os.path.join(d, "no_such_taxa.json"), "--write-confusions", keep], capture_output=True)
    check("cli: a missing taxonomy table -> exit 1, nothing on stdout, the reason on stderr", nt.returncode == 1 and nt.stdout == b"" and b"taxonomy table" in nt.stderr and b"missing or unreadable" in nt.stderr, str(nt))
    check("cli: a missing taxonomy table leaves an existing confusions file byte-identical (never overwritten with {})", open(keep, "rb").read() == kept)
    nt2 = cli(d, "--write-confusions", keep)
    check("cli: twin, the same call with a readable table exits 0 and writes the (empty) document", nt2.returncode == 0 and open(keep, "rb").read() == b"{}\n", str(nt2))
    nd = cli(d, "--write-confusions", os.path.join(d, "no_such_dir", "c.json"))
    check("cli: --write-confusions into a missing directory -> exit 2, a clean message, no traceback",
          nd.returncode == 2 and b"directory" in nd.stderr and b"does not exist" in nd.stderr and b"Traceback" not in nd.stderr and nd.stdout == b"", str(nd))
    check("cli: that refusal created neither the directory nor the file", not os.path.exists(os.path.join(d, "no_such_dir")))
    nx = cli(d, "--write-confusions", d)
    check("cli: --write-confusions naming a directory -> exit 2 and says so", nx.returncode == 2 and b"is a directory" in nx.stderr and b"Traceback" not in nx.stderr, str(nx))
    birds_p = os.path.join(d, "analysis", "birds.jsonl")
    birds_before = open(birds_p, "rb").read()
    ni = cli(d, "--write-confusions", birds_p)
    check("cli: --write-confusions naming birds.jsonl -> exit 2, refused, the file untouched",
          ni.returncode == 2 and b"input file" in ni.stderr and open(birds_p, "rb").read() == birds_before and len(birds_before) > 0, str(ni))
    ni2 = cli(d, "--write-confusions", os.path.join(d, "analysis", "..", "analysis", "birds.jsonl"))
    check("cli: the same file reached through a '..' path is refused too", ni2.returncode == 2 and b"input file" in ni2.stderr and open(birds_p, "rb").read() == birds_before, str(ni2))
    taxa_p = os.path.join(d, "taxa.json")
    taxa_before = open(taxa_p, "rb").read()
    ni3 = cli(d, "--write-confusions", taxa_p)
    check("cli: --write-confusions naming the taxonomy table -> exit 2, refused", ni3.returncode == 2 and open(taxa_p, "rb").read() == taxa_before, str(ni3))
    shutil.rmtree(d)

    # ---- write-confusions: deterministic file, nothing written without the flag
    rows = []
    for i in range(10):
        rows += [row(T0 + i * 1000, 1, 0.9, "Ccc far", "Far Bird", k="bird_filtered", in_region=False), row(T0 + i * 1000 + 1, 2, 0.5, "Ccc near", "Near Bird")]
    d = tree(rows)
    p1, p2 = os.path.join(d, "c1.json"), os.path.join(d, "c2.json")
    before = sorted(os.listdir(d))
    cli(d)
    check("cli: without --write-confusions nothing is written", sorted(os.listdir(d)) == before)
    cli(d, "--write-confusions", p1)
    cli(d, "--write-confusions", p2)
    b1, b2 = open(p1, "rb").read(), open(p2, "rb").read()
    check("write-confusions: two runs are byte-identical, LF, trailing newline", b1 == b2 and b"\r" not in b1 and b1.endswith(b"}\n"))
    check("write-confusions: parses to the exact document", json.loads(b1) == want)
    check("write-confusions: keys are sorted in the file text", b1.index(b'"evidence"') < b1.index(b'"likely_as"') < b1.index(b'"source"'))
    txt = cli(d).stdout.decode("utf-8").splitlines()
    counts = next((ln for ln in txt if ln.startswith("verdict counts:")), "")
    check("cli text: the counts line names the verdict as 'confusable with X' (partner stripped, 'with' kept) and the other verdict, exact",
          counts == "verdict counts: confusable with X 1, no high-confidence events 1", counts)
    check("cli text: the species row of the filtered species carries the full verdict with the partner and 'all' in the oor column",
          any(ln.startswith("Far Bird") and " all " in ln and "confusable with Near Bird (partner in 10 of 10 reports)" in ln for ln in txt), "\n".join(txt[:6]))
    check("cli text: the in-region species has '-' in the oor column", any(ln.startswith("Near Bird") and ln.split()[3] == "-" for ln in txt), "\n".join(txt[:6]))
    jv = json.loads(cli(d, "--json").stdout)["species"]
    check("cli --json: out_of_region / all_out_of_region are output (true,true for the filtered species, false,false for the in-region one)",
          (jv["Ccc far"]["out_of_region"], jv["Ccc far"]["all_out_of_region"], jv["Ccc near"]["out_of_region"], jv["Ccc near"]["all_out_of_region"]) == (True, True, False, False), str(jv))
    shutil.rmtree(d)

    # ---- the shipped confusions.json: pinned exactly (a self-consistent but different entry must fail), plus the documented shape
    shipped = json.load(open(os.path.join(HERE, "confusions.json"), encoding="utf-8"))
    shipped_want = {"Golden-fronted Woodpecker": {
        "evidence": {"co_occurring": 18, "min_confidence": 0.7, "reports": 21, "share": 0.8571, "window_s": 5.0},
        "likely_as": "Red-bellied Woodpecker", "likely_as_scientific": "Melanerpes carolinus", "scientific": "Melanerpes aurifrons",
        "source": "co-occurrence within 5 s"}}
    check("shipped confusions.json: exactly the pinned Golden-fronted Woodpecker entry (18 of 21, 0.8571)", shipped == shipped_want, str(shipped))
    for lab, ent in shipped.items():
        ev = ent["evidence"]
        check("shipped confusions.json: %s has the documented keys and passes the thresholds" % lab,
              sorted(ent) == ["evidence", "likely_as", "likely_as_scientific", "scientific", "source"] and ent["source"] == R.CONFUSION_SOURCE
              and sorted(ev) == ["co_occurring", "min_confidence", "reports", "share", "window_s"] and ev["reports"] >= R.CONFUSION_MIN_REPORTS
              and ev["share"] >= R.CONFUSION_MIN_SHARE and ev["share"] == round(ev["co_occurring"] / ev["reports"], 4)
              and ent["scientific"].split()[0] == ent["likely_as_scientific"].split()[0], str(ent))

    # ---- docs: README documents every constant with its value, the verdicts and the JSON keys
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    sec = readme[readme.index("## Reliability report"):] if "## Reliability report" in readme else ""
    check("README: has the '## Reliability report' section", bool(sec))
    for name in ("EVENT_GAP_S", "HIGH_CONF", "MIN_EVENTS", "MIN_HIGH_EVENTS", "TRUSTED_MULTIBEAM_SHARE", "TEMP_MIN_KNOWN", "INSECT_COLD_C",
                 "INSECT_SUSPECT_SHARE", "AMPHIBIAN_COLD_C", "AMPHIBIAN_SUSPECT_SHARE", "CONFUSION_MIN_CONF", "CONFUSION_WINDOW_S",
                 "CONFUSION_MIN_REPORTS", "CONFUSION_MIN_SHARE", "MIN_CONF_FLOOR", "TRUSTED_MULTIBEAM_LOWER", "WILSON_Z",
                 "MIN_SPECIES_MULTIBEAM_SHARE", "CONFUSION_NULL_SHIFT_S", "CONFUSION_MAX_NULL_RATIO"):
        val = getattr(R, name)
        line = next((ln for ln in sec.splitlines() if ln.startswith("| `%s`" % name)), "")
        check("README: constant %s is documented with its value %g" % (name, val), ("| %g |" % val) in line, line)
    for v in (R.V_TRUST, R.V_BEAM, R.V_TEMP, R.V_FEW, R.V_CONF.strip(), R.V_OOR, R.V_NOHIGH):
        check("README: verdict %r is documented" % v, v in sec)

    def pc(x):
        return "%g" % round(100 * x, 6)

    rule = {v: next((ln for ln in sec.splitlines() if ln.startswith("| ") and "| `%s`" % v in ln), "") for v in
            (R.V_CONF.strip() + " X", R.V_FEW, R.V_OOR, R.V_TEMP, R.V_NOHIGH, R.V_BEAM, R.V_TRUST)}
    need = {
        R.V_CONF.strip() + " X": ["reports >= %d" % R.CONFUSION_MIN_REPORTS, ">= %s %%" % pc(R.CONFUSION_MIN_SHARE), "+-%g s" % R.CONFUSION_NULL_SHIFT_S,
                                  "%s %% of the real share" % pc(R.CONFUSION_MAX_NULL_RATIO)],
        R.V_FEW: ["events < %d" % R.MIN_EVENTS],
        R.V_OOR: ["every report"],
        R.V_TEMP: ["insect: >= %s %% of events below %g C" % (pc(R.INSECT_SUSPECT_SHARE), R.INSECT_COLD_C),
                   "amphibian: >= %s %% below %g C" % (pc(R.AMPHIBIAN_SUSPECT_SHARE), R.AMPHIBIAN_COLD_C), ">= %d known" % R.TEMP_MIN_KNOWN],
        R.V_NOHIGH: ["no event at >= %g" % R.HIGH_CONF],
        R.V_BEAM: ["fewer than %d events at >= %g" % (R.MIN_HIGH_EVENTS, R.HIGH_CONF), "fewer than %s %% of them on >= 2 beams" % pc(R.TRUSTED_MULTIBEAM_SHARE),
                   "lower bound below %s %%" % pc(R.TRUSTED_MULTIBEAM_LOWER), "fewer than %s %% of all" % pc(R.MIN_SPECIES_MULTIBEAM_SHARE)],
        R.V_TRUST: ["otherwise"],
    }
    for v, parts in need.items():
        for part in parts:
            check("README: verdict table row %r states %r (numbers taken from the constants)" % (v, part), part in rule[v], rule[v])
    for key in ("likely_as", "likely_as_scientific", "evidence", "co_occurring", "min_confidence", "window_s", "source", "advice_only", "min_beams", "scientific", "reports"):
        check("README: key `%s` is documented" % key, "`%s`" % key in sec)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
