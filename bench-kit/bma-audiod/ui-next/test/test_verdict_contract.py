#!/usr/bin/env python3
"""The verdict record the prototype writes (test/fixtures/verdict_record_sample.json, produced by test_app.js) against the REAL writer-side validator of the
label store (analysis/labels.py validate_record, contract verdict-labels 1.1.0).  Also states why a 1.2.0 bump is proposed: the prototype's evidence block
(views used, listened, box) is rejected by 1.1.0, so it travels beside the record, never inside it.
    python test_verdict_contract.py        (PASS/FAIL lines, '<n> checks, <m> failure(s)', exit 1 on a failure)"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "analysis"))
import labels as L  # noqa: E402

n, fails = 0, []


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [%s]" % detail))
    if not ok:
        fails.append(name)


def rejects(rec, field):
    try:
        L.validate_record(rec)
    except L.LabelError as e:
        return str(e).startswith(field), str(e)
    return False, "accepted"


rec = json.load(open(os.path.join(HERE, "fixtures", "verdict_record_sample.json"), encoding="utf-8"))
try:
    L.validate_record(rec)
    ok, why = True, ""
except L.LabelError as e:
    ok, why = False, str(e)
check("the record written by the prototype validates against labels.validate_record (verdict-labels 1.1.0)", ok, why)
check("it is a synthetic event: epoch 1793000000000 plus the clip time, species key without the claim suffix, basis clip_visual, source ui, provenance owner_verdict",
      rec["t_start_ms"] >= 1793000000000 and rec["species_key"].startswith("synthetic") and rec["basis"] == "clip_visual" and rec["source"] == "ui" and rec["provenance"] == "owner_verdict", json.dumps(rec)[:200])
bad = copy.deepcopy(rec)
bad["evidence"] = {"views": ["stft"], "listened": True, "box": None}
ok, msg = rejects(bad, "evidence")
check("negative twin: the proposed `evidence` block inside the record is rejected by 1.1.0 (%s): hence the 1.2.0 proposal" % msg, ok, msg)
bad = copy.deepcopy(rec)
bad["audio_clip"] = "x"
ok, msg = rejects(bad, "audio_clip")
check("negative twin: a key naming audio is forbidden (policy: a verdict never points at audio)", ok, msg)
bad = copy.deepcopy(rec)
bad["verdict"] = "wrong"
ok, msg = rejects(bad, "wrong_reason")
check("negative twin: verdict wrong without a wrong_reason is rejected", ok, msg)
good = copy.deepcopy(rec)
good["verdict"], good["wrong_reason"] = "wrong", "mechanical"
try:
    L.validate_record(good)
    ok, why = True, ""
except L.LabelError as e:
    ok, why = False, str(e)
check("positive twin: verdict wrong with wrong_reason mechanical validates", ok, why)
print("\n%d checks, %d failure(s)" % (n, len(fails)))
sys.exit(1 if fails else 0)
