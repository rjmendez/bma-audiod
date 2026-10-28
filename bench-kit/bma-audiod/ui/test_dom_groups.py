#!/usr/bin/env python3
"""DOM-level checks (real headless Microsoft Edge, the real bma_ui server over synthetic pipeline files) of the taxonomic-group views: group chips with
exact counts, the density view instead of a silently truncated scatter, the zoom / full-range toggle, the Group column and the continuous-caller badge,
the group-aware dot shapes, the Overview recent runs / top species per group / activity by group (stacked and per-group scale), the beam pattern card
(three board states, directions credited to the stored calibration, markup shown as text) and the not-steerable 50-250 Hz bands.

    python test_dom_groups.py          (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' if Edge is missing)
Env BMA_UI_DIR selects the ui directory under test (the mutation script uses it); BMA_DOM_WAIT shortens the page-load wait."""

import json, os, re, shutil, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import bma_ui as U  # noqa: E402
import test_bma_ui as T  # noqa: E402
import test_dom_edge as D  # noqa: E402

check, text_of, Browser, EDGE = D.check, D.text_of, D.Browser, D.EDGE


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    root = tempfile.mkdtemp(prefix="bma_ui_domg_")
    browsers, edge_profiles = [], []
    try:
        now = time.time()
        pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
        for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
            os.makedirs(d)
        T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(now - i, None, extra={(7, 6): -20.0, (0, 5): -25.0}) for i in range(120, 0, -1)])
        T.jl(os.path.join(pr, "analysis", "birds.jsonl"), [T.bird(now - 300, 2, 0.8, "Alpha Wren", "Aus a")])
        board = U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False)
        app = U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=48, slot_fix_at=now - 40 * 86400)
        app.start_backfill()
        threading.Thread(target=app.run_loop, daemon=True).start()
        srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
        port = srv.server_address[1]
        srv.allowed_hosts = U.allowed_hosts_for(port, [])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        edge_prof = os.path.join(tempfile.gettempdir(), "bma_ui_domg_edge_%d_%d" % (os.getpid(), int(time.time())))
        edge_profiles.append(edge_prof)
        br = Browser(edge_prof, 9400 + port % 500)
        browsers.append(br)
        base = f"http://127.0.0.1:{port}/"
        PANEL = [75, 30, 345, 300, 255, 210, 165, 120]
        tok = os.path.join(root, "token")
        open(tok, "w").write(T.TOKEN + "\n")

        def serve(brd, fix):
            a = U.App(pr, rp, os.path.join(root, "rollups_%d" % int(fix)), brd, window_h=48, slot_fix_at=fix)
            a.start_backfill()
            threading.Thread(target=a.run_loop, daemon=True).start()
            s = U.Server(("127.0.0.1", 0), a, set(), U.load_assets())
            s.allowed_hosts = U.allowed_hosts_for(s.server_address[1], [])
            threading.Thread(target=s.serve_forever, daemon=True).start()
            return s, f"http://127.0.0.1:{s.server_address[1]}/"

        time.sleep(2.5)  # the server polls once a second
        # ------------------------------------------------------------------ taxonomic groups, density, zoom, group activity, pattern card, not-steerable bands
        gr = os.path.join(root, "g_pipeline"), os.path.join(root, "g_reports")
        for d in (os.path.join(gr[0], "analysis"), os.path.join(gr[0], "logs"), gr[1]):
            os.makedirs(d)
        T.jl(os.path.join(gr[1], "beam_bands.jsonl"), [T.bands_rec(now - i, None) for i in range(60, 0, -1)])
        g_recs = [
            T.bird(now - 14000 + 20 * i, 1 + i % 8, 0.6, "Common Virtuoso Katydid", "Amblycorypha longinicta") for i in range(700)
        ]  # 700 events, 20 s apart over 3.9 h (4-5 hour buckets): more than the 500-dot cap, a chorus
        g_recs += [T.bird(now - 1800 + 10 * i, 5, 0.9, "Eastern Narrow-mouthed Toad", "Gastrophryne carolinensis") for i in range(3)]
        g_recs += [
            T.bird(now - 1700, 6, 0.9, "White-tailed Deer", "Odocoileus virginianus"),
            T.bird(now - 1600, 2, 0.9, "<img src=x onerror=window.__xss=1>", "Zzyzx xss"),
        ]
        for k in range(9):  # 9 bird species x 4 events, each busier than the toad
            g_recs += [T.bird(now - 3000 + 100 * k + 20 * j, 1 + (k + j) % 8, 0.9, "Bird %d" % k, "Melanerpes b%d" % k) for j in range(4)]
        g_recs += [
            {
                "k": "bird",
                "t": now - 1500,
                "beam": 3,
                "level_dbfs": -60.0,
                "common": "Engine",
                "scientific": "Engine",
                "confidence": 0.9,
                "provenance": "model_opinion",
            },  # an old record: no flag
            T.bird(now - 1400, 3, 0.9, "Dog", "Dog", non=True),
        ]
        T.jl(os.path.join(gr[0], "analysis", "birds.jsonl"), g_recs)

        def serve_g(brd, fix):
            a = U.App(gr[0], gr[1], os.path.join(root, "g_rollups_%d" % int(fix)), brd, window_h=48, slot_fix_at=fix)
            a.start_backfill()
            threading.Thread(target=a.run_loop, daemon=True).start()
            s_ = U.Server(("127.0.0.1", 0), a, set(), U.load_assets())
            s_.allowed_hosts = U.allowed_hosts_for(s_.server_address[1], [])
            threading.Thread(target=s_.serve_forever, daemon=True).start()
            return s_, f"http://127.0.0.1:{s_.server_address[1]}/"

        srv_g, base_g = serve_g(U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False), int(now - 1800))
        time.sleep(2.5)
        dg = br.dump(base_g + "#/detections")
        tdg = text_of(dg)
        chips = br.eval(
            "Array.from(document.querySelectorAll('#tab-detections .chips button')).map(b => [b.getAttribute('data-group'), b.getAttribute('aria-pressed'), b.textContent])"
        )
        check(
            "group chips: one per group with its exact event count (the unflagged old Engine record is not biological (counted as Non-biological 1); Dog is a mammal and counted there)",
            chips
            == [
                ["bio", "true", "All biological (742)"],
                ["bird", "false", "Bird (36)"],
                ["insect", "false", "Insect (700)"],
                ["amphibian", "false", "Amphibian (3)"],
                ["mammal", "false", "Mammal (2)"],
                ["other-bio", "false", "Other biological (1)"],
                ["mechanical", "false", "Non-biological (1)"],
            ],
            str(chips),
        )
        unc = br.eval("[DT.unc.firstChild.textContent, DT.unc.querySelector('.tip-bubble').textContent]")
        check(
            "unclassified labels: the label in no table is named with its event count, filed under Other biological and never dropped (shown as text, markup inert)",
            unc
            == [
                "Unclassified labels: 1 event(s)",
                "Unclassified labels (filed under Other biological, not dropped): 1 event(s): <img src=x onerror=window.__xss=1> (1)",
            ],
            repr(unc),
        )
        g2 = os.path.join(root, "g2_pipeline"), os.path.join(root, "g2_reports")
        for d in (os.path.join(g2[0], "analysis"), os.path.join(g2[0], "logs"), g2[1]):
            os.makedirs(d)
        T.jl(os.path.join(g2[1], "beam_bands.jsonl"), [T.bands_rec(now - i, None) for i in range(60, 0, -1)])
        T.jl(
            os.path.join(g2[0], "analysis", "birds.jsonl"),
            [T.bird(now - 600 + 30 * i, 1 + i % 8, 0.7, "Common Virtuoso Katydid", "Amblycorypha longinicta") for i in range(4)],
        )
        a2 = U.App(
            g2[0],
            g2[1],
            os.path.join(root, "g2_rollups"),
            U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False),
            window_h=48,
            slot_fix_at=int(now - 40 * 86400),
        )
        a2.start_backfill()
        threading.Thread(target=a2.run_loop, daemon=True).start()
        s2 = U.Server(("127.0.0.1", 0), a2, set(), U.load_assets())
        s2.allowed_hosts = U.allowed_hosts_for(s2.server_address[1], [])
        threading.Thread(target=s2.serve_forever, daemon=True).start()
        time.sleep(2.5)
        br.dump(f"http://127.0.0.1:{s2.server_address[1]}/#/detections")
        unc2 = br.eval("[DT.unc.textContent, document.querySelectorAll('#tab-detections .ev-dot').length]")
        check(
            "unclassified labels: positive twin - a tree whose only label is mapped (katydid) has no such line, and its 4 events are dots (4 <= 500)",
            unc2 == ["", 4],
            str(unc2),
        )
        s2.shutdown()
        br.dump(base_g + "#/detections")
        sc = br.eval("""(() => { const cells = Array.from(document.querySelectorAll('#tab-detections .dens-cell')); return [document.querySelectorAll('#tab-detections .ev-dot').length, cells.length > 0,
          cells.reduce((a, c) => a + Number(c.getAttribute('data-events')), 0), document.querySelector('#tab-detections svg[aria-label^="Detections density"]').getAttribute('aria-label').replace(/in \\d+ cells/, 'in N cells'),
          Array.from(new Set(cells.map(c => c.getAttribute('data-group')))).sort()]; })()""")
        check(
            "density: 742 events (more than 500) are NOT truncated to the newest 500 and drawn as dots: no dots, cells whose exact counts sum to 742, aria-label names the total",
            sc
            == [
                0,
                True,
                742,
                "Detections density: 742 events in N cells, time by beam direction, darker is more events",
                ["amphibian", "bird", "insect", "mammal", "other-bio"],
            ],
            str(sc),
        )
        check(
            "density: the note says how many events are aggregated and why (more than 500), and the legend counts per group",
            "742 events are aggregated into" in tdg and "because there are more than 500" in tdg and "Insect (700)" in tdg,
        )
        zoom = br.eval("""(async () => {
          const span = () => { const xs = Array.from(document.querySelectorAll('#tab-detections .dens-cell')).map(c => Number(c.getAttribute('x'))); return Math.max(...xs) - Math.min(...xs); };
          const z = span(), zt = DT.scatterBar.textContent, zp = DT.scatterBar.querySelector('button').getAttribute('aria-pressed');
          DT.scatterBar.querySelector('button').click(); await new Promise(r => setTimeout(r, 2500));
          const f = span(), ft = DT.scatterBar.textContent, fp = DT.scatterBar.querySelector('button').getAttribute('aria-pressed');
          DT.scatterBar.querySelector('button').click(); await new Promise(r => setTimeout(r, 2500));
          return [z > 3 * f, f > 0, zt.includes('the extent of the data'), zp, ft.includes('the full range'), fp, span() > 3 * f];
        })()""")
        check(
            "zoom: the x-axis is the data extent by default (cells spread over the axis, more than 3x wider than on the full 24 h range), the Full range toggle (aria-pressed) shows the whole range, and toggling back zooms again",
            zoom == [True, True, True, "false", True, "true", True],
            str(zoom),
        )
        cell = br.eval("""(() => { const cs = Array.from(document.querySelectorAll('#tab-detections .dens-cell[data-group="insect"]')); const c = cs.sort((a, b) => Number(b.getAttribute('data-events')) - Number(a.getAttribute('data-events')))[0];
          const n = c.getAttribute('data-events'); return [c.querySelector('title').textContent.split(': ').pop() === n + ' event(s) (Common Virtuoso Katydid \u00d7' + n + ')', Number(n) >= 1, /^\\d+\\/\\d+ \\d\\d:\\d\\d Insect, beam B\\d: /.test(c.querySelector('title').textContent)]; })()""")
        check(
            "density: a cell's title names the exact count and the species behind it (busiest insect cell), the group and the beam",
            cell == [True, True, True],
            str(cell),
        )
        gtab = br.eval(
            """(() => { const t = document.querySelector('#tab-detections .tablewrap table'); return [Array.from(t.querySelectorAll('th')).map(h => h.textContent).slice(0, 3), Array.from(t.querySelectorAll('tbody tr')).slice(0, 2).map(r => r.children[2].textContent)]; })()"""
        )
        check(
            "events table has a Group column (Time, Species, Group ...)",
            isinstance(gtab, list)
            and gtab[0] == ["Time", "Species", "Group"]
            and all(x in ("Insect", "Other biological", "Bird", "Amphibian", "Mammal") for x in gtab[1]),
            str(gtab),
        )
        sm = br.eval("""(() => { const t = Array.from(document.querySelectorAll('#tab-detections .tablewrap table')).find(x => x.textContent.includes('Last seen')); const hs = Array.from(t.querySelectorAll('th')).map(h => h.textContent);
          const row = Array.from(t.querySelectorAll('tbody tr')).find(r => r.textContent.includes('Common Virtuoso Katydid')); return [hs.slice(0, 3), row.children[1].textContent, row.children[0].textContent.includes('continuous caller'),
          Array.from(t.querySelectorAll('tbody tr')).filter(r => r.textContent.includes('continuous caller')).length]; })()""")
        check(
            "species summary: Group column, the katydid row says Insect and carries the 'continuous caller' badge (text), and it is the only row that does",
            sm == [["Species", "Group", "Events"], "Insect", True, 1],
            str(sm),
        )
        check(
            "the page has no <img> element from the hostile species label (rendered as text, nothing executed)",
            br.eval("[document.querySelectorAll('#tab-detections img, #tab-overview img').length, typeof window.__xss]") == [0, "undefined"],
        )
        # click the Amphibian chip: events, summary and scatter follow; one group -> species colour and shape
        r = br.eval("""(async () => { S.det.species = 'Odocoileus virginianus'; document.querySelector('#tab-detections .chips [data-group="amphibian"]').click(); await new Promise(r => setTimeout(r, 3500));
          const dots = Array.from(document.querySelectorAll('#tab-detections .ev-dot')).map(d => [d.getAttribute('data-species'), d.getAttribute('data-group'), d.getAttribute('data-shape'), d.tagName]);
          const pressed = Array.from(document.querySelectorAll('#tab-detections .chips button')).filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.getAttribute('data-group'));
          const evrows = Array.from(document.querySelectorAll('#tab-detections .tablewrap')[0].querySelectorAll('tbody tr')).map(t => t.children[1].textContent);
          return [pressed, dots.length, dots[0], new Set(evrows).size, evrows[0], document.querySelector('#tab-detections .legend').textContent]; })()""")
        check(
            "Amphibian chip (a species of another group was selected: the chip clears it): only it is pressed, the 3 toad events are dots (circle, species mode), the table lists only the toad, the legend counts them",
            r
            == [
                ["amphibian"],
                3,
                ["Eastern Narrow-mouthed Toad", "amphibian", "circle", "circle"],
                1,
                "Eastern Narrow-mouthed Toad",
                "Eastern Narrow-mouthed Toad (3)",
            ],
            str(r),
        )
        r = br.eval("""(async () => { S.det.group = 'bio'; S.det.minConf = 0.8; await refreshDetections(); await new Promise(r => setTimeout(r, 1500));
          const dots = Array.from(document.querySelectorAll('#tab-detections .ev-dot')).map(d => d.getAttribute('data-group') + ':' + d.getAttribute('data-shape') + ':' + d.tagName);
          return [Array.from(new Set(dots)).sort(), dots.length, document.querySelector('#tab-detections .legend').textContent]; })()""")
        check(
            "mixed groups (min conf 0.8): colour AND shape by group: bird circle, amphibian triangle, mammal square, other-bio cross (a polygon); legend = group names with counts",
            r
            == [
                ["amphibian:triangle:polygon", "bird:circle:circle", "mammal:square:rect", "other-bio:cross:polygon"],
                42,
                "Bird (36)Amphibian (3)Mammal (2)Other biological (1)",
            ],
            str(r),
        )
        br.eval("S.det.minConf = 0.3; S.det.group = 'bio'; true")
        r = br.eval("""(async () => { document.querySelector('#tab-detections .chips [data-group="mechanical"]').click(); await new Promise(r => setTimeout(r, 3500));
          return Array.from(document.querySelectorAll('#tab-detections .tablewrap')[0].querySelectorAll('tbody tr')).map(t => t.children[1].textContent + '|' + t.children[2].textContent); })()""")
        check(
            "Non-biological chip shows the unflagged old Engine record (and the non-species box is not needed); positive twin: it was absent in the biological view (742 above does not include it)",
            r == ["Engine (non-species)|Non-biological"],
            str(r),
        )
        br.eval("S.det.group = 'bio'; true")
        # overview: runs, top per group, activity by group
        ovg = br.dump(base_g + "#/overview")
        tovg = text_of(ovg)
        r = br.eval("""(() => { const rec = document.querySelector('#tab-overview .tablewrap'); const rows = Array.from(rec.querySelectorAll('tbody tr')).map(t => t.children[1].textContent); const top = Array.from(document.querySelectorAll('#tab-overview .tablewrap')[1].querySelectorAll('tbody tr')).map(t => t.children[0].textContent + '|' + t.children[1].textContent);
          return [rows.length, rows.filter(x => x.startsWith('Common Virtuoso Katydid')), top]; })()""")
        check(
            "overview recent: the katydid events fold into ONE run (not rows of katydid crowding the table); only the newest 500 of 742 events are fetched, so that run reads x458+ and the note says so",
            isinstance(r, list)
            and r[0] <= 10
            and r[1] == ["Common Virtuoso Katydid ×458+"]
            and "only the newest 500 events were fetched" in tovg
            and "742 event(s) in the last 6 h" in tovg,
            str(r),
        )
        check(
            "overview top species: the 3 busiest of each group in group order, so the toad, the deer and the unknown label are listed although 9 bird species have more events than the toad (the old top-8 list lost them)",
            isinstance(r, list)
            and r[2]
            == [
                "Bird 0|Bird",
                "Bird 1|Bird",
                "Bird 2|Bird",
                "Common Virtuoso Katydid|Insect",
                "Eastern Narrow-mouthed Toad|Amphibian",
                "Dog|Mammal",
                "White-tailed Deer|Mammal",
                "<img src=x onerror=window.__xss=1>|Other biological",
            ],
            str(r),
        )
        act = br.eval("""(async () => { const sum = g => Array.from(OV.act.querySelectorAll('.grp-bar[data-group="' + g + '"] title')).reduce((a, t) => a + Number(t.textContent.split(': ').pop()), 0);
          const stacked = [sum('insect'), sum('bird'), sum('amphibian'), sum('mammal'), sum('other-bio'), OV.act.querySelectorAll('.hatch-region').length, OV.act.querySelector('svg').getAttribute('aria-label')];
          Array.from(OV.actBar.querySelectorAll('button')).find(b => b.textContent === 'Each group on its own scale').click(); await new Promise(r => setTimeout(r, 2500));
          const own = {}; for (const c of OV.act.querySelectorAll('.grp-cell')) { const g = c.getAttribute('data-group'); own[g] = Math.max(own[g] || 0, Number(c.getAttribute('fill-opacity'))); }
          return [stacked, own, OV.act.querySelector('svg').getAttribute('aria-label'), Array.from(OV.actBar.querySelectorAll('button')).map(b => b.getAttribute('aria-pressed')), OV.act.querySelectorAll('.hatch-region').length]; })()""")
        check(
            "activity by group: stacked counts add up per group (insect 700, bird 36, amphibian 3, mammal 2, other 1), the pre-fix hatch is drawn once (fix inside the 24 h), exact aria-label",
            isinstance(act, list) and act[0] == [700, 36, 3, 2, 1, 1, "Events per hour by taxonomic group, stacked counts"],
            str(act),
        )
        check(
            "activity by group, own scale: every group's busiest hour is at full opacity (1), so the 3 toad events are as visible as the 700 katydid events; toggle state, label and the pre-fix hatch follow",
            isinstance(act, list)
            and act[1] == {"insect": 1, "bird": 1, "amphibian": 1, "mammal": 1, "other-bio": 1}
            and act[2] == "Events per hour by taxonomic group, each group on its own scale"
            and act[3] == ["false", "true"]
            and act[4] == 1,
            str(act),
        )
        srv_g.shutdown()

        # pattern card: the three board states, the stored calibration as the source of the directions, markup in pattern_ref is text
        def card_of(dom_url, brd, fix):
            s_, b_ = serve(brd, fix)
            br.dump(b_ + "#/overview")
            time.sleep(1.0)
            out = br.eval("""(() => { const c = Array.from(document.querySelectorAll('#strip .card')).find(x => x.querySelector('.k').textContent === 'Beam pattern');
              return [c.querySelector('.v').textContent, c.querySelector('.s').firstChild.textContent, c.querySelector('.pattern-dirs .tip-bubble').textContent, c.querySelectorAll('b, img').length]; })()""")
            s_.shutdown()
            return out

        dirs_txt = "Beam directions from the stored calibration profile (panel frame, upright array, provisional, measured 2026-10-25T17:47:00Z), metadata only, not read back from the DSPs: B1 75° right, B2 30° up-right, B3 345° up, B4 300° up-left, B5 255° left, B6 210° down-left, B7 165° down, B8 120° down-right"
        checked_cards = []
        for label, state, want in (
            (
                "applied without a name",
                {"beams": {"applied": True, "pattern_ref": None}},
                ["applied by dspd", "reported by dspd, which gives no pattern name", dirs_txt, 0],
            ),
            ("applied with a name", {"beams": {"applied": True, "pattern_ref": "pattern-7"}}, ["pattern-7", "reported by dspd (board state)", dirs_txt, 0]),
            ("not applied", {"beams": {"applied": False, "pattern_ref": None}}, ["not applied", "dspd reports no applied pattern", dirs_txt, 0]),
            (
                "hostile pattern_ref",
                {"beams": {"applied": True, "pattern_ref": "<b>bold</b><img src=x onerror=window.__xss=2>"}},
                ["<b>bold</b><img src=x onerror=window.__xss=2>", "reported by dspd (board state)", dirs_txt, 0],
            ),
        ):
            fk = T.FakeBoard()
            fk.calibration = {"beam_azimuth_deg": PANEL, "measured_at": "2026-10-25T17:47:00Z"}
            fk.state = state
            bd_ = U.Board(f"http://127.0.0.1:{fk.port}", tok, timeout=3.0)
            bd_.refresh(["info", "state", "monitor", "health", "network", "dsps", "calibration"], now=time.time())
            got = card_of(None, bd_, int(now - 41 * 86400 - len(checked_cards)))
            checked_cards.append(label)
            check(
                "pattern card, %s: value, provenance, directions credited to the stored calibration, no markup became an element" % label, got == want, str(got)
            )
            fk.close()
        got = card_of(None, U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False), int(now - 40 * 86400))
        check(
            "pattern card, board not readable and no calibration: 'not readable' and no direction claimed",
            got
            == [
                "not readable",
                "the board state could not be read, so the applied pattern is unknown",
                "Beam directions: no calibration profile stored, so no physical direction is known (the DSP-frame fallback angles are not a pattern).",
                0,
            ],
            str(got),
        )

        # not steerable bands: 50-250 Hz are identical on all beams
        br.dump(base + "#/beams")
        r = br.eval("""(async () => { const out = {};
          for (const b of [0, 1, 2, 5]) { S.band = b; await refreshBeams(); await new Promise(r => setTimeout(r, 1200));
            out[b] = [Array.from(BM.steer.querySelectorAll('.tip-bubble')).map(t => t.textContent).join(''), BM.bias.querySelectorAll('.hatch-region').length, Array.from(BM.contrast.querySelectorAll('tbody tr')).filter(t => t.textContent.includes('(not steerable)')).length, BM.compass.querySelectorAll('.steer-note').length, BM.bias.textContent.includes('Not steerable'), BM.heat.textContent.includes('Not steerable'), (BM.heat.querySelector('canvas') || document.body).getAttribute('aria-label').includes('Not steerable'), Array.from(BM.steer.querySelectorAll('.tbadge-text')).map(t => t.textContent).join('')]; }
          return out; })()""")
        note_txt = "Not steerable: 50-250 Hz is identical on all beams (the array cannot steer below 250 Hz), so differences between beams in this band are channel gain or noise, not direction."
        check(
            "not steerable: on 50-125 and 125-250 Hz the note is shown, the bias bars are hatched with the note, all 8 contrast rows say (not steerable), the compass and the heatmap (text and canvas aria-label) carry the note",
            isinstance(r, dict)
            and r["0"] == [note_txt, 1, 8, 1, True, True, True, "Not steerable"]
            and r["1"] == [note_txt, 1, 8, 1, True, True, True, "Not steerable"],
            str(r),
        )
        check(
            "not steerable: positive twin - on 250-500 Hz and 2-4 kHz none of it appears",
            isinstance(r, dict) and r["2"] == ["", 0, 0, 0, False, False, False, ""] and r["5"] == ["", 0, 0, 0, False, False, False, ""],
            str(r),
        )

        br.close()
        srv.shutdown()
    finally:
        for b in browsers:
            b.close()
        shutil.rmtree(root, ignore_errors=True)
        for p in edge_profiles:
            shutil.rmtree(p, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
