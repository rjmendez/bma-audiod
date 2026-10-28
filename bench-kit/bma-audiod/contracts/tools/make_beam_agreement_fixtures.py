#!/usr/bin/env python3
"""Regenerate contracts/fixtures/beam_agreement_report.json by running the REAL analysis/beam_agreement.py (its CLI) on a
deterministic synthetic scenario (no real data, no randomness):

    python -I make_beam_agreement_fixtures.py [out_dir]      (default: ../fixtures)

`scenario()` is also what analysis/test_beam_agreement.py builds its decision tests from, so the fixture and the tests share one
definition. All numbers are synthetic: species, times, levels and verdicts are invented to populate every field of the report."""

import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "analysis")))
import beam_agreement as BA  # noqa: E402

BASE_T = 1792972800.0  # a UTC hour boundary (multiple of 3600)
BIRD = ("Melanerpes carolinus", "Red-bellied Woodpecker")
INSECT = ("Amblycorypha oblongifolia", "Angular-winged Katydid")
GENERATED_S = 1792972800.0
DUR_NW = {"1": 1, "2-3": 2, ">=4": 4}  # windows per event for each duration class
LEVELS = (-65.0, -60.0, -55.0, -50.0, -45.0)


def event_rows(species, t0, conf, n_windows, conc_windows, level, in_region, adjacent=True):
    """Rows of one event: window j is at t0 + 3 j (exactly representable) and holds beams {2, 3} (ring-adjacent) or {2, 5}
    when j < conc_windows, else beam 2 alone. Beam 3 / 5 is the second-strongest beam, as in the top-2 scoring."""
    rows = []
    for j in range(n_windows):
        beams = [2, 3 if adjacent else 5] if j < conc_windows else [2]
        for b in beams:
            r = {
                "k": "bird",
                "t": t0 + 3.0 * j,
                "beam": b,
                "level_dbfs": level,
                "common": species[1],
                "scientific": species[0],
                "confidence": conf,
                "provenance": "model_opinion",
            }
            if in_region is not None:
                r["in_region"] = in_region
            rows.append(r)
    return rows


def scenario(bird_conc=0.5, insect_conc=0.97, label_effect=True, per_cell=20):
    """-> (birds rows, ambient rows, label rows).
    Birds: in conf 0.6 and 0.75 and 0.9, per duration class `per_cell` events concurrent and `per_cell` not (so 6 strata of
    2 x per_cell labelled events in the 0.5-0.85 band); concurrent events are labelled correct 80% of the time when
    `label_effect` else 50%, single events 50%. Cold insects (an ambient file of 2 C hours): per conf and duration class
    `per_cell` events, a fraction `insect_conc` concurrent."""
    birds, labels, i = [], [], 0
    t = BASE_T + 1000.0
    for conf in (0.3, 0.6, 0.75, 0.9):
        for dc, nw in DUR_NW.items():
            for conc in (True, False):
                for k in range(per_cell):
                    cw = (1 if nw == 1 else 2) if conc else 0
                    birds += event_rows(BIRD, t, conf, nw, cw, LEVELS[(i + k) % 5], True)
                    if 0.5 <= conf < 0.85:
                        good = (k % 10) < (8 if (conc and label_effect) else 5)
                        labels.append({"k": "beam_label", "v": 1, "species_key": BIRD[0].lower(), "t_start": t, "verdict": "correct" if good else "wrong"})
                    t += 1000.0
                    i += 1
    for conf in (0.3, 0.6, 0.75, 0.9):
        for dc, nw in DUR_NW.items():
            n_conc = round(per_cell * insect_conc)
            for k in range(per_cell):
                cw = (1 if nw == 1 else 2) if k < n_conc else 0
                birds += event_rows(INSECT, t, conf, nw, cw, LEVELS[(i + k) % 5], None)
                t += 1000.0
                i += 1
    t_end = t
    hours = int((t_end - BASE_T) // 3600) + 2
    ambient = [
        {"k": "ambient_temp", "v": 1, "t_hour_utc": int(BASE_T) + 3600 * h, "temp_c": 2.0, "source": "open-meteo", "model_grid_km": None} for h in range(hours)
    ]
    birds.sort(key=lambda r: r["t"])
    return birds, ambient, labels


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def build(out_dir):
    work = tempfile.mkdtemp(prefix="bafix_")
    try:
        birds, ambient, labels = scenario()
        os.makedirs(os.path.join(work, "in"))
        for name, rows in (("birds.jsonl", birds), ("ambient_temp.jsonl", ambient), ("labels.jsonl", labels)):
            write_jsonl(os.path.join(work, "in", name), rows)
        os.makedirs(os.path.join(work, "out"))
        rc = BA.main(
            [
                "--birds",
                os.path.join(work, "in", "birds.jsonl"),
                "--ambient",
                os.path.join(work, "in", "ambient_temp.jsonl"),
                "--labels",
                os.path.join(work, "in", "labels.jsonl"),
                "--taxa",
                os.path.join(HERE, "..", "..", "ui", "taxa.json"),
                "--generated-s",
                repr(GENERATED_S),
                "--out",
                os.path.join(work, "out", "report.json"),
            ]
        )
        if rc != 0:
            raise SystemExit("beam_agreement.py failed")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, "beam_agreement_report.json")
        with open(os.path.join(work, "out", "report.json"), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
