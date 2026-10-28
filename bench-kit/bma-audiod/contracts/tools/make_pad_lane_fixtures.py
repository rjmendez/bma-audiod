#!/usr/bin/env python3
"""Regenerate contracts/fixtures/tags_pad.jsonl and clap_pad.jsonl by running the REAL analysis/pad_lane.py plugins (needs numpy, scipy, torch):
    python -I make_pad_lane_fixtures.py [out_dir]      (default: ../fixtures)
The model is the plugins' documented seam (a stub answering from a table); the clock is a fake that makes every call 42.0 ms.
tags_pad: 3 non-speech windows written, a 4th window (speech 0.45) writes nothing. clap_pad: 2 windows."""

import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(BMA, "analysis"))
import numpy as np  # noqa: E402
import torch  # noqa: E402
import pad_lane as PL  # noqa: E402
import plugins as P  # noqa: E402

B = 1800000000.0
LABELS = ["Speech", "Insect", "Dog", "Silence", "Bird", "Wind", "Rain"]
MODEL_TAGS = P.model_block("PANNs CNN14", "0.1.1", "best_beam_32k", "ab" * 32)  # the production plugin's block; the lane stamps variant pad10 on it
MODEL_CLAP = P.model_block("laion/clap-htsat-unfused", "rev0", "best_beam_48k", "cd" * 32)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.k = 0

    def __call__(self):
        self.k += 1
        return 0.0 if self.k % 2 else 0.042


def window(beam, seed):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal((5 * 48000, 8)) * 0.01).astype(np.float32)
    x[:, beam] *= 10.0
    return x


class At:
    def __init__(self, probs):
        self.probs = np.array(probs, dtype=np.float32)

    def inference(self, a):
        return self.probs[None, :], np.zeros((1, 4), dtype=np.float32)


class TagShare:
    def __init__(self, probs):
        self.labels, self.speech_idx, self.at, self.device, self.model = LABELS, [0], At(probs), "cpu", MODEL_TAGS


class Proc:
    def __call__(self, audio=None, sampling_rate=None, return_tensors=None):
        return {"x": torch.zeros(1, 1)}


class Model:
    def __init__(self, v):
        self.v = v

    def get_audio_features(self, **kw):
        return torch.tensor([self.v])


class ClapShare:
    def __init__(self, v):
        self.torch, self.device, self.proc, self.model = torch, "cpu", Proc(), Model(v)
        self.model_meta = MODEL_CLAP
        self.prompts = ["room tone, quiet background noise", "a dog barking", "insects buzzing", "rain falling", "wind blowing", "birds singing"]
        self.text = torch.nn.functional.normalize(torch.tensor([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8], [-1.0, 0.0], [0.0, -1.0], [-0.6, 0.8]]), dim=-1)
        self.scale = 5.0


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    PL.time.perf_counter = Clock()
    tags = []
    for i, (beam, probs) in enumerate(
        [
            (2, [0.10, 0.62, 0.20, 0.06, 0.03, 0.01, 0.005]),
            (4, [0.05, 0.08, 0.71, 0.12, 0.02, 0.01, 0.004]),
            (0, [0.45, 0.20, 0.10, 0.05, 0.02, 0.01, 0.003]),
            (6, [0.02, 0.15, 0.04, 0.55, 0.30, 0.20, 0.07]),
        ]
    ):
        recs, _ = PL.PadTagger(None, "cpu", 10, share=TagShare(probs)).process(window(beam, i), B + 2.0 * i)
        tags += recs
    clap = []
    for i, (beam, v) in enumerate([(1, [1.0, 0.1]), (5, [0.2, 1.0])]):
        recs, _ = PL.PadClap("cpu", 10, share=ClapShare(v)).process(window(beam, 10 + i), B + 5.0 * (i + 1))
        clap += recs
    for name, rows in (("tags_pad.jsonl", tags), ("clap_pad.jsonl", clap)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    return out_dir


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
