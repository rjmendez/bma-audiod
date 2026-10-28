#!/usr/bin/env python3
"""Tests for tools/daily_digest.py: temp data dirs, exact output lines, a positive twin for every omission. No stubs.   python -I test_daily_digest.py"""

import contextlib, io, json, os, re, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import daily_digest as D  # noqa: E402

n_checks, fails = 0, []
DAY = "20260615"
LO, HI = D.day_bounds(DAY)
HOUR3, HOUR14 = LO + 3 * 3600 + 1800, LO + 14 * 3600 + 1800


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail)[:400] + "]"))
    if not cond:
        fails.append(name)


def put(root, rel, rows, raw=False):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write((r if raw else json.dumps(r)) + "\n")


def bird(t, common, sci, group, **kw):
    o = {"k": "bird", "t": t, "beam": 3, "level_dbfs": -50.0, "common": common, "scientific": sci, "confidence": 0.9, "group": group, "non_species": False}
    o.update(kw)
    return o


def flags(level):
    return {"v": 1, "flags": [], "level": level, "evidence": {}}


def lines(root, **kw):
    return D.build(root, DAY, now=LO + 7200.0, **kw).splitlines()


BIRDS = [
    bird(LO, "Northern Cardinal", "Cardinalis cardinalis", "bird"),  # first second of the day: inside
    bird(LO + 100, "Northern Cardinal", "Cardinalis cardinalis", "bird"),
    bird(LO + 200, "Carolina Chickadee", "Poecile carolinensis", "bird"),
    bird(LO + 300, "Katydid sp", "Tettigoniidae", "insect"),
    bird(LO + 400, "Human vocal", "Homo sapiens", "other-bio"),  # human label: not counted
    bird(LO + 500, "Human non-vocal", "", "other-bio"),  # human label: not counted
    bird(LO + 600, "Speech", "", "mechanical"),  # speech label: not counted
    bird(HI, "Northern Cardinal", "Cardinalis cardinalis", "bird"),  # first second of the next day: outside
    bird(LO - 1, "Northern Cardinal", "Cardinalis cardinalis", "bird"),  # last second of the previous day: outside
]


def make_root(tmp, name, birds=BIRDS, loss=True, bridge=True, clips=True, perch=True, status=True):
    root = os.path.join(tmp, name)
    os.makedirs(root)
    put(root, "analysis/birds.jsonl", birds)
    if loss:
        rec = lambda t, v: {"k": "loss", "v": 1, "t": t, "restart": False, "bridge": {"loss_pct": v, "reason": None if v is not None else "stale"}}
        put(
            root,
            "history/loss.jsonl",
            [
                rec(HOUR3, 0.2),
                rec(HOUR3 + 30, 0.4),
                rec(HOUR14, 1.0),
                rec(HOUR14 + 30, 2.0),
                rec(HOUR14 + 60, None),
                rec(HI, 50.0),
                rec(LO - 1, 70.0),
                rec(LO, 0.6),
            ],
        )
    if bridge:
        with open(os.path.join(root, "bridge_status.json"), "w") as f:
            json.dump({"t": LO, "loss_pct": 0.75, "pkts": 1}, f)
    if clips:
        put(
            root,
            "analysis/clips/index.jsonl",
            [
                {"k": "clip_stored", "t_start": LO + 10, "audio_stored": True},
                {"k": "clip_stored", "t_start": LO + 20, "audio_stored": False},
                {"k": "clip_stored", "t_start": HI, "audio_stored": True},
                {"k": "clip_stored", "t_start": LO, "audio_stored": False},
                {"k": "clip_evicted", "t": LO + 30, "reason": "human_voice"},
                {"k": "clip_evicted", "t": LO + 40, "reason": "species_hour_cap"},
                {"k": "clip_audio_expired", "t": LO + 50},
            ],
        )
    if perch:
        pr = lambda t, sp: {"k": "perch", "v": 1, "t": t, "src": "loudest", "beam": 3, "top": [[sp, 8.0]], "best_species": [sp, 8.0], "top1_group": "bird"}
        put(
            root,
            "analysis/perch.jsonl",
            [{"k": "perch_agreement", "t": LO + 1, "min_logit": 5.0}, pr(LO + 1, "Cardinalis cardinalis"), pr(LO + 201, "Cardinalis cardinalis")],
        )
    if status:
        with open(os.path.join(root, "analysis", "status.json"), "w") as f:
            json.dump({"started": LO, "uptime_s": 7200, "plugins": {"dsp": {"errors": 2}, "tags": {"errors": 1}, "birds": {"errors": 0}, "x": "bad"}}, f)
    return root


with tempfile.TemporaryDirectory(prefix="bma_dg_") as tmp:
    root = make_root(tmp, "full")
    L1 = lines(root)
    check("header", L1[0] == "bma digest 20260615", L1[0])
    check("detections by group exclude the human and speech labels and the other days, sorted by count", L1[1] == "detections 4 (bird 3, insect 1)", L1[1])
    check(
        "top species: counts, ordered by count then name, no artifact column without artifact_flags",
        L1[2:5]
        == [
            "species Northern Cardinal (Cardinalis cardinalis) n=2",
            "species Carolina Chickadee (Poecile carolinensis) n=1",
            "species Katydid sp (Tettigoniidae) n=1",
        ],
        L1[2:5],
    )
    check("no human label in the detection or species lines", not re.search(r"(?i)human|speech|homo", " ".join(L1[1:5])), L1[1:5])
    check(
        "loss from history/loss.jsonl: mean of 5 samples (one at the first second of the day), worst local hour, null and neighbouring-day samples skipped",
        L1[5] == "loss mean 0.84 pct, worst hour 14 1.50 pct, samples 5",
        L1[5],
    )
    check(
        "clips: stored 3 (1 with audio), purged human voice 1, other evictions 1, next-day clip not counted",
        L1[6] == "clips stored 3 (audio 1), purged human voice 1, evicted other 1",
        L1[6],
    )
    check(
        "perch headline: 2 windows, both compared, 1 same species (Cardinal vs Cardinal; Cardinal vs Chickadee)",
        L1[7] == "perch windows 2, compared 2, same species 1, agreement 50.0 pct",
        L1[7],
    )
    check(
        "health: analyzer uptime hours, plugin errors summed, disk free of the data dir's drive",
        re.fullmatch(r"health analyzer_uptime_h 2\.0, plugin_errors 3, disk_free_gb \d+\.\d", L1[8]) is not None,
        L1[8],
    )
    check("exactly 9 lines", len(L1) == 9, len(L1))

    # artifact share: only for species whose records carry artifact_flags, only when any record does
    af = [
        bird(LO + 1, "Katydid sp", "Tettigoniidae", "insect", artifact_flags=flags("likely")),
        bird(LO + 2, "Katydid sp", "Tettigoniidae", "insect", artifact_flags=flags("suspect")),
        bird(LO + 3, "Katydid sp", "Tettigoniidae", "insect", artifact_flags=flags("none")),
        bird(LO + 4, "Katydid sp", "Tettigoniidae", "insect", artifact_flags=flags("none")),
        bird(LO + 5, "Northern Cardinal", "Cardinalis cardinalis", "bird"),
    ]
    r2 = make_root(tmp, "af", birds=af)
    L2 = lines(r2)
    check(
        "artifact share: 2 of 4 flagged (likely + suspect) = 50%; a species without the field shows no share",
        L2[2:4] == ["species Katydid sp (Tettigoniidae) n=4 artifact 50%", "species Northern Cardinal (Cardinalis cardinalis) n=1"],
        L2[2:4],
    )
    check("positive twin: the same species without artifact_flags has no artifact column", "artifact" not in "\n".join(L1))

    # loss fallback and its positive twin
    r3 = make_root(tmp, "noloss", loss=False)
    check(
        "no history/loss.jsonl: the bridge_status loss is reported and labelled as the last interval",
        lines(r3)[5] == "loss now 0.75 pct (bridge_status, last interval)",
        lines(r3)[5],
    )
    r3b = make_root(tmp, "noloss_nobridge", loss=False, bridge=False)
    check("no loss.jsonl and no bridge_status: loss unknown", lines(r3b)[5] == "loss unknown", lines(r3b)[5])
    check("positive twin: with loss.jsonl present, bridge_status is not used", "bridge_status" not in L1[5])

    # perch omitted without perch.jsonl; empty perch compares nothing
    r4 = make_root(tmp, "noperch", perch=False)
    L4 = lines(r4)
    check("no perch.jsonl: no perch line at all", not any(x.startswith("perch") for x in L4) and len(L4) == 8, L4)
    put(r4, "analysis/perch.jsonl", [])
    check(
        "empty perch.jsonl: the line says 0 windows, compared 0", [x for x in lines(r4) if x.startswith("perch")] == ["perch windows 0, compared 0"], lines(r4)
    )

    # health unknowns
    r5 = make_root(tmp, "nostatus", status=False)
    check(
        "no status.json: uptime and plugin errors unknown",
        re.fullmatch(r"health analyzer_uptime_h unknown, plugin_errors unknown, disk_free_gb \d+\.\d", lines(r5)[-1]) is not None,
        lines(r5)[-1],
    )
    with open(os.path.join(r5, "analysis", "status.json"), "w") as f:
        json.dump({"started": LO - 3600}, f)
    check(
        "status.json without uptime_s: uptime from started and the clock",
        lines(r5)[-1].startswith("health analyzer_uptime_h 3.0, plugin_errors unknown"),
        lines(r5)[-1],
    )

    # rotated generation, hostile lines, top cap, ASCII
    r6 = os.path.join(tmp, "rot")
    os.makedirs(r6)
    put(r6, "analysis/birds.jsonl.1", [bird(LO + 1, "Zorro", "Zorro zorro", "bird"), bird(LO + 3, "Zorro", "Zorro zorro", "bird")])
    put(
        r6,
        "analysis/birds.jsonl",
        ["not json", "[1,2]", json.dumps(bird(LO + 2, "Müller's Warbler", "Mueller warbler", "bird")), json.dumps(bird("x", "A", "A a", "bird"))],
        raw=True,
    )
    L6 = lines(r6, top=1)
    check(
        "the .1 generation is read; bad lines and a non-numeric t are skipped; --top caps the species lines; non-ASCII is folded",
        L6[1:3] == ["detections 3 (bird 3)", "species Zorro (Zorro zorro) n=2"] and len([x for x in L6 if x.startswith("species")]) == 1,
        L6,
    )
    txt = D.build(r6, DAY, top=5)
    check("ASCII output with the folded name", txt.isascii() and "species Muller's Warbler (Mueller warbler) n=1" in txt, txt)
    check("positive twin: top 5 lists both species", len([x for x in txt.splitlines() if x.startswith("species")]) == 2)

    # privacy: no path, no coordinate-looking pair, no host name in the output
    check("no data-dir path in the output", root not in "\n".join(L1) and tmp not in "\n".join(L1))
    check("no lat/lon-like decimal pairs in the output", re.search(r"-?\d{1,3}\.\d{3,}\s*[, ]\s*-?\d{1,3}\.\d{3,}", "\n".join(L1)) is None)

    # CLI: writes <root>/reports/digest/<day>.txt, LF, same as stdout, exit codes
    env = dict(os.environ)
    r = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "daily_digest.py"), "--root", root, "--day", DAY], capture_output=True, text=True, timeout=120, env=env
    )
    dest = os.path.join(root, "reports", "digest", DAY + ".txt")
    data = open(dest, "rb").read() if os.path.exists(dest) else b""
    check(
        "CLI: exit 0, file reports/digest/<day>.txt equals stdout, LF only, ASCII",
        r.returncode == 0
        and data.decode("ascii") == r.stdout.replace("\r\n", "\n")
        and b"\r" not in data
        and data.decode().splitlines()[1] == "detections 4 (bird 3, insect 1)",
        (r.returncode, r.stderr),
    )
    check("CLI: no .tmp file left behind", os.listdir(os.path.dirname(dest)) == [DAY + ".txt"], os.listdir(os.path.dirname(dest)))
    r = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "daily_digest.py"), "--root", root, "--day", "2026-06-15"], capture_output=True, text=True, timeout=120
    )
    check("CLI: a malformed --day is exit 2 with one message", (r.returncode, r.stderr.strip()) == (2, "--day must be YYYYMMDD"), (r.returncode, r.stderr))
    r = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "daily_digest.py"), "--root", os.path.join(tmp, "nope"), "--day", DAY],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check("CLI: a missing root is exit 2", (r.returncode, r.stderr.strip()) == (2, "--root is not a directory"), (r.returncode, r.stderr))
    out2 = os.path.join(tmp, "elsewhere")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = D.main(["--root", root, "--day", DAY, "--out", out2])
    check("--out redirects the file; default --day is yesterday", rc == 0 and os.path.exists(os.path.join(out2, DAY + ".txt")), rc)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        D.main(["--root", root, "--out", out2])
    import datetime

    yday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
    check("no --day: the file is named for yesterday (local)", os.path.exists(os.path.join(out2, yday + ".txt")), os.listdir(out2))
    check(
        "no --day: header says yesterday",
        buf.getvalue().startswith("bma digest %s\n" % yday)
        or buf.getvalue().startswith("bma digest %s\n" % (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")),
        buf.getvalue()[:30],
    )

# ---- features that are ON while the registry says pending or N
with tempfile.TemporaryDirectory() as tmp_f:
    rf = os.path.join(tmp_f, "root")
    put(
        rf,
        "analysis/status.json",
        [{"plugins": {"perch": {"enabled": True, "device": "cuda:1"}, "dsp": {"enabled": True}}, "clips": {"enabled": True, "audio": True}, "uptime_s": 3600}],
    )
    reg = os.path.join(tmp_f, "FEATURES.md")
    head = "| Feature | Keys | What | Default | Enable | Cost | Approved |\n|---|---|---|---|---|---|---|\n"
    with open(reg, "w", encoding="utf-8", newline="\n") as f:
        f.write(
            head
            + "| Perch shadow plugin | `--perch-model` | x | OFF | y | z | pending |\n| Clip audio | `--clip-audio` | x | OFF | y | z | N |\n| DSP plugin | `--no dsp` | x | ON | y | z | Y |\n"
        )
    got = D.build(rf, DAY, now=LO + 7200.0, registry=reg).splitlines()
    check(
        "digest: the two warning lines come right before the health line (perch ON + pending, clip audio ON + N; DSP ON + Y is not named)",
        got[-3:-1] == ["WARNING ON but Approved=pending (1): Perch shadow plugin", "WARNING ON but Approved=N (1): Clip audio"]
        and got[-1].startswith("health "),
        got[-4:],
    )
    with open(reg, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + "| Perch shadow plugin | `--perch-model` | x | OFF | y | z | Y |\n| Clip audio | `--clip-audio` | x | OFF | y | z | Y |\n")
    got2 = D.build(rf, DAY, now=LO + 7200.0, registry=reg).splitlines()
    check(
        "digest positive twin: with both rows approved there is no WARNING line and the health line is last",
        not any(x.startswith("WARNING") for x in got2) and got2[-1].startswith("health "),
        got2[-3:],
    )

print("\n%d checks, %d failure(s)" % (n_checks, len(fails)))
sys.exit(1 if fails else 0)
