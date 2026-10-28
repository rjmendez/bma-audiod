#!/usr/bin/env python3
"""Hermetic tests for the analysis plugins and host, with SYNTHETIC native-48 kHz signals (no files, no models,
no network). Run in the audioeval venv:  python test_analysis.py
Checks: dsp levels/transients/tonal tracker, spatial dominance + directional state machine, novelty (warmup,
inlier vs outlier, persistence), the host end to end over UDP (rings aligned across slots, JSONL written,
status.json, NO audio file written, gap handling)."""

import json, os, socket, struct, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import plugins as P  # noqa: E402

FS = 48000
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


rng = np.random.default_rng(1)


def noise(n, level_db=-60):
    return (rng.standard_normal((n, 8)) * 10 ** (level_db / 20)).astype(np.float32)


# ---- DSP: levels, tonal line appearance/disappearance, transient
d = P.DSPMonitor()
t = 0.0
tone = np.sin(2 * np.pi * 978.0 * np.arange(FS) / FS).astype(np.float32)[:, None] * 10 ** (-45 / 20)
recs = []
for sec in range(60):
    x = noise(FS)
    if 5 <= sec < 25:
        x = x + tone
    if sec == 12:  # 100 ms broadband burst, +25 dB
        x[20000:24800] += noise(4800, -35)
    t += 1.0  # window END time, as the host passes it
    r, _ = d.process(x, t)
    recs += r
dsp = [r for r in recs if r["k"] == "dsp"]
check("dsp: rms of -60 dBFS noise reads about -60", all(abs(np.mean(r["rms_dbfs"]) + 60) < 1.5 for r in dsp[:4]), str(dsp[0]["rms_dbfs"][:2]))
check("dsp: 60 one-second records", len(dsp) == 60, str(len(dsp)))
tr = [r for r in recs if r["k"] == "transient"]
check("dsp: the burst at t=12 s is flagged as a transient", any(12.0 <= r["t"] <= 13.0 for r in tr), str([round(r["t"], 1) for r in tr][:6]))
check("dsp: a sustained 15 dB step is one onset, not a storm", len(tr) <= 4, str(len(tr)))
app = [r for r in recs if r["k"] == "tonal_appeared"]
gone = [r for r in recs if r["k"] == "tonal_gone"]
check("dsp: 978 Hz line announced", any(abs(r["hz"] - 978) < 6 for r in app), str(app))
check("dsp: line announced AFTER it started (>=3 windows) and reported gone after it stopped", any(abs(r["hz"] - 978) < 6 for r in gone), str(gone))

# ---- DSP tonal mask: the unit's own electronic line (12000.0 Hz) is flagged and silent, a real tone is not
tone12k = np.sin(2 * np.pi * 12000.0 * np.arange(FS) / FS).astype(np.float32)[:, None] * 10 ** (-45 / 20)
wins = []
for sec in range(60):  # same length as the tracker test above: announce at 30 s, gone at 50 s
    x = noise(FS)
    if 5 <= sec < 25:
        x = x + tone + tone12k  # 978 Hz (real) and 12000 Hz (electronic) together
    wins.append(x)


def run_dsp(mon):
    out_ = []
    for i, w in enumerate(wins):
        out_ += mon.process(w, float(i + 1))[0]
    return out_


def lines_in_state(rs, lo, hi):
    return [ln for r in rs if r["k"] == "tonal_state" for ln in r["lines"] if lo <= ln["hz"] <= hi]


mask_dir = tempfile.mkdtemp()


def write_mask(name, lines, tol=4.0):
    p = os.path.join(mask_dir, name)
    with open(p, "w") as f:
        json.dump({"version": 1, "lines_hz": lines, "tolerance_hz": tol}, f)
    return p


plain = run_dsp(P.DSPMonitor())
check(
    "mask baseline: without a mask both lines are announced",
    any(abs(r["hz"] - 12000) < 6 for r in plain if r["k"] == "tonal_appeared") and any(abs(r["hz"] - 978) < 6 for r in plain if r["k"] == "tonal_appeared"),
)
check(
    "mask baseline: no tonal_state line carries an 'electronic' key", all("electronic" not in ln for r in plain if r["k"] == "tonal_state" for ln in r["lines"])
)
check(
    "mask: mask_path=None gives output identical to the unmasked monitor (exact JSON)",
    json.dumps(run_dsp(P.DSPMonitor(mask_path=None)), sort_keys=True) == json.dumps(plain, sort_keys=True),
)
masked = run_dsp(P.DSPMonitor(write_mask("m12k.json", [12000.0])))
check("mask: the 12000 Hz line produces no tonal_appeared", not any(abs(r["hz"] - 12000) < 6 for r in masked if r["k"] == "tonal_appeared"))
check("mask: the 12000 Hz line produces no tonal_gone", not any(abs(r["hz"] - 12000) < 6 for r in masked if r["k"] == "tonal_gone"))
check(
    "mask: the 978 Hz line is still announced AND reported gone (positive twin)",
    any(abs(r["hz"] - 978) < 6 for r in masked if r["k"] == "tonal_appeared") and any(abs(r["hz"] - 978) < 6 for r in masked if r["k"] == "tonal_gone"),
)
l12 = lines_in_state(masked, 11990, 12010)
l978 = lines_in_state(masked, 970, 990)
n12, n978 = len(lines_in_state(plain, 11990, 12010)), len(lines_in_state(plain, 970, 990))
check(
    "mask: the masked line is listed in every tonal_state the unmasked run lists it in, all flagged electronic=True",
    len(l12) == n12 and n12 >= 2 and all(ln.get("electronic") is True for ln in l12),
    f"{len(l12)} vs {n12}: {l12[:2]}",
)
check(
    "mask: the unmasked line is listed in the same number of states and carries no electronic key",
    len(l978) == n978 and n978 >= 2 and all("electronic" not in ln for ln in l978),
    f"{len(l978)} vs {n978}: {l978[:2]}",
)
check(
    "mask: every other tonal_state field is unchanged (same keys, same values apart from the flag)",
    [{k: v for k, v in ln.items() if k != "electronic"} for r in masked if r["k"] == "tonal_state" for ln in r["lines"]]
    == [ln for r in plain if r["k"] == "tonal_state" for ln in r["lines"]],
)
far = run_dsp(P.DSPMonitor(write_mask("far.json", [12010.0])))
check(
    "mask: a mask line 10 Hz away (tolerance 4) does NOT mask the tone: it is announced",
    any(abs(r["hz"] - 12000) < 6 for r in far if r["k"] == "tonal_appeared"),
)
near = run_dsp(P.DSPMonitor(write_mask("near.json", [12003.0])))
check("mask: a mask line 3 Hz away (tolerance 4) masks it", not any(abs(r["hz"] - 12000) < 6 for r in near if r["k"] == "tonal_appeared"))
tight = run_dsp(P.DSPMonitor(write_mask("near2.json", [12003.0]), mask_tol_hz=2.0))
check(
    "mask: the constructor tolerance overrides the file's (2 Hz: 3 Hz away is no longer masked)",
    any(abs(r["hz"] - 12000) < 6 for r in tight if r["k"] == "tonal_appeared"),
)

# ---- Spatial: a source that dominates beam 3 -> dominant 3, directional after the baseline settles
s = P.Spatial()
dom = []
for sec in range(60):
    x = noise(FS)
    if sec >= 40:  # source heard mostly in beams 3 (and a bit in 2 and 4)
        src = rng.standard_normal(FS).astype(np.float32) * 10 ** (-40 / 20)
        x[:, 2] += src
        x[:, 1] += 0.5 * src
        x[:, 3] += 0.5 * src
    r, _ = s.process(x, float(sec))
    dom += r
sp = [r for r in dom if r["k"] == "spatial"]
quiet = sp[25:40]
loud = sp[42:]
check("spatial: diffuse noise is not directional", sum(r["directional"] for r in quiet) <= 1, str(sum(r["directional"] for r in quiet)))
check(
    "spatial: the source is directional and in beam 3",
    sum(r["directional"] for r in loud) >= len(loud) - 2 and all(r["dominant_beam"] == 3 for r in loud),
    str([(r["dominant_beam"], r["directional"]) for r in loud[:5]]),
)
check("spatial: azimuth points at beam 3 (90 deg in the ring frame)", all(abs(r["az_deg"] - 90) < 25 for r in loud), str([r["az_deg"] for r in loud[:5]]))
check("spatial: marked uncalibrated", all(r["calibrated"] is False for r in sp))
check("spatial: a source event is emitted", any(r["k"] == "source" for r in dom))

# ---- Novelty
tmp = tempfile.mkdtemp()
nv = P.Novelty(os.path.join(tmp, "n.pkl"), k=4, warmup=30)
centers = rng.standard_normal((3, 64))
out = []
for i in range(150):
    v = centers[i % 3] + 0.05 * rng.standard_normal(64)
    out += nv.add(v.astype(np.float32), float(i))
warm = [r for r in out if r.get("state") == "warming_up"]
check("novelty: warms up on the first 30 embeddings", len(warm) == 30, str(len(warm)))
late = [r for r in out if r["k"] == "novelty" and r.get("novel") is not None and r["t"] > 100]
check("novelty: inliers are not novel", sum(r["novel"] for r in late) <= 2, str(sum(r["novel"] for r in late)))
o2 = []
outlier = rng.standard_normal(64)
for i in range(5):
    o2 += nv.add((outlier + 0.05 * rng.standard_normal(64)).astype(np.float32), 200.0 + i)
check("novelty: a persistent outlier raises novel_event", any(r["k"] == "novel_event" for r in o2), str(o2[-2:]))
nv.save()
check("novelty: state persisted", os.path.getsize(os.path.join(tmp, "n.pkl")) > 100)

# ---- Host end to end over UDP with synthetic 48 kHz beams (dsp + spatial only; no models)
out_dir = tempfile.mkdtemp()
hist_dir = tempfile.mkdtemp()
port = 48391
env = dict(os.environ, PYTHONPATH=os.pathsep.join([HERE]))
proc = subprocess.Popen(
    [
        sys.executable,
        os.path.join(HERE, "host.py"),
        "--listen",
        f"127.0.0.1:{port}",
        "--out",
        out_dir,
        "--history-dir",
        hist_dir,
        "--no",
        "tags",
        "--no",
        "clap",
        "--no",
        "birds",
        "--secs",
        "40",
    ],
    stderr=subprocess.PIPE,
    text=True,
    env=env,
)


def wait_bound(proc, port, deadline_s=180.0):
    """Block until the host owns the UDP port (it binds after every plugin has loaded) or it exits.
    The old test slept a fixed 2 s; the host needs ~6 s idle and 10+ s on a busy machine to import and
    start its plugins, so the first seconds of the stream (including the injected hole) went to a closed port."""
    end = time.time() + deadline_s
    while time.time() < end:
        if proc.poll() is not None:
            return False
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True  # address in use: the host has bound it
        finally:
            probe.close()
        time.sleep(0.1)
    return False


_ready = wait_bound(proc, port)
check("host: bound its UDP port before the stream starts", _ready, f"returncode={proc.poll()}")
sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def bmaa(slot, block, x, flags=0):
    s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
    return b"BMAA" + bytes([1, 0, slot, flags]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()


blk = 0
t_start = time.time()
n_blocks = 0
for sec in range(30):
    x = noise(FS)
    if sec >= 18:
        src = rng.standard_normal(FS).astype(np.float32) * 10 ** (-38 / 20)
        x[:, 5] += src
    for off in range(0, FS, 256):
        for slot in range(8):
            if sec == 8 and slot == 3 and off < 2048:
                continue  # a hole on slot 3 only: that slot's ring must reset, others stay aligned
            sk.sendto(bmaa(slot, blk + off, x[off : off + 256, slot]), ("127.0.0.1", port))
        n_blocks += 1
        due = t_start + n_blocks * 256 / FS  # paced by the stream clock, like the bridge
        d = due - time.time()
        if d > 0:
            time.sleep(d)
    blk += FS
proc.wait(timeout=60)
err = proc.stderr.read()
st = json.load(open(os.path.join(out_dir, "status.json")))
files = sorted(os.listdir(out_dir))
check("host: exited cleanly", proc.returncode == 0, err[-300:])
check("host: dsp and spatial records written", "dsp.jsonl" in files and "spatial.jsonl" in files, str(files))
sp_lines = [json.loads(l) for l in open(os.path.join(out_dir, "spatial.jsonl"))]
dirn = [r for r in sp_lines if r.get("k") == "spatial" and r["directional"]]
check(
    "host: directional windows found in beam 6 once the source starts",
    len(dirn) >= 3 and sum(r["dominant_beam"] == 6 for r in dirn) >= len(dirn) - 1,
    str([(r["dominant_beam"]) for r in dirn][:8]),
)
check("host: status.json says no audio written", st.get("audio_written") is False)
check("host: nothing audio-like on disk", not any(f.endswith((".wav", ".flac", ".raw", ".pcm", ".mp3", ".ogg")) for f in files), str(files))
_cl = [os.path.join(dp, f) for dp, _, fs_ in os.walk(out_dir) for f in fs_ if f.endswith((".wav", ".flac", ".png", ".tmp", ".part"))]
check(
    "host: wildlife clip capture is on by default and, with no detections, stored nothing (status.clips zero, no clip file of any kind, positive twin in test_clips.py)",
    st.get("clips", {}).get("stored") == 0
    and st["clips"]["audio_files"] == 0
    and st["clips"]["audio_bytes"] == 0
    and st["meta"].get("clips_stored") == 0
    and _cl == [],
    str((st.get("clips"), _cl)),
)
check("host: the hole on slot 3 was seen as exactly one gap (the other seven slots stayed contiguous)", st["rx"]["gaps"] == 1, str(st["rx"]))
check("host: dsp processed most seconds", st["plugins"]["dsp"]["windows"] >= 18, str(st["plugins"]["dsp"]))

# ---- metadata history written by the host (history dir, status meta, daily summary, hourly state, config snapshot)
cfg = [json.loads(l) for l in open(os.path.join(hist_dir, "config_history.jsonl"))]
check("history: exactly one analysis_config at start (no change since)", len(cfg) == 1 and cfg[0]["k"] == "analysis_config", str(cfg))
check(
    "history: config says no mask, no calibration file, no geo filter",
    cfg[0]["mask"]["path_set"] is False and cfg[0]["calibration"] == {"source": "absent"} and cfg[0]["geo"]["on"] is False,
    str(cfg[0]),
)
check("history: config run_id is the int start time", isinstance(cfg[0]["run_id"], int) and 0 <= st["started"] - cfg[0]["run_id"] < 60, str(cfg[0]["run_id"]))
_m = st.get("meta", {})
_clip_keys = {k for k in _m if k.startswith(("clip", "audio_", "orphans_", "sweep_"))}
check(
    "status meta: all eight counters plus coherence_errors, indices_errors and bursts_errors present and zero",
    {k: v for k, v in _m.items() if k not in _clip_keys}
    == {
        "write_errors": 0,
        "rotate_fallbacks": 0,
        "taxa_missing": 0,
        "taxa_unmapped": 0,
        "config_write_errors": 0,
        "rollup_errors": 0,
        "daily_flush_errors": 0,
        "history_dropped": 0,
        "coherence_errors": 0,
        "indices_errors": 0,
        "bursts_errors": 0,
    },
    str(_m),
)
check(
    "status meta: the clip counters are present, all zero except the one sweep at start (sweep_runs 1)",
    len(_clip_keys) == 30 and {k: v for k, v in _m.items() if k in _clip_keys and v} == {"sweep_runs": 1},
    str({k: v for k, v in _m.items() if k in _clip_keys and v}),
)
dly = json.load(open(os.path.join(hist_dir, "daily_summary.json")))["days"]
day = time.strftime("%Y%m%d", time.gmtime(st["started"]))
d0 = dly.get(day) or {}
check(
    "daily summary: dsp windows equal status.json dsp windows, one restart, audio seconds = windows",
    d0.get("windows", {}).get("dsp") == st["plugins"]["dsp"]["windows"]
    and d0.get("restarts") == 1
    and d0.get("audio_s") == float(st["plugins"]["dsp"]["windows"]),
    str(d0),
)
roll = json.load(open(os.path.join(hist_dir, "hourly_rollup_state.json")))
closed = [json.loads(l) for l in open(os.path.join(hist_dir, "hourly_rollup.jsonl"))] if os.path.exists(os.path.join(hist_dir, "hourly_rollup.jsonl")) else []
check(
    "hourly rollup: every dsp window is in an open or a closed hour",
    sum(a["w"] for a in roll["open"].values()) + sum(c["windows"] for c in closed) == st["plugins"]["dsp"]["windows"],
    str(roll)[:200],
)
spr = [json.loads(l) for l in open(os.path.join(out_dir, "spatial.jsonl"))]
check("host: spatial rows carry decorr in [0, 1]", all(0.0 <= r["decorr"] <= 1.0 for r in spr if r["k"] == "spatial") and len(spr) > 5)
print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
