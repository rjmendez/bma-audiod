#!/usr/bin/env python3
"""Conformance tests for the BMA interface contracts. Hermetic: only the committed fixtures, schemas and production code.
Run with a venv that has jsonschema and numpy:  python -I test_contracts.py

Style (as in analysis/test_analysis.py): exact values, a positive twin for every negative case, mutation proofs, known
defects pinned by expected failures (XFAIL: must fail today; if one starts passing the run fails so the pin is removed)."""
import copy, json, os, shutil, struct, sys, tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
PC = os.path.join(BMA, "pc")
for p in (HERE, PC, os.path.join(BMA, "classify"), os.path.join(BMA, "tools")):
    sys.path.insert(0, p)
import check_contracts as C  # noqa: E402
import ref_binary as R  # noqa: E402
import bma_cable_sink as S  # noqa: E402
import bma_cable_decode as D  # noqa: E402
import dante_bridge as B  # noqa: E402
import bma_classify as CL  # noqa: E402

fails, n_pass, n_xfail, n_skip = [], 0, 0, 0


def _raises(fn):
    """-> the ContractError code fn raises, or None."""
    try:
        fn()
    except Exception as e:           # noqa: BLE001
        return getattr(e, "code", type(e).__name__)
    return None
HAS_BANDS = hasattr(S, "BandMeter") and hasattr(S, "feed_bands")
FIX = os.path.join(HERE, "fixtures")
SCH = C.load_schemas(os.path.join(HERE, "schemas"))


def check(name, ok, detail=""):
    global n_pass
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if ok:
        n_pass += 1
    else:
        fails.append(name)


def skip(name, why):
    global n_skip
    n_skip += 1
    print(f"SKIP {name}  [{why}]")


def xfail(name, conforming, why):
    """`conforming` is what the contract demands; it is False today because of the known defect `why`."""
    global n_xfail
    if conforming:
        print(f"FAIL {name}  [expected failure now passes: remove this xfail and the CHANGELOG entry]")
        fails.append(name)
    else:
        n_xfail += 1
        print(f"XFAIL {name}  [{why}]")


def jl(name):
    return [json.loads(l) for l in open(os.path.join(FIX, name), encoding="utf-8") if l.strip()]


def jf(name):
    return json.load(open(os.path.join(FIX, name), encoding="utf-8"))


def first(name, field, kind, **match):
    return next(r for r in jl(name) if r.get(field) == kind and all(r.get(k) == v for k, v in match.items()))


def drifts(schema_name, rec, file):
    return C.check_record(file, "1", rec, SCH[schema_name], SCH[schema_name].get("x-files", {}).get(file), {})


def whole(schema_name, rec):
    return [(e.json_path, e.message) for e in C.errors(C.whole_validator(SCH[schema_name]), rec)]


def rejected(drs, path, part):
    return any(d.path == path and part in d.message for d in drs)


def reject_pair(name, schema_name, file, good, bad, path, part):
    """negative: `bad` is rejected at `path` with `part` in the message; positive twin: `good` has no finding."""
    check(f"{name}: rejected at {path} ({part!r})", rejected(drifts(schema_name, bad, file), path, part), str([str(d) for d in drifts(schema_name, bad, file)][:2]))
    check(f"{name}: positive twin passes", drifts(schema_name, good, file) == [], str([str(d) for d in drifts(schema_name, good, file)][:2]))


def whole_pair(name, schema_name, good, bad, path, part):
    errs = whole(schema_name, bad)
    check(f"{name}: rejected at {path} ({part!r})", any(p == path and part in m for p, m in errs), str(errs[:2]))
    check(f"{name}: positive twin passes", whole(schema_name, good) == [], str(whole(schema_name, good)[:2]))


def mod(rec, **kw):
    r = copy.deepcopy(rec)
    for k, v in kw.items():
        if v is KeyError:
            r.pop(k)
        else:
            r[k] = v
    return r


# ============================================================================================ hermetic run
n, dr, _ = C.run(HERE)
check("hermetic run: specs, schemas, versions, changelog, fixtures and binary examples are all clean", dr == [], str([str(d) for d in dr][:3]))
check("specs/analysis-outputs.md exists and is a recognised spec", not any(d.file == "analysis-outputs.md" for d in dr))
check("hermetic run: fixtures cover 96+ records", n >= 96, str(n))
vers = {k: SCH[k]["x-contract-version"] for k in SCH}
# the plugin interfaces (analysis/plugins.manifest.json) are not pinned here: the manifest's version must equal the schema's, so a bump needs
# a manifest edit (the check below), and a new plugin needs no edit of this line; the five plugin interfaces that exist today stay pinned
# to their exact version (1.0.0) as on master, so a schema and its manifest entry cannot be bumped together without editing this line
_plug_vers = {n_: c_["version"] for n_, c_ in C.PLUGIN_CONTRACTS.items()}
check("schema versions: sink-status 1.2.0, analysis-records 1.3.0, analysis-status 2.0.0, ui-http 1.13.0, analysis-history 1.1.0, clips 1.3.0, ambient-temp 1.1.0, loss-log 1.0.1, perch-report 1.0.0, active-coherence-report 1.0.0, eval-report 1.0.0, event-model 1.0.0, beam-agreement 1.1.0, verdict-labels 1.1.0, ui-live 1.0.0, the other non-plugin interfaces 1.0.0; every plugin interface (" + ", ".join(sorted(_plug_vers)) + ") has the version its manifest entry declares",
      {k: v for k, v in vers.items() if k not in _plug_vers} == {"bridge-status": "1.0.0", "sink-status": "1.2.0", "board-debug": "1.0.0", "analysis-records": "1.3.0",
               "analysis-status": "2.0.0", "beam-bands": "1.0.0", "ui-http": "1.13.0", "ui-live": "1.0.0", "run-history": "1.0.0", "analysis-history": "1.1.0", "clips": "1.3.0", "ambient-temp": "1.1.0", "loss-log": "1.0.1", "perch-report": "1.0.0", "active-coherence-report": "1.0.0", "eval-report": "1.0.0", "event-model": "1.0.0", "artifact-gate": "1.0.0", "beam-agreement": "1.1.0", "verdict-labels": "1.1.0", "burst-scorecard": "1.0.0", "beam-response-map": "1.0.0"}
      and {k: vers[k] for k in _plug_vers} == _plug_vers
      and {k: vers.get(k) for k in ("beam-coherence", "perch", "mixscore", "burst-stats", "acoustic-indices")} == {"beam-coherence": "1.0.0", "perch": "1.1.0", "mixscore": "1.0.0", "burst-stats": "1.0.0", "acoustic-indices": "1.0.0"}, str(vers))
_spec_head = open(os.path.join(HERE, "specs", "analysis-outputs.md"), encoding="utf-8").read(600)
check("specs/analysis-outputs.md header says contract_version 2.0.1 (the audio exception) and still names analysis-records 1.3.0 (its fields are described there)",
      "contract_version: 2.0.1" in _spec_head and "analysis-records 1.3.0" in _spec_head and SCH["analysis-records"]["x-contract-version"] == "1.3.0", _spec_head[:200])
# ============================================================================================ ui-http fixtures
nui, dui = C.check_ui_fixtures(os.path.join(HERE, "fixtures"), SCH)
check("ui-http: the 22 example files (21 responses + rollup lines) validate, 24 records", (nui, dui) == (24, []), f"{nui} {[str(d) for d in dui][:3]}")
_tmp = tempfile.mkdtemp()
try:
    for fn in C.UI_FIXTURES:
        shutil.copy(os.path.join(HERE, "fixtures", fn), _tmp)
    _p = os.path.join(_tmp, "ui_beams_live.json")
    _j = json.load(open(_p))
    _j["bands"][3]["band"] = 11
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: a drifted example (band 11) is reported with its json path and message", [(d.file, d.path) for d in _d] == [("ui_beams_live.json", "$.bands[3].band")] and "maximum of 7" in _d[0].message, str([str(d) for d in _d]))
    json.dump(json.load(open(os.path.join(HERE, "fixtures", "ui_beams_live.json"))), open(_p, "w"))                      # restore the live example
    _p = os.path.join(_tmp, "ui_density.json")
    _j = json.load(open(_p))
    _j["rows"][0]["group"] = "fish"
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: a density example with a group outside the enum is reported at its json path", [(d.file, d.path) for d in _d] == [("ui_density.json", "$.rows[0].group")], str([str(d) for d in _d]))
    shutil.copy(os.path.join(HERE, "fixtures", "ui_density.json"), _tmp)
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    _p = os.path.join(_tmp, "ui_problem_401.json")
    _j = json.load(open(_p))
    _j["code"] = "invalid_parameter"
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: a 401 problem whose code is not `unauthorized` is reported at its json path", [(d.file, d.path) for d in _d] == [("ui_problem_401.json", "$.code")] and "unauthorized" in _d[0].message, str([str(d) for d in _d]))
    shutil.copy(os.path.join(HERE, "fixtures", "ui_problem_401.json"), _tmp)
    _p = os.path.join(_tmp, "ui_problem_429.json")
    _j = json.load(open(_p))
    _j["status"] = 429
    _j["code"] = "unauthorized"
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: a 429 problem whose code is not `too_many_attempts` is reported at its json path", [(d.file, d.path) for d in _d] == [("ui_problem_429.json", "$.code")], str([str(d) for d in _d]))
    shutil.copy(os.path.join(HERE, "fixtures", "ui_problem_429.json"), _tmp)
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: positive twin - the untouched 401 and 429 examples pass (exact content: status, code, type)", _d == []
          and json.load(open(os.path.join(HERE, "fixtures", "ui_problem_401.json")))["code"] == "unauthorized"
          and json.load(open(os.path.join(HERE, "fixtures", "ui_problem_429.json")))["status"] == 429, str([str(d) for d in _d]))
    check("ui-http: positive twin - the untouched density example passes alongside the other examples", _d == [], str([str(d) for d in _d]))
    _p = os.path.join(_tmp, "ui_review_next.json")
    _j = json.load(open(_p))
    _j["item"]["reason"] = "species_cap"
    json.dump(_j, open(_p, "w"))
    _p2 = os.path.join(_tmp, "ui_review_stats.json")
    _j2 = json.load(open(_p2))
    _j2["verdicts"]["maybe"] = 1
    json.dump(_j2, open(_p2, "w"))
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: a review item with a reason outside near|disagree|low|audit and a stats verdict outside the vocabulary are reported at their json paths",
          sorted((d.file, d.path) for d in _d) == [("ui_review_next.json", "$.item.reason"), ("ui_review_stats.json", "$.verdicts")], str([str(d) for d in _d]))
    for _f in ("ui_review_next.json", "ui_review_stats.json"):
        shutil.copy(os.path.join(HERE, "fixtures", _f), _tmp)
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: positive twin - the untouched review examples pass (reason near, 4 verdict keys)", _d == []
          and json.load(open(os.path.join(HERE, "fixtures", "ui_review_next.json")))["item"]["reason"] == "near"
          and sorted(json.load(open(os.path.join(HERE, "fixtures", "ui_review_stats.json")))["verdicts"]) == ["artifact", "correct", "unsure", "wrong"], str([str(d) for d in _d]))
    _p = os.path.join(_tmp, "ui_review_next.json")
    _j = json.load(open(_p))
    _j["item"]["clip_audio"] = "/api/review/audio?id=../../x"
    json.dump(_j, open(_p, "w"))
    _p2 = os.path.join(_tmp, "ui_review_stats.json")
    _j2 = json.load(open(_p2))
    del _j2["no_clip"]
    json.dump(_j2, open(_p2, "w"))
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http 1.9.0: a clip_audio url outside the clip-id pattern and a stats document without no_clip are reported at their json paths",
          sorted((d.file, d.path) for d in _d) == [("ui_review_next.json", "$.item.clip_audio"), ("ui_review_stats.json", "$")], str([str(d) for d in _d]))
    for _f in ("ui_review_next.json", "ui_review_stats.json"):
        shutil.copy(os.path.join(HERE, "fixtures", _f), _tmp)
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    _fx = json.load(open(os.path.join(HERE, "fixtures", "ui_review_next.json")))["item"]
    check("ui-http 1.9.0: positive twin - the untouched review examples pass (clip_audio url, 4 span keys, no_clip 112)", _d == []
          and _fx["clip_audio"] == "/api/review/audio?id=1791997838000-poecilecarolinensis-b1" and sorted(_fx["clip_span"]) == ["clip_end_s", "clip_start_s", "event_end_s", "event_start_s"]
          and json.load(open(os.path.join(HERE, "fixtures", "ui_review_stats.json")))["no_clip"] == 112, str([str(d) for d in _d]))
    _p = os.path.join(_tmp, "ui_healthz.json")
    _bad = []
    for _doc in ({"ok": True, "ts": 1, "version": "1.10.0"}, {"ok": False, "ts": 1}, {"ok": True, "ts": 1.5}):
        json.dump(_doc, open(_p, "w"))
        _bad.append(sorted((d.file, d.path) for d in C.check_ui_fixtures(_tmp, SCH)[1]))
    shutil.copy(os.path.join(HERE, "fixtures", "ui_healthz.json"), _tmp)
    check("ui-http 1.10.0: healthz rejects an extra field, ok false and a float ts (each at its path); positive twin - the shipped example passes",
          _bad == [[("ui_healthz.json", "$")], [("ui_healthz.json", "$.ok")], [("ui_healthz.json", "$.ts")]] and C.check_ui_fixtures(_tmp, SCH)[1] == [], str(_bad))
    _p = os.path.join(_tmp, "ui_rollup.jsonl")
    _lines = [l for l in open(_p, "rb").read().splitlines() if b'"kind":"stream"' not in l]
    open(_p, "wb").write(b"\n".join(_lines) + b"\n")
    _n, _d = C.check_ui_fixtures(_tmp, SCH)
    check("ui-http: a rollup fixture without a stream line is reported", [str(d) for d in _d if "stream" in d.path or "`stream`" in d.message] != [], str([str(d) for d in _d]))
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# ---- ui-http 1.11.0: the advisory artifact_flags on events (artifact-gate phase 2)
def _artifact_def_normal(d):
    """a $defs.artifact_flags with its two enum refs inlined, so the copy in ui-http and the original in artifact-gate compare by value"""
    d = json.loads(json.dumps(d).replace("#/$defs/artifact_reason", "#/$defs/reason").replace("#/$defs/artifact_level", "#/$defs/level"))
    d.pop("description", None)
    d["properties"]["level"].pop("description", None)
    return d


check("ui-http 1.11.0: $defs.artifact_flags is the artifact-gate contract's object (same required keys, evidence keys and enums)",
      _artifact_def_normal(SCH["ui-http"]["$defs"]["artifact_flags"]) == _artifact_def_normal(SCH["artifact-gate"]["$defs"]["artifact_flags"])
      and SCH["ui-http"]["$defs"]["artifact_reason"] == SCH["artifact-gate"]["$defs"]["reason"] and SCH["ui-http"]["$defs"]["artifact_level"] == SCH["artifact-gate"]["$defs"]["level"],
      "the two definitions drifted apart")
_fx_ev = json.load(open(os.path.join(HERE, "fixtures", "ui_events.json")))
_fx_flags = [e["artifact_flags"] for e in _fx_ev["events"]]
check("ui-http 1.11.0: the events example is the insect view (artifact echo 'any'); its three katydid events are cold_insect at 2.3 C / floor 5.0 with burst counts 8, 15 and 22 (22 reaches the burst flag: 20 transients, spread 1.5 dB)",
      _fx_ev["artifact"] == "any" and _fx_ev["group"] == "insect" and [(f["flags"], f["level"], f["evidence"]["burst_n"], f["evidence"]["temp_c"], f["evidence"]["floor_c"]) for f in _fx_flags]
      == [(["cold_insect"], "likely", 8, 2.3, 5.0), (["cold_insect"], "likely", 15, 2.3, 5.0), (["burst_coincident", "cold_insect"], "likely", 22, 2.3, 5.0)],
      str(_fx_flags)[:300])
_tmp2 = tempfile.mkdtemp()
try:
    for fn in C.UI_FIXTURES:
        shutil.copy(os.path.join(HERE, "fixtures", fn), _tmp2)
    _n, _d = C.check_ui_fixtures(_tmp2, SCH)
    check("ui-http 1.11.0: positive twin - the untouched examples (with artifact_flags) validate", _d == [], str([str(d) for d in _d]))
    _p = os.path.join(_tmp2, "ui_events.json")
    _j = json.load(open(_p))
    _j["events"][0]["artifact_flags"]["flags"] = ["warm_insect"]
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp2, SCH)
    check("ui-http 1.11.0: an artifact reason outside the closed set is reported at its json path", [(d.file, d.path) for d in _d] == [("ui_events.json", "$.events[0].artifact_flags.flags[0]")], str([str(d) for d in _d]))
    shutil.copy(os.path.join(HERE, "fixtures", "ui_events.json"), _tmp2)
    _p = os.path.join(_tmp2, "ui_events.json")
    _j = json.load(open(_p))
    _j["events"][0]["artifact_flags"]["evidence"]["lat"] = 40.0
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp2, SCH)
    check("ui-http 1.11.0: a coordinate smuggled into the evidence is rejected (closed object)", [(d.file, d.path) for d in _d] == [("ui_events.json", "$.events[0].artifact_flags.evidence")], str([str(d) for d in _d]))
    shutil.copy(os.path.join(HERE, "fixtures", "ui_events.json"), _tmp2)
    _p = os.path.join(_tmp2, "ui_species.json")
    _j = json.load(open(_p))
    del _j["artifact_events_total"]
    json.dump(_j, open(_p, "w"))
    _n, _d = C.check_ui_fixtures(_tmp2, SCH)
    check("ui-http 1.11.0: a species response without artifact_events_total is reported (the hidden count is always served)", [(d.file, d.path) for d in _d] == [("ui_species.json", "$")] and "artifact_events_total" in _d[0].message, str([str(d) for d in _d]))
finally:
    shutil.rmtree(_tmp2, ignore_errors=True)

_cs_spec = open(os.path.join(HERE, "specs", "cable-stream.md"), encoding="utf-8").read()
check("cable-stream spec is at 2.0.0, mentions kind 4, the (u - 2048) << 8 symbols, gcd 32, the 0.985 gain and 750000 of 750000",
      "contract_version: 2.0.0" in _cs_spec[:600] and "4 beam bands" in _cs_spec and "(u - 2048) << 8" in _cs_spec and "gcd 32" in _cs_spec
      and "0.985" in _cs_spec and "750000 of 750000" in _cs_spec)
check("cable-stream 2.0.0 has a changelog entry that says BREAKING",
      "cable-stream 2.0.0 - the index track" in open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8").read()
      and "(BREAKING; wire format of channels 9-13" in open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8").read())

# ============================================================================================ JSON: bridge / sink / analysis status
bs = jf("bridge_status.json")
whole_pair("bridge_status state", "bridge-status", bs, mod(bs, state="BAD"), "$.state", "is not one of")
whole_pair("bridge_status missing gaps", "bridge-status", bs, mod(bs, gaps=KeyError), "$", "'gaps' is a required property")
whole_pair("bridge_status loss over 100", "bridge-status", mod(bs, loss_pct=100.0), mod(bs, loss_pct=101.0), "$.loss_pct", "greater than the maximum of 100")
whole_pair("bridge_status pps type", "bridge-status", bs, mod(bs, pps="3000"), "$.pps", "is not of type 'number'")
whole_pair("bridge_status target without port", "bridge-status", bs, mod(bs, targets=["192.0.2.10"]), "$.targets[0]", "does not match")
whole_pair("bridge_status channel 9", "bridge-status", mod(bs, channels=[8]), mod(bs, channels=[9]), "$.channels[0]", "greater than the maximum of 8")
sk = jf("sink_status.json")
whole_pair("sink_status playing type", "sink-status", sk, mod(sk, playing="yes"), "$.playing", "is not of type 'boolean'")
whole_pair("sink_status negative underruns", "sink-status", mod(sk, underruns=0), mod(sk, underruns=-1), "$.underruns", "less than the minimum of 0")
st = jf("analysis_status.json")
whole_pair("status.json device", "analysis-status", mod(st, device="cuda:0"), mod(st, device="gpu0"), "$.device", "does not match")
bad_plugins = copy.deepcopy(st)
bad_plugins["plugins"]["dsp"].pop("errors")
errs = whole("analysis-status", bad_plugins)
check("status.json: a running plugin without `errors` is rejected", any(p == "$.plugins.dsp" for p, m in errs), str(errs[:2]))
off = copy.deepcopy(st)
off["plugins"]["clap"] = {"enabled": False, "reason": "ModuleNotFoundError: No module named 'torch'"}
check("status.json: an off plugin with a reason is valid", whole("analysis-status", off) == [], str(whole("analysis-status", off)[:2]))

# ============================================================================================ JSON: board_debug
BD = "board_debug.jsonl"
meter = first(BD, "type", "meter")
reject_pair("meter value of 3 hex digits", "board-debug", BD, meter, mod(meter, vals={**meter["vals"], "76": "abc"}), "$.vals['76']", "does not match")
reject_pair("meter selector must be 2 hex digits", "board-debug", BD, meter, mod(meter, vals={"7": "0000"}), "$.vals", "does not match")
mon = first(BD, "type", "monitor", slot=3)
reject_pair("monitor dsp 3 is outside 4..9", "board-debug", BD, mod(mon, dsp=4), mod(mon, dsp=3), "$.dsp", "less than the minimum of 4")
reject_pair("monitor peak_dbfs above 0", "board-debug", BD, mon, mod(mon, peak_dbfs=3.0), "$.peak_dbfs", "greater than the maximum")
mon4 = first(BD, "type", "monitor", slot=4)
check("monitor slot 4 fixture has rms_dbfs null (not an audio channel)", mon4["rms_dbfs"] is None)
reject_pair("monitor slot 4 must have rms_dbfs null", "board-debug", BD, mon4, mod(mon4, rms_dbfs=-70.1), "$.rms_dbfs", "is not of type 'null'")
raw = first(BD, "type", "monitor", line="garbled line")
reject_pair("raw monitor shape with a stray `dsp` becomes the parsed shape and needs all its fields", "board-debug", BD, raw, mod(raw, dsp=5), "$", "'t' is a required property")
hl = next(r for r in jl(BD) if r.get("type") == "health" and r["temp_local_hex"] != "")
reject_pair("health eth0 must have 5 counters", "board-debug", BD, hl, mod(hl, eth0="1,2,3"), "$.eth0", "does not match")
fp = copy.deepcopy(hl)
fp["fpga"].pop("ea")
check("health: missing FPGA register ea is rejected at $.fpga", rejected(drifts("board-debug", fp, BD), "$.fpga", "'ea' is a required property"))
reject_pair("health k and type must both be health", "board-debug", BD, hl, mod(hl, k="meter"), "$.k", "'health' was expected")
reject_pair("health load format", "board-debug", BD, hl, mod(hl, load="0.5"), "$.load", "does not match")
empty = next(r for r in jl(BD) if r.get("type") == "health" and r["temp_local_hex"] == "")
check("health with empty temperature hex and null temps is valid (2026-10-11 records)", drifts("board-debug", empty, BD) == [])
unp = first(BD, "type", "health_unparsed")
reject_pair("health_unparsed line must start with @health", "board-debug", BD, unp, mod(unp, line="garbage"), "$.line", "does not match")
check("health_unparsed fixture keeps the NUL run", "\x00" * 8 in unp["line"])
stt = first(BD, "type", "status", json="{\"state\":\"starting\"}")
inner_ok = [r for r in jl(BD) if r.get("type") == "status" and "meters" in r["json"]][0]
check("status {\"state\":\"starting\"} is valid", drifts("board-debug", stt, BD) == [])
check("status with the full inner object is valid", drifts("board-debug", inner_ok, BD) == [])
check("status inner JSON that does not parse is rejected at $.json", rejected(drifts("board-debug", mod(inner_ok, json="{not json"), BD), "$.json", "does not parse"))
check("status inner object without `state` is rejected at $.json", rejected(drifts("board-debug", mod(inner_ok, json="{\"media\":false}"), BD), "$.json", "'state' is a required property"))
check("status inner `subs` must be an integer, reported at $.json.subs",
      rejected(drifts("board-debug", mod(inner_ok, json=json.dumps({**json.loads(inner_ok["json"]), "subs": "two"})), BD), "$.json.subs", "is not of type 'integer'"))
reject_pair("unknown board_debug type is drift", "board-debug", BD, meter, mod(meter, type="spectrum"), "$.type", "unknown record kind 'spectrum'")

# ============================================================================================ JSON: analysis records
DSP = "dsp.jsonl"
dsp = first(DSP, "k", "dsp")
reject_pair("dsp rms_dbfs needs 8 beams", "analysis-records", DSP, dsp, mod(dsp, rms_dbfs=dsp["rms_dbfs"][:7]), "$.rms_dbfs", "too short")
reject_pair("dsp band_pct needs 5 bands", "analysis-records", DSP, dsp, mod(dsp, band_pct=dsp["band_pct"] + [1.0]), "$.band_pct", "too long")
reject_pair("dsp flatness above 1", "analysis-records", DSP, dsp, mod(dsp, flatness=1.5), "$.flatness", "greater than the maximum of 1")
tr = first(DSP, "k", "transient")
reject_pair("transient loudest_beam 9", "analysis-records", DSP, mod(tr, loudest_beam=8), mod(tr, loudest_beam=9), "$.loudest_beam", "greater than the maximum of 8")
ts_ = first(DSP, "k", "tonal_state")
ts_bad = copy.deepcopy(ts_)
ts_bad["lines"] = [{"hz": 1000.0, "excess_db": 9.0}]
check("tonal_state line without age_s is rejected", rejected(drifts("analysis-records", ts_bad, DSP), "$.lines[0]", "'age_s' is a required property"))
check("tonal_state fixture is valid (positive twin)", drifts("analysis-records", ts_, DSP) == [])
reject_pair("tonal_gone lasted_s", "analysis-records", DSP, first(DSP, "k", "tonal_gone"), mod(first(DSP, "k", "tonal_gone"), lasted_s="60"), "$.lasted_s", "is not of type 'number'")
check("a tags record in dsp.jsonl is not allowed", rejected(drifts("analysis-records", first("tags.jsonl", "k", "tags"), DSP), "$.k", "not allowed in dsp.jsonl"))
check("the same tags record in tags.jsonl is fine", drifts("analysis-records", first("tags.jsonl", "k", "tags"), "tags.jsonl") == [])
sp = first("spatial.jsonl", "k", "spatial")
reject_pair("spatial calibrated must be false in 1.0.0", "analysis-records", "spatial.jsonl", sp, mod(sp, calibrated=True), "$.calibrated", "False was expected")
reject_pair("spatial e_rel_db is relative to the loudest (max 0)", "analysis-records", "spatial.jsonl", sp, mod(sp, e_rel_db=[0.0, 3.0] + sp["e_rel_db"][2:]), "$.e_rel_db[1]", "greater than the maximum")
tg = first("tags.jsonl", "k", "tags", speech_active=False)
reject_pair("tags: speech_active false requires emb_stored true", "analysis-records", "tags.jsonl", tg, mod(tg, emb_stored=False), "$.emb_stored", "True was expected")
tg_sp = mod(tg, speech_active=True, emb_stored=False, speech_p=0.6)
reject_pair("tags: speech_active true requires emb_stored false", "analysis-records", "tags.jsonl", tg_sp, mod(tg_sp, emb_stored=True), "$.emb_stored", "False was expected")
reject_pair("tags: a label entry is [label, probability]", "analysis-records", "tags.jsonl", tg, mod(tg, top=[["Silence", "0.5"]]), "$.top[0][1]", "is not of type 'number'")
check("tags: an empty `top` is valid (all labels below 0.05)", drifts("analysis-records", mod(tg, top=[]), "tags.jsonl") == [])
cl = first("clap.jsonl", "k", "clap")
reject_pair("clap carries exactly 3 prompts", "analysis-records", "clap.jsonl", cl, mod(cl, top=cl["top"][:2]), "$.top", "too short")
bd_ = first("birds.jsonl", "k", "bird")
reject_pair("bird provenance is always model_opinion", "analysis-records", "birds.jsonl", bd_, mod(bd_, provenance="ground_truth"), "$.provenance", "'model_opinion' was expected")
check("bird with null common/scientific/confidence is valid", drifts("analysis-records", mod(bd_, common=None, scientific=None, confidence=None), "birds.jsonl") == [])
bf_ = first("birds.jsonl", "k", "bird_filtered")
check("bird_filtered fixture is the real producer shape: in_region false, provenance model_opinion; 3 live 1.1.0 lines (no non_species) plus 1 line of the 1.2.0 shape and 2 of the 1.3.0 shape (one per beam)",
      bf_["in_region"] is False and bf_["provenance"] == "model_opinion" and "non_species" not in bf_ and
      len([r for r in jl("birds.jsonl") if r["k"] == "bird_filtered" and "reject_reason" not in r]) == 3 and
      len([r for r in jl("birds.jsonl") if r["k"] == "bird_filtered" and r.get("reject_reason") == "geo_not_predicted"]) == 3)
reject_pair("bird_filtered in_region must be false (a kept-and-flagged out-of-region record)", "analysis-records", "birds.jsonl", bf_, mod(bf_, in_region=True), "$.in_region", "False was expected")
reject_pair("bird_filtered requires in_region", "analysis-records", "birds.jsonl", bf_, mod(bf_, in_region=KeyError), "$", "'in_region' is a required property")
reject_pair("bird_filtered requires provenance", "analysis-records", "birds.jsonl", bf_, mod(bf_, provenance=KeyError), "$", "'provenance' is a required property")
# ---- analysis-records 1.3.0 / perch 1.1.0 / verdict-labels 1.1.0 provenance fields (fixtures written by the real plugins, make_provenance_fixtures.py)
pv_tags, pv_clap = next(r for r in jl("tags.jsonl") if "model" in r), next(r for r in jl("clap.jsonl") if "model" in r)
pv_bird = next(r for r in jl("birds.jsonl") if r["k"] == "bird" and "model" in r)
check("provenance fixtures: tags model {PANNs CNN14, variant best_beam_32k}, claim, 5 top_k without Speech, max_unstored 0.02; clap 5 top_k; bird max_unstored null with score_floor 0.25",
      (pv_tags["model"]["name"], pv_tags["model"]["variant"], len(pv_tags["model"]["sha256"]), pv_tags["claim"], len(pv_tags["top_k"]), pv_tags["max_unstored_score"]) == ("PANNs CNN14", "best_beam_32k", 64, {"human_verified": False, "source": "model_opinion"}, 5, 0.02)
      and "Speech" not in [x[0] for x in pv_tags["top_k"]] and ["Speech", 0.197] in pv_tags["top"]
      and (len(pv_clap["top_k"]), pv_clap["model"]["variant"]) == (5, "best_beam_48k") and (pv_bird["max_unstored_score"], pv_bird["score_floor"], pv_bird["provenance"]) == (None, 0.25, "model_opinion"), str((pv_tags, pv_clap, pv_bird))[:300])
reject_pair("tags claim.human_verified is always false", "analysis-records", "tags.jsonl", pv_tags, mod(pv_tags, claim={"human_verified": True, "source": "model_opinion"}), "$.claim.human_verified", "False was expected")
reject_pair("tags top_k carries exactly 5 labels", "analysis-records", "tags.jsonl", pv_tags, mod(pv_tags, top_k=pv_tags["top_k"][:4]), "$.top_k", "too short")
reject_pair("tags model.sha256 is 64 lowercase hex", "analysis-records", "tags.jsonl", pv_tags, mod(pv_tags, model=dict(pv_tags["model"], sha256="ABC")), "$.model.sha256", "does not match")
reject_pair("clap model needs a name", "analysis-records", "clap.jsonl", pv_clap, mod(pv_clap, model={k: v for k, v in pv_clap["model"].items() if k != "name"}), "$.model", "'name' is a required property")
reject_pair("bird score_floor is a probability", "analysis-records", "birds.jsonl", pv_bird, mod(pv_bird, score_floor=2), "$.score_floor", "greater than the maximum of 1")
check("analysis-records 1.2.0 rows without any provenance field still pass (readers tolerate absent)", drifts("analysis-records", first("tags.jsonl", "k", "tags"), "tags.jsonl") == [] and "model" not in first("tags.jsonl", "k", "tags"))
pv_perch = first("perch.jsonl", "k", "perch")
reject_pair("perch claim.source is always model_opinion", "perch", "perch.jsonl", pv_perch, mod(pv_perch, claim={"human_verified": False, "source": "owner_verdict"}), "$.claim.source", "'model_opinion' was expected")
pv_v = [r for r in jl("verdict_labels.jsonl") if "model_sha256" in r][0]
reject_pair("verdict model_sha256 is 64 lowercase hex", "verdict-labels", "verdict_labels.jsonl", pv_v, mod(pv_v, model_sha256="0123"), "$.model_sha256", "does not match")
check("verdict fixtures: one verdict carries model_sha256 (64 hex), two do not (the event had no digest)", len([r for r in jl("verdict_labels.jsonl") if "model_sha256" in r]) == 1 and len(jl("verdict_labels.jsonl")) == 3)
reject_pair("bird_filtered confidence is 0..1", "analysis-records", "birds.jsonl", bf_, mod(bf_, confidence=1.5), "$.confidence", "greater than the maximum of 1")
reject_pair("bird_filtered provenance is always model_opinion", "analysis-records", "birds.jsonl", bf_, mod(bf_, provenance="ground_truth"), "$.provenance", "'model_opinion' was expected")
check("bird_filtered with null common/scientific/confidence is valid", drifts("analysis-records", mod(bf_, common=None, scientific=None, confidence=None), "birds.jsonl") == [])
check("bird_filtered is not allowed in dsp.jsonl", rejected(drifts("analysis-records", bf_, DSP), "$.k", "not allowed in dsp.jsonl"))
check("the unknown-kind message lists bird_filtered among the known kinds", "'bird_filtered'" in [d.message for d in drifts("analysis-records", mod(bf_, k="bird_v2"), "birds.jsonl")][0])
check("`bird` records are still accepted and a bird with in_region true is still valid (additive change)", drifts("analysis-records", mod(bd_, in_region=True, non_species=False), "birds.jsonl") == [])
nv = first("novelty.jsonl", "k", "novelty", novel=False)
nv_novel = mod(nv, novel=True, score=3.0)
reject_pair("novelty novel with score 3.0 is valid (rounding), 2.9 is not", "analysis-records", "novelty.jsonl", nv_novel, mod(nv_novel, score=2.9), "$.score", "less than the minimum of 3.0")
warm = next(r for r in jl("novelty.jsonl") if r.get("state") == "warming_up")
check("novelty warming_up shape is valid", drifts("analysis-records", warm, "novelty.jsonl") == [])
check("novelty warming_up with a scene is rejected", drifts("analysis-records", mod(warm, scene=1), "novelty.jsonl") != [])
ne = first("novelty.jsonl", "k", "novel_event")
reject_pair("novel_event score", "analysis-records", "novelty.jsonl", ne, mod(ne, score=2.0), "$.score", "less than the minimum of 3.0")
reject_pair("unknown analysis kind is drift", "analysis-records", DSP, dsp, mod(dsp, k="dsp_v2"), "$.k", "unknown record kind 'dsp_v2'")

# ============================================================================================ sink status 1.1.0 and beam_bands
sk_m = jf("sink_status_metering_only.json")
check("sink_status metering-only fixture: device null, audio_output false, playing false, device_error a string",
      sk_m["device"] is None and sk_m["audio_output"] is False and sk_m["playing"] is False and isinstance(sk_m["device_error"], str), str(sk_m))
check("sink_status metering-only fixture is valid", whole("sink-status", sk_m) == [], str(whole("sink-status", sk_m)[:2]))
check("sink_status 1.0.0 shape (no audio_output / device_error) is still valid: the 1.1.0 fields are additive",
      whole("sink-status", {k: v for k, v in sk.items() if k not in ("audio_output", "device_error")}) == [])
check("sink_status fixtures carry rx_errors 0 and the 1.1.0 shape without it is still valid: the 1.2.0 field is additive",
      sk["rx_errors"] == 0 and sk_m["rx_errors"] == 0 and whole("sink-status", {k: v for k, v in sk.items() if k != "rx_errors"}) == [])
whole_pair("sink_status rx_errors negative", "sink-status", mod(sk, rx_errors=3), mod(sk, rx_errors=-1), "$.rx_errors", "less than the minimum of 0")
whole_pair("sink_status rx_errors type", "sink-status", sk, mod(sk, rx_errors="3"), "$.rx_errors", "is not of type 'integer'")
whole_pair("sink_status audio_output type", "sink-status", sk, mod(sk, audio_output="yes"), "$.audio_output", "is not of type 'boolean'")
whole_pair("sink_status device_error type", "sink-status", mod(sk, device_error=None), mod(sk, device_error=5), "$.device_error", "is not of type 'string', 'null'")
whole_pair("sink_status device must not be an empty string", "sink-status", sk, mod(sk, device=""), "$.device", "should be non-empty")

BBF = "beam_bands.jsonl"
bb = jl(BBF)
check("beam_bands fixtures: 5 records from known signals", len(bb) == 5, str(len(bb)))
import math  # noqa: E402
sine = bb[1]
reject_pair("beam_bands record version v", "beam-bands", BBF, sine, mod(sine, v=2), "$.v", "1 was expected")
reject_pair("beam_bands window length n", "beam-bands", BBF, sine, mod(sine, n=44100), "$.n", "48000 was expected")
reject_pair("beam_bands zero_frac above 1", "beam-bands", BBF, sine, mod(sine, zero_frac=1.5), "$.zero_frac", "greater than the maximum of 1")
reject_pair("beam_bands idx above u32", "beam-bands", BBF, mod(sine, idx=4294967295), mod(sine, idx=1 << 32), "$.idx", "greater than the maximum of 4294967295")
reject_pair("beam_bands edges_hz are pinned", "beam-bands", BBF, sine, mod(sine, edges_hz=sine["edges_hz"][:7]), "$.edges_hz", "was expected")
reject_pair("beam_bands needs t_us", "beam-bands", BBF, sine, mod(sine, t_us=KeyError), "$", "'t_us' is a required property")
db7 = mod(sine, db=sine["db"][:7])
reject_pair("beam_bands needs 8 beams", "beam-bands", BBF, sine, db7, "$.db", "too short")
row7 = copy.deepcopy(sine)
row7["db"][0] = row7["db"][0][:7]
reject_pair("beam_bands needs 8 bands per beam", "beam-bands", BBF, sine, row7, "$.db[0]", "too short")
low = copy.deepcopy(sine)
low["db"][0][0] = -201.0
reject_pair("beam_bands level below -200.0", "beam-bands", BBF, sine, low, "$.db[0][0]", "less than the minimum of -200.0")
high = copy.deepcopy(sine)
high["db"][0][0] = 0.5
reject_pair("beam_bands level above 0.0 dB re full scale", "beam-bands", BBF, sine, high, "$.db[0][0]", "greater than the maximum of 0.0")
check("beam_bands silence record: zero_frac 1.0 and every level exactly -200.0", bb[0]["zero_frac"] == 1.0 and all(v == -200.0 for row in bb[0]["db"] for v in row))
check("beam_bands 700 Hz sine A=0.1 on beam 3 reads 20*log10(0.1/sqrt(2)) = -23.0 dB in band 500-1000 (db[2][3]), nothing above -150 elsewhere on the beam",
      sine["db"][2][3] == round(20 * math.log10(0.1 / math.sqrt(2)), 1) == -23.0 and max(v for i, v in enumerate(sine["db"][2]) if i != 3) < -150 and max(max(r) for i, r in enumerate(sine["db"]) if i != 2) == -200.0)
check("beam_bands full-scale 700 Hz sine on beam 1 reads -3.0 dB (db[0][3])", bb[2]["db"][0][3] == -3.0)
check("beam_bands half-zero window has zero_frac exactly 0.5", bb[4]["zero_frac"] == 0.5)
check("beam_bands fixture indexes advance by one window (48000) per record", [r["idx"] for r in bb] == [284267690 + i * 48000 for i in range(5)])
if HAS_BANDS:
    import numpy as _np
    tt = _np.arange(48000) / 48000.0
    zz = _np.zeros(48000)
    meter = S.BandMeter(clock_us=lambda: 5)
    prod_rec = meter.push(7, _np.stack([zz, zz, 0.1 * _np.sin(2 * _np.pi * 700 * tt)] + [zz] * 5, axis=1).astype(_np.float32))
    exp = {k: v for k, v in sine.items() if k not in ("idx", "t_us")}
    got = {k: v for k, v in prod_rec[0].items() if k not in ("idx", "t_us")}
    check("beam_bands: the production BandMeter reproduces the fixture record for the same signal exactly", got == exp)
    check("beam_bands: idx is the window's first sample index and t_us comes from the clock", (prod_rec[0]["idx"], prod_rec[0]["t_us"]) == (7, 5))
    bands_validator = C.kind_validator(SCH["beam-bands"], "beam_bands")
    check("beam_bands: a record produced by BandMeter validates against the schema", [e.message for e in C.errors(bands_validator, prod_rec[0])] == [])
    m2 = S.BandMeter(clock_us=lambda: 5)
    r1000 = m2.push(0, _np.stack([0.1 * _np.sin(2 * _np.pi * 1000 * tt)] + [zz] * 7, axis=1).astype(_np.float32))[0]["db"][0]
    check("beam_bands: a 1000 Hz tone (a band edge) splits across 500-1000 and 1000-2000 but the powers add up to A^2/2 within 0.15 dB",
          abs(10 * math.log10(sum(10 ** (v / 10) for v in r1000)) - 20 * math.log10(0.1 / math.sqrt(2))) < 0.15 and r1000[3] > -200 and r1000[4] > r1000[3], str(r1000))
    m3 = S.BandMeter(clock_us=lambda: 5)
    a1 = m3.push(0, _np.zeros((30000, 8), _np.float32))
    a2 = m3.push(30000, _np.zeros((30000, 8), _np.float32))
    check("beam_bands: a window is 48000 contiguous samples: 30000 + 30000 gives no record, then one starting at idx 0 (12000 samples remain)", (len(a1), len(a2), a2[0]["idx"] if a2 else None) == (0, 1, 0))
    m4 = S.BandMeter(clock_us=lambda: 5)
    m4.push(0, _np.zeros((30000, 8), _np.float32))
    check("beam_bands: a hole between frames restarts the window (no record from 30000 + a frame at 50000)", m4.push(50000, _np.zeros((30000, 8), _np.float32)) == [])

    class _Log:
        def __init__(self):
            self.lines = []

        def write(self, line):
            self.lines.append(line)
    dtrack, lg = S.DataTrack(), _Log()
    S.feed_bands(S.BandMeter(clock_us=lambda: 9), 100, _np.stack([zz] * 8, axis=1).astype(_np.float32), dtrack, lg)
    frames4, bad4 = R.parse_frames(bytes(dtrack.fifo))
    check("beam_bands: feed_bands puts the identical compact JSON line on the log and in a kind-4 data-track frame",
          len(lg.lines) == 1 and frames4 == [(R.K_BANDS, lg.lines[0])] and bad4 == 0 and lg.lines[0] == json.dumps(json.loads(lg.lines[0]), separators=(",", ":")).encode(),
          str(frames4)[:120])
else:
    for nm in ("the production BandMeter reproduces the fixture record", "BandMeter windows and band-edge behaviour", "feed_bands writes log line == kind-4 payload"):
        skip(f"beam_bands: {nm}", "the sink under test has no BandMeter (pc/bma_cable_sink.py must define BandMeter)")

# ============================================================================================ binary: BMAA
ex = jf("bmaa_examples.json")
prod_same = all(B.bmaa(e["slot"], e["flags"], e["seq"], e["block"], e["samples"]) == bytes.fromhex(e["hex"]) for e in ex["valid"])
check("BMAA: the production dante_bridge.bmaa() emits exactly the bytes in every example", prod_same)
check("BMAA: the reference builder emits exactly the same bytes",
      all(R.build_bmaa(e["slot"], e["flags"], e["seq"], e["block"], e["samples"]) == bytes.fromhex(e["hex"]) for e in ex["valid"]))
wrap = next(e for e in ex["valid"] if "wrap" in e["name"])
pw = R.parse_bmaa(bytes.fromhex(wrap["hex"]))
check("BMAA: seq and block 0xFFFFFFFF / 0xFFFFFF00 survive unchanged and flags 3 = zero + gap_before",
      (pw["seq"], pw["block"], pw["flags"], pw["zero"], pw["gap_before"]) == (0xFFFFFFFF, 0xFFFFFF00, 3, True, True), str(pw))
ext = next(e for e in ex["valid"] if e["name"].startswith("plain"))
pe = R.parse_bmaa(bytes.fromhex(ext["hex"]))
check("BMAA: extreme samples -8388608 and 8388607 round trip", pe["samples"][3:5] == [8388607, -8388608], str(pe["samples"]))
sk_ok = []
for e in ex["valid"]:
    got = S.parse_datagram(bytes.fromhex(e["hex"]))
    sk_ok.append(got is not None and got[0] == e["slot"] and got[1] == e["flags"] and got[2] == e["block"]
                 and np.array_equal(got[3], np.array(e["samples"], np.float32) * np.float32(1.0 / 8388608.0)))
check("BMAA: the cable sink parser agrees with the reference on every valid example (slot, flags, block, scaled samples)", all(sk_ok), str(sk_ok))
cls_ok = []
for e in ex["valid"]:
    got = CL._parse_datagram(bytes.fromhex(e["hex"]))
    cls_ok.append(got is not None)
check("BMAA: the classifier parser accepts every valid example", all(cls_ok), str(cls_ok))
for e in ex["invalid"]:
    if e["code"] in ("magic", "slot", "length", "short"):
        check(f"BMAA invalid ({e['name']}): the sink parser rejects it like the reference (code {e['code']})", S.parse_datagram(bytes.fromhex(e["hex"])) is None)
for e in ex["invalid"]:
    try:
        R.parse_bmaa(bytes.fromhex(e["hex"]))
        got = None
    except R.ContractError as ce:
        got = ce.code
    check(f"BMAA invalid ({e['name']}): reference rejects with code {e['code']}", got == e["code"], str(got))
vers2 = bytes.fromhex(next(e for e in ex["invalid"] if e["code"] == "version")["hex"])
resv = bytes.fromhex(next(e for e in ex["invalid"] if e["code"] == "reserved")["hex"])
oor = B.bmaa(0, 0, 0, 0, np.array([1 << 24], dtype=np.int32))
slot8 = bytes.fromhex(next(e for e in ex["invalid"] if e["code"] == "slot")["hex"])
base = bytes.fromhex(ext["hex"])                                   # a valid datagram, used as the positive twin
setb = lambda pkt, i, v: pkt[:i] + bytes([v]) + pkt[i + 1:]
edge_in = [B.bmaa(0, 0, 0, 0, np.array([v], dtype=np.int32)) for v in (8388607, -8388608)]
edge_out = [B.bmaa(0, 0, 0, 0, np.array([v], dtype=np.int32)) for v in (8388608, -8388609, 1 << 24)]
for pname, parse in (("sink", S.parse_datagram), ("classifier", CL._parse_datagram)):
    check(f"BMAA: the {pname} parser accepts the unmodified valid datagram (positive twin of the rejections below)", parse(base) is not None)
    check(f"BMAA: the {pname} rejects version 2", parse(vers2) is None)
    check(f"BMAA: the {pname} rejects version 0", parse(setb(base, 4, 0)) is None)
    check(f"BMAA: the {pname} rejects a non-zero reserved byte (1 and 255)", parse(resv) is None and parse(setb(base, 5, 255)) is None)
    check(f"BMAA: the {pname} rejects slot 8 and slot 255", parse(slot8) is None and parse(setb(base, 6, 255)) is None)
    check(f"BMAA: the {pname} rejects a length that is not exactly 20 + 4n (one byte too many, one too few)",
          parse(base + b"\0") is None and parse(base[:-1]) is None)
    check(f"BMAA: the {pname} accepts the extreme in-range samples 8388607 and -8388608", all(parse(p) is not None for p in edge_in))
    check(f"BMAA: the {pname} rejects samples just outside signed 24 bit (8388608, -8388609, 1<<24)", all(parse(p) is None for p in edge_out))
    check(f"BMAA: the {pname} accepts flag bits 0-1 and an empty block (n = 0)",
          parse(setb(base, 7, 3)) is not None and parse(B.bmaa(0, 0, 0, 0, np.array([], dtype=np.int32))) is not None)
asm = S.Assembler()
pre = []
for b in (4294966784, 4294967040):
    for s in range(8):
        pre += asm.push(s, b, np.zeros(256, np.float32))
post = []
for s in range(8):
    post += asm.push(s, 0, np.zeros(256, np.float32))
check("BMAA wrap: the two blocks before the u32 wrap are assembled", len(pre) == 2, str(len(pre)))
check("BMAA wrap: the block with index 0 after the wrap is assembled (sink unwraps the u32 index; CHANGELOG 1)", len(post) == 1, str(len(post)))

# ============================================================================================ binary: cable stream
fr = jf("cable_frames.json")["frames"]
check("BMD1: the fixture has frames for kinds 1, 2, 3 and 4", [f["kind"] for f in fr] == [1, 2, 3, 4], str([f["kind"] for f in fr]))
check("BMD1: the production encode_frame emits exactly the example bytes for kinds 1-4",
      [S.encode_frame(f["kind"], bytes.fromhex(f["payload_hex"])).hex() for f in fr] == [f["frame_hex"] for f in fr])
check("BMD1: the reference encoder emits the same bytes", [R.encode_frame(f["kind"], bytes.fromhex(f["payload_hex"])).hex() for f in fr] == [f["frame_hex"] for f in fr])
anc = bytes.fromhex(fr[0]["payload_hex"])
check("BMD1 anchor payload decodes to (284267690, 1792886495588944)", R.parse_anchor(anc) == (284267690, 1792886495588944))
frame0 = bytes.fromhex(fr[0]["frame_hex"])
flipped = bytearray(frame0)
flipped[8] ^= 0x01
check("BMD1: a flipped payload bit is a corrupt frame (counted), the intact frame is not", R.parse_frames(bytes(flipped)) == ([], 1) and R.parse_frames(frame0) == ([(1, anc)], 0))
check("BMD1: a frame inside idle zero bytes and after garbage still parses", R.parse_frames(b"\x00" * 9 + b"junk" + frame0 + b"\x00" * 5)[0] == [(1, anc)])
cc = jf("cable_columns.json")
c0 = cc["cases"][0]
check("cable index (3<<24)|0x123456 = 0x3123456 has columns (0x456-2048, 0x123-2048, 3-2048) << 8 = (-240128, -449792, -523520) and round trips",
      (R.index_columns(c0["index"]), R.index_from_columns(*c0["columns"])) == ((-240128, -449792, -523520), 51524694), str(c0))
check("cable fixture cases: 0xFFFF8001 -> (-524032, 522240, -459008); the u32 maximum -> (524032, 524032, -459008); index 0 is a live row: three columns at -524288",
      [c["columns"] for c in cc["cases"] if c["index"] in (0xFFFF8001, 0xFFFFFFFF, 0)] == [[-524032, 522240, -459008], [524032, 524032, -459008], [-524288, -524288, -524288]],
      str([c for c in cc["cases"] if c["index"] in (0xFFFF8001, 0xFFFFFFFF, 0)]))
SYMS = [0, 1, 2047, 2048, 2049, 4094, 4095, 0x123, 0xA5A]
check("cable column helpers match the production sym12_to_f32 exactly (symbol -> float32 of (u - 2048) << 8 counts)",
      np.array_equal(S.sym12_to_f32(np.array(SYMS)) * np.float32(8388608.0), np.array([R.symbol_column(v) for v in SYMS], np.float32)))
check("cable: R.symbol_from_column inverts R.symbol_column for all 4096 symbols",
      all(R.symbol_from_column(R.symbol_column(v)) == v for v in range(4096)))
check("cable: R.symbol_from_column rounds - +100 and -100 counts of noise read the same symbol, +-128 do not",
      R.symbol_from_column(R.symbol_column(0x123) + 100) == 0x123 and R.symbol_from_column(R.symbol_column(0x123) - 100) == 0x123
      and R.symbol_from_column(R.symbol_column(0x123) + 128) == 0x124 and R.symbol_from_column(R.symbol_column(0x123) - 129) == 0x122)
check("cable: the rounding window is asymmetric - R.symbol_from_column reads -128 and +127 counts exactly (residual window [-128, +127])",
      R.symbol_from_column(R.symbol_column(0x123) - 128) == 0x123 and R.symbol_from_column(R.symbol_column(0x123) + 127) == 0x123)
check("cable: R.symbol_column rejects symbols outside 0..4095",
      all(_raises(lambda v=v: R.symbol_column(v)) == "symbol" for v in (-1, 4096)))

clock = [1_792_884_000_000_000]
data = S.DataTrack()
comp = S.Composer(data, anchor_every=300, clock_us=lambda: clock[0])
board_rec = json.dumps({"type": "meter", "seq": 1, "ms": 5, "zero": False, "vals": {"76": "0001"}, "host_t": 1792884000.25}, separators=(",", ":")).encode()
BIG = (3 << 24) | 0x123456
beams = np.stack([np.arange(600, dtype=np.int64) * 8 + c for c in range(8)], axis=1)
data.push(S.K_BOARD, board_rec)
rows_a = comp.compose(BIG, (beams[:300] / 8388608.0).astype(np.float32))
clock[0] += 1_000_000
rows_b = comp.compose(BIG + 300, (beams[300:] / 8388608.0).astype(np.float32))
out16 = np.concatenate([rows_a, rows_b])
k16 = np.rint(out16.astype(np.float64) * 8388608.0).astype(np.int64)
ref = R.decode_rows(k16.tolist())
check("cable: the reference decodes the production Composer's index track exactly for all 600 rows", ref["index"] == list(range(BIG, BIG + 600)))
check("cable: beams 1-8 are carried untouched", ref["beams"] == beams.tolist())
check("cable: channels 9-13 carry only symbols - every count is a multiple of 256 (low 8 bits zero)", not (k16[:, 8:13] & 255).any())
check("cable: reserved channels 14-16 are exactly zero", not k16[:, 13:].any())
check("cable: no silent rows, no corrupt frames", not any(ref["silent"]) and ref["corrupt"] == 0)
check("cable: data-track frames in FIFO order are board record, anchor 1, anchor 2",
      ref["frames"] == [(2, board_rec), (1, R.anchor_payload(BIG, 1_792_884_000_000_000)), (1, R.anchor_payload(BIG + 300, 1_792_884_001_000_000))], str(ref["frames"])[:200])
prod = D.decode(k16)
check("cable: the production decoder agrees with the reference on index and anchors",
      prod["index"].tolist() == ref["index"] and prod["anchors"] == [R.parse_anchor(p) for kd, p in ref["frames"] if kd == 1])
sil = k16.copy()
sil[100:150] = 0
ref2 = R.decode_rows(sil.tolist())
check("cable: rows the sink invented (all-zero index columns, the underrun marker) are flagged silent and carry no data bytes",
      sum(ref2["silent"]) == 50 and ref2["frames"] == ref["frames"] and ref2["corrupt"] == 0)
# underrun rows INSIDE a frame: 30 zero rows inserted between rows 10 and 11, where the first board record is still being sent
ins = np.concatenate([k16[:11], np.zeros((30, 16), np.int64), k16[11:]])
ref3 = R.decode_rows(ins.tolist())
prod3 = D.decode(ins)
check("cable: 30 underrun rows inserted INSIDE the first frame (rows 11-40) are skipped by the reference decoder: 30 silent rows, frames byte-identical, 0 corrupt",
      sum(ref3["silent"]) == 30 and ref3["frames"] == ref["frames"] and ref3["corrupt"] == 0 and ref3["index"][:11] == list(range(BIG, BIG + 11)) and ref3["index"][11:41] == [0] * 30,
      f"{sum(ref3['silent'])} {ref3['corrupt']}")
check("cable: the production decoder skips them identically (30 silent, same frames, 0 corrupt, no impossible value)",
      int(prod3["silent"].sum()) == 30 and prod3["frames"] == ref["frames"] and prod3["bad_frames"] == 0 and prod3["impossible"] == 0, f"{prod3['silent'].sum()} {prod3['bad_frames']}")
row_8 = [0] * 16
row_8[8:11] = list(R.index_columns(0x800))                       # bits 0-11 = 2048: ch9 is exactly 0 counts, ch10/ch11 are not
row_c = [0] * 16
row_c[8:11] = [0, 0, R.symbol_column(5)]                         # ch9 = ch10 = 0 counts, ch11 = u 5
row_z = [0] * 16
check("cable: silence is ALL THREE index columns at 0 counts - 0x800 (ch9 alone at 0) and (0, 0, u=5) are live rows; the all-zero row is silent",
      R.is_silent_row(row_8) is False and R.is_silent_row(row_c) is False and R.is_silent_row(row_z) is True
      and R.decode_rows([row_8, row_c, row_z])["silent"] == [False, False, True] and R.decode_rows([row_8])["index"] == [0x800]
      and R.decode_rows([row_c])["index"] == [(5 << 24) | (2048 << 12) | 2048], str(R.decode_rows([row_8, row_c, row_z])))
check("cable: the production decoder agrees on those three rows (only the all-zero one is silent)",
      D.decode(np.array([row_8, row_c, row_z], np.int64))["silent"].tolist() == [False, False, True])
dcols = R.data_columns(R.encode_frame(2, board_rec)[:80])
check("cable: R.data_columns of the first 80 bytes of the board record frame equals the production Composer's ch12/ch13 for rows 0-39 (first byte on ch12)",
      dcols == [tuple(r) for r in k16[:40, 11:13].tolist()] and dcols[0] == (-507392, -504576) and len(dcols) == 40, str(dcols[:2]))
check("cable: R.data_columns rejects an odd byte count with code data_length", _raises(lambda: R.data_columns(b"abc")) == "data_length")
try:
    R.decode_rows([[0] * 15])
    width_code = None
except R.ContractError as ce:
    width_code = ce.code
check("cable: a row of the wrong width is rejected with code row_width", width_code == "row_width", str(width_code))
try:
    R.decode_rows([[0] * 16] * 2)
    check("cable: two all-zero rows (the underrun marker) decode without error", True)
except R.ContractError as ce:
    check("cable: two all-zero rows (the underrun marker) decode without error", False, str(ce))

# ============================================================================================ checker behavior on a synthetic live root
tmp = tempfile.mkdtemp(prefix="contracts_test_")


def mkcontracts(dst):
    shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns("__pycache__", "tools"))
    stub = os.path.join(dst, "specs", "analysis-outputs.md")
    if not os.path.exists(stub):
        with open(stub, "w") as f:
            f.write("contract: analysis-outputs\ncontract_version: 1.0.0\n")


def mkroot(root):
    os.makedirs(os.path.join(root, "analysis"))
    for fn in ("dsp.jsonl", "spatial.jsonl", "tags.jsonl", "clap.jsonl", "birds.jsonl", "novelty.jsonl", "board_debug.jsonl"):
        shutil.copy(os.path.join(FIX, fn), os.path.join(root, "analysis", fn))
    shutil.copy(os.path.join(FIX, "analysis_status.json"), os.path.join(root, "analysis", "status.json"))
    shutil.copy(os.path.join(FIX, "bridge_status.json"), os.path.join(root, "bridge_status.json"))


root = os.path.join(tmp, "live")
mkroot(root)
n1, d1, notes = C.check_live(root, SCH, 100)
check("live check on a clean synthetic root: 0 drift", d1 == [], str([str(d) for d in d1][:2]))
_opt_prefixes = ("history/", "labels/", "analysis/clips/") + tuple(lv_["file"] for c_ in C.PLUGIN_CONTRACTS.values() for lv_ in c_["live"] if lv_["optional"])     # derived from the manifest
_other_notes = [n_ for n_ in notes if not n_.startswith(_opt_prefixes)]
check("live check: the absent optional sink_status.json is a note, not drift", _other_notes[0].startswith("sink_status.json") and len(_other_notes) == 1, str(notes))





root_bf = os.path.join(tmp, "live_bf")
mkroot(root_bf)
_bf_live = os.path.join(root_bf, "analysis", "birds.jsonl")
with open(_bf_live, "a", encoding="utf-8") as f:                    # the producer's real out-of-region records (plugins.py MultiBeamBirds)
    f.write(json.dumps(bf_) + "\n" + json.dumps(mod(bf_, beam=2, confidence=None)) + "\n")
_n, _d, _ = C.check_live(root_bf, SCH, 100)
check("live check: appended valid bird_filtered records (the producer's out-of-region audit lines) are 0 drift", _d == [], str([str(d) for d in _d][:2]))
with open(_bf_live, "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(bf_, in_region=True)) + "\n")
_n, _d, _ = C.check_live(root_bf, SCH, 100)
check("live check: a bird_filtered record with in_region true is reported with file, kind, path and message",
      len(_d) == 1 and (_d[0].file, _d[0].kind, _d[0].path) == ("analysis/birds.jsonl", "bird_filtered", "$.in_region") and "False was expected" in _d[0].message, str([str(d) for d in _d]))
with open(os.path.join(root, "analysis", "dsp.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(dsp, rms_dbfs=dsp["rms_dbfs"][:7])) + "\n")
n2, d2, _ = C.check_live(root, SCH, 100)
check("live check: an appended malformed dsp record is reported with file, tail position, path and message",
      len(d2) == 1 and d2[0].file == "analysis/dsp.jsonl" and d2[0].path == "$.rms_dbfs" and "too short" in d2[0].message and d2[0].where.startswith("tail["), str([str(d) for d in d2]))
with open(os.path.join(root, "analysis", "dsp.jsonl"), "a", encoding="utf-8") as f:
    f.write('{"k":"dsp","t":17')                       # a record being written right now (no newline)
n3, d3, _ = C.check_live(root, SCH, 100)
check("live check: a trailing partial line (mid-write) is ignored, not drift", [str(d) for d in d3] == [str(d) for d in d2])
with open(os.path.join(root, "analysis", "tags.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"k": "tags", "t": 1.0}) + "\n")
n4, d4, _ = C.check_live(root, SCH, 100)
check("live check: a tags record missing its fields lists each missing field",
      sorted(d.message for d in d4 if d.file == "analysis/tags.jsonl") == sorted(f"'{k}' is a required property" for k in ("beam", "speech_p", "speech_active", "top", "emb_stored")),
      str([d.message for d in d4 if d.file == "analysis/tags.jsonl"]))
os.remove(os.path.join(root, "analysis", "clap.jsonl"))
n5, d5, _ = C.check_live(root, SCH, 100)
check("live check: a missing required live file is drift", any(d.file == "analysis/clap.jsonl" and d.message == "required live file is missing" for d in d5))
big = os.path.join(root, "analysis", "spatial.jsonl")
with open(big, "a", encoding="utf-8") as f:
    for _ in range(300):
        f.write(json.dumps(first("spatial.jsonl", "k", "spatial")) + "\n")
n6, d6, _ = C.check_live(root, SCH, 50)
check("live check with --lines 50 on a longer file reads exactly 50 whole records and no fragment", [d for d in d6 if d.file == "analysis/spatial.jsonl"] == [])

# CLI exit codes against copies of the contracts directory
cdir = os.path.join(tmp, "contracts")
mkcontracts(cdir)
root2 = os.path.join(tmp, "live2")
mkroot(root2)
check("CLI: exit 0 on a clean contracts copy + clean live root", C.main(["--contracts", cdir, "--live", "--root", root2]) == 0)
with open(os.path.join(root2, "analysis", "birds.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(bd_, provenance="ground_truth")) + "\n")
check("CLI: exit 1 when a live record drifts", C.main(["--contracts", cdir, "--live", "--root", root2]) == 1)
check("CLI: exit 2 when the contracts directory is unusable", C.main(["--contracts", os.path.join(tmp, "nowhere")]) == 2)

# ============================================================================================ every plugin contract of analysis/plugins.manifest.json (table-driven)
# What used to be repeated by hand for each plugin (the version in schema, spec and changelog, the fixture validates, the live file is optional and
# counted, an unknown record kind is exactly one drift, a CHANGELOG without the version is flagged) runs here for every contract the manifest
# declares. Bespoke checks (exact fixture contents, reject pairs, mutations of one schema field) stay in the per-contract blocks below, or in the
# files a contract lists under `tests` in the manifest (explicit, no glob).
import runpy  # noqa: E402
for cname_, c_ in sorted(C.PLUGIN_CONTRACTS.items()):
    sch_ = SCH[cname_]
    xf_ = sch_["x-files"]
    field_ = sch_["x-discriminator"]["field"]
    pairs_ = list(zip(c_["fixtures"], c_["live"]))
    spec_head_ = open(os.path.join(HERE, "specs", c_["spec"]), encoding="utf-8").read(600)
    check(f"manifest contract {cname_}: {len(c_['fixtures'])} fixture(s) for {len(c_['live'])} live file(s); schema $id urn:bma:contract:{cname_}; version {c_['version']} in manifest, schema and spec header; x-files maps exactly the live file name(s)",
          len(pairs_) == len(c_["fixtures"]) == len(c_["live"]) and sch_["$id"] == f"urn:bma:contract:{cname_}" and sch_["x-contract-version"] == c_["version"]
          and f"contract_version: {c_['version']}" in spec_head_ and sorted(xf_) == sorted(os.path.basename(lv_["file"]) for lv_ in c_["live"]), str((c_["version"], sorted(xf_))))
    for fx_, lv_ in pairs_:
        live_name_ = os.path.basename(lv_["file"])
        recs_ = jl(fx_)
        bad_ = [str(d_) for r_ in recs_ for d_ in C.check_record(live_name_, "1", r_, sch_, xf_[live_name_], {})]
        check(f"manifest contract {cname_}: fixture {fx_} holds {len(recs_)} record(s), every one validates, and every kind {live_name_} may hold ({xf_[live_name_]}) is present",
              len(recs_) >= 1 and bad_ == [] and {r_[field_] for r_ in recs_} >= set(xf_[live_name_]), str(bad_[:2]))
        unknown_ = dict(recs_[0], **{field_: "no_such_kind"})
        check(f"manifest contract {cname_}: a record of an unknown kind is drift of that kind (positive twin: the first fixture record is clean)",
              C.check_record(live_name_, "1", recs_[0], sch_, xf_[live_name_], {}) == []
              and [d_.kind for d_ in C.check_record(live_name_, "1", unknown_, sch_, xf_[live_name_], {})] == ["no_such_kind"], str(recs_[0])[:120])
        # live check on a synthetic root: absent = 0 drift and a note; the fixture adds exactly its records; one appended unknown-kind record = exactly one drift
        root_m = os.path.join(tmp, "live_m_" + cname_ + "_" + live_name_)
        mkroot(root_m)
        n0_, d0_, notes0_ = C.check_live(root_m, SCH, 100)
        shutil.copy(os.path.join(FIX, fx_), os.path.join(root_m, *lv_["file"].split("/")))
        n1_, d1_, notes1_ = C.check_live(root_m, SCH, 100)
        with open(os.path.join(root_m, *lv_["file"].split("/")), "a", encoding="utf-8", newline="\n") as f_:
            f_.write(json.dumps(unknown_) + "\n")
        n2_, d2_, _ = C.check_live(root_m, SCH, 100)
        check(f"manifest contract {cname_}: {lv_['file']} is optional (absent: 0 drift and a note), the {len(recs_)} fixture record(s) add {len(recs_)} and 0 drift, one appended record of an unknown kind is exactly one drift",
              lv_["optional"] is True and d0_ == [] and any(n_.startswith(lv_["file"]) for n_ in notes0_) and not any(n_.startswith(lv_["file"]) for n_ in notes1_)
              and d1_ == [] and n1_ == n0_ + len(recs_) and len(d2_) == 1 and (d2_[0].file, d2_[0].kind) == (lv_["file"], "no_such_kind"), str((n0_, n1_, [str(d_) for d_ in d2_][:2])))
    # the changelog names `<interface> <version>`: in CHANGELOG.md or a changelog.d fragment. Mutation: erase that text everywhere -> flagged; as committed -> clean
    cl_dir_ = os.path.join(tmp, "clmut_" + cname_)
    mkcontracts(cl_dir_)
    clean_ = [d_ for d_ in C.run(cl_dir_)[1] if d_.kind == "changelog" and d_.path == cname_]
    for root_, _dirs, files_ in os.walk(cl_dir_):
        for fn_ in files_:
            if fn_.endswith(".md") and (fn_ == "CHANGELOG.md" or os.path.basename(root_) == "changelog.d"):
                fp_ = os.path.join(root_, fn_)
                with open(fp_, "r", encoding="utf-8") as f_:
                    t_ = f_.read()
                with open(fp_, "w", encoding="utf-8", newline="\n") as f_:
                    f_.write(t_.replace(f"{cname_} {c_['version']}", f"{cname_} x"))
    gone_ = [d_ for d_ in C.run(cl_dir_)[1] if d_.kind == "changelog" and d_.path == cname_]
    check(f"manifest contract {cname_}: the changelog (CHANGELOG.md or a changelog.d fragment) names `{cname_} {c_['version']}`: as committed no drift, with that text erased exactly one",
          clean_ == [] and len(gone_) == 1, str((clean_, [str(d_) for d_ in gone_])))
    # the manifest's version is a third copy: bump it alone and the checker names it (positive twin: restored, no manifest drift)
    orig_ = c_["version"]
    c_["version"] = "9.9.9"
    try:
        bumped_ = [d_ for d_ in C.run(HERE)[1] if d_.file == "plugins.manifest.json"]
    finally:
        c_["version"] = orig_
    restored_ = [d_ for d_ in C.run(HERE)[1] if d_.file == "plugins.manifest.json"]
    check(f"manifest contract {cname_}: a manifest version bumped alone to 9.9.9 is exactly one drift naming plugins.{c_['plugin']}.contract.version (positive twin: restored, none)",
          restored_ == [] and len(bumped_) == 1 and bumped_[0].path == f"plugins.{c_['plugin']}.contract.version" and "manifest says 9.9.9" in bumped_[0].message, str([str(d_) for d_ in bumped_]))
    for t_ in c_.get("tests", []):                  # bespoke per-contract tests listed explicitly in the manifest (no glob): run in this namespace
        runpy.run_path(os.path.join(HERE, t_), init_globals=dict(globals()))


# ============================================================================================ mutation proofs (break the contract, the check must go red)


def mutated(fn, change):
    d = os.path.join(tmp, "m_" + str(abs(hash((fn, change.__name__)))))
    mkcontracts(d)
    p = os.path.join(d, "schemas", fn)
    s = json.load(open(p, encoding="utf-8"))
    change(s)
    json.dump(s, open(p, "w", encoding="utf-8"), indent=1)
    return d


def m_pps(s): s["properties"]["pps"]["type"] = "string"
def m_dsp(s): s["$defs"]["monitor"]["properties"]["dsp"]["maximum"] = 8
def m_top(s): s["$defs"]["tags"]["properties"]["top"]["maxItems"] = 2
def m_ver(s): s.pop("x-contract-version")
def m_kind(s): s["x-discriminator"]["map"].pop("dsp")
def m_loose(s): s["$defs"]["bird"]["properties"]["provenance"] = {}


base_n, base_d, _ = C.run(cdir)
check("mutation baseline: the unmutated contracts copy is clean", base_d == [], str([str(d) for d in base_d][:2]))
d_ = C.run(mutated("bridge_status.schema.json", m_pps))[1]
check("mutation: bridge_status pps typed as string -> the fixture is flagged at $.pps", any(d.file == "bridge_status.json" and d.path == "$.pps" and "is not of type 'string'" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("board_debug.schema.json", m_dsp))[1]
check("mutation: monitor dsp maximum 8 -> fixture records with dsp 9 are flagged", any(d.file == "board_debug.jsonl" and d.path == "$.dsp" and "greater than the maximum of 8" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("analysis_records.schema.json", m_top))[1]
check("mutation: tags.top maxItems 2 -> fixture tags with 3+ labels are flagged", any(d.file == "tags.jsonl" and d.path == "$.top" and "is too long" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("sink_status.schema.json", m_ver))[1]
check("mutation: schema without x-contract-version -> flagged", any(d.file == "sink_status.schema.json" and d.path == "x-contract-version" for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("analysis_records.schema.json", m_kind))[1]
check("mutation: a record kind removed from the schema -> the dsp fixture records are unknown-kind drift", any(d.file == "dsp.jsonl" and "unknown record kind 'dsp'" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = mutated("analysis_records.schema.json", m_loose)
sloose = C.load_schemas(os.path.join(d_, "schemas"))
bad_bird = mod(bd_, provenance="ground_truth")
check("mutation of the tests: with `provenance` loosened the malformed bird IS accepted (so the negative test above can fail)",
      C.check_record("birds.jsonl", "1", bad_bird, sloose["analysis-records"], ["bird"], {}) == [] and rejected(drifts("analysis-records", bad_bird, "birds.jsonl"), "$.provenance", "'model_opinion' was expected"))
cl_dir = os.path.join(tmp, "cl")
mkcontracts(cl_dir)
cl_path = os.path.join(cl_dir, "CHANGELOG.md")
cl_text = open(cl_path, encoding="utf-8").read()
open(cl_path, "w", encoding="utf-8").write(cl_text.replace("bridge-status 1.0.0", "bridge-status 0.9.0"))
d_ = C.run(cl_dir)[1]
check("mutation: CHANGELOG without `bridge-status 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "bridge-status" for d in d_), str([str(d) for d in d_][:2]))
sp_dir = os.path.join(tmp, "sp")
mkcontracts(sp_dir)
sp_path = os.path.join(sp_dir, "specs", "bridge-status.md")
sp_text = open(sp_path, encoding="utf-8").read()
open(sp_path, "w", encoding="utf-8").write(sp_text.replace("contract_version: 1.0.0", "contract_version: 1.1.0", 1))
d_ = C.run(sp_dir)[1]
check("mutation: spec version 1.1.0 vs schema 1.0.0 -> flagged", any(d.file == "bridge-status.md" and "spec says 1.1.0, schema says 1.0.0" in d.message for d in d_), str([str(d) for d in d_][:2]))
fx_dir = os.path.join(tmp, "fx")
mkcontracts(fx_dir)
bp = os.path.join(fx_dir, "fixtures", "bmaa_examples.json")
bj = json.load(open(bp))
bj["invalid"][0]["code"] = "length"
json.dump(bj, open(bp, "w"))
d_ = C.run(fx_dir)[1]
check("mutation: a binary example claiming the wrong rejection code -> flagged", any(d.file == "bmaa_examples.json" and "rejected with code 'magic', expected 'length'" in d.message for d in d_), str([str(d) for d in d_][:2]))
def m_bn(s): s["$defs"]["beam_bands"]["properties"]["n"]["const"] = 44100
def m_edges(s): s["$defs"]["beam_bands"]["properties"]["edges_hz"]["const"][0] = [40, 125]
def m_dbmax(s): s["$defs"]["beam_bands"]["properties"]["db"]["items"]["items"]["maximum"] = -30.0


d_ = C.run(mutated("beam_bands.schema.json", m_bn))[1]
check("mutation: beam_bands n pinned to 44100 -> fixture records flagged at $.n", any(d.file == "beam_bands.jsonl" and d.path == "$.n" and "44100 was expected" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("beam_bands.schema.json", m_edges))[1]
check("mutation: beam_bands first band edge changed -> fixture records and the kind-4 frame are flagged",
      any(d.file == "beam_bands.jsonl" and d.path == "$.edges_hz" for d in d_) and any(d.file == "cable_frames.json" and d.path == "payload.edges_hz" for d in d_), str([str(d) for d in d_][:3]))
d_ = C.run(mutated("beam_bands.schema.json", m_dbmax))[1]
check("mutation: beam_bands level ceiling -30 dB -> the -3.0 dB fixture is flagged", any(d.file == "beam_bands.jsonl" and "greater than the maximum of -30.0" in d.message for d in d_), str([str(d) for d in d_][:2]))
k4 = os.path.join(tmp, "k4")
mkcontracts(k4)
cfp = os.path.join(k4, "fixtures", "cable_frames.json")
cfj = json.load(open(cfp))
bad4 = json.loads(bytes.fromhex(cfj["frames"][3]["payload_hex"]))
bad4["v"] = 2
payload4 = json.dumps(bad4, separators=(",", ":")).encode()
cfj["frames"][3]["payload_hex"] = payload4.hex()
cfj["frames"][3]["frame_hex"] = R.encode_frame(4, payload4).hex()
json.dump(cfj, open(cfp, "w"))
d_ = C.run(k4)[1]
check("mutation: a kind-4 data-track frame whose JSON payload breaks the beam-bands schema (v 2) is flagged at payload.v",
      any(d.file == "cable_frames.json" and d.path == "payload.v" and "1 was expected" in d.message for d in d_), str([str(d) for d in d_][:2]))
k5 = os.path.join(tmp, "k5")
mkcontracts(k5)
ccp = os.path.join(k5, "fixtures", "cable_columns.json")
ccj = json.load(open(ccp))
ccj["cases"][1]["columns"][0] += 1            # a 1.x style column: low bits populated, and no longer the index's field
json.dump(ccj, open(ccp, "w"))
d_ = C.run(k5)[1]
check("mutation: a cable_columns case whose count has low bits set (and is not the index's field) is flagged twice (round trip, grid)",
      sorted(d.path for d in d_ if d.file == "cable_columns.json") == ["columns", "index"] and all(d.where == "1" for d in d_ if d.file == "cable_columns.json"), str([str(d) for d in d_][:3]))
nofx = os.path.join(tmp, "nofx")
mkcontracts(nofx)
os.remove(os.path.join(nofx, "fixtures", "clap.jsonl"))
d_ = C.run(nofx)[1]
check("mutation: a missing fixture file -> flagged", any(d.file == "clap.jsonl" and d.message == "fixture file is missing" for d in d_), str([str(d) for d in d_][:2]))

# ============================================================================================ run-history 1.0.0 (history files of the pipeline itself)
SH, AH, BH, RL = "sink_history.jsonl", "analyzer_history.jsonl", "board_history.jsonl", "run_ledger.jsonl"
sink0, ana0 = jl(SH)[0], jl(AH)[0]
smp, boot = first(BH, "k", "board_sample", seq=10778), first(BH, "k", "board_boot", prev_boot_id=None)
boot2 = first(BH, "k", "board_boot", prev_last_up=130970.72)
ev_exit, ev_sup = first(RL, "k", "run_event", ev="exit", exit_code=-1073741819), first(RL, "k", "run_event", ev="suppressed")
check("run-history fixtures: 3 sink samples, 2 analyzer samples, 6 board lines (2 boots), 8 ledger events",
      (len(jl(SH)), len(jl(AH)), len(jl(BH)), len(jl(RL))) == (3, 2, 6, 8), str((len(jl(SH)), len(jl(AH)), len(jl(BH)), len(jl(RL)))))
check("run-history fixtures: the restarted sink shows a new started_t and zeroed counters; playing false with the device error",
      (jl(SH)[2]["started_t"], jl(SH)[2]["holes"], jl(SH)[2]["playing"], jl(SH)[2]["device_error"]) == (1792884045.0, 0, False, "no CABLE In 16 device"))
check("run-history fixtures: the reboot shows up as a second board_boot with prev_last_up 130970.72 s and a 240 s gap, sensors null on its first sample",
      (boot2["boot_id"], boot2["prev_boot_id"], boot2["prev_last_up"], boot2["gap_s"], jl(BH)[4]["temp_local_c"]) == (1792884287, 1792753089, 130970.72, 240.0, None))
check("run-history fixtures: ledger holds the unknown exit code, the negative code, the 127 code and one suppressed record of count 1",
      (first(RL, "k", "run_event", proc="ui")["exit_code_known"], ev_exit["exit_code_known"], ev_sup["count"]) == (False, True, 1) and
      sum(1 for r in jl(RL) if r["exit_code"] == 127) == 2)
reject_pair("sink_sample holes negative", "run-history", SH, sink0, mod(sink0, holes=-1), "$.holes", "less than the minimum of 0")
reject_pair("sink_sample playing must be boolean", "run-history", SH, sink0, mod(sink0, playing="yes"), "$.playing", "is not of type 'boolean'")
reject_pair("sink_sample needs started_t", "run-history", SH, sink0, mod(sink0, started_t=KeyError), "$", "'started_t' is a required property")
reject_pair("sink_sample version v", "run-history", SH, sink0, mod(sink0, v=2), "$.v", "1 was expected")
reject_pair("analyzer_sample plugin counters are exactly 4", "run-history", AH, ana0, mod(ana0, plugins={"dsp": [1, 2, 3]}), "$.plugins.dsp", "too short")
reject_pair("analyzer_sample rx.gaps integer", "run-history", AH, ana0, mod(ana0, rx={"datagrams": 1, "bad": 0, "gaps": 1.5}), "$.rx.gaps", "is not of type 'integer'")
reject_pair("board_sample negative uptime", "run-history", BH, smp, mod(smp, up=-1.0), "$.up", "less than the minimum of 0")
reject_pair("board_sample boot_id is an integer", "run-history", BH, smp, mod(smp, boot_id=1792753089.5), "$.boot_id", "is not of type 'integer'")
reject_pair("board_sample temperatures may be null but not strings", "run-history", BH, mod(smp, temp_local_c=None), mod(smp, temp_local_c="40"), "$.temp_local_c", "is not of type")
reject_pair("board_boot prev_last_up negative", "run-history", BH, boot2, mod(boot2, prev_last_up=-5), "$.prev_last_up", "less than the minimum of 0")
reject_pair("run_event ev enum", "run-history", RL, ev_exit, mod(ev_exit, ev="died"), "$.ev", "is not one of")
reject_pair("run_event suppressed needs count", "run-history", RL, ev_sup, mod(ev_sup, count=KeyError), "$", "'count' is a required property")
reject_pair("run_event exit code present while exit_code_known is false", "run-history", RL, first(RL, "k", "run_event", proc="ui"),
            mod(first(RL, "k", "run_event", proc="ui"), exit_code=0), "$.exit_code", "None was expected")
reject_pair("run_event exit_code_known true needs an integer code", "run-history", RL, ev_exit, mod(ev_exit, exit_code=None), "$.exit_code", "is not of type 'integer'")
check("run-history: a sink_sample in analyzer_history.jsonl is flagged as not allowed in that file",
      any("is not allowed in analyzer_history.jsonl" in d.message for d in drifts("run-history", sink0, AH)), str([str(d) for d in drifts("run-history", sink0, AH)][:1]))
check("run-history: the same sink_sample in sink_history.jsonl is clean (positive twin)", drifts("run-history", sink0, SH) == [])
# the production writers agree with the fixtures they generated (a change to a producer without a contract change breaks these)
st0 = {k: sink0[k] for k in ("holes", "slips", "slipped_frames", "underruns", "missing_slot_blocks", "data_dropped", "data_frames", "playing", "audio_output",
                             "device_error", "buffered_ms", "datagrams", "bad")}
st0.update(t=sink0["t"], device="CABLE In 16ch (VB-Audio Virtual Cable)")
check("run-history: bma_cable_sink.build_sink_sample reproduces the first sink fixture line exactly", S.build_sink_sample(st0, sink0["started_t"]) == sink0)
sys.path.insert(0, os.path.join(BMA, "analysis"))
import analyzer_history as AHM  # noqa: E402
st_a = {"started": ana0["started"], "t": ana0["t"], "uptime_s": ana0["uptime_s"], "device": ana0["device"], "rx": ana0["rx"], "contiguous_s": ana0["contiguous_s"],
        "embeddings_stored": ana0["embeddings_stored"],
        "plugins": {n: {"enabled": True, "windows": c[0], "records": c[1], "errors": c[2], "dropped": c[3]} for n, c in ana0["plugins"].items()}}
check("run-history: analyzer_history.build_analyzer_sample reproduces the first analyzer fixture line exactly", AHM.build_analyzer_sample(st_a) == ana0)
# live check over a synthetic root with the history files
root_h = os.path.join(tmp, "live_hist")
mkroot(root_h)
os.makedirs(os.path.join(root_h, "history"))
for fn in (SH, AH, BH, RL):
    shutil.copy(os.path.join(FIX, fn), os.path.join(root_h, "history", fn))
_n, _d, _notes = C.check_live(root_h, SCH, 100)
root_h0 = os.path.join(tmp, "live_hist0")
mkroot(root_h0)
_n0, _d0, _notes0 = C.check_live(root_h0, SCH, 100)
check("live check: the four history files at their fixture content are 0 drift and add 3 + 2 + 6 + 8 = 19 records to the root without them",
      _d == [] and _d0 == [] and _n == _n0 + 19, str(([str(d) for d in _d][:2], _n, _n0)))
with open(os.path.join(root_h, "history", SH), "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(sink0, holes=-3)) + "\n")
_n, _d, _ = C.check_live(root_h, SCH, 100)
check("live check: an appended bad sink_sample is reported with file, kind, path",
      len(_d) == 1 and (_d[0].file, _d[0].kind, _d[0].path) == ("history/sink_history.jsonl", "sink_sample", "$.holes") and "less than the minimum of 0" in _d[0].message, str([str(d) for d in _d]))
check("live check: absent history files are notes, not drift (the producers are not deployed yet)",
      {"history/analyzer_history.jsonl", "history/board_history.jsonl", "history/run_ledger.jsonl", "history/sink_history.jsonl"} <= {n_.split(":")[0] for n_ in _notes0})


def m_hist_holes(s): s["$defs"]["sink_sample"]["properties"]["holes"]["maximum"] = 10
def m_hist_files(s): s["x-files"]["sink_history.jsonl"] = ["analyzer_sample"]
def m_hist_ver(s): s.pop("x-contract-version")


d_ = C.run(mutated("run_history.schema.json", m_hist_holes))[1]
check("mutation: sink_sample holes maximum 10 -> the 41-holes fixture is flagged at $.holes", any(d.file == SH and d.path == "$.holes" and "greater than the maximum of 10" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("run_history.schema.json", m_hist_files))[1]
check("mutation: sink_history.jsonl declared to hold analyzer_sample -> its fixture lines are flagged as not allowed", any(d.file == SH and "is not allowed" in d.message for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("run_history.schema.json", m_hist_ver))[1]
check("mutation: run_history schema without x-contract-version -> flagged", any(d.file == "run_history.schema.json" and d.path == "x-contract-version" for d in d_), str([str(d) for d in d_][:2]))
nocl = os.path.join(tmp, "nocl")
mkcontracts(nocl)
_cl = open(os.path.join(nocl, "CHANGELOG.md"), encoding="utf-8").read().replace("run-history 1.0.0", "run-history x")
open(os.path.join(nocl, "CHANGELOG.md"), "w", encoding="utf-8").write(_cl)
d_ = C.run(nocl)[1]
check("mutation: the CHANGELOG without a run-history 1.0.0 line -> flagged", any(d.file == "CHANGELOG.md" and d.path == "run-history" for d in d_), str([str(d) for d in d_][:2]))

# ============================================================================================ analysis-records 1.2.0 / analysis-status 1.1.0 / analysis-history 1.0.0 (lane L2)
CFG, HRL, DLY = "config_history.jsonl", "hourly_rollup.jsonl", "daily_summary.json"
tp_new = first(DSP, "k", "tonal_appeared", beam=4)
tg_new = first(DSP, "k", "tonal_gone", beam=4)
ts_new = first(DSP, "k", "tonal_state", noise_floor_db=-71.3)
sp_new = first("spatial.jsonl", "k", "spatial", decorr=0.412)
bd_new = first("birds.jsonl", "k", "bird", group="bird")
bf_new = first("birds.jsonl", "k", "bird_filtered", reject_reason="geo_not_predicted")
nv_new = first("novelty.jsonl", "k", "novelty", run=1793029538, seen=63200)
reject_pair("tonal_appeared beam 9", "analysis-records", DSP, tp_new, mod(tp_new, beam=9), "$.beam", "greater than the maximum of 8")
check("tonal_appeared with a null beam and an old row without any beam are both valid (additive, backward compatible)",
      drifts("analysis-records", mod(tp_new, beam=None), DSP) == [] and drifts("analysis-records", first(DSP, "k", "tonal_appeared"), DSP) == [])
reject_pair("tonal_gone beam 0", "analysis-records", DSP, tg_new, mod(tg_new, beam=0), "$.beam", "less than the minimum of 1")
bad_line = copy.deepcopy(ts_new)
bad_line["lines"][1]["beam"] = "5"
check("tonal_state line beam must be an integer or null", rejected(drifts("analysis-records", bad_line, DSP), "$.lines[1].beam", "is not of type"))
check("tonal_state with beams on its lines is valid (positive twin)", drifts("analysis-records", ts_new, DSP) == [])
reject_pair("spatial decorr above 1", "analysis-records", "spatial.jsonl", sp_new, mod(sp_new, decorr=1.2), "$.decorr", "greater than the maximum of 1")
reject_pair("bird group is a string or null", "analysis-records", "birds.jsonl", bd_new, mod(bd_new, group=5), "$.group", "is not of type")
check("bird with group null (taxa table missing) is valid", drifts("analysis-records", mod(bd_new, group=None), "birds.jsonl") == [])
reject_pair("bird_filtered reject_reason is geo_not_predicted", "analysis-records", "birds.jsonl", bf_new, mod(bf_new, reject_reason="other"), "$.reject_reason", "'geo_not_predicted' was expected")
reject_pair("novelty run is an integer", "analysis-records", "novelty.jsonl", nv_new, mod(nv_new, run=1.5), "$.run", "is not of type")
whole_pair("status.json meta counters are integers >= 0", "analysis-status", st, mod(st, meta=dict(st["meta"], write_errors=-1)), "$.meta.write_errors", "less than the minimum of 0")
check("status.json without meta is still valid (an older analyzer)", whole("analysis-status", {k: v for k, v in st.items() if k != "meta"}) == [])
whole_pair("status.json meta needs all eight counters", "analysis-status", st, mod(st, meta={"write_errors": 0}), "$.meta", "is a required property")

cfg0, hr0 = jl(CFG)[0], jl(HRL)[0]
dly0 = jf(DLY)
reject_pair("analysis_config mask sha12 is 12 hex chars or null", "analysis-history", CFG, cfg0, mod(cfg0, mask=dict(cfg0["mask"], sha12="xyz")), "$.mask.sha12", "does not match")
reject_pair("analysis_config calibration source enum", "analysis-history", CFG, cfg0, mod(cfg0, calibration={"source": "board"}), "$.calibration.source", "is not one of")
reject_pair("analysis_config calibration azimuth needs 8 entries", "analysis-history", CFG, cfg0, mod(cfg0, calibration=dict(cfg0["calibration"], azimuth_deg=[1, 2, 3])), "$.calibration.azimuth_deg", "too short")
reject_pair("analysis_config never carries a location", "analysis-history", CFG, cfg0, mod(cfg0, geo=dict(cfg0["geo"], lat=10.5)), "$.geo", "should not be valid under")
reject_pair("analysis_config never carries a cell hash", "analysis-history", CFG, cfg0, mod(cfg0, geo=dict(cfg0["geo"], cell_sha12="0123456789ab")), "$.geo", "should not be valid under")
reject_pair("analysis_config beam_shift needs 8 integers within +-4", "analysis-history", CFG, mod(cfg0, beam_shift=[1, 0, 0, 0, 0, 0, 0, -1]), mod(cfg0, beam_shift=[1, 0, 0, 0, 0, 0, 0]), "$.beam_shift", "too short")
reject_pair("analysis_config beam_shift entries are at most 4 in magnitude", "analysis-history", CFG, mod(cfg0, beam_shift=[4, 0, 0, 0, 0, 0, 0, -4]), mod(cfg0, beam_shift=[5, 0, 0, 0, 0, 0, 0, 0]), "$.beam_shift[0]", "greater than the maximum of 4")
reject_pair("analysis_config run_id is an integer", "analysis-history", CFG, cfg0, mod(cfg0, run_id=1.5), "$.run_id", "is not of type 'integer'")
reject_pair("hourly_rollup hour_s is on an hour boundary", "analysis-history", HRL, hr0, mod(hr0, hour_s=hr0["hour_s"] + 1), "$.hour_s", "is not a multiple of 3600")
reject_pair("hourly_rollup rms needs 8 beams", "analysis-history", HRL, hr0, mod(hr0, rms_dbfs_mean=hr0["rms_dbfs_mean"][:7]), "$.rms_dbfs_mean", "too short")
reject_pair("hourly_rollup spatial decorr_mean within 0..1", "analysis-history", HRL, hr0, mod(hr0, spatial=dict(hr0["spatial"], decorr_mean=1.5)), "$.spatial.decorr_mean", "greater than the maximum of 1")
many = mod(hr0, tonal=dict(hr0["tonal"], lines=[hr0["tonal"]["lines"][0]] * 31))
check("hourly_rollup keeps at most 30 tonal lines", rejected(drifts("analysis-history", many, HRL), "$.tonal.lines", "too long"))
check("an analysis_config record in hourly_rollup.jsonl is flagged as not allowed in that file",
      any("is not allowed in hourly_rollup.jsonl" in d.message for d in drifts("analysis-history", cfg0, HRL)))
bad_day = copy.deepcopy(dly0)
bad_day["days"]["20251026"]["audio_s"] = -1.0
_dly_schema = {k: v for k, v in SCH["analysis-history"].items() if k != "oneOf"}
_dly_schema["$ref"] = "#/$defs/daily_summary"          # validate the file against its own definition: precise paths, not "not valid under any"


def whole_daily(rec):
    return [(e.json_path, e.message) for e in C.errors(C.whole_validator(_dly_schema), rec)]


def daily_pair(name, good, bad, path, part):
    errs = whole_daily(bad)
    check(f"{name}: rejected at {path} ({part!r})", any(p == path and part in m for p, m in errs), str(errs[:2]))
    check(f"{name}: positive twin passes", whole_daily(good) == [], str(whole_daily(good)[:2]))


daily_pair("daily_summary audio_s >= 0", dly0, bad_day, "$.days['20251026'].audio_s", "less than the minimum of 0")
bad_key = copy.deepcopy(dly0)
bad_key["days"]["2025-10-26"] = bad_key["days"].pop("20251026")
daily_pair("daily_summary day keys are YYYYMMDD", dly0, bad_key, "$.days", "does not match")
check("daily_summary is also accepted by the discriminated whole-file schema (what the live check uses)", whole("analysis-history", dly0) == [])
# the fixtures are what the real producers write (a producer change without a fixture and contract change breaks this)
sys.path.insert(0, os.path.join(HERE, "tools"))
import make_analysis_history_fixtures as MAF  # noqa: E402
_regen = MAF.build(os.path.join(tmp, "regen_afix"))
check("analysis-history fixtures equal the output of the real producers byte for byte",
      all(open(os.path.join(_regen, f), "rb").read() == open(os.path.join(FIX, f), "rb").read() for f in (CFG, HRL, DLY)))
root_a = os.path.join(tmp, "live_ahist")
mkroot(root_a)
os.makedirs(os.path.join(root_a, "history"))
for fn in (CFG, HRL, DLY):
    shutil.copy(os.path.join(FIX, fn), os.path.join(root_a, "history", fn))
_na, _da, _notes_a = C.check_live(root_a, SCH, 100)
root_a0 = os.path.join(tmp, "live_ahist0")
mkroot(root_a0)
_na0, _da0, _ = C.check_live(root_a0, SCH, 100)
check("live check: the three analysis-history files at fixture content are 0 drift and add 1 + 1 + 1 records", _da == [] and _da0 == [] and _na == _na0 + 3, str(([str(d) for d in _da][:2], _na, _na0)))
with open(os.path.join(root_a, "history", CFG), "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(cfg0, run_id=-1)) + chr(10))
_na, _da, _ = C.check_live(root_a, SCH, 100)
check("live check: an appended analysis_config with run_id -1 is reported with file, kind, path",
      len(_da) == 1 and (_da[0].file, _da[0].kind, _da[0].path) == ("history/config_history.jsonl", "analysis_config", "$.run_id") and "less than the minimum of 0" in _da[0].message, str([str(d) for d in _da]))

# ============================================================================================ clips 1.1.0 / analysis-status 2.0.0 (wildlife clips, analysis/clips.py)
CIX, CSC = "clips_index.jsonl", "clip_sidecar.json"
st_c = jf("analysis_status_clips.json")
whole_pair("status.json audio_written is a boolean (2.0.0; 1.x had the constant false)", "analysis-status", mod(st, audio_written=True), mod(st, audio_written="yes"), "$.audio_written", "is not of type 'boolean'")
check("status.json with a clips block (from the real service) is valid and says audio_written true with 2 audio files", whole("analysis-status", st_c) == [] and st_c["audio_written"] is True and st_c["clips"]["audio_files"] == 2, str(whole("analysis-status", st_c)[:2]))
whole_pair("status.json clips.stored is an integer >= 0", "analysis-status", st_c, mod(st_c, clips=dict(st_c["clips"], stored=-1)), "$.clips.stored", "less than the minimum of 0")
whole_pair("status.json clips needs queue_depth", "analysis-status", st_c, mod(st_c, clips={k: v for k, v in st_c["clips"].items() if k != "queue_depth"}), "$.clips", "'queue_depth' is a required property")
c_st, c_ev, c_ae, c_se = (first(CIX, "k", k) for k in ("clip_stored", "clip_evicted", "clip_audio_expired", "clip_sidecar_expired"))
csc, csx = jf(CSC), jf("clip_sidecar_audio_expired.json")
reject_pair("clip_stored confidence_max must be above 0 (the threshold is configurable since 1.3.0)", "clips", CIX, c_st, mod(c_st, confidence_max=0), "$.confidence_max", "less than or equal to the minimum of 0")
check("clips 1.3.0: confidence_max 0.85 and 0.3 are accepted (no fixed 0.85 floor; twin of the 0 rejection)", drifts("clips", mod(c_st, confidence_max=0.85), CIX) == [] and drifts("clips", mod(c_st, confidence_max=0.3), CIX) == [], "")
CIXL = "clips_index_lowconf.jsonl"
c_lc = [r for r in jl(CIXL) if r["k"] == "clip_stored"]
check("clips 1.3.0 low-confidence fixture from the real writer (--clip-min-conf 0.3): one clip_stored with confidence_max exactly 0.31, valid", len(jl(CIXL)) == 1 and len(c_lc) == 1 and c_lc[0]["confidence_max"] == 0.31 and drifts("clips", c_lc[0], CIXL) == [], str(jl(CIXL)))
reject_pair("clip_stored group must be a wildlife group (null is not stored)", "clips", CIX, c_st, mod(c_st, group=None), "$.group", "is not one of")
reject_pair("clip_stored group mechanical is not stored", "clips", CIX, c_st, mod(c_st, group="mechanical"), "$.group", "is not one of")
reject_pair("clip_stored audio_file is relative (an absolute path is rejected)", "clips", CIX, c_st, mod(c_st, audio_file="/20261014/1791997814000-strixvaria-b1.flac"), "$.audio_file", "does not match")
reject_pair("clip_stored provenance is model_opinion", "clips", CIX, c_st, mod(c_st, provenance="measured"), "$.provenance", "'model_opinion' was expected")
reject_pair("clip_stored duration at most 12 s", "clips", CIX, c_st, mod(c_st, duration_s=12.5), "$.duration_s", "greater than the maximum of 12.0")
reject_pair("clip_stored beams_agreeing is at least 1 (0 rejected)", "clips", CIX, c_st, mod(c_st, beams_agreeing=0), "$.beams_agreeing", "less than the minimum of 1")
reject_pair("clip_stored beams_agreeing is at most 8 beams (9 rejected)", "clips", CIX, c_st, mod(c_st, beams_agreeing=9), "$.beams_agreeing", "greater than the maximum of 8")
reject_pair("clip_stored needs beams_agreeing (the beam-agreement decision is recorded)", "clips", CIX, c_st, {k: v for k, v in c_st.items() if k != "beams_agreeing"}, "$", "'beams_agreeing' is a required property")
check("clip_stored fixtures from the real writer (default --clip-min-beams 2) all say beams_agreeing 2", [r["beams_agreeing"] for r in jl(CIX) if r["k"] == "clip_stored"] == [2, 2, 2, 2], str([r.get("beams_agreeing") for r in jl(CIX)]))
reject_pair("clip_audio_expired reason enum", "clips", CIX, c_ae, mod(c_ae, reason="because"), "$.reason", "is not one of")
reject_pair("clip_evicted reason", "clips", CIX, c_ev, mod(c_ev, reason="age"), "$.reason", "is not one of")
check("clip_evicted accepts reason human_voice (1.1.0); the fixture holds exactly species_hour_cap and human_voice", drifts("clips", mod(c_ev, reason="human_voice"), CIX) == [] and [r["reason"] for r in jl(CIX) if r["k"] == "clip_evicted"] == ["species_hour_cap", "human_voice"], str([r for r in jl(CIX) if r["k"] == "clip_evicted"]))
reject_pair("clip_sidecar_expired id is <13 digits>-<slug>-b<beam>", "clips", CIX, c_se, mod(c_se, id="abc"), "$.id", "does not match")
check("a clip sidecar record in index.jsonl is flagged as not allowed in that file", any("is not allowed in clips_index.jsonl" in d.message for d in drifts("clips", csc, CIX)))
whole_pair("clip sidecar: audio_expired false needs an audio_file", "clips", csc, mod(csc, audio_file=None), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: audio_expired true needs audio_file null (the mutation: expired but a file named)", "clips", csx, mod(csx, audio_file=csc["audio_file"]), "$", "is not valid under any of the given schemas")
check("clip sidecar after audio expiry has audio_expired true, audio_file null and keeps the png", csx["audio_expired"] is True and csx["audio_file"] is None and csx["png"] == csc["png"])
whole_pair("clip sidecar: speech_guard.human_labels_seen is always false", "clips", csc, mod(csc, speech_guard=dict(csc["speech_guard"], human_labels_seen=True)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: beams_agreeing is required", "clips", csc, {k: v for k, v in csc.items() if k != "beams_agreeing"}, "$", "is not valid under any of the given schemas")
check("clip sidecar: beams_agreeing equals the number of beams_reported (2 of [1, 2]) in both fixtures", csc["beams_agreeing"] == len(csc["beams_reported"]) == 2 and csx["beams_agreeing"] == len(csx["beams_reported"]) == 2, str((csc["beams_agreeing"], csc["beams_reported"])))
whole_pair("clip sidecar: 3 band energies (4-8, 8-16, 16-24 kHz; nothing below the 3.5 kHz floor)", "clips", csc, mod(csc, signatures=dict(csc["signatures"], band_db=csc["signatures"]["band_db"][:2])), "$", "is not valid under any of the given schemas")
CIXV, CSV = "clips_index_visual.jsonl", "clip_sidecar_visual.json"
cv_st, csv = first(CIXV, "k", "clip_stored"), jf(CSV)
st_cv = jf("analysis_status_clips_visual.json")
check("visual-only (the default) fixtures from the real writer: the index record and the sidecar say audio_stored false, audio_file null, codec null, audio_bytes 0, audio_expired false; the status says audio_written false, clips.audio false, audio_files 0",
      (cv_st["audio_stored"], cv_st["audio_file"], cv_st["codec"], cv_st["audio_bytes"]) == (False, None, None, 0) and (csv["audio_stored"], csv["audio_file"], csv["codec"], csv["audio_bytes"], csv["audio_expired"]) == (False, None, None, 0, False)
      and st_cv["audio_written"] is False and st_cv["clips"]["audio"] is False and st_cv["clips"]["audio_files"] == 0 and whole("analysis-status", st_cv) == [], str((cv_st, st_cv["clips"])))
reject_pair("clip_stored visual-only must not name an audio file (audio_stored false)", "clips", CIXV, cv_st, mod(cv_st, audio_file="20261014/1791997838000-poecilecarolinensis-b1.flac"), "$.audio_file", "is not of type 'null'")
reject_pair("clip_stored visual-only has audio_bytes 0", "clips", CIXV, cv_st, mod(cv_st, audio_bytes=1000), "$.audio_bytes", "0 was expected")
reject_pair("clip_stored with audio_stored true needs an audio file", "clips", CIX, c_st, mod(c_st, audio_file=None), "$.audio_file", "is not of type")
reject_pair("clip_stored needs audio_stored", "clips", CIXV, cv_st, {k: v for k, v in cv_st.items() if k != "audio_stored"}, "$", "'audio_stored' is a required property")
whole_pair("clip sidecar visual-only: audio_expired true is not applicable (audio never existed)", "clips", csv, mod(csv, audio_expired=True), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar with audio: audio_stored true and no audio file while audio_expired is false is rejected", "clips", csc, mod(csc, audio_file=None), "$", "is not valid under any of the given schemas")
CSP = "clip_sidecar_pcen.json"
csp = jf(CSP)
_pp = csp["spectrogram"]["pcen"]
check("clips 1.2.0 pcen fixture from the real writer: scale pcen, parameters exact (s 0.11743140267094843 = time constant 0.4 s at 20 columns/s), no db_lo/db_hi, valid",
      csp["spectrogram"]["scale"] == "pcen" and _pp == {"time_constant_s": 0.4, "s": 0.11743140267094843, "alpha": 0.98, "delta": 2.0, "r": 0.5, "eps": 1e-06, "power_scale": 1073741824.0, "init": "first", "vmax": 4.0}
      and "db_lo" not in csp["spectrogram"] and "db_hi" not in csp["spectrogram"] and whole("clips", csp) == [], str(csp["spectrogram"]))
check("clips 1.2.0 positive twin: the log fixture still has scale log, db_lo -110, db_hi -30 and no pcen object, valid",
      (csv["spectrogram"]["scale"], csv["spectrogram"]["db_lo"], csv["spectrogram"]["db_hi"], "pcen" in csv["spectrogram"]) == ("log", -110.0, -30.0, False) and whole("clips", csv) == [])
whole_pair("clip sidecar: scale pcen needs the pcen parameter object", "clips", csp, mod(csp, spectrogram={k: v for k, v in csp["spectrogram"].items() if k != "pcen"}), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: scale pcen has no db_lo/db_hi", "clips", csp, mod(csp, spectrogram=dict(csp["spectrogram"], db_lo=-110.0, db_hi=-30.0)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: scale log has no pcen object", "clips", csv, mod(csv, spectrogram=dict(csv["spectrogram"], pcen=_pp)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: scale log needs db_lo and db_hi", "clips", csv, mod(csv, spectrogram={k: v for k, v in csv["spectrogram"].items() if k != "db_lo"}), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: scale is log or pcen (mel rejected)", "clips", csv, mod(csv, spectrogram=dict(csv["spectrogram"], scale="mel")), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: pcen s is at most 1 (1.5 rejected)", "clips", csp, mod(csp, spectrogram=dict(csp["spectrogram"], pcen=dict(_pp, s=1.5))), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: the spectrogram floor is exactly 3500 Hz (100 Hz rejected)", "clips", csv, mod(csv, spectrogram=dict(csv["spectrogram"], f_lo_hz=100.0)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: the spectrogram has exactly 96 rows (300 rejected)", "clips", csv, mod(csv, spectrogram=dict(csv["spectrogram"], rows=300)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: the spectrogram column is 50 ms (10 ms rejected)", "clips", csv, mod(csv, spectrogram=dict(csv["spectrogram"], col_s=0.01)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: the envelope has no min/max pairs and runs at 50 ms steps (1 ms step rejected)", "clips", csv, mod(csv, envelope=dict(csv["envelope"], step_s=0.001)), "$", "is not valid under any of the given schemas")
whole_pair("clip sidecar: the envelope holds at most 238 values (an audio-rate envelope of 400 is rejected)", "clips", csv, mod(csv, envelope=dict(csv["envelope"], n=400, rms_db=[-60.0] * 400)), "$", "is not valid under any of the given schemas")
whole_pair("status.json clips needs the audio flag", "analysis-status", st_cv, mod(st_cv, clips={k: v for k, v in st_cv["clips"].items() if k != "audio"}), "$.clips", "'audio' is a required property")
check("clips: the live path analysis/clips/index.jsonl is registered as an optional live file", C.LIVE.get("analysis/clips/index.jsonl") == ("clips", True))
root_c = os.path.join(tmp, "live_clips")
mkroot(root_c)
os.makedirs(os.path.join(root_c, "analysis", "clips"))
shutil.copy(os.path.join(FIX, CIX), os.path.join(root_c, "analysis", "clips", "index.jsonl"))
_nc, _dc, _ = C.check_live(root_c, SCH, 100)
root_c0 = os.path.join(tmp, "live_clips0")
mkroot(root_c0)
_nc0, _dc0, _notes_c0 = C.check_live(root_c0, SCH, 100)
check("live check: the clips index at fixture content is 0 drift and adds its 10 records; without it a note, not drift", _dc == [] and _dc0 == [] and _nc == _nc0 + 10 and any(n_.startswith("analysis/clips/index.jsonl") for n_ in _notes_c0), str(([str(d) for d in _dc][:2], _nc, _nc0)))
with open(os.path.join(root_c, "analysis", "clips", "index.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(c_st, confidence_max=0)) + chr(10))
_nc, _dc, _ = C.check_live(root_c, SCH, 100)
check("live check: an appended clip_stored with confidence 0 is reported with file, kind, path",
      len(_dc) == 1 and (_dc[0].file, _dc[0].kind, _dc[0].path) == ("analysis/clips/index.jsonl", "clip_stored", "$.confidence_max"), str([str(d) for d in _dc]))


def m_clips_conf(s): s["$defs"]["conf"]["exclusiveMinimum"] = 0.9
def m_clips_ver(s): s.pop("x-contract-version")


d_ = C.run(mutated("clips.schema.json", m_clips_conf))[1]
check("mutation: clips confidence floor raised to 0.9 -> the 0.9 / 0.91 fixture records are flagged", any(d.file == CIX and d.path == "$.confidence_max" for d in d_), str([str(d) for d in d_][:2]))
d_ = C.run(mutated("clips.schema.json", m_clips_ver))[1]
check("mutation: clips schema without x-contract-version -> flagged", any(d.file == "clips.schema.json" and d.path == "x-contract-version" for d in d_), str([str(d) for d in d_][:2]))
nocl2 = os.path.join(tmp, "nocl2")
mkcontracts(nocl2)
_frag = os.path.join(nocl2, "changelog.d", "clips.md")        # the clips 1.3.0 entry is a changelog fragment
_ftxt = open(_frag, encoding="utf-8").read()
check("clips 1.3.0 changelog fragment names the version line the check looks for", "clips 1.3.0" in _ftxt, _ftxt[:80])
open(_frag, "w", encoding="utf-8").write(_ftxt.replace("clips 1.3.0", "clips x"))
d_ = C.run(nocl2)[1]
check("mutation: the changelog without a clips 1.3.0 line -> flagged", any(d.file == "CHANGELOG.md" and d.path == "clips" for d in d_), str([str(d) for d in d_][:2]))
_clspec = open(os.path.join(HERE, "specs", "clips.md"), encoding="utf-8").read()
check("clips spec says 1.0.0, strictly above 0.85, 24 hours, fail closed and names the sidecar retention decision", "contract_version: 1.3.0" in _clspec[:300] and "STRICTLY above" in _clspec and "24 hours" in _clspec and "fail closed" in _clspec and "--sidecar-retention-days" in _clspec)

# ============================================================================================ spectrum-packet (BMAS)
sx = jf("spectrum_examples.json")
check("BMAS: every valid example parses to its fields and the reference builder reproduces its bytes",
      all(R.parse_spectrum(bytes.fromhex(e["hex"]))["bins"] == e["bins"] and R.build_spectrum(e["tier"], e["flags"], e["beam"], e["row"], e["t_us"], e["bins"]) == bytes.fromhex(e["hex"]) for e in sx["valid"]))
check("BMAS: a standard row is 70 bytes and a detail row 124 (24-byte header + uint8 bands, no samples)",
      [len(bytes.fromhex(e["hex"])) for e in sx["valid"]] == [70, 70, 70, 70, 124])
codes = []
for e in sx["invalid"]:
    try:
        R.parse_spectrum(bytes.fromhex(e["hex"]))
        codes.append("ACCEPTED")
    except R.ContractError as ex:
        codes.append(ex.code)
check("BMAS: the ten invalid examples are rejected with exactly their stable codes",
      codes == ["short", "magic", "version", "tier", "flags", "beam", "hop", "n_bins", "length", "gated_not_coarse"], str(codes))
check("BMAS: band layout - 46 standard bands (50 Hz to 16 kHz) and 100 detail bands (0 to 16 kHz), contiguous",
      [len(R.spectrum_band_edges(t)) for t in (0, 1)] == [46, 100]
      and R.spectrum_band_edges(0)[0][0] == 50.0 and abs(R.spectrum_band_edges(0)[-1][1] - 16000.0) < 1e-6
      and all(abs(a[1] - b[0]) < 1e-9 for t in (0, 1) for a, b in zip(R.spectrum_band_edges(t), R.spectrum_band_edges(t)[1:])))
check("BMAS: quantization is round half even of (dB + 100) * 2 clamped to 0..255: -100 -> 0, -3.0103 -> 194, 0 -> 200, +27.5 -> 255, +40 -> 255, -120 -> 0",
      [R.quantize_db(x) for x in (-100.0, -3.0103, 0.0, 27.5, 40.0, -120.0)] == [0, 194, 200, 255, 255, 0])
half = [[200] * 46] * 5 + [[0] * 46] * 5
check("BMAS: coarsen_block of five rows at 0 dB and five at -100 dB is the power mean -3.01 dB, value 194, in every band (not the dB mean 100)",
      R.coarsen_block(0, half) == [194] * 46)
check("BMAS: coarsen_block output is constant inside every coarse group of the real example (so a gated row passes the parser)",
      R.parse_spectrum(R.build_spectrum(0, 1, 0, 0, 0, R.coarsen_block(0, sx["coarsen_example"]["rows"])))["gated"] is True)
try:
    R.coarsen_block(0, half[:9])
    check("BMAS: coarsen_block refuses a block of 9 rows", False)
except R.ContractError as ex:
    check("BMAS: coarsen_block refuses a block of 9 rows (positive twin: 10 rows accepted above)", ex.code == "coarsen_shape", ex.code)
sp_mut = os.path.join(tmp, "spx")
mkcontracts(sp_mut)
sp = os.path.join(sp_mut, "fixtures", "spectrum_examples.json")
sj = json.load(open(sp))
sj["valid"][0]["bins"][5] += 1
json.dump(sj, open(sp, "w"))
d_ = C.run(sp_mut)[1]
check("mutation: a spectrum example whose bins no longer match its hex -> flagged at `bins`", any(d.file == "spectrum_examples.json" and d.path == "bins" for d in d_), str([str(d) for d in d_][:2]))
check("mutation twin: the untouched copy of the contracts has no spectrum drift", not any("spectrum" in d.file for d in C.run(os.path.join(tmp, "fx"))[1]))
sj["valid"][0]["bins"][5] -= 1
sj["coarsen_example"]["result"][0] += 1
json.dump(sj, open(sp, "w"))
d_ = C.run(sp_mut)[1]
check("mutation: a wrong coarsen_example result -> flagged", any(d.file == "spectrum_examples.json" and d.path == "result" for d in d_), str([str(d) for d in d_][:2]))

# ============================================================================================ ui-live 1.0.0 (BMW1 frames and the WebSocket framing, specs/ui-live.md)
lv = jf("ui_live_examples.json")
check("ui-live: the 4 valid BMW1 frames parse to their fields, are rebuilt byte for byte and are 438 bytes",
      all(R.parse_live(bytes.fromhex(e["hex"]))["seq"] == e["seq"] and R.parse_live(bytes.fromhex(e["hex"]))["flags"] == e["flags"] and len(bytes.fromhex(e["hex"])) == 438
          and R.build_live(e["seq"], e["t_us"], e["flags"], [[f0] + [fl] * 45 for fl, f0 in e["beams"]]) == bytes.fromhex(e["hex"]) for e in lv["frames"]["valid"]) and len(lv["frames"]["valid"]) == 4)
_live_codes = []
for e in lv["frames"]["invalid"]:
    try:
        R.parse_live(bytes.fromhex(e["hex"]))
        _live_codes.append("accepted")
    except R.ContractError as ex:
        _live_codes.append(ex.code)
check("ui-live: the 9 invalid BMW1 frames are refused each with its exact code (positive twin: the valid ones above)", _live_codes == [e["code"] for e in lv["frames"]["invalid"]]
      and _live_codes == ["short", "magic", "type", "beams", "bins", "flags", "reserved", "length", "length"], str(_live_codes))
check("ui-live: the RFC 6455 handshake vector (dGhlIHNhbXBsZSBub25jZQ== -> s3pPLMBiTxaQ9kYGzzhZRbK+xOo=) and the flag bits gated 1, gap 2, zero 4, missing 8",
      R.ws_accept_key(lv["handshake"]["key"]) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" and (R.LIVE_FLAG_GATED, R.LIVE_FLAG_GAP, R.LIVE_FLAG_ZERO, R.LIVE_FLAG_MISSING) == (1, 2, 4, 8))
check("ui-live: server frames reproduce (the 438-byte binary frame uses the 16-bit length: header 0x82 0x7e 0x01 0xb6)",
      all(R.ws_encode(e["opcode"], bytes.fromhex(e["payload_hex"])) == bytes.fromhex(e["hex"]) for e in lv["server_frames"]) and lv["server_frames"][-1]["hex"][:8] == "827e01b6")
_cc = []
for e in lv["client_frames"]["invalid"]:
    try:
        R.ws_decode_client(bytes.fromhex(e["hex"]))
        _cc.append("accepted")
    except R.ContractError as ex:
        _cc.append(ex.code)
check("ui-live: the 6 invalid client frames are refused with unmasked, reserved_bits, fragmented, opcode, opcode, payload_too_big; the 4 valid ones round trip",
      _cc == ["unmasked", "reserved_bits", "fragmented", "opcode", "opcode", "payload_too_big"]
      and all(R.ws_decode_client(bytes.fromhex(e["hex"])) == (e["opcode"], bytes.fromhex(e["payload_hex"]), len(bytes.fromhex(e["hex"]))) for e in lv["client_frames"]["valid"]), str(_cc))
lv_mut = os.path.join(tmp, "lvx")
mkcontracts(lv_mut)
lvp = os.path.join(lv_mut, "fixtures", "ui_live_examples.json")
lj = json.load(open(lvp))
lj["frames"]["valid"][0]["seq"] += 1
json.dump(lj, open(lvp, "w"))
d_ = C.run(lv_mut)[1]
check("mutation: a BMW1 example whose seq no longer matches its hex -> flagged at `seq`", any(d.file == "ui_live_examples.json" and d.path == "seq" for d in d_), str([str(d) for d in d_][:2]))
check("mutation twin: the untouched copy of the contracts has no ui-live drift", not any("ui_live" in d.file for d in C.run(os.path.join(tmp, "fx"))[1]))
lj["frames"]["valid"][0]["seq"] -= 1
lj["text"][1]["json"]["extra"] = 1
json.dump(lj, open(lvp, "w"))
d_ = C.run(lv_mut)[1]
check("mutation: a text frame with an extra field -> flagged (the schema is closed)", any(d.file == "ui_live_examples.json" and d.where == "reset" for d in d_), str([str(d) for d in d_][:2]))

# ============================================================================================ beam-coherence 1.0.0 (analysis/coherence.py)
BCF, BCL = "beam_coherence.jsonl", "coherence.jsonl"                  # fixture name, live name (the x-files key)
bcs = jl(BCF)
bc_q = next(r for r in bcs if r["bands"][0]["quiet"] and r["win_gated"] == 1)         # the minute with a gated window
bc_a = next(r for r in bcs if r["bands"][7]["active"])
check("beam-coherence fixture: 5 records of kind beam_coherence from the real plugin, validating against the schema", len(bcs) == 5 and all(not drifts("beam-coherence", r, BCL) for r in bcs))
check("beam-coherence fixture: it holds a gated window, quiet cells, active cells, a thin (null, n_quiet 3) cell and msc_unmasked_p50 in 8-16 kHz only",
      bc_q["win_gated"] == 1 and bc_a["bands"][7]["active"]["n"] > 5 and any(b["quiet"] is None and b["n_quiet"] == 3 for r in bcs for b in r["bands"])
      and "msc_unmasked_p50" in bc_a["bands"][7]["active"] and "msc_unmasked_p50" not in bc_a["bands"][3]["active"] and bc_a["mask_hz"] == [11510.7, 12000.0, 15334.0], str(bc_q["win_gated"]))


def with_cell(rec, band, cond, **kw):
    r = copy.deepcopy(rec)
    r["bands"][band][cond].update(kw)
    return r


reject_pair("beam_coherence version v", "beam-coherence", BCL, bc_q, mod(bc_q, v=2), "$.v", "1 was expected")
reject_pair("beam_coherence msc is within 0..1 (1.2)", "beam-coherence", BCL, bc_q, with_cell(bc_q, 0, "quiet", msc_p50=[1.2] + bc_q["bands"][0]["quiet"]["msc_p50"][1:]), "$.bands[0].quiet.msc_p50[0]", "greater than the maximum of 1")
reject_pair("beam_coherence msc below 0", "beam-coherence", BCL, bc_q, with_cell(bc_q, 0, "quiet", msc_p10=[-0.1] + bc_q["bands"][0]["quiet"]["msc_p10"][1:]), "$.bands[0].quiet.msc_p10[0]", "less than the minimum of 0")
reject_pair("beam_coherence phase is within -180..180 degrees", "beam-coherence", BCL, bc_q, with_cell(bc_q, 1, "quiet", ph_deg=[181.0] + bc_q["bands"][1]["quiet"]["ph_deg"][1:]), "$.bands[1].quiet.ph_deg[0]", "greater than the maximum of 180")
reject_pair("beam_coherence msc arrays have 8 pairs", "beam-coherence", BCL, bc_q, with_cell(bc_q, 0, "quiet", msc_p50=bc_q["bands"][0]["quiet"]["msc_p50"][:7]), "$.bands[0].quiet.msc_p50", "too short")
reject_pair("beam_coherence has exactly 8 bands", "beam-coherence", BCL, bc_q, mod(bc_q, bands=bc_q["bands"][:7]), "$.bands", "too short")
b_no = copy.deepcopy(bc_q)
b_no["bands"][0].pop("n_active")
reject_pair("beam_coherence band needs n_active (every condition key present)", "beam-coherence", BCL, bc_q, b_no, "$.bands[0]", "'n_active' is a required property")
b_nul = copy.deepcopy(bc_q)
b_nul["bands"][0]["quiet"] = None
check("beam_coherence a null cell is valid (fewer than 5 windows), and so is a null xc_lag and a null level_dbfs",
      drifts("beam-coherence", b_nul, BCL) == [] and drifts("beam-coherence", mod(bc_q, xc_lag=None, level_dbfs=None), BCL) == [])
reject_pair("beam_coherence nfft is pinned to 4096", "beam-coherence", BCL, bc_q, mod(bc_q, nfft=2048), "$.nfft", "4096 was expected")
reject_pair("beam_coherence edges_hz are pinned", "beam-coherence", BCL, bc_q, mod(bc_q, edges_hz=bc_q["edges_hz"][:7]), "$.edges_hz", "was expected")
reject_pair("beam_coherence window counts are non-negative", "beam-coherence", BCL, bc_q, mod(bc_q, win_gated=-1), "$.win_gated", "less than the minimum of 0")
reject_pair("beam_coherence xc_lag needs 8 pairs", "beam-coherence", BCL, bc_q, mod(bc_q, xc_lag=bc_q["xc_lag"][:7]), "$.xc_lag", "too short")
fq = copy.deepcopy(bc_a)
fq["full"]["cond"]["active"]["msc"][0] = fq["full"]["cond"]["active"]["msc"][0] + [0.5]
reject_pair("beam_coherence full MSC has 28 pairs per broad band", "beam-coherence", BCL, bc_a, fq, "$.full.cond.active.msc[0]", "too long")
check("beam-coherence: the schema maps coherence.jsonl to exactly the beam_coherence kind and a kind that is not in the discriminator is drift (positive twin: beam_coherence passes)",
      SCH["beam-coherence"]["x-files"] == {"coherence.jsonl": ["beam_coherence"]} and drifts("beam-coherence", bc_q, BCL) == []
      and rejected(drifts("beam-coherence", mod(bc_q, k="beam_coherence_v2"), BCL), "$.k", "unknown record kind 'beam_coherence_v2'"))


def m_msc(s): s["$defs"]["beam8_unit"]["items"]["maximum"] = 0.5


d_ = C.run(mutated("beam_coherence.schema.json", m_msc))[1]
check("mutation: beam_coherence MSC maximum 0.5 -> the fixture cells above 0.5 are flagged with their path",
      any(d.file == "beam_coherence.jsonl" and d.path.endswith(".msc_p50[0]") and "greater than the maximum of 0.5" in d.message for d in d_), str([str(d) for d in d_][:2]))
bcl_dir = os.path.join(tmp, "bccl")
mkcontracts(bcl_dir)
_p = os.path.join(bcl_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8").write(open(_p, encoding="utf-8").read().replace("beam-coherence 1.0.0", "beam-coherence x"))
d_ = C.run(bcl_dir)[1]
check("mutation: the CHANGELOG without `beam-coherence 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "beam-coherence" for d in d_), str([str(d) for d in d_][:2]))
root_bc = os.path.join(tmp, "live_bc")
mkroot(root_bc)
_n0, _d0, _ = C.check_live(root_bc, SCH, 100)
shutil.copy(os.path.join(FIX, BCF), os.path.join(root_bc, "analysis", BCL))
_n1, _d1, _ = C.check_live(root_bc, SCH, 100)
with open(os.path.join(root_bc, "analysis", BCL), "a", encoding="utf-8") as f:
    f.write(json.dumps(mod(bc_q, v=2)) + chr(10))
_n2, _d2, _ = C.check_live(root_bc, SCH, 100)
check("live check: analysis/coherence.jsonl is optional (absent: 0 drift), 5 fixture records add 5 and 0 drift, an appended v=2 record is reported",
      _d0 == [] and _d1 == [] and _n1 == _n0 + 5 and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("analysis/coherence.jsonl", "$.v"), str((_n0, _n1, [str(d) for d in _d2][:2])))
try:                                                                     # the fixture regenerates from the real plugin (needs scipy; the contracts venv has none)
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import make_coherence_fixtures as MCOH  # noqa: E402
except ImportError as ex:
    skip("beam-coherence fixture regeneration", f"the plugin needs scipy: {ex}; analysis/test_coherence.py covers it")
else:
    _bc_regen = [json.loads(l) for l in open(os.path.join(MCOH.build(os.path.join(tmp, "regen_bc")), BCF), encoding="utf-8")]

    def _same(x, y):
        if isinstance(x, float) or isinstance(y, float):
            return isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= 0.002 + 1e-9
        if isinstance(x, dict):
            return isinstance(y, dict) and x.keys() == y.keys() and all(_same(x[k], y[k]) for k in x)
        if isinstance(x, list):
            return isinstance(y, list) and len(x) == len(y) and all(_same(p_, q_) for p_, q_ in zip(x, y))
        return x == y
    check("beam-coherence fixture equals the output of the real plugin (floats within 0.002, everything else exact)", len(_bc_regen) == len(bcs) and all(_same(a_, b_) for a_, b_ in zip(_bc_regen, bcs)))
# ============================================================================================ ambient-temp 1.1.0 (tools/ambient_temp.py)
AMBF = "ambient_temp.jsonl"
_amb_x = 56.8                                                      # a synthetic longitude for the rejection case
ams = jl(AMBF)
am0 = ams[0]
check("ambient-temp fixture: 8 hourly records of kind ambient_temp from the real tool, validating against the schema, consecutive hours, a negative and a 0.0 value, model_grid_km null",
      len(ams) == 8 and all(not drifts("ambient-temp", r, AMBF) for r in ams)
      and [r["t_hour_utc"] for r in ams] == [1792972800 + 3600 * i for i in range(8)]
      and [r["temp_c"] for r in ams] == [3.4, 2.9, 2.1, 1.0, -0.6, -0.2, 0.0, 4.5] and {r["model_grid_km"] for r in ams} == {None}, str(ams[:2]))
reject_pair("ambient_temp version v", "ambient-temp", AMBF, am0, mod(am0, v=2), "$.v", "1 was expected")
reject_pair("ambient_temp t_hour_utc is on an hour boundary", "ambient-temp", AMBF, am0, mod(am0, t_hour_utc=am0["t_hour_utc"] + 1), "$.t_hour_utc", "is not a multiple of 3600")
reject_pair("ambient_temp temp_c above 60 C", "ambient-temp", AMBF, am0, mod(am0, temp_c=61.0), "$.temp_c", "greater than the maximum of 60")
reject_pair("ambient_temp temp_c below -90 C", "ambient-temp", AMBF, am0, mod(am0, temp_c=-91.0), "$.temp_c", "less than the minimum of -90")
reject_pair("ambient_temp temp_c is never null", "ambient-temp", AMBF, am0, mod(am0, temp_c=None), "$.temp_c", "is not of type 'number'")
reject_pair("ambient_temp model_grid_km may be null or 11 but not 0", "ambient-temp", AMBF, mod(am0, model_grid_km=11), mod(am0, model_grid_km=0), "$.model_grid_km", "less than the minimum of 1")
reject_pair("ambient_temp needs source", "ambient-temp", AMBF, am0, mod(am0, source=KeyError), "$", "'source' is a required property")
reject_pair("ambient_temp never carries a latitude", "ambient-temp", AMBF, am0, mod(am0, latitude=12.3), "$", "should not be valid under")
reject_pair("ambient_temp never carries a lon", "ambient-temp", AMBF, am0, mod(am0, lon=_amb_x), "$", "should not be valid under")
check("ambient-temp: the schema maps ambient_temp.jsonl to exactly the ambient_temp kind; a run_event in that file is drift (positive twin: ambient_temp passes)",
      SCH["ambient-temp"]["x-files"] == {AMBF: ["ambient_temp"]} and drifts("ambient-temp", am0, AMBF) == []
      and rejected(drifts("ambient-temp", mod(am0, k="run_event"), AMBF), "$.k", "unknown record kind 'run_event'"), "")
_amb_old = {k_: v_ for k_, v_ in am0.items() if k_ not in ("wind_ms", "precip_mm", "cloud_pct")}
check("ambient-temp 1.1.0 fixture: exact covariates, hour 05 has no wind_ms, hour 06 has no cloud_pct, every record has precip_mm",
      [r.get("wind_ms") for r in ams] == [1.2, 0.8, 0.0, 2.5, 3.1, None, 4.0, 5.6] and [r.get("cloud_pct") for r in ams] == [100.0, 90.0, 75.0, 40.0, 0.0, 10.0, None, 5.0]
      and [r["precip_mm"] for r in ams] == [0.0, 0.0, 0.0, 0.2, 1.4, 0.0, 0.0, 0.0] and "wind_ms" not in ams[5] and "cloud_pct" not in ams[6], str(ams[5:7]))
check("ambient-temp 1.1.0: an old (1.0.0) record without covariates is still valid; positive twin of the covariate rejections below",
      set(_amb_old) == {"k", "v", "t_hour_utc", "temp_c", "source", "model_grid_km"} and drifts("ambient-temp", _amb_old, AMBF) == [] and drifts("ambient-temp", am0, AMBF) == [], str(_amb_old))
reject_pair("ambient_temp wind_ms is not negative", "ambient-temp", AMBF, mod(am0, wind_ms=0.0), mod(am0, wind_ms=-0.1), "$.wind_ms", "less than the minimum of 0")
reject_pair("ambient_temp wind_ms at most 150", "ambient-temp", AMBF, mod(am0, wind_ms=150.0), mod(am0, wind_ms=150.5), "$.wind_ms", "greater than the maximum of 150")
reject_pair("ambient_temp precip_mm at most 500", "ambient-temp", AMBF, mod(am0, precip_mm=500.0), mod(am0, precip_mm=501.0), "$.precip_mm", "greater than the maximum of 500")
reject_pair("ambient_temp cloud_pct at most 100", "ambient-temp", AMBF, mod(am0, cloud_pct=100.0), mod(am0, cloud_pct=100.5), "$.cloud_pct", "greater than the maximum of 100")
reject_pair("ambient_temp cloud_pct is never null (the field is left out instead)", "ambient-temp", AMBF, _amb_old, mod(am0, cloud_pct=None), "$.cloud_pct", "is not of type 'number'")
def m_amb_cloud(s): s["$defs"]["ambient_temp"]["properties"]["cloud_pct"]["maximum"] = 50


d_ = C.run(mutated("ambient_temp.schema.json", m_amb_cloud))[1]
check("mutation: cloud_pct maximum 50 -> the fixture records with 75 / 100 % are flagged with their path",
      sum(1 for d in d_ if d.file == AMBF and d.path == "$.cloud_pct" and "greater than the maximum of 50" in d.message) == 3, str([str(d) for d in d_][:2]))
_amb_raw = open(os.path.join(FIX, AMBF), "rb").read()
check("ambient-temp fixture: LF line ends, no BOM, and no coordinate (the synthetic ones, their rounded values, any lat/lon key) anywhere in the file",
      b"\r" not in _amb_raw and not _amb_raw.startswith(b"\xef\xbb\xbf") and not any(s_ in _amb_raw for s_ in (b"12.34", b"56.78", b"12.3,", b"56.8,", b"lat", b"lon")), _amb_raw[:80])


def m_amb_hour(s): s["$defs"]["ambient_temp"]["properties"]["t_hour_utc"]["multipleOf"] = 7200


d_ = C.run(mutated("ambient_temp.schema.json", m_amb_hour))[1]
check("mutation: ambient_temp hour multipleOf 7200 -> the odd-hour fixture records are flagged with their path",
      any(d.file == AMBF and d.path == "$.t_hour_utc" and "is not a multiple of 7200" in d.message for d in d_), str([str(d) for d in d_][:2]))


def m_amb_loc(s): s["$defs"]["ambient_temp"].pop("not")


_ambloc = mutated("ambient_temp.schema.json", m_amb_loc)
_sch_loc = C.load_schemas(os.path.join(_ambloc, "schemas"))
check("mutation: the schema without its location ban accepts a record with a latitude (proves the ban is what rejects it above)",
      C.check_record(AMBF, "1", mod(am0, latitude=12.3), _sch_loc["ambient-temp"], ["ambient_temp"], {}) == [], "")
amb_dir = os.path.join(tmp, "ambcl")
mkcontracts(amb_dir)
_p = os.path.join(amb_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("ambient-temp 1.1.0", "ambient-temp x"))
d_ = C.run(amb_dir)[1]
check("mutation: the CHANGELOG without `ambient-temp 1.1.0` -> flagged", any(d.kind == "changelog" and d.path == "ambient-temp" for d in d_), str([str(d) for d in d_][:2]))
root_am = os.path.join(tmp, "live_am")
mkroot(root_am)
_n0, _d0, _notes0 = C.check_live(root_am, SCH, 100)
os.makedirs(os.path.join(root_am, "history"))
shutil.copy(os.path.join(FIX, AMBF), os.path.join(root_am, "history", AMBF))
_n1, _d1, _ = C.check_live(root_am, SCH, 100)
with open(os.path.join(root_am, "history", AMBF), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(am0, temp_c=99.0), separators=(",", ":")) + "\n")
_n2, _d2, _ = C.check_live(root_am, SCH, 100)
check("live check: history/ambient_temp.jsonl is optional (absent: a note, 0 drift), the 8 fixture records add 8 and 0 drift, an appended 99 C record is reported",
      _d0 == [] and any(n_.startswith("history/ambient_temp.jsonl") for n_ in _notes0) and _d1 == [] and _n1 == _n0 + 8
      and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("history/ambient_temp.jsonl", "$.temp_c") and "greater than the maximum of 60" in _d2[0].message, str((_n0, _n1, [str(d) for d in _d2][:2])))
sys.path.insert(0, os.path.join(HERE, "tools"))
import make_ambient_fixtures as MAMB  # noqa: E402
_amb_regen = open(MAMB.build(os.path.join(tmp, "regen_amb")), "rb").read()
check("ambient-temp fixture equals the output of the real tool byte for byte", _amb_regen == _amb_raw, _amb_regen[:80])
# ============================================================================================ perch 1.0.0 (analysis/perch.py)
PRF = "perch.jsonl"
prs = jl(PRF)
pk = [r for r in prs if r["k"] == "perch"]
pa = [r for r in prs if r["k"] == "perch_agreement"]
p_l = next(r for r in pk if r["src"] == "loudest")
p_m = next(r for r in pk if r["src"] == "mixed")
check("perch fixture: from the real plugin: 12 perch records (6 windows x mixed + loudest) and 2 agreement records, all validating; the speech-flagged and the human-voice windows left none",
      len(pk) == 12 and len(pa) == 2 and all(not drifts("perch", r, PRF) for r in prs) and sorted({r["t"] - 1792900020.0 for r in pk}) == [5.0, 10.0, 15.0, 30.0, 35.0, 65.0]
      and not any(t_[0] in ("Speech", "Male_speech_and_man_speaking") for r in pk for t_ in r["top"]), str((len(pk), len(pa))))
check("perch fixture: first window: mixed has beam null, loudest beam 4, label 'Poecile carolinensis' with a logit within 0.1 of 6.25, best_species the same, group bird, emb_i null, run 1792899000",
      (p_m["beam"], p_l["beam"], p_l["top"][0][0], p_l["best_species"][0], p_l["top1_group"], p_l["emb_i"], p_l["run"]) == (None, 4, "Poecile carolinensis", "Poecile carolinensis", "bird", None, 1792899000)
      and abs(p_l["top"][0][1] - 6.25) < 0.1 and len(p_l["top"]) == 5, str(p_l))
a1, a2 = pa
check("perch fixture: minute 1 agreement: 5 windows = same_species 1 + same_genus 1 + birds_only 1 + both_silent 1 + birds_missing 1, groups bird 3 / insect 2, insect {perch 2, birds 1, both 1}, not partial; minute 2 is partial with 1 birds_missing window",
      (a1["t"], a1["windows"], a1["both_same_species"], a1["both_same_genus"], a1["both_differ"], a1["perch_only"], a1["birds_only"], a1["both_silent"], a1["birds_missing"]) == (1792900020.0, 5, 1, 1, 0, 0, 1, 1, 1)
      and a1["perch_top1_groups"] == {"bird": 3, "insect": 2} and a1["insect"] == {"perch": 2, "birds": 1, "both": 1} and "partial" not in a1
      and (a2["t"], a2["windows"], a2["birds_missing"], a2.get("partial")) == (1792900080.0, 1, 1, True), str((a1, a2)))
check("perch fixture: every agreement record's windows equals the sum of its seven categories and the sum of perch_top1_groups",
      all(r["windows"] == sum(r[c] for c in ("both_same_species", "both_same_genus", "both_differ", "perch_only", "birds_only", "both_silent", "birds_missing")) == sum(r["perch_top1_groups"].values()) for r in pa))
reject_pair("perch version v", "perch", PRF, p_l, mod(p_l, v=2), "$.v", "1 was expected")
reject_pair("perch src is mixed or loudest", "perch", PRF, p_l, mod(p_l, src="best"), "$.src", "is not one of")
reject_pair("perch beam is 1..8", "perch", PRF, p_l, mod(p_l, beam=9), "$.beam", "greater than the maximum of 8")
check("perch beam null (mixed) and beam 1 are valid", drifts("perch", p_m, PRF) == [] and drifts("perch", mod(p_l, beam=1), PRF) == [])
reject_pair("perch top has exactly 5 entries", "perch", PRF, p_l, mod(p_l, top=p_l["top"][:4]), "$.top", "too short")
reject_pair("perch top entry is [label, number]", "perch", PRF, p_l, mod(p_l, top=[["Poecile carolinensis", "6.2"]] + p_l["top"][1:]), "$.top[0][1]", "is not of type 'number'")
reject_pair("perch top1_group is from the closed set", "perch", PRF, p_l, mod(p_l, top1_group="fish"), "$.top1_group", "is not one of")
reject_pair("perch mv is name@12 hex", "perch", PRF, p_l, mod(p_l, mv="perch_v2.onnx"), "$.mv", "does not match")
reject_pair("perch emb_i is a non-negative integer or null", "perch", PRF, p_l, mod(p_l, emb_i=-1), "$.emb_i", "less than the minimum of 0")
check("perch emb_i 7 and best_species null are valid (--perch-embeddings; a label list without a binomial)", drifts("perch", mod(p_l, emb_i=7), PRF) == [] and drifts("perch", mod(p_l, best_species=None), PRF) == [])
reject_pair("perch level_dbfs is at most 0", "perch", PRF, p_l, mod(p_l, level_dbfs=3.0), "$.level_dbfs", "greater than the maximum of 0")
reject_pair("perch_agreement count is non-negative", "perch", PRF, a1, mod(a1, perch_only=-1), "$.perch_only", "less than the minimum of 0")
reject_pair("perch_agreement partial is only ever true", "perch", PRF, a2, mod(a2, partial=False), "$.partial", "True was expected")
reject_pair("perch_agreement group keys are the closed set", "perch", PRF, a1, mod(a1, perch_top1_groups={"fish": 1}), "$.perch_top1_groups", "'fish' is not one of")
reject_pair("perch_agreement insect has exactly perch, birds, both", "perch", PRF, a1, mod(a1, insect={"perch": 1, "birds": 0, "both": 0, "x": 1}), "$.insect", "Additional properties are not allowed")
reject_pair("perch_agreement needs birds_missing (every category present)", "perch", PRF, a1, mod(a1, birds_missing=KeyError), "$", "'birds_missing' is a required property")
check("perch: the schema maps perch.jsonl to exactly perch and perch_agreement; a kind outside the discriminator is drift (positive twin: both known kinds pass)",
      SCH["perch"]["x-files"] == {"perch.jsonl": ["perch", "perch_agreement"]} and drifts("perch", p_l, PRF) == [] and drifts("perch", a1, PRF) == []
      and rejected(drifts("perch", mod(p_l, k="perch_v2"), PRF), "$.k", "unknown record kind 'perch_v2'"))


def m_top(s): s["$defs"]["perch"]["properties"]["top"]["maxItems"] = 3


d_ = C.run(mutated("perch.schema.json", m_top))[1]
check("mutation: perch top maxItems 3 -> the fixture records (5 entries) are flagged with their path",
      any(d.file == "perch.jsonl" and d.path == "$.top" and "too long" in d.message for d in d_), str([str(d) for d in d_][:2]))
prl_dir = os.path.join(tmp, "prcl")
mkcontracts(prl_dir)
_p = os.path.join(prl_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8").write(open(_p, encoding="utf-8").read().replace("perch 1.0.0", "perch x"))
d_ = C.run(prl_dir)[1]
check("mutation: the CHANGELOG without `perch 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "perch" for d in d_), str([str(d) for d in d_][:2]))
root_pr = os.path.join(tmp, "live_pr")
mkroot(root_pr)
_n0, _d0, _ = C.check_live(root_pr, SCH, 100)
shutil.copy(os.path.join(FIX, PRF), os.path.join(root_pr, "analysis", PRF))
_n1, _d1, _ = C.check_live(root_pr, SCH, 100)
with open(os.path.join(root_pr, "analysis", PRF), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(p_l, v=2), separators=(",", ":")) + "\n")
_n2, _d2, _ = C.check_live(root_pr, SCH, 100)
check("live check: analysis/perch.jsonl is optional (absent: 0 drift), the 14 fixture records add 14 and 0 drift, an appended v=2 record is reported",
      _d0 == [] and _d1 == [] and _n1 == _n0 + 14 and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("analysis/perch.jsonl", "$.v"), str((_n0, _n1, [str(d) for d in _d2][:2])))
try:                                                                     # the fixture regenerates from the real plugin (needs scipy, onnx and onnxruntime; the contracts venv has none)
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import make_perch_fixtures as MPF  # noqa: E402
    _pr_regen_dir = MPF.build(os.path.join(tmp, "regen_pr"))             # the missing onnx / onnxruntime / scipy is raised HERE, not by the import above
except ImportError as ex:
    skip("perch fixture regeneration", f"the plugin needs scipy, onnx and onnxruntime: {ex}; analysis/test_perch.py covers it")
else:
    _pr_regen = [json.loads(l) for l in open(os.path.join(_pr_regen_dir, PRF), encoding="utf-8")]

    def _same_p(x, y):
        if isinstance(x, float) or isinstance(y, float):
            return isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= 0.01 + 1e-9
        if isinstance(x, dict):
            return isinstance(y, dict) and x.keys() == y.keys() and all(_same_p(x[k], y[k]) for k in x if k != "mv")
        if isinstance(x, list):
            return isinstance(y, list) and len(x) == len(y) and all(_same_p(p_, q_) for p_, q_ in zip(x, y))
        return x == y
    check("perch fixture equals the output of the real plugin (floats within 0.01, everything else exact, `mv` excepted: the model file's hash depends on the onnx version)",
          len(_pr_regen) == len(prs) and all(_same_p(a_, b_) for a_, b_ in zip(_pr_regen, prs)))

# ============================================================================================ mixscore 1.0.0 (analysis/mixscore.py)
MXF = "mixscore.jsonl"
mxs = jl(MXF)
mx_w = next(r for r in mxs if r["type"] == "mixscore")
mx_c = next(r for r in mxs if r["type"] == "mixscore_compare" and "partial" not in r)
mx_p = next(r for r in mxs if r.get("partial"))
check("mixscore fixture: 6 window records and 2 compare records (one partial) from the real plugin, all validating against the schema",
      sum(r["type"] == "mixscore" for r in mxs) == 6 and sum(r["type"] == "mixscore_compare" for r in mxs) == 2 and all(not drifts("mixscore", r, MXF) for r in mxs), str(len(mxs)))
check("mixscore fixture: the minute record has the exact scenario counts and no speech-window species (no Barn Owl anywhere)",
      mx_c["events"] == {"both": 2, "beam_only": 2, "mix_only": 1} and mx_c["windows"]["dropped_speech"] == 1 and mx_c["groups"]["insect"]["beam_only"] == 1
      and "Barn Owl" not in open(os.path.join(FIX, MXF), encoding="utf-8").read(), str(mx_c["events"]))
reject_pair("mixscore version v", "mixscore", MXF, mx_c, mod(mx_c, v=2), "$.v", "1 was expected")
reject_pair("mixscore confidence within 0..1 (1.2)", "mixscore", MXF, mx_w, mod(mx_w, top=[mod(mx_w["top"][0], confidence=1.2)]), "$.top[0].confidence", "greater than the maximum of 1")
reject_pair("mixscore top needs at least one detection", "mixscore", MXF, mx_w, mod(mx_w, top=[]), "$.top", "should be non-empty")
reject_pair("mixscore top holds at most 5 detections", "mixscore", MXF, mx_w, mod(mx_w, top=[mx_w["top"][0]] * 6), "$.top", "too long")
reject_pair("mixscore gain is within 0..9.04 dB (12)", "mixscore", MXF, mx_w, mod(mx_w, mix_gain_db=12.0), "$.mix_gain_db", "greater than the maximum of 9.04")
reject_pair("mixscore privacy: a window record with a lat key is rejected", "mixscore", MXF, mx_w, mod(mx_w, lat=None), "$", "should not be valid under")
reject_pair("mixscore privacy: a compare record with an embedding key is rejected", "mixscore", MXF, mx_c, mod(mx_c, embedding=[0.1]), "$", "should not be valid under")
reject_pair("mixscore compare minute start is a multiple of 60 (1800000001)", "mixscore", MXF, mx_c, mod(mx_c, t=1800000001), "$.t", "is not a multiple of 60")
reject_pair("mixscore compare interval is pinned to 60", "mixscore", MXF, mx_c, mod(mx_c, interval_s=30), "$.interval_s", "60 was expected")
reject_pair("mixscore compare events need mix_only", "mixscore", MXF, mx_c, mod(mx_c, events={"both": 1, "beam_only": 0}), "$.events", "'mix_only' is a required property")
reject_pair("mixscore compare partial may only be true", "mixscore", MXF, mx_p, mod(mx_p, partial=False), "$.partial", "True was expected")
check("mixscore: null shared-event statistics are valid when no event was shared (positive twin of the all-numbers record), and a kind outside the map is drift",
      drifts("mixscore", mod(mx_c, both_conf={"n": 0, "best_beam_mean": None, "mix_mean": None, "diff_mean": None, "mix_higher": 0}), MXF) == [] and drifts("mixscore", mx_c, MXF) == []
      and rejected(drifts("mixscore", mod(mx_c, type="mixscore_v2"), MXF), "$.type", "unknown record kind 'mixscore_v2'") and SCH["mixscore"]["x-files"] == {"mixscore.jsonl": ["mixscore", "mixscore_compare"]})


def m_gain(s): s["$defs"]["mixscore"]["properties"]["mix_gain_db"]["minimum"] = 0.5


d_ = C.run(mutated("mixscore.schema.json", m_gain))[1]
check("mutation: mixscore gain minimum 0.5 -> the fixture window records (gain 0.0) are flagged with their path",
      any(d.file == "mixscore.jsonl" and d.path == "$.mix_gain_db" and "less than the minimum of 0.5" in d.message for d in d_), str([str(d) for d in d_][:2]))
mxl_dir = os.path.join(tmp, "mxcl")
mkcontracts(mxl_dir)
_p = os.path.join(mxl_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("mixscore 1.0.0", "mixscore x"))
d_ = C.run(mxl_dir)[1]
check("mutation: the CHANGELOG without `mixscore 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "mixscore" for d in d_), str([str(d) for d in d_][:2]))
root_mx = os.path.join(tmp, "live_mx")
mkroot(root_mx)
_n0, _d0, _ = C.check_live(root_mx, SCH, 100)
shutil.copy(os.path.join(FIX, MXF), os.path.join(root_mx, "analysis", MXF))
_n1, _d1, _ = C.check_live(root_mx, SCH, 100)
with open(os.path.join(root_mx, "analysis", MXF), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(mx_c, v=2)) + "\n")
_n2, _d2, _ = C.check_live(root_mx, SCH, 100)
check("live check: analysis/mixscore.jsonl is optional (absent: 0 drift), the 8 fixture records add 8 and 0 drift, an appended v=2 record is reported",
      _d0 == [] and _d1 == [] and _n1 == _n0 + 8 and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("analysis/mixscore.jsonl", "$.v"), str((_n0, _n1, [str(d) for d in _d2][:2])))
sys.path.insert(0, os.path.join(BMA, "analysis"))
import make_mixscore_fixtures as MMIX  # noqa: E402
_mx_regen = open(os.path.join(MMIX.build(os.path.join(tmp, "regen_mx")), MXF), "rb").read()
check("mixscore fixture equals the output of the real plugin and writer byte for byte", _mx_regen == open(os.path.join(FIX, MXF), "rb").read(), _mx_regen[:80])

# ============================================================================================ burst-stats 1.0.0 (analysis/bursts.py)
BSF, BSL = "burst_stats.jsonl", "bursts.jsonl"                       # fixture name, live name (the x-files key)
bss = jl(BSF)
bs_reg = next(r for r in bss if r["gaps"]["regularity"] == 1.0 and r["impulses"]["coincident_all_n"] == r["impulses"]["n"] > 0 and "partial" not in r)
bs_sp = next(r for r in bss if r["win_speech"] > 0)
check("burst-stats fixture: 5 records of kind burst_stats from the real plugin, validating against the schema", len(bss) == 5 and all(not drifts("burst-stats", r, BSL) for r in bss))
check("burst-stats fixture: it holds a regular all-beam train (mode 0.7, regularity 1.0), speech and unvetted windows excluded, a null mode (no impulses) and one partial interval",
      bs_reg["gaps"]["mode_s"] == 0.7 and bs_sp["win_unvetted"] > 0 and bs_sp["win_total"] == bs_sp["win_used"] + bs_sp["win_speech"] + bs_sp["win_unvetted"] + bs_sp["win_dead"]
      and any(r["gaps"]["mode_s"] is None and r["impulses"]["n"] == 0 for r in bss) and sum(1 for r in bss if r.get("partial")) == 1, str([(r["win_used"], r["gaps"]["mode_s"]) for r in bss]))
reject_pair("burst_stats version v", "burst-stats", BSL, bs_reg, mod(bs_reg, v=2), "$.v", "1 was expected")
reject_pair("burst_stats regularity is a share (1.5)", "burst-stats", BSL, bs_reg, mod(bs_reg, gaps=mod(bs_reg["gaps"], regularity=1.5)), "$.gaps.regularity", "")
reject_pair("burst_stats modal gap stays inside 5 ms..30 s (40 s)", "burst-stats", BSL, bs_reg, mod(bs_reg, gaps=mod(bs_reg["gaps"], mode_s=40.0)), "$.gaps.mode_s", "")
reject_pair("burst_stats gap histogram has 30 bins", "burst-stats", BSL, bs_reg, mod(bs_reg, gaps=mod(bs_reg["gaps"], hist=bs_reg["gaps"]["hist"][:29])), "$.gaps.hist", "too short")
reject_pair("burst_stats within3_hist has 8 entries", "burst-stats", BSL, bs_reg, mod(bs_reg, impulses=mod(bs_reg["impulses"], within3_hist=[0] * 7)), "$.impulses.within3_hist", "")
reject_pair("burst_stats beams_hit_hist has 9 entries (0..8 beams)", "burst-stats", BSL, bs_reg, mod(bs_reg, impulses=mod(bs_reg["impulses"], beams_hit_hist=[0] * 8)), "$.impulses.beams_hit_hist", "")
reject_pair("burst_stats band is pinned to 6-16 kHz", "burst-stats", BSL, bs_reg, mod(bs_reg, detector=mod(bs_reg["detector"], band_hz=[6000, 12000])), "$.detector.band_hz", "was expected")
reject_pair("burst_stats bands_db has 10 values per percentile", "burst-stats", BSL, bs_reg, mod(bs_reg, bands_db=mod(bs_reg["bands_db"], p50=bs_reg["bands_db"]["p50"][:9])), "$.bands_db.p50", "too short")
b_no = {k: v for k, v in bs_reg.items() if k != "win_unvetted"}
reject_pair("burst_stats needs win_unvetted (the speech-exclusion accounting)", "burst-stats", BSL, bs_reg, b_no, "$", "'win_unvetted' is a required property")
check("burst_stats a no-impulse interval is valid: null levels, null mode, null bands are allowed",
      drifts("burst-stats", mod(bs_reg, bands_db=None, ref_2_6k_db=None, impulses=mod(bs_reg["impulses"], level_db=None, excess_db=None, spread_db=None, within3_hist=None, beams_hit_hist=None, coincident_all_n=None, loudest_beam_hist=None)), BSL) == [])
check("burst-stats: the schema maps bursts.jsonl to exactly the burst_stats kind and a kind that is not in the discriminator is drift (positive twin: burst_stats passes)",
      SCH["burst-stats"]["x-files"] == {"bursts.jsonl": ["burst_stats"]} and drifts("burst-stats", bs_reg, BSL) == []
      and rejected(drifts("burst-stats", mod(bs_reg, k="burst_stats_v2"), BSL), "$.k", "unknown record kind 'burst_stats_v2'"))


def m_reg(s_): s_["$defs"]["burst_stats"]["properties"]["gaps"]["properties"]["regularity"]["maximum"] = 0.5


d_ = C.run(mutated("burst_stats.schema.json", m_reg))[1]
check("mutation: burst_stats regularity maximum 0.5 -> the fixture records above 0.5 are flagged with their path",
      any(d.file == "burst_stats.jsonl" and d.path == "$.gaps.regularity" and "greater than the maximum of 0.5" in d.message for d in d_), str([str(d) for d in d_][:2]))
bsl_dir = os.path.join(tmp, "bstatscl")
mkcontracts(bsl_dir)
_p = os.path.join(bsl_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("burst-stats 1.0.0", "burst-stats x"))
d_ = C.run(bsl_dir)[1]
check("mutation: the CHANGELOG without `burst-stats 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "burst-stats" for d in d_), str([str(d) for d in d_][:2]))
root_bs = os.path.join(tmp, "live_bs")
mkroot(root_bs)
_n0, _d0, _ = C.check_live(root_bs, SCH, 100)
shutil.copy(os.path.join(FIX, BSF), os.path.join(root_bs, "analysis", BSL))
_n1, _d1, _ = C.check_live(root_bs, SCH, 100)
with open(os.path.join(root_bs, "analysis", BSL), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(bs_reg, v=2)) + "\n")
_n2, _d2, _ = C.check_live(root_bs, SCH, 100)
check("live check: analysis/bursts.jsonl is optional (absent: 0 drift), 5 fixture records add 5 and 0 drift, an appended v=2 record is reported",
      _d0 == [] and _d1 == [] and _n1 == _n0 + 5 and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("analysis/bursts.jsonl", "$.v"), str((_n0, _n1, [str(d) for d in _d2][:2])))
try:                                                                     # the fixture regenerates from the real plugin (needs scipy; the contracts venv has none)
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import make_burst_fixtures as MBUR  # noqa: E402
except ImportError as ex:
    skip("burst-stats fixture regeneration", f"the plugin needs scipy: {ex}; analysis/test_bursts.py covers it")
else:
    _bs_regen = [json.loads(l) for l in open(os.path.join(MBUR.build(os.path.join(tmp, "regen_bs")), BSF), encoding="utf-8")]

    def _same_b(x, y):
        if isinstance(x, float) or isinstance(y, float):
            return isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= 0.01 + 1e-9
        if isinstance(x, dict):
            return isinstance(y, dict) and x.keys() == y.keys() and all(_same_b(x[k], y[k]) for k in x)
        if isinstance(x, list):
            return isinstance(y, list) and len(x) == len(y) and all(_same_b(p_, q_) for p_, q_ in zip(x, y))
        return x == y
    check("burst-stats fixture equals the output of the real plugin (floats within 0.01, everything else exact)", len(_bs_regen) == len(bss) and all(_same_b(a_, b_) for a_, b_ in zip(_bs_regen, bss)))

# ============================================================================================ acoustic-indices 1.0.0 (analysis/indices.py)
AIF, AIL = "acoustic_indices.jsonl", "indices.jsonl"                  # fixture name, live name (the x-files key)
ais = jl(AIF)
ai0, ai1 = ais[0], ais[1]
check("acoustic-indices fixture: 4 acoustic_indices records from the real plugin, validating against the schema, only the last one partial",
      len(ais) == 4 and all(not drifts("acoustic-indices", r, AIL) for r in ais) and [bool(r.get("partial")) for r in ais] == [False, False, False, True], str([r.get("partial") for r in ais]))
check("acoustic-indices fixture: interval 1 holds 2 speech-excluded, 1 silent, 1 non-finite and 4 untagged windows of 20 (12 used: untagged windows are excluded); every record has win_total = used + speech + silent + nonfinite + untagged",
      (ai1["win_total"], ai1["win_used"], ai1["win_speech"], ai1["win_silent"], ai1["win_nonfinite"], ai1["win_untagged"]) == (20, 12, 2, 1, 1, 4)
      and all(r["win_total"] == r["win_used"] + r["win_speech"] + r["win_silent"] + r["win_nonfinite"] + r["win_untagged"] for r in ais), str(ai1))
check("acoustic-indices fixture: the 2 speech windows (-20 dBFS) left no trace: interval 1 reads about -50 dBFS (p50 and p90), not -20",
      ai1["indices"]["level_dbfs"]["p90"] < -49.0 and -50.2 < ai1["indices"]["level_dbfs"]["p50"] < -49.9, str(ai1["indices"]["level_dbfs"]))
reject_pair("acoustic_indices version v", "acoustic-indices", AIL, ai0, mod(ai0, v=2), "$.v", "1 was expected")
reject_pair("acoustic_indices win_used is at least 1 (an interval without a used window writes no record)", "acoustic-indices", AIL, ai0, mod(ai0, win_used=0), "$.win_used", "less than the minimum of 1")
reject_pair("acoustic_indices counts are non-negative", "acoustic-indices", AIL, ai0, mod(ai0, win_speech=-1), "$.win_speech", "less than the minimum of 0")
reject_pair("acoustic_indices needs speech_gate", "acoustic-indices", AIL, ai0, mod(ai0, speech_gate=KeyError), "$", "'speech_gate' is a required property")


def with_ind(rec, name, **kw):
    r = copy.deepcopy(rec)
    r["indices"][name].update(kw)
    return r


reject_pair("acoustic_indices ndsi above 1", "acoustic-indices", AIL, ai0, with_ind(ai0, "ndsi", p50=1.5), "$.indices.ndsi.p50", "greater than the maximum of 1")
reject_pair("acoustic_indices hf above 1", "acoustic-indices", AIL, ai0, with_ind(ai0, "hf", p90=1.2), "$.indices.hf.p90", "greater than the maximum of 1")
reject_pair("acoustic_indices aci below 0", "acoustic-indices", AIL, ai0, with_ind(ai0, "aci", p10=-0.5), "$.indices.aci.p10", "less than the minimum of 0")
reject_pair("acoustic_indices level above 0 dBFS", "acoustic-indices", AIL, ai0, with_ind(ai0, "level_dbfs", p50=3.0), "$.indices.level_dbfs.p50", "greater than the maximum of 0")
ai_no = copy.deepcopy(ai0)
ai_no["indices"].pop("chorus_dbfs")
reject_pair("acoustic_indices needs every index (chorus_dbfs)", "acoustic-indices", AIL, ai0, ai_no, "$.indices", "'chorus_dbfs' is a required property")
ai_nn = copy.deepcopy(ai0)
ai_nn["indices"]["ht"]["p50"] = None
reject_pair("acoustic_indices a null index value is not allowed (non-finite windows are excluded, never stored)", "acoustic-indices", AIL, ai0, ai_nn, "$.indices.ht.p50", "is not of type 'number'")
ai_fr = copy.deepcopy(ai0)
ai_fr["params"]["frame"] = "hann 512 no overlap"
reject_pair("acoustic_indices frame is pinned", "acoustic-indices", AIL, ai0, ai_fr, "$.params.frame", "was expected")
check("acoustic-indices: the schema maps indices.jsonl to exactly the acoustic_indices kind; a kind that is not in the discriminator is drift (positive twin: acoustic_indices passes)",
      SCH["acoustic-indices"]["x-files"] == {AIL: ["acoustic_indices"]} and drifts("acoustic-indices", ai0, AIL) == []
      and rejected(drifts("acoustic-indices", mod(ai0, k="beam_coherence"), AIL), "$.k", "unknown record kind 'beam_coherence'"))
_ai_raw = open(os.path.join(FIX, AIF), "rb").read()
check("acoustic-indices fixture: LF line ends, no BOM, no coordinate-like key (lat, lon) anywhere",
      b"\r" not in _ai_raw and not _ai_raw.startswith(b"\xef\xbb\xbf") and b'"lat' not in _ai_raw and b'"lon' not in _ai_raw)


def m_ndsi(s): s["$defs"]["ndsi_stat"]["properties"]["p50"]["maximum"] = 0.05


d_ = C.run(mutated("acoustic_indices.schema.json", m_ndsi))[1]
check("mutation: acoustic_indices ndsi p50 maximum 0.05 -> the fixture records (NDSI about 0.12) are flagged with their path",
      any(d.file == AIF and d.path == "$.indices.ndsi.p50" and "greater than the maximum of 0.05" in d.message for d in d_), str([str(d) for d in d_][:2]))
ai_dir = os.path.join(tmp, "aicl")
mkcontracts(ai_dir)
_p = os.path.join(ai_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("acoustic-indices 1.0.0", "acoustic-indices x"))
d_ = C.run(ai_dir)[1]
check("mutation: the CHANGELOG without `acoustic-indices 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "acoustic-indices" for d in d_), str([str(d) for d in d_][:2]))
root_ai = os.path.join(tmp, "live_ai")
mkroot(root_ai)
_n0, _d0, _ = C.check_live(root_ai, SCH, 100)
shutil.copy(os.path.join(FIX, AIF), os.path.join(root_ai, "analysis", AIL))
_n1, _d1, _ = C.check_live(root_ai, SCH, 100)
with open(os.path.join(root_ai, "analysis", AIL), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(ai0, v=2)) + "\n")
_n2, _d2, _ = C.check_live(root_ai, SCH, 100)
check("live check: analysis/indices.jsonl is optional (absent: 0 drift), 4 fixture records add 4 and 0 drift, an appended v=2 record is reported",
      _d0 == [] and _d1 == [] and _n1 == _n0 + 4 and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("analysis/indices.jsonl", "$.v"), str((_n0, _n1, [str(d) for d in _d2][:2])))
try:                                                                     # the fixture regenerates from the real plugin (needs scipy; the contracts venv has none)
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import make_indices_fixtures as MIND  # noqa: E402
except ImportError as ex:
    skip("acoustic-indices fixture regeneration", f"the plugin needs scipy: {ex}; analysis/test_indices.py covers it")
else:
    _ai_regen = [json.loads(l) for l in open(os.path.join(MIND.build(os.path.join(tmp, "regen_ai")), AIF), encoding="utf-8")]

    def _same_ai(x, y):
        if isinstance(x, float) or isinstance(y, float):
            return isinstance(x, (int, float)) and isinstance(y, (int, float)) and abs(x - y) <= 0.01 + 1e-9
        if isinstance(x, dict):
            return isinstance(y, dict) and x.keys() == y.keys() and all(_same_ai(x[k], y[k]) for k in x)
        if isinstance(x, list):
            return isinstance(y, list) and len(x) == len(y) and all(_same_ai(p_, q_) for p_, q_ in zip(x, y))
        return x == y
    check("acoustic-indices fixture equals the output of the real plugin (floats within 0.01, everything else exact)", len(_ai_regen) == len(ais) and all(_same_ai(a_, b_) for a_, b_ in zip(_ai_regen, ais)))


# ============================================================================================ beam-response-map 1.0.0 (analysis/beam_response_map.py)
BRMF = "beam_response_map.json"
brm = jf(BRMF)
blk0 = brm["sources"][0]["epochs"][0]
check("beam-response-map fixture: validates against the closed schema; 18 source-band entries from the real tool; overall go; Engine skipped; no human-voice field; no epoch given",
      whole("beam-response-map", brm) == [] and len(brm["sources"]) == 18 and brm["overall"]["verdict"] == "go" and brm["skipped_sources"] == [{"name": "Engine", "reason": "non_biological"}]
      and "excluded_human" not in brm and brm["epochs"] is None and "beam pattern epoch unknown" in brm["warnings"], str(whole("beam-response-map", brm)[:2]))
check("beam-response-map fixture: verdicts are go (broadband and 4-8 kHz for the planted Barn Owl), identical_expected for the 3 bands below 500 Hz, no_go for the other bands and for Hairy Woodpecker",
      [(e["band"], e["verdict"]) for e in brm["sources"] if e["name"] == "Barn Owl"] == [("spatial_200_4000", "go"), ("b_50_125", "identical_expected"), ("b_125_250", "identical_expected"), ("b_250_500", "identical_expected"),
        ("b_500_1000", "no_go"), ("b_1000_2000", "no_go"), ("b_2000_4000", "no_go"), ("b_4000_8000", "go"), ("b_8000_16000", "no_go")]
      and {e["verdict"] for e in brm["sources"] if e["name"] == "Hairy Woodpecker" and e["band"] not in ("b_50_125", "b_125_250", "b_250_500")} == {"no_go"}, "")
whole_pair("beam_response_map version v", "beam-response-map", brm, mod(brm, v=2), "$.v", "1 was expected")
whole_pair("beam_response_map never carries a latitude", "beam-response-map", brm, mod(brm, lat=1.0), "$", "Additional properties are not allowed")
whole_pair("beam_response_map never carries a path", "beam-response-map", brm, mod(brm, path="x"), "$", "Additional properties are not allowed")
whole_pair("beam_response_map never carries an audio file key", "beam-response-map", brm, mod(brm, wav="x"), "$", "Additional properties are not allowed")
_bad_blk = copy.deepcopy(brm)
_bad_blk["sources"][0]["epochs"][0]["vecs"] = [[0.0] * 8]
whole_pair("beam_response_map carries no per-event vectors in a block", "beam-response-map", brm, _bad_blk, "$.sources[0].epochs[0]", "Additional properties are not allowed")
_bad_slash = copy.deepcopy(brm)
_bad_slash["sources"][0]["reason"] = "read a/b/c.jsonl"
whole_pair("beam_response_map free text cannot carry a path separator", "beam-response-map", brm, _bad_slash, "$.sources[0].reason", "does not match")
_bad_win = copy.deepcopy(brm)
_bad_win["warnings"] = ["opened data\\spatial.jsonl"]
whole_pair("beam_response_map warnings cannot carry a backslash path", "beam-response-map", brm, _bad_win, "$.warnings[0]", "does not match")
_bad_lag = copy.deepcopy(brm)
_bad_lag["sources"][0]["epochs"][0].pop("lag_scan")
whole_pair("beam_response_map an evaluated block needs its lag_scan", "beam-response-map", brm, _bad_lag, "$.sources[0].epochs[0]", "'lag_scan' is a required property")
_bad_ins = copy.deepcopy(brm)
_bad_ins["sources"][0]["epochs"][0]["verdict"] = "insufficient"
whole_pair("beam_response_map an insufficient block carries no statistics", "beam-response-map", brm, _bad_ins, "$.sources[0].epochs[0]", "should not be valid under")
_ins_ok = copy.deepcopy(brm)
_ins_ok["sources"][0]["epochs"][0] = {k: blk0[k] for k in ("label", "events", "bouts", "joined", "unjoined", "events_no_baseline")} | {"verdict": "insufficient", "reason": "2 joined events < 30"}
check("beam_response_map twin: a counts-only insufficient block is valid", whole("beam-response-map", _ins_ok) == [], str(whole("beam-response-map", _ins_ok)[:2]))
_ins_nb = copy.deepcopy(_ins_ok)
_ins_nb["sources"][0]["epochs"][0].pop("bouts")
whole_pair("beam_response_map a block without its bout count is rejected", "beam-response-map", _ins_ok, _ins_nb, "$.sources[0].epochs[0]", "'bouts' is a required property")
whole_pair("beam_response_map verdict vocabulary", "beam-response-map", brm, mod(brm, overall=mod(brm["overall"], verdict="great")), "$.overall.verdict", "is not one of")
_bad_band = copy.deepcopy(brm)
_bad_band["sources"][0]["band"] = "b_100_200"
whole_pair("beam_response_map band names are pinned", "beam-response-map", brm, _bad_band, "$.sources[0].band", "is not one of")
_bad_lag2 = copy.deepcopy(brm)
_bad_lag2["sources"][0]["epochs"][0]["lag_scan"] = blk0["lag_scan"][:20]
whole_pair("beam_response_map lag_scan has 21 shifts", "beam-response-map", brm, _bad_lag2, "$.sources[0].epochs[0].lag_scan", "too short")
_bad_r = copy.deepcopy(brm)
_bad_r["sources"][0]["epochs"][0]["R"] = 1.2
whole_pair("beam_response_map R above 1", "beam-response-map", brm, _bad_r, "$.sources[0].epochs[0].R", "greater than the maximum")
whole_pair("beam_response_map never reports a human-voice count", "beam-response-map", brm, mod(brm, excluded_human=20), "$", "Additional properties are not allowed")
_bad_bouts = copy.deepcopy(brm)
_bad_bouts["sources"][0]["epochs"][0]["bouts"] = -1
whole_pair("beam_response_map bouts is a count", "beam-response-map", brm, _bad_bouts, "$.sources[0].epochs[0].bouts", "less than the minimum of 0")
check("beam-response-map fixture: every block carries its bout count (40 Barn Owl events 100 s apart are 40 bouts)", brm["sources"][0]["epochs"][0]["bouts"] == 40 == brm["sources"][0]["epochs"][0]["joined"], str(brm["sources"][0]["epochs"][0]["bouts"]))
_raw_brm = open(os.path.join(FIX, BRMF), "rb").read()
check("beam-response-map fixture: LF, one line, no BOM, no Human, no coordinate-like key and no wav/path text", b"\r" not in _raw_brm and _raw_brm.count(b"\n") == 1 and not _raw_brm.startswith(b"\xef\xbb\xbf")
      and b"Human" not in _raw_brm and not any(x in _raw_brm for x in (b'"lat', b'"lon', b".wav", b"county")), "")


def m_brm_safe(s): s["$defs"]["safe"].pop("pattern")
def m_brm_closed(s): s.pop("additionalProperties")


_sch_safe = C.load_schemas(os.path.join(mutated("beam_response_map.schema.json", m_brm_safe), "schemas"))
_sch_open = C.load_schemas(os.path.join(mutated("beam_response_map.schema.json", m_brm_closed), "schemas"))
_e_safe = [e.json_path for e in C.errors(C.whole_validator(_sch_safe["beam-response-map"]), _bad_slash)]
_e_open = [e.json_path for e in C.errors(C.whole_validator(_sch_open["beam-response-map"]), mod(brm, lat=1.0))]
check("mutation: the schema without its path pattern accepts a slash in free text; without additionalProperties false it accepts a latitude (proves these rules do the rejecting above)",
      _e_safe == [] and _e_open == [], str((_e_safe, _e_open)))
brm_dir = os.path.join(tmp, "brmcl")
mkcontracts(brm_dir)
_p = os.path.join(brm_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("beam-response-map 1.0.0", "beam-response-map x"))
d_ = C.run(brm_dir)[1]
check("mutation: the CHANGELOG without `beam-response-map 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "beam-response-map" for d in d_), str([str(d) for d in d_][:2]))
root_brm = os.path.join(tmp, "live_brm")
mkroot(root_brm)
_rep = os.path.join(tmp, "elsewhere", BRMF)
os.makedirs(os.path.dirname(_rep))
_n0, _d0, _notes0 = C.check_live(root_brm, SCH, 100, None, os.path.join(tmp, "elsewhere", "absent.json"))
shutil.copy(os.path.join(FIX, BRMF), _rep)
_n1, _d1, _ = C.check_live(root_brm, SCH, 100, None, _rep)
with open(_rep, "w", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(brm, v=2), separators=(",", ":")) + "\n")
_n2, _d2, _ = C.check_live(root_brm, SCH, 100, None, _rep)
check("live check: the report is optional (absent path: a note, 0 drift), the fixture adds 1 record and 0 drift, a v=2 report is reported at $.v; without --brm-report nothing is looked for",
      _d0 == [] and any(n_.startswith("beam_response_map.json") for n_ in _notes0) and _d1 == [] and _n1 == _n0 + 1 and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("beam_response_map.json", "$.v")
      and not any(n_.startswith("beam_response_map") for n_ in C.check_live(root_brm, SCH, 100)[2]), str((_n0, _n1, [str(d) for d in _d2][:2])))
sys.path.insert(0, os.path.join(HERE, "tools"))
import make_beam_response_map_fixtures as MBRM  # noqa: E402
_brm_regen = open(MBRM.build(os.path.join(tmp, "regen_brm")), "rb").read()
check("beam-response-map fixture equals the output of the real tool byte for byte", _brm_regen == _raw_brm, _brm_regen[:80])

# ============================================================================================ burst-scorecard 1.0.0 (tools/burst_source_report.py)
BSC, BHF = "burst_scorecard.json", "burst_hours.jsonl"
bhs = jl(BHF)
bh0 = bhs[0]
bsc = jf(BSC)
check("burst-scorecard fixtures: 30 burst_hour records and one scorecard from the real tool, validating against the schema (hours consecutive, 10 burst hours, 2 nights)",
      len(bhs) == 30 and all(not drifts("burst-scorecard", r, BHF) for r in bhs) and whole("burst-scorecard", bsc) == [] and [r["hour_utc"] for r in bhs] == [bhs[0]["hour_utc"] + 3600 * i for i in range(30)]
      and bsc["n_burst_hours"] == 10 and bsc["n_nights_burst"] == 2 and sum(1 for r in bhs if r["is_burst"]) == 10, str(bsc["n_burst_hours"]))
check("burst-scorecard fixtures cover the optional blocks: a bursts block with ioi_source bursts, a board block, a spatial and a band block, a coherence count",
      any(r["ioi_source"] == "bursts" and r["bursts"]["regularity"] == 0.9 for r in bhs) and any(r["board"] and r["board"]["dtdt_c_per_h"] is not None for r in bhs)
      and all(r["spatial"] is not None and r["band_8_16k"] is not None for r in bhs) and any(r["coherence"]["n_records"] == 1 for r in bhs), "")
reject_pair("burst_hour version v", "burst-scorecard", BHF, bh0, mod(bh0, v=2), "$.v", "1 was expected")
reject_pair("burst_hour hour_utc is on an hour boundary", "burst-scorecard", BHF, bh0, mod(bh0, hour_utc=bh0["hour_utc"] + 1), "$.hour_utc", "is not a multiple of 3600")
reject_pair("burst_hour covered_s above 3600", "burst-scorecard", BHF, bh0, mod(bh0, covered_s=3601), "$.covered_s", "greater than the maximum of 3600")
reject_pair("burst_hour source is raw or digest", "burst-scorecard", BHF, bh0, mod(bh0, source="live"), "$.source", "is not one of")
reject_pair("burst_hour excluded_reason is null or low_coverage", "burst-scorecard", BHF, mod(bh0, excluded_reason="low_coverage"), mod(bh0, excluded_reason="nope"), "$.excluded_reason", "is not one of")
reject_pair("burst_hour beam counts has exactly 8 entries", "burst-scorecard", BHF, bh0, mod(bh0, beam=dict(bh0["beam"], counts=[1] * 7)), "$.beam.counts", "is too short")
reject_pair("burst_hour ambient age at most 3 h", "burst-scorecard", BHF, mod(bh0, ambient=dict(bh0["ambient"], age_h=3)), mod(bh0, ambient=dict(bh0["ambient"], age_h=4)), "$.ambient.age_h", "greater than the maximum of 3")
reject_pair("burst_hour acorr is a closed object", "burst-scorecard", BHF, bh0, mod(bh0, acorr=dict(bh0["acorr"], extra=1)), "$.acorr", "Additional properties are not allowed")
reject_pair("burst_hour needs ioi_source", "burst-scorecard", BHF, bh0, mod(bh0, ioi_source=KeyError), "$", "'ioi_source' is a required property")
reject_pair("burst_hour never carries a latitude", "burst-scorecard", BHF, bh0, mod(bh0, latitude=12.3), "$", "should not be valid under")
reject_pair("burst_hour never carries a path", "burst-scorecard", BHF, bh0, mod(bh0, path="x"), "$", "should not be valid under")
check("burst-scorecard: the schema maps burst_hours.jsonl to the burst_hour kind and burst_scorecard.json to burst_scorecard; a burst_hour in the scorecard file is drift (twin: in its own file it passes)",
      SCH["burst-scorecard"]["x-files"] == {BSC: ["burst_scorecard"], BHF: ["burst_hour"]} and drifts("burst-scorecard", bh0, BHF) == []
      and rejected(drifts("burst-scorecard", bh0, BSC), "$.k", "kind 'burst_hour' is not allowed in burst_scorecard.json"), "")
whole_pair("burst_scorecard verdict name", "burst-scorecard", bsc, dict(bsc, verdicts=dict(bsc["verdicts"], insect=dict(bsc["verdicts"]["insect"], verdict="maybe"))), "$", "is not valid under any of the given schemas")
whole_pair("burst_scorecard sha256 is 64 hex digits", "burst-scorecard", bsc, dict(bsc, thresholds_sha256="abc"), "$", "is not valid under any of the given schemas")
whole_pair("burst_scorecard needs all nine tests", "burst-scorecard", bsc, dict(bsc, tests={k: v for k, v in bsc["tests"].items() if k != "T9"}), "$", "is not valid under any of the given schemas")
whole_pair("burst_scorecard never carries a longitude", "burst-scorecard", bsc, dict(bsc, longitude=56.8), "$", "is not valid under any of the given schemas")
_bs_raw = open(os.path.join(FIX, BSC), "rb").read() + open(os.path.join(FIX, BHF), "rb").read()
check("burst-scorecard fixtures: LF line ends, no BOM, no coordinate key and no path in either file", b"\r" not in _bs_raw and not _bs_raw.startswith(b"\xef\xbb\xbf")
      and not any(s_ in _bs_raw for s_ in (b'"lat"', b'"lon"', b'"latitude"', b'"longitude"', b'"path"', b"/home/")), _bs_raw[:80])


def m_bs_hour(s): s["$defs"]["burst_hour"]["properties"]["hour_utc"]["multipleOf"] = 7200


d_ = C.run(mutated("burst_scorecard.schema.json", m_bs_hour))[1]
check("mutation: burst_hour hour multipleOf 7200 -> the odd-hour fixture records are flagged with their path",
      any(d.file == BHF and d.path == "$.hour_utc" and "is not a multiple of 7200" in d.message for d in d_), str([str(d) for d in d_][:2]))


def m_bs_loc(s): s["$defs"]["burst_hour"].pop("not"); s["$defs"]["burst_hour"].pop("additionalProperties")


def m_bs_not(s): s["$defs"]["burst_hour"].pop("not")


_sch_bs = C.load_schemas(os.path.join(mutated("burst_scorecard.schema.json", m_bs_loc), "schemas"))
_sch_bn = C.load_schemas(os.path.join(mutated("burst_scorecard.schema.json", m_bs_not), "schemas"))
check("mutation: without the closed object AND the location ban a burst_hour with a latitude is accepted (twin: with only the explicit ban removed the closed object still rejects it)",
      C.check_record(BHF, "1", mod(bh0, latitude=12.3), _sch_bs["burst-scorecard"], ["burst_hour"], {}) == []
      and rejected(C.check_record(BHF, "1", mod(bh0, latitude=12.3), _sch_bn["burst-scorecard"], ["burst_hour"], {}), "$", "Additional properties are not allowed"), "")
bs_dir = os.path.join(tmp, "bscl")
mkcontracts(bs_dir)
_p = os.path.join(bs_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("burst-scorecard 1.0.0", "burst-scorecard x"))
d_ = C.run(bs_dir)[1]
check("mutation: the CHANGELOG without `burst-scorecard 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "burst-scorecard" for d in d_), str([str(d) for d in d_][:2]))
import make_burst_source_fixtures as MBS  # noqa: E402
_bs_out = MBS.build_contract_fixtures(os.path.join(tmp, "regen_bs"))
check("burst-scorecard fixtures equal the output of the real tool byte for byte",
      open(os.path.join(_bs_out, BSC), "rb").read() == open(os.path.join(FIX, BSC), "rb").read() and open(os.path.join(_bs_out, BHF), "rb").read() == open(os.path.join(FIX, BHF), "rb").read())

# ============================================================================================ beam-agreement 1.0.0 (analysis/beam_agreement.py)
BAF = "beam_agreement_report.json"
ba = jf(BAF)
check("beam-agreement fixture: the real tool's report validates; decision reframe_diffuse (owner labels significant, but the control out-scores birds); 240 labelled events, OR 4.0 over 6 strata",
      whole("beam-agreement", ba) == [] and ba["decision"] == "reframe_diffuse" and ba["reasons"] == ["artifact_scores_higher"]
      and ba["outcomes"]["o1"]["n"] == 240 and ba["outcomes"]["o1"]["or"] == 4.0 and ba["outcomes"]["o1"]["strata_used"] == 6
      and ba["negative_control"]["artifact_scores_higher"] is True, str(ba["decision"]))
whole_pair("beam_agreement decision is a closed set", "beam-agreement", ba, mod(ba, decision="ship_it"), "$.decision", "is not one of")
whole_pair("beam_agreement needs reasons", "beam-agreement", ba, mod(ba, reasons=[]), "$.reasons", "should be non-empty")
whole_pair("beam_agreement has exactly 5 confidence bins", "beam-agreement", ba, mod(ba, bins=ba["bins"][:4]), "$.bins", "is too short")
whole_pair("beam_agreement needs generated_s", "beam-agreement", ba, mod(ba, generated_s=KeyError), "$", "'generated_s' is a required property")
whole_pair("beam_agreement o1 is 'unavailable' or an object, not another string", "beam-agreement", mod(ba, outcomes=dict(ba["outcomes"], o1="unavailable")),
           mod(ba, outcomes=dict(ba["outcomes"], o1="none")), "$.outcomes.o1", "is not valid under any of the given schemas")
whole_pair("beam_agreement window rule is exact_t", "beam-agreement", ba, mod(ba, definition=dict(ba["definition"], window_equality="rounded")), "$.definition.window_equality", "'exact_t' was expected")
whole_pair("beam_agreement tier counts are integers >= 0", "beam-agreement", ba,
           mod(ba, bins=[dict(ba["bins"][0], tiers=dict(ba["bins"][0]["tiers"], confirmed=-1))] + ba["bins"][1:]), "$.bins[0].tiers.confirmed", "less than the minimum of 0")
whole_pair("beam_agreement never carries a coordinate or county key", "beam-agreement", ba, mod(ba, latitude=1.5), "$", "Additional properties are not allowed")
_ba_raw = open(os.path.join(FIX, BAF), "rb").read()
check("beam-agreement fixture: one LF-terminated line, no BOM, no path or drive letter, no lat/lon key",
      _ba_raw.count(b"\n") == 1 and _ba_raw.endswith(b"\n") and b"\r" not in _ba_raw and not _ba_raw.startswith(b"\xef\xbb\xbf")
      and not any(x_ in _ba_raw for x_ in (b":\\\\", b"/home/", b"\"lat\"", b"\"lon\"")), _ba_raw[:80])
import make_beam_agreement_fixtures as MBA  # noqa: E402
_ba_regen = open(MBA.build(os.path.join(tmp, "regen_ba")), "rb").read()
check("beam-agreement fixture equals the output of the real tool byte for byte (generated_s is a parameter, so the run is deterministic)", _ba_regen == _ba_raw, _ba_regen[:80])


sn_ = ba["shuffle_null"]
check("beam-agreement 1.1.0 fixture: shuffle_null from the real tool, 200 draws seed 20261027 over 202 groups; pooled real 0.5 vs null 0.512375 (excess -0.012375); quantile cuts 0.3 0.6 0.75 0.9, gating false",
      sn_["trials"] == 200 and sn_["seed"] == 20261027 and sn_["groups"] == 202 and sn_["all"]["n"] == 480 and sn_["all"]["real_pass_rate"] == 0.5
      and sn_["all"]["null_pass_rate"] == 0.512375 and sn_["all"]["excess"] == -0.012375 and sn_["quantile_bands"]["cuts"] == [0.3, 0.6, 0.75, 0.9]
      and sn_["quantile_bands"]["gating"] is False and len(sn_["bins"]) == 5, str(sn_["all"]))
ba_old = {k: v for k, v in ba.items() if k != "shuffle_null"}
check("beam-agreement 1.1.0: a 1.0.0-shaped report (no shuffle_null) still validates (the block is optional)", whole("beam-agreement", ba_old) == [])
whole_pair("beam_agreement shuffle_null quantile bands are never gating", "beam-agreement", ba,
           mod(ba, shuffle_null=dict(sn_, quantile_bands=dict(sn_["quantile_bands"], gating=True))), "$.shuffle_null.quantile_bands.gating", "False was expected")
whole_pair("beam_agreement shuffle_null has exactly 5 confidence bins", "beam-agreement", ba, mod(ba, shuffle_null=dict(sn_, bins=sn_["bins"][:4])), "$.shuffle_null.bins", "is too short")
whole_pair("beam_agreement shuffle_null trials are >= 0", "beam-agreement", ba, mod(ba, shuffle_null=dict(sn_, trials=-1)), "$.shuffle_null.trials", "less than the minimum of 0")
whole_pair("beam_agreement shuffle_null rows are closed", "beam-agreement", ba, mod(ba, shuffle_null=dict(sn_, all=dict(sn_["all"], extra=1))), "$.shuffle_null.all", "Additional properties are not allowed")


def m_ba_enum(s): s["properties"]["decision"]["enum"] = [x for x in s["properties"]["decision"]["enum"] if x != "reframe_diffuse"]


d_ = C.run(mutated("beam_agreement_report.schema.json", m_ba_enum))[1]
check("mutation: decision enum without reframe_diffuse -> the fixture is flagged at $.decision",
      any(d.file == BAF and d.path == "$.decision" and "is not one of" in d.message for d in d_), str([str(d) for d in d_][:2]))

# ============================================================================================ event-model 1.0.0 (analysis/events.py, tools/bma_labels.py events)
EMF = "event_model_report.json"
em = jf(EMF)
check("event-model fixture: the real tool's report validates; 9 input = 7 reports in 4 events + 2 rejections (invalid 1, other_kind 1); labels exact 1, superseded 1, orphaned 1",
      whole("event-model", em) == [] and em["conservation"]["input"] == 9 and em["conservation"]["events"] == 4 and em["conservation"]["reports"] == 7
      and em["conservation"]["rejected"] == {"other_kind": 1, "out_of_region": 0, "invalid": 1, "hidden_human": 0, "duplicate_window": 0}
      and em["labels"] == {"exact": 1, "superseded": 1, "orphaned": 1, "ambiguous": 0}, str(em))
check("event-model fixture: input == reports + sum(rejected)", em["conservation"]["input"] == em["conservation"]["reports"] + sum(em["conservation"]["rejected"].values()))
whole_pair("event_model needs every rejection name", "event-model", em, mod(em, conservation=dict(em["conservation"], rejected={"invalid": 1})), "$.conservation.rejected", "is a required property")
whole_pair("event_model counts are integers >= 0", "event-model", em, mod(em, labels=dict(em["labels"], orphaned=-1)), "$.labels.orphaned", "less than the minimum of 0")
whole_pair("event_model id_version is ev1", "event-model", em, mod(em, id_version="ev2"), "$.id_version", "'ev1' was expected")
whole_pair("event_model carries no extra key", "event-model", em, mod(em, latitude=1.5), "$", "Additional properties are not allowed")
_em_raw = open(os.path.join(FIX, EMF), "rb").read()
check("event-model fixture: one LF-terminated line, no BOM, no path", _em_raw.count(b"\n") == 1 and _em_raw.endswith(b"\n") and b"\r" not in _em_raw
      and not _em_raw.startswith(b"\xef\xbb\xbf") and b":\\\\" not in _em_raw, _em_raw[:80])
import make_event_fixtures as MEV  # noqa: E402
_em_regen = open(MEV.build(os.path.join(tmp, "regen_em")), "rb").read()
check("event-model fixture equals the output of the real tool byte for byte", _em_regen == _em_raw, _em_regen[:80])


def m_em_count(s): s["$defs"]["count"]["minimum"] = 1


d_ = C.run(mutated("event_model.schema.json", m_em_count))[1]
check("mutation: count minimum 1 -> the fixture's zero counts are flagged",
      any(d.file == EMF and "less than the minimum of 1" in d.message for d in d_), str([str(d) for d in d_][:2]))

# ============================================================================================ perch-report 1.0.0 (tools/perch_report.py)
PRF = "perch_report.json"
pr = jf(PRF)
check("perch-report fixture: the real tool's report validates; 6 compared windows, 3 agree (rate 0.5), 3 disagreements, RBWO never named, 2 windows excluded as human voice",
      whole("perch-report", pr) == [] and pr["agreement"] == {"compared": 6, "agree": 3, "rate": 0.5} and pr["disagreements"]["total"] == 3
      and [r["key"] for r in pr["coverage"]["never_named"]] == ["melanerpes carolinus"] and pr["input"]["excluded_human_voice"] == 2, str(pr["agreement"]))
whole_pair("perch_report confidence bin key is a closed set", "perch-report", pr, mod(pr, by_confidence_bin=[dict(pr["by_confidence_bin"][0], key="0.1-0.2")]), "$.by_confidence_bin[0].key", "is not one of")
whole_pair("perch_report beam key is at most 8", "perch-report", pr, mod(pr, by_beam=[dict(pr["by_beam"][0], key=9)]), "$.by_beam[0].key", "greater than the maximum of 8")
whole_pair("perch_report never carries a path", "perch-report", pr, mod(pr, path="x"), "$", "Additional properties are not allowed")
_pr_raw = open(os.path.join(FIX, PRF), "rb").read()
check("perch-report fixture: one LF-terminated line, no BOM, no path or drive letter, no lat/lon key",
      _pr_raw.count(b"\n") == 1 and _pr_raw.endswith(b"\n") and b"\r" not in _pr_raw and not _pr_raw.startswith(b"\xef\xbb\xbf")
      and not any(x_ in _pr_raw for x_ in (b":\\", b"/home/", b"\"lat\"", b"\"lon\"")), _pr_raw[:80])
import make_perch_report_fixtures as MPR  # noqa: E402
_pr_regen = open(MPR.build(os.path.join(tmp, "regen_pr")), "rb").read()
check("perch-report fixture equals the output of the real tool byte for byte", _pr_regen == _pr_raw, _pr_regen[:80])

# ============================================================================================ eval-report 1.0.0 (tools/eval_models.py)
EVF = "eval_report.json"
ev = jf(EVF)
check("eval-report fixture: the real tool's report validates; birdnet 18 scored (12 top-1 exact, 4 near), perch 14, clap not_measurable, 18 species truths, 3 rejections",
      whole("eval-report", ev) == [] and [(v["name"], v["n"], v["status"]) for v in ev["variants"]] == [("birdnet", 18, "ok"), ("perch", 14, "ok"), ("tags", 18, "ok"), ("clap", 5, "not_measurable")]
      and ev["variants"][0]["top1"]["exact"] == 12 and ev["variants"][0]["top1"]["near"] == 4 and ev["input"]["species_truth"] == 18 and ev["input"]["rejections"] == 3, str(ev["input"]))
whole_pair("eval_report calibration bin key is a closed set", "eval-report", ev, mod(ev, variants=[dict(ev["variants"][0], calibration=dict(ev["variants"][0]["calibration"], bins=[dict(ev["variants"][0]["calibration"]["bins"][0], bin="0.1-0.3")]))] + ev["variants"][1:]), "$.variants[0].calibration.bins[0].bin", "is not one of")
whole_pair("eval_report McNemar p above 1 is rejected", "eval-report", ev, mod(ev, pairs=[dict(ev["pairs"][0], top1=dict(ev["pairs"][0]["top1"], p=1.5))] + ev["pairs"][1:]), "$.pairs[0].top1", "is not valid under any of the given schemas")
whole_pair("eval_report never carries a path", "eval-report", ev, mod(ev, path="x"), "$", "Additional properties are not allowed")
_ev_raw = open(os.path.join(FIX, EVF), "rb").read()
check("eval-report fixture: one LF-terminated line, no BOM, no path or drive letter, no lat/lon key, no human-voice label",
      _ev_raw.count(b"\n") == 1 and _ev_raw.endswith(b"\n") and b"\r" not in _ev_raw and not _ev_raw.startswith(b"\xef\xbb\xbf")
      and not any(x_ in _ev_raw for x_ in (b":\\", b"/home/", b"\"lat\"", b"\"lon\"", b"Human", b"Speech")), _ev_raw[:80])
import make_eval_report_fixtures as MEV  # noqa: E402
_ev_regen = open(MEV.build(os.path.join(tmp, "regen_ev")), "rb").read()
check("eval-report fixture equals the output of the real tool byte for byte", _ev_regen == _ev_raw, _ev_regen[:80])

# ============================================================================================ active-coherence-report 1.0.0 (tools/active_coherence.py)
ACF = "active_coherence_report.json"
ac = jf(ACF)
check("active-coherence-report fixture: the real tool's report validates; 13 multi-beam events, 10 isolating, Barn Owl and Tufted Titmouse ok, Eastern Screech-Owl insufficient, 3 human-voice rows excluded",
      whole("active-coherence-report", ac) == [] and ac["input"]["events_multi_beam"] == 13 and ac["measurability"]["isolating_events"] == 10
      and [(s["species"], s["bands_ok"]) for s in ac["species"]] == [("Barn Owl", 1), ("Eastern Screech-Owl", 0), ("Tufted Titmouse", 1)]
      and ac["input"]["excluded_human_voice"] == 3, str(ac["input"]))
whole_pair("active_coherence_report method verdict vocabulary", "active-coherence-report", ac, mod(ac, methods=dict(ac["methods"], duet=dict(ac["methods"]["duet"], verdict="maybe"))), "$.methods.duet.verdict", "is not one of")
whole_pair("active_coherence_report row status is a closed set", "active-coherence-report", ac, mod(ac, rows=[dict(ac["rows"][0], status="good")] + ac["rows"][1:]), "$.rows[0].status", "is not one of")
whole_pair("active_coherence_report never carries a path", "active-coherence-report", ac, mod(ac, path="x"), "$", "Additional properties are not allowed")
whole_pair("active_coherence_report band is at most 7", "active-coherence-report", ac, mod(ac, rows=[dict(ac["rows"][0], band=8)] + ac["rows"][1:]), "$.rows[0].band", "greater than the maximum of 7")
_ac_raw = open(os.path.join(FIX, ACF), "rb").read()
check("active-coherence-report fixture: one LF-terminated line, no BOM, no path or drive letter, no lat/lon key",
      _ac_raw.count(b"\n") == 1 and _ac_raw.endswith(b"\n") and b"\r" not in _ac_raw and not _ac_raw.startswith(b"\xef\xbb\xbf")
      and not any(x_ in _ac_raw for x_ in (b":\\", b"/home/", b"\"lat\"", b"\"lon\"")), _ac_raw[:80])
import make_active_coherence_fixtures as MAC  # noqa: E402
_ac_regen = open(MAC.build(os.path.join(tmp, "regen_ac")), "rb").read()
check("active-coherence-report fixture equals the output of the real tool byte for byte", _ac_regen == _ac_raw, _ac_regen[:80])

# ============================================================================================ artifact-gate 1.0.0 (analysis/artifact_gate.py, tools/artifact_replay.py)
AGF = "artifact_gate_report.json"
agr = jf(AGF)
ag_raw = open(os.path.join(FIX, AGF), "rb").read()
sys.path.insert(0, os.path.join(BMA, "analysis"))
import artifact_gate as AG  # noqa: E402
_ag_sch = SCH["artifact-gate"]
_ag_flags_v = C.Draft202012Validator({"$schema": _ag_sch["$schema"], "$ref": "#/$defs/artifact_flags", "$defs": _ag_sch["$defs"]})


def ag_flag_errs(rec):
    return [(e.json_path, e.message) for e in C.errors(_ag_flags_v, rec)]


check("artifact-gate fixture: validates as a whole document, a valid (not degraded) run: 5 hidden rows excluded, confusion 5/4/3/10/2/1",
      whole("artifact-gate", agr) == [] and agr["experiment"] == {"valid": True} and agr["coverage"]["degraded"] is False and agr["excluded_hidden_reports"] == 5
      and agr["confusion"] == {"cold_burst": 5, "cold_noburst": 4, "warm_burst": 3, "warm_noburst": 10, "unknown_burst": 2, "unknown_noburst": 1}, whole("artifact-gate", agr)[:2])
whole_pair("artifact_replay version v", "artifact-gate", agr, mod(agr, v=2), "$.v", "1 was expected")
whole_pair("artifact_replay kind k", "artifact-gate", agr, mod(agr, k="x"), "$.k", "'artifact_replay' was expected")
whole_pair("artifact_replay never carries a latitude", "artifact-gate", agr, mod(agr, latitude=12.3), "$", "should not be valid under")
whole_pair("artifact_replay never carries a lon", "artifact-gate", agr, mod(agr, lon=56.8), "$", "should not be valid under")
whole_pair("artifact_replay degraded reason must be from the closed set", "artifact-gate", mod(agr, coverage=mod(agr["coverage"], degraded_reasons=["taxa_missing"])),
           mod(agr, coverage=mod(agr["coverage"], degraded_reasons=["whatever"])), "$.coverage.degraded_reasons[0]", "is not one of")
whole_pair("artifact_replay input sha256 is 64 hex digits", "artifact-gate", agr, mod(agr, coverage=mod(agr["coverage"], inputs=[mod(agr["coverage"]["inputs"][0], sha256="abc")])),
           "$.coverage.inputs[0].sha256", "does not match")
whole_pair("artifact_replay needs excluded_hidden_reports", "artifact-gate", agr, mod(agr, excluded_hidden_reports=KeyError), "$", "'excluded_hidden_reports' is a required property")
whole_pair("artifact_replay hour histograms have 24 bins", "artifact-gate", agr, mod(agr, flagged_by_hour=mod(agr["flagged_by_hour"], utc_hour={"flagged": [0] * 23, "unflagged": [0] * 24})),
           "$.flagged_by_hour.utc_hour.flagged", "is too short")
whole_pair("artifact_replay out_of_scope_counts values are counts", "artifact-gate", agr, mod(agr, out_of_scope_counts={"Engine": -1}),
           "$.out_of_scope_counts.Engine", "is less than the minimum")
# the real producer of artifact_flags validated against $defs/artifact_flags
_ag_amb = AG.AmbientIndex([{"k": "ambient_temp", "v": 1, "t_hour_utc": 1792972800, "temp_c": 2.04, "source": "open-meteo", "model_grid_km": None}])
_ag_tr = AG.TransientIndex([(1792974600.0 + 0.1 * i, 1.0) for i in range(25)])
_ag_good = AG.evaluate(group="insect", label="Common Virtuoso Katydid", scientific="Neoconocephalus triops", t=1792974600.0, ambient=_ag_amb, transients=_ag_tr)
_ag_mech = AG.evaluate(group="mechanical", label="Engine", scientific="Engine", t=1792974600.0, ambient=_ag_amb, transients=_ag_tr)
_ag_unk = AG.evaluate(group="insect", label="Common Virtuoso Katydid", scientific="Neoconocephalus triops", t=1792974600.0, ambient=None, transients=None)
check("artifact_flags from the real gate: likely+burst, mechanical_label and temp_unknown-only all validate; the values are exact",
      ag_flag_errs(_ag_good) == [] and ag_flag_errs(_ag_mech) == [] and ag_flag_errs(_ag_unk) == []
      and _ag_good == {"v": 1, "flags": ["burst_coincident", "cold_insect"], "level": "likely", "evidence": {"temp_c": 2.0, "temp_age_s": 0, "floor_c": 5.0, "burst_n": 25, "burst_spread_db": 1.0, "window_s": 30.0}}
      and _ag_unk["flags"] == ["temp_unknown"], (ag_flag_errs(_ag_good), _ag_good))
check("artifact_flags: an unknown flag is rejected (twin: each of the 4 known flags is accepted)",
      any("is not one of" in m for _p, m in ag_flag_errs(mod(_ag_good, flags=["nonsense"])))
      and all(ag_flag_errs(mod(_ag_good, flags=[f_])) == [] for f_ in AG.REASONS), ag_flag_errs(mod(_ag_good, flags=["nonsense"])))
check("artifact_flags: a duplicate flag, an unknown level, v 2, a latitude and an extra evidence key are each rejected (twin: the good object passes)",
      ag_flag_errs(_ag_good) == []
      and any("non-unique" in m for _p, m in ag_flag_errs(mod(_ag_good, flags=["cold_insect", "cold_insect"])))
      and any(p == "$.level" for p, _m in ag_flag_errs(mod(_ag_good, level="certain")))
      and any(p == "$.v" for p, _m in ag_flag_errs(mod(_ag_good, v=2)))
      and any("should not be valid under" in m for _p, m in ag_flag_errs(mod(_ag_good, latitude=12.3)))
      and any("Additional properties" in m for _p, m in ag_flag_errs(mod(_ag_good, evidence=mod(_ag_good["evidence"], lat=1.0)))), "")
check("artifact_flags: evidence temp_c 61 is rejected, burst_spread_db -1 is rejected (twins: 60.0 and 0.0 pass)",
      ag_flag_errs(mod(_ag_good, evidence=mod(_ag_good["evidence"], temp_c=60.0, burst_spread_db=0.0))) == []
      and any(p == "$.evidence.temp_c" for p, _m in ag_flag_errs(mod(_ag_good, evidence=mod(_ag_good["evidence"], temp_c=61.0))))
      and any(p == "$.evidence.burst_spread_db" for p, _m in ag_flag_errs(mod(_ag_good, evidence=mod(_ag_good["evidence"], burst_spread_db=-1.0)))), "")
check("artifact-gate fixture: LF line ends, no BOM; neither 'human' nor 'speech' appears; no coordinate keys; the sha256 values are 64 hex digits",
      b"\r" not in ag_raw and not ag_raw.startswith(b"\xef\xbb\xbf") and b"human" not in ag_raw.lower() and b"speech" not in ag_raw.lower()
      and b'"lat' not in ag_raw and b'"lon' not in ag_raw and all(len(i["sha256"]) == 64 for i in agr["coverage"]["inputs"]), ag_raw[:80])
check("artifact-gate: the schema root is the report (no x-discriminator, like analysis-status): a bare artifact_flags object is NOT a valid whole document",
      "x-discriminator" not in _ag_sch and whole("artifact-gate", _ag_good) != [] and whole("artifact-gate", agr) == [], "")
sys.path.insert(0, os.path.join(HERE, "tools"))
import make_artifact_gate_fixtures as MAG  # noqa: E402
_ag_regen = open(MAG.build(os.path.join(tmp, "regen_ag")), "rb").read()
check("artifact-gate fixture equals the output of the real replay byte for byte", _ag_regen == ag_raw, _ag_regen[:80])


def m_ag_loc(s): s["$defs"]["replay_report"].pop("not")


_ag_noban = C.load_schemas(os.path.join(mutated("artifact_gate.schema.json", m_ag_loc), "schemas"))["artifact-gate"]
check("mutation: the replay_report schema without its location ban accepts a latitude (proves the ban is what rejects it above)",
      list(C.whole_validator(_ag_noban).iter_errors(mod(agr, latitude=12.3))) == [] and whole("artifact-gate", mod(agr, latitude=12.3)) != [], "")


def m_ag_reason(s): s["$defs"]["reason"]["enum"].append("nonsense")


_ag_m = C.load_schemas(os.path.join(mutated("artifact_gate.schema.json", m_ag_reason), "schemas"))["artifact-gate"]
_ag_v3 = C.Draft202012Validator({"$schema": _ag_m["$schema"], "$ref": "#/$defs/artifact_flags", "$defs": _ag_m["$defs"]})
check("mutation: a schema whose reason enum gained 'nonsense' accepts the unknown flag (proves the enum is what rejects it above)",
      list(_ag_v3.iter_errors(mod(_ag_good, flags=["nonsense"]))) == [] and ag_flag_errs(mod(_ag_good, flags=["nonsense"])) != [], "")
ag_dir = os.path.join(tmp, "agcl")
mkcontracts(ag_dir)
_p = os.path.join(ag_dir, "CHANGELOG.md")
_cl_ok = open(_p, encoding="utf-8").read()
open(_p, "w", encoding="utf-8", newline="\n").write(_cl_ok.replace("artifact-gate 1.0.0", "artifact-gate x"))
d_ = C.run(ag_dir)[1]
check("mutation: the CHANGELOG without `artifact-gate 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "artifact-gate" for d in d_), str([str(d) for d in d_][:2]))
open(_p, "w", encoding="utf-8", newline="\n").write(_cl_ok)
check("twin: the restored CHANGELOG is clean again", C.run(ag_dir)[1] == [], "")
open(os.path.join(ag_dir, "fixtures", AGF), "w", encoding="utf-8", newline="\n").write(json.dumps(mod(agr, excluded_hidden_reports=-1)))
d_ = C.run(ag_dir)[1]
check("mutation: a fixture with excluded_hidden_reports -1 is reported against the fixture file", any(d.file == AGF and d.path == "$.excluded_hidden_reports" for d in d_), str([str(d) for d in d_][:2]))
# ============================================================================================ loss-log 1.0.0 (tools/loss_log.py)
LLF = "loss_log.jsonl"
lls = jl(LLF)
ll1 = lls[1]
_ll_rs = [[r_[s_]["reason"] for s_ in ("bridge", "sink", "analyzer")] for r_ in lls]
check("loss-log fixture: 9 records from the real tool, all validating; reasons per tick: first, exact, exact (burst), sink stale + analyzer missing, bridge restart, analyzer unreadable, exact, exact, exact",
      len(lls) == 9 and all(not drifts("loss-log", r_, LLF) for r_ in lls)
      and _ll_rs == [["first"] * 3, [None] * 3, [None] * 3, [None, "stale", "missing"], ["restart", None, None], [None, None, "unreadable"], [None] * 3, [None] * 3, [None] * 3], str(_ll_rs))
check("loss-log fixture: exact values (burst tick: bridge 8800 datagrams / 120 gaps / loss_pct 1.35; restart tick: top-level restart true; recovery spans 60.0 s)",
      (lls[2]["bridge"]["datagrams"], lls[2]["bridge"]["gaps"], lls[2]["bridge"]["loss_pct"]) == (8800, 120, 1.35)
      and [r_["restart"] for r_ in lls] == [False, False, False, False, True, False, False, False, False]
      and lls[4]["bridge"]["datagrams"] is None and lls[4]["sink"]["span_s"] == 60.0 and lls[3]["sink"]["age_s"] == 30.5
      and lls[3]["analyzer"]["age_s"] is None and [r_["t"] for r_ in lls] == [1793016000.5 + 30 * i_ for i_ in range(9)], str(lls[2]))
reject_pair("loss version v", "loss-log", LLF, ll1, mod(ll1, v=2), "$.v", "1 was expected")
reject_pair("loss: reason null needs integer deltas", "loss-log", LLF, ll1, mod(ll1, bridge=dict(ll1["bridge"], datagrams=None)), "$.bridge.datagrams", "None is not of type 'integer'")
reject_pair("loss: a stale source carries no deltas", "loss-log", LLF, lls[3], mod(lls[3], sink=dict(lls[3]["sink"], datagrams=5)), "$.sink.datagrams", "5 is not of type 'null'")
reject_pair("loss: a stale source carries no span", "loss-log", LLF, lls[3], mod(lls[3], sink=dict(lls[3]["sink"], span_s=30.0)), "$.sink.span_s", "30.0 is not of type 'null'")
reject_pair("loss: restart is true exactly for reason restart (reason null, restart true)", "loss-log", LLF, ll1, mod(ll1, bridge=dict(ll1["bridge"], restart=True)), "$.bridge.restart", "False was expected")
reject_pair("loss: restart is true exactly for reason restart (reason restart, restart false)", "loss-log", LLF, lls[4], mod(lls[4], bridge=dict(lls[4]["bridge"], restart=False)), "$.bridge.restart", "True was expected")
reject_pair("loss: reason is a closed set", "loss-log", LLF, ll1, mod(ll1, bridge=dict(ll1["bridge"], reason="gone")), "$.bridge.reason", "is not one of")
reject_pair("loss: a delta is never negative", "loss-log", LLF, ll1, mod(ll1, bridge=dict(ll1["bridge"], gaps=-1)), "$.bridge.gaps", "less than the minimum of 0")
reject_pair("loss: age_s is never negative", "loss-log", LLF, ll1, mod(ll1, bridge=dict(ll1["bridge"], age_s=-1)), "$.bridge.age_s", "less than the minimum of 0")
reject_pair("loss: a delta is an integer", "loss-log", LLF, ll1, mod(ll1, sink=dict(ll1["sink"], rx_errors=1.5)), "$.sink.rx_errors", "is not of type 'integer'")
reject_pair("loss: a source is required", "loss-log", LLF, ll1, mod(ll1, sink=KeyError), "$", "'sink' is a required property")
reject_pair("loss: a source never carries a path", "loss-log", LLF, ll1, mod(ll1, bridge=dict(ll1["bridge"], path="x")), "$.bridge", "Additional properties are not allowed")
reject_pair("loss: a record never carries an address", "loss-log", LLF, ll1, mod(ll1, addr="x"), "$", "Additional properties are not allowed")
check("loss-log: the schema maps loss.jsonl and loss_log.jsonl to exactly the loss kind; a run_event in that file is drift (positive twin: loss passes)",
      SCH["loss-log"]["x-files"] == {"loss.jsonl": ["loss"], LLF: ["loss"]} and drifts("loss-log", ll1, LLF) == []
      and rejected(drifts("loss-log", mod(ll1, k="run_event"), LLF), "$.k", "unknown record kind 'run_event'"), "")
_ll_raw = open(os.path.join(FIX, LLF), "rb").read()
check("loss-log fixture: LF line ends, no BOM, no path, device or address text",
      b"\r" not in _ll_raw and not _ll_raw.startswith(b"\xef\xbb\xbf") and not any(s_ in _ll_raw for s_ in (b"lossfix", b"Temp", b"\\\\", b"/", b"CABLE", b"device", b"239.")), _ll_raw[:80])


def m_ll_closed(s): s["$defs"]["bridge"].pop("additionalProperties")


_llm = mutated("loss_log.schema.json", m_ll_closed)
_sch_ll = C.load_schemas(os.path.join(_llm, "schemas"))
check("mutation: the bridge def without additionalProperties false accepts a path key (proves the closed schema is what rejects it above)",
      C.check_record(LLF, "1", mod(ll1, bridge=dict(ll1["bridge"], path="x")), _sch_ll["loss-log"], ["loss"], {}) == [], "")


def m_ll_cond(s): s["$defs"]["sink"]["allOf"].pop(0)


_llc = mutated("loss_log.schema.json", m_ll_cond)
_sch_llc = C.load_schemas(os.path.join(_llc, "schemas"))
check("mutation: the sink def without the reason/delta rule accepts a stale source with a delta",
      C.check_record(LLF, "1", mod(lls[3], sink=dict(lls[3]["sink"], datagrams=5)), _sch_llc["loss-log"], ["loss"], {}) == [], "")
ll_dir = os.path.join(tmp, "llcl")
mkcontracts(ll_dir)
_p = os.path.join(ll_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("loss-log 1.0.0", "loss-log x"))
d_ = C.run(ll_dir)[1]
check("mutation: the CHANGELOG without `loss-log 1.0.0` -> flagged", any(d.kind == "changelog" and d.path == "loss-log" for d in d_), str([str(d) for d in d_][:2]))
root_ll = os.path.join(tmp, "live_ll")
mkroot(root_ll)
_n0, _d0, _notes0 = C.check_live(root_ll, SCH, 100)
os.makedirs(os.path.join(root_ll, "history"))
shutil.copy(os.path.join(FIX, LLF), os.path.join(root_ll, "history", "loss.jsonl"))
_n1, _d1, _ = C.check_live(root_ll, SCH, 100)
with open(os.path.join(root_ll, "history", "loss.jsonl"), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(ll1, bridge=dict(ll1["bridge"], gaps=-1)), separators=(",", ":")) + "\n")
_n2, _d2, _ = C.check_live(root_ll, SCH, 100)
check("live check: history/loss.jsonl is optional (absent: a note, 0 drift), the 9 fixture records add 9 and 0 drift, an appended record with gaps -1 is reported",
      _d0 == [] and any(n_.startswith("history/loss.jsonl") for n_ in _notes0) and _d1 == [] and _n1 == _n0 + 9
      and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("history/loss.jsonl", "$.bridge.gaps") and "less than the minimum of 0" in _d2[0].message, str((_n0, _n1, [str(d) for d in _d2][:2])))
import make_loss_fixtures as MLL  # noqa: E402
_ll_regen = open(MLL.build(os.path.join(tmp, "regen_ll")), "rb").read()
check("loss-log fixture equals the output of the real tool byte for byte", _ll_regen == _ll_raw, _ll_regen[:80])
# ============================================================================================ pad-lane 1.1.0 (analysis/pad_lane.py)
PTF, PCF = "tags_pad.jsonl", "clap_pad.jsonl"
pdt, pdc = jl(PTF), jl(PCF)
check("pad-lane fixtures: 3 tags_pad records (the speech window wrote none) and 2 clap_pad records, all valid, all variant pad10 / pad_s 10.0 / ms 42.0",
      len(pdt) == 3 and len(pdc) == 2 and all(not drifts("pad-lane", r, PTF) for r in pdt) and all(not drifts("pad-lane", r, PCF) for r in pdc)
      and all((r["variant"], r["pad_s"], r["ms"]) == ("pad10", 10.0, 42.0) for r in pdt + pdc) and all(r["speech_p"] < 0.3 for r in pdt), str(len(pdt)))
reject_pair("pad-lane tags speech_active true (a speech window writes no record)", "pad-lane", PTF, pdt[0], mod(pdt[0], speech_active=True), "$.speech_active", "False was expected")
reject_pair("pad-lane tags speech_p 0.3 (the guard threshold)", "pad-lane", PTF, mod(pdt[0], speech_p=0.299), mod(pdt[0], speech_p=0.3), "$.speech_p", "greater than or equal to the maximum of 0.3")
reject_pair("pad-lane tags emb_stored", "pad-lane", PTF, pdt[0], mod(pdt[0], emb_stored=True), "$.emb_stored", "False was expected")
reject_pair("pad-lane variant is pad10", "pad-lane", PTF, pdt[0], mod(pdt[0], variant="pad5"), "$.variant", "'pad10' was expected")
reject_pair("pad-lane pad_s at least 5", "pad-lane", PTF, mod(pdt[0], pad_s=5), mod(pdt[0], pad_s=4), "$.pad_s", "less than the minimum of 5")
reject_pair("pad-lane ms is required", "pad-lane", PTF, pdt[0], mod(pdt[0], ms=KeyError), "$", "'ms' is a required property")
reject_pair("pad-lane privacy: an embedding key is rejected", "pad-lane", PTF, pdt[0], mod(pdt[0], embedding=[0.1]), "$", "should not be valid under")
reject_pair("pad-lane clap carries exactly 3 prompts", "pad-lane", PCF, pdc[0], mod(pdc[0], top=pdc[0]["top"][:2]), "$.top", "too short")
check("pad-lane: a tags record in clap_pad.jsonl is not allowed, the same record in tags_pad.jsonl is fine",
      rejected(drifts("pad-lane", pdt[0], PCF), "$.k", "not allowed in clap_pad.jsonl") and drifts("pad-lane", pdt[0], PTF) == [] and SCH["pad-lane"]["x-files"] == {PTF: ["tags"], PCF: ["clap"]})
# 1.1.0 provenance: exact values of the fixtures (made by the real plugins from a table), each new field rejected when wrong with its positive twin
_mt, _mc = {"name": "PANNs CNN14", "version": "0.1.1", "variant": "pad10", "sha256": "ab" * 32}, {"name": "laion/clap-htsat-unfused", "version": "rev0", "variant": "pad10", "sha256": "cd" * 32}
check("pad-lane 1.1.0 fixtures: every record has model (the production plugin's block with variant pad10), claim, top_k of 5 and max_unstored_score; exact values of the first records",
      all(r["model"] == _mt for r in pdt) and all(r["model"] == _mc for r in pdc) and all(r["claim"] == {"human_verified": False, "source": "model_opinion"} for r in pdt + pdc)
      and all(len(r["top_k"]) == 5 for r in pdt + pdc) and [r["max_unstored_score"] for r in pdt] == [0.005, 0.004, 0.04] and [r["max_unstored_score"] for r in pdc] == [0.0, 0.0]
      and pdt[0]["top_k"] == [["Insect", 0.62], ["Dog", 0.2], ["Silence", 0.06], ["Bird", 0.03], ["Wind", 0.01]] and all(["Speech", 0.1] not in r["top_k"] for r in pdt), str(pdt[0]))
reject_pair("pad-lane 1.1.0 model.variant is pad10", "pad-lane", PTF, pdt[0], mod(pdt[0], model=dict(_mt, variant="best_beam_32k")), "$.model.variant", "'pad10' was expected")
reject_pair("pad-lane 1.1.0 model.sha256 is 64 hex or null", "pad-lane", PTF, mod(pdt[0], model=dict(_mt, sha256=None)), mod(pdt[0], model=dict(_mt, sha256="zz")), "$.model.sha256", "does not match")
reject_pair("pad-lane 1.1.0 model needs its four keys", "pad-lane", PCF, pdc[0], mod(pdc[0], model={"name": "x", "variant": "pad10", "sha256": None}), "$.model", "'version' is a required property")
reject_pair("pad-lane 1.1.0 claim.human_verified is false", "pad-lane", PCF, pdc[0], mod(pdc[0], claim={"human_verified": True, "source": "model_opinion"}), "$.claim.human_verified", "False was expected")
reject_pair("pad-lane 1.1.0 top_k has exactly 5 entries", "pad-lane", PTF, pdt[0], mod(pdt[0], top_k=pdt[0]["top_k"][:4]), "$.top_k", "too short")
reject_pair("pad-lane 1.1.0 max_unstored_score is within 0..1 or null", "pad-lane", PCF, mod(pdc[0], max_unstored_score=None), mod(pdc[0], max_unstored_score=1.5), "$.max_unstored_score", "greater than the maximum of 1")
check("pad-lane 1.1.0: the provenance fields are optional (a 1.0.0-shaped record without them still validates)",
      drifts("pad-lane", {k: v for k, v in pdt[0].items() if k not in ("model", "claim", "top_k", "max_unstored_score")}, PTF) == [])
def m_pad(s): s["$defs"]["variant"]["const"] = "pad5"
d_ = C.run(mutated("pad_lane.schema.json", m_pad))[1]
check("mutation: variant const pad5 -> every fixture record is flagged at $.variant", sum(1 for d in d_ if d.path == "$.variant" and "'pad5' was expected" in d.message) == 5, str([str(d) for d in d_][:2]))
pad_dir = os.path.join(tmp, "padcl")
mkcontracts(pad_dir)
_p = os.path.join(pad_dir, "CHANGELOG.md")
open(_p, "w", encoding="utf-8", newline="\n").write(open(_p, encoding="utf-8").read().replace("pad-lane 1.1.0", "pad-lane x"))
d_ = C.run(pad_dir)[1]
check("mutation: the CHANGELOG without `pad-lane 1.1.0` -> flagged", any(d.kind == "changelog" and d.path == "pad-lane" for d in d_), str([str(d) for d in d_][:2]))
root_pad = os.path.join(tmp, "live_pad")
mkroot(root_pad)
_n0, _d0, _notes0 = C.check_live(root_pad, SCH, 100)
shutil.copy(os.path.join(FIX, PTF), os.path.join(root_pad, "analysis", PTF))
_n1, _d1, _ = C.check_live(root_pad, SCH, 100)
with open(os.path.join(root_pad, "analysis", PTF), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mod(pdt[0], variant="pad5"), separators=(",", ":")) + "\n")
_n2, _d2, _ = C.check_live(root_pad, SCH, 100)
check("live check: analysis/tags_pad.jsonl is optional (absent: a note, 0 drift), the 3 fixture records add 3 and 0 drift, an appended variant pad5 is reported",
      _d0 == [] and any(n_.startswith("analysis/tags_pad.jsonl") for n_ in _notes0) and _d1 == [] and _n1 == _n0 + 3
      and len(_d2) == 1 and (_d2[0].file, _d2[0].path) == ("analysis/tags_pad.jsonl", "$.variant"), str((_n0, _n1, [str(d) for d in _d2])))
try:
    import torch  # noqa: F401, E402  (the real plugins import it)
except ImportError:
    skip("pad-lane fixtures equal the output of the real plugins byte for byte", "needs torch (the plugins import it); regenerate with contracts/tools/make_pad_lane_fixtures.py")
else:
    import make_pad_lane_fixtures as MPL  # noqa: E402
    _pl = MPL.build(os.path.join(tmp, "regen_pad"))
    check("pad-lane fixtures equal the output of the real plugins byte for byte",
          all(open(os.path.join(_pl, f), "rb").read() == open(os.path.join(FIX, f), "rb").read() for f in (PTF, PCF)))

shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{n_pass} passed, {n_xfail} expected failures (known defects), {n_skip} skipped (need the sink with BandMeter or torch), {len(fails)} failure(s)")
for f in fails:
    print("  FAILED:", f)
sys.exit(1 if fails else 0)
