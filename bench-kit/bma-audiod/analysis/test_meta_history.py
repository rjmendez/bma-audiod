#!/usr/bin/env python3
"""Tests for the analyzer's metadata history: hist (safe rotation, atomic JSON), taxa stamping, per-beam tonal
attribution, ring decorrelation, run markers on novelty rows, config snapshots, daily summary, hourly rollup,
and that the existing readers ignore the new fields.  pytest-compatible; also runs without pytest:
    python -I test_meta_history.py        (exit 0 = all pass)
Everything uses temp dirs; no live data."""

import json, os, shutil, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CLASSIFY = os.path.abspath(os.path.join(HERE, "..", "classify"))
import hist, taxa as T, runmeta, rollup, daily  # noqa: E402
import plugins as P  # noqa: E402

FS = 48000


def tmp():
    return tempfile.mkdtemp(prefix="metahist_")


def lines(path):
    with open(path, "rb") as f:
        return [json.loads(l) for l in f.read().decode().splitlines()]


class RecReplace:
    """Recording stand-in for os.replace at the unit's boundary; fails the first `fail` calls like a locked file."""

    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, a, b):
        self.calls.append((a, b))
        if len(self.calls) <= self.fail:
            raise PermissionError(13, "file is held by a reader")
        os.replace(a, b)


# ------------------------------------------------------------------ hist
def test_rotate_with_working_replace():
    d = tmp()
    r = RecReplace()
    w = hist.RotatingJsonl(os.path.join(d, "a.jsonl"), 40, replace=r)
    for i in range(4):
        assert w.write({"i": i}) is True  # '{"i":0}\n' = 8 bytes; 5 fit in 40
    assert lines(w.path) == [{"i": i} for i in range(4)] and not os.path.exists(w.path + ".1")
    for i in range(4, 7):
        w.write({"i": i})
    # 5 lines fill 40 bytes, the 6th rotates
    assert [c[0] for c in r.calls] == [w.path] and r.calls[0][1] == w.path + ".1"
    assert lines(w.path + ".1") == [{"i": i} for i in range(5)]
    assert lines(w.path) == [{"i": 5}, {"i": 6}]
    assert (w.write_errors, w.rotate_fallbacks) == (0, 0)
    w.close()


def test_torn_tail_is_isolated_not_glued_to_the_next_record():
    d = tmp()
    p = os.path.join(d, "a.jsonl")
    with open(p, "wb") as f:
        f.write(b'{"k":"a","n":1}\n{"k":"a","n":2,"par')
    w = hist.RotatingJsonl(p, 10**6)
    assert w.write({"k": "board_boot", "n": 3}) is True
    assert w.write({"k": "a", "n": 4}) is True
    w.close()
    parsed = []
    for l in [x for x in open(p, "rb").read().split(b"\n") if x]:
        try:
            parsed.append(json.loads(l)["n"])
        except ValueError:
            parsed.append("TORN")
    assert parsed == [1, "TORN", 3, 4] and w.torn_repaired == 1 and w.write_errors == 0


def test_newline_terminated_file_gets_no_blank_line():
    d = tmp()
    p = os.path.join(d, "a.jsonl")
    with open(p, "wb") as f:
        f.write(b'{"k":"a","n":1}\n')
    w = hist.RotatingJsonl(p, 10**6)
    w.write({"k": "a", "n": 2})
    w.close()
    assert open(p, "rb").read() == b'{"k":"a","n":1}\n{"k":"a","n":2}\n' and w.torn_repaired == 0


def test_rotate_falls_back_to_copy_truncate_when_replace_fails():
    d = tmp()
    r = RecReplace(fail=1)
    w = hist.RotatingJsonl(os.path.join(d, "a.jsonl"), 40, replace=r)
    for i in range(7):
        assert w.write({"i": i}) is True  # no write is lost while the rotate is refused
    assert w.rotate_fallbacks == 1 and w.write_errors == 0
    assert lines(w.path + ".1") == [{"i": i} for i in range(5)]
    assert lines(w.path) == [{"i": 5}, {"i": 6}]
    assert w.f is not None and not w.f.closed  # still writable afterwards
    w.close()


def test_rotate_total_failure_keeps_appending_and_counts():
    d = tmp()
    w = hist.RotatingJsonl(os.path.join(d, "a.jsonl"), 40, replace=RecReplace(fail=99))
    real_copy = shutil.copyfile
    shutil.copyfile = lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied"))
    try:
        for i in range(7):
            assert w.write({"i": i}) is True
    finally:
        shutil.copyfile = real_copy
    assert w.rotate_fallbacks == 1 and w.write_errors == 1
    assert lines(w.path) == [{"i": i} for i in range(7)] and not os.path.exists(w.path + ".1")
    w.close()


def test_atomic_json_keeps_previous_file_when_replace_fails():
    d = tmp()
    p = os.path.join(d, "s.json")
    a = hist.AtomicJson(p)
    assert a.write({"n": 1}) is True and a.read() == {"n": 1}
    b = hist.AtomicJson(p, replace=RecReplace(fail=1))
    assert b.write({"n": 2}) is False and b.errors == 1
    assert a.read() == {"n": 1} and os.path.exists(p + ".tmp")  # previous file intact, tmp left behind
    assert b.write({"n": 3}) is True and b.errors == 1 and a.read() == {"n": 3}


# ------------------------------------------------------------------ taxa
def write_taxa(d, name="taxa.json"):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        json.dump(
            {
                "version": 1,
                "explicit_labels": {"dog": {"group": "mammal", "non_species": True}, "engine": {"group": "mechanical", "non_species": True}},
                "genera": {"bird": ["turdus"], "insect": ["gryllus"], "amphibian": ["pseudacris"], "mammal": ["canis"]},
            },
            f,
        )
    return p


def test_taxa_lookup_exact():
    t = T.Taxa(write_taxa(tmp()))
    assert t.lookup("Turdus migratorius", "American Robin") == ("bird", False)
    assert t.lookup("Gryllus pennsylvanicus", "Fall Field Cricket") == ("insect", False)
    assert t.lookup("Dog", "Dog") == ("mammal", True)
    assert t.lookup("Engine", "Engine") == ("mechanical", True)
    assert (t.missing, t.unmapped) == (0, 0)
    assert t.lookup("Zzyzx unknownus", "Mystery") == ("other-bio", False)  # unknown genus, never guessed as bird
    assert (t.missing, t.unmapped) == (0, 1)


def test_taxa_missing_or_corrupt_gives_none_and_counts():
    d = tmp()
    t = T.Taxa(os.path.join(d, "nope.json"))
    assert t.lookup("Turdus migratorius", "American Robin") == (None, None) and t.missing == 1
    bad = os.path.join(d, "bad.json")
    with open(bad, "w") as f:
        f.write("{not json")
    t2 = T.Taxa(bad)
    assert t2.lookup("Dog", "Dog") == (None, None) and t2.missing == 1 and t2.ok is False


class FakeBN:
    """The BirdNET boundary: returns canned detections; the unit under test is MultiBeamBirds.process."""

    def __init__(self, dets, filtered, geo):
        self.dets, self.filtered_list, self.geo = dets, filtered, geo
        self.filtered = []

    def classify(self, audio, when=None):
        self.filtered = list(self.filtered_list)
        return list(self.dets)


def loud(db_=-30):
    return (np.random.default_rng(3).standard_normal((3 * FS, 8)) * 10 ** (db_ / 20)).astype(np.float32)


def birds_plugin(dets, filtered, taxa_obj, geo=False):
    p = P.MultiBeamBirds(CLASSIFY, taxa=taxa_obj)
    p.bn = FakeBN(dets, filtered, geo)
    return p


def test_birds_rows_are_stamped_geo_off():
    p = birds_plugin(
        [{"common": "American Robin", "scientific": "Turdus migratorius", "confidence": 0.9}, {"common": "Dog", "scientific": "Dog", "confidence": 0.5}],
        [],
        T.Taxa(write_taxa(tmp())),
    )
    recs, _ = p.process(loud(), 100.0)
    b = [r for r in recs if r["k"] == "bird"]
    assert len(b) == 4  # 2 detections x 2 loudest beams
    assert [(r["scientific"], r["in_region"], r["non_species"], r["group"]) for r in b[:2]] == [
        ("Turdus migratorius", None, False, "bird"),
        ("Dog", None, True, "mammal"),
    ]
    assert not [r for r in recs if r["k"] == "bird_filtered"]
    assert p.window_info == {"beams_classified": 2}


def test_filtered_row_has_reason_beam_group_and_kept_row_has_no_reason():
    kept = {"common": "American Robin", "scientific": "Turdus migratorius", "confidence": 0.9, "in_region": True, "non_species": False}
    gone = {"common": "Fall Field Cricket", "scientific": "Gryllus pennsylvanicus", "confidence": 0.4, "in_region": False, "non_species": False}
    p = birds_plugin([kept], [gone], T.Taxa(write_taxa(tmp())), geo=True)
    recs, _ = p.process(loud(), 100.0)
    f = [r for r in recs if r["k"] == "bird_filtered"]
    k = [r for r in recs if r["k"] == "bird"]
    assert len(f) == 2 and len(k) == 2
    assert {r["beam"] for r in f} == {r["beam"] for r in k} and len({r["beam"] for r in f}) == 2
    for r in f:
        assert (r["reject_reason"], r["in_region"], r["non_species"], r["group"]) == ("geo_not_predicted", False, False, "insect")
    for r in k:
        assert "reject_reason" not in r and (r["in_region"], r["group"]) == (True, "bird")


def test_missing_taxa_still_emits_every_record_with_group_none():
    t = T.Taxa(os.path.join(tmp(), "absent.json"))
    p = birds_plugin([{"common": "American Robin", "scientific": "Turdus migratorius", "confidence": 0.9}], [], t)
    recs, _ = p.process(loud(), 100.0)
    assert len(recs) == 2 and all(r["group"] is None and r["non_species"] is False for r in recs)
    assert t.missing == 2


def test_gated_window_reports_zero_beams_classified():
    p = birds_plugin([{"common": "x", "scientific": "Turdus x", "confidence": 0.9}], [], T.Taxa(write_taxa(tmp())))
    recs, _ = p.process(loud(-90), 100.0)
    assert recs == [] and p.window_info == {"beams_classified": 0}


# ------------------------------------------------------------------ dsp per-beam tonal attribution
def tone_buf(beams, amp_db=-45.0, hz=978.0, seconds=10):
    rng = np.random.default_rng(5)
    n = seconds * FS
    y = (rng.standard_normal((n, 8)) * 10 ** (-60 / 20)).astype(np.float32)
    s = (np.sin(2 * np.pi * hz * np.arange(n) / FS) * 10 ** (amp_db / 20)).astype(np.float32)
    for b in beams:
        y[:, b] += s
    return y


def test_beam_of_finds_the_tone_beam():
    d = P.DSPMonitor()
    beam, margin = d.beam_of(tone_buf([3]), 978.0)
    assert beam == 4 and margin > 10.0


def test_beam_of_is_null_when_the_tone_is_not_in_the_segment():
    d = P.DSPMonitor()
    beam, margin = d.beam_of(tone_buf([]), 978.0)
    assert beam is None
    beam2, _ = d.beam_of(tone_buf([3]), 978.0)  # positive twin: same call with the tone present
    assert beam2 == 4


def test_beam_of_is_null_when_all_beams_hear_it_equally():
    d = P.DSPMonitor()
    beam, margin = d.beam_of(tone_buf(range(8)), 978.0)
    assert beam is None and 0.0 <= margin < 1.5


def test_tonal_events_carry_the_beam_through_the_tracker():
    d = P.DSPMonitor()
    recs = []
    for sec in range(80):
        w = tone_buf([3], seconds=1) if 5 <= sec < 45 else tone_buf([], seconds=1)
        recs += d.process(w, float(sec + 1))[0]
    app = [r for r in recs if r["k"] == "tonal_appeared"]
    assert len(app) == 1 and app[0]["beam"] == 4 and app[0]["beam_margin_db"] > 10.0
    gone = [r for r in recs if r["k"] == "tonal_gone"]
    assert len(gone) == 1 and gone[0]["beam"] == 4
    st = [ln for r in recs if r["k"] == "tonal_state" for ln in r["lines"]]
    assert st and all(ln["beam"] == 4 for ln in st)


# ------------------------------------------------------------------ decorrelation
def test_decorr_identical_beams_is_exactly_zero():
    s = np.random.default_rng(1).standard_normal(FS)
    assert P.ring_decorr(np.tile(s[:, None], (1, 8))) == 0.0


def test_decorr_independent_noise_is_near_one():
    v = P.ring_decorr(np.random.default_rng(2).standard_normal((FS, 8)))
    assert 0.98 <= v <= 1.0, v


def test_decorr_uses_ring_adjacent_pairs_only():
    s = np.sin(2 * np.pi * 440 * np.arange(FS) / FS)
    y = np.stack([s] * 4 + [-s] * 4, axis=1)  # ring pairs: 6 at +1, 2 at -1 -> mean 0.5 -> decorr 0.5 (all pairs would give 1.0)
    assert P.ring_decorr(y) == 0.5


def test_spatial_record_carries_decorr():
    s = np.random.default_rng(1).standard_normal(FS).astype(np.float32) * 0.01
    rec = P.Spatial().process(np.tile(s[:, None], (1, 8)), 1.0)[0][0]
    assert rec["k"] == "spatial" and rec["decorr"] == 0.0
    rec2 = P.Spatial().process((np.random.default_rng(2).standard_normal((FS, 8)) * 0.01).astype(np.float32), 1.0)[0][0]
    assert 0.9 <= rec2["decorr"] <= 1.0


# ------------------------------------------------------------------ novelty markers
def novelty_rows(**kw):
    n = P.Novelty(os.path.join(tmp(), "n.pkl"), k=2, warmup=3, **kw)
    rng = np.random.default_rng(0)
    rows = []
    for i in range(5):
        rows += n.add(rng.standard_normal(16).astype(np.float32), 10.0 + i)
    return rows


def test_novelty_rows_carry_run_and_mv_warmup_and_scored():
    rows = novelty_rows(run=1761500000, mv="km2-sk9.9")
    assert [r.get("state") for r in rows[:3]] == ["warming_up"] * 3 and len(rows) >= 5
    assert all(r["run"] == 1761500000 and r["mv"] == "km2-sk9.9" for r in rows)
    assert "scene" in rows[3]


def test_novelty_without_markers_is_unchanged():
    rows = novelty_rows()
    assert rows and all("run" not in r and "mv" not in r for r in rows)


def test_novelty_flags_a_state_from_another_sklearn():
    import pickle

    d = tmp()
    path = os.path.join(d, "n.pkl")
    n = P.Novelty(path, k=2, warmup=3, mv="km2-sk" + P.Novelty(os.path.join(d, "x.pkl")).sk)
    n.save()
    with open(path, "rb") as f:
        st = pickle.load(f)
    st["sk"] = "0.0.1"
    with open(path, "wb") as f:
        pickle.dump(st, f)
    assert P.Novelty(path, k=2, warmup=3, mv="m").mv == "m-from0.0.1"
    st["sk"] = P.Novelty(os.path.join(d, "x.pkl")).sk
    with open(path, "wb") as f:
        pickle.dump(st, f)
    assert P.Novelty(path, k=2, warmup=3, mv="m").mv == "m"


# ------------------------------------------------------------------ config snapshot
def make_build(d, mask=None, calib=None, geo=None):
    sys.path.insert(0, CLASSIFY)
    from bma_classify import load_geo_config

    return lambda: runmeta.build_config(d, mask, None, calib, geo, load_geo_config, d, "cpu", 12, "km12-skX")


def write_json(p, o):
    with open(p, "w") as f:
        json.dump(o, f)


def test_config_written_at_start_then_only_on_change():
    d = tmp()
    mask = os.path.join(d, "mask.json")
    write_json(mask, {"version": 1, "lines_hz": [12000.0, 11510.7], "tolerance_hz": 4.0})
    w = runmeta.ConfigWatcher(os.path.join(d, "h"), make_build(d, mask=mask), 1761500000, clock=lambda: 5.0)
    assert w.poll() is True and w.poll() is False  # unchanged: no second record
    write_json(mask, {"version": 1, "lines_hz": [12000.0, 11510.7, 15334.0], "tolerance_hz": 4.0})
    assert w.poll() is True
    recs = lines(os.path.join(d, "h", "config_history.jsonl"))
    assert len(recs) == 2 and [r["mask"]["n_lines"] for r in recs] == [2, 3]
    assert recs[0]["mask"]["sha12"] != recs[1]["mask"]["sha12"]
    assert (recs[0]["k"], recs[0]["v"], recs[0]["t"], recs[0]["run_id"]) == ("analysis_config", 1, 5.0, 1761500000)
    assert recs[0]["mask"] == {"path_set": True, "sha12": recs[0]["mask"]["sha12"], "n_lines": 2, "tol_hz": 4.0, "version": 1}


def test_mask_hash_is_over_content_not_path():
    d = tmp()
    a, b = os.path.join(d, "a.json"), os.path.join(d, "b.json")
    write_json(a, {"version": 1, "lines_hz": [1.0, 2.0], "tolerance_hz": 4.0, "note": "x"})
    write_json(b, {"version": 1, "lines_hz": [2.0, 1.0], "tolerance_hz": 4.0, "note": "y"})
    assert runmeta.mask_info(a)["sha12"] == runmeta.mask_info(b)["sha12"]
    write_json(b, {"version": 1, "lines_hz": [1.0, 2.5], "tolerance_hz": 4.0})
    assert runmeta.mask_info(a)["sha12"] != runmeta.mask_info(b)["sha12"]


def test_calibration_absent_and_present():
    d = tmp()
    assert runmeta.calibration_info(os.path.join(d, "calibration.json")) == {"source": "absent"}
    p = os.path.join(d, "calibration.json")
    write_json(
        p, {"profile": {"beam_azimuth_deg": [10, 55, 100, 145, 190, 235, 280, 325.123], "north_reference": "compass", "measured_at": "2026-10-26T12:00:00Z"}}
    )
    c = runmeta.calibration_info(p)
    assert c["azimuth_deg"] == [10.0, 55.0, 100.0, 145.0, 190.0, 235.0, 280.0, 325.12]
    assert (c["source"], c["north_reference"], c["measured_at"]) == ("file", "compass", "2026-10-26T12:00:00Z")
    assert len(c["sha12"]) == 12


def test_config_never_stores_the_location():
    d = tmp()
    g = os.path.join(d, "geo.json")
    write_json(g, {"lat": 12.3456, "lon": -65.4321, "week": 43, "threshold": 0.03})
    w = runmeta.ConfigWatcher(os.path.join(d, "h"), make_build(d, geo=g), 1)
    assert w.poll() is True
    txt = open(os.path.join(d, "h", "config_history.jsonl")).read()
    rec = json.loads(txt)
    assert rec["geo"] == {"on": True, "week": 43, "threshold": 0.03}
    assert "12.34" not in txt and "65.43" not in txt and "lat" not in txt
    write_json(g, {"lat": 13.05, "lon": -66.24, "week": 43, "threshold": 0.03})  # a moved cell is still detected
    assert w.poll() is True


# ------------------------------------------------------------------ daily summary
def test_daily_two_days_and_restart_does_not_double():
    d = tmp()
    p = os.path.join(d, "daily_summary.json")
    day1, day2 = 1761436800.0, 1761436800.0 + 86400  # 2025-10-26 00:00:00 UTC and the next day
    clock = [day1 + 10]
    s = daily.DailySummary(p, clock=lambda: clock[0])
    dsp_recs = [{"k": "dsp"}, {"k": "transient"}]
    for i in range(3):
        s.on_window("dsp", dsp_recs, day1 + 100 + i)
    s.on_window("spatial", [{"k": "spatial", "directional": True}, {"k": "source"}], day1 + 100)
    s.on_window("spatial", [{"k": "spatial", "directional": False}], day1 + 101)
    s.on_window("tags", [{"k": "tags", "speech_active": True}], day1 + 100)
    s.on_window("birds", [], day1 + 100, {"beams_classified": 0})
    s.on_window("birds", [{"k": "bird"}], day1 + 103, {"beams_classified": 2})
    s.on_window("dsp", dsp_recs, day2 + 5)
    s.on_error("clap", day1 + 50)
    s.on_dropped("tags", 4, day1 + 60)
    s.on_status(day1 + 200, {"datagrams": 1000, "bad": 1, "gaps": 8}, 0.0, 5.0)
    s.on_status(day1 + 205, {"datagrams": 1500, "bad": 1, "gaps": 8}, 12.0, 5.0)
    assert s.flush() is True
    d1 = lines_obj(p)["days"]["20251026"]
    assert d1["windows"] == {"dsp": 3, "spatial": 2, "tags": 1, "birds": 2}
    assert d1["records"]["dsp.dsp"] == 3 and d1["records"]["dsp.transient"] == 3 and d1["records"]["spatial.source"] == 1
    assert (d1["audio_s"], d1["spatial_directional"], d1["tags_speech_suppressed"]) == (3.0, 1, 1)
    assert (d1["birds_windows"], d1["birds_no_beam_over_gate"]) == (2, 1)
    assert (d1["errors"], d1["dropped"]) == ({"clap": 1}, {"tags": 4})
    assert (d1["rx_datagrams"], d1["rx_bad"], d1["rx_gaps"], d1["ring_reset_s"], d1["uptime_s"], d1["restarts"]) == (1500, 1, 8, 5.0, 10.0, 1)
    assert lines_obj(p)["days"]["20251027"]["windows"] == {"dsp": 1}
    clock[0] = day1 + 3600  # restart the same day
    s2 = daily.DailySummary(p, clock=lambda: clock[0])
    s2.on_window("dsp", dsp_recs, day1 + 3700)
    s2.flush()
    d1b = lines_obj(p)["days"]["20251026"]
    assert (d1b["restarts"], d1b["windows"]["dsp"], d1b["audio_s"]) == (2, 4, 4.0)  # added once, not doubled


def lines_obj(p):
    with open(p) as f:
        return json.load(f)


def test_daily_failed_flush_keeps_previous_file_and_counts_then_recovers():
    d = tmp()
    p = os.path.join(d, "daily_summary.json")
    s = daily.DailySummary(p, clock=lambda: 1761436800.0)
    assert s.flush() is True
    before = lines_obj(p)
    s2 = daily.DailySummary(p, clock=lambda: 1761436800.0, replace=RecReplace(fail=1))
    s2.on_window("dsp", [{"k": "dsp"}], 1761436900.0)
    assert s2.flush() is False and s2.errors == 1
    assert lines_obj(p) == before  # previous file intact
    assert s2.flush() is True and lines_obj(p)["days"]["20251026"]["windows"] == {"dsp": 1}


def test_daily_keeps_400_days():
    s = daily.DailySummary(os.path.join(tmp(), "d.json"), clock=lambda: 1761436800.0 + 450 * 86400)
    for i in range(450):
        s.day(1761436800.0 + i * 86400)
    s.flush()
    assert len(s.days) == 400 and "20251026" not in s.days


# ------------------------------------------------------------------ hourly rollup
H = 1761436800 + 7200  # an exact UTC hour boundary


def feed_hour(r, h=H):
    for i in range(2):
        r.feed(
            {
                "k": "dsp",
                "t": h + 10 + i,
                "rms_dbfs": [-60.0 + j for j in range(8)],
                "band_pct": [10.0, 20.0, 30.0, 25.0, 15.0],
                "centroid_hz": 1000.0 + 100 * i,
            }
        )
    r.feed({"k": "transient", "t": h + 20, "excess_db": 12.0, "loudest_beam": 3})
    r.feed({"k": "tonal_appeared", "t": h + 30, "hz": 978.1, "excess_db": 20.0, "beam": 4, "beam_margin_db": 9.0})
    r.feed({"k": "tonal_gone", "t": h + 90, "hz": 978.1, "lasted_s": 52.0, "beam": 4})
    r.feed(
        {
            "k": "tonal_state",
            "t": h + 40,
            "lines": [
                {"hz": 978.1, "excess_db": 20.0, "age_s": 10.0, "beam": 4},
                {"hz": 12000.0, "excess_db": 15.0, "age_s": 99.0, "electronic": True, "beam": None},
            ],
            "noise_floor_db": -70.0,
            "noise_floor_drift_db": 0.1,
        }
    )
    r.feed(
        {
            "k": "tonal_state",
            "t": h + 50,
            "lines": [{"hz": 978.1, "excess_db": 20.0, "age_s": 20.0, "beam": 4}],
            "noise_floor_db": -66.0,
            "noise_floor_drift_db": 0.1,
        }
    )
    for dom, dr, dec in ((2, True, 0.5), (2, False, 0.7)):
        r.feed(
            {"k": "spatial", "t": h + 15, "dominant_beam": dom, "directional": dr, "decorr": dec, "e_rel_db": [0.0 if i == dom - 1 else -3.0 for i in range(8)]}
        )


def test_rollup_hour_closes_after_grace_with_exact_values():
    d = tmp()
    r = rollup.HourlyRollup(d)
    feed_hour(r)
    r.tick(H + 3600 + 119)
    assert not os.path.exists(os.path.join(d, "hourly_rollup.jsonl"))  # grace not over: nothing written
    r.tick(H + 3600 + 120)
    rec = lines(os.path.join(d, "hourly_rollup.jsonl"))
    assert len(rec) == 1
    x = rec[0]
    assert (x["k"], x["v"], x["kind"], x["hour_s"], x["windows"]) == ("hourly_rollup", 1, "dsp", H, 2)
    assert x["rms_dbfs_mean"] == [-60.0 + j for j in range(8)] and x["band_pct_mean"] == [10.0, 20.0, 30.0, 25.0, 15.0]
    assert x["centroid_hz_mean"] == 1050.0
    assert x["noise_floor_db"] == {"min": -70.0, "p50": -66.0, "max": -66.0}
    assert x["transients"] == {"n": 1, "by_loudest_beam": [0, 0, 1, 0, 0, 0, 0, 0], "excess_db_max": 12.0}
    t = x["tonal"]
    assert (t["appeared_n"], t["gone_n"], t["state_n"], t["electronic_line_s"]) == (1, 1, 2, 10)
    assert t["lines"][0] == {"bin_hz": 980, "state_n": 2, "appeared_n": 1, "gone_n": 1, "dur_s_total": 52.0, "beam": 4, "beam_n": 2, "electronic": False}
    assert t["lines"][1] == {"bin_hz": 12000, "state_n": 1, "appeared_n": 0, "gone_n": 0, "dur_s_total": 0.0, "beam": None, "beam_n": 0, "electronic": True}
    assert x["spatial"] == {
        "windows": 2,
        "directional_n": 1,
        "leader_count": [0, 2, 0, 0, 0, 0, 0, 0],
        "rel_mean_db": [-3.0, 0.0, -3.0, -3.0, -3.0, -3.0, -3.0, -3.0],
        "decorr_mean": 0.6,
    }
    assert r.open == {} and r.written == 1


def test_rollup_restart_resumes_open_hour_without_double_count():
    d = tmp()
    r = rollup.HourlyRollup(d)
    r.feed({"k": "dsp", "t": H + 10, "rms_dbfs": [-60.0] * 8, "band_pct": [20.0] * 5, "centroid_hz": 1000.0})
    r.save()
    r2 = rollup.HourlyRollup(d)  # restart mid-hour
    r2.feed({"k": "dsp", "t": H + 20, "rms_dbfs": [-60.0] * 8, "band_pct": [20.0] * 5, "centroid_hz": 1000.0})
    r2.tick(H + 3720)
    assert [x["windows"] for x in lines(os.path.join(d, "hourly_rollup.jsonl"))] == [2]
    r3 = rollup.HourlyRollup(d)  # restart after the close: nothing re-written
    r3.tick(H + 9000)
    assert len(lines(os.path.join(d, "hourly_rollup.jsonl"))) == 1


def test_rollup_is_append_only_and_late_records_are_counted_not_applied():
    d = tmp()
    r = rollup.HourlyRollup(d)
    feed_hour(r)
    r.tick(H + 3720)
    p = os.path.join(d, "hourly_rollup.jsonl")
    first = open(p, "rb").read()
    r.feed({"k": "dsp", "t": H + 100, "rms_dbfs": [-60.0] * 8, "band_pct": [20.0] * 5, "centroid_hz": 1000.0})
    assert r.late == 1 and r.open == {}
    feed_hour(r, H + 3600)
    r.tick(H + 7320)
    data = open(p, "rb").read()
    assert data.startswith(first) and len(lines(p)) == 2 and lines(p)[1]["hour_s"] == H + 3600


def test_rollup_size_cap_is_enforced():
    d = tmp()
    r = rollup.HourlyRollup(d, cap_bytes=3000)
    for i in range(20):
        feed_hour(r, H + 3600 * i)
    r.tick(H + 3600 * 25)
    p = os.path.join(d, "hourly_rollup.jsonl")
    assert os.path.exists(p + ".1")
    assert os.path.getsize(p) <= 3000 and os.path.getsize(p + ".1") <= 3000
    assert r.written == 20 and r.write_errors == 0


def test_rollup_bad_record_is_counted_not_raised():
    r = rollup.HourlyRollup(tmp())
    r.feed({"k": "dsp", "t": H})  # missing fields
    assert r.errors == 1


# ------------------------------------------------------------------ readers ignore the new fields
def test_summarize_output_identical_for_old_and_new_rows():
    now = time.time()
    old = {
        "dsp": [
            {"k": "dsp", "t": now - 30, "rms_dbfs": [-60.0] * 8, "peak_dbfs": -40.0, "band_pct": [10, 20, 30, 25, 15], "centroid_hz": 900.0, "flatness": 0.1},
            {"k": "tonal_appeared", "t": now - 20, "hz": 978.1, "excess_db": 20.0},
            {"k": "tonal_gone", "t": now - 10, "hz": 978.1, "lasted_s": 52.0},
        ],
        "birds": [
            {
                "k": "bird",
                "t": now - 30,
                "beam": 2,
                "level_dbfs": -50.0,
                "common": "American Robin",
                "scientific": "Turdus migratorius",
                "confidence": 0.9,
                "provenance": "model_opinion",
            }
        ],
        "novelty": [{"k": "novelty", "t": now - 30, "scene": 1, "dist": 0.1, "score": 1.2, "novel": False, "seen": 50}],
        "spatial": [
            {
                "k": "spatial",
                "t": now - 30,
                "az_deg": 90.0,
                "R": 0.4,
                "dominant_beam": 3,
                "spread_db": 5.0,
                "e_rel_db": [0.0] * 8,
                "directional": False,
                "calibrated": False,
            }
        ],
    }
    new = json.loads(json.dumps(old))
    new["dsp"][1].update(beam=4, beam_margin_db=9.0)
    new["dsp"][2].update(beam=4)
    new["birds"][0].update(in_region=True, non_species=False, group="bird")
    filt = {
        "k": "bird_filtered",
        "t": now - 30,
        "beam": 2,
        "level_dbfs": -50.0,
        "common": "Fall Field Cricket",
        "scientific": "Gryllus pennsylvanicus",
        "confidence": 0.4,
        "in_region": False,
        "provenance": "model_opinion",
    }
    old["birds"].append(dict(filt))
    new["birds"].append({**filt, "reject_reason": "geo_not_predicted", "non_species": False, "group": "insect"})
    new["novelty"][0].update(run=1761500000, mv="km12-sk1")
    new["spatial"][0]["decorr"] = 0.5
    outs = []
    for name, rows in (("old", old), ("new", new)):
        d = tmp()
        for stream, rs in rows.items():
            with open(os.path.join(d, stream + ".jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rs))
        st = {
            "started": now - 100,
            "device": "cpu",
            "plugins": {"tags": {"speech_fraction": 0.1}},
            "rx": {"datagrams": 5, "bad": 0, "gaps": 0},
            "uptime_s": 100.0,
            "embeddings_stored": 3,
            "audio_written": False,
        }
        if name == "new":
            st["meta"] = {"write_errors": 0}
        write_json(os.path.join(d, "status.json"), st)
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "summarize.py"), d, "10"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        outs.append(r.stdout.replace(d, "<DIR>"))
    import difflib

    assert outs[0] == outs[1], chr(10).join(difflib.unified_diff(outs[0].splitlines(), outs[1].splitlines()))
    assert "978" in outs[0]  # identical, and the tonal row was really read (positive twin)


def test_learn_tonal_mask_reads_new_tonal_state_identically():
    import learn_tonal_mask as L

    base = {
        "k": "tonal_state",
        "t": 1761436800.0,
        "lines": [{"hz": 12000.0, "excess_db": 15.0, "age_s": 9.0}],
        "noise_floor_db": -70.0,
        "noise_floor_drift_db": 0.0,
    }
    newr = json.loads(json.dumps(base))
    newr["lines"][0]["beam"] = 4
    res = []
    for rec in (base, newr):
        p = os.path.join(tmp(), "dsp.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps(rec) + "\n")
        res.append(L.load_states(p))
    assert res[0] == res[1] and len(res[0][0]) == 1 and res[0][1] == {round(12000.0 / L.DF): 12000.0}


def test_out_routes_dsp_to_a_192mb_cap_others_64mb():
    import importlib.util

    spec = importlib.util.spec_from_file_location("host_mod", os.path.join(HERE, "host.py"))
    sys.path.insert(0, CLASSIFY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    d = tmp()
    o = m.Out(d)
    o.write("dsp", {"k": "dsp"})
    o.write("birds", {"k": "bird"})
    assert (o.w["dsp"].cap, o.w["birds"].cap) == (192 << 20, 64 << 20)
    assert lines(os.path.join(d, "dsp.jsonl")) == [{"k": "dsp"}] and o.counters() == (0, 0)


if __name__ == "__main__":
    bad = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                bad += 1
                import traceback

                traceback.print_exc()
                print("FAIL", name, type(e).__name__, e)
    print(f"{bad} failure(s)")
    sys.exit(1 if bad else 0)
