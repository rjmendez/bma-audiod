#!/usr/bin/env python3
"""Regenerates fixtures/ui_*.json and fixtures/ui_rollup.jsonl: the real bma_ui server answering over synthetic pipeline
files (the tree built by ui/test_bma_ui.py, a fixed clock, a recording fake dspd). Deterministic apart from nothing:
run it, review the diff.   python tools/make_ui_fixtures.py"""

import json, os, shutil, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.normpath(os.path.join(HERE, "..", "..", "ui"))
FIX = os.path.normpath(os.path.join(HERE, "..", "fixtures"))
sys.path.insert(0, UI)
import test_bma_ui as T  # noqa: E402
import bma_ui as U  # noqa: E402


HC_ = T.HC


def main():
    root = tempfile.mkdtemp(prefix="bma_ui_fx_")
    board = T.FakeBoard()
    board.fail_network = False
    try:
        pr, rp = T.make_tree(root)
        T0 = HC_ + 2000  # a katydid chorus, a toad, a deer, the domestic Dog, an old unflagged Engine record and a label in no table
        extra = [
            {
                "k": "bird",
                "t": T0 + 7 * i,
                "beam": 1 + i % 8,
                "level_dbfs": -60.0,
                "common": "Common Virtuoso Katydid",
                "scientific": "Amblycorypha longinicta",
                "confidence": 0.7,
                "provenance": "model_opinion",
            }
            for i in range(6)
        ]
        extra += [
            T.bird(T0 + 100, 5, 0.6, "Eastern Narrow-mouthed Toad", "Gastrophryne carolinensis"),
            T.bird(T0 + 110, 6, 0.6, "White-tailed Deer", "Odocoileus virginianus"),
            T.bird(T0 + 120, 7, 0.6, "Dog", "Dog", non=True),
            T.bird(T0 + 130, 1, 0.6, "Zzyzx Mystery", "Zzyzx fictus"),
            {
                "k": "bird",
                "t": T0 + 140,
                "beam": 2,
                "level_dbfs": -60.0,
                "common": "Engine",
                "scientific": "Engine",
                "confidence": 0.8,
                "provenance": "model_opinion",
            },
        ]
        T.jl(os.path.join(pr, "analysis", "birds.jsonl"), extra)
        os.makedirs(
            os.path.join(pr, "history"), exist_ok=True
        )  # a cold hour (the katydids are below the 5 C floor) and 25 diffuse dsp transients around the first katydid
        T.jl(
            os.path.join(pr, "history", "ambient_temp.jsonl"),
            [{"k": "ambient_temp", "v": 1, "t_hour_utc": HC_, "temp_c": 2.3, "source": "open-meteo", "model_grid_km": None}],
        )
        T.jl(
            os.path.join(pr, "analysis", "dsp.jsonl"),
            [{"k": "dsp", "t": T0 - 100, "rms_dbfs": [-70.0] * 8}, {"k": "dsp", "t": T0 + 100, "rms_dbfs": [-70.0] * 8}]
            + [{"k": "transient", "t": T0 - 12 + i, "excess_db": 10.0, "level_db": -40.0, "loudest_beam": 2, "beam_spread_db": 1.5} for i in range(25)],
        )
        app = T.build_app(root, pr, rp, board, lambda: T.NOW, [])
        srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
        srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], [])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = T.Client(srv)
        HC, HO, NOW = T.HC, T.HO, T.NOW
        for name, path in (
            ("status", "/api/status"),
            ("events", f"/api/detections/events?since={HC}&until={NOW}&in_region=any&min_conf=0&group=insect&limit=3"),
            ("density", f"/api/detections/density?since={HC}&until={NOW}&in_region=any&min_conf=0&non_species=any&bucket_s=60"),
            ("species", f"/api/detections/species?since={HC}&until={NOW}"),
            ("beams_live", "/api/beams/live"),
            ("beams_stats", f"/api/beams/stats?since={HC}&until={HC + 3600}&band=5"),
            ("beams_heatmap", f"/api/beams/heatmap?since={HO}&until={HO + 600}&band=5&bucket_s=300"),
            ("system", f"/api/system?since={HC - 3600}"),
            ("calibration", "/api/calibration"),
            ("problem", "/api/beams/stats?band=99"),
        ):
            s, h, j = c.get(path)
            assert s in (200, 422), (name, s)
            if name == "events":  # the detail example is the first (newest) event listed here
                s2, _h2, j2 = c.get("/api/detections/event?id=" + j["events"][0]["id"])
                assert s2 == 200, s2
                with open(os.path.join(FIX, "ui_event.json"), "w", encoding="utf-8") as f2:
                    json.dump(j2, f2, indent=1)
                    f2.write("\n")
            with open(os.path.join(FIX, f"ui_{name}.json"), "w", encoding="utf-8") as f:
                json.dump(j, f, indent=1)
                f.write("\n")
        srv.shutdown()
        rd = os.path.join(root, "rollups")
        with open(os.path.join(FIX, "ui_rollup.jsonl"), "wb") as out:
            for fn in sorted(os.listdir(rd)):
                out.write(open(os.path.join(rd, fn), "rb").read())
    finally:
        board.close()
        shutil.rmtree(root, ignore_errors=True)


main()
