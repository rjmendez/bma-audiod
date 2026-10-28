#!/usr/bin/env python3
"""Browser test (real headless Microsoft Edge, the real bma_ui server with a login over a synthetic pipeline dir) of the Review tab:
log in, load the tab, press real keys, see the label lines appended to <pipeline>/labels/labels.jsonl and the card move on.

    python test_dom_review.py          (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' if Edge is missing)"""

import json, os, shutil, struct, sys, tempfile, threading, time, wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402
import auth as A  # noqa: E402
import test_dom_edge as D  # noqa: E402
import test_review as R  # noqa: E402

check, text_of, Browser, EDGE = D.check, D.text_of, D.Browser, D.EDGE
T_TIT = R.NOW - 2200
EID_TIT = R.L.make_event_id(T_TIT, "baeolophus bicolor")


def tree(root):
    pr = R.make_tree(root)
    with open(os.path.join(pr, "analysis", "birds.jsonl"), "ab") as f:
        f.write((json.dumps(R.brow(T_TIT, "Tufted Titmouse", "Baeolophus bicolor", 0.55, 2), separators=(",", ":")) + "\n").encode())
    with open(os.path.join(pr, "analysis", "tags.jsonl"), "ab") as f:
        f.write((json.dumps(R.trow(T_TIT), separators=(",", ":")) + "\n").encode())
    return pr


def keypress(br, key):
    code = "Digit" + key if key.isdigit() else ("Key" + key.upper() if len(key) == 1 else key)
    vk = ord(key.upper()) if len(key) == 1 else 27
    for typ in ("keyDown", "keyUp"):
        ev = {"type": typ, "key": key, "code": code, "windowsVirtualKeyCode": vk}
        if typ == "keyDown" and len(key) == 1:
            ev["text"] = key
        br.ws.call("Input.dispatchKeyEvent", ev)


def wait(br, expr, secs=20):
    end = time.time() + secs
    v = None
    while time.time() < end:
        v = br.eval(expr)
        if D.truthy(v):
            return v
        time.sleep(0.3)
    return v


def lines(rig):
    return rig.lines()


def main():
    if not os.path.exists(EDGE):
        print("SKIP msedge not found")
        return 77
    rig = R.Rig(tree=tree, allow_no_clip=True)  # legacy flow: no-clip events are in the queue only with the flag
    prof = os.path.join(tempfile.gettempdir(), "bma_ui_domrv_edge_%d_%d" % (os.getpid(), int(time.time())))
    br = None
    try:
        br = Browser(prof, 9600 + rig.port % 300)
        base = "http://127.0.0.1:%d/" % rig.port
        br.dump(base + "?token=" + R.TOKEN + "#/review", wait=2.0)
        first = wait(br, "document.querySelector('#tab-review .rv-facts td.rv-species').textContent")
        check(
            "Review tab: the first card is the Tufted Titmouse (rarest-first queue order), with its scientific name",
            first == "Tufted Titmouse / Baeolophus bicolor",
            str(first),
        )
        facts = br.eval(
            "Object.fromEntries([...document.querySelectorAll('#tab-review .rv-facts tr')].map(r => [r.children[0].textContent, r.children[1].textContent]))"
        )
        check(
            "card facts are exact",
            facts
            == {
                "Species": "Tufted Titmouse / Baeolophus bicolor",
                "Conf": "0.55",
                "Beams": "2 (best 2)",
                "Time UTC": R.RV.iso_z(T_TIT),
                "In region": "yes",
                "Group": "bird",
                "Non-species": "no",
                "Queue": "near",
                "Species labels": "0/20",
            },
            str(facts),
        )
        check(
            "no clip: the card says 'no clip', has no image and no audio player",
            br.eval("document.querySelectorAll('#tab-review img.rv-img, #tab-review audio').length") == 0
            and "no clip" in text_of(br.eval("document.querySelector('#tab-review .rv-card').outerHTML")),
        )
        check(
            "four verdict buttons 1-4 in order, plus 's skip', each verdict with a tooltip bubble",
            br.eval("[...document.querySelectorAll('#tab-review .rv-verdict')].map(b => b.textContent).join('|')") == "1 correct|2 wrong|3 unsure|4 artifact"
            and br.eval("document.querySelector('#tab-review .rv-skip').textContent") == "s skip"
            and br.eval("document.querySelectorAll('#tab-review .rv-v .tip-bubble').length") == 4,
        )
        check(
            "progress line: labelled 0, species >= 20: 0, queue 3",
            br.eval("document.querySelector('#tab-review .rv-count').textContent") == "labelled 0 | species >= 20: 0 | queue 3",
        )

        # keys are ignored outside the Review tab
        br.eval("location.hash = '#/overview'; 1")
        time.sleep(1.0)
        keypress(br, "1")
        time.sleep(1.5)
        check("pressing 1 on another tab appends nothing (no labels file)", R.raw(rig) is None, str(R.raw(rig)))
        br.eval("location.hash = '#/review'; 1")
        wait(br, "document.querySelector('#tab-review .rv-facts') && !document.getElementById('tab-review').hidden")

        # s = skip: next card, no record
        keypress(br, "s")
        nxt = wait(br, "(document.querySelector('#tab-review .rv-facts td.rv-species') || {}).textContent === 'Blue Jay / Cyanocitta cristata'")
        check("s: the card moves to the Blue Jay and no record is written", bool(nxt) and R.raw(rig) is None, str(R.raw(rig)))
        check("the Blue Jay card shows its artifact flags", br.eval("document.querySelector('#tab-review .rv-artifact').textContent") == "none temp_unknown")

        # 2 opens the reason row; Escape cancels it
        keypress(br, "2")
        wait(br, "document.querySelectorAll('#tab-review .rv-reason').length === 5")
        check(
            "2: five reason buttons 1-5 replace the verdict buttons",
            br.eval("[...document.querySelectorAll('#tab-review .rv-reason')].map(b => b.dataset.reason).join()")
            == "other_species,non_bird_sound,mechanical,insect_or_amphibian,no_sound_seen"
            and br.eval("document.querySelectorAll('#tab-review .rv-verdict').length") == 0,
        )
        keypress(br, "Escape")
        check(
            "Escape: back to the four verdict buttons, nothing written",
            wait(br, "document.querySelectorAll('#tab-review .rv-verdict').length === 4") is True and R.raw(rig) is None,
        )

        # 2 then 3 = wrong / mechanical
        keypress(br, "2")
        wait(br, "document.querySelectorAll('#tab-review .rv-reason').length === 5")
        keypress(br, "3")
        got = wait(br, "(document.querySelector('#tab-review .rv-facts td.rv-species') || {}).textContent === 'Carolina Chickadee / Poecile carolinensis'")
        ls = lines(rig)
        check(
            "2 then 3: exactly one record, the Blue Jay as wrong / mechanical from the ui; the card moves to the Chickadee",
            bool(got)
            and len(ls) == 1
            and (ls[0]["event_id"], ls[0]["verdict"], ls[0]["wrong_reason"], ls[0]["source"], ls[0]["basis"])
            == (R.EID["jay"], "wrong", "mechanical", "ui", "metadata_only"),
            str(ls),
        )
        check(
            "the Chickadee card has its spectrogram image, loaded through the authenticated route (1x1 px test PNG)",
            wait(
                br,
                "(() => { const i = document.querySelector('#tab-review img.rv-img'); return !!i && i.complete && i.naturalWidth === 1 && i.getAttribute('src') === '/api/review/clip?id="
                + R.CLIP_ID
                + "'; })()",
            )
            is True,
        )
        check("the Chickadee card lists beams 1,2", br.eval("document.querySelector('#tab-review .rv-beams').textContent") == "1,2 (best 1)")

        # 1 = correct
        keypress(br, "1")
        empty = wait(
            br,
            "!!document.querySelector('#tab-review .rv-card .empty') && document.querySelector('#tab-review .rv-card .empty').textContent === 'queue empty: 0 candidates without clip'",
        )
        ls = lines(rig)
        check(
            "1: the Chickadee is appended as correct / clip_visual; the card says 'queue empty: 0 candidates without clip'",
            bool(empty)
            and len(ls) == 2
            and (ls[1]["event_id"], ls[1]["verdict"], ls[1]["wrong_reason"], ls[1]["basis"]) == (R.EID["chick"], "correct", None, "clip_visual"),
            str(ls),
        )
        check("the skipped Titmouse was never labelled", all(x["event_id"] != EID_TIT for x in ls))
        wait(br, "document.querySelector('#tab-review .rv-count').textContent.startsWith('labelled 2')")
        check(
            "progress line: labelled 2, queue 0",
            br.eval("document.querySelector('#tab-review .rv-count').textContent") == "labelled 2 | species >= 20: 0 | queue 0",
        )
        rows = br.eval(
            "[...document.querySelectorAll('#tab-review .rv-species tbody tr')].map(r => [r.children[0].textContent, r.children[1].textContent, r.querySelector('progress').value, r.querySelector('progress').max])"
        )
        check(
            "per-species table: both species with 1 label and a bar at 1 of 20",
            rows == [["cyanocitta cristata", "1", 1, 20], ["poecile carolinensis", "1", 1, 20]],
            str(rows),
        )

        # an error state: the label store becomes unreadable
        lp = rig.labels
        os.remove(lp)
        os.makedirs(lp)
        rig.app.review.cache = None  # the queue cache lives 30 s: drop it so the next poll reads the broken store
        wait(br, "document.querySelector('#tab-review .rv-err') && !document.querySelector('#tab-review .rv-err').hidden", 20)
        err = br.eval("document.querySelector('#tab-review .rv-err').textContent")
        check(
            "unreadable store: the tab shows 'label store unavailable: <cause>'", isinstance(err, str) and err.startswith("label store unavailable: "), str(err)
        )
        shutil.rmtree(lp)

        # phone width: every button inside the viewport
        br.resize(390, 800)
        time.sleep(1.0)
        br.eval("location.hash = '#/overview'; 1")
        time.sleep(0.5)
        br.eval("location.hash = '#/review'; 1")
        time.sleep(1.0)
        check(
            "phone width 390: the empty-queue card and the species table fit the viewport (no horizontal overflow of the tab)",
            br.eval("document.getElementById('tab-review').scrollWidth <= document.getElementById('tab-review').clientWidth + 1") is True,
        )
    finally:
        if br:
            br.close()
        rig.close()
        shutil.rmtree(prof, ignore_errors=True)
    print(f"\n{D.n_checks} checks, {len(D.fails)} failure(s)")
    return 1 if D.fails else 0


def wav_tree(root):
    """evidence_tree with a REAL 2 s silent wav for the titmouse (Edge must decode it), served under the clip's id."""
    pr = R.evidence_tree(root)
    os.remove(os.path.join(R.day_dir(pr, R.TIT_ID), R.TIT_ID + ".flac"))
    with wave.open(os.path.join(R.day_dir(pr, R.TIT_ID), R.TIT_ID + ".wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * 32000)
    return pr


def evidence_flow():
    rig = R.Rig(tree=wav_tree)  # default: evidence only
    prof = os.path.join(tempfile.gettempdir(), "bma_ui_domrv2_edge_%d_%d" % (os.getpid(), int(time.time())))
    br = None
    try:
        br = Browser(prof, 9600 + (rig.port + 7) % 300)
        br.dump("http://127.0.0.1:%d/?token=%s#/review" % (rig.port, R.TOKEN), wait=2.0)
        who = "(document.querySelector('#tab-review .rv-facts td.rv-species') || {}).textContent"

        def goto(name):
            for _ in range(5):
                if wait(br, who + " && " + who + ".startsWith('%s')" % name, 8):
                    return True
                keypress(br, "s")
            return False

        check(
            "evidence queue: progress line says queue 3 (no Blue Jay: it has no clip)",
            wait(br, "document.querySelector('#tab-review .rv-count').textContent === 'labelled 0 | species >= 20: 0 | queue 3'") is True,
            str(br.eval("document.querySelector('#tab-review .rv-count').textContent")),
        )
        # ---- an item with audio and a PNG
        check("navigate to the Titmouse card (audio + PNG)", goto("Tufted Titmouse"), str(br.eval(who)))
        au = br.eval(
            "(() => { const a = document.querySelector('#tab-review audio'); return a && [a.getAttribute('src'), a.controls, a.preload, a.tagName, document.querySelectorAll('#tab-review audio').length]; })()"
        )
        check(
            "Titmouse: exactly one <audio controls preload=none> with src /api/review/audio?id=<clip id>",
            au == ["/api/review/audio?id=" + R.TIT_ID, True, "none", "AUDIO", 1],
            str(au),
        )
        img = wait(
            br,
            "(() => { const i = document.querySelector('#tab-review img.rv-img'); return !!i && i.complete && i.naturalWidth === 1 && i.getAttribute('src') === '/api/review/clip?id="
            + R.TIT_ID
            + "'; })()",
        )
        check("Titmouse: the spectrogram image is shown (src = clip route, loaded)", img is True)
        rv = rig.app.review
        item = rv._item_json(rv._load(), rv._load()["items"][R.EID_TIT])

        def span_expect(it):
            cs = it["clip_span"]
            d = cs["clip_end_s"] - cs["clip_start_s"]
            lo, hi = (cs["event_start_s"] - cs["clip_start_s"]) / d, (cs["event_end_s"] - cs["clip_start_s"]) / d
            return "%g%%" % round(lo * 100, 2), "%g%%" % round(max(hi - lo, 0.01) * 100, 2), d

        left, width, dur = span_expect(item)
        got = br.eval("(() => { const e = document.querySelector('#tab-review .rv-span'); return e && [e.style.left, e.style.width]; })()")
        check(
            "Titmouse (one report, zero length): the event is a 1%% marker at the exact fraction of the clip (left %s, width %s)" % (left, width),
            got == [left, width] and width == "1%",
            str((got, item["clip_span"])),
        )
        ticks = br.eval("[...document.querySelectorAll('#tab-review .rv-tick')].map(t => t.textContent)")
        check("Titmouse: time axis 0s..5s for a 5 s clip", ticks == ["0s", "1s", "2s", "3s", "4s", "5s"], str((ticks, dur)))
        # ---- keys: space play/pause, r replay; playback survives a re-render
        keypress(br, " ")
        playing = wait(br, "!document.querySelector('#tab-review audio').paused", 8)
        check(
            "space starts playback",
            playing is True,
            str(
                br.eval(
                    "[document.querySelector('#tab-review audio').paused, document.querySelector('#tab-review audio').error && document.querySelector('#tab-review audio').error.code]"
                )
            ),
        )
        keypress(br, "2")
        wait(br, "document.querySelectorAll('#tab-review .rv-reason').length === 5")
        still = br.eval("document.querySelector('#tab-review audio') && !document.querySelector('#tab-review audio').paused")
        check(
            "opening the reason row (a re-render) does not stop the playback, and the audio element is the same single element",
            still is True and br.eval("document.querySelectorAll('#tab-review audio').length") == 1,
        )
        keypress(br, "Escape")
        wait(br, "document.querySelectorAll('#tab-review .rv-verdict').length === 4")
        time.sleep(0.6)
        keypress(br, " ")
        check("space again pauses", wait(br, "document.querySelector('#tab-review audio').paused", 8) is True)
        t_pause = br.eval("document.querySelector('#tab-review audio').currentTime")
        keypress(br, "r")
        replay = wait(br, "!document.querySelector('#tab-review audio').paused", 8)
        t_new = br.eval("document.querySelector('#tab-review audio').currentTime")
        check(
            "r replays from the start: playing again and the position is before the paused position (%.2f -> %.2f)" % (t_pause, t_new),
            replay is True and t_pause > 0.3 and t_new < t_pause,
            str((t_pause, t_new)),
        )
        keypress(br, " ")
        wait(br, "document.querySelector('#tab-review audio').paused")
        # ---- an item with audio and NO png
        check("navigate to the Woodpecker card (audio only)", goto("Downy Woodpecker"), str(br.eval(who)))
        w = br.eval(
            "[document.querySelectorAll('#tab-review audio').length, document.querySelectorAll('#tab-review img.rv-img').length, document.querySelectorAll('#tab-review .rv-noaudio').length, (document.querySelector('#tab-review audio') || {getAttribute: () => null}).getAttribute('src')]"
        )
        check(
            "Woodpecker (audio, no PNG): the player is there with its src, no image, no 'no audio' note",
            w == [1, 0, 0, "/api/review/audio?id=" + R.WOOD_ID],
            str(w),
        )
        # ---- a PNG-only item: no player, 'no audio'
        check("navigate to the Chickadee card (PNG only)", goto("Carolina Chickadee"), str(br.eval(who)))
        c = br.eval(
            "[document.querySelectorAll('#tab-review audio').length, document.querySelectorAll('#tab-review img.rv-img').length, (document.querySelector('#tab-review .rv-noaudio') || {}).textContent]"
        )
        citem = rv._item_json(rv._load(), rv._load()["items"][R.EID["chick"]])
        cl, cw, _d = span_expect(citem)
        cgot = br.eval("(() => { const e = document.querySelector('#tab-review .rv-span'); return e && [e.style.left, e.style.width]; })()")
        check(
            "Chickadee (two reports, 1 s event in a 5 s clip): span left %s width %s" % (cl, cw),
            cgot == [cl, cw] and cw == "20%",
            str((cgot, citem["clip_span"])),
        )
        check("Chickadee (PNG only): no <audio> element, the spectrogram shown, the card says exactly 'no audio'", c == [0, 1, "no audio"], str(c))
        # ---- keys with no player do nothing, no error
        keypress(br, " ")
        keypress(br, "r")
        time.sleep(0.5)
        check(
            "space and r on an item without audio change nothing (same card, no record, no error text)",
            br.eval(who).startswith("Carolina Chickadee") and R.raw(rig) is None and br.eval("document.querySelector('#tab-review .rv-err').hidden") is True,
        )
        # ---- empty queue says why
        for _ in range(3):
            keypress(br, "s")
            time.sleep(0.4)
        empty = wait(
            br,
            "!!document.querySelector('#tab-review .rv-card .empty') && document.querySelector('#tab-review .rv-card .empty').textContent === 'queue empty: 1 candidates without clip'",
            10,
        )
        check(
            "empty queue: 'queue empty: 1 candidates without clip' (the Blue Jay)",
            empty is True,
            str(br.eval("document.querySelector('#tab-review .rv-card').textContent")),
        )
    finally:
        if br:
            br.close()
        rig.close()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        evidence_flow()
        print(f"\n{D.n_checks} checks (with evidence flow), {len(D.fails)} failure(s)")
        rc = 1 if D.fails else 0
    sys.exit(rc)
