#!/usr/bin/env python3
"""Summarize the analysis host's JSONL output over the last N minutes (default 10). Metadata only.
python summarize.py [<data-dir>/analysis] [minutes]   (default: $BMA_DATA_DIR/analysis, ./data/analysis)"""

import collections, glob, json, os, sys, time

d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("BMA_DATA_DIR") or "data", "analysis")
mins = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
now = time.time()
cut = now - mins * 60


def rows(name):
    p = os.path.join(d, name + ".jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for l in f:
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("t", 0) >= cut:
                out.append(r)
    return out


def counter(name, extract, top=8):
    c = collections.Counter()
    for r in rows(name):
        for k in extract(r):
            c[k] += 1
    return c.most_common(top)


st = json.load(open(os.path.join(d, "status.json")))
print(f"host up {st['uptime_s']:.0f}s on {st['device']}; rx {st['rx']}; embeddings stored {st['embeddings_stored']}; audio_written={st['audio_written']}")
print(f"speech fraction (tagger): {st['plugins'].get('tags', {}).get('speech_fraction')}")
print(f"\n== last {mins:g} min ==")
dsp = [r for r in rows("dsp") if r["k"] == "dsp"]
if dsp:
    lv = [sum(r["rms_dbfs"]) / 8 for r in dsp]
    print(f"level: mean {sum(lv) / len(lv):.1f} dBFS, range {min(lv):.1f}..{max(lv):.1f} over {len(dsp)} s")
tr = [r for r in rows("dsp") if r["k"] == "transient"]
print(f"transients: {len(tr)}")
ts = [r for r in rows("dsp") if r["k"] == "tonal_state"]
if ts:
    print("tonal lines now:", [(l["hz"], l["excess_db"]) for l in ts[-1]["lines"]], "| noise floor", ts[-1]["noise_floor_db"], "dB")
ev = [r for r in rows("dsp") if r["k"] in ("tonal_appeared", "tonal_gone")]
for r in ev[-5:]:
    print("  ", r["k"], r["hz"], "Hz")
sp = [r for r in rows("spatial") if r["k"] == "spatial"]
if sp:
    dirn = [r for r in sp if r["directional"]]
    print(f"spatial: {len(dirn)}/{len(sp)} windows directional; dominant beam histogram:", collections.Counter(r["dominant_beam"] for r in sp).most_common(4))
print("AudioSet tags (top-1 per 5 s window):", counter("tags", lambda r: [r["top"][0][0]] if r.get("top") else []))
print("AudioSet tags (any >=0.05):", counter("tags", lambda r: [t[0] for t in r.get("top", [])], 10))
print("CLAP zero-shot (top-1):", counter("clap", lambda r: [r["top"][0][0]] if r.get("top") else []))
brows = rows("birds")
birds = [r for r in brows if r.get("k", "bird") == "bird"]
print("BirdNET species:", collections.Counter(r["common"] for r in birds if not r.get("non_species")).most_common(6))
print("BirdNET non-species classes (Engine, Dog, ...):", collections.Counter(r["common"] for r in birds if r.get("non_species")).most_common(4))
gf = [r for r in brows if r.get("k") == "bird_filtered"]
if gf:
    print(f"BirdNET removed by the location filter: {len(gf)}", collections.Counter(r["common"] for r in gf).most_common(4))
nv = [r for r in rows("novelty") if r["k"] == "novelty" and "score" in r]
if nv:
    print(
        f"novelty: {len(nv)} scored windows, scenes {dict(collections.Counter(r['scene'] for r in nv))}, "
        f"max score {max(r['score'] for r in nv):.2f}, novel {sum(r['novel'] for r in nv)}"
    )
print("novel events:", [(round(r["t"] - now), r["scene"], r["score"]) for r in rows("novelty") if r["k"] == "novel_event"][-5:])


# ---- board / driver side: the board daemon's debug stream (raw-mic levels read on the DSPs BEFORE beamforming,
# meters, @health housekeeping) collected by bma_debug_rx.py, plus the bridge's own status file.
def board_rows():
    p = os.path.join(d, "board_debug.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 8_000_000))  # the window is minutes; the file is weeks
        for l in f.read().decode("utf-8", "replace").splitlines()[1:]:
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("host_t", 0) >= cut:
                out.append(r)
    return out


def f32(h):
    import struct

    try:
        return struct.unpack(">f", bytes.fromhex(h))[0]
    except (TypeError, ValueError, struct.error):
        return None


def med(v):
    v = sorted(v)
    return v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2


print(f"\n== board / driver side, last {mins:g} min ==")
bs_path = os.path.join(os.path.dirname(os.path.abspath(d)), "bridge_status.json")
if os.path.exists(bs_path):
    bs = json.load(open(bs_path))
    print(
        f"bridge: state={bs['state']} {bs['pps']:.0f} pkt/s loss {bs['loss_pct']:.2f}% all-zero blocks {bs['blocks_all_zero_frac']:.2f} "
        f"(status {max(0, now - bs['t']):.0f}s old)"
    )
br = board_rows()
if not br:
    print("board debug: NO records in this window (collector down or board unreachable)")
else:
    mon = [r for r in br if r.get("type") == "monitor" and r.get("rms_dbfs") is not None]
    if mon:
        per = collections.defaultdict(list)
        for r in mon:
            per[r["dsp"]].append(r)
        print(
            "raw mics on the DSPs (pre-beamformer), median rms / max peak dBFS:",
            " ".join(f"dsp{k}={med([r['rms_dbfs'] for r in v]):.1f}/{max(r['peak_dbfs'] for r in v):.1f}" for k, v in sorted(per.items())),
        )
        raw = med([r["rms_dbfs"] for r in mon])
        if dsp:
            beam = med(lv)
            print(f"raw-mic median {raw:.1f} dBFS vs beam-output median {beam:.1f} dBFS (delta {beam - raw:+.1f} dB)")
    hl = [r for r in br if r.get("k") == "health"]
    if hl:
        h = hl[-1]
        tl = h.get("temp_local_c", f32(h.get("temp_local_hex")))
        tr_ = h.get("temp_remote_c", f32(h.get("temp_remote_hex")))
        reboots = sum(1 for a, b in zip(hl, hl[1:]) if b["up"] < a["up"])
        fp = h.get("fpga", {})
        bad = {k: v for k, v in fp.items() if k.startswith(("glink_err", "freq_err")) and v != "0000"}
        print(
            f"board health: up {h['up']:.0f}s load {h['load']} temps {tl}/{tr_} C, D-state {h.get('d_state')}, "
            f"procs {h.get('procs')}, reboots in window {reboots}, link errors {bad or 'none'}"
        )
        print(f"  route: {h.get('route_last')} | safety mute EA={fp.get('ea')}")
    print(f"collector: {len(br)} records, newest {max(0, now - max(r['host_t'] for r in br)):.0f}s old")

_ext = (".wav", ".flac", ".mp3", ".ogg", ".raw", ".pcm", ".opus")
_clips = os.path.join(d, "clips") + os.sep
_all = [f for f in glob.glob(os.path.join(d, "**", "*"), recursive=True) if f.lower().endswith(_ext)]
aud = [f for f in _all if not f.startswith(_clips)]
print(
    f"\naudio files under {d} outside clips/: {len(aud)} (must be 0); wildlife clip audio under clips/ (0 unless --clip-audio; certainty > 0.85, deleted after 24 h): {len(_all) - len(aud)}"
)
