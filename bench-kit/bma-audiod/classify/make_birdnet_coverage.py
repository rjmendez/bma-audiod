#!/usr/bin/env python3
"""Build birdnet_coverage.json: the labels BirdNET's location model cannot judge anywhere.

The location model (birdnetlib SpeciesList) only knows birds with a real range; every other class (insects, frogs,
mammals, rare tropical birds) scores ~0.001 at every place and week, so a location filter would drop them all.
bma_classify uses this list to keep and flag those labels instead of dropping them. A label is "uncovered" when its
highest score over a 10 degree x 4-week grid is below THRESH. The grid can miss a tiny-range bird: the error is
always in the keep-and-flag direction, never a drop.

    python make_birdnet_coverage.py [out.json]      (about 40 s)
"""

import contextlib, io, json, os, sys

THRESH = 0.01
WEEKS = (4, 16, 28, 40)

from birdnetlib.species import SpeciesList  # noqa: E402
import birdnetlib  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "birdnet_coverage.json")
with contextlib.redirect_stdout(io.StringIO()):  # birdnetlib prints progress to stdout
    sl = SpeciesList()
best = {}
for lat in range(-60, 81, 10):
    for lon in range(-180, 180, 10):
        for wk in WEEKS:
            with contextlib.redirect_stdout(io.StringIO()):
                items = sl.return_list(lon=lon, lat=lat, week_48=wk, threshold=0.0)
            for i in items:
                k = i["scientific_name"] + "_" + i["common_name"]
                if i["threshold"] > best.get(k, 0.0):
                    best[k] = i["threshold"]
uncovered = sorted(k for k in sl.labels if best.get(k, 0.0) < THRESH)
try:
    import importlib.metadata as md

    ver = md.version("birdnetlib")
except Exception:
    ver = getattr(birdnetlib, "__version__", "?")
with open(out, "w", encoding="utf-8") as f:
    json.dump(
        {
            "birdnetlib": ver,
            "grid": "lat -60..80 step 10, lon -180..170 step 10, weeks %s" % (WEEKS,),
            "threshold": THRESH,
            "labels": len(sl.labels),
            "uncovered": uncovered,
        },
        f,
        indent=0,
    )
print("wrote %s: %d of %d labels uncovered" % (out, len(uncovered), len(sl.labels)))
