#!/usr/bin/env python3
"""Tests for the BirdNET location/date filter (county level) in bma_classify.BirdNet and the birds plugin.
birdnetlib itself is replaced by a RECORDING stub (it logs the exact kwargs it is given and returns canned
detections), so the code under test runs for real; one test also asks the REAL birdnetlib for its species list.
Run in the audioeval venv (numpy, scipy, birdnetlib):  python test_geo.py"""

import contextlib, importlib.util, io, json, logging, os, sys, tempfile, types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
fails, skips = [], []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- REAL birdnetlib species list (before any stub)
CELL = (38.375, -98.125)  # an arbitrary county-level cell centre in the central US (not the deployment); WEEK 40 = late Oct
try:
    with contextlib.redirect_stdout(io.StringIO()):
        from birdnetlib.analyzer import Analyzer

        an = Analyzer()
        lst = an.return_predicted_species_list(lat=CELL[0], lon=CELL[1], week_48=40)
    check("real birdnetlib: species list for the cell has a plausible size (50..300)", 50 < len(lst) < 300, str(len(lst)))
    check("real birdnetlib: Mallard is in the list", "Anas platyrhynchos_Mallard" in lst)
    check("real birdnetlib: Alpine Swift is NOT in the list", "Apus melba_Alpine Swift" not in lst)
    check("real birdnetlib: Laysan Albatross is NOT in the list", "Phoebastria immutabilis_Laysan Albatross" not in lst)
    check("real birdnetlib: Tawny Frogmouth is NOT in the list", "Podargus strigoides_Tawny Frogmouth" not in lst)
    check("real birdnetlib: a cricket is NOT in the list (non-birds are not covered)", not any("Four-spotted Tree Cricket" in s for s in lst))
except ImportError as e:
    skips.append("real birdnetlib list (" + str(e) + ")")
    print("SKIP real birdnetlib species list:", e)
for m in [k for k in sys.modules if k == "birdnetlib" or k.startswith("birdnetlib.")]:
    del sys.modules[m]

# ---------------------------------------------------------------- recording stub for birdnetlib
CALLS = []
CANNED = [
    dict(common_name="Mallard", scientific_name="Anas platyrhynchos", confidence=0.91, start_time=0.0, end_time=3.0, in_list=True),
    dict(common_name="Alpine Swift", scientific_name="Apus melba", confidence=0.74, start_time=0.0, end_time=3.0, in_list=False),
    dict(common_name="Laysan Albatross", scientific_name="Phoebastria immutabilis", confidence=0.35, start_time=0.0, end_time=3.0, in_list=False),
    dict(common_name="Four-spotted Tree Cricket", scientific_name="Oecanthus quadripunctatus", confidence=0.45, start_time=0.0, end_time=3.0, in_list=False),
    dict(common_name="Engine", scientific_name="Engine", confidence=0.34, start_time=0.0, end_time=3.0, in_list=False),
]
UNCOVERED = frozenset({"Oecanthus quadripunctatus_Four-spotted Tree Cricket"})


class RecBuf:
    def __init__(self, analyzer, audio, sr, **kw):
        CALLS.append(dict(kw))
        self.kw = kw

    def analyze(self):
        pass

    @property
    def detections(self):
        out = []
        for d in CANNED:
            r = {k: v for k, v in d.items() if k != "in_list"}
            if self.kw.get("return_all_detections"):
                r["is_predicted_for_location_and_date"] = d["in_list"]
            out.append(r)
        return out


class FakeAnalyzer:
    pass


bl = types.ModuleType("birdnetlib")
bl.RecordingBuffer = RecBuf
bl.__version__ = "stub"
bla = types.ModuleType("birdnetlib.analyzer")
bla.Analyzer = FakeAnalyzer
sys.modules["birdnetlib"], sys.modules["birdnetlib.analyzer"] = bl, bla
import bma_classify as C  # noqa: E402

AUDIO = np.zeros(144000, dtype=np.float32)
keyset = lambda dets: {(d["common"]) for d in dets}

# ---------------------------------------------------------------- county_level
lat, lon = C.county_level(10.123456, 20.654321)  # arbitrary synthetic position, not anyone's
check("county_level: snaps to the 0.25-degree cell CENTRE (10.125, 20.625)", (lat, lon) == (10.125, 20.625), str((lat, lon)))
check("county_level: idempotent", C.county_level(lat, lon) == (lat, lon))
check(
    "county_level: negative values snap by floor (-76.0 -> -75.875 cell centre of [-76.0,-75.75))",
    C.county_level(40.0, -76.0) == (40.125, -75.875),
    str(C.county_level(40.0, -76.0)),
)
check("county_level: two positions 0.2 degrees apart in one cell give the same value", C.county_level(10.01, 20.01) == C.county_level(10.20, 20.24))
check("county_level: the cell boundary splits (10.249 vs 10.251 differ)", C.county_level(10.249, 20.0)[0] != C.county_level(10.251, 20.0)[0])
try:
    C.county_level(91.0, 0.0)
    ok = False
except ValueError as e:
    ok = "out of range" in str(e)
check("county_level: latitude 91 raises ValueError('out of range')", ok)
check("county_level: the pole clamps inside the valid range", C.county_level(90.0, 0.0)[0] <= 90.0)

# ---------------------------------------------------------------- config / coverage / helpers
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "geo.json")
    json.dump({"lat": 10.123456, "lon": 20.654321}, open(p, "w"))
    g = C.load_geo_config(p)
    check("load_geo_config: a precise value in the file comes back county-level", (g["lat"], g["lon"]) == (10.125, 20.625), str(g))
    check("load_geo_config: defaults week -1, threshold None", g["week"] == -1 and g["threshold"] is None)
    json.dump({"lat": 10.125, "lon": 20.625, "week": 7, "threshold": 0.1}, open(p, "w"))
    g = C.load_geo_config(p)
    check("load_geo_config: week and threshold are read", g["week"] == 7 and g["threshold"] == 0.1)
    json.dump({"lat": 10.1}, open(p, "w"))
    try:
        C.load_geo_config(p)
        ok = False
    except KeyError as e:
        ok = "lon" in str(e)
    check("load_geo_config: missing lon raises KeyError('lon')", ok)
    json.dump({"uncovered": ["a_b", "c_d"]}, open(os.path.join(td, "cov.json"), "w"))
    check("load_uncovered: reads the list", C.load_uncovered(os.path.join(td, "cov.json")) == frozenset({"a_b", "c_d"}))
    check("load_uncovered: a missing file gives None (never drop)", C.load_uncovered(os.path.join(td, "nope.json")) is None)
check("is_non_species: Engine_Engine yes", C.is_non_species("Engine", "Engine"))
check("is_non_species: a real species no (positive twin)", not C.is_non_species("Anas platyrhynchos", "Mallard"))
shipped = C.load_uncovered()
check(
    "shipped birdnet_coverage.json: has the crickets/katydids/frog/wolf as uncovered and not the four false positives",
    shipped is not None
    and all(any(s.endswith("_" + n) for s in shipped) for n in ("Four-spotted Tree Cricket", "Common Virtuoso Katydid", "Pacific Chorus Frog", "Gray Wolf"))
    and not any(s.endswith(("_Alpine Swift", "_Laysan Albatross", "_Black-footed Albatross", "_Tawny Frogmouth", "_Mallard")) for s in shipped),
)

# ---------------------------------------------------------------- no location: exactly the old behaviour
CALLS.clear()
off = C.BirdNet(0.25)
d = off.classify(AUDIO)
check("off: BirdNet kwargs are exactly {min_conf} (nothing location-related is passed)", CALLS == [{"min_conf": 0.25}], str(CALLS))
check(
    "off: every detection is returned, with exactly the old five keys",
    len(d) == 5 and all(set(x) == {"common", "scientific", "confidence", "start_s", "end_s"} for x in d),
    str(d[:1]),
)
check("off: nothing is filtered", off.filtered == [])

# ---------------------------------------------------------------- location on
CALLS.clear()
on = C.BirdNet(0.25, 10.123456, 20.654321, uncovered=UNCOVERED)  # a precise position is coarsened at once
check("on: the object holds only the county-level cell centre", (on.lat, on.lon) == (10.125, 20.625), str((on.lat, on.lon)))
import datetime as _dt

d = on.classify(AUDIO, when=_dt.datetime(2026, 10, 24, 20, 0))
check(
    "on: only the cell centre, return_all_detections and the window date reach birdnetlib",
    CALLS == [{"min_conf": 0.25, "lat": 10.125, "lon": 20.625, "return_all_detections": True, "date": _dt.datetime(2026, 10, 24, 20, 0)}],
    str(CALLS),
)
check(
    "on: returned = Mallard, the cricket (uncovered) and Engine (non-species)", keyset(d) == {"Mallard", "Four-spotted Tree Cricket", "Engine"}, str(keyset(d))
)
check(
    "on: Alpine Swift and Laysan Albatross are filtered, with their detail kept",
    {x["common"] for x in on.filtered} == {"Alpine Swift", "Laysan Albatross"} and on.filtered[0]["confidence"] == 0.74,
    str(on.filtered),
)
by = {x["common"]: x for x in d}
check("on: Mallard is in_region True, non_species False", by["Mallard"]["in_region"] is True and by["Mallard"]["non_species"] is False)
check(
    "on: the cricket is kept with in_region None (the location model cannot judge it), non_species False",
    by["Four-spotted Tree Cricket"]["in_region"] is None and by["Four-spotted Tree Cricket"]["non_species"] is False,
)
check("on: Engine is kept with non_species True and in_region None", by["Engine"]["non_species"] is True and by["Engine"]["in_region"] is None)
check("on: filtered items carry in_region False", all(x["in_region"] is False for x in on.filtered))
CALLS.clear()
on.classify(AUDIO)
check("on: with no `when` the date is today", CALLS[0]["date"] == _dt.date.today(), str(CALLS))
CALLS.clear()
wk = C.BirdNet(0.25, 10.1, 20.6, week=12, uncovered=UNCOVERED)
wk.classify(AUDIO)
check("on: an explicit week goes as week_48 and no date is sent", CALLS[0].get("week_48") == 12 and "date" not in CALLS[0], str(CALLS))
CALLS.clear()
th = C.BirdNet(0.25, 10.1, 20.6, filter_threshold=0.2, uncovered=UNCOVERED)
th.classify(AUDIO)
check("on: a configured threshold is passed as filter_threshold", CALLS[0].get("filter_threshold") == 0.2, str(CALLS))
CALLS.clear()
C.BirdNet(0.25, 10.1, 20.6, uncovered=UNCOVERED).classify(AUDIO)
check("on: no threshold configured -> filter_threshold is not passed (library default)", "filter_threshold" not in CALLS[0])

# coverage unknown: flag only, never drop
nc = C.BirdNet(0.25, 10.1, 20.6, uncovered=None)
d2 = nc.classify(AUDIO)
check("no coverage data: nothing is dropped (5 returned), nothing filtered", len(d2) == 5 and nc.filtered == [], str(len(d2)))
check(
    "no coverage data: every in_region is None (not judged), Engine still flagged non_species",
    all(x["in_region"] is None for x in d2) and {x["common"] for x in d2 if x["non_species"]} == {"Engine"},
)

# emit_filtered writes an audit line per removed detection
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    C.emit_filtered(on, -60.04)
lines = [json.loads(x) for x in buf.getvalue().splitlines()]
check(
    "emit_filtered: one filtered_out_of_region JSON line per removed detection, with level",
    [(x["event"], x["common"], x["level_dbfs"]) for x in lines]
    == [("filtered_out_of_region", "Alpine Swift", -60.0), ("filtered_out_of_region", "Laysan Albatross", -60.0)],
    str(lines),
)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    C.emit_filtered(off, -60.0)
check("emit_filtered: nothing printed when nothing was filtered", buf.getvalue() == "")


# ---------------------------------------------------------------- privacy: nothing precise is logged
class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, r):
        self.msgs.append(r.getMessage())


cap = Cap()
C.log.addHandler(cap)
C.log.setLevel(logging.DEBUG)
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "geo.json")
    json.dump({"lat": 10.123456, "lon": 20.654321}, open(p, "w"))
    ns = types.SimpleNamespace(geo_config=p, min_conf=0.25, lat=None, lon=None, week=-1)
    nb = C.BirdNet.from_args(ns)
text = " ".join(cap.msgs)
check("from_args: logs the county-level cell centre", "10.125,20.625" in text, text)
check("from_args: the precise digits appear nowhere in the log", "10.123456" not in text and "20.654321" not in text and "123456" not in text)
check("from_args: the object holds the cell centre", (nb.lat, nb.lon) == (10.125, 20.625))
ns2 = types.SimpleNamespace(min_conf=0.25, lat=10.123456, lon=20.654321, week=-1)
check("from_args: --lat/--lon are coarsened too", (C.BirdNet.from_args(ns2).lat, C.BirdNet.from_args(ns2).lon) == (10.125, 20.625))
ns3 = types.SimpleNamespace(min_conf=0.25, lat=None, lon=None, week=-1)
check("from_args: no config and no lat/lon -> the filter is off", C.BirdNet.from_args(ns3).geo is False)

# ---------------------------------------------------------------- birds plugin (analysis/plugins.py)
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))  # plugins.py imports its sibling taxa.py
try:
    spec = importlib.util.spec_from_file_location("plugins_under_test", os.path.join(HERE, "..", "analysis", "plugins.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
except ImportError as e:
    P = None
    skips.append("plugin tests (" + str(e) + ")")
    print("SKIP plugin tests:", e)
if P is not None:
    rng = np.random.default_rng(3)
    x = (rng.standard_normal((144000, 8)) * 10 ** (-50 / 20)).astype(np.float32)  # -50 dBFS, above the -70 gate
    po = P.MultiBeamBirds(HERE)
    CALLS.clear()
    recs, _ = po.process(x, 1792884000.0)
    check(
        "plugin off: 10 records (2 beams x 5 detections), all k=bird, in_region null (nothing judged), none filtered",
        len(recs) == 10 and all(r["k"] == "bird" and r["in_region"] is None for r in recs),
        str(recs[:1]),
    )
    check("plugin off: no location kwargs reached BirdNET", all(set(c) == {"min_conf"} for c in CALLS), str(CALLS[:1]))
    with tempfile.TemporaryDirectory() as td:
        pth = os.path.join(td, "geo.json")
        json.dump({"lat": 10.123456, "lon": 20.654321}, open(pth, "w"))
        pg = P.MultiBeamBirds(HERE, geo_config=pth)
        pg.bn.uncovered = UNCOVERED
        CALLS.clear()
        recs, _ = pg.process(x, 1792884000.0)
    kept = [r for r in recs if r["k"] == "bird"]
    gone = [r for r in recs if r["k"] == "bird_filtered"]
    check(
        "plugin on: per beam 3 kept (Mallard, cricket, Engine) and 2 audit records (2 beams -> 6 and 4)",
        (len(kept), len(gone)) == (6, 4),
        f"{len(kept)} {len(gone)}",
    )
    check(
        "plugin on: kept records carry in_region and non_species",
        all("in_region" in r and "non_species" in r for r in kept) and {r["common"] for r in kept if r["non_species"]} == {"Engine"},
    )
    check(
        "plugin on: audit records are bird_filtered with in_region False and the removed species",
        {r["common"] for r in gone} == {"Alpine Swift", "Laysan Albatross"} and all(r["in_region"] is False for r in gone),
    )
    check(
        "plugin on: the window's own date reaches BirdNET (week from the window time, not the process clock)",
        all(c["date"] == _dt.datetime.fromtimestamp(1792884000.0) for c in CALLS),
        str(CALLS[:1]),
    )
    check("plugin on: only the cell centre reaches BirdNET", all((c["lat"], c["lon"]) == (10.125, 20.625) for c in CALLS))
    with tempfile.TemporaryDirectory() as td:
        missing = os.path.join(td, "geo.json")  # a path that does not exist
        try:
            P.MultiBeamBirds(HERE, geo_config=missing)
            ok = False
        except FileNotFoundError:
            ok = True
        check("plugin: geo_config pointing at a missing file fails loudly (FileNotFoundError), not silently unfiltered", ok)
        # the old $BMA_GEO_CONFIG fallback is gone: the variable set to a valid file must NOT switch the filter on
        valid = os.path.join(td, "valid.json")
        json.dump({"lat": 10.123456, "lon": 20.654321}, open(valid, "w"))
        os.environ["BMA_GEO_CONFIG"] = valid
        try:
            env_only = P.MultiBeamBirds(HERE)
            with_flag = P.MultiBeamBirds(HERE, geo_config=valid)
        finally:
            os.environ.pop("BMA_GEO_CONFIG", None)
        check("plugin: $BMA_GEO_CONFIG alone does not enable the filter (only geo_config does)", env_only.geo_on is False, str(env_only.geo_on))
        check("plugin: the same file passed as geo_config enables it (positive twin)", with_flag.geo_on is True, str(with_flag.geo_on))

print(f"\n{len(fails)} failure(s), {len(skips)} skipped {skips if skips else ''}")
sys.exit(1 if fails or skips else 0)
