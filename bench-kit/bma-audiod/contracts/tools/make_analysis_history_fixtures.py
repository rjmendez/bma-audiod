#!/usr/bin/env python3
"""Regenerate contracts/fixtures/{config_history.jsonl, hourly_rollup.jsonl, daily_summary.json} by running the REAL
producers (analysis/runmeta.py, rollup.py, daily.py) on fixed inputs in a temp directory, so the fixtures are what the code writes.
    python -I make_analysis_history_fixtures.py [out_dir]      (default: ../fixtures)
analysis/test_meta_history.py regenerates them in a temp dir and requires byte equality with the committed files."""

import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AN = os.path.abspath(os.path.join(HERE, "..", "..", "analysis"))
sys.path.insert(0, AN)
import runmeta, rollup, daily  # noqa: E402

H = 1761436800 + 7200  # 2025-10-26 02:00:00 UTC
DAY = 1761436800.0


def build(out_dir):
    work = tempfile.mkdtemp(prefix="afix_")
    hist = os.path.join(work, "h")
    mask = os.path.join(work, "mask.json")
    cal = os.path.join(work, "calibration.json")
    with open(mask, "w") as f:
        json.dump({"version": 1, "lines_hz": [11510.7, 12000.0, 15334.0], "tolerance_hz": 4.0}, f)
    with open(cal, "w") as f:
        json.dump(
            {
                "profile": {
                    "beam_azimuth_deg": [12.5, 57.5, 102.5, 147.5, 192.5, 237.5, 282.5, 327.5],
                    "north_reference": "compass",
                    "measured_at": "2026-10-20T15:04:05Z",
                }
            },
            f,
        )

    def mk():
        body, fp = runmeta.build_config(work, mask, None, cal, None, None, work, "cuda:1", 12, "km12-sk1.7.2")
        body["app_version"] = "2026.10.26-1"  # a stable stand-in for the installed VERSION line
        body["models"] = {
            "birdnetlib": "0.18.0",
            "panns": {"ckpt": "Cnn14_mAP=0.431.pth", "bytes": 327428481, "mtime": 1760000000},
            "panns_inference": "0.1.1",
            "clap": "laion/clap-htsat-unfused",
            "transformers": "4.57.0",
            "torch": "2.9.0",
            "sklearn": "1.7.2",
            "novelty": {"k": 12, "mv": "km12-sk1.7.2"},
        }
        return body, fp

    w = runmeta.ConfigWatcher(hist, mk, 1761437000, clock=lambda: 1761437000.5)
    w.poll()
    r = rollup.HourlyRollup(hist)
    rms = [-61.2, -60.8, -60.1, -59.7, -60.4, -61.0, -61.5, -61.9]
    for i in range(3):
        r.feed({"k": "dsp", "t": H + 10 + i, "rms_dbfs": rms, "band_pct": [12.0, 30.5, 28.0, 20.5, 9.0], "centroid_hz": 1500.0 + 100 * i})
    r.feed({"k": "transient", "t": H + 20, "excess_db": 12.0, "loudest_beam": 3})
    r.feed({"k": "tonal_appeared", "t": H + 30, "hz": 978.1, "excess_db": 20.0, "beam": 4, "beam_margin_db": 9.0})
    r.feed({"k": "tonal_gone", "t": H + 90, "hz": 978.1, "lasted_s": 52.0, "beam": 4})
    for t, nf in ((H + 40, -70.0), (H + 50, -66.0)):
        r.feed(
            {
                "k": "tonal_state",
                "t": t,
                "lines": [
                    {"hz": 978.1, "excess_db": 20.0, "age_s": 10.0, "beam": 4},
                    {"hz": 12000.0, "excess_db": 15.0, "age_s": 99.0, "electronic": True, "beam": None},
                ],
                "noise_floor_db": nf,
                "noise_floor_drift_db": 0.1,
            }
        )
    for dom, dr, dec in ((2, True, 0.5), (2, False, 0.7), (4, False, 0.6)):
        r.feed(
            {"k": "spatial", "t": H + 15, "dominant_beam": dom, "directional": dr, "decorr": dec, "e_rel_db": [0.0 if i == dom - 1 else -3.0 for i in range(8)]}
        )
    r.tick(H + 3600 + 120)
    s = daily.DailySummary(os.path.join(hist, "daily_summary.json"), clock=lambda: DAY + 10)
    for i in range(3):
        s.on_window("dsp", [{"k": "dsp"}, {"k": "transient"}], DAY + 100 + i)
    s.on_window("spatial", [{"k": "spatial", "directional": True}, {"k": "source"}], DAY + 100)
    s.on_window("tags", [{"k": "tags", "speech_active": True}], DAY + 100)
    s.on_window("birds", [{"k": "bird"}], DAY + 103, {"beams_classified": 2})
    s.on_window("birds", [], DAY + 106, {"beams_classified": 0})
    s.on_error("clap", DAY + 50)
    s.on_dropped("tags", 4, DAY + 60)
    s.on_status(DAY + 200, {"datagrams": 1000, "bad": 1, "gaps": 8}, 0.0, 5.0)
    s.flush()
    os.makedirs(out_dir, exist_ok=True)
    for src, dst in (
        ("config_history.jsonl", "config_history.jsonl"),
        ("hourly_rollup.jsonl", "hourly_rollup.jsonl"),
        ("daily_summary.json", "daily_summary.json"),
    ):
        with open(os.path.join(hist, src), "rb") as f:
            data = f.read()
        if src.endswith(".json"):  # the live file is compact; the fixture is pretty for review
            data = (json.dumps(json.loads(data), indent=1) + "\n").encode()
        with open(os.path.join(out_dir, dst), "wb") as f:
            f.write(data)
    return out_dir


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
