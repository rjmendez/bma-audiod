#!/usr/bin/env python3
"""Regenerate contracts/fixtures from the live pipeline files (read-only on <data-dir>).

    python tools/make_fixtures.py [--root <data-dir>]

Takes the newest records of each kind (a handful per kind, never more than 50 lines per file), sanitizes them
(documentation addresses instead of the real host addresses; shortened NUL run in the health_unparsed line), and
writes the binary examples from the reference implementation. Lines that no live record could supply are built by
hand and listed in fixtures/README.md as SYNTHETIC. Metadata only: no audio, no coordinates."""

import argparse, collections, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CON = os.path.dirname(HERE)
sys.path.insert(0, CON)
import ref_binary as R  # noqa: E402

FIX = os.path.join(CON, "fixtures")
DOC_ADDR = "192.0.2.10"  # RFC 5737 documentation address


def tail_records(path, keyfield, want, cap=12_000_000):
    """newest `want[kind]` records of each kind, in file order."""
    got = collections.defaultdict(list)
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - cap))
        lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        k = r.get(keyfield)
        if k in want:
            got[k].append(r)
    out = []
    for k, n in want.items():
        out += got[k][-n:]
    return sorted(out, key=lambda r: r.get("t", r.get("host_t", 0)) if keyfield == "k" else r.get("host_t", 0))


def write_jsonl(name, recs):
    assert len(recs) <= 50, (name, len(recs))
    with open(os.path.join(FIX, name), "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=True) + "\n")
    return len(recs)


PC_DIR = os.path.join(os.path.dirname(CON), "pc")


def main():
    global PC_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("BMA_DATA_DIR") or "data", help="the data dir (default $BMA_DATA_DIR or ./data)")
    ap.add_argument("--pc-dir", default=PC_DIR, help="directory holding the sink to build beam_bands fixtures from (needs BandMeter)")
    a = ap.parse_args()
    PC_DIR = a.pc_dir
    A = os.path.join(a.root, "analysis")
    os.makedirs(FIX, exist_ok=True)
    n = {}
    n["dsp.jsonl"] = write_jsonl(
        "dsp.jsonl", tail_records(os.path.join(A, "dsp.jsonl"), "k", {"dsp": 8, "transient": 3, "tonal_appeared": 3, "tonal_gone": 3, "tonal_state": 3})
    )
    n["spatial.jsonl"] = write_jsonl("spatial.jsonl", tail_records(os.path.join(A, "spatial.jsonl"), "k", {"spatial": 8, "source": 2}))
    n["tags.jsonl"] = write_jsonl("tags.jsonl", tail_records(os.path.join(A, "tags.jsonl"), "k", {"tags": 10}))
    n["clap.jsonl"] = write_jsonl("clap.jsonl", tail_records(os.path.join(A, "clap.jsonl"), "k", {"clap": 6}))
    n["birds.jsonl"] = write_jsonl("birds.jsonl", tail_records(os.path.join(A, "birds.jsonl"), "k", {"bird": 6, "bird_filtered": 3}))
    nov = tail_records(os.path.join(A, "novelty.jsonl"), "k", {"novelty": 8, "novel_event": 2})
    nov.append({"k": "novelty", "t": 1792880000.5, "state": "warming_up", "seen": 12})  # SYNTHETIC (producer shape, plugins.py)
    n["novelty.jsonl"] = write_jsonl("novelty.jsonl", nov)
    bd = tail_records(os.path.join(A, "board_debug.jsonl"), "type", {"meter": 6, "health": 3, "status": 3, "health_unparsed": 1})
    mon = [r for r in tail_records(os.path.join(A, "board_debug.jsonl"), "type", {"monitor": 60}) if r.get("dsp") in (4, 9)][-14:]
    mon.append({"type": "monitor", "seq": 7, "line": "garbled line", "host_t": 1792880000.1})  # SYNTHETIC raw shape (bma_debug_rx.py parse_monitor)
    for r in bd:
        if r["type"] == "health_unparsed":  # keep the shape, shorten the real NUL run
            r["line"] = "@health " + "\x00" * 16 + r["line"].split("\x00")[-1][:60]
    h0 = next(r for r in bd if r["type"] == "health")
    bd.append({"type": "status", "json": '{"state":"starting"}', "host_t": h0["host_t"] - 100})  # SYNTHETIC copy of the real 2026-10-11 startup status
    bd.append(
        dict(h0, temp_local_hex="", temp_remote_hex="", temp_local_c=None, temp_remote_c=None, seq=h0["seq"] + 1, host_t=h0["host_t"] + 1)
    )  # SYNTHETIC: real 2026-10-11 shape with unreadable sensors
    n["board_debug.jsonl"] = write_jsonl("board_debug.jsonl", sorted(bd + mon, key=lambda r: r["host_t"]))
    for name, src in (("analysis_status.json", os.path.join(A, "status.json")), ("bridge_status.json", os.path.join(a.root, "bridge_status.json"))):
        d = json.load(open(src))
        if name == "bridge_status.json":
            d["targets"] = [f"{DOC_ADDR}:{t.rsplit(':', 1)[1]}" for t in d["targets"]]
        with open(os.path.join(FIX, name), "w", newline="\n") as f:
            json.dump(d, f, indent=1)
            f.write("\n")
    sink = {
        "t": 1792884000.0,
        "device": "CABLE In 16ch (VB-Audio Virtual Cable)",
        "audio_output": True,
        "device_error": None,
        "datagrams": 144000,
        "bad": 0,
        "rx_errors": 0,
        "buffered_ms": 151,
        "underruns": 0,
        "slips": 2,
        "slipped_frames": 14400,
        "missing_slot_blocks": 0,
        "holes": 41,
        "playing": True,
        "data_frames": 141,
        "data_dropped": 0,
    }  # SYNTHETIC (shape from bma_cable_sink.py)
    metering = dict(
        sink,
        device=None,
        audio_output=False,
        playing=False,
        buffered_ms=0,
        device_error="'CABLE In 16ch (VB-Audio Virtual Cable)' is configured for 2 channel(s), need 16: set its default format "
        "to 16 channels, 24 bit, 48000 Hz",
    )  # SYNTHETIC metering-only mode (endpoint still stereo)
    for name, obj in (("sink_status.json", sink), ("sink_status_metering_only.json", metering)):
        with open(os.path.join(FIX, name), "w", newline="\n") as f:
            json.dump(obj, f, indent=1)
            f.write("\n")
    # ---- beam_bands records, produced by the production BandMeter from known synthetic signals (needs numpy and the sink with BandMeter)
    bands = []
    try:
        sys.path.insert(0, PC_DIR)
        import numpy as np
        import bma_cable_sink as SK

        t = np.arange(48000) / 48000.0
        z = np.zeros(48000)
        rng = np.random.default_rng(7)
        signals = [
            ("silence", [z] * 8),
            ("700 Hz sine A=0.1 on beam 3: db[2][3] == -23.0", [z, z, 0.1 * np.sin(2 * np.pi * 700 * t)] + [z] * 5),
            ("full-scale 700 Hz sine on beam 1: db[0][3] == -3.0", [np.sin(2 * np.pi * 700 * t)] + [z] * 7),
            ("low-level noise on all beams", [rng.standard_normal(48000) * 10 ** (-70 / 20) for _ in range(8)]),
            ("half the window zero (zero_frac 0.5)", [np.concatenate([np.zeros(24000), 0.01 * np.sin(2 * np.pi * 300 * t[:24000])])] * 8),
        ]
        for i, (_, sig) in enumerate(signals):
            m = SK.BandMeter(clock_us=lambda i=i: 1792886495000000 + i * 1000000)
            bands += m.push(284267690 + i * 48000, np.stack(sig, axis=1).astype(np.float32))
        write_jsonl("beam_bands.jsonl", [json.loads(json.dumps(r)) for r in bands])
        n["beam_bands.jsonl"] = len(bands)
    except (ImportError, AttributeError) as e:
        print("beam_bands.jsonl NOT regenerated (needs the sink with BandMeter via --pc-dir):", e, file=sys.stderr)
    # ---- binary examples (built by the reference implementation, then frozen as hex)
    ex = []
    cases = [
        ("plain block, slot 0", 0, 0, 7, 284267690, [0, 1, -1, 8388607, -8388608, 100, -100, 12345]),
        ("zero block (flags bit0)", 3, 1, 8, 284267946, [0] * 8),
        ("block after a hole (flags bit1 gap_before)", 5, 2, 9, 284268202, [-2048, 2048, 0, 7, -7, 1, -1, 3]),
        ("both flags, seq and block wrap at 2**32", 7, 3, 0xFFFFFFFF, 0xFFFFFF00, [5, -5, 4, -4]),
        ("empty payload", 1, 0, 1, 1000, []),
    ]
    for name, slot, flags, seq, block, smp in cases:
        pkt = R.build_bmaa(slot, flags, seq, block, smp)
        ex.append({"name": name, "slot": slot, "flags": flags, "seq": seq, "block": block, "samples": smp, "hex": pkt.hex()})
    bad = [
        ("bad magic", "58585858" + ex[0]["hex"][8:], "magic"),
        ("bad version", ex[0]["hex"][:8] + "02" + ex[0]["hex"][10:], "version"),
        ("slot 8", ex[0]["hex"][:12] + "08" + ex[0]["hex"][14:], "slot"),
        ("truncated body", ex[0]["hex"][:-8], "length"),
        ("reserved byte set", ex[0]["hex"][:10] + "01" + ex[0]["hex"][12:], "reserved"),
        ("header only 19 bytes", ex[0]["hex"][:38], "short"),
    ]
    with open(os.path.join(FIX, "bmaa_examples.json"), "w", newline="\n") as f:
        json.dump({"valid": ex, "invalid": [{"name": n_, "hex": h, "code": c} for n_, h, c in bad]}, f, indent=1)
        f.write("\n")
    board_line = json.dumps(mon[0], separators=(",", ":")).encode()
    frames = [
        ("anchor", R.K_ANCHOR, R.anchor_payload(284267690, 1792886495588944)),
        ("board record", R.K_BOARD, board_line),
        ("bridge status", R.K_BRIDGE, json.dumps(json.load(open(os.path.join(FIX, "bridge_status.json"))), separators=(",", ":")).encode()),
    ]
    bl = os.path.join(FIX, "beam_bands.jsonl")
    if os.path.exists(bl):
        frames.append(("beam bands", R.K_BANDS, json.dumps(json.loads(open(bl).readline()), separators=(",", ":")).encode()))
    with open(os.path.join(FIX, "cable_frames.json"), "w", newline="\n") as f:
        json.dump({"frames": [{"name": nm, "kind": k, "payload_hex": p.hex(), "frame_hex": R.encode_frame(k, p).hex()} for nm, k, p in frames]}, f, indent=1)
        f.write("\n")
    cases = []
    for index in ((3 << 24) | 0x123456, 0xFFFF8001, 0x00018000, 0x7FFF7FFF, 1, 0xFFFFFFFF, 0, 0xFF000FFF):
        cols = R.index_columns(index)
        cases.append({"index": index, "columns": list(cols), "from_columns": R.index_from_columns(*cols)})
    with open(os.path.join(FIX, "cable_columns.json"), "w", newline="\n") as f:
        json.dump(
            {
                "note": "cable-stream 2.0.0: the u32 card index split 12/12/8 bits onto channels 9, 10, 11; each field u is the "
                "column count (u - 2048) << 8. Index 0 is a legal row (its columns are -524288 each); the underrun "
                "marker is all three columns exactly 0 (u = 2048 on channel 11 is not a valid index byte)",
                "cases": cases,
            },
            f,
            indent=1,
        )
        f.write("\n")
    print(json.dumps(n))


if __name__ == "__main__":
    main()
