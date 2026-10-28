#!/usr/bin/env python3
"""Tests for the Review API (ui-http 1.9.0): real server on an ephemeral port, real HTTP, a synthetic pipeline tree in a temp dir.

python test_review.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 1 on failure)"""

import base64, http.client, json, os, shutil, struct, subprocess, sys, tempfile, threading, time, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "analysis"))
import auth as A  # noqa: E402
import bma_ui as U  # noqa: E402
import labels as L  # noqa: E402
import review as RV  # noqa: E402

fails, n_checks = [], 0
TOKEN = "T0ken-" + "a1b2c3d4e5f6" * 4
BASIC = {"Authorization": "Basic " + base64.b64encode(("u:" + TOKEN).encode()).decode()}
CLI = os.path.join(HERE, "..", "tools", "bma_labels.py")
PY = sys.executable
NOW = time.time()


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def png_bytes():
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\x7f")) + chunk(b"IEND", b"")
    )


PNG = png_bytes()


def brow(t, common, sci, conf=0.7, beam=1, **extra):
    return dict(
        {
            "k": "bird",
            "t": t,
            "beam": beam,
            "level_dbfs": -60.0,
            "common": common,
            "scientific": sci,
            "confidence": conf,
            "provenance": "model_opinion",
            "in_region": True,
            "non_species": False,
        },
        **extra,
    )


def trow(t, p=0.0, active=False):
    return {"k": "tags", "t": t, "beam": 1, "speech_p": p, "speech_active": active, "top": [["Bird", 0.5]], "emb_stored": True}


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for r in rows:
            f.write((json.dumps(r, separators=(",", ":")) + "\n").encode())


# the synthetic world: times are seconds before NOW, 200 s apart so no two events merge and no tag window of one covers another
T_JAY, T_CHICK, T_HUMAN, T_TAGSPEECH, T_UNTAGGED = NOW - 1000, NOW - 1200, NOW - 1400, NOW - 1600, NOW - 1800
EID = {
    k: L.make_event_id(t, s)
    for k, t, s in (
        ("jay", T_JAY, "cyanocitta cristata"),
        ("chick", T_CHICK, "poecile carolinensis"),
        ("human", T_HUMAN, "human vocal"),
        ("tagspeech", T_TAGSPEECH, "cardinalis cardinalis"),
        ("untagged", T_UNTAGGED, "sitta carolinensis"),
    )
}
CLIP_ID = "%d-poecilecarolinensis-b1" % int((T_CHICK - 1) * 1000)
ART = {"v": 1, "flags": ["temp_unknown"], "level": "none", "evidence": {"temp_c": None}}


def make_tree(root):
    pr = os.path.join(root, "p")
    write_jsonl(
        os.path.join(pr, "analysis", "birds.jsonl"),
        [
            brow(T_JAY, "Blue Jay", "Cyanocitta cristata", 0.6, 3, artifact_flags=ART),
            brow(T_CHICK, "Carolina Chickadee", "Poecile carolinensis", 0.7, 1),
            brow(T_CHICK + 1, "Carolina Chickadee", "Poecile carolinensis", 0.6, 2),
            brow(T_HUMAN, "Human vocal", "Human vocal", 0.9, 1),
            brow(T_TAGSPEECH, "Northern Cardinal", "Cardinalis cardinalis", 0.7, 1),
            brow(T_UNTAGGED, "White-breasted Nuthatch", "Sitta carolinensis", 0.7, 1),
        ],
    )
    write_jsonl(os.path.join(pr, "analysis", "tags.jsonl"), [trow(T_JAY), trow(T_CHICK), trow(T_HUMAN), trow(T_TAGSPEECH + 1, 0.9, True)])
    day = time.strftime("%Y%m%d", time.gmtime((T_CHICK - 1)))
    cd = os.path.join(pr, "analysis", "clips", day)
    os.makedirs(cd, exist_ok=True)
    with open(os.path.join(cd, CLIP_ID + ".png"), "wb") as f:
        f.write(PNG)
    write_jsonl(
        os.path.join(pr, "analysis", "clips", "index.jsonl"),
        [
            {
                "k": "clip_stored",
                "v": 1,
                "id": CLIP_ID,
                "t_start": T_CHICK - 1,
                "t_end": T_CHICK + 4,
                "species": {"scientific": "Poecile carolinensis", "common": "Carolina Chickadee"},
                "group": "bird",
                "confidence_max": 0.7,
                "beam": 1,
                "png": day + "/" + CLIP_ID + ".png",
            }
        ],
    )
    with open(os.path.join(root, "secret.png"), "wb") as f:
        f.write(PNG)
    d2 = os.path.join(
        pr, "analysis", "clips", "20261014"
    )  # files a loosened id check would reach: a bad name in a day dir, a file above it via a real directory
    os.makedirs(os.path.join(d2, "1791997838000-d"), exist_ok=True)
    for pth in (os.path.join(d2, "1791997838000-evil.png"), os.path.join(pr, "analysis", "clips", "secret2.png")):
        with open(pth, "wb") as f:
            f.write(PNG)
    return pr


class Rig:
    def __init__(self, auth=True, labels_dir=None, tree=make_tree, allow_no_clip=False):
        self.root = tempfile.mkdtemp(prefix="bma_rv_")
        self.logs = []
        pr = tree(self.root)
        self.pr = pr
        rp = os.path.join(self.root, "r")
        os.makedirs(rp, exist_ok=True)
        board = U.Board("http://127.0.0.1:9", os.path.join(self.root, "nt"), enabled=False)
        self.app = U.App(
            pr, rp, os.path.join(self.root, "ru"), board, window_h=48, log=self.logs.append, labels_dir=labels_dir, review_allow_no_clip=allow_no_clip
        )
        self.srv = U.Server(("127.0.0.1", 0), self.app, set(), U.load_assets(), auth=A.Authenticator(TOKEN, log=self.logs.append) if auth else None)
        self.port = self.srv.server_address[1]
        self.srv.allowed_hosts = U.allowed_hosts_for(self.port, [])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.labels = self.app.review.store.path

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, auth=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        h = {"Host": "localhost:%d" % self.port}
        if auth:
            h.update(BASIC)
        h.update(headers or {})
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hd, data

    def j(self, *a, **k):
        s, h, b = self.req(*a, **k)
        try:
            return s, json.loads(b)
        except ValueError:
            return s, b

    def csrf(self):
        return self.j("GET", "/api/session")[1]["csrf"]

    def post(self, path, body, csrf=True, headers=None):
        b = dict(body)
        if csrf is True:
            b["csrf"] = self.csrf()
        elif csrf:
            b["csrf"] = csrf
        return self.j("POST", path, b, headers)

    def lines(self):
        try:
            return [json.loads(x) for x in open(self.labels, "rb").read().splitlines() if x.strip()]
        except FileNotFoundError:
            return []


def raw(rig):
    try:
        return open(rig.labels, "rb").read()
    except FileNotFoundError:
        return None


def run():
    r = Rig(allow_no_clip=True)  # legacy flow: the Blue Jay has no clip, so it needs --review-allow-no-clip
    try:
        # ------------------------------------------------------------------------------------ authentication
        codes = [
            r.req(m, p, auth=False)[0]
            for m, p in (
                ("GET", "/api/review/next"),
                ("GET", "/api/review/stats"),
                ("GET", "/api/review/clip?id=" + CLIP_ID),
                ("POST", "/api/review/verdict"),
                ("POST", "/api/review/skip"),
            )
        ]
        check("no credential: all five review routes answer 401", codes == [401] * 5, str(codes))
        check(
            "positive twin: the same GET routes with the credential answer 200",
            [r.req("GET", p)[0] for p in ("/api/review/next", "/api/review/stats", "/api/review/clip?id=" + CLIP_ID)] == [200] * 3,
        )
        s, h, b = r.req("GET", "/api/review/bogus")
        check("an unknown review path is 404 not_found", (s, json.loads(b)["code"]) == (404, "not_found"), str((s, b)))
        s, h, b = r.req("POST", "/api/review/next", {"csrf": r.csrf()})
        check("POST on a GET route is 405 with Allow: GET, HEAD", (s, h.get("allow")) == (405, "GET, HEAD"), str((s, h)))
        s, h, b = r.req("GET", "/api/review/verdict")
        check("GET on a POST route is 405 with Allow: POST", (s, h.get("allow")) == (405, "POST"), str((s, h)))

        # ------------------------------------------------------------------------------------ next, stats
        s, nx = r.j("GET", "/api/review/next")
        jay = {
            "event_id": EID["jay"],
            "species": {"common": "Blue Jay", "scientific": "Cyanocitta cristata"},
            "confidence": 0.6,
            "beams": [3],
            "best_beam": 3,
            "t_start_s": round(T_JAY, 3),
            "t_utc": RV.iso_z(T_JAY),
            "in_region": True,
            "group": "bird",
            "non_species": False,
            "reason": "near",
            "artifact_flags": {"flags": ["temp_unknown"], "level": "none"},
        }
        check(
            "GET next: the first item is the Blue Jay with exactly these fields (artifact flags present, no clip), depth 2",
            (s, nx) == (200, {"item": jay, "queue_depth": 2}),
            str((s, nx)),
        )
        check("GET next never offers the Human vocal, the tag-speech or the untagged event (depth 2 = jay + chickadee)", nx["queue_depth"] == 2)
        s, st = r.j("GET", "/api/review/stats")
        check(
            "GET stats on an empty store: exact",
            (s, st)
            == (
                200,
                {
                    "labelled": 0,
                    "verdicts": {"correct": 0, "wrong": 0, "unsure": 0, "artifact": 0},
                    "target": 20,
                    "species_at_target": 0,
                    "queue_depth": 2,
                    "no_clip": 1,
                    "species": [],
                },
            ),
            str((s, st)),
        )
        try:
            import jsonschema

            sch = json.load(open(os.path.join(HERE, "..", "contracts", "schemas", "ui_http.schema.json")))
            ok = True
            for name, doc in (("review_next", nx), ("review_stats", st)):
                ok = ok and not list(
                    jsonschema.Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/" + name, "$defs": sch["$defs"]}).iter_errors(doc)
                )
            check("next and stats validate against the ui-http schema", ok)
        except ImportError:
            print("SKIP schema validation: jsonschema is not installed")

        # ------------------------------------------------------------------------------------ CSRF and origin
        body = {"event_id": EID["jay"], "verdict": "correct"}
        res = [
            r.post("/api/review/verdict", body, csrf=False)[0],
            r.post("/api/review/verdict", body, csrf="0" * 64)[0],
            r.post("/api/review/verdict", body, csrf=r.csrf(), headers={"Sec-Fetch-Site": "cross-site"})[0],
            r.post("/api/review/verdict", body, csrf=r.csrf(), headers={"Origin": "http://evil.example"})[0],
            r.post("/api/review/skip", {"event_id": EID["jay"]}, csrf=False)[0],
        ]
        check(
            "verdict without CSRF, with a wrong CSRF, cross-site, foreign Origin, and skip without CSRF: all 403, nothing written",
            res == [403] * 5 and raw(r) is None,
            str((res, raw(r))),
        )
        s, pb = r.post("/api/review/verdict", body, csrf=False)
        check("the CSRF refusal is the csrf_failed problem", pb["code"] == "csrf_failed", str(pb))
        s, pb = r.j("POST", "/api/review/verdict", b"{not json", {"Content-Type": "application/json"})
        check("a malformed JSON body is 400 invalid_request", (s, pb["code"]) == (400, "invalid_request"), str((s, pb)))
        check(
            "positive twin: the CSRF in the X-CSRF-Token header is accepted (skip, no record)",
            r.j("POST", "/api/review/skip", {"event_id": "1-x"}, {"X-CSRF-Token": r.csrf()})[0] == 200 and raw(r) is None,
        )

        # ------------------------------------------------------------------------------------ vocabulary
        res = [
            r.post("/api/review/verdict", dict(body, verdict="maybe")),
            r.post("/api/review/verdict", dict(body, verdict="wrong")),
            r.post("/api/review/verdict", dict(body, verdict="wrong", wrong_reason="because")),
            r.post("/api/review/verdict", dict(body, verdict="Correct")),
            r.post("/api/review/verdict", dict(body, event_id="../x")),
        ]
        check(
            "unknown verdict, wrong without reason, unknown reason, wrong case, bad event id: 422 invalid_parameter with the param, nothing written",
            [(s, p["code"], p["param"]) for s, p in res]
            == [
                (422, "invalid_parameter", "verdict"),
                (422, "invalid_parameter", "wrong_reason"),
                (422, "invalid_parameter", "wrong_reason"),
                (422, "invalid_parameter", "verdict"),
                (422, "invalid_parameter", "event_id"),
            ]
            and raw(r) is None,
            str((res, raw(r))),
        )
        s, ok = r.post("/api/review/verdict", dict(body, verdict="wrong", wrong_reason="other_species"))
        rec = r.lines()[0]
        check(
            "positive twin: wrong + other_species is recorded (exact verdict, reason, source, basis, stratum, queue id)",
            s == 200
            and ok["recorded"] is True
            and len(r.lines()) == 1
            and (
                rec["verdict"],
                rec["wrong_reason"],
                rec["corrected_key"],
                rec["source"],
                rec["basis"],
                rec["stratum"],
                rec["event_id"],
                rec["species_key"],
                rec["provenance"],
            )
            == ("wrong", "other_species", None, "ui", "metadata_only", "near", EID["jay"], "cyanocitta cristata", "owner_verdict")
            and rec["model"] == {"conf_max": 0.6, "n_beams": 1, "best_beam": 3, "group": None, "in_region": True}
            and rec["label_id"] == ok["label_id"],
            str((s, ok, rec)),
        )
        check("the written record passes the store's own strict validator", L.validate_record(rec) is rec)
        check(
            "the response carries the next item (the chickadee, with its clip url) and depth 1",
            ok["item"]["event_id"] == EID["chick"]
            and ok["item"]["clip_png"] == "/api/review/clip?id=" + CLIP_ID
            and ok["item"]["reason"] == "near"
            and ok["queue_depth"] == 1
            and "artifact_flags" not in ok["item"]
            and ok["item"]["beams"] == [1, 2],
            str(ok),
        )

        # ------------------------------------------------------------------------------------ idempotency
        s, a = r.post("/api/review/verdict", {"event_id": EID["chick"], "verdict": "correct"})
        s2, b2 = r.post("/api/review/verdict", {"event_id": EID["chick"], "verdict": "correct"})
        ls = r.lines()
        check(
            "verdict twice within the window: second answer recorded false with the SAME label_id, exactly 2 lines in the file (jay + chickadee)",
            (s, s2, a["recorded"], b2["recorded"], a["label_id"] == b2["label_id"], len(ls)) == (200, 200, True, False, True, 2),
            str((a, b2, len(ls))),
        )
        check(
            "the chickadee record has basis clip_visual (a clip PNG exists for the event)",
            ls[1]["basis"] == "clip_visual" and ls[1]["event_id"] == EID["chick"],
            str(ls[1]),
        )
        s, c = r.post("/api/review/verdict", {"event_id": EID["chick"], "verdict": "unsure"})
        ls = r.lines()
        check(
            "positive twin: a different verdict for the same event IS recorded (3 lines, fold keeps the newest)",
            (s, c["recorded"], len(ls), L.fold(ls)[EID["chick"]]["verdict"]) == (200, True, 3, "unsure"),
            str((s, c, len(ls))),
        )
        check("the queue is now empty: item null, depth 0", (c["item"], c["queue_depth"]) == (None, 0), str(c))
        s, st = r.j("GET", "/api/review/stats")
        check(
            "stats after 2 labelled events: exact",
            st
            == {
                "labelled": 2,
                "verdicts": {"correct": 0, "wrong": 1, "unsure": 1, "artifact": 0},
                "target": 20,
                "species_at_target": 0,
                "queue_depth": 0,
                "no_clip": 0,
                "species": [{"species_key": "cyanocitta cristata", "labels": 1}, {"species_key": "poecile carolinensis", "labels": 1}],
            },
            str(st),
        )

        # ------------------------------------------------------------------------------------ speech and unknown events
        before = raw(r)
        res = {k: r.post("/api/review/verdict", {"event_id": EID[k], "verdict": "correct"}) for k in ("human", "tagspeech", "untagged")}
        res["unknown"] = r.post("/api/review/verdict", {"event_id": "1700000000000-nothing", "verdict": "correct"})
        check(
            "Human vocal, tag-speech, untagged and unknown events: 422 not_labelable, detail exactly 'not_labelable' (one code for every cause), file unchanged",
            all((s, p["code"], p["detail"]) == (422, "not_labelable", "not_labelable") for s, p in res.values()) and raw(r) == before,
            str(res),
        )
        check("the refusal never names the Human label", all("uman" not in json.dumps(p) for s, p in res.values()))

        # ------------------------------------------------------------------------------------ skip
    finally:
        r.close()

    r = Rig(allow_no_clip=True)
    try:
        s, a = r.post("/api/review/skip", {"event_id": EID["jay"]})
        check(
            "skip: 200, recorded false, label_id null, the next item is the chickadee, depth 1, NO record written",
            (s, a["recorded"], a["label_id"], a["item"]["event_id"], a["queue_depth"], raw(r)) == (200, False, None, EID["chick"], 1, None),
            str((s, a)),
        )
        s, a = r.post("/api/review/skip", {"event_id": EID["jay"]})
        check("skip is idempotent: the same event twice leaves depth 1", (s, a["queue_depth"]) == (200, 1), str(a))
        s, a = r.post("/api/review/skip", {"event_id": EID["chick"]})
        check("skipping everything: item null, depth 0", (a["item"], a["queue_depth"]) == (None, 0), str(a))
        r2 = Rig(allow_no_clip=True)
        try:
            check("fresh server: first item is the Blue Jay again", r2.j("GET", "/api/review/next")[1]["item"]["event_id"] == EID["jay"])
        finally:
            r2.close()

        # ------------------------------------------------------------------------------------ the clip route
        s, h, b = r.req("GET", "/api/review/clip?id=" + CLIP_ID)
        check(
            "clip: 200 image/png no-store with the exact bytes",
            (s, h["content-type"], h["cache-control"], b) == (200, "image/png", "no-store", PNG),
            str((s, h)),
        )
        s, h, b = r.req("HEAD", "/api/review/clip?id=" + CLIP_ID)
        check("clip HEAD: 200, empty body, the length of the PNG", (s, b, h["content-length"]) == (200, b"", str(len(PNG))), str((s, h)))
        bad = [
            "../../../secret",
            "..%2f..%2fsecret",
            "%2e%2e%2f%2e%2e%2fsecret",
            CLIP_ID + "/../../../../secret",
            "..\\..\\secret",
            "c%3A%5Cwindows%5Cwin.ini",
            "/etc/passwd",
            CLIP_ID.upper(),
            CLIP_ID + "%00",
            CLIP_ID + ".png",
            "1791997838000-x-b9",
            "179199783800-x-b1",
            "1791997838000-" + "a" * 25 + "-b1",
            "1791997838000--b1",
            "",
            "secret",
            "1791997838000-evil",
            "1791997838000-d/../../secret2",
        ]
        got = [r.req("GET", "/api/review/clip?id=" + x)[0] for x in bad]
        check("clip: %d traversal / malformed ids are all 404" % len(bad), got == [404] * len(bad), str(list(zip(bad, got))))
        check(
            "clip: a well-formed id with no file is 404 (positive twin: the real id is 200)",
            r.req("GET", "/api/review/clip?id=1791997838000-nothing-b1")[0] == 404 and r.req("GET", "/api/review/clip?id=" + CLIP_ID)[0] == 200,
        )
        check(
            "clip: no id and an extra parameter are refused (404 / 422)",
            (r.req("GET", "/api/review/clip")[0], r.req("GET", "/api/review/clip?id=%s&x=1" % CLIP_ID)[0]) == (404, 422),
        )
        check(
            "clip_path maps the id to <clips>/<UTC day>/<id>.png exactly",
            r.app.review.clip_path(CLIP_ID) == os.path.join(r.pr, "analysis", "clips", time.strftime("%Y%m%d", time.gmtime(T_CHICK - 1)), CLIP_ID + ".png"),
        )
        check(
            "/api/review/clip never serves audio (a .flac id is 404) and /api/review/audio without an id is 404",
            [r.req("GET", p)[0] for p in ("/api/review/audio", "/api/review/clip?id=" + CLIP_ID + ".flac")] == [404, 404],
        )
    finally:
        r.close()

    # ---------------------------------------------------------------------------------------- an open server has no review routes
    r = Rig(auth=False)
    try:
        check(
            "no login configured: GET /api/review/next is 404, POST is 405 (as every POST on an open server)",
            (r.req("GET", "/api/review/next", auth=False)[0], r.req("POST", "/api/review/verdict", {"x": 1}, auth=False)[0]) == (404, 405),
        )
    finally:
        r.close()

    # ---------------------------------------------------------------------------------------- label store unavailable
    def broken(root):
        pr = make_tree(root)
        os.makedirs(os.path.join(pr, "labels", "labels.jsonl"))  # the label file is a directory: unreadable
        return pr

    r = Rig(tree=broken)
    try:
        s, p = r.j("GET", "/api/review/next")
        s2, p2 = r.post("/api/review/verdict", {"event_id": EID["jay"], "verdict": "correct"})
        check(
            "unreadable label store: next and verdict are 503 labels_unavailable, detail starts 'label store unavailable: '",
            (s, p["code"], s2, p2["code"]) == (503, "labels_unavailable", 503, "labels_unavailable")
            and p["detail"].startswith("label store unavailable: ")
            and p2["detail"].startswith("label store unavailable: "),
            str((p, p2)),
        )
    finally:
        r.close()

    # ---------------------------------------------------------------------------------------- concurrent writers: threads in the dashboard + the CLI
    def many(root):
        pr = os.path.join(root, "p")
        rows, tags = [], []
        for i in range(48):
            t = NOW - 600 - i * 100
            rows.append(brow(t, "Bird %d" % i, "Genus%c species%d" % (97 + i % 26, i), 0.6))
            tags.append(trow(t))
        write_jsonl(os.path.join(pr, "analysis", "birds.jsonl"), rows)
        write_jsonl(os.path.join(pr, "analysis", "tags.jsonl"), tags)
        return pr

    r = Rig(tree=many)
    try:
        rv = r.app.review
        ids = [L.make_event_id(NOW - 600 - i * 100, ("Genus%c species%d" % (97 + i % 26, i)).lower()) for i in range(48)]
        errs = []

        def worker(chunk):
            for e in chunk:
                try:
                    rv.verdict(e, "correct")
                except Exception as ex:  # noqa: BLE001 - recorded, asserted empty below
                    errs.append(repr(ex))

        ths = [threading.Thread(target=worker, args=(ids[k:32:4],)) for k in range(4)]
        procs = [
            subprocess.Popen(
                [PY, "-I", CLI, "--data-dir", r.pr, "add", "--event-id", e, "--verdict", "unsure", "--basis", "metadata_only"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for e in ids[32:40]
        ]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        outs = [(p.wait(), p.stderr.read()) for p in procs]
        ls = r.lines()
        v = subprocess.run([PY, "-I", CLI, "--data-dir", r.pr, "verify"], capture_output=True)
        check(
            "concurrent writers (4 dashboard threads x 8 events + 8 CLI processes): no error, every CLI exit 0",
            errs == [] and [o[0] for o in outs] == [0] * 8,
            str((errs, outs)),
        )
        check(
            "exactly 40 whole records, 40 distinct label_ids, the 40 distinct events, `bma_labels verify` exit 0 (no torn or interleaved line)",
            len(ls) == 40
            and len({x["label_id"] for x in ls}) == 40
            and {x["event_id"] for x in ls} == set(ids[:40])
            and v.returncode == 0
            and b"0 problem" in v.stdout
            and b"\r" not in raw(r),
            str((len(ls), v.returncode, v.stdout, v.stderr)),
        )
        check(
            "the 32 dashboard records have source ui, the 8 CLI records source cli",
            sorted(x["source"] for x in ls).count("ui") == 32 and sorted(x["source"] for x in ls).count("cli") == 8,
        )
    finally:
        r.close()


# ---------------------------------------------------------------------------------------------- evidence (ui-http 1.9.0)
T_TIT, T_WOOD = NOW - 2200, NOW - 2400
AUDIO = bytes((i * 7 + 3) % 256 for i in range(3000))  # a fake 3000-byte flac: the route serves bytes, it never decodes
WAV = b"RIFF" + bytes(range(100)) * 5
TIT_ID = "%d-baeolophusbicolor-b2" % int((T_TIT - 1) * 1000)
WOOD_ID = "%d-dryobatespubescens-b3" % int((T_WOOD - 1) * 1000)
EID_TIT = L.make_event_id(T_TIT, "baeolophus bicolor")
EID_WOOD = L.make_event_id(T_WOOD, "dryobates pubescens")
BIG_ID = "%d-bigaudio-b1" % int((NOW - 3000) * 1000)
HUGE_ID = "%d-hugeaudio-b1" % int((NOW - 3100) * 1000)


def day_dir(pr, cid):
    d = os.path.join(pr, "analysis", "clips", time.strftime("%Y%m%d", time.gmtime(int(cid[:13]) / 1000.0)))
    os.makedirs(d, exist_ok=True)
    return d


def put(pr, cid, ext, data):
    with open(os.path.join(day_dir(pr, cid), cid + ext), "wb") as f:
        f.write(data)


def evidence_tree(root):
    """make_tree (jay: no clip, chickadee: PNG only) + the titmouse (PNG + flac) and the downy woodpecker (wav only, no PNG)."""
    pr = make_tree(root)
    for sp_c, sp_s, t, b in (("Tufted Titmouse", "Baeolophus bicolor", T_TIT, 2), ("Downy Woodpecker", "Dryobates pubescens", T_WOOD, 3)):
        with open(os.path.join(pr, "analysis", "birds.jsonl"), "ab") as f:
            f.write((json.dumps(brow(t, sp_c, sp_s, 0.55, b), separators=(",", ":")) + "\n").encode())
        with open(os.path.join(pr, "analysis", "tags.jsonl"), "ab") as f:
            f.write((json.dumps(trow(t), separators=(",", ":")) + "\n").encode())
    put(pr, TIT_ID, ".png", PNG)
    put(pr, TIT_ID, ".flac", AUDIO)
    put(pr, WOOD_ID, ".wav", WAV)
    put(pr, BIG_ID, ".flac", bytes(5 << 20))
    put(pr, HUGE_ID, ".flac", bytes((16 << 20) + 1))
    day = lambda c: time.strftime("%Y%m%d", time.gmtime(int(c[:13]) / 1000.0))
    with open(os.path.join(pr, "analysis", "clips", "index.jsonl"), "ab") as f:
        for cid, t, sp_c, sp_s, png, audio in (
            (TIT_ID, T_TIT, "Tufted Titmouse", "Baeolophus bicolor", True, ".flac"),
            (WOOD_ID, T_WOOD, "Downy Woodpecker", "Dryobates pubescens", False, ".wav"),
        ):
            rec = {
                "k": "clip_stored",
                "v": 1,
                "id": cid,
                "t_start": t - 1,
                "t_end": t + 4,
                "species": {"scientific": sp_s, "common": sp_c},
                "group": "bird",
                "confidence_max": 0.55,
                "beam": 2,
                "png": (day(cid) + "/" + cid + ".png") if png else None,
                "audio_stored": True,
                "audio_file": day(cid) + "/" + cid + audio,
            }
            f.write((json.dumps(rec, separators=(",", ":")) + "\n").encode())
    return pr


def drain(r, n=10):
    """-> [item] serving order: next, skip it, next ... (skips are in memory only)."""
    out = []
    for _ in range(n):
        it = r.j("GET", "/api/review/next")[1]["item"]
        if it is None:
            break
        out.append(it)
        r.post("/api/review/skip", {"event_id": it["event_id"]})
    return out


def run_evidence():
    r = Rig(tree=evidence_tree)
    try:
        TIT_URL, WOOD_URL = "/api/review/audio?id=" + TIT_ID, "/api/review/audio?id=" + WOOD_ID
        # ---- queue: evidence only, audio first
        s, nx = r.j("GET", "/api/review/next")
        s2, st = r.j("GET", "/api/review/stats")
        check(
            "evidence-only default: depth 3 (titmouse + woodpecker with audio, chickadee PNG only), the Blue Jay without a clip is not counted, stats no_clip 1",
            (s, nx["queue_depth"], st["queue_depth"], st["no_clip"]) == (200, 3, 3, 1),
            str((nx["queue_depth"], st)),
        )
        order = drain(r)
        names = [i["species"]["common"] for i in order]
        check(
            "served order: both audio items first (either order), then the PNG-only chickadee; the Blue Jay is never served",
            sorted(names[:2]) == ["Downy Woodpecker", "Tufted Titmouse"] and names[2:] == ["Carolina Chickadee"] and len(names) == 3,
            str(names),
        )
        by = {i["species"]["common"]: i for i in order}
        tit, wood, chick = by["Tufted Titmouse"], by["Downy Woodpecker"], by["Carolina Chickadee"]
        check(
            "titmouse item: clip_png and clip_audio urls and the exact span (clip t-1..t+4, event start t)",
            (tit["clip_png"], tit["clip_audio"], tit["clip_span"]["clip_start_s"], tit["clip_span"]["clip_end_s"], tit["clip_span"]["event_start_s"])
            == ("/api/review/clip?id=" + TIT_ID, TIT_URL, round(T_TIT - 1, 3), round(T_TIT + 4, 3), round(T_TIT, 3)),
            str(tit),
        )
        check(
            "woodpecker item (audio only, no PNG): clip_audio present, clip_png absent",
            (wood["clip_audio"], "clip_png" in wood) == (WOOD_URL, False),
            str(wood),
        )
        check(
            "chickadee item (PNG only): clip_png present, clip_audio and no player url absent, clip_span present",
            (chick["clip_png"], "clip_audio" in chick, sorted(chick["clip_span"]))
            == ("/api/review/clip?id=" + CLIP_ID, False, ["clip_end_s", "clip_start_s", "event_end_s", "event_start_s"]),
            str(chick),
        )
        check(
            "chickadee clip_span exact: clip t-1..t+4, event T_CHICK..T_CHICK+1 (two reports 1 s apart)",
            chick["clip_span"]
            == {
                "clip_start_s": round(T_CHICK - 1, 3),
                "clip_end_s": round(T_CHICK + 4, 3),
                "event_start_s": round(T_CHICK, 3),
                "event_end_s": round(T_CHICK + 1, 3),
            },
            str(chick["clip_span"]),
        )
        try:
            import jsonschema

            sch = json.load(open(os.path.join(HERE, "..", "contracts", "schemas", "ui_http.schema.json")))
            sst = r.j("GET", "/api/review/stats")[1]
            ok = all(
                not list(jsonschema.Draft202012Validator({"$schema": sch["$schema"], "$ref": "#/$defs/" + nm, "$defs": sch["$defs"]}).iter_errors(doc))
                for nm, doc in (("review_next", {"item": tit, "queue_depth": 3}), ("review_next", {"item": wood, "queue_depth": 3}), ("review_stats", sst))
            )
            check("items with clip_audio / clip_span and stats with no_clip validate against the ui-http schema", ok)
        except ImportError:
            print("SKIP schema validation: jsonschema is not installed")
        r.app.review.skipped.clear()
        # ---- a verdict on an audio-only item is recorded
        s, ok = r.post("/api/review/verdict", {"event_id": EID_WOOD, "verdict": "correct"})
        check(
            "verdict on the audio-only woodpecker: recorded, depth 2, no_clip still 1",
            (s, ok["recorded"], ok["queue_depth"], r.j("GET", "/api/review/stats")[1]["no_clip"]) == (200, True, 2, 1),
            str(ok),
        )

        # ---- audio route: auth
        check(
            "audio route without a credential: 401 (positive twin: with the credential 200)",
            (r.req("GET", TIT_URL, auth=False)[0], r.req("GET", TIT_URL)[0]) == (401, 200),
        )
        # ---- whole file, content types, headers
        s, h, b = r.req("GET", TIT_URL)
        check(
            "audio: 200, audio/flac, Cache-Control no-store, Accept-Ranges bytes, exact 3000 bytes",
            (s, h["content-type"], h["cache-control"], h["accept-ranges"], b, h["content-length"]) == (200, "audio/flac", "no-store", "bytes", AUDIO, "3000"),
            str((s, h)),
        )
        s, h, b = r.req("GET", WOOD_URL)
        check("audio: a .wav clip is audio/wav with its exact bytes", (s, h["content-type"], b) == (200, "audio/wav", WAV), str((s, h)))
        s, h, b = r.req("HEAD", TIT_URL)
        check(
            "audio HEAD: 200, empty body, content-length 3000, audio/flac",
            (s, b, h["content-length"], h["content-type"]) == (200, b"", "3000", "audio/flac"),
            str((s, h)),
        )

        # ---- ranges
        def rng(v, url=TIT_URL):
            s, h, b = r.req("GET", url, headers={"Range": v})
            return s, h.get("content-range"), b, h

        res = {
            "start-end": rng("bytes=0-99"),
            "open end": rng("bytes=2900-"),
            "suffix": rng("bytes=-100"),
            "clamped end": rng("bytes=10-5000"),
            "one byte": rng("bytes=2999-2999"),
        }
        check(
            "Range bytes=0-99: 206, Content-Range bytes 0-99/3000, exactly bytes 0..99",
            res["start-end"][:3] == (206, "bytes 0-99/3000", AUDIO[:100]),
            str(res["start-end"][:2]),
        )
        check(
            "Range bytes=2900-: 206, bytes 2900-2999/3000, the last 100 bytes",
            res["open end"][:3] == (206, "bytes 2900-2999/3000", AUDIO[2900:]),
            str(res["open end"][:2]),
        )
        check(
            "Range bytes=-100 (suffix): 206, bytes 2900-2999/3000, the last 100 bytes",
            res["suffix"][:3] == (206, "bytes 2900-2999/3000", AUDIO[-100:]),
            str(res["suffix"][:2]),
        )
        check(
            "Range bytes=10-5000: end clamped to the file, 206, bytes 10-2999/3000",
            res["clamped end"][:3] == (206, "bytes 10-2999/3000", AUDIO[10:]),
            str(res["clamped end"][:2]),
        )
        check("Range bytes=2999-2999: one byte", res["one byte"][:3] == (206, "bytes 2999-2999/3000", AUDIO[2999:]), str(res["one byte"][:2]))
        check(
            "a 206 keeps audio/flac, no-store and Content-Length of the slice",
            all(
                v[3]["content-type"] == "audio/flac" and v[3]["cache-control"] == "no-store" and v[3]["content-length"] == str(len(v[2])) for v in res.values()
            ),
        )
        bad_r = {"start at end": "bytes=3000-", "far past": "bytes=5000-6000", "suffix zero": "bytes=-0"}
        got = {k: rng(v) for k, v in bad_r.items()}
        check(
            "unsatisfiable ranges (start at the size, far past the size, suffix 0): 416 range_not_satisfiable with Content-Range bytes */3000",
            all(v[0] == 416 and v[1] == "bytes */3000" and json.loads(v[2])["code"] == "range_not_satisfiable" for v in got.values()),
            str({k: v[:2] for k, v in got.items()}),
        )
        ign = {
            k: rng(v)
            for k, v in {"garbage": "bytes=abc", "multi": "bytes=0-1,5-6", "other unit": "items=0-1", "reversed": "bytes=5-2", "empty": "bytes=-"}.items()
        }
        check(
            "malformed / multi-range / reversed Range headers are ignored: 200 with the whole file",
            all(v[0] == 200 and v[2] == AUDIO and v[1] is None for v in ign.values()),
            str({k: v[:2] for k, v in ign.items()}),
        )
        # ---- bounds
        s, ch, b, h = rng("bytes=0-", "/api/review/audio?id=" + BIG_ID)
        check(
            "a 5 MiB clip answered with an open range: 206 of exactly 4 MiB, bytes 0-4194303/5242880 (bounded)",
            (s, ch, len(b)) == (206, "bytes 0-4194303/5242880", 4 << 20),
            str((s, ch, len(b))),
        )
        s, h, b = r.req("GET", "/api/review/audio?id=" + BIG_ID)
        check("positive twin: the same 5 MiB clip with no Range is served whole (200, 5242880 bytes)", (s, len(b)) == (200, 5 << 20), str((s, len(b))))
        s, h, b = r.req("GET", "/api/review/audio?id=" + HUGE_ID)
        check("a clip over 16 MiB is 404, never served", (s, json.loads(b)["code"]) == (404, "not_found"), str((s, b[:80])))
        # ---- 404: absent, expired, wrong day
        check("audio: a well-formed id with no file is 404", r.req("GET", "/api/review/audio?id=1791997838000-nothing-b1")[0] == 404)
        wrong = os.path.join(r.pr, "analysis", "clips", "20200101")
        os.makedirs(wrong, exist_ok=True)
        with open(os.path.join(wrong, "1791997838000-misplaced-b1.flac"), "wb") as f:
            f.write(AUDIO)
        check(
            "audio: a file in a day directory that is not the id's day is 404 (the day comes from the id)",
            r.req("GET", "/api/review/audio?id=1791997838000-misplaced-b1")[0] == 404,
        )
        # ---- traversal / malformed: the 18 clip cases + audio-specific ones
        for pth in (
            os.path.join(r.pr, "analysis", "clips", "secret2.flac"),
            os.path.join(r.root, "secret.flac"),
            os.path.join(r.pr, "analysis", "clips", "20261014", "1791997838000-evil.flac"),
        ):
            with open(pth, "wb") as f:
                f.write(AUDIO)
        bad = [
            "../../../secret",
            "..%2f..%2fsecret",
            "%2e%2e%2f%2e%2e%2fsecret",
            TIT_ID + "/../../../../secret",
            "..\\..\\secret",
            "c%3A%5Cwindows%5Cwin.ini",
            "/etc/passwd",
            TIT_ID.upper(),
            TIT_ID + "%00",
            TIT_ID + ".flac",
            TIT_ID + ".wav",
            "1791997838000-x-b9",
            "179199783800-x-b1",
            "1791997838000-" + "a" * 25 + "-b1",
            "1791997838000--b1",
            "",
            "secret",
            "1791997838000-evil",
            "1791997838000-d/../../secret2",
            "1791997838000-evil.flac",
            "../../../secret.flac",
        ]
        got = [r.req("GET", "/api/review/audio?id=" + x)[0] for x in bad]
        check("audio: %d traversal / malformed ids are all 404" % len(bad), got == [404] * len(bad), str(list(zip(bad, got))))
        check(
            "audio: no id 404, an extra parameter 422, a repeated id 422 (positive twin: the real id is 200)",
            (r.req("GET", "/api/review/audio")[0], r.req("GET", TIT_URL + "&x=1")[0], r.req("GET", TIT_URL + "&id=" + TIT_ID)[0], r.req("GET", TIT_URL)[0])
            == (404, 422, 422, 200),
        )
        check(
            "audio_path maps the id to <clips>/<UTC day>/<id>.flac exactly",
            r.app.review.audio_path(TIT_ID) == os.path.join(r.pr, "analysis", "clips", time.strftime("%Y%m%d", time.gmtime(T_TIT - 1)), TIT_ID + ".flac"),
        )
        check(
            "POST on the audio route is 405 with Allow: GET, HEAD",
            (lambda x: (x[0], x[1].get("allow")))(r.req("POST", TIT_URL, {"csrf": r.csrf()})) == (405, "GET, HEAD"),
        )
        # ---- expiry: the audio file is deleted (24 h retention): the route is 404 and the item loses clip_audio, keeps the PNG
        os.remove(os.path.join(day_dir(r.pr, TIT_ID), TIT_ID + ".flac"))
        s, h, b = r.req("GET", TIT_URL)
        r.app.review.cache = None
        it = [i for i in drain(r) if i["event_id"] == EID_TIT]
        check(
            "expired audio: the route is 404; the item no longer carries clip_audio but keeps clip_png (still evidence)",
            s == 404 and len(it) == 1 and "clip_audio" not in it[0] and it[0]["clip_png"] == "/api/review/clip?id=" + TIT_ID,
            str((s, it)),
        )
        r.app.review.skipped.clear()
        os.remove(os.path.join(day_dir(r.pr, WOOD_ID), WOOD_ID + ".wav"))
        r.app.review.cache = None
        ev_w = r.app.review._load()["events"][EID_WOOD]
        check(
            "an audio-only clip whose audio expired is no evidence: clip_for(woodpecker) is None (positive twin: the titmouse, PNG left, still has a clip)",
            (r.app.review.clip_for(ev_w), r.app.review.clip_for(r.app.review._load()["events"][EID_TIT])["id"]) == (None, TIT_ID),
        )
        ev_c = r.app.review._load()["events"][EID["chick"]]  # clip times are ms-rounded, event times are not: 50 ms slack on both sides
        idx_p = os.path.join(r.pr, "analysis", "clips", "index.jsonl")
        with open(idx_p, newline="") as f:
            idx_keep = f.read()

        def chick_clip(t0, t1):
            write_jsonl(
                idx_p,
                [
                    {
                        "k": "clip_stored",
                        "v": 1,
                        "id": CLIP_ID,
                        "t_start": t0,
                        "t_end": t1,
                        "species": {"scientific": "Poecile carolinensis", "common": "Carolina Chickadee"},
                        "group": "bird",
                        "confidence_max": 0.7,
                        "beam": 1,
                        "png": CLIP_ID + ".png",
                    }
                ],
            )
            r.app.review.clip_cache = None
            c = r.app.review.clip_for(ev_c)
            return None if c is None else c["id"]

        got = (
            chick_clip(ev_c.t_start - 5, ev_c.t_start - 0.0003),
            chick_clip(ev_c.t_start - 5, ev_c.t_start - 0.0499),
            chick_clip(ev_c.t_end + 0.0003, ev_c.t_end + 5),
            chick_clip(ev_c.t_end + 0.0499, ev_c.t_end + 5),
        )
        check(
            "clip_for: a clip ending 0.3 ms / 49.9 ms before the event starts, or starting 0.3 ms / 49.9 ms after it ends, is its clip",
            got == (CLIP_ID,) * 4,
            str(got),
        )
        got = (chick_clip(ev_c.t_start - 5, ev_c.t_start - 0.0501), chick_clip(ev_c.t_end + 0.0501, ev_c.t_end + 5))
        check("clip_for: just outside the 50 ms slack (50.1 ms before the start / after the end) the clip is not evidence", got == (None, None), str(got))
        with open(idx_p, "w", newline="") as f:
            f.write(idx_keep)
        r.app.review.clip_cache = None
        os.remove(os.path.join(day_dir(r.pr, TIT_ID), TIT_ID + ".png"))
        st = r.j("GET", "/api/review/stats")[1]
        check(
            "titmouse with neither PNG nor audio left: queue_depth 1 (chickadee), no_clip 2 (jay + titmouse)",
            (st["queue_depth"], st["no_clip"]) == (1, 2),
            str(st),
        )
    finally:
        r.close()

    # ---- the flag: no-clip events are served only with --review-allow-no-clip
    def only_jay(root):
        pr = make_tree(root)
        shutil.rmtree(os.path.join(pr, "analysis", "clips"))
        return pr

    r = Rig(tree=only_jay)
    try:
        s, nx = r.j("GET", "/api/review/next")
        st = r.j("GET", "/api/review/stats")[1]
        check(
            "default, no clips at all: the queue is empty (item null, depth 0) and stats say no_clip 2 (jay + chickadee)",
            (s, nx, st["queue_depth"], st["no_clip"]) == (200, {"item": None, "queue_depth": 0}, 0, 2),
            str((nx, st)),
        )
        check(
            "default: verdict is still accepted for a no-clip event by id (guard rules unchanged), speech event still 422",
            (
                r.post("/api/review/verdict", {"event_id": EID["jay"], "verdict": "unsure"})[0],
                r.post("/api/review/verdict", {"event_id": EID["human"], "verdict": "correct"})[0],
            )
            == (200, 422),
        )
    finally:
        r.close()
    r = Rig(tree=only_jay, allow_no_clip=True)
    try:
        nx = r.j("GET", "/api/review/next")[1]
        st = r.j("GET", "/api/review/stats")[1]
        check(
            "--review-allow-no-clip, same tree: the Blue Jay is served, depth 2, no_clip still reports 2",
            (nx["item"]["event_id"], nx["queue_depth"], st["queue_depth"], st["no_clip"]) == (EID["jay"], 2, 2, 2),
            str((nx, st)),
        )
    finally:
        r.close()
    # ---- audio before PNG-only, even when the queue order puts a PNG-only item first (rarest-first ties break by species name: baeolophus < cyanocitta < dryobates < poecile)
    JAY_ID = "%d-cyanocittacristata-b3" % int((T_JAY - 1) * 1000)

    def jay_png(root):
        pr = evidence_tree(root)
        put(pr, JAY_ID, ".png", PNG)
        day = time.strftime("%Y%m%d", time.gmtime(int(JAY_ID[:13]) / 1000.0))
        with open(os.path.join(pr, "analysis", "clips", "index.jsonl"), "ab") as f:
            f.write(
                (
                    json.dumps(
                        {
                            "k": "clip_stored",
                            "v": 1,
                            "id": JAY_ID,
                            "t_start": T_JAY - 1,
                            "t_end": T_JAY + 4,
                            "species": {"scientific": "Cyanocitta cristata", "common": "Blue Jay"},
                            "group": "bird",
                            "confidence_max": 0.6,
                            "beam": 3,
                            "png": day + "/" + JAY_ID + ".png",
                            "audio_stored": False,
                            "audio_file": None,
                        },
                        separators=(",", ":"),
                    )
                    + chr(10)
                ).encode()
            )
        return pr

    r = Rig(tree=jay_png)
    try:
        names = [i["species"]["common"] for i in drain(r)]
        check(
            "queue order with a PNG-only Blue Jay: both audio items first (titmouse, woodpecker), then the PNG-only jay, then the PNG-only chickadee (queue order inside each class)",
            names == ["Tufted Titmouse", "Downy Woodpecker", "Blue Jay", "Carolina Chickadee"],
            str(names),
        )
    finally:
        r.close()
    pa = U.make_parser({}).parse_args([])
    pb = U.make_parser({}).parse_args(["--review-allow-no-clip"])
    check("the CLI flag --review-allow-no-clip is off by default and parsed when given", (pa.review_allow_no_clip, pb.review_allow_no_clip) == (False, True))


# ---------------------------------------------------------------------------------------------- evidence first (review queue without the sampling cap)
EF_NOW = 1793100000.0  # fixed clock: event ids (and so the seeded audit draw of the old queue) are the same on every run


def ef_tree(root):
    """~340 eligible events, 200 s apart: 20 filler species x 10 clipless (conf 0.6) and four 'old' species with 30-40 clipless near events (conf 0.5) each
    plus ONE clip-backed low (conf 0.05) event, ranked last in its species: png (PNG only), aud (PNG + flac), spc (PNG; a speech-active tag on it), lab (PNG; labelled by
    the test).  Plus 3 clipless 2-beam hi events (audit stratum)."""
    pr = os.path.join(root, "p")
    birds, tags, idx, n = [], [], [], [0]

    def at():
        n[0] += 1
        return EF_NOW - 3000 - 200 * n[0]

    def ev(name, conf, speech=False, two=False):
        t = at()
        birds.append(brow(t, name.title(), "Gen " + name, conf, 1))
        if two:
            birds.append(brow(t + 0.1, name.title(), "Gen " + name, conf, 2))
        tags.append(trow(t, 0.9 if speech else 0.0, speech))
        return t

    for i in range(20):
        for _ in range(10):
            ev("fill%02d" % i, 0.6)
    for _ in range(3):
        ev("hi", 0.9, two=True)
    ids = {}
    for name, n_none, audio, speech in (("png", 30, False, False), ("spc", 30, False, True), ("lab", 30, False, False), ("aud", 40, True, False)):
        for _ in range(n_none - 1):
            ev(name, 0.5)
        t = ev(name, 0.05, speech)
        cid = "%d-gen%s-b1" % (int((t - 1) * 1000), name)
        day = time.strftime("%Y%m%d", time.gmtime(t - 1))
        put(pr, cid, ".png", PNG)
        if audio:
            put(pr, cid, ".flac", AUDIO)
        idx.append(
            {
                "k": "clip_stored",
                "v": 1,
                "id": cid,
                "t_start": t - 1,
                "t_end": t + 4,
                "species": {"scientific": "Gen " + name, "common": name.title()},
                "group": "bird",
                "confidence_max": 0.05,
                "beam": 1,
                "png": day + "/" + cid + ".png",
                "audio_stored": audio,
                "audio_file": (day + "/" + cid + ".flac") if audio else None,
            }
        )
        ids[name] = L.make_event_id(t, "gen " + name)
    write_jsonl(os.path.join(pr, "analysis", "birds.jsonl"), sorted(birds, key=lambda r: r["t"]))  # time order, as the analyzer writes it
    write_jsonl(os.path.join(pr, "analysis", "tags.jsonl"), sorted(tags, key=lambda r: r["t"]))
    write_jsonl(os.path.join(pr, "analysis", "clips", "index.jsonl"), idx)
    ef_tree.ids = ids
    ef_tree.n = 333  # events: 200 fillers + 3 hi (2 beams each) + 30 + 30 + 30 + 40
    return pr


def run_evidence_first():
    import review_queue as Q

    root = tempfile.mkdtemp(prefix="bma_rv_ef_")
    try:
        pr = ef_tree(root)
        ids, n_all = ef_tree.ids, ef_tree.n
        old = RV.Review(pr, allow_no_clip=True, clock=lambda: EF_NOW)
        co = old._load()
        offered_old = {i["event_id"] for i in old._todo(co)[0]}
        miss = offered_old & {ids["aud"], ids["png"]}
        check(
            "old sampled queue (allow_no_clip, limit 200): 200 items of %d eligible events (speech one excluded); neither the audio nor the PNG clip event is among them (the audit draw happened to take 'lab')"
            % (n_all - 1),
            (len(co["queue"]["items"]), co["queue"]["eligible"]["events"], miss, ids["lab"] in offered_old) == (200, n_all - 1, set(), True),
            str((len(co["queue"]["items"]), co["queue"]["eligible"]["events"], miss)),
        )
        rv = RV.Review(pr, clock=lambda: EF_NOW)
        rv.verdict(ids["lab"], "unsure")  # the human-labelled one
        rv._drop()
        c = rv._load()
        q = c["queue"]
        todo, n_none = rv._todo(c)
        check(
            "evidence first: the queue holds every eligible event (%d = %d - speech - labelled), the sampling cap is gone (params 10**6)" % (n_all - 2, n_all),
            (len(q["items"]), q["eligible"]["events"], q["params"]["limit"], q["params"]["per_species_cap"], q["params"]["audit_per_species_cap"])
            == (n_all - 2, n_all - 2, 10**6, 10**6, 10**6),
            str((len(q["items"]), q["eligible"]["events"])),
        )
        served = [i["event_id"] for i in todo]
        check(
            "served exactly [aud, png]: audio first; the speech-flagged and the human-labelled clip events are not offered",
            served == [ids["aud"], ids["png"]],
            str(served),
        )
        check(
            "positive twin: png precedes aud in the queue's own order (the audio-first sort put aud first)",
            [i["event_id"] for i in q["items"] if i["event_id"] in (ids["png"], ids["aud"])] == [ids["png"], ids["aud"]],
        )
        st = rv.stats()
        check(
            "stats exact: queue_depth 2, no_clip %d (eligible %d minus 2)" % (n_all - 4, n_all - 2),
            (st["queue_depth"], st["no_clip"], st["labelled"]) == (2, n_all - 4, 1),
            str(st),
        )
        nx = rv.next()
        check(
            "next(): the audio item first with clip_audio, queue_depth 2",
            (nx["item"]["event_id"], nx["queue_depth"], "clip_audio" in nx["item"]) == (ids["aud"], 2, True),
            str(nx),
        )
        try:
            rv.verdict(ids["spc"], "correct")
            refused = None
        except RV.ReviewError as ex:
            refused = (ex.status, ex.code)
        check("the speech-flagged clip event is refused by verdict (422 not_labelable)", refused == (422, "not_labelable"), str(refused))
        item = {i["event_id"]: i for i in q["items"]}[ids["aud"]]
        out = rv.verdict(ids["aud"], "correct")
        rec = rv.store.read()[0][-1]
        check(
            "label of the audio event copies the builder's item: stratum low, p_include null (an uncertainty pick, no known probability), queue_id of the uncapped queue",
            (rec["event_id"], rec["stratum"], rec["p_include"], rec["queue_id"], item["stratum"], item["p_include"], item["selection"], out["recorded"])
            == (ids["aud"], "low", None, q["queue_id"], "low", None, "uncertainty", True),
            str(rec),
        )
        c2 = rv._load()
        check(
            "after the verdict: depth 1 (png), aud not offered again, no_clip unchanged",
            (out["queue_depth"], [i["event_id"] for i in rv._todo(c2)[0]], rv.stats()["no_clip"]) == (1, [ids["png"]], n_all - 4),
            str(out),
        )
        hi = [i for i in q["items"] if i["stratum"] == "hi"]
        check(
            "uncapped sampling probabilities: the 3 hi events are audit items with p_include exactly 1.0; every other item an uncertainty pick with null p_include",
            len(hi) == 3
            and all((i["selection"], i["p_include"]) == ("audit", 1.0) for i in hi)
            and all((i["selection"], i["p_include"]) == ("uncertainty", None) for i in q["items"] if i["stratum"] != "hi"),
            str(hi),
        )
        check(
            "review_queue defaults untouched (limit 200, per_species_cap 25, audit cap 5, 14 days)",
            (Q.DEFAULTS["limit"], Q.DEFAULTS["per_species_cap"], Q.DEFAULTS["audit_per_species_cap"], Q.DEFAULTS["days"]) == (200, 25, 5, 14.0),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    run()
    run_evidence()
    run_evidence_first()
    print(f"\n{n_checks} checks, {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
