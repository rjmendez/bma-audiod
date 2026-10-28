#!/usr/bin/env python3
"""Smoke test against the REAL pipeline data (read-only): starts bma_ui on 127.0.0.1 (ephemeral port, rollups into a scratch
directory so nothing under the reports directory is written), waits for the backfill, GETs every endpoint and prints the top-line numbers.
Exit 0 when every endpoint answered 200 with the expected shape.

    python smoke_real.py [--pipeline-root DIR] [--reports-dir DIR] [--no-board] [--rollup-dir DIR]
Defaults: $BMA_DATA_DIR (./data), <data-dir>/reports; the board is $BMA_BOARD (an IP address), its token $BMA_DSPD_TOKEN_FILE."""

import argparse, http.client, json, os, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pipeline-root", default=U.data_dir())
    ap.add_argument("--reports-dir", default=os.path.join(U.data_dir(), "reports"))
    ap.add_argument("--rollup-dir", default=None)
    ap.add_argument("--no-board", action="store_true")
    ap.add_argument("--window-h", type=float, default=48.0)
    a = ap.parse_args()
    scratch = a.rollup_dir or tempfile.mkdtemp(prefix="bma_ui_smoke_")
    url = U.board_url_from_env()
    board = U.Board(url, os.environ.get("BMA_DSPD_TOKEN_FILE", ""), enabled=not a.no_board and bool(url))
    app = U.App(a.pipeline_root, a.reports_dir, scratch, board, window_h=a.window_h)
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
    port = srv.server_address[1]
    srv.allowed_hosts = U.allowed_hosts_for(port, [])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t0 = time.time()
    app.start_backfill()
    print(
        f"startup backfill: {time.time() - t0:.1f} s ({app.beams.size()} beam windows, {len(app.dets.snapshot())} events, {len(app.stream.samples)} stream samples)"
    )
    if board.enabled:
        board.refresh(["info", "state", "monitor", "health", "network", "dsps", "calibration"])
    bad = []

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.putrequest("GET", path, skip_host=True)
        c.putheader("Host", f"127.0.0.1:{port}")
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        t = time.time()
        if r.status != 200:
            bad.append((path, r.status))
        return json.loads(body)

    now = time.time()
    d = get("/api/status")
    print(
        f"/api/status: bridge {d['bridge']['state']} loss {d['bridge']['loss_pct']:.2f} %, board reachable={d['board']['reachable']} api={d['board']['api_version']}, "
        f"any_stale={d['any_stale']}, last event {d['last_event']['species'] if d['last_event'] else None}"
    )
    print(
        "  sources: "
        + ", ".join(
            f"{s['name']}={'ok' if s['stale'] is False else ('event' if s['stale'] is None and s['present'] else 'STALE/missing')}({s['garbage_lines']} garbage)"
            for s in d["sources"]
        )
    )
    d = get(f"/api/detections/events?since={int(now - 86400)}&until={int(now)}&limit=5")
    print(f"/api/detections/events (24 h, in-region): total {d['total']}, newest {d['events'][0]['species'] if d['events'] else None}")
    d = get(f"/api/detections/events?since={int(now - 86400)}&until={int(now)}&in_region=false&limit=1")
    d2 = get(f"/api/detections/events?since={int(now - 86400)}&until={int(now)}&non_species=true&limit=1")
    print(f"/api/detections/events (24 h): out-of-region events {d['total']} (from bird_filtered records), non-species events {d2['total']}")
    d = get(f"/api/detections/species?since={int(now - 86400)}&until={int(now)}")
    print(
        f"/api/detections/species (24 h): {len(d['species'])} species, {d['events_total']} events, top {d['species'][0]['species'] if d['species'] else None}"
    )
    d = get("/api/beams/live")
    print(
        f"/api/beams/live: {d['windows']} windows, newest {d['newest_age_s']} s old, 4-8 kHz leader beam {d['bands'][6]['leader_beam']}, contrast {d['bands'][6]['contrast_db']} dB"
    )
    d = get(f"/api/beams/stats?since={int(now - 86400)}&until={int(now)}&band=6")
    print(f"/api/beams/stats (24 h, 4-8 kHz): {d['windows']} windows, rel-to-ring " + " ".join(f"B{b['beam']}={b['rel_ring_db']:+.2f}" for b in d["beams"]))
    d = get(f"/api/beams/heatmap?since={int(now - 6 * 3600)}&until={int(now)}&band=6&bucket_s=60")
    print(f"/api/beams/heatmap (6 h, 60 s): {d['n_buckets']} buckets, {d['windows']} windows, basis {d['basis']}")
    d = get(f"/api/system?since={int(now - 86400)}")
    print(
        f"/api/system: {d['stream']['n_samples']} loss samples, mean {d['stream']['loss_pct_mean']} %, {len(d['stream']['hourly'])} hourly rows, board temps {d['board']['temperatures']['local_c']}/{d['board']['temperatures']['remote_c']} C, "
        f"{len(d['board']['dsps'])} DSPs, {len(d['pipeline']['plugins'])} plugins"
    )
    d = get("/api/calibration")
    print(f"/api/calibration: calibrated={d['calibrated']} frame={d['frame']!r}")
    app.housekeeping()
    print(f"rollups written to scratch dir: {len(app.rollups.recs)} records, write errors {app.rollups.write_errors}")
    srv.shutdown()
    print("FAIL: " + str(bad) if bad else "all endpoints 200")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
