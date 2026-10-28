#!/usr/bin/env python3
"""Mutation run for the Review tab: each mutant changes one behaviour in a copy-on-write fashion (file restored afterwards) and the named
suite must go red.  Exit 0 only when every mutant is KILLED.

    python mutation_review.py [name-substring]"""

import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
A = os.path.join(HERE, "..", "analysis")
PY = sys.executable
API, DOM = "test_review.py", "test_dom_review.py"

MUTANTS = [
    ("CSRF not read from the body", "bma_ui.py", "if not self._csrf_ok(body):", "if not self._csrf_ok():", API),
    (
        "CSRF check removed",
        "bma_ui.py",
        'if not self._csrf_ok(body):\n                    self._problem(403, "csrf_failed", "Request refused", "missing or wrong CSRF token (see GET /api/session)")\n                    return True\n                out = rv.verdict',
        "if False:\n                    return True\n                out = rv.verdict",
        API,
    ),
    ("speech guard removed from verdict", "review.py", 'if ev is None or L.speech_reason(ev, c["tags"]) is not None:', "if ev is None:", API),
    (
        "untagged events enter the queue",
        "review.py",
        "Q.build_queue(events, folded, tags, {} if self.allow_no_clip else EVIDENCE_FIRST_PARAMS, now",
        'Q.build_queue(events, folded, tags, {"include_untagged": True}, now',
        API,
    ),
    ("evidence-first mode keeps the sampling cap", "review.py", "{} if self.allow_no_clip else EVIDENCE_FIRST_PARAMS", "{}", API),
    ("evidence-first cap too small (per species cap 25 left in)", "review.py", '"per_species_cap": NO_CAP', '"per_species_cap": 25', API),
    ("param of a store refusal lost", "review.py", 'msg.split(":")[0])', '"x")', API),
    ("no idempotency", "review.py", 'and 0 <= now - last["t_label"] < DEDUP_S):', "and False):", API),
    ("clip id check loosened", "review.py", 'CLIP_ID_RE = re.compile(r"[0-9]{13}-[a-z0-9]{1,24}-b[1-8]")', 'CLIP_ID_RE = re.compile(r"[0-9]{13}-.*")', API),
    (
        "skip does not remember",
        "review.py",
        "if event_id not in self.skipped:\n                self.skipped.append(event_id)",
        "if False:\n                self.skipped.append(event_id)",
        API,
    ),
    ("source ui recorded as cli", "review.py", 'source="ui"', 'source="cli"', API),
    ("basis always metadata_only", "review.py", '"clip_visual" if self.clip_id_for(ev) else "metadata_only"', '"metadata_only"', API),
    ("artifact flags dropped", "review.py", "        if arts:\n", "        if False:\n", API),
    ("queue reason code changed", "review.py", 'REASONS = {"near": "near"', 'REASONS = {"near": "nearish"', API),
    ("clip URL not offered", "review.py", '            if cl["png"]:\n', "            if False:\n", API),
    ("evidence filter removed (no-clip events served by default)", "review.py", "if cl or self.allow_no_clip:", "if True:", API),
    ("audio-first ordering removed", "review.py", "rows.sort(key=lambda x: x[0])", "pass", API),
    ("no_clip not counted", "review.py", "n_none += cl is None", "n_none += 0", API),
    ("flag default on", "review.py", "self.allow_no_clip = bool(allow_no_clip)", "self.allow_no_clip = True", API),
    ("clip_audio offered for PNG-only clips", "review.py", '            if cl["audio"]:\n', "            if True:\n", API),
    ("event end reported as its start", "review.py", '"event_end_s": round(ev.t_end, 3)', '"event_end_s": round(ev.t_start, 3)', API),
    ("audio content type wrong", "review.py", '".flac": "audio/flac"', '".flac": "audio/x-flac"', API),
    ("suffix range off by one", "review.py", "max(0, size - n)), size - 1", "max(0, size - n + 1)), size - 1", API),
    ("unsatisfiable range not refused", "review.py", "if lo >= size:", "if False:", API),
    ("range not bounded", "review.py", "hi = min(hi, size - 1, lo + MAX_RANGE_BYTES - 1)", "hi = min(hi, size - 1)", API),
    ("audio size cap removed", "review.py", "if not 0 < size <= MAX_AUDIO_BYTES:", "if not 0 < size:", API),
    ("Range header ignored by the route", "bma_ui.py", 'rv.clip_audio(q.get("id"), self.headers.get("Range"))', 'rv.clip_audio(q.get("id"), None)', API),
    ("416 loses Content-Range", "bma_ui.py", "head, param=ex.param, extra=ex.extra)", "head, param=ex.param)", API),
    (
        "audio of an expired clip still offered (no existence check)",
        "review.py",
        'png, audio = self.clip_path(r["id"]) is not None, self.audio_path(r["id"]) is not None',
        'png, audio = self.clip_path(r["id"]) is not None, True',
        API,
    ),
    (
        "no lock and a split write (interleaving)",
        "labels.py",
        "        lock = self._lock()\n        try:\n            fd = os.open",
        '        lock = self.path + ".lock"\n        try:\n            fd = os.open',
        API,
        (
            "labels.py",
            "                n = os.write(fd, prefix + line)\n                if n != len(prefix) + len(line):",
            "                os.write(fd, (prefix + line)[:20]); time.sleep(0.001); os.write(fd, (prefix + line)[20:]); n = len(prefix) + len(line)\n                if n != len(prefix) + len(line):",
        ),
        ("review.py", "self.lock = threading.RLock()", 'self.lock = type("N", (), {"__enter__": lambda s: None, "__exit__": lambda s, *a: None})()'),
    ),
    ("key 1 mapped to wrong", "static/review.js", 'const i = "1234".indexOf(k);', 'const i = "2134".indexOf(k);', DOM),
    ("s key is not skip", "static/review.js", 'k === "s" || k === "S"', 'k === "x"', DOM),
    ("keys act on a hidden tab", "static/review.js", "if (!R.root || R.root.hidden || ev.ctrlKey", "if (!R.root || ev.ctrlKey", DOM),
    ("Escape does not cancel the reason row", "static/review.js", 'if (k === "Escape") { R.wrong = false;', 'if (k === "Escapex") { R.wrong = false;', DOM),
    ("CSRF token not sent", "static/review.js", 'csrf: tk || "" };\n    if (reason)', 'csrf: "" };\n    if (reason)', DOM),
    ("wrong reason not sent", "static/review.js", "if (reason) body.wrong_reason = reason;", "", DOM),
    ("empty state text changed", "static/review.js", '"queue empty: "', '"nothing: "', DOM),
    ("empty state ignores the no_clip number", "static/review.js", "(st ? st.no_clip : 0)", "0", DOM),
    ("player preload not none", "static/review.js", 'preload: "none", ', "", DOM),
    ("player offered without audio", "static/review.js", "    if (it.clip_audio) {\n      m.audio", "    if (true) {\n      m.audio", DOM),
    ("no-audio note text changed", "static/review.js", '}, "no audio")', '}, "none")', DOM),
    ("space does not play", "static/review.js", 'if (k === " ") { ev.preventDefault(); togglePlay(); return; }', "", DOM),
    ("r does not replay", "static/review.js", 'if (k === "r" || k === "R") { ev.preventDefault(); replay(); return; }', "", DOM),
    (
        "media block rebuilt on every render (playback stops)",
        "static/review.js",
        "if (!R.media || R.media.id !== it.event_id) R.media = mediaOf(it);",
        "R.media = mediaOf(it);",
        DOM,
    ),
    ("one-report event gets no span marker", "static/review.js", "Math.max(b - a, 0.01)", "(b - a)", DOM),
    ("time axis step changed", "static/review.js", "Math.ceil(dur / 8)", "Math.ceil(dur / 2)", DOM),
    ("player src points at the PNG route", "static/review.js", "src: it.clip_audio })", "src: it.clip_png })", DOM),
    ("spectrogram not shown", "static/review.js", "if (it.clip_png) {\n      const wrap", "if (false) {\n      const wrap", DOM),
    ("clip min conf ignored in the capture", "../analysis/clips.py", "if not mx > self.cfg.min_conf:", "if not mx > 0.85:", "../analysis/test_clips.py"),
]


def path(name):
    return os.path.join(A if name == "labels.py" else HERE, name)


def run_one(m):
    name, f, old, new, suite = m[:5]
    extra = m[5:]
    edits = [(f, old, new)] + list(extra)
    saved = {}
    try:
        for fn, o, n in edits:
            p = path(fn)
            s = saved.setdefault(p, open(p, newline="", encoding="utf-8").read())
            cur = open(p, newline="", encoding="utf-8").read()
            if cur.count(o) != 1:
                return "BAD-MUTANT (pattern found %d times in %s)" % (cur.count(o), fn)
            open(p, "w", newline="", encoding="utf-8").write(cur.replace(o, n))
        r = subprocess.run([PY, os.path.join(HERE, suite)], capture_output=True, text=True, timeout=400)
        fl = [ln for ln in r.stdout.splitlines() if ln.startswith("FAIL")]
        return (
            ("KILLED (%d red: %s)" % (len(fl), fl[0][5:90]))
            if r.returncode != 0 and fl
            else ("KILLED (exit %d)" % r.returncode if r.returncode else "SURVIVED")
        )
    finally:
        for p, s in saved.items():
            open(p, "w", newline="", encoding="utf-8").write(s)


def main():
    sel = sys.argv[1] if len(sys.argv) > 1 else ""
    bad = 0
    for m in MUTANTS:
        if sel in m[0]:
            res = run_one(m)
            print("%-48s %s" % (m[0], res), flush=True)
            bad += not res.startswith("KILLED")
    print("%d mutant(s) not killed" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
