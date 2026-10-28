#!/usr/bin/env python3
"""Tests for analysis/events.py and its two consumers (contract event-model 1.0.0).  Stdlib only.  Exact values, hand-written.

    python -I test_events.py [--real-birds FILE]

`--real-birds FILE` (read only) compares the new clustering with the pre-migration algorithm (LEGACY_*, a verbatim copy) on real rows."""
import hashlib, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import events as E  # noqa: E402
import beam_agreement as BA  # noqa: E402
import labels as L  # noqa: E402

TOOL = os.path.join(HERE, "..", "tools", "bma_labels.py")
TAXA = os.path.join(HERE, "..", "ui", "taxa.json")
fails, n_checks = [], 0


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def raises(exc, match, fn):
    try:
        fn()
    except exc as e:
        return match in str(e) and type(e) is exc
    except Exception:           # noqa: BLE001
        return False
    return False


def row(t, beam=1, key=("Red-bellied Woodpecker", "Melanerpes carolinus"), conf=0.7, k="bird", **kw):
    r = {"k": k, "t": t, "beam": beam, "level_dbfs": -60.0, "common": key[0], "scientific": key[1], "confidence": conf, "provenance": "model_opinion"}
    r.update(kw)
    return r


RBW = "melanerpes carolinus"
T0 = 1792940400.0

# ------------------------------------------------------------------------------------------------ id algorithm
txt = "ev1\n%s\n%d\n%s" % (RBW, 1792940400000, "1792940400000:1;1792940400000:2;1792940402000:1")
want = "ev1-" + hashlib.sha256(txt.encode()).hexdigest()[:24]
rows = [row(T0, 1), row(T0, 2), row(T0 + 2, 1)]
res = E.build(rows, 3.0)
check("id: sha256 of `ev1\\n<key>\\n<first ms>\\n<t_ms:beam;...>`, first 24 hex, prefix ev1-", res.clusters[0].id == want, (res.clusters[0].id, want))
check("id: pinned literal for that event", want == "ev1-6a216aaaba6133f1325fc813", want)
check("id: same inputs run twice -> same ids", [c.id for c in E.build(rows, 3.0).clusters] == [c.id for c in E.build(rows, 3.0).clusters])
check("id: member order does not matter (reversed rows, same set, same id)", E.build(list(reversed(rows)), 3.0).clusters[0].id == want)
check("id twin: one more (beam, window) member -> a NEW id", E.build(rows + [row(T0 + 2, 2)], 3.0).clusters[0].id != want)
check("id twin: a different species key with the same members -> a different id", E.event_id("other", E.build(rows, 3.0).clusters[0].members) != want)
check("id twin: same windows, one report on another beam -> a different id",
      E.build([row(T0, 1), row(T0, 3), row(T0 + 2, 1)], 3.0).clusters[0].id != want)
check("id: t is quantised to ms (1e-4 s jitter is the same member), 1 ms is not",
      E.build([row(T0 + 0.0001, 1), row(T0, 2), row(T0 + 2, 1)], 3.0).clusters[0].id == want
      and E.build([row(T0 + 0.001, 1), row(T0, 2), row(T0 + 2, 1)], 3.0).clusters[0].id != want)

# ------------------------------------------------------------------------------------------------ conservation
mix = [row(T0, 1), row(T0 + 1, 2),
       row(T0, 1, k="bird_filtered"),                       # out_of_region (kinds = bird)
       row(T0, 1, k="dsp"), "junk",                         # other_kind x2
       row(T0, 9),                                          # invalid beam
       row(T0, 1, key=("Human vocal", "Human vocal")),      # hidden_human
       row(T0, 1)]                                          # duplicate window (kept unless dedup)
r = E.build(mix, 3.0)
c = r.check()
check("conservation: 8 input = 3 reports in 1 event + 5 rejections (other_kind 2, out_of_region 1, invalid 1, hidden_human 1), duplicate_windows 1",
      c == {"input": 8, "events": 1, "reports": 3, "rejected": {"other_kind": 2, "out_of_region": 1, "invalid": 1, "hidden_human": 1, "duplicate_window": 0},
            "duplicate_windows": 1}, c)
r2 = E.build(mix, 3.0, dedup=True)
c2 = r2.check()
check("conservation twin: dedup=True rejects the duplicate window (reports 2, duplicate_window 1), still balanced",
      c2["reports"] == 2 and c2["rejected"]["duplicate_window"] == 1 and c2["input"] == 8, c2)
r3 = E.build(mix, 3.0, kinds=("bird", "bird_filtered"), drop_human=False)
c3 = r3.check()
check("conservation twin: kinds + keep humans -> out_of_region 0, hidden_human 0, 2 events (woodpecker, human), reports 5, duplicate_windows 2",
      c3["rejected"]["out_of_region"] == 0 and c3["rejected"]["hidden_human"] == 0 and c3["events"] == 2 and c3["reports"] == 5 and c3["duplicate_windows"] == 2, c3)
try:
    huge = E.build([row(10 ** 400), row(T0)], 3.0)
    huge_ok = huge.check()["rejected"]["invalid"] == 1 and huge.reports == 1
except OverflowError as ex:
    huge_ok = False
check("invalid: t = 10**400 is rejected (no OverflowError), the finite twin is kept", huge_ok)
check("conservation: empty input is balanced at 0", E.build([], 3.0).check()["input"] == 0)
bad = E.build(mix, 3.0)
bad.rejected["invalid"] -= 1
check("conservation fails loudly: a lost rejection raises ConservationError naming the numbers",
      raises(E.ConservationError, "input 8 != reports-in-events 3 + rejected 4", bad.check))
bad2 = E.build(mix, 3.0)
bad2.input += 1
check("conservation fails loudly: an unseen extra input raises ConservationError", raises(E.ConservationError, "input 9 != reports-in-events 3 + rejected 5", bad2.check))

# ------------------------------------------------------------------------------------------------ supersedes
run1 = [row(T0 + 10, 1), row(T0 + 12, 2)]
ev1 = E.build(run1, 3.0)
snap = [c.snapshot() for c in ev1.clusters]
run2 = run1 + [row(T0 + 8, 3)]                              # a late beam report 2 s before the first window: the event grows
ev2 = E.build(run2, 3.0)
d = E.diff(snap, ev2.clusters)
old_id, new_id = snap[0]["id"], ev2.clusters[0].id
check("supersedes: a grown event gets a new id and the old id points at it",
      old_id != new_id and d == {"kept": [], "superseded": {old_id: new_id}, "orphaned": [], "new": [], "membership_changed": 1}, d)
d0 = E.diff(snap, E.build(run1, 3.0).clusters)
check("supersedes twin: the same inputs keep the id, nothing superseded, membership_changed 0",
      d0 == {"kept": [old_id], "superseded": {}, "orphaned": [], "new": [], "membership_changed": 0}, d0)
d_new = E.diff(snap, E.build(run1 + [row(T0 + 100, 1)], 3.0).clusters)
check("supersedes twin: an unrelated new event is `new`, the old one is kept", d_new["kept"] == [old_id] and len(d_new["new"]) == 1 and d_new["superseded"] == {}, d_new)
split = {"id": "ev1-" + "0" * 24, "key": RBW, "members": [[int(round((T0 + 10) * 1000)), 1], [int(round((T0 + 100) * 1000)), 1]]}
d_or = E.diff([split], E.build(run1 + [row(T0 + 100, 1)], 3.0).clusters)
check("orphan: an old event whose members now live in two events is orphaned, not superseded",
      d_or["orphaned"] == [split["id"]] and d_or["superseded"] == {} and d_or["membership_changed"] == 0, d_or)
gone = E.diff(snap, [])
check("orphan: an old event with no current event is orphaned", gone["orphaned"] == [old_id] and gone["membership_changed"] == 0, gone)
other = E.build([row(T0 + 10, 1, key=("A", "Aus bus")), row(T0 + 12, 2, key=("A", "Aus bus"))], 3.0)
check("supersedes: other species with the same windows never receives the link", E.diff(snap, other.clusters)["orphaned"] == [old_id])
tab = E.merge_aliases({}, d)
run3 = E.build(run2 + [row(T0 + 6, 4)], 3.0)
tab = E.merge_aliases(tab, E.diff([ev2.clusters[0].snapshot()], run3.clusters))
check("aliases: two growths keep every old id one hop from the current id", tab == {old_id: run3.clusters[0].id, new_id: run3.clusters[0].id}, tab)
check("aliases: resolve follows the table; an unknown id resolves to itself", E.resolve(old_id, tab) == run3.clusters[0].id and E.resolve("x", tab) == "x")

# ------------------------------------------------------------------------------------------------ legacy golden (pre-migration algorithms, verbatim)
def legacy_cluster_rows(rows, gap_s, valid, accept, human):
    events, last = [], {}
    for o in rows:
        if not valid(o) or not accept(o) or (human and E.is_human(o)):
            continue
        key = E.key_of(o)
        ev = last.get(key)
        if ev is not None and ev["t_start"] - gap_s <= o["t"] <= ev["t_end"] + gap_s:
            ev["t_start"], ev["t_end"] = min(ev["t_start"], o["t"]), max(ev["t_end"], o["t"])
            ev["rows"].append(o)
        else:
            ev = {"key": key, "t_start": o["t"], "t_end": o["t"], "rows": [o]}
            events.append(ev)
            if key not in last or ev["t_start"] >= last[key]["t_start"]:
                last[key] = ev
    return events


def stress_rows(n=4000):
    x, out = 12345, []
    names = [("Red-bellied Woodpecker", "Melanerpes carolinus"), ("Engine", "Engine"), ("Human vocal", "Human vocal"), ("Katydid", None), ("Wren", "Troglodytes aedon")]
    for _ in range(n):
        x = (x * 1103515245 + 12345) % 2147483648
        nm = names[x % 5]
        x = (x * 1103515245 + 12345) % 2147483648
        t = T0 + (x % 40000) / 100.0                         # out of order on purpose
        x = (x * 1103515245 + 12345) % 2147483648
        kind = ("bird", "bird", "bird", "bird_filtered", "dsp")[x % 5]
        o = {"k": kind, "t": t, "beam": 1 + x % 9 if x % 31 == 0 else 1 + (x >> 3) % 8, "level_dbfs": -60.0, "common": nm[0], "confidence": ((x >> 5) % 100) / 100.0}
        if nm[1] is not None:
            o["scientific"] = nm[1]
        if x % 53 == 0:
            o["confidence"] = 1.5
        out.append(o)
    return out


def fp_ba(evs):
    return [(e.key, e.t_start, e.t_end, sorted((t, sorted(b)) for t, b in e.windows.items()), e.reports) for e in evs]


def fp_leg(evs):
    out = []
    for e in evs:
        w = {}
        for o in e["rows"]:
            w.setdefault(o["t"], set()).add(o["beam"])
        out.append((e["key"], e["t_start"], e["t_end"], sorted((t, sorted(b)) for t, b in w.items()), len(e["rows"])))
    return out


def golden(rows, label):
    ta = BA.Taxa(TAXA)
    for gap in (3.0, 5.0):
        got = fp_ba(BA.cluster(rows, gap, ta))
        want = fp_leg(legacy_cluster_rows(rows, gap, BA.valid_bird, lambda o: o["k"] == "bird", True))
        check("golden %s: beam_agreement.cluster at gap %s equals the pre-migration algorithm (%d events)" % (label, gap, len(want)), got == want and len(want) > 0, (len(got), len(want)))
    lt = L.Taxa(TAXA)
    got = [(e.key, e.t_start, e.t_end, e.reports, e.event_id) for e in L.iter_events(rows, lt)]
    leg = legacy_cluster_rows(rows, 3.0, L.valid_bird, lambda o: True, False)
    want = [(e["key"], e["t_start"], e["t_end"], len(e["rows"]), L.make_event_id(e["t_start"], e["key"])) for e in leg]
    check("golden %s: labels.iter_events equals the pre-migration algorithm incl. legacy event ids (%d events)" % (label, len(want)), got == want and len(want) > 0, (len(got), len(want)))
    res = E.build(rows, 3.0, kinds=E.KINDS, drop_human=False)
    check("golden %s: conservation holds (%s)" % (label, json.dumps(res.counts(), sort_keys=True)), bool(res.check()))


golden(stress_rows(), "synthetic 4000 rows")
sr = stress_rows()
check("golden synthetic: the stress rows really exercise every rejection (other_kind, out_of_region, invalid, hidden_human all > 0)",
      all(v > 0 for k_, v in E.build(sr, 3.0).rejected.items() if k_ != "duplicate_window"), E.build(sr, 3.0).rejected)
if "--real-birds" in sys.argv:
    real = [json.loads(ln) for ln in open(sys.argv[sys.argv.index("--real-birds") + 1], "rb").read().split(b"\n") if ln.strip()]
    golden(real, "real %d rows" % len(real))

# ------------------------------------------------------------------------------------------------ labels: ids and resolution
lrows = [row(T0 + 10, 1), row(T0 + 12, 2)]
ev_old = L.iter_events(lrows, L.Taxa(TAXA))[0]
check("labels: the verdict-labels event_id stays the legacy `<start ms>-<slug>` and gains a content_id",
      ev_old.event_id == "%d-melanerpes-carolinus" % int((T0 + 10) * 1000) and E.ID_RE.match(ev_old.content_id) is not None, (ev_old.event_id, ev_old.content_id))
ev_f = L.iter_events([row(T0 + 10, 1, k="bird_filtered")], L.Taxa(TAXA))[0]
ev_bf = L.iter_events([row(T0 + 10, 1, k="bird_filtered"), row(T0 + 11, 2, in_region=True)], L.Taxa(TAXA))[0]
check("labels: an event of bird_filtered rows only has in_region False; with one in-region bird row it is True (positive twin)",
      (ev_f.in_region, ev_f.reports, ev_bf.in_region, ev_bf.reports) == (False, 1, True, 2), (ev_f.in_region, ev_bf.in_region))
rec = L.make_record(ev_old, "correct", "field", t_label=T0 + 100, label_id="0123456789abcdef")
check("resolve: a label on a current event is exact", L.resolve_labels([rec], [ev_old]) == {"exact": 1, "superseded": {}, "orphaned": [], "ambiguous": 0})
ev_new = L.iter_events(lrows + [row(T0 + 8, 3)], L.Taxa(TAXA))[0]
check("resolve: the event grew by a late earlier report, its legacy id changed",
      ev_new.event_id != ev_old.event_id and ev_new.event_id.startswith(str(int((T0 + 8) * 1000)) + "-"))
check("resolve: the old label resolves to the current event through supersedes (not orphaned)",
      L.resolve_labels([rec], [ev_new]) == {"exact": 0, "superseded": {ev_old.event_id: ev_new.event_id}, "orphaned": [], "ambiguous": 0})
ev_far = L.iter_events([row(T0 + 500, 1)], L.Taxa(TAXA))[0]
check("resolve twin: a current event far outside the label's time is an orphan", L.resolve_labels([rec], [ev_far]) == {"exact": 0, "superseded": {}, "orphaned": [ev_old.event_id], "ambiguous": 0})
ev_oth = L.iter_events([row(T0 + 9, 3, key=("A", "Aus bus")), row(T0 + 11, 3, key=("A", "Aus bus"))], L.Taxa(TAXA))[0]
check("resolve twin: another species at the same time is an orphan, not a hit", L.resolve_labels([rec], [ev_oth])["orphaned"] == [ev_old.event_id])

# ------------------------------------------------------------------------------------------------ tool and report
tmp = tempfile.mkdtemp(prefix="evtest_")
os.makedirs(os.path.join(tmp, "analysis"))
with open(os.path.join(tmp, "analysis", "birds.jsonl"), "wb") as f:
    f.write(b"".join((json.dumps(o) + "\n").encode() for o in mix[:2] + mix[2:3] + [mix[3], mix[5], mix[6], mix[7]]))
p = subprocess.run([sys.executable, "-I", TOOL, "--data-dir", tmp, "--taxa", TAXA, "events"], capture_output=True)
exp = {"k": "event_model_report", "v": 1, "id_version": "ev1", "gap_s": 3.0, "conservation": {"input": 7, "events": 2, "reports": 5, "rejected": {"other_kind": 1, "out_of_region": 0, "invalid": 1, "hidden_human": 0, "duplicate_window": 0}, "duplicate_windows": 2},
       "labels": {"exact": 0, "superseded": 0, "orphaned": 0, "ambiguous": 0}}
check("tool: `bma_labels events` prints the conservation counts and label resolution, exit 0 (bird_filtered kept at the labels level)",
      p.returncode == 0 and json.loads(p.stdout) == exp, (p.returncode, p.stdout, p.stderr))
bp = os.path.join(tmp, "analysis", "birds.jsonl")
rp = subprocess.run([sys.executable, "-I", os.path.join(HERE, "beam_agreement.py"), "--birds", bp, "--generated-s", "1", "--taxa", TAXA, "--out", os.path.join(tempfile.gettempdir(), "evtest_report.json")],
                    capture_output=True)
check("beam_agreement CLI reports event conservation on stderr and exits 0",
      rp.returncode == 0 and b'event conservation ok: {"duplicate_windows": 1, "events": 1, "input": 7, "rejected": {"duplicate_window": 0, "hidden_human": 1, "invalid": 1, "other_kind": 1, "out_of_region": 1}, "reports": 3}' in rp.stderr,
      (rp.returncode, rp.stderr))
try:
    os.remove(os.path.join(tempfile.gettempdir(), "evtest_report.json"))
except OSError:
    pass
check("the sources are LF only", all(b"\r" not in open(p_, "rb").read() for p_ in (os.path.join(HERE, "events.py"), os.path.join(HERE, "test_events.py"))))
check("events.py is stdlib only and does no I/O (no open, os, socket, urllib imports)",
      all(w not in open(os.path.join(HERE, "events.py"), encoding="utf-8").read() for w in ("open(", "import os", "socket", "urllib", "subprocess")))
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
