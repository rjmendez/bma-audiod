#!/usr/bin/env python3
"""Regenerate contracts/fixtures/beam_response_map.json by running the REAL analysis/beam_response_map.py (analyze + to_json, stdlib only)
on fixed, seeded synthetic input:   python -I make_beam_response_map_fixtures.py [out_dir]      (default: ../fixtures)

Input (2 h of 1 Hz spatial rows, 1 Hz beam_bands rows, birds records): 40 Barn Owl events with a planted 4 dB first-harmonic pattern at a
fixed phase and +6 dB on beam 3 in the 4-8 kHz band (verdict go there), 32 Hairy Woodpecker events with no effect (no_go), 40 Engine events
(skipped: non-biological), 20 Human vocal events (never a source; their records mark speech windows). The clock is fixed, so the output is byte-for-byte reproducible.
analysis/test_beam_response_map.py and contracts/test_contracts.py regenerate it in a temp dir and require the same bytes."""

import json, math, os, random, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYSIS = os.path.abspath(os.path.join(HERE, "..", "..", "analysis"))
sys.path.insert(0, ANALYSIS)
import beam_response_map as B  # noqa: E402

T0 = 86400 * 19675
B0 = [0.0, -0.5, -1.0, -1.5, -2.0, -2.5, -3.0, -3.5]
NOW = 1793000000.0
N = 4800


def wlines(path, recs):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in recs:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def cosv(amp, phi):
    return [amp * math.cos(2 * math.pi * (k - phi) / 8) for k in range(8)]


def build_inputs(d):
    owl = [T0 + 300 + 100 * j for j in range(40)]
    hairy = [T0 + 350 + 120 * j for j in range(32)]
    engine = [T0 + 320 + 100 * j for j in range(40)]
    human = [T0 + 340 + 200 * j for j in range(20)]
    plants = {}
    for t in owl:
        for w in range(3):
            plants[int(t - T0) - w] = cosv(4.0, 1)
    rng = random.Random(5)
    sp = []
    for i in range(N):
        e = [b + rng.gauss(0, 0.05) for b in B0]
        if i in plants:
            e = [x + p for x, p in zip(e, plants[i])]
        m = max(e)
        sp.append({"k": "spatial", "t": T0 + i, "e_rel_db": [round(v - m, 3) for v in e], "spread_db": 1.0})
    wlines(os.path.join(d, "spatial.jsonl"), sp)

    def birds(ts, common, sci):
        return [
            {"k": "bird", "t": t, "beam": 3, "level_dbfs": -50.0, "common": common, "scientific": sci, "confidence": 0.95, "provenance": "model_opinion"}
            for t in ts
        ]

    wlines(
        os.path.join(d, "birds.jsonl"),
        birds(owl, "Barn Owl", "Tyto alba")
        + birds(hairy, "Hairy Woodpecker", "Dryobates villosus")
        + birds(engine, "Engine", "Engine")
        + birds(human, "Human vocal", "Human vocal"),
    )
    rng = random.Random(3)
    bands = []
    for i in range(N):
        db = [[round(-70.0 - 2.0 * b + 0.3 * k + rng.gauss(0, 0.05), 3) for b in range(8)] for k in range(8)]
        if i in plants:
            db[2][6] += 6.0
        bands.append({"k": "beam_bands", "v": 1, "idx": i * 48000, "t_us": int((T0 + i) * 1e6), "n": 48000, "zero_frac": 0.0, "db": db})
    wlines(os.path.join(d, "beam_bands.jsonl"), bands)


def build(out_dir, taxa=None):
    work = tempfile.mkdtemp(prefix="brmfix_")
    build_inputs(work)
    P = B.default_params()
    P.update(controls=200, boot=200, perms=200)
    tx = B.TX.Taxa(taxa or os.path.join(ANALYSIS, "..", "ui", "taxa.json"))
    rep, _ = B.analyze(
        P, [os.path.join(work, "spatial.jsonl")], [os.path.join(work, "birds.jsonl")], os.path.join(work, "beam_bands.jsonl"), tx, None, (), now=NOW
    )
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "beam_response_map.json")
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(B.to_json(rep))
    return out


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
