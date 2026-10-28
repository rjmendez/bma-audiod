#!/usr/bin/env python3
"""Regenerate contracts/fixtures/mixscore.jsonl by running the REAL analysis/mixscore.py MixScore plugin and the real host
rotating writer (host.Out -> hist.RotatingJsonl) on fixed synthetic windows (needs numpy and scipy):
    python -I make_mixscore_fixtures.py [out_dir]      (default: ../fixtures)
The scorer is the plugin's documented seam (a table standing in for BirdNET); beams are identical noise so the mix gain is 0.
Minute 0 (B): four paired windows (both / both + per-beam-only katydid / mix-only / per-beam-only), one speech window (dropped, the
owl is nowhere), one gated window, one window the birds plugin never reports on (unpaired), a human label on the mix side (dropped).
Minute 1 (B + 60): two windows, then the shutdown flush: a `partial` record."""

import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(BMA, "analysis"))
import numpy as np  # noqa: E402
import host as H  # noqa: E402
import mixscore as M  # noqa: E402
import taxa as T  # noqa: E402

B = 1800000000.0
N = 3 * 48000
CHICK, TIT, CARD, KATY, OWL = (
    ("Carolina Chickadee", "Poecile carolinensis"),
    ("Tufted Titmouse", "Baeolophus bicolor"),
    ("Northern Cardinal", "Cardinalis cardinalis"),
    ("Common Virtuoso Katydid", "Neoconocephalus triops"),
    ("Barn Owl", "Tyto alba"),
)


def build(out_dir):
    taxa = T.Taxa(os.path.join(BMA, "ui", "taxa.json"))
    rng = np.random.default_rng(2026)

    def det(sp, conf, **kw):
        return dict({"common": sp[0], "scientific": sp[1], "confidence": conf}, **kw)

    def brec(sp, conf, t, beam=1):
        return {"k": "bird", "t": t, "beam": beam, "common": sp[0], "scientific": sp[1], "confidence": conf, "group": taxa.lookup(sp[1], sp[0])[0]}

    def loud():
        return np.repeat((rng.standard_normal((N, 1)) * 10 ** (-40 / 20)).astype(np.float32), 8, axis=1)

    quiet = lambda: (rng.standard_normal((N, 8)) * 10 ** (-90 / 20)).astype(np.float32)
    wins = [
        (3, [det(CHICK, 0.70, in_region=True), det(("Human vocal", "Human vocal"), 0.9)], [brec(CHICK, 0.60, B + 3), brec(CHICK, 0.40, B + 3, 2)], True),
        (6, [det(TIT, 0.40, in_region=True)], [brec(TIT, 0.50, B + 6), brec(KATY, 0.90, B + 6, 2)], True),
        (9, [det(CARD, 0.30, in_region=True)], [], True),
        (12, [], [brec(CHICK, 0.30, B + 12)], True),
        (30, [det(OWL, 0.88)], [brec(OWL, 0.80, B + 30)], True),
        (33, None, [], False),
        (42, [det(CHICK, 0.55, in_region=True)], None, True),
        (63, [det(TIT, 0.62, in_region=True)], [brec(TIT, 0.66, B + 63)], True),
        (66, [det(CHICK, 0.35, in_region=True)], [brec(CHICK, 0.30, B + 66)], True),
    ]
    answers = [w[1] for w in wins if w[1] is not None]
    calls = []

    def scorer(audio, when):
        calls.append(1)
        return list(answers[len(calls) - 1]), []

    p = M.MixScore(taxa=taxa, scorer=scorer)
    out = H.Out(out_dir)
    emit = lambda recs: [out.write("mixscore", r) for r in recs]
    ev = [(B + o, 0, (b, l)) for o, _, b, l in wins]
    ev += [(B + o + 0.2, 1, b) for o, _, b, _l in wins if b is not None]
    ev += [(B + tt + 0.5, 2, 28 <= tt <= 34) for tt in range(2, 107, 2)]
    for t, kind, arg in sorted(ev, key=lambda e: (e[0], e[1])):
        if kind == 0:
            p.process(loud() if arg[1] else quiet(), round(t))
        elif kind == 1:
            emit(p.observe("birds", arg, t))
        else:
            emit(p.observe("tags", [{"k": "tags", "speech_active": arg}], round(t - 0.5)))
    emit(p.flush())
    return out_dir


if __name__ == "__main__":
    out = build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures"))
    print("wrote", os.path.join(out, "mixscore.jsonl"))
