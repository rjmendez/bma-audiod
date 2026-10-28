#!/usr/bin/env python3
"""DOM-level checks (real headless Microsoft Edge, the real bma_ui server over synthetic pipeline files) of the second visualisation round:
the Beams-tab direction strip (beams ordered by azimuth, level against the ring mean, physical names, 'not steerable' on 50-250 Hz), the event
detail panel (per-beam confidence / level trace, beams, group, species, provenance, neighbours within 60 s, one row for a continuous caller, markup
as text, keyboard, close, event gone), the species x hour x direction multiples (top species per group, share cells, 'too few' hours) and the
24 h health sparklines in the status strip (loss, board temperature, stream state, throttled refresh).

    python test_dom_viz.py          (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' if Edge is missing)
Env BMA_UI_DIR selects the ui directory under test (the mutation script uses it); BMA_DOM_WAIT shortens the page-load wait."""

import os, shutil, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import bma_ui as U  # noqa: E402
import test_bma_ui as T  # noqa: E402
import test_dom_edge as D  # noqa: E402

check, Browser, EDGE = D.check, D.Browser, D.EDGE
PANEL = [75, 30, 345, 300, 255, 210, 165, 120]
LABELS_BY_AZ = ["B2 30° up-right", "B1 75° right", "B8 120° down-right", "B7 165° down", "B6 210° down-left", "B5 255° left", "B4 300° up-left", "B3 345° up"]
STEER = "Not steerable: 50-250 Hz is identical on all beams (the array cannot steer below 250 Hz), so differences between beams in this band are channel gain or noise, not direction."


def fmt_sec(t):  # the page's "M/DD HH:MM:SS" local time
    lt = time.localtime(t)
    return "%d/%02d %02d:%02d:%02d" % (lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)


def tree(root, name, now, birds=None, bridge=None, sink=None):
    pr, rp = os.path.join(root, name + "_pipeline"), os.path.join(root, name + "_reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
        os.makedirs(d)
    T.jl(
        os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(now - i, None, extra={(2, 6): -10.0}) for i in range(1200, 0, -5)]
    )  # B3 (index 2) is 20 dB above the others in 4-8 kHz only
    T.jl(os.path.join(pr, "analysis", "birds.jsonl"), birds or [T.bird(now - 300, 2, 0.8, "Alpha Wren", "Troglodytes aedon")])
    if bridge:
        with open(os.path.join(pr, "logs", "bridge.log"), "w") as f:
            for t, loss, state in bridge:
                f.write(
                    "%s stats: 2990 pkt/s loss %s%% gaps=100 back=0 bad=0 blocks=5 sent=1 all_sent=1 state=%s nch=8 group=x\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)), loss, state)
                )
    import json

    json.dump(
        {"t": now - 5, "state": "ok", "loss_pct": 1.2, "gaps": 7, "pkts": 99, "pps": 2990.0, "uptime_s": 100.0, "nch": 8},
        open(os.path.join(pr, "bridge_status.json"), "w"),
    )
    json.dump(
        {"t": now - 5, "audio_output": True, "playing": True, "holes": 7, "data_dropped": 0, "underruns": 2} if sink is None else sink,
        open(os.path.join(pr, "sink_status.json"), "w"),
    )
    return pr, rp


def serve(root, name, pr, rp, board, fix):
    app = U.App(pr, rp, os.path.join(root, name + "_rollups"), board, window_h=48, slot_fix_at=fix)
    app.start_backfill()
    threading.Thread(target=app.run_loop, daemon=True).start()
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
    srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], [])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    root = tempfile.mkdtemp(prefix="bma_ui_domv_")
    browsers, edge_profiles, servers, boards = [], [], [], []
    try:
        now = time.time()
        hs = int(now) // 3600 * 3600
        Bb = hs - 1800  # the middle of the previous UTC hour: every event below falls in that one hour
        H = time.localtime(Bb).tm_hour
        tok = os.path.join(root, "token")
        open(tok, "w").write(T.TOKEN + "\n")
        fk = T.FakeBoard()
        boards.append(fk)
        fk.calibration = {"beam_azimuth_deg": PANEL, "measured_at": "2026-10-25T17:47:00Z"}
        bd = U.Board(f"http://127.0.0.1:{fk.port}", tok, timeout=3.0)
        bd.refresh(["info", "state", "monitor", "health", "network", "dsps", "calibration"], now=time.time())
        toad = {
            "k": "bird",
            "t": Bb - 597,
            "beam": 5,
            "level_dbfs": None,
            "common": "Eastern Narrow-mouthed Toad",
            "scientific": "Gastrophryne carolinensis",
            "confidence": 0.6,
            "provenance": "model_opinion",
            "in_region": True,
            "non_species": False,
        }
        birds = [
            T.bird(Bb - 600, 2, 0.5, "Alpha Wren", "Troglodytes aedon", level=-61.0),
            T.bird(Bb - 599, 3, 0.8, "Alpha Wren", "Troglodytes aedon", level=-58.0),
            T.bird(Bb - 598, 2, 0.6, "Alpha Wren", "Troglodytes aedon", level=-62.0),
        ]
        birds += [
            T.bird(Bb - 640 + 4 * i, 1 + i % 8, 0.4, "Common Virtuoso Katydid", "Amblycorypha longinicta") for i in range(20)
        ]  # a continuous caller: 20 events 4 s apart, one per beam in turn
        birds += [
            toad,
            T.bird(Bb - 605, 6, 0.7, "<img src=x onerror=window.__xss=3>", "Zzyzx xss"),
            T.bird(Bb - 570, 7, 0.6, "White-tailed Deer", "Odocoileus virginianus"),
            T.bird(Bb - 300, 1, 0.9, "Great Horned Owl", "Bubo virginianus"),
            T.bird(Bb - 290, 1, 0.9, "Great Horned Owl", "Bubo virginianus"),
        ]
        ts = now - 20000
        bridge = [(ts + 30 * i, loss, "stall" if i in (4, 5) else "ok") for i, loss in enumerate((0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 2.0, 1.0, 0.5, 0.2))]
        pr, rp = tree(root, "v", now, birds, bridge)
        srv, base = serve(root, "v", pr, rp, bd, int(now - 40 * 86400))
        servers.append(srv)
        edge_prof = os.path.join(tempfile.gettempdir(), "bma_ui_domv_edge_%d_%d" % (os.getpid(), int(time.time())))
        edge_profiles.append(edge_prof)
        br = Browser(edge_prof, 9900 + srv.server_address[1] % 90)
        browsers.append(br)
        time.sleep(2.5)

        # ------------------------------------------------------------------ health sparklines in the status strip
        br.dump(base + "#/overview")
        time.sleep(1.5)
        h = br.eval("""(() => { const cards = Array.from(document.querySelectorAll('#strip .card')).map(c => [c.querySelector('.k').textContent, c.querySelector('.v').textContent, c.className, Array.from(c.querySelectorAll('.s > .muted')).map(m => m.textContent)]);
          const aria = Array.from(document.querySelectorAll('#strip svg.spark')).map(s => s.getAttribute('aria-label'));
          return [cards.slice(-3), aria, document.querySelectorAll('#strip svg.spark:not(.state-strip) .spark-line').length, Array.from(document.querySelectorAll('#strip .state-run')).map(r => r.getAttribute('data-state'))]; })()""")
        check(
            "health strip: three cards after the six originals, in order, with exact values (loss mean 1.42 %, board 41.0 degC local, latest stream state ok) and the card of the stalled stream marked warn",
            isinstance(h, list)
            and [c[:3] for c in h[0]]
            == [["Loss, 24 h", "1.42 % mean", "card "], ["Board temp, 24 h", "41.0 °C local", "card "], ["Stream state, 24 h", "ok", "card warn"]],
            str(h),
        )
        check(
            "health strip: the text under each sparkline carries the same numbers (max, latest, count; min to max and the remote temperature; ok share, other states, no-data time, sink now)",
            isinstance(h, list)
            and [c[3] for c in h[0]]
            == [
                ["max 3.00 % · latest 0.20 % · 10 samples"],
                ["41.0 to 41.0 °C local · remote now 42.5 °C"],
                ["ok 0.3 % of the last 24 h, other states 60 s, no data 23.9 h", "sink now: playing, 7 holes, 2 underruns"],
            ],
            str(h),
        )
        check(
            "health strip: every sparkline has an exact aria-label (not colour or shape only), and the loss and temperature lines are drawn",
            isinstance(h, list)
            and h[1]
            == [
                "Packet loss over the last 24 hours: mean 1.42 %, maximum 3.00 %, latest 0.20 %",
                "Board local temperature over the last 24 hours: minimum 41.0, maximum 41.0, latest 41.0 degrees Celsius",
                "Stream state over the last 24 hours: ok 0.3 % of the last 24 h, other states 60 s, no data 23.9 h",
            ]
            and h[2] == 2,
            str(h),
        )
        check(
            "health strip: the state strip is runs in time order - no data, ok, the 60 s stall, ok, no data - the stall is a taller run with its own state name (text in the title), not only a colour",
            isinstance(h, list) and h[3] == ["no data", "ok", "stall", "ok", "no data"],
            str(h),
        )
        ys = br.eval(
            """(() => { const d = document.querySelector('#strip svg.spark:not(.state-strip) .spark-line').getAttribute('d'); return d.match(/[ML][0-9.]+ ([0-9.]+)/g).map(m => m.split(' ')[1]); })()"""
        )
        check(
            "health strip: the loss line is scaled from 0 to the maximum (not from its own minimum): y of the 10 samples 0.5 1 1.5 2 2.5 3 2 1 0.5 0.2 % on a 28 px sparkline is exactly 22.0 18.0 14.0 10.0 6.0 2.0 10.0 18.0 22.0 24.4",
            ys == ["22.0", "18.0", "14.0", "10.0", "6.0", "2.0", "10.0", "18.0", "22.0", "24.4"],
            str(ys),
        )
        r = br.eval("""(async () => { const a = S.healthAt; await refresh(); const b = S.healthAt; return [a > 0, a === b]; })()""")
        check(
            "health strip: the 24 h series are fetched once a minute, not on every 7 s refresh (healthAt unchanged by an immediate refresh), positive twin: it was fetched (healthAt set)",
            r == [True, True],
            str(r),
        )
        stall = br.eval(
            "Array.from(document.querySelectorAll('#strip .state-run')).filter(r => r.getAttribute('data-state') === 'stall').map(r => [r.getAttribute('height'), r.querySelector('title').textContent.split(', ')[0], r.getAttribute('fill')])"
        )
        check(
            "health strip: the stall run is full height (14) and filled, an ok run is the 4 px baseline",
            stall == [["14", "stall", "var(--bad)"]]
            and br.eval("Array.from(document.querySelectorAll('#strip .state-run[data-state=ok]')).map(r => r.getAttribute('height'))") == ["4", "4"],
            str(stall),
        )

        # ------------------------------------------------------------------ Beams tab: direction strip
        br.dump(base + "#/beams")
        time.sleep(1.5)
        px = """(row) => { const cv = BM.dir.querySelector('canvas'), g = cv.getContext('2d'), W = cv.width, y = 6 + row * 22 + 8; let x = W - 11;
          const t = document.createElement('canvas').getContext('2d'); t.fillStyle = cssVar('--panel2'); t.fillRect(0, 0, 1, 1); const nd = t.getImageData(0, 0, 1, 1).data;     // the 'no data' colour of the current theme
          for (; x > 130; x--) { const d = g.getImageData(x, y, 1, 1).data; if (!(d[0] === nd[0] && d[1] === nd[1] && d[2] === nd[2])) return [d[0], d[1], d[2]]; } return null; }"""
        r = br.eval("(" + px + ")(0)")
        r7 = br.eval("(" + px + ")(7)")
        r1 = br.eval("(" + px + ")(1)")
        check(
            "direction strip, 4-8 kHz: row 0 (B2, 30 deg) is below the ring by more than 6 dB (first viridis stop), row 7 (B3 at 345 deg = up, the hot beam) is above it by more than 6 dB (last stop)",
            r == [68, 1, 84] and r7 == [253, 231, 37] and r1 == [68, 1, 84],
            str([r, r1, r7]),
        )
        lab = br.eval("scatterLanes(curAz()).map(b => beamDirLabel(b, curAz()[b - 1], curKind()))")
        check(
            "direction strip rows: beams ordered by azimuth, each named by its physical direction in the panel frame (exact labels)",
            lab == LABELS_BY_AZ,
            str(lab),
        )
        al = br.eval("BM.dir.querySelector('canvas').getAttribute('aria-label')")
        check(
            "direction strip canvas has the exact aria-label and no steerable note in 4-8 kHz; panel h2 exists",
            al == "Direction strip, beams ordered by azimuth, level relative to the ring mean, 4-8 kHz"
            and br.eval("BM.dir.querySelectorAll('.note-warn').length") == 0
            and br.eval("document.querySelector('#dir-strip-panel h2').firstChild.textContent") == "Direction strip: time × direction",
            str(al),
        )
        r = br.eval("""(async () => { S.band = 0; await refreshBeams(); await new Promise(r => setTimeout(r, 1500)); return [BM.dir.querySelector('.note-warn .tip-bubble').textContent, BM.dir.querySelector('canvas').getAttribute('aria-label'),
          Array.from(document.querySelectorAll('#tab-beams .bar button[title]')).map(b => [b.textContent, b.title]), BM.dir.querySelector('.note-warn .tbadge-text').textContent]; })()""")
        check(
            "direction strip, 50-125 Hz: the 'not steerable' note under the strip (exact text), in the canvas aria-label, and the two low band buttons carry it as their title (the others do not)",
            isinstance(r, list)
            and r[0] == STEER
            and r[1] == "Direction strip, beams ordered by azimuth, level relative to the ring mean, 50-125 Hz. " + STEER
            and r[2] == [["50-125 Hz", STEER], ["125-250 Hz", STEER]]
            and r[3] == "Not steerable",
            str(r),
        )
        r0, r7b = br.eval("(" + px + ")(0)"), br.eval("(" + px + ")(7)")
        check(
            "direction strip, 50-125 Hz: all beams are equal there, so every row sits at 0 dB vs the ring (the middle viridis stop): the strip shows no direction where there is none",
            r0 == [33, 144, 141] and r7b == [33, 144, 141],
            str([r0, r7b]),
        )
        r = br.eval(
            """(async () => { S.band = 2; await refreshBeams(); await new Promise(r => setTimeout(r, 1500)); return [BM.dir.querySelectorAll('.note-warn').length, BM.dir.querySelector('canvas').getAttribute('aria-label')]; })()"""
        )
        check(
            "direction strip, 250-500 Hz (positive twin): no note, the label names the band",
            r == [0, "Direction strip, beams ordered by azimuth, level relative to the ring mean, 250-500 Hz"],
            str(r),
        )

        # ------------------------------------------------------------------ Detections: event detail
        br.dump(base + "#/detections")
        time.sleep(1.5)
        eid = "%d-troglodytes-aedon" % int((Bb - 600) * 1000)
        r = br.eval(
            "[DT.detail.hidden, document.querySelectorAll('#tab-detections tr[data-eid]').length, document.querySelector('#tab-detections tr[data-eid=\"%s\"]' ).getAttribute('tabindex')]"
            % eid
        )
        check(
            "event detail: closed on load; 26 event rows (20 katydid + Alpha Wren + toad + deer + unknown label + 2 owl), each focusable (tabindex 0)",
            r == [True, 26, "0"],
            str(r),
        )
        r = br.eval(
            """(async () => { document.querySelector('#tab-detections tr[data-eid="%s"]').click(); await new Promise(r => setTimeout(r, 2500));
          const facts = Array.from(DT.detail.querySelectorAll('table.facts tr')).map(t => [t.children[0].textContent, t.children[1].textContent]);
          const per = Array.from(DT.detail.querySelectorAll(':scope > div.grid .tablewrap tbody tr')).map(t => Array.from(t.children).map(c => c.textContent));
          const nb = Array.from(DT.detail.querySelectorAll(':scope > .tablewrap tbody tr')).map(t => Array.from(t.children).map(c => c.textContent));
          const svgs = DT.detail.querySelectorAll('svg[role=img]');
          const pts = (svg) => Array.from(svg.querySelectorAll('.trace-pt')).map(p => [p.getAttribute('data-beam'), p.tagName, p.querySelector('title').textContent]);
          return { hidden: DT.detail.hidden, facts: facts, per: per, nb: nb, nbnote: DT.detail.querySelector(':scope > .tablewrap > .tipnote .tip-bubble').textContent, conf: [svgs[0].getAttribute('aria-label'), pts(svgs[0]), svgs[0].querySelectorAll('path[stroke]').length],
            lvl: [svgs[1].getAttribute('aria-label'), pts(svgs[1])], cur: Array.from(document.querySelectorAll('#tab-detections tr[aria-current]')).map(t => t.getAttribute('data-eid')), img: DT.detail.querySelectorAll('img').length, xss: typeof window.__xss, h3: Array.from(DT.detail.querySelectorAll('h3')).map(h => h.textContent) }; })()"""
            % eid
        )
        check(
            "event detail: opens, and the facts are exact - species with scientific name, group, provenance model_opinion, time span, beams involved (physical names), best beam, report count, region",
            isinstance(r, dict)
            and r["hidden"] is False
            and r["facts"]
            == [
                ["Species", "Alpha Wren (Troglodytes aedon)"],
                ["Group", "Bird"],
                ["Provenance", "model_opinion (a model's opinion, not a confirmed identification)"],
                ["Time", fmt_sec(Bb - 600) + " to " + fmt_sec(Bb - 598) + " (2.0 s), local"],
                ["Beams involved", "B2 30° up-right, B3 345° up (2 of 8)"],
                ["Best beam", "B3 345° up, confidence 0.80"],
                ["Reports", "3"],
                ["Region", "in region"],
            ],
            str(r)[:900],
        )
        check(
            "event detail: per-beam table - B2 had 2 reports (max confidence 0.60, max level -61.0), B3 one (0.80, -58.0)",
            isinstance(r, dict) and r["per"] == [["B2 30° up-right", "2", "0.60", "-61.0"], ["B3 345° up", "1", "0.80", "-58.0"]],
            str(r.get("per") if isinstance(r, dict) else r),
        )
        check(
            "event detail: the confidence trace has 3 points (beam 2 as squares, beam 3 as a triangle: shape as well as colour), the exact tooltip of each, and one connecting line (only beam 2 has two reports); exact aria-label",
            isinstance(r, dict)
            and r["conf"]
            == [
                "Confidence of each report, per beam: beam 2 2 report(s), beam 3 1 report(s)",
                [["2", "rect", "beam 2 at +0.00 s: 0.50"], ["2", "rect", "beam 2 at +2.00 s: 0.60"], ["3", "polygon", "beam 3 at +1.00 s: 0.80"]],
                1,
            ],
            str(r.get("conf") if isinstance(r, dict) else r),
        )
        check(
            "event detail: the level trace has the same 3 points with their levels in dBFS",
            isinstance(r, dict)
            and r["lvl"]
            == [
                "Level of each report, per beam: beam 2 2 report(s), beam 3 1 report(s)",
                [
                    ["2", "rect", "beam 2 at +0.00 s: -61.0 dBFS"],
                    ["2", "rect", "beam 2 at +2.00 s: -62.0 dBFS"],
                    ["3", "polygon", "beam 3 at +1.00 s: -58.0 dBFS"],
                ],
            ],
            str(r.get("lvl") if isinstance(r, dict) else r),
        )
        check(
            "event detail: neighbours within 60 s - the katydid chorus (20 events) is ONE row, then the toad (+1.0 s), the unknown label (-5.0 s), the deer (+28.0 s); the owl 298 s later (inside a 600 s window but outside 60 s) is not there; markup shown as text",
            isinstance(r, dict)
            and r["nb"]
            == [
                ["Common Virtuoso Katydid", "Insect", "20", "overlaps", "B1 B2 B3 B4 B5 B6 B7 B8", "0.40"],
                ["Eastern Narrow-mouthed Toad", "Amphibian", "1", "+1.0 s", "B5", "0.60"],
                ["<img src=x onerror=window.__xss=3>", "Other biological", "1", "-5.0 s", "B6", "0.70"],
                ["White-tailed Deer", "Mammal", "1", "+28.0 s", "B7", "0.60"],
            ]
            and r["img"] == 0
            and r["xss"] == "undefined",
            str(r.get("nb") if isinstance(r, dict) else r),
        )
        check(
            "event detail: the neighbour note states the counts and that one row is one species",
            isinstance(r, dict)
            and r["nbnote"] == "23 other event(s) of 4 species within 60 s before and after (one row per species, so a continuous caller is one row).",
            str(r.get("nbnote") if isinstance(r, dict) else r),
        )
        check(
            "event detail: the clicked row is marked aria-current (not colour only) and only that row; the headings are exact",
            isinstance(r, dict) and r["cur"] == [eid] and r["h3"] == ["Confidence per beam", "Level per beam", "Detections within ±60 s"],
            str(r.get("cur") if isinstance(r, dict) else r),
        )
        r = br.eval(
            """(async () => { const row = document.querySelector('#tab-detections tr[data-eid^="%d-gastrophryne"]'); row.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await new Promise(r => setTimeout(r, 2500));
          const facts = Array.from(DT.detail.querySelectorAll('table.facts tr')).map(t => [t.children[0].textContent, t.children[1].textContent]);
          const svgs = DT.detail.querySelectorAll('svg[role=img]');
          return { facts: facts, per: Array.from(DT.detail.querySelectorAll(':scope > div.grid .tablewrap tbody tr')).map(t => Array.from(t.children).map(c => c.textContent)), pts: svgs[0].querySelectorAll('.trace-pt').length, paths: svgs[0].querySelectorAll('path[stroke]').length,
            lvl: DT.detail.querySelector('.chart:last-child').textContent, cur: Array.from(document.querySelectorAll('#tab-detections tr[aria-current]')).map(t => t.getAttribute('data-eid')) }; })()"""
            % int((Bb - 597) * 1000)
        )
        check(
            "event detail by keyboard (Enter on the focusable row): a single-report event without a level - single instant, 1 beam, level 'n/a', one point and no line, the level chart says no level was reported, only the new row is current",
            isinstance(r, dict)
            and r["facts"][0] == ["Species", "Eastern Narrow-mouthed Toad (Gastrophryne carolinensis)"]
            and r["facts"][1] == ["Group", "Amphibian"]
            and r["facts"][3] == ["Time", fmt_sec(Bb - 597) + " (a single instant), local"]
            and r["facts"][4] == ["Beams involved", "B5 255° left (1 of 8)"]
            and r["per"] == [["B5 255° left", "1", "0.60", "n/a"]]
            and r["pts"] == 1
            and r["paths"] == 0
            and r["lvl"] == "No report of this event carried a level."
            and r["cur"] == ["%d-gastrophryne-carolinensis" % int((Bb - 597) * 1000)]
            if isinstance(r, dict)
            else False,
            str(r),
        )
        r = br.eval(
            """(async () => { DT.detail.querySelector('.close-detail').click(); return [DT.detail.hidden, S.detailId, document.querySelectorAll('#tab-detections tr[aria-current]').length]; })()"""
        )
        check("event detail: Close hides the panel, forgets the event and clears the current-row mark", r == [True, None, 0], str(r))
        r = br.eval(
            """(async () => { await openEvent('1-gone'); await new Promise(r => setTimeout(r, 500)); return [DT.detail.hidden, DT.detail.textContent]; })()"""
        )
        check(
            "event detail: an event that left the in-memory window says so (exact text), the panel is open with a Close button",
            r
            == [
                False,
                "Event detailClose"
                + "This event is no longer in the server's in-memory window (events older than the window are kept only as hourly rollups).",
            ],
            str(r),
        )
        r = br.eval("""(async () => { await openEvent('BAD ID'); await new Promise(r => setTimeout(r, 500)); return DT.detail.textContent; })()""")
        check(
            "event detail: a malformed id is shown as the server's validation error, not as 'no longer in memory' (negative twin)",
            r == "Event detailClose" + "Invalid parameter: expected an event id such as 1792940400000-common-virtuoso-katydid",
            str(r),
        )

        # ------------------------------------------------------------------ species x hour x direction multiples
        r = br.eval("""(async () => { await refreshDetections(); await new Promise(r => setTimeout(r, 2500));
          const figs = Array.from(document.querySelectorAll('#hour-dir-panel figure.dh-fig'));
          return { caps: figs.map(f => f.querySelector('figcaption').textContent), aria: figs.map(f => f.querySelector('svg.dirhour').getAttribute('aria-label')), note: DT.smNote.querySelector('.tip-bubble').textContent, img: document.querySelectorAll('#hour-dir-panel img').length }; })()""")
        hh = "%d:00" % H
        check(
            "multiples: the top 2 species of EVERY group, in group order (birds, insect, amphibian, mammal, other biological) - the 20-event katydid does not crowd out the toad, the deer or the unknown label",
            isinstance(r, dict)
            and r["caps"]
            == [
                " Great Horned Owl (Bird), 2 events, busiest hour " + hh,
                " Alpha Wren (Bird), 1 event, busiest hour " + hh,
                " Common Virtuoso Katydid (Insect), 20 events, busiest hour " + hh,
                " Eastern Narrow-mouthed Toad (Amphibian), 1 event, busiest hour " + hh,
                " White-tailed Deer (Mammal), 1 event, busiest hour " + hh,
                " <img src=x onerror=window.__xss=3> (Other biological), 1 event, busiest hour " + hh,
            ]
            and r["img"] == 0,
            str(r.get("caps") if isinstance(r, dict) else r),
        )
        check(
            "multiples: exact svg aria-label of the katydid figure",
            isinstance(r, dict) and r["aria"][2] == "Common Virtuoso Katydid, Insect: 20 events by hour of day and direction, busiest hour " + hh + " with 20",
            str(r.get("aria") if isinstance(r, dict) else r),
        )
        check(
            "multiples: the note says how to read it, the 5-event minimum and that the filters apply",
            isinstance(r, dict)
            and "Hours with fewer than 5 events are drawn dashed and empty: too few to read a direction from." in r["note"]
            and r["note"].startswith("The 2 busiest species of each group"),
            str(r.get("note") if isinstance(r, dict) else r),
        )
        r = br.eval(
            """(() => { const fig = n => Array.from(document.querySelectorAll('#hour-dir-panel figure.dh-fig')).find(f => f.getAttribute('data-species') === n), H = %d;
          const kat = fig('Common Virtuoso Katydid'), cell = (f, hr, b) => f.querySelector('.dh-cell[data-hour="' + hr + '"][data-beam="' + b + '"]'), at = c => [c.getAttribute('data-events'), c.getAttribute('fill'), c.getAttribute('fill-opacity'), c.getAttribute('stroke-dasharray')];
          const wren = fig('Alpha Wren'), labels = Array.from(kat.querySelectorAll('svg.dirhour > text')).slice(0, 8).map(t => t.textContent);
          return { kat1: at(cell(kat, H, 1)), kat8: at(cell(kat, H, 8)), katOther: at(cell(kat, (H + 5) %% 24, 1)), wren3: at(cell(wren, H, 3)), wrenTitle: cell(wren, H, 3).querySelector('title').textContent, katTitle: cell(kat, H, 1).querySelector('title').textContent,
            total: [kat.querySelector('.dh-total[data-hour="' + H + '"]').getAttribute('height'), kat.querySelector('.dh-total[data-hour="' + H + '"]').getAttribute('data-events'), kat.querySelector('.dh-total[data-hour="' + ((H + 5) %% 24) + '"]').getAttribute('height')], labels: labels, cells: kat.querySelectorAll('.dh-cell').length }; })()"""
            % H
        )
        check(
            "multiples: a readable hour (20 katydid events) shades each direction cell by its SHARE: beam 1 had 3 of 20 (opacity 0.06 + 0.94 x 0.15 = 0.201), beam 8 had 2 of 20 (0.154), filled with the insect colour",
            isinstance(r, dict)
            and r["kat1"] == ["3", "#E69F00", "0.201", None]
            and r["kat8"] == ["2", "#E69F00", "0.154", None]
            and r["katTitle"] == "%s B1 75° right: 3 of 20 events (15 %%)" % hh,
            str(r),
        )
        check(
            "multiples: an hour with 1 event (Alpha Wren, best beam 3) is too few to read a direction: dashed, no fill, with that said in the title - positive twin of the readable katydid hour above",
            isinstance(r, dict)
            and r["wren3"] == ["1", "none", None, "1.5 1.5"]
            and r["wrenTitle"] == "%s B3 345° up: 1 of 1 events (too few to read a direction)" % hh,
            str(r),
        )
        check(
            "multiples: an hour without events is empty (0 events, dashed, no fill); the per-hour total bar is full height (16) for the busiest hour and 0 elsewhere; 8 directions x 24 hours = 192 cells; rows ordered by azimuth with physical names",
            isinstance(r, dict)
            and r["katOther"] == ["0", "none", None, "1.5 1.5"]
            and r["total"] == ["16.0", "20", "0.0"]
            and r["cells"] == 192
            and r["labels"] == LABELS_BY_AZ,
            str(r),
        )

        # ------------------------------------------------------------------ a tree without a board, without stream samples
        pr2, rp2 = tree(root, "n", time.time(), None, None)
        srv2, base2 = serve(root, "n", pr2, rp2, U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False), int(now - 40 * 86400))
        servers.append(srv2)
        time.sleep(2.5)
        br.dump(base2 + "#/overview")
        time.sleep(1.5)
        h = br.eval(
            """(() => Array.from(document.querySelectorAll('#strip .card')).slice(-3).map(c => [c.querySelector('.k').textContent, c.querySelector('.v').textContent, Array.from(c.querySelectorAll('.s > .muted')).map(m => m.textContent).concat(c.querySelectorAll('.s > .muted').length ? [] : [c.querySelector('.s').textContent]), c.className, c.querySelectorAll('svg.spark:not(.state-strip)').length]))()"""
        )
        check(
            "health strip, no stream samples and a disabled board: loss and temperature say n/a with the reason (no raw code), the state card says 'no data' with an all-'no data' strip and the sink line",
            h
            == [
                ["Loss, 24 h", "n/a", ["no stream samples in the last 24 h"], "card ", 0],
                ["Board temp, 24 h", "n/a", ["Board polling is disabled."], "card ", 0],
                [
                    "Stream state, 24 h",
                    "no data",
                    ["ok 0.0 % of the last 24 h, other states 0 s, no data 24.0 h", "sink now: playing, 7 holes, 2 underruns"],
                    "card ",
                    0,
                ],
            ],
            str(h),
        )
        lab = br.eval("S.tab = 'beams'; scatterLanes(curAz()).map(b => beamDirLabel(b, curAz()[b - 1], curKind()))")
        check(
            "direction labels in the DSP-frame fallback (no calibration): beam and angle only, no up/down names invented",
            lab == ["B3 0°", "B2 45°", "B1 90°", "B8 135°", "B7 180°", "B6 225°", "B5 270°", "B4 315°"],
            str(lab),
        )
        br.close()
    finally:
        for b in browsers:
            b.close()
        for s in servers:
            s.shutdown()
        for b in boards:
            b.close()
        shutil.rmtree(root, ignore_errors=True)
        for p in edge_profiles:
            shutil.rmtree(p, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
