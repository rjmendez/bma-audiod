#!/usr/bin/env python3
"""DOM-level checks (real headless Microsoft Edge, the real bma_ui server over synthetic pipeline files) of the advisory artifact flags: the "possible
artifact" badge and its tooltip on cold-night insect events, no badge on a bird or on a warm-night insect (positive twin), the species-list toggle
"hide flagged insect detections" OFF by default with the flagged count always shown, the toggle leaving the flagged events out and bringing them back,
the System-panel note, and the human-voice labels staying hidden.

    python test_dom_artifact.py          (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' if Edge is missing)
Env BMA_UI_DIR selects the ui directory under test; BMA_DOM_WAIT shortens the page-load wait."""

import json, os, shutil, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.environ.get("BMA_UI_DIR", HERE)
sys.path.insert(0, UI_DIR)
sys.path.insert(1, HERE)
import bma_ui as U  # noqa: E402
import test_bma_ui as T  # noqa: E402
import test_dom_edge as D  # noqa: E402

check, Browser, EDGE = D.check, D.Browser, D.EDGE


def serve(root, name, now, temp):
    pr, rp = os.path.join(root, name + "_pipeline"), os.path.join(root, name + "_reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), os.path.join(pr, "history"), rp):
        os.makedirs(d)
    T.jl(os.path.join(rp, "beam_bands.jsonl"), [T.bands_rec(now - i, None) for i in range(60, 0, -1)])
    T.jl(
        os.path.join(pr, "analysis", "birds.jsonl"),
        [T.bird(now - 900 + 100 * i, 1 + i, 0.8, "Common Virtuoso Katydid", "Amblycorypha longinicta") for i in range(3)]
        + [T.bird(now - 500, 2, 0.8, "Alpha Wren", "Aus a"), T.bird(now - 400, 3, 0.9, "Human vocal", "Homo sapiens")],
    )
    hour = int(now // 3600) * 3600
    T.jl(
        os.path.join(pr, "history", "ambient_temp.jsonl"),
        [{"k": "ambient_temp", "v": 1, "t_hour_utc": h, "temp_c": temp, "source": "open-meteo", "model_grid_km": None} for h in (hour - 3600, hour)],
    )
    app = U.App(
        pr,
        rp,
        os.path.join(root, name + "_rollups"),
        U.Board("http://127.0.0.1:9", os.path.join(root, "none"), enabled=False),
        window_h=48,
        slot_fix_at=int(now - 40 * 86400),
    )
    app.start_backfill()
    threading.Thread(target=app.run_loop, daemon=True).start()
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets())
    srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], [])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


ROWS = "Array.from(document.querySelectorAll('#tab-detections .tablewrap')[0].querySelectorAll('tbody tr')).map(r => [r.children[1].firstChild.textContent, r.querySelectorAll('.tbadge').length])"


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    root = tempfile.mkdtemp(prefix="bma_ui_doma_")
    browsers, profiles, servers = [], [], []
    try:
        now = time.time()
        prof = os.path.join(tempfile.gettempdir(), "bma_ui_doma_edge_%d_%d" % (os.getpid(), int(time.time())))
        profiles.append(prof)
        br = Browser(prof, 9400 + int(now) % 500)
        browsers.append(br)
        cold, base_c = serve(root, "cold", now, 2.0)
        warm, base_w = serve(root, "warm", now, 14.0)
        servers += [cold, warm]
        time.sleep(2.5)  # the server polls once a second
        # ------------------------------------------------------------------ cold night: the three katydid events are flagged, the wren is not
        br.dump(base_c + "#/detections")
        rows = br.eval(ROWS)
        check(
            "cold night: the three katydid events carry exactly one 'possible artifact' badge each, the wren row none (a bird has no flag)",
            sorted(rows) == sorted([["Common Virtuoso Katydid", 1]] * 3 + [["Alpha Wren", 0]]),
            str(rows),
        )
        tip = br.eval(
            "(() => { const b = document.querySelector('#tab-detections .tablewrap tbody .tbadge'); return [b.querySelector('.tbadge-text').textContent, b.querySelector('.tip-bubble').textContent]; })()"
        )
        check(
            "the badge reads 'possible artifact' and its tooltip explains the cold rule and says the detection is kept",
            tip
            == [
                "possible artifact",
                "Cold: the ambient temperature was 2.0 °C (this hour), below the 5 °C floor. Insects are not expected to call that cold, so this label may be a noise the model matched to its nearest label. Advisory only: the detection is kept and counted.",
            ],
            str(tip),
        )
        tg = br.eval(
            "(() => { const t = document.querySelector('[data-artifact-toggle]'); return [t.type, t.checked, document.getElementById('artifact-count').textContent]; })()"
        )
        check(
            "the 'hide flagged insect detections' toggle exists, is OFF by default, and the flagged events are counted next to it",
            tg == ["checkbox", False, "3 insect events flagged as possible artifacts (still counted)"],
            str(tg),
        )
        sp = br.eval(
            "(() => { const t = Array.from(document.querySelectorAll('#tab-detections .tablewrap table')).find(x => x.textContent.includes('Last seen')); return Array.from(t.querySelectorAll('tbody tr')).map(r => [r.children[0].firstChild.textContent, r.querySelector('.tbadge .tbadge-text') ? r.querySelector('.tbadge .tbadge-text').textContent : null]); })()"
        )
        check(
            "the species summary badges the katydid row '3 of 3 possible artifact' and not the wren",
            sorted(map(tuple, sp)) == sorted([("Common Virtuoso Katydid", "3 of 3 possible artifact"), ("Alpha Wren", None)]),
            str(sp),
        )
        check(
            "human-voice labels stay hidden: the page text never mentions one",
            br.eval("document.body.textContent.includes('Human') || document.body.textContent.includes('Homo')") is False,
        )
        # ------------------------------------------------------------------ the toggle leaves the flagged events out and brings them back
        off = br.eval("""(async () => { const sleep = ms => new Promise(r => setTimeout(r, ms)); document.querySelector('[data-artifact-toggle]').click(); await sleep(1500);
          return [document.querySelector('[data-artifact-toggle]').checked, document.getElementById('artifact-count').textContent, document.querySelectorAll('#tab-detections .tablewrap')[0].querySelectorAll('tbody tr').length]; })()""")
        check(
            "toggle ON: the flagged events leave the list (1 row left) and the count says 3 hidden",
            off == [True, "3 flagged insect events hidden", 1],
            str(off),
        )
        back = br.eval("""(async () => { const sleep = ms => new Promise(r => setTimeout(r, ms)); document.querySelector('[data-artifact-toggle]').click(); await sleep(1500);
          return [document.querySelector('[data-artifact-toggle]').checked, document.getElementById('artifact-count').textContent, document.querySelectorAll('#tab-detections .tablewrap')[0].querySelectorAll('tbody tr').length]; })()""")
        check(
            "toggle OFF again: all 4 rows are back and the count is the 'still counted' text (nothing was deleted)",
            back == [False, "3 insect events flagged as possible artifacts (still counted)", 4],
            str(back),
        )
        # ------------------------------------------------------------------ the System panel note
        br.dump(base_c + "#/system")
        note = br.eval("(() => { const n = document.querySelector('#artifact-gate-note .tip-bubble'); return n ? n.textContent : null; })()")
        check(
            "System panel: the honest note names the rule, the 2 temperature records, the 0 transients and the 3 of 3 flagged tally and says it is a heuristic",
            isinstance(note, str)
            and note.startswith(
                "Artifact flags (advisory): an insect detection is flagged as a possible artifact when the ambient air temperature was below 5 °C"
            )
            and "Temperature: 2 hourly temperature records" in note
            and "no noise transients read, so there is no burst evidence" in note
            and "Last 24 h: 3 of 3 insect and mechanical events flagged." in note
            and note.endswith("an unflagged one can still be an artifact."),
            str(note),
        )
        # ------------------------------------------------------------------ warm night: the same katydids carry no badge (positive twin)
        br.dump(base_w + "#/detections")
        rows = br.eval(ROWS)
        tg = br.eval("document.getElementById('artifact-count').textContent")
        check(
            "warm night (14.0 C), positive twin: the same four rows carry NO badge and the count is 0 flagged",
            sorted(rows) == sorted([["Common Virtuoso Katydid", 0]] * 3 + [["Alpha Wren", 0]])
            and tg == "0 insect events flagged as possible artifacts (still counted)",
            str((rows, tg)),
        )
    finally:
        for b in browsers:
            b.close()
        for s in servers:
            s.shutdown()
        for p in profiles:
            shutil.rmtree(p, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)
    print("\n%d checks, %d failure(s)" % (D.n_checks, len(D.fails)))
    return 1 if D.fails else 0


if __name__ == "__main__":
    sys.exit(main())
