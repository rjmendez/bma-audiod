#!/usr/bin/env python3
"""Tests for analysis/labels.py and tools/bma_labels.py (verdict-labels 1.0.0).  Exact values; every negative case has a positive twin;
the unit under test is never stubbed.  Expected values are written by hand here, never produced by the code under test.  Temp dirs only.

    python -I test_labels.py [--real-birds FILE]      (stdlib; the parity check against the dashboard needs ui/bma_ui.py, which needs numpy)

`--real-birds FILE` additionally runs the dashboard and the library over the rows of a real birds.jsonl (read only) and compares the event ids."""
import ast, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(BMA, "ui"))
import labels as L  # noqa: E402
import bma_ui as UI  # noqa: E402

TOOL = os.path.join(BMA, "tools", "bma_labels.py")
TAXA = os.path.join(BMA, "ui", "taxa.json")
fails, n_checks = [], 0


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def raises(exc, match, fn):
    """True when fn() raises exactly `exc` and the message contains `match`."""
    try:
        fn()
    except exc as e:
        return match in str(e) and type(e) is exc
    except Exception:           # noqa: BLE001
        return False
    return False


T0 = 1792940400.0
RBW = ("Red-bellied Woodpecker", "Melanerpes carolinus")
NUT = ("White-breasted Nuthatch", "Sitta carolinensis")


def row(t, sp=RBW, conf=0.7, beam=1, region=True, k="bird", **kw):
    r = {"k": k, "t": t, "beam": beam, "level_dbfs": -60.0, "common": sp[0], "scientific": sp[1], "confidence": conf, "provenance": "model_opinion"}
    if region is not None:
        r["in_region"] = region
    r.update(kw)
    return r


def tag(t, p=0.02, active=False):
    return {"k": "tags", "t": t, "beam": 1, "speech_p": p, "speech_active": active, "top": [["Bird", 0.5]], "emb_stored": True}


def ev_of(rows, taxa=None):
    return L.iter_events(rows, taxa)


# ---------------------------------------------------------------------------------------------- 1. event identity and clustering
e1 = ev_of([row(T0 + 0.125)])
check("event_id of a known row is exactly 1792940400125-melanerpes-carolinus", [e.event_id for e in e1] == ["1792940400125-melanerpes-carolinus"], [e.event_id for e in e1])
check("event_id key falls back to the common name when there is no scientific name",
      ev_of([row(T0 + 0.5, ("Engine", ""))])[0].event_id == "1792940400500-engine")
check("a gap of exactly 3.0 s merges (1 event, 2 reports, t_end = t + 3)", [(e.reports, e.t_end) for e in ev_of([row(T0), row(T0 + 3.0)])] == [(2, T0 + 3.0)])
check("a gap of 3.001 s splits (positive twin: 2 events)", len(ev_of([row(T0), row(T0 + 3.001)])) == 2)
bridge = ev_of([row(T0, conf=0.6), row(T0 + 2.5, conf=0.7, region=False, k="bird_filtered"), row(T0 + 5.0, conf=0.8)])
check("an out-of-region report bridges two in-region ones (dashboard rule): 1 event, 3 reports, in_region True, max conf 0.8",
      [(e.reports, e.in_region, e.max_conf) for e in bridge] == [(3, True, 0.8)], [(e.reports, e.in_region) for e in bridge])
check("positive twin: without the bridging report the same two in-region rows are 2 events", len(ev_of([row(T0, conf=0.6), row(T0 + 5.0, conf=0.8)])) == 2)
only_filtered = ev_of([row(T0, region=False, k="bird_filtered")])
check("a bird_filtered row alone is an event with in_region False; an unjudged row is None; a kept row is True",
      [only_filtered[0].in_region, ev_of([row(T0, region=None)])[0].in_region, ev_of([row(T0)])[0].in_region] == [False, None, True])
bad = [row(T0, beam=9), row(T0 + 10, conf=1.2), row(T0 + 20, sp=("", "")), row(T0 + 30, beam=True), row(T0 + 40, conf=1.0, beam=8)]
check("invalid rows (beam 9, conf 1.2, empty common, bool beam) are dropped; conf 1.0 beam 8 is kept (positive twin): 1 event",
      [(e.event_id.split("-")[0], e.best_beam) for e in ev_of(bad)] == [("1792940440000", 8)])
check("rows of two species 1 s apart are 2 events (species key separates)", len(ev_of([row(T0), row(T0 + 1, sp=NUT)])) == 2)
check("best_beam and best_conf follow the strongest report; beams is the set", [(e.best_beam, e.best_conf, sorted(e.beams)) for e in ev_of([row(T0, conf=0.5, beam=2), row(T0 + 1, conf=0.9, beam=5)])] == [(5, 0.9, [2, 5])])
check("the producer group is None when no row carries one (allowed) and the row's value when one does",
      [ev_of([row(T0)])[0].producer_group, ev_of([row(T0, group="bird")])[0].producer_group, ev_of([row(T0), row(T0 + 1, group="insect")])[0].producer_group] == [None, "bird", "insect"])

# ---------------------------------------------------------------------------------------------- 2. parity with the dashboard
KATY = ("Common Virtuoso Katydid", "Pterophylla camellifolia")
EMPTY_SCI = ("Engine", "")


def parity_rows():
    rs = [row(T0), row(T0 + 2.9, conf=0.9, beam=3), row(T0 + 3.001 + 2.9, beam=2), row(T0 + 100, sp=NUT, conf=0.51, beam=4),
          row(T0 + 101, sp=NUT, region=False, k="bird_filtered"), row(T0 + 103.5, sp=NUT, conf=0.88, beam=6),
          row(T0 + 200, sp=KATY, region=None, group="insect"), row(T0 + 201, sp=EMPTY_SCI, region=None, non_species=True),
          row(T0 + 300, beam=0), row(T0 + 301, conf=-0.1), row(T0 + 302, sp=("", "")),
          row(T0 + 400, sp=("Human vocal", ""), region=None), row(T0 + 402.9, sp=("Human vocal", ""), region=None),
          row(T0 + 500, region=False, k="bird_filtered"), row(T0 + 502, conf=0.9), row(T0 + 504.5, conf=0.4, beam=7)]
    return rs


def dashboard_events(rows):
    st = UI.DetStore(1e12, UI.load_taxa(TAXA))
    for r in rows:
        st.add(r)
    return st.snapshot()


def same_events(rows):
    dash = dashboard_events(rows)
    # the dashboard hides human-voice events (ui-http 1.6.0); the library keeps them so that it can refuse them (speech_label), so compare without them
    lib = [e for e in L.iter_events(rows, L.Taxa(TAXA)) if not (L.is_speech_label(e.common) or L.is_speech_label(e.scientific))]
    ids_d = [UI.App._event_id(e) for e in dash]
    ok = ids_d == [e.event_id for e in lib]
    fields = all((d.t_start, d.t_end, d.reports, round(d.max_conf, 6), sorted(d.beams), d.best_beam, d.in_region, d.group, d.non_species) ==
                 (m.t_start, m.t_end, m.reports, round(m.max_conf, 6), sorted(m.beams), m.best_beam, m.in_region, m.taxa_group if m.taxa_group != "other-bio" else d.group, m.non_species)
                 for d, m in zip(dash, lib))
    return ok, fields, len(dash), ids_d


ok, fields, n_dash, ids_d = same_events(parity_rows())
check("parity: the dashboard (DetStore) and the library give the same ids, in the same order, over a fixture with a bridging filtered row", ok, ids_d)
check("parity: the same t_start / t_end / reports / max_conf / beams / best_beam / in_region / group / non_species per event", fields)
check("parity fixture: the dashboard sees 6 events (2 Red-bellied: 5.901 splits; the Nuthatch and the 500 s Red-bellied bridge; invalid rows vanish; the human-voice event is hidden)", n_dash == 6, n_dash)
check("parity: every valid_bird verdict agrees (valid rows kept, invalid dropped)", [UI.valid_bird(r) for r in parity_rows()] == [bool(L.valid_bird(r)) for r in parity_rows()])
real = None
if "--real-birds" in sys.argv:
    real = sys.argv[sys.argv.index("--real-birds") + 1]
if real:
    rrows = L.read_rotated(real)
    okr, fr, nr, idr = same_events(rrows)
    check("parity on the REAL birds.jsonl (%d rows, %d events): identical id lists" % (len(rrows), nr), okr and nr > 1000, (nr, idr[:2]))
    check("parity on the REAL birds.jsonl: identical per-event fields", fr)
    print("INFO real parity: rows=%d events=%d" % (len(rrows), nr))

# ---------------------------------------------------------------------------------------------- 3. speech guard
def reason(rows_, tags_, **kw):
    return L.speech_reason(ev_of(rows_)[0], L.Tags(tags_), **kw)


quiet = [tag(T0 + 1.0)]
check("a Human vocal event is refused with speech_label", reason([row(T0, ("Human vocal", ""))], quiet) == "speech_label")
check("a Human non-vocal event is refused with speech_label", reason([row(T0, ("Human non-vocal", ""))], quiet) == "speech_label")
check("a PANNs Speech label is refused with speech_label", reason([row(T0, ("Speech", "Speech"))], quiet) == "speech_label")
check("positive twin: a bird label with the same quiet tags is allowed (None)", reason([row(T0)], quiet) is None)
check("speech_active 4.9 s after the event end is refused with speech_tag", reason([row(T0)], quiet + [tag(T0 + 4.9, 0.6, True)]) == "speech_tag")
check("speech_p exactly 0.15 (not active) is refused with speech_tag", reason([row(T0)], quiet + [tag(T0 + 2.0, 0.15)]) == "speech_tag")
check("positive twin: the nearest speech tag at 5.1 s with speech_p 0.14 is allowed", reason([row(T0)], quiet + [tag(T0 + 5.1, 0.14, False)]) is None)
check("positive twin: a speech_active tag at 5.1 s (outside the margin) is allowed", reason([row(T0)], quiet + [tag(T0 + 5.1, 0.9, True)]) is None)
check("speech_p 0.14 inside the margin is allowed (the cut is 0.15)", reason([row(T0)], quiet + [tag(T0 + 2.0, 0.14)]) is None)
check("a speech tag 4.9 s BEFORE the event start is refused (the margin is symmetric)", reason([row(T0)], [tag(T0 - 4.9, 0.7, True)]) == "speech_tag")
check("no tags row at all -> speech_unknown (fail closed)", reason([row(T0)], []) == "speech_unknown")
check("a tags row only 5.1 s away -> speech_unknown (nothing within the margin)", reason([row(T0)], [tag(T0 + 5.1)]) == "speech_unknown")
check("positive twin: --include-untagged lifts speech_unknown only (None)", reason([row(T0)], [], include_untagged=True) is None)
check("include_untagged does not lift a speech tag", reason([row(T0)], [tag(T0 + 1.0, 0.9, True)], include_untagged=True) == "speech_tag")
check("a wider --speech-margin-s catches the 5.1 s tag (positive twin of the 5.1 s allow)", reason([row(T0)], quiet + [tag(T0 + 5.1, 0.9, True)], margin_s=6.0) == "speech_tag")
check("the margin is measured from the END of a long event: a speech tag 4.9 s after t_end is refused",
      reason([row(T0), row(T0 + 2.0), row(T0 + 4.0)], quiet + [tag(T0 + 8.9, 0.9, True)]) == "speech_tag")
check("an event that is speech_label AND untagged reports speech_label (first failing reason)", reason([row(T0, ("Human vocal", ""))], []) == "speech_label")
# pins
plugins_src = open(os.path.join(HERE, "plugins.py"), encoding="utf-8").read()
tree = ast.parse(plugins_src)
panns = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SPEECH_LABELS" for t in n.targets))
check("pin: the guard's SPEECH_LABELS contains every entry of plugins.SPEECH_LABELS (%d entries)" % len(panns), set(panns) <= set(L.SPEECH_LABELS) and len(panns) == 14, sorted(set(panns) - set(L.SPEECH_LABELS)))
check("pin: the guard has no extra label beyond the tagger's list (so a rename shows here)", set(L.SPEECH_LABELS) == set(panns))
taxa_d = json.load(open(TAXA, encoding="utf-8"))
human_in_taxa = sorted(k for k in taxa_d["explicit_labels"] if k.startswith("human"))
check("pin: every explicit label of ui/taxa.json starting with Human is matched by the guard (%s)" % human_in_taxa,
      len(human_in_taxa) >= 3 and all(L.is_speech_label(k) for k in human_in_taxa) and L.is_speech_label("Human vocal") and L.is_speech_label("Human non-vocal"))
check("positive twin: Engine and Northern Cardinal are not speech labels", not L.is_speech_label("Engine") and not L.is_speech_label("Northern Cardinal"))
check("HUMAN_LABEL_PREFIXES is exactly ('human',)", L.HUMAN_LABEL_PREFIXES == ("human",))

# ---------------------------------------------------------------------------------------------- 4. records and the schema-equivalent validator
E0 = ev_of([row(T0 + 0.125, conf=0.78, beam=6)])[0]
GOOD = L.make_record(E0, "correct", "field", t_label=1793000000.5, label_id="0123456789abcdef")
check("make_record gives exactly the documented record", GOOD == {
    "k": "verdict", "v": 1, "label_id": "0123456789abcdef", "t_label": 1793000000.5, "event_id": "1792940400125-melanerpes-carolinus", "t_start_ms": 1792940400125,
    "species_key": "melanerpes carolinus", "verdict": "correct", "wrong_reason": None, "corrected_key": None, "basis": "field", "source": "cli",
    "stratum": None, "p_include": None, "queue_id": None, "model": {"conf_max": 0.78, "n_beams": 1, "best_beam": 6, "group": None, "in_region": True},
    "provenance": "owner_verdict"}, GOOD)
check("positive twin: the good record validates", L.validate_record(dict(GOOD)) == GOOD)


def bad_rec(**kw):
    r = json.loads(json.dumps(GOOD))
    for k, v in kw.items():
        if v is KeyError:
            r.pop(k)
        else:
            r[k] = v
    return r


check("validate: wrong without a wrong_reason is refused", raises(L.LabelError, "wrong_reason: required for verdict wrong", lambda: L.validate_record(bad_rec(verdict="wrong"))))
check("positive twin: wrong with a wrong_reason validates", L.validate_record(bad_rec(verdict="wrong", wrong_reason="mechanical"))["wrong_reason"] == "mechanical")
check("validate: correct with a wrong_reason is refused", raises(L.LabelError, "wrong_reason: only allowed with verdict wrong", lambda: L.validate_record(bad_rec(wrong_reason="mechanical"))))
check("validate: corrected_key with a reason other than other_species is refused",
      raises(L.LabelError, "corrected_key: only allowed with wrong_reason other_species", lambda: L.validate_record(bad_rec(verdict="wrong", wrong_reason="mechanical", corrected_key="sitta carolinensis"))))
check("positive twin: corrected_key with other_species validates", L.validate_record(bad_rec(verdict="wrong", wrong_reason="other_species", corrected_key="sitta carolinensis"))["corrected_key"] == "sitta carolinensis")
check("validate: provenance model_opinion is refused", raises(L.LabelError, "provenance: must be owner_verdict", lambda: L.validate_record(bad_rec(provenance="model_opinion"))))
check("validate: an unknown top-level key is refused", raises(L.LabelError, "mood: unknown top-level key", lambda: L.validate_record(bad_rec(mood="happy"))))
check("validate: a free-text note key is refused", raises(L.LabelError, "note: unknown top-level key", lambda: L.validate_record(bad_rec(note="sang"))))
check("validate: t_start_ms that disagrees with the event_id prefix is refused", raises(L.LabelError, "t_start_ms: does not match the event_id prefix", lambda: L.validate_record(bad_rec(t_start_ms=1792940400126))))
check("validate: verdict outside the vocabulary is refused", raises(L.LabelError, "verdict: must be one of correct, wrong, unsure, artifact", lambda: L.validate_record(bad_rec(verdict="maybe"))))
check("validate: p_include 0 is refused, 0.25 passes (twin)", raises(L.LabelError, "p_include", lambda: L.validate_record(bad_rec(p_include=0.0))) and L.validate_record(bad_rec(p_include=0.25))["p_include"] == 0.25)
check("validate: model.group null passes, a number is refused",
      L.validate_record(bad_rec(model=dict(GOOD["model"], group=None)))["model"]["group"] is None
      and raises(L.LabelError, "model.group: must be null or a string", lambda: L.validate_record(bad_rec(model=dict(GOOD["model"], group=3)))))
check("validate: a missing field is refused", raises(L.LabelError, "basis: missing", lambda: L.validate_record(bad_rec(basis=KeyError))))
check("validate: an event_id with an upper-case letter is refused", raises(L.LabelError, "event_id: must match", lambda: L.validate_record(bad_rec(event_id="1792940400125-Melanerpes"))))

# ---------------------------------------------------------------------------------------------- 5. fold
def rec(eid, t, verdict, label_id):
    return dict(GOOD, event_id=eid, t_label=t, verdict=verdict, label_id=label_id)


A = "1792940400125-a"
check("fold: two verdicts for one event, the later t_label wins", L.fold([rec(A, 1.0, "wrong", "a"), rec(A, 2.0, "correct", "b")])[A]["label_id"] == "b")
check("fold: with the t_labels reversed the other record wins (positive twin of the above)", L.fold([rec(A, 2.0, "correct", "b"), rec(A, 1.0, "wrong", "a")])[A]["label_id"] == "b")
check("fold: equal t_label, the later line wins", L.fold([rec(A, 5.0, "wrong", "a"), rec(A, 5.0, "correct", "b")])[A]["label_id"] == "b")
check("fold: equal t_label in the other line order, the later line (a) wins (twin)", L.fold([rec(A, 5.0, "correct", "b"), rec(A, 5.0, "wrong", "a")])[A]["label_id"] == "a")
check("fold: two events stay two entries; an unknown version and a record without t_label are ignored",
      set(L.fold([rec(A, 1.0, "wrong", "a"), rec("1-b", 1.0, "correct", "c"), dict(rec("1-c", 1.0, "correct", "d"), v=2), {"k": "verdict", "v": 1, "event_id": "1-d"}])) == {A, "1-b"})

# ---------------------------------------------------------------------------------------------- 6. the writer
tmp = tempfile.mkdtemp(prefix="labtest_")
store = L.LabelStore(os.path.join(tmp, "labels"))
n_bytes = store.append(GOOD)
raw = open(store.path, "rb").read()
check("writer: the dir is created lazily and the file holds exactly one LF-terminated compact line with no CR byte",
      raw == (L.dumps(GOOD) + "\n").encode() and b"\r" not in raw and raw.endswith(b"\n") and n_bytes == len(raw) and raw.count(b"\n") == 1, raw[:60])
check("writer: the lock file is gone after the append", not os.path.exists(store.path + ".lock"))
store.append(bad_rec(label_id="1111111111111111"))
check("writer: a second append adds exactly one line and the bytes of the first are unchanged", open(store.path, "rb").read().startswith(raw) and open(store.path, "rb").read().count(b"\n") == 2)
before = open(store.path, "rb").read()
check("writer: an invalid record raises LabelError and leaves the file bytes unchanged", raises(L.LabelError, "provenance", lambda: store.append(bad_rec(provenance="model_opinion"))) and open(store.path, "rb").read() == before)
# partial last line
pdir = os.path.join(tmp, "partial")
os.makedirs(pdir)
with open(os.path.join(pdir, "labels.jsonl"), "wb") as f:
    f.write((L.dumps(GOOD) + "\n").encode() + b'{"k":"verdict","v":1,"label_id":"dead')
pst = L.LabelStore(pdir)
_r, _bad, _part = pst.read()
check("reader: a trailing partial line is skipped and counted (1 record, 0 bad, 1 partial)", (len(_r), _bad, _part) == (1, 0, 1), (len(_r), _bad, _part))
pst.append(bad_rec(label_id="2222222222222222"))
_r, _bad, _part = pst.read()
check("writer after a partial line: the new record starts its own line; the partial one is now a bad line (2 records, 1 bad, 0 partial)", (len(_r), _bad, _part) == (2, 1, 0) and _r[1]["label_id"] == "2222222222222222", (len(_r), _bad, _part))
_r, _bad, _part = store.read()
check("positive twin: a clean file reads 2 records, 0 bad, 0 partial", (len(_r), _bad, _part) == (2, 0, 0))
cdir = os.path.join(tmp, "corrupt")
os.makedirs(cdir)
with open(os.path.join(cdir, "labels.jsonl"), "wb") as f:
    f.write(b"not json\n[1,2]\n" + (L.dumps(GOOD) + "\n").encode() + b"\n")
_r, _bad, _part = L.LabelStore(cdir).read()
check("reader: a non-JSON line and a non-object line are 2 bad lines, the good record survives, a blank line is ignored", (len(_r), _bad, _part) == (1, 2, 0))
check("reader: a missing file is empty (0, 0, 0)", L.LabelStore(os.path.join(tmp, "nope")).read() == ([], 0, 0))
# lock handling
ldir = os.path.join(tmp, "locked")
os.makedirs(ldir)
lst = L.LabelStore(ldir)
open(lst.path + ".lock", "wb").close()
lst.LOCK_WAIT_S = 0.2
check("writer: a held lock that is not stale makes the append fail with 'labels file busy' and write nothing", raises(L.LabelError, "labels file busy", lambda: lst.append(GOOD)) and not os.path.exists(lst.path))
old = os.path.getmtime(lst.path + ".lock") - 3600
os.utime(lst.path + ".lock", (old, old))
try:
    lst.append(GOOD)
    stale_ok = True
except L.LabelError:
    stale_ok = False
check("positive twin: a stale lock (1 h old) is taken over and the record is written, the lock removed", stale_ok and L.LabelStore(ldir).read()[0][0]["label_id"] == "0123456789abcdef" and not os.path.exists(lst.path + ".lock"))
# two processes x 500 appends
cdir2 = os.path.join(tmp, "concurrent")
child = os.path.join(tmp, "child.py")
with open(child, "w", encoding="utf-8", newline="\n") as f:
    f.write("import sys, json\nsys.path.insert(0, sys.argv[1])\nimport labels as L\nrec = json.loads(sys.argv[3])\nst = L.LabelStore(sys.argv[2])\n"
            "for i in range(500):\n    r = dict(rec, label_id=('%s%03d' % (sys.argv[4], i)).ljust(16, '0'))\n    st.append(r)\n")
procs = [subprocess.Popen([sys.executable, "-I", child, HERE, cdir2, json.dumps(GOOD), pre]) for pre in ("aa", "bb")]
codes = [p.wait(timeout=300) for p in procs]
data = open(os.path.join(cdir2, "labels.jsonl"), "rb").read()
lines = data.split(b"\n")
ids = [json.loads(l)["label_id"] for l in lines if l]
check("writer: two processes x 500 appends give exactly 1000 valid whole lines, 1000 distinct ids, no CR, no lock left",
      codes == [0, 0] and len(ids) == 1000 and len(set(ids)) == 1000 and lines[-1] == b"" and b"\r" not in data and not os.path.exists(os.path.join(cdir2, "labels.jsonl.lock")),
      (codes, len(ids), len(set(ids))))
check("writer: every one of the 1000 lines is byte-identical to the canonical dump of its own record", all(l.decode() == L.dumps(json.loads(l)) for l in lines if l))

# ---------------------------------------------------------------------------------------------- 7. ambient rule
am = L.Ambient([{"k": "ambient_temp", "t_hour_utc": 7200, "temp_c": 3.0}, {"k": "ambient_temp", "t_hour_utc": 10800, "temp_c": 4.0}, {"k": "ambient_temp", "t_hour_utc": 10800, "temp_c": 6.5}])
check("ambient: the event's own hour is used when present (a repeated hour: the last line wins, 6.5)", am.at(10800 + 100) == 6.5)
check("ambient: the nearest PAST hour is used when the own hour is missing (hour 3 -> hour 1's record is 2 h old -> 3.0)", L.Ambient([{"k": "ambient_temp", "t_hour_utc": 3600, "temp_c": 3.0}]).at(3 * 3600 + 5) == 3.0)
check("ambient: a record exactly 3 h old is used, one older is unknown (positive twin first)", L.Ambient([{"k": "ambient_temp", "t_hour_utc": 0, "temp_c": 1.0}]).at(3 * 3600 + 5) == 1.0
      and L.Ambient([{"k": "ambient_temp", "t_hour_utc": 0, "temp_c": 1.0}]).at(4 * 3600 + 5) is None)
check("ambient: a later hour is never used (event before every record -> unknown)", am.at(100) is None)


# ---------------------------------------------------------------------------------------------- 8. the CLI
def cli(args, data=None, cwd=None):
    p = subprocess.run([sys.executable, "-I", TOOL] + args, capture_output=True, cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def jl_write(path, recs):
    with open(path, "wb") as f:
        for r in recs:
            f.write((json.dumps(r, separators=(",", ":")) + "\n").encode())


def mkdata(name, birds, tags):
    d = os.path.join(tmp, name)
    os.makedirs(os.path.join(d, "analysis"))
    jl_write(os.path.join(d, "analysis", "birds.jsonl"), birds)
    if tags is not None:
        jl_write(os.path.join(d, "analysis", "tags.jsonl"), tags)
    return d


cb = [row(T0 + 10, conf=0.6), row(T0 + 110, ("Human vocal", ""), conf=0.8, region=None), row(T0 + 210, conf=0.7), row(T0 + 310, NUT, conf=0.6)]
ct = [tag(T0 + 10.5), tag(T0 + 110.5), tag(T0 + 213.0, 0.7, True), tag(T0 + 310.5)]      # event 3 has a speech tag; event 4 clean; event 2 is a Human label
dd = mkdata("cli", cb, ct)
ids_ok, ids_human, ids_tag, ids_nut = ("1792940410000-melanerpes-carolinus", "1792940510000-human-vocal", "1792940610000-melanerpes-carolinus", "1792940710000-sitta-carolinensis")
lab = os.path.join(tmp, "cli_labels")
base = ["--data-dir", dd, "--labels-dir", lab, "--taxa", TAXA]
rc_h, out_h, err_h = cli(base + ["add", "--event-id", ids_human, "--verdict", "correct", "--basis", "field"])
rc_t, out_t, err_t = cli(base + ["add", "--event-id", ids_tag, "--verdict", "correct", "--basis", "field"])
rc_n, out_n, err_n = cli(base + ["add", "--event-id", "1-nonexistent", "--verdict", "correct", "--basis", "field"])
check("add: a Human-label event, a speech-tag event and an unknown id are all refused with exit 2 and the IDENTICAL stderr bytes 'not_labelable'",
      (rc_h, rc_t, rc_n) == (2, 2, 2) and err_h == err_t == err_n == b"not_labelable\n" and out_h == out_t == out_n == b"", (rc_h, rc_t, rc_n, err_h, err_t, err_n))
check("add: no label dir or file was created by the refusals (the bytes cannot change)", not os.path.exists(lab))
check("add: the refusal never names the label", b"human" not in (err_h + out_h).lower() and b"speech" not in (err_t + out_t).lower())
rc_o, out_o, err_o = cli(["--now", "1793000000.5"] + base + ["add", "--event-id", ids_ok, "--verdict", "correct", "--basis", "field"])
check("add: positive twin, a clean event is written (exit 0), the printed record is the file's single line, no CR",
      rc_o == 0 and open(os.path.join(lab, "labels.jsonl"), "rb").read() == out_o and out_o.count(b"\n") == 1 and b"\r" not in out_o, (rc_o, err_o))
rec_o = json.loads(out_o)
check("add: the written record has the exact event id, t_start_ms, species_key, t_label and model snapshot",
      (rec_o["event_id"], rec_o["t_start_ms"], rec_o["species_key"], rec_o["t_label"], rec_o["model"], rec_o["stratum"], rec_o["p_include"], rec_o["queue_id"]) ==
      (ids_ok, 1792940410000, "melanerpes carolinus", 1793000000.5, {"conf_max": 0.6, "n_beams": 1, "best_beam": 1, "group": None, "in_region": True}, None, None, None), rec_o)
snap = open(os.path.join(lab, "labels.jsonl"), "rb").read()
rc_h2 = cli(base + ["add", "--event-id", ids_human, "--verdict", "wrong", "--wrong-reason", "mechanical", "--basis", "field"])[0]
check("add: refusing a speech event after a label exists leaves the file bytes unchanged", rc_h2 == 2 and open(os.path.join(lab, "labels.jsonl"), "rb").read() == snap)
dd_nt = mkdata("cli_notags", cb, None)
rc_u = cli(["--data-dir", dd_nt, "--labels-dir", os.path.join(tmp, "nt_labels"), "--taxa", TAXA, "add", "--event-id", ids_ok, "--verdict", "correct", "--basis", "field"])
rc_u2 = cli(["--data-dir", dd_nt, "--labels-dir", os.path.join(tmp, "nt_labels"), "--taxa", TAXA, "add", "--event-id", ids_ok, "--verdict", "correct", "--basis", "field", "--include-untagged"])
check("add: with no tags file the event is refused (exit 2, not_labelable); --include-untagged writes it (exit 0, positive twin)", (rc_u[0], rc_u[2], rc_u2[0]) == (2, b"not_labelable\n", 0), (rc_u[0], rc_u[2], rc_u2[0]))
rc_k = cli(base + ["add", "--event-id", ids_nut, "--verdict", "wrong", "--wrong-reason", "other_species", "--corrected-key", "no such species", "--basis", "field"])
rc_k2 = cli(base + ["add", "--event-id", ids_nut, "--verdict", "wrong", "--wrong-reason", "other_species", "--corrected-key", "melanerpes carolinus", "--basis", "field"])
check("add: a corrected_key that is not a species seen in birds.jsonl is refused (exit 2); a seen one is written (twin)", (rc_k[0], rc_k2[0]) == (2, 0) and json.loads(rc_k2[1])["corrected_key"] == "melanerpes carolinus", (rc_k[2], rc_k2[2]))
rc_w = cli(base + ["add", "--event-id", ids_nut, "--verdict", "wrong", "--basis", "field"])
check("add: wrong without --wrong-reason is refused (exit 2, message names wrong_reason)", rc_w[0] == 2 and b"wrong_reason: required for verdict wrong" in rc_w[2], rc_w[2])
rc_l, out_l, _ = cli(base + ["list"])
rc_f, out_f, _ = cli(base + ["fold"])
rc_s, out_s, _ = cli(base + ["stats"])
rc_v, out_v, err_v = cli(base + ["verify"])
stats = json.loads(out_s)
check("list prints the 2 records, fold the 2 events, stats counts verdicts, species and the null group rate exactly",
      rc_l == rc_f == rc_s == 0 and out_l.count(b"\n") == 2 and out_f.count(b"\n") == 2 and
      (stats["records"], stats["folded_events"], stats["bad_lines"], stats["partial_lines"], stats["verdicts"], stats["by_species"], stats["model_group_null"], stats["model_group_null_rate"]) ==
      (2, 2, 0, 0, {"correct": 1, "wrong": 1}, {"melanerpes carolinus": 1, "sitta carolinensis": 1}, 2, 1.0), stats)
check("verify: a clean file passes (exit 0, 2 records, 0 problems)", (rc_v, out_v) == (0, b"verified 2 record(s): 0 problem(s)\n"), (rc_v, out_v, err_v))
with open(os.path.join(lab, "labels.jsonl"), "ab") as f:
    f.write(b'{"k":"verdict","v":1}\n')
rc_v2, out_v2, err_v2 = cli(base + ["verify"])
check("verify: an appended incomplete record is a problem (exit 1, line 3 named)", rc_v2 == 1 and err_v2 == b"line 3: label_id: missing\n" and out_v2 == b"verified 3 record(s): 1 problem(s)\n", (rc_v2, err_v2, out_v2))
with open(os.path.join(lab, "labels.jsonl"), "ab") as f:
    f.write(b"\r\n")
check("verify: a CR byte is reported", cli(base + ["verify"])[2].startswith(b"file contains CR bytes"))
udir = os.path.join(tmp, "unreadable")
os.makedirs(os.path.join(udir, "labels.jsonl"))
rc_x = cli(["--data-dir", dd, "--labels-dir", udir, "--taxa", TAXA, "queue"])
check("queue: an unreadable labels file (a directory in its place) exits 2 and prints no queue", rc_x[0] == 2 and rc_x[1] == b"" and b"cannot read the labels file" in rc_x[2], rc_x)
out_dir = os.path.join(dd, "analysis", "q.json")
rc_a = cli(base + ["queue", "--out", out_dir])
rc_a2 = cli(base + ["--now", "1793000000", "queue", "--out", os.path.join(tmp, "q_ok.json")])
check("queue: --out under <data-dir>/analysis is refused (exit 2, nothing written); a scratch path works (exit 0, twin)",
      rc_a[0] == 2 and not os.path.exists(out_dir) and rc_a2[0] == 0 and os.path.exists(os.path.join(tmp, "q_ok.json")), (rc_a, rc_a2[2]))
lab_file = os.path.join(lab, "labels.jsonl")
snap_l = open(lab_file, "rb").read()
rc_o1 = cli(base + ["--now", "1793000500", "queue", "--out", lab_file])
check("queue: --out equal to the label store is refused (exit 2, the store bytes unchanged)",
      rc_o1[0] == 2 and b"labels dir" in rc_o1[2] and open(lab_file, "rb").read() == snap_l, rc_o1)
lab_other = os.path.join(lab, "q.json")
rc_o2 = cli(base + ["--now", "1793000500", "queue", "--out", lab_other])
check("queue: --out anywhere under the labels dir is refused (exit 2, nothing created)", rc_o2[0] == 2 and not os.path.exists(lab_other), rc_o2)
if os.path.normcase("A") != "A":
    alias = os.path.join(dd, "ANALYSIS", "x.json")
else:
    os.symlink(os.path.join(dd, "analysis"), os.path.join(tmp, "alias_analysis"))
    alias = os.path.join(tmp, "alias_analysis", "x.json")
rc_o3 = cli(base + ["--now", "1793000500", "queue", "--out", alias])
check("queue: --out reaching analysis/ by case (Windows) or a symlink (POSIX) is refused (exit 2, nothing written)",
      rc_o3[0] == 2 and not os.path.exists(os.path.join(dd, "analysis", "x.json")), rc_o3)
hist_dir = os.path.join(dd, "history")
rc_o4 = cli(["--data-dir", dd, "--labels-dir", hist_dir, "--taxa", TAXA, "--now", "1793000500", "add", "--event-id", ids_ok, "--verdict", "correct", "--basis", "field"])
check("add: --labels-dir pointing at <data-dir>/history is refused (exit 2, nothing created)",
      rc_o4[0] == 2 and b"history/" in rc_o4[2] and not os.path.exists(hist_dir), rc_o4)
rc_o5 = cli(base + ["--now", "1793000500", "queue", "--out", os.path.join(tmp, "no_such_dir", "q.json")])
check("queue: --out in a missing directory exits 2 with a message, not a traceback; the existing-dir twin exits 0",
      rc_o5[0] == 2 and rc_o5[1] == b"" and rc_o5[2] == b"cannot write --out: FileNotFoundError\n" and rc_a2[0] == 0, rc_o5)
snap_k = open(lab_file, "rb").read()
rc_hk = cli(base + ["add", "--event-id", ids_nut, "--verdict", "wrong", "--wrong-reason", "other_species", "--corrected-key", "human vocal", "--basis", "field"])
check("add: a Human/speech --corrected-key is refused with the single not_labelable (exit 2, store bytes unchanged); a normal species key is accepted (twin rc_k2)",
      (rc_hk[0], rc_hk[1], rc_hk[2]) == (2, b"", b"not_labelable\n") and open(lab_file, "rb").read() == snap_k and rc_k2[0] == 0, rc_hk)
check("validate: corrected_key 'human vocal' is refused by the record validator, 'sitta carolinensis' passes (twin)",
      raises(L.LabelError, "corrected_key: not labelable", lambda: L.validate_record(bad_rec(verdict="wrong", wrong_reason="other_species", corrected_key="human vocal")))
      and L.validate_record(bad_rec(verdict="wrong", wrong_reason="other_species", corrected_key="sitta carolinensis"))["corrected_key"] == "sitta carolinensis")
qd = json.load(open(os.path.join(tmp, "q_ok.json")))
qr = json.loads(cli(["--now", "1793000000"] + base + ["queue", "--relabel"])[1])
check("queue over the CLI data: the 2 labelled events are already_labelled (0 eligible, no items); with --relabel both are queued (twin); 1 speech_label and 1 speech_tag either way",
      (qd["eligible"]["events"], qd["excluded"]["speech_label"], qd["excluded"]["speech_tag"], qd["excluded"]["already_labelled"], qd["excluded"]["region"]) == (0, 1, 1, 2, 0)
      and qd["items"] == [] and (qr["eligible"]["events"], qr["excluded"]["already_labelled"]) == (2, 0)
      and sorted(i["event_id"] for i in qr["items"]) == sorted([ids_ok, ids_nut]), (qd["eligible"], qd["excluded"], [i["event_id"] for i in qd["items"]]))
dd_empty = mkdata("cli_empty", [], [])
rc_e = cli(["--now", "1793000000", "--data-dir", dd_empty, "--labels-dir", os.path.join(tmp, "e_labels"), "--taxa", TAXA, "queue"])
check("queue: an empty birds.jsonl gives exit 0 and zero items", rc_e[0] == 0 and json.loads(rc_e[1])["items"] == [] and json.loads(rc_e[1])["eligible"]["events"] == 0)
dd_nobirds = os.path.join(tmp, "cli_nobirds")
os.makedirs(os.path.join(dd_nobirds, "analysis"))
rc_nb = cli(["--now", "1793000000", "--data-dir", dd_nobirds, "--labels-dir", os.path.join(tmp, "nb_labels"), "--taxa", TAXA, "queue"])
rc_nb2 = cli(["--data-dir", dd_nobirds, "--labels-dir", os.path.join(tmp, "nb_labels"), "--taxa", TAXA, "add", "--event-id", ids_ok, "--verdict", "correct", "--basis", "field"])
check("a missing birds.jsonl: queue exits 0 with no items, add exits 2 (not_labelable)", rc_nb[0] == 0 and json.loads(rc_nb[1])["items"] == [] and rc_nb2[0] == 2 and rc_nb2[2] == b"not_labelable\n")
dd_nt2 = mkdata("cli_nt2", cb, None)
rc_q = cli(["--now", str(T0 + 1000), "--data-dir", dd_nt2, "--labels-dir", os.path.join(tmp, "nt2_labels"), "--taxa", TAXA, "queue"])
rc_q2 = cli(["--now", str(T0 + 1000), "--data-dir", dd_nt2, "--labels-dir", os.path.join(tmp, "nt2_labels"), "--taxa", TAXA, "queue", "--include-untagged"])
qa, qb = json.loads(rc_q[1]), json.loads(rc_q2[1])
check("queue with no tags file: every non-speech-label event is speech_unknown, nothing queued, stderr says why; --include-untagged queues them (twin)",
      qa["items"] == [] and qa["excluded"]["speech_unknown"] == 3 and qa["excluded"]["speech_label"] == 1 and b"fail closed" in rc_q[2]
      and sorted(i["event_id"] for i in qb["items"]) == sorted([ids_ok, ids_tag, ids_nut]), (qa["excluded"], rc_q[2]))
q_a = cli(["--now", str(T0 + 1000), "--data-dir", dd, "--labels-dir", os.path.join(tmp, "det1"), "--taxa", TAXA, "queue", "--seed", "3"])[1]
q_b = cli(["--now", str(T0 + 1000), "--data-dir", dd, "--labels-dir", os.path.join(tmp, "det1"), "--taxa", TAXA, "queue", "--seed", "3"])[1]
check("queue: two runs with the same inputs, --now and seed give byte-identical output with LF only", q_a == q_b and b"\r" not in q_a and q_a.endswith(b"\n") and len(q_a) > 200)
check("queue --insect-mode only with an unusable taxa table exits 2 (cannot tell insects)",
      cli(["--now", "1793000000", "--data-dir", dd, "--labels-dir", os.path.join(tmp, "det1"), "--taxa", os.path.join(tmp, "missing_taxa.json"), "queue", "--insect-mode", "only"])[0] == 2)
rc_bp = cli(["--now", "1793000000", "--data-dir", dd, "--labels-dir", os.path.join(tmp, "det1"), "--taxa", TAXA, "queue", "--near-lo", "0.9", "--near-hi", "0.5"])
check("queue: near-lo above near-hi exits 2 and prints no queue", rc_bp[0] == 2 and rc_bp[1] == b"" and b"near_lo" in rc_bp[2], rc_bp)
src_files = [open(p_, "rb").read() for p_ in (os.path.join(HERE, "labels.py"), os.path.join(HERE, "review_queue.py"), TOOL, os.path.join(HERE, "test_labels.py"))]
check("the new sources are LF only", all(b"\r" not in s_ for s_ in src_files))
check("policy: the sources never name the novelty file or an emb_ file, and import no network module",
      all(w not in open(p_, encoding="utf-8").read() for p_ in (os.path.join(HERE, "labels.py"), os.path.join(HERE, "review_queue.py"), TOOL)
          for w in ("novelty.jsonl", "emb_2", ".f16", "import socket", "urllib", "http.client", "import requests")))
check("parity twin: the library still builds the human-voice event the dashboard hides (it refuses it later, speech_label): 7 events vs the dashboard's 6",
      len(L.iter_events(parity_rows(), L.Taxa(TAXA))) == 7)
shutil.rmtree(tmp, ignore_errors=True)
print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
for f_ in fails:
    print("  FAILED:", f_)
sys.exit(1 if fails else 0)
