#!/usr/bin/env python3
"""Tests for feature_state.py (live ON/OFF state of the feature registry) on scratch data roots. Exit 0 = all checks passed."""

import contextlib, io, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import feature_state as FS

fails, n = [], 0


def ck(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def put(root, rel, text):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def states(root):
    c, rows = FS.collect(root)
    return {r["name"]: (r["state"], r["detail"]) for r in rows}


def run(argv):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = FS.main(argv)
    return rc, buf.getvalue(), err.getvalue()


STATUS = {
    "plugins": {p: {"enabled": True} for p in ("dsp", "spatial", "tags", "clap", "birds", "coherence", "indices", "bursts")},
    "clips": {"enabled": True, "audio": False},
}
STATUS["embeddings_stored"] = 115
STATUS["plugins"]["perch"] = {"enabled": True, "device": "cuda:1"}
STATUS["plugins"]["mixscore"] = {"enabled": False, "reason": "off by default (--mixscore)"}

with tempfile.TemporaryDirectory() as d:
    # ---- an empty root: no file at all
    empty = os.path.join(d, "empty")
    os.makedirs(empty)
    s = states(empty)
    s0 = s
    ck(
        "empty root: the opt-in Mixscore plugin is UNKNOWN (no status.json), not OFF",
        s["Mixscore plugin"] == (FS.UNKNOWN, "no analysis/status.json"),
        str(s["Mixscore plugin"]),
    )
    ck("empty root: clip audio is OFF (no marker, no status)", s["Clip audio"] == (FS.OFF, "no marker, no status.json"), str(s["Clip audio"]))
    ck("empty root: a removable plugin is ON by default", s["DSP plugin"] == (FS.ON, "default, no status.json"), str(s["DSP plugin"]))
    ck(
        "empty root: supervisor-passed flag is UNKNOWN without run_pipeline.ps1",
        s["Spectrum rows to dashboard"][0] == FS.UNKNOWN,
        str(s["Spectrum rows to dashboard"]),
    )

    # ---- a full root
    r = os.path.join(d, "full")
    put(r, "analysis/status.json", json.dumps(STATUS))
    put(r, "analyzer_args.txt", "﻿# comment\n--perch-model\nD:\\m\\perch.onnx\n--perch-device\ncuda:1\n--clip-pcen\n--clip-min-conf\n0.5\n--no\nbursts\n")
    put(r, "enable_clip_audio.txt", "")
    put(r, "allowed_users.txt", "# nobody yet\n\n")
    put(r, "enable_passkeys.txt", "")
    put(r, "tls/tls_name.txt", "node.example.invalid\n")
    put(r, "tls/dashboard_cert.pem", "x")
    put(r, "tls/dashboard_key.pem", "x")
    put(r, "app/geo_county.json", "{}")
    put(r, "bma.local.ps1", "$BMA_BEAM_SHIFT = '1,0,0,0,0,0,0,-1'\n")
    put(r, "run_pipeline.ps1", "$a = @('--bind', '0.0.0.0', '--allow-ip-hosts', '--spectrum-port', '1')\n# '--require-identity' is only a comment word\n")
    put(
        r,
        "logs/supervisor.log",
        "2026-10-27 11:12:27 supervisor up, app version: x\n2026-10-27 13:16:34 supervisor up, app version: y\n2026-10-27 13:17:00 starting bridge\n",
    )
    s = states(r)
    want = {
        "Mixscore plugin": (FS.OFF, "off by default (--mixscore)"),
        "Perch shadow plugin": (FS.ON, "plugin running (status.json), device cuda:1"),
        "Perch device": (FS.ON, "analyzer_args.txt: --perch-device cuda:1"),
        "Perch embeddings": (FS.OFF, "not in analyzer_args.txt"),
        "Clip audio": (FS.OFF, "configured but the running analyzer has audio off: ON at the next analyzer start"),
        "Clip PCEN spectrogram": (FS.ON, "analyzer_args.txt: --clip-pcen"),
        "Clip confidence gate": (FS.ON, "analyzer_args.txt: --clip-min-conf 0.5 (default 0.85)"),
        "Analyzer args file": (FS.ON, "9 line(s)"),
        "County species filter": (FS.ON, "app\\geo_county.json present"),
        "Tonal mask": (FS.OFF, "app\\tonal_mask.json absent"),
        "Beam shift": (FS.ON, "bma.local.ps1 BMA_BEAM_SHIFT 1,0,0,0,0,0,0,-1"),
        "Dashboard TLS": (FS.ON, "certificate and key found in tls\\"),
        "Tailnet identity login": (FS.OFF, "allowed_users.txt absent or without a login"),
        "Passkeys": (FS.ON, "enable_passkeys.txt and tls\\tls_name.txt present"),
        "Dashboard IP hosts": (FS.ON, "supervisor passes --allow-ip-hosts"),
        "Spectrum rows to dashboard": (FS.ON, "supervisor passes --spectrum-port"),
        "Identity only": (FS.OFF, "supervisor does not pass it"),
        "Bridge manual addressing": (FS.OFF, "supervisor does not pass it"),
        "DSP plugin": (FS.ON, "plugin running (status.json)"),
        "Wildlife clips": (FS.ON, "status.json clips.enabled"),
        "Dashboard board panel": (FS.UNKNOWN, "board URL is resolved at dashboard start and not logged"),
        "Cable sink recording": (FS.OFF, "run by hand, never scheduled"),
        "Legacy classifier": (FS.OFF, "supervisor does not start it"),
        "PANNs embedding store": (FS.ON, "status.json embeddings_stored 115 since the analyzer start"),
    }
    for k, v in want.items():
        ck(f"full root: {k} = {v[0]} ({v[1]})", s[k] == v, str(s[k]))
    # positive twins of the OFF/ON pairs above
    put(r, "run_pipeline.ps1", "Start-WslLauncher 'run_classifier.sh' $port\n")
    ck(
        "positive twin: a supervisor script that starts run_classifier.sh turns the legacy classifier ON",
        states(r)["Legacy classifier"] == (FS.ON, "supervisor starts it (run_classifier.sh)"),
    )
    ck(
        "empty root: the legacy classifier is UNKNOWN without run_pipeline.ps1",
        s0["Legacy classifier"] == (FS.UNKNOWN, "no run_pipeline.ps1 in the data root"),
        str(s0["Legacy classifier"]),
    )
    ck(
        "empty root: the embedding store is UNKNOWN without status.json",
        s0["PANNs embedding store"] == (FS.UNKNOWN, "no embeddings_stored in status.json"),
        str(s0["PANNs embedding store"]),
    )
    put(r, "allowed_users.txt", "# nobody yet\nuser@example.invalid\n")
    ck(
        "positive twin: allowed_users.txt with a login turns Tailnet identity ON",
        states(r)["Tailnet identity login"] == (FS.ON, "allowed_users.txt with 1 login(s)"),
    )
    put(r, "tls/tls_name.txt", "x")
    os.remove(os.path.join(r, "enable_passkeys.txt"))
    ck("passkeys need the marker as well as the name file", states(r)["Passkeys"] == (FS.OFF, "enable_passkeys.txt absent, tls\\tls_name.txt present"))
    put(r, "enable_passkeys.txt", "")
    os.remove(os.path.join(r, "tls", "tls_name.txt"))
    ck(
        "passkeys need the name file as well as the marker",
        states(r)["Passkeys"] == (FS.OFF, "enable_passkeys.txt present, tls\\tls_name.txt absent"),
        str(states(r)["Passkeys"]),
    )
    put(r, "tls/tls_name.txt", "x")
    ck("positive twin: marker and name file together turn passkeys ON", states(r)["Passkeys"] == (FS.ON, "enable_passkeys.txt and tls\\tls_name.txt present"))
    STATUS2 = json.loads(json.dumps(STATUS))
    STATUS2["clips"]["audio"] = True
    put(r, "analysis/status.json", json.dumps(STATUS2))
    ck(
        "clip audio ON when the running analyzer stores audio",
        states(r)["Clip audio"] == (FS.ON, "analyzer is storing audio (status.json clips.audio), enable_clip_audio.txt present"),
    )
    STATUS3 = json.loads(json.dumps(STATUS))
    STATUS3["plugins"]["bursts"] = {"enabled": False, "reason": "removed with --no bursts"}
    put(r, "analysis/status.json", json.dumps(STATUS3))
    ck("a removed plugin is OFF with the analyzer's own reason", states(r)["Bursts plugin"] == (FS.OFF, "removed with --no bursts"))
    ck("positive twin: its neighbour stays ON", states(r)["Indices plugin"] == (FS.ON, "plugin running (status.json)"))
    put(r, "analysis/status.json", "{")
    s = states(r)
    ck(
        "a broken status.json is treated as absent (Mixscore UNKNOWN, no crash)",
        s["Mixscore plugin"] == (FS.UNKNOWN, "no analysis/status.json"),
        str(s["Mixscore plugin"]),
    )
    ck(
        "a --no line removes the plugin when there is no status.json",
        s["Bursts plugin"] == (FS.OFF, "--no bursts in analyzer_args.txt"),
        str(s["Bursts plugin"]),
    )

    # ---- output: order, count, json, write, exit code
    put(r, "analysis/status.json", json.dumps(STATUS))
    put(r, "allowed_users.txt", "")
    rc, out, err = run(["--root", r])
    lines = out.splitlines()
    c, rows = FS.collect(r)
    n_off = sum(1 for x in rows if x["state"] == FS.OFF)
    ck("exit code 0", rc == 0 and err == "", (rc, err))
    ck("first line is the exact count", lines[0] == f"{n_off} features OFF, {len(rows) - n_off - 1} ON, 1 UNKNOWN", lines[0])
    rowl = [l for l in lines[2:] if not l.startswith("WARNING")]
    ck(
        "rows are OFF first, then UNKNOWN, then ON",
        [l.split()[0] for l in rowl] == [FS.OFF] * n_off + [FS.UNKNOWN] + [FS.ON] * (len(rows) - n_off - 1),
        str([l.split()[0] for l in rowl]),
    )
    ck("one row per registry feature", len(rowl) == len(FS.FEATURES), f"{len(rowl)} vs {len(FS.FEATURES)}")
    ck("the second line says the status age is unknown when status.json has no t", "status.json age unknown;" in lines[1], lines[1])
    put(r, "analysis/status.json", json.dumps({**STATUS, "t": __import__("time").time() - 100}))
    ck("positive twin: with t the age is 100 to 110 s", any(f"status.json age {a} s;" in run(["--root", r])[1].splitlines()[1] for a in range(100, 111)))
    put(r, "analysis/status.json", json.dumps(STATUS))
    ck("the second line names the last supervisor start", "supervisor started 2026-10-27 13:16:34" in lines[1], lines[1])
    rc, js, _ = run(["--root", r, "--json"])
    j = json.loads(js)
    ck(
        "--json: counts and one entry per feature",
        j["off"] == n_off and j["unknown"] == 1 and len(j["features"]) == len(FS.FEATURES) and j["features"][0]["state"] == "OFF",
        str({k: j[k] for k in ("off", "on", "unknown")}),
    )
    ck("--json: a row carries key, name, table, state, detail", set(j["features"][0]) == {"key", "name", "table", "state", "detail"})
    ck("no --write: reports/features.txt is not created", not os.path.exists(os.path.join(r, "reports", "features.txt")))
    rc, out2, _ = run(["--root", r, "--write"])
    with open(os.path.join(r, "reports", "features.txt"), encoding="utf-8", newline="") as f:
        wrote = f.read()
    ck(
        "--write: reports/features.txt holds the table, LF only",
        wrote.splitlines()[0] == lines[0] and "\r" not in wrote and wrote.splitlines()[2:] == lines[2:],
        wrote[:120],
    )

    # ---- ON but not approved: the registry's Approved column against the live state
    REG = os.path.join(d, "FEATURES.md")
    with open(REG, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "| Term | Meaning |\n|---|---|\n| Approved | Y owner decided |\n\n"
            "| Feature | Keys | What | Default | Enable | Cost | Approved |\n|---|---|---|---|---|---|---|\n"
            "| Perch shadow plugin | `--perch-model` | x | OFF | y | z | pending |\n"
            "| Clip audio | `--clip-audio` | x | OFF | y | z | N |\n"
            "| Mixscore plugin | `--mixscore` | x | OFF | y | z | pending |\n"
            "| Clip PCEN spectrogram | `--clip-pcen` | x | OFF | y | z | Y |\n"
            "\n| Feature | Keys | What | Default | Enable | Cost | Approved |\n|---|---|---|---|---|---|---|\n"
            "| DSP plugin | `--no dsp` | x | ON | y | z | pending |\n"
        )
    ap_ = FS.load_approved(REG)
    ck(
        "registry: Approved cell per feature from every table, header and divider rows skipped, the Term/Meaning table ignored",
        ap_ == {"Perch shadow plugin": "pending", "Clip audio": "N", "Mixscore plugin": "pending", "Clip PCEN spectrogram": "Y", "DSP plugin": "pending"},
        str(ap_),
    )
    # status.json: perch ON (pending), mixscore OFF (pending: no warning), clip audio ON (N), clip pcen via args ON (Y), dsp ON (pending)
    put(r, "analyzer_args.txt", "--clip-pcen\n")
    put(r, "enable_clip_audio.txt", "")
    put(r, "analysis/status.json", json.dumps({**STATUS, "clips": {"enabled": True, "audio": True}}))
    rows_w = FS.collect(r)[1]
    w = FS.approval_warnings(rows_w, ap_)
    ck(
        "warnings: exact lines, pending first then N; OFF+pending (Mixscore) and ON+Y (Clip PCEN) are not named",
        w == ["WARNING ON but Approved=pending (2): Perch shadow plugin, DSP plugin", "WARNING ON but Approved=N (1): Clip audio"],
        str(w),
    )
    ck("warnings: positive twin, when every ON feature is approved there is no line", FS.approval_warnings(rows_w, {k: "Y" for k in ap_}) == [])
    ck("warnings: a feature missing from the registry is not named", FS.approval_warnings(rows_w, {}) == [])
    rc, out, err = run(["--root", r, "--registry", REG])
    ol = out.splitlines()
    ck("CLI: the warning lines follow the second line, before the rows", rc == 0 and ol[2:4] == w and ol[4].split()[0] == FS.OFF, str(ol[:5]))
    rc, js, _ = run(["--root", r, "--registry", REG, "--json"])
    ck("--json: the warnings are listed", json.loads(js)["warnings"] == w, js[:200])
    with open(REG, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            "| Feature | Keys | What | Default | Enable | Cost | Approved |\n|---|---|---|---|---|---|---|\n| Perch shadow plugin | `--perch-model` | x | OFF | y | z | Y |\n"
        )
    rc, out, _ = run(["--root", r, "--registry", REG])
    ck("CLI positive twin: an approved registry prints no warning line", rc == 0 and not any(l.startswith("WARNING") for l in out.splitlines()), out[:300])

    # ---- never fails
    rc, out, err = run(["--root", os.path.join(d, "does_not_exist")])
    ck(
        "a missing root still exits 0 and prints the count line",
        rc == 0 and out.splitlines()[0].endswith("UNKNOWN") and "features OFF" in out.splitlines()[0],
        out[:80],
    )
    saved = dict(FS.FEATURES)
    calls = []

    def boom(c):
        calls.append("boom")
        raise ValueError("secret detail must not leak")

    FS.FEATURES["--mixscore"] = ("Mixscore plugin", "off", boom)
    s = states(r)
    FS.FEATURES.clear()
    FS.FEATURES.update(saved)
    ck(
        "a failing detector is one UNKNOWN row naming only the exception class (recorded: it ran once)",
        s["Mixscore plugin"] == (FS.UNKNOWN, "detector failed: ValueError") and calls == ["boom"],
        str(s["Mixscore plugin"]),
    )
    ck("positive twin: the other rows are still computed", s["DSP plugin"][0] == FS.ON)

print(f"\n{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
