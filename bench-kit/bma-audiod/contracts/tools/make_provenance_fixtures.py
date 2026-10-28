#!/usr/bin/env python3
"""Append the analysis-records 1.3.0 provenance records to contracts/fixtures/tags.jsonl, clap.jsonl, birds.jsonl by running the REAL
Tagger.process, ClapZeroShot.process and MultiBeamBirds.process of analysis/plugins.py. The model behind each plugin is a fixed stand-in
(a score vector, a text/audio embedding pair, a detection list); the stand-in weights are the bytes b"stub-<name>", so the digests are
fixed. Lines that carry `model` are replaced on every run; the other lines of each file are untouched.
    python -I make_provenance_fixtures.py [fixtures_dir]      (needs numpy, scipy, torch)
analysis/test_provenance.py regenerates the records in memory and requires them equal to the file."""

import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "analysis")))
import numpy as np  # noqa: E402
import plugins as P  # noqa: E402
import taxa as T  # noqa: E402

FS = 48000
T_TAGS, T_CLAP, T_BIRDS = 1792889700.0, 1792889705.0, 1792889710.0
TAG_LABELS = ["Speech", "Silence", "Animal", "Inside, small room", "White noise", "Mouse", "Bird", "Insect"]
TAG_SCORES = [0.197, 0.213, 0.081, 0.060, 0.042, 0.031, 0.020, 0.009]


def noise(seconds, seed=0):
    return (np.random.default_rng(seed).standard_normal((int(seconds * FS), 8)) * 1e-2).astype(np.float32)


def stub_sha(workdir, name):
    p = os.path.join(workdir, name)
    with open(p, "wb") as f:
        f.write(b"stub-" + name.encode())
    return P.weights_id(p)


class FakeAT:
    def __init__(self, scores):
        self.scores = np.array(scores, np.float32)

    def inference(self, a):
        return self.scores[None, :], np.zeros((1, 2048), np.float32)


def tags_records(work):
    t = object.__new__(P.Tagger)
    t.labels, t.speech_idx, t.at, t.device = TAG_LABELS, [0], FakeAT(TAG_SCORES), "cpu"
    t.model = P.model_block("PANNs CNN14", "0.1.1", "best_beam_32k", stub_sha(work, "tags.pth"))
    return t.process(noise(5), T_TAGS)[0]


class FakeProc:
    def __call__(self, **kw):
        import torch

        return {"input_features": torch.zeros(1, 1)}


class FakeClapModel:
    def __init__(self, vec):
        self.vec = vec

    def get_audio_features(self, **kw):
        return self.vec[None, :]


def clap_records(work):
    import torch

    c = object.__new__(P.ClapZeroShot)
    n = len(P.DEFAULT_PROMPTS)
    g = torch.Generator().manual_seed(7)
    text = torch.nn.functional.normalize(torch.randn(n, 16, generator=g), dim=-1)
    c.torch, c.device, c.proc, c.prompts, c.scale = torch, "cpu", FakeProc(), P.DEFAULT_PROMPTS, 5.0
    c.text = text
    c.model = FakeClapModel(0.9 * text[2] + 0.3 * text[8] + 0.2 * text[20])
    c.model_meta = P.model_block("laion/clap-htsat-unfused", "a" * 40, "best_beam_48k", stub_sha(work, "clap.safetensors"))
    return c.process(noise(5), T_CLAP)[0]


class FakeBN:
    def __init__(self, dets, filtered, sha):
        self.dets, self.filtered_list, self.sha = dets, filtered, sha
        self.filtered = []

    def model_info(self):
        return {"name": "BirdNET", "version": "0.8.1", "sha256": self.sha}

    def classify(self, audio, when=None):
        self.filtered = list(self.filtered_list)
        return list(self.dets)


def birds_records(work):
    dets = [
        {"common": "Carolina Chickadee", "scientific": "Poecile carolinensis", "confidence": 0.8123, "in_region": True, "non_species": False},
        {"common": "Tufted Titmouse", "scientific": "Baeolophus bicolor", "confidence": 0.4411, "in_region": True, "non_species": False},
    ]
    gone = [{"common": "Fall Field Cricket", "scientific": "Gryllus pennsylvanicus", "confidence": 0.3302, "in_region": False, "non_species": False}]
    p = P.MultiBeamBirds(os.path.join(HERE, "..", "..", "classify"), geo_config=None, taxa=T.Taxa(os.path.join(HERE, "..", "..", "ui", "taxa.json")))
    p.bn, p.geo_on = FakeBN(dets, gone, stub_sha(work, "birds.tflite")), True
    return p.process(noise(3), T_BIRDS)[0]


FILES = {"tags.jsonl": tags_records, "clap.jsonl": clap_records, "birds.jsonl": birds_records}


def build():
    with tempfile.TemporaryDirectory() as work:
        return {name: fn(work) for name, fn in FILES.items()}


def line(r):
    return json.dumps(r, separators=(",", ":"))


def main(out):
    for name, recs in build().items():
        path = os.path.join(out, name)
        with open(path, encoding="utf-8", newline="") as f:
            keep = [ln for ln in f.read().split("\n") if ln and '"model":' not in ln]
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(keep + [line(r) for r in recs]) + "\n")
        print("wrote", path, len(recs))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures"))
