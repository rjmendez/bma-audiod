#!/usr/bin/env python3
"""Synthetic pipeline snapshot for the artifact gate tests and for contracts/tools/make_artifact_gate_fixtures.py: writes
<dir>/analysis/{birds,dsp}.jsonl and <dir>/history/ambient_temp.jsonl with crafted, entirely invented data (a made-up day; no real
timestamp, temperature or label count). Every cell of the gate's confusion table gets a different count, so a swapped cell is
visible. See EXPECT for the numbers the replay must report. Standard library only."""

import json, os

H = 1_792_972_800
DAY0 = H - H % 86400  # midnight UTC of the made-up day
HOUR = {"cold": 2, "mild": 8, "warm": 14, "nogap": 20, "hidden": 22}
TEMPS = {"cold": 2.0, "mild": 10.0, "warm": 20.0}  # the "nogap" hour has no ambient record within 3 h
HID_T = DAY0 + HOUR["hidden"] * 3600 + 777.25  # timestamp of the hidden-label rows; must appear nowhere in any output
KAT = ("Common Virtuoso Katydid", "Neoconocephalus triops")
ORC = ("Slender Meadow Katydid", "Orchelimum agile")
WOOD = ("Red-bellied Woodpecker", "Melanerpes carolinus")
NUT = ("White-breasted Nuthatch", "Sitta carolinensis")
ENG = ("Engine", "Engine")
# (hour name, label tuple, with_burst, count) in file order; spacing is 150 s inside the hour
PLAN = [
    ("cold", KAT, True, 5),
    ("cold", KAT, False, 3),
    ("cold", ORC, False, 1),
    ("cold", WOOD, True, 1),
    ("cold", WOOD, False, 3),
    ("cold", NUT, False, 3),
    ("mild", KAT, True, 2),
    ("mild", KAT, False, 4),
    ("warm", KAT, True, 1),
    ("warm", KAT, False, 6),
    ("warm", ENG, False, 2),
    ("nogap", KAT, True, 2),
    ("nogap", KAT, False, 1),
]
EXPECT = {
    "n_rows": 34,
    "n_insect": 25,
    "hidden": 5,
    "confusion": {"cold_burst": 5, "cold_noburst": 4, "warm_burst": 3, "warm_noburst": 10, "unknown_burst": 2, "unknown_noburst": 1},
    "flags": {"burst_coincident": 10, "cold_insect": 9, "mechanical_label": 2, "temp_unknown": 3},
    "levels": {"likely": 9, "none": 13, "suspect": 5},
    "hour_flagged": {2: 9, 8: 2, 14: 1, 20: 2},
    "hour_unflagged": {8: 4, 14: 6, 20: 1},
    "oos": {"Red-bellied Woodpecker": 4, "White-breasted Nuthatch": 3},
}


def build(d, with_ambient=True, with_dsp=True, dsp_to_hour=None):
    os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
    os.makedirs(os.path.join(d, "history"), exist_ok=True)
    rows, trs, first, last = [], [], None, None
    slot = {}
    for hname, (common, sci), burst, n in PLAN:
        for _ in range(n):
            i = slot.get(hname, 0)
            slot[hname] = i + 1
            t = float(DAY0 + HOUR[hname] * 3600 + 120 + 150 * i)
            first = t if first is None else min(first, t)
            last = t if last is None else max(last, t)
            rows.append(
                {
                    "k": "bird",
                    "t": t,
                    "beam": 1 + i % 8,
                    "level_dbfs": -60.0,
                    "common": common,
                    "scientific": sci,
                    "confidence": 0.5,
                    "provenance": "model_opinion",
                }
            )
            if burst:
                trs += [
                    {"k": "transient", "t": t - 1.2 + 0.1 * j, "excess_db": 9.0, "level_db": -45.0, "loudest_beam": 1, "beam_spread_db": 1.0} for j in range(25)
                ]
    hid = [
        {"k": "bird", "t": HID_T + i, "beam": 1, "level_dbfs": -50.0, "common": c, "scientific": c, "confidence": 0.9, "provenance": "model_opinion"}
        for i, c in enumerate(("Human vocal", "Human vocal", "Human vocal", "Human whistle", "Speech"))
    ]
    rows[2:2] = hid[:2]
    rows += hid[2:]
    rows.append(
        {
            "k": "bird_filtered",
            "t": first + 5.0,
            "beam": 1,
            "level_dbfs": -60.0,
            "common": "Mexican Jay",
            "scientific": "Aphelocoma wollweberi",
            "confidence": 0.3,
            "in_region": False,
        }
    )
    lines = [json.dumps(r, separators=(",", ":")) for r in rows]
    lines[5:5] = [
        "this line is not json",
        "[1, 2, 3]",
        '{"k":"bird","t":NaN,"common":"Engine","scientific":"Engine"}',
        '{"k":"bird","x":"' + "a" * 1_100_000 + '"}',
    ]
    with open(os.path.join(d, "analysis", "birds.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    if with_dsp:
        dsp = [{"k": "dsp", "t": first - 10.0, "beams": 8}, {"k": "dsp", "t": last + 10.0, "beams": 8}]
        if dsp_to_hour is not None:
            dsp = [r for r in dsp if r["t"] < DAY0 + dsp_to_hour * 3600]
            trs = [r for r in trs if r["t"] < DAY0 + dsp_to_hour * 3600]
        allr = sorted(dsp + trs, key=lambda r: r["t"])
        half = len(allr) // 2  # a rotated generation (.1) and the current file, like the live pair
        for fn, part in (("dsp.jsonl.1", allr[:half]), ("dsp.jsonl", allr[half:])):
            with open(os.path.join(d, "analysis", fn), "w", encoding="utf-8", newline="\n") as f:
                f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in part))
    if with_ambient:
        with open(os.path.join(d, "history", "ambient_temp.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            for name, c in TEMPS.items():
                f.write(
                    json.dumps(
                        {"k": "ambient_temp", "v": 1, "t_hour_utc": DAY0 + HOUR[name] * 3600, "temp_c": c, "source": "open-meteo", "model_grid_km": None},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
    return int(first), int(last)
