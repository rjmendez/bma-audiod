#!/usr/bin/env python3
"""Tests for model provenance (analysis-records 1.3.0, verdict-labels 1.1.0): the real Tagger.process, ClapZeroShot.process,
MultiBeamBirds.process and BirdNet.model_info run with a stand-in model (a fixed score vector / embedding pair / detection list; the
third-party libraries are not installed here), and the real labels.py, review_queue.py and ui/review.py run on temp files.
Expected values are written by hand. Needs numpy, scipy, torch.   python -I test_provenance.py"""
import hashlib, json, os, sys, tempfile, time, types
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
for d_ in (HERE, os.path.join(BMA, "ui"), os.path.join(BMA, "classify"), os.path.join(BMA, "contracts", "tools")):
    sys.path.insert(0, d_)
import torch  # noqa: E402
import plugins as P  # noqa: E402
import labels as L  # noqa: E402
import review_queue as Q  # noqa: E402
import review as R  # noqa: E402
import bma_classify as BC  # noqa: E402
import taxa as T  # noqa: E402
import make_provenance_fixtures as MF  # noqa: E402

fails, n_checks = [], 0
FS = 48000
CLAIM = {"human_verified": False, "source": "model_opinion"}


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + str(detail)[:400] + "]"))
    if not ok:
        fails.append(name)


def raises(exc, match, fn):
    try:
        fn()
    except exc as e:
        return match in str(e) and type(e) is exc
    return False


def sha(b):
    return hashlib.sha256(b).hexdigest()


def noise(seconds=5, seed=0, amp=1e-2):
    return (np.random.default_rng(seed).standard_normal((int(seconds * FS), 8)) * amp).astype(np.float32)


# ------------------------------------------------------------------ helpers
print("-- score_tail / digests")
labels = ["a", "b", "c", "d", "e", "f", "g"]
scores = [0.10, 0.50, 0.30, 0.2004, 0.05, 0.4, 0.0005]
check("1 score_tail: 5 best by score rounded to 3 places, then the 6th best as max_unstored_score",
      P.score_tail(scores, labels, list(np.argsort(scores)[::-1][:6])) == ([["b", 0.5], ["f", 0.4], ["c", 0.3], ["d", 0.2], ["a", 0.1]], 0.05))
check("1b score_tail with exactly 5 labels: nothing outside top_k, max_unstored_score None",
      P.score_tail(scores[:5], labels[:5], list(np.argsort(scores[:5])[::-1][:6])) == ([["b", 0.5], ["c", 0.3], ["d", 0.2], ["a", 0.1], ["e", 0.05]], None))

d = tempfile.mkdtemp(prefix="prov_")
f1 = os.path.join(d, "w.bin")
with open(f1, "wb") as f:
    f.write(b"weights-1")
check("2 file_sha256 = sha256 of the bytes", P.file_sha256(f1) == sha(b"weights-1"))
check("2b weights_id of an ordinary file name hashes the content", P.weights_id(f1) == sha(b"weights-1"))
blob = os.path.join(d, "a" * 64)
with open(blob, "wb") as f:
    f.write(b"other bytes")
check("2c weights_id of a file already named by a sha256 (hub blob) returns that name without reading the content", P.weights_id(blob) == "a" * 64 != sha(b"other bytes"))
check("2e weights_id of an absent file is None, not an exception", P.weights_id(os.path.join(d, "absent.pth")) is None)
check("2d model_block keys and values exact", P.model_block("n", "v", "x", "s") == {"name": "n", "version": "v", "variant": "x", "sha256": "s"})

cache = os.path.join(d, "hub")                                   # hub cache layout: models--org--name/{refs/main, snapshots/<rev>/<file>}
snap = os.path.join(cache, "models--org--m", "snapshots", "rev123")
os.makedirs(snap)
os.makedirs(os.path.join(cache, "models--org--m", "refs"))
with open(os.path.join(cache, "models--org--m", "refs", "main"), "w") as f:
    f.write("rev123")
with open(os.path.join(snap, "model.safetensors"), "wb") as f:
    f.write(b"hub weights")
check("3 hub_weights: (snapshot revision, sha256 of the weights file) from the local cache", P.hub_weights("org/m", cache) == ("rev123", sha(b"hub weights")))
check("3b hub_weights: a model that is not cached gives (None, None)", P.hub_weights("org/absent", cache) == (None, None))

# ------------------------------------------------------------------ tags
print("-- tags")
tsha = sha(b"stub-tags.pth")
with tempfile.TemporaryDirectory() as work:
    trec = MF.tags_records(work)[0]
check("4 tags record: model, claim, top_k (5 non-speech classes, no floor) and max_unstored_score exact; `top` keeps its 0.05 floor and Speech",
      trec == {"k": "tags", "t": MF.T_TAGS, "beam": trec["beam"], "speech_p": 0.197, "speech_active": False,
               "top": [["Silence", 0.213], ["Speech", 0.197], ["Animal", 0.081], ["Inside, small room", 0.06]], "emb_stored": True,
               "model": {"name": "PANNs CNN14", "version": "0.1.1", "variant": "best_beam_32k", "sha256": tsha}, "claim": CLAIM,
               "top_k": [["Silence", 0.213], ["Animal", 0.081], ["Inside, small room", 0.06], ["White noise", 0.042], ["Mouse", 0.031]],
               "max_unstored_score": 0.02}, trec)


def tagger(sc):
    t = object.__new__(P.Tagger)
    t.labels, t.speech_idx, t.at, t.device = MF.TAG_LABELS, [0], MF.FakeAT(sc), "cpu"
    t.model = P.model_block("PANNs CNN14", "0.1.1", "best_beam_32k", tsha)
    return t


sp = list(MF.TAG_SCORES)
sp[0] = 0.5
recs, emb = tagger(sp).process(noise(5), 1.0)
r = recs[0]
check("4b a speech window keeps its provenance and stores no embedding; top keeps Speech, top_k lists no human-voice class",
      (r["speech_active"], r["emb_stored"], emb, r["top"][0], [x[0] for x in r["top_k"]], r["max_unstored_score"], r["model"]["sha256"], r["claim"])
      == (True, False, None, ["Speech", 0.5], ["Silence", "Animal", "Inside, small room", "White noise", "Mouse"], 0.02, tsha, CLAIM), r)
emb2 = tagger(MF.TAG_SCORES).process(noise(5), 1.0)[1]
check("4c positive twin: a non-speech window returns the 2048-d embedding", emb2 is not None and emb2.shape == (2048,))
tg = tagger(MF.TAG_SCORES)
r1 = tg.process(noise(5), 1.0)[0][0]
r1["model"]["name"] = "mutated"
r1["claim"]["human_verified"] = True
r2 = tg.process(noise(5), 2.0)[0][0]
check("4d a record does not share its model / claim dict with the plugin (mutating one row leaves the next intact)",
      r2["model"]["name"] == "PANNs CNN14" and r2["claim"] == CLAIM, r2)

# Tagger.__init__ with a stand-in AudioTagging whose checkpoint file does not exist (test_perch.py shape): the plugin loads, sha256 is null
ckpts = []


class StubAT:
    def __init__(self, checkpoint_path, device):
        ckpts.append(checkpoint_path)


_saved = {k: sys.modules.get(k) for k in ("panns_inference", "panns_inference.config")}
sys.modules["panns_inference"] = types.SimpleNamespace(AudioTagging=StubAT)
sys.modules["panns_inference.config"] = types.SimpleNamespace(labels=["Speech", "Silence"])
try:
    with tempfile.TemporaryDirectory() as dd:
        tg_absent = P.Tagger(dd, "cpu")
        with open(os.path.join(dd, "Cnn14_mAP=0.431.pth"), "wb") as fh:
            fh.write(b"ckpt-bytes")
        tg_present = P.Tagger(dd, "cpu")
        ck = os.path.join(dd, "Cnn14_mAP=0.431.pth")
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
check("4e Tagger with an absent checkpoint loads and records sha256 null", tg_absent.model["sha256"] is None and tg_absent.model["name"] == "PANNs CNN14"
      and tg_absent.speech_idx == [0], tg_absent.model)
check("4f positive twin: with the checkpoint present sha256 = the file's sha256, and AudioTagging got that path",
      tg_present.model["sha256"] == sha(b"ckpt-bytes") and ckpts[0] == ckpts[1] == ck, (tg_present.model, ckpts))

# ------------------------------------------------------------------ clap
print("-- clap")
n = len(P.DEFAULT_PROMPTS)
w = torch.zeros(n)
for i, v in ((3, 5.0), (10, 4.0), (0, 3.0), (7, 2.0), (12, 1.0), (20, 0.5)):
    w[i] = v
c = object.__new__(P.ClapZeroShot)
c.torch, c.device, c.proc, c.prompts, c.scale = torch, "cpu", MF.FakeProc(), P.DEFAULT_PROMPTS, 2.0
c.text = torch.eye(n)
c.model = MF.FakeClapModel(w)
c.model_meta = P.model_block("laion/clap-htsat-unfused", "rev", "best_beam_48k", "b" * 64)
crec = c.process(noise(5), 2.0)[0][0]
a = (w / w.norm()).numpy().astype(np.float64)
pe = np.exp(2.0 * a) / np.exp(2.0 * a).sum()
o = np.argsort(pe)[::-1]
want_k = [[P.DEFAULT_PROMPTS[i], round(float(pe[i]), 3)] for i in o[:5]]
check("5 clap record: top = 3 prompts as before, top_k = 5 prompts, max_unstored_score = the 6th softmax value, model and claim exact",
      crec["top"] == want_k[:3] and crec["top_k"] == want_k and crec["max_unstored_score"] == round(float(pe[o[5]]), 3)
      and crec["model"] == {"name": "laion/clap-htsat-unfused", "version": "rev", "variant": "best_beam_48k", "sha256": "b" * 64} and crec["claim"] == CLAIM, crec)
check("5b the expected order is the one planted (prompts 3, 10, 0, 7, 12, then 20 sixth)", [int(i) for i in o[:6]] == [3, 10, 0, 7, 12, 20])

# ------------------------------------------------------------------ birds
print("-- birds")
TAXA = T.Taxa(os.path.join(BMA, "ui", "taxa.json"))


class NoInfoBN:
    """A BirdNET boundary without model_info (what the stand-ins of older tests look like)."""
    def __init__(self, dets):
        self.dets, self.filtered = dets, []

    def classify(self, audio, when=None):
        return list(self.dets)


def bird_plugin(dets, filtered, geo=True, min_conf=0.25, bn=None):
    p = P.MultiBeamBirds(os.path.join(BMA, "classify"), min_conf=min_conf, taxa=TAXA)
    p.bn = bn or MF.FakeBN(dets, filtered, "c" * 64)
    p.geo_on = geo
    return p


def det(sci, conf):
    return {"common": sci.upper(), "scientific": sci, "confidence": conf, "in_region": True, "non_species": False}


dets = [det("S1", 0.9), det("S2", 0.8), det("S3", 0.7), det("S4", 0.6), det("S5", 0.5), det("S6", 0.4)]
p = bird_plugin(dets, [det("F1", 0.95)])
recs, _ = p.process(noise(3, amp=0.1), 10.0)
b = [r for r in recs if r["k"] == "bird"]
f = [r for r in recs if r["k"] == "bird_filtered"]
want_top = [["F1", 0.95], ["S1", 0.9], ["S2", 0.8], ["S3", 0.7], ["S4", 0.6]]
check("6 bird / bird_filtered rows: 2 beams x (6 kept + 1 filtered), each with top_k = the 5 best of the beam's window INCLUDING the filtered one, sorted",
      len(b) == 12 and len(f) == 2 and all(r["top_k"] == want_top for r in b + f), [r["top_k"] for r in b[:1]])
check("6b model {BirdNET, version, geo_filter, sha256 from model_info}, claim, max_unstored_score None, score_floor 0.25 on every row",
      all(r["model"] == {"name": "BirdNET", "version": "0.8.1", "variant": "geo_filter", "sha256": "c" * 64} and r["claim"] == CLAIM and r["max_unstored_score"] is None
          and r["score_floor"] == 0.25 and r["provenance"] == "model_opinion" for r in b + f))
r2 = bird_plugin(dets[:2], [], geo=False, min_conf=0.3).process(noise(3, amp=0.1), 10.0)[0]
check("6c geo filter off: variant no_filter, score_floor follows --min-conf (0.3), top_k has the 2 detections",
      len(r2) == 4 and all(r["model"]["variant"] == "no_filter" and r["score_floor"] == 0.3 and r["top_k"] == [["S1", 0.9], ["S2", 0.8]] for r in r2), r2[:1])
r3 = bird_plugin(None, None, bn=NoInfoBN(dets[:1])).process(noise(3, amp=0.1), 10.0)[0]
check("6d a BirdNET boundary without model_info: model {BirdNET, version None, geo_filter, sha256 None}, the row is still written",
      len(r3) == 2 and all(r["model"] == {"name": "BirdNET", "version": None, "variant": "geo_filter", "sha256": None} for r in r3), r3[:1])
check("6e a window below the gate writes nothing", p.process(noise(3, amp=1e-8), 11.0)[0] == [])

mf = os.path.join(d, "model.tflite")
with open(mf, "wb") as fh:
    fh.write(b"tflite-bytes")
bn = BC.BirdNet(0.25)
bn._an, bn.version = types.SimpleNamespace(classifier_model_path=mf), "9.9"
check("7 BirdNet.model_info: name BirdNET, birdnetlib version, sha256 of the analyzer's model file", bn.model_info() == {"name": "BirdNET", "version": "9.9", "sha256": sha(b"tflite-bytes")})
bn2 = BC.BirdNet(0.25)
bn2._an, bn2.version = types.SimpleNamespace(classifier_model_path=os.path.join(d, "absent.tflite")), "9.9"
check("7b positive twin: a model file that does not exist gives sha256 None, not an error", bn2.model_info() == {"name": "BirdNET", "version": "9.9", "sha256": None})
bn3 = BC.BirdNet(0.25)
bn3._an, bn3.version = types.SimpleNamespace(model_path=mf), "9.9"
check("7c the second known attribute (model_path) is read when the first is absent", bn3.model_info()["sha256"] == sha(b"tflite-bytes"))

# ------------------------------------------------------------------ fixtures
print("-- fixtures")
fx = MF.build()
for name in ("tags.jsonl", "clap.jsonl", "birds.jsonl"):
    with open(os.path.join(BMA, "contracts", "fixtures", name), encoding="utf-8") as fh:
        have = [json.loads(x) for x in fh if '"model":' in x]
    want = json.loads(json.dumps(fx[name]))
    same = len(have) == len(want) and all(h.keys() == w_.keys() and all(abs(h[k] - w_[k]) < 0.01 if isinstance(h[k], float) else h[k] == w_[k] for k in h)
                                          for h, w_ in zip(have, want))
    check("8 %s: the committed provenance records equal what the real plugin writes now (%d records)" % (name, len(want)), same and len(want) > 0)

# ------------------------------------------------------------------ labels
print("-- labels")
S_A, S_B = "a" * 64, "b" * 64
T0 = 1792940400.0
RBW = ("Red-bellied Woodpecker", "Melanerpes carolinus")


def row(t, sha_=None, beam=1):
    r = {"k": "bird", "t": t, "beam": beam, "level_dbfs": -60.0, "common": RBW[0], "scientific": RBW[1], "confidence": 0.7, "provenance": "model_opinion",
         "in_region": True}
    if sha_ is not None:
        r["model"] = {"name": "BirdNET", "version": "1", "variant": "geo_filter", "sha256": sha_}
    return r


e_a = L.iter_events([row(T0, S_A)])[0]
e_b = L.iter_events([row(T0, S_B)])[0]
e_none = L.iter_events([row(T0)])[0]
e_mixed = L.iter_events([row(T0, S_A), row(T0 + 1, S_B, beam=2)])[0]
e_bad = L.iter_events([row(T0, "A" * 64), row(T0 + 1, "xyz", beam=2)])[0]
check("9 Event.model_sha256: the digest of its reports; None with none, with two different ones, or with malformed ones",
      (e_a.model_sha256, e_none.model_sha256, e_mixed.model_sha256, e_bad.model_sha256) == (S_A, None, None, None))
check("9a the same event id with and without digest (the join key is the id)", e_a.event_id == e_b.event_id == e_none.event_id)
rec_a = L.make_record(e_a, "correct", "field", t_label=T0 + 100, label_id="0" * 16)
rec_n = L.make_record(e_none, "correct", "field", t_label=T0 + 100, label_id="0" * 16)
check("9b make_record writes model_sha256 when the event carries one", rec_a["model_sha256"] == S_A)
check("9c positive twin: no digest on the event, no model_sha256 key, and the rest of the record is identical",
      "model_sha256" not in rec_n and {k: v for k, v in rec_a.items() if k != "model_sha256"} == rec_n)
check("9d a mixed-digest event writes no model_sha256", "model_sha256" not in L.make_record(e_mixed, "correct", "field", t_label=T0 + 100))
check("9e validate_record rejects a malformed model_sha256 (upper case, short)",
      raises(L.LabelError, "model_sha256: must be 64 lowercase hex chars", lambda: L.validate_record(dict(rec_a, model_sha256="A" * 64)))
      and raises(L.LabelError, "model_sha256: must be 64 lowercase hex chars", lambda: L.validate_record(dict(rec_a, model_sha256="abc"))))
check("9f validate_record accepts the 64-hex digest and the record without one", L.validate_record(dict(rec_a)) == rec_a and L.validate_record(dict(rec_n)) == rec_n)
store = L.LabelStore(os.path.join(d, "labels"))
store.append(rec_a)
check("9g the stored line carries model_sha256 and reads back equal", store.read()[0] == [rec_a])

fold_a, fold_n = L.fold([rec_a]), L.fold([rec_n])
check("10 label_for: the same digest joins", L.label_for(fold_a, e_a) == rec_a)
check("10b label_for: another digest does not (the verdict was given under another model)", L.label_for(fold_a, e_b) is None)
check("10c label_for: a verdict without digest joins an event with one (id only)", L.label_for(fold_n, e_a) == rec_n)
check("10d label_for: an event without digest joins a verdict with one (id only)", L.label_for(fold_a, e_none) == rec_a)
fold_ab = L.fold([rec_a, dict(rec_a, model_sha256=S_B, label_id="1" * 16, t_label=T0 + 200, verdict="wrong")])
check("10f label_for: two digests for one event, each event joins its own digest's verdict (A then B)",
      (L.label_for(fold_ab, e_a)["verdict"], L.label_for(fold_ab, e_b)["verdict"]) == ("correct", "wrong"))
check("10g label_for: the older digest's verdict is not replaced by the newer one's (reverting to A restores it); the event id alone gives the latest",
      L.label_for(fold_ab, e_a) == rec_a and fold_ab[e_a.event_id]["model_sha256"] == S_B)
check("10e label_for: another event id -> None", L.label_for({}, e_a) is None)

tags = L.Tags([{"k": "tags", "t": T0 + 0.5, "beam": 1, "speech_p": 0.02, "speech_active": False, "top": [], "emb_stored": True}])


def queue_for(events, folded):
    q = Q.build_queue(events, folded, tags, {"days": 2.0}, T0 + 3600)
    return q["excluded"]["already_labelled"], [i["event_id"] for i in q["items"]]


check("11 queue: an event labelled under the same digest is excluded as already_labelled", queue_for([e_a], fold_a) == (1, []))
check("11b the same event under another model digest is queued again", queue_for([e_b], fold_a) == (0, [e_b.event_id]))
check("11c a verdict without digest keeps the event excluded (1.0.0 labels still join)", queue_for([e_a], fold_n) == (1, []))

# the dashboard Review service end to end
root = tempfile.mkdtemp(prefix="prov_rv_")
an = os.path.join(root, "analysis")
os.makedirs(an)
t_ev = time.time() - 600


def write_rows(sha_):
    with open(os.path.join(an, "birds.jsonl"), "wb") as fh:
        fh.write((json.dumps(row(t_ev, sha_)) + "\n").encode())


write_rows(S_A)
with open(os.path.join(an, "tags.jsonl"), "wb") as fh:
    fh.write((json.dumps({"k": "tags", "t": t_ev + 0.5, "beam": 1, "speech_p": 0.02, "speech_active": False, "top": [], "emb_stored": True}) + "\n").encode())
TX = os.path.join(BMA, "ui", "taxa.json")
rv = R.Review(root, taxa_path=TX, allow_no_clip=True)
nx = rv.next()
eid = nx["item"]["event_id"] if nx["item"] else None
check("12 Review: the event is offered once (depth 1)", nx["queue_depth"] == 1 and eid is not None, nx)
rv.verdict(eid, "correct")
lab = L.LabelStore(os.path.join(root, "labels")).read()[0]
check("12b Review.verdict writes model_sha256 from the event's rows", len(lab) == 1 and lab[0]["model_sha256"] == S_A, lab)
rv._drop()
check("12c same model: depth 0 after the verdict", rv.next() == {"item": None, "queue_depth": 0})
write_rows(S_B)
rv2 = R.Review(root, taxa_path=TX, allow_no_clip=True)
nx2 = rv2.next()
check("12d rows under another model digest: the verdict does not apply, the event is offered again (depth 1, same id)",
      nx2["queue_depth"] == 1 and nx2["item"]["event_id"] == eid, nx2)
check("12e stats: the queue depth counts it", rv2.stats()["queue_depth"] == 1)
check("12e2 stats: the verdict given under the other digest is not counted (labelled 0, no verdicts, no species)",
      (lambda s_: (s_["labelled"], s_["verdicts"], s_["species"], s_["species_at_target"]))(rv2.stats())
      == (0, {"correct": 0, "wrong": 0, "unsure": 0, "artifact": 0}, [], 0), rv2.stats())
res = rv2.verdict(eid, "correct")
labs = L.LabelStore(os.path.join(root, "labels")).read()[0]
check("12f the same verdict again within the dedup window, under the new digest, is a new line (A then B), not a duplicate of the old one",
      [x.get("model_sha256") for x in labs] == [S_A, S_B] and res.get("recorded") is True, (labs, res))
rv2._drop()
check("12g and the event is closed again", rv2.next()["queue_depth"] == 0)
st_b = rv2.stats()
check("12g2 stats under the new digest: labelled 1, one correct verdict, depth 0", (st_b["labelled"], st_b["verdicts"]["correct"], st_b["queue_depth"]) == (1, 1, 0), st_b)
write_rows(S_A)
rv3 = R.Review(root, taxa_path=TX, allow_no_clip=True)
st_a = rv3.stats()
check("12h B then A: reverting the rows to model A restores A's verdict (depth 0, labelled 1) although B's line is later",
      rv3.next() == {"item": None, "queue_depth": 0} and (st_a["labelled"], st_a["queue_depth"]) == (1, 0), st_a)

print()
print("%d checks, %d failed" % (n_checks, len(fails)))
for x in fails:
    print("FAILED: " + x)
sys.exit(1 if fails else 0)
