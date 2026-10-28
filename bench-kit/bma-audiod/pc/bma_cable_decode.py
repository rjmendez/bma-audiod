#!/usr/bin/env python3
"""Decode a recording of the BMA cable stream (16 ch, see bma_cable_sink.py): the card sample-index track, the
wall-clock anchors, and the control-channel debug records, each placed on the card's sample index.

    python bma_cable_decode.py recording.wav [--out records.jsonl]

Cable-stream 2.0.0: channels 9-13 are 12-bit symbol channels, u in 0..4095 carried as the count (u - 2048) << 8
(a 256-count grid, peak 0.0625 full scale); the reader rounds, u = ((count + 128) >> 8) + 2048, so a path that loses
up to 7 low bits or disturbs a sample by -128..+127 counts still decodes exactly (the window is asymmetric). A sample further than IMPOSSIBLE_TOL counts from the
grid, a symbol outside 0..4095, an index byte above 255 and non-zero reserved channels are counted and printed, never
hidden. Any recording that keeps about 14 bits carries the symbol channels; 16-bit PCM does, but a recorder that dithers at 16 bits
has no margin. Exit codes: 0 clean, 1 unusable channels/rate, 3 impossible values / reserved channels / corrupt frames, 4 unreadable
file, 5 narrower than 16 bits."""

import argparse, json, os, struct, sys, zlib

import numpy as np

MAGIC = b"BMD1"
K_ANCHOR, K_BOARD, K_BRIDGE, K_BANDS = 1, 2, 3, 4
RATE = 48000


IMPOSSIBLE_TOL = 16  # |residual| above this (of 128 = half a grid step) cannot be a clean symbol; it is counted and reported
ZERO = 2048


def sym12(col):
    """Counts (int64) -> (symbol u, residual). u = ((counts + 128) >> 8) + 2048 (rounding: exact for an untouched sample
    and for any disturbance of less than 128 counts); residual = counts - ((u - 2048) << 8), in [-128, 127]."""
    c = np.asarray(col, dtype=np.int64)
    r = (c + 128) >> 8
    return r + ZERO, c - (r << 8)


def parse_frames(buf):
    """-> (list of (kind, payload), number of corrupt frames skipped). Idle zero bytes between frames are ignored."""
    out, bad, i = [], 0, 0
    while True:
        i = buf.find(MAGIC, i)
        if i < 0 or i + 7 > len(buf):
            return out, bad
        kind, ln = struct.unpack_from(">BH", buf, i + 4)
        end = i + 7 + ln + 4
        if end <= len(buf):
            body = buf[i + 4 : i + 7 + ln]
            (crc,) = struct.unpack_from(">I", buf, i + 7 + ln)
            if zlib.crc32(body) & 0xFFFFFFFF == crc:
                out.append((kind, buf[i + 7 : i + 7 + ln]))
                i = end
                continue
        bad += 1
        i += 1


def decode(k):
    """k: int64 array (n, >=16) of signed 24-bit sample counts (float * 2^23, rounded).
    -> dict(frames [(kind, payload)], index, silent, jumps, anchors, board, bridge, bands, bad_frames, impossible,
    impossible_first, range_errors, max_residual, reserved_nonzero); board/bridge records are parsed JSON with an added `card_index`
    (their host_t / t placed on the card's sample index through the anchors). `index` is the u32 from channels 9-11
    (0 on silent rows); it wraps at 2^32 and `jumps` treats the wrap as a normal step. `silent` rows are those whose
    three index channels are all-zero samples (the silent-row marker; index 0 is a live row, not silence).
    `impossible` counts samples on channels 9-13 that are further than
    IMPOSSIBLE_TOL counts from the 256-count grid, outside 0..4095, or an index/data byte above 255 (live rows), and
    `impossible_first` is the first such row or None; `range_errors` is the part of that count that is out of range or a
    byte above 255 (wrong whatever the grid distance); `max_residual` is the largest distance from the grid;
    `reserved_nonzero` counts non-zero samples on channels 14-16."""
    k = np.asarray(k, dtype=np.int64)
    u, res = sym12(k[:, 8:13])  # (n, 5): index bits 0-11, 12-23, 24-31, data byte 1, data byte 2
    silent = (u[:, 0] == ZERO) & (u[:, 1] == ZERO) & (u[:, 2] == ZERO)  # all-zero index columns: the sink's underrun
    live = ~silent  # silence (u = 2048 on ch11 is not a valid index byte); no data
    off_grid = np.abs(res) > IMPOSSIBLE_TOL
    out_of_range = (u < 0) | (u > 4095)
    big_byte = live & (u[:, 2] > 255)  # index bits 24-31 must be < 256
    bad_byte = live[:, None] & (u[:, 3:5] > 255)  # a data byte must be < 256
    range_cells = out_of_range.copy()  # impossible whatever the grid distance: outside 0..4095, byte > 255
    range_cells[:, 2] |= big_byte
    range_cells[:, 3:5] |= bad_byte
    bad_cells = off_grid | range_cells
    range_errors = int(range_cells.sum())
    impossible = int(bad_cells.sum())
    bad_rows = bad_cells.any(axis=1)
    first = int(np.argmax(bad_rows)) if bad_rows.any() else None
    max_residual = int(np.abs(res).max()) if len(res) else 0
    reserved_nonzero = int(np.count_nonzero(k[:, 13:16]))
    uc = np.clip(u, 0, 4095)
    idx = (uc[:, 0] & 0xFFF) | ((uc[:, 1] & 0xFFF) << 12) | ((uc[:, 2] & 0xFF) << 24)
    idx = np.where(silent, 0, idx)
    raw = (np.clip(u[live][:, 3:5], 0, 255)).astype(np.uint8).tobytes()
    frames, bad = parse_frames(raw)
    li = idx[live]
    step = (np.diff(li) - 1) & 0xFFFFFFFF
    jr = np.nonzero(step != 0)[0]
    jumps = [(int(li[j]), int(li[j + 1])) for j in jr]  # (last index before, first index after)
    anchors = [struct.unpack(">QQ", p) for kd, p in frames if kd == K_ANCHOR]

    def place(t_s):
        """card sample index for PC wall-clock time t_s (seconds), from the nearest anchor."""
        if not anchors:
            return None
        a = min(anchors, key=lambda x: abs(x[1] / 1e6 - t_s))
        return int(round(a[0] + (t_s - a[1] / 1e6) * RATE))

    def jsons(kind, tkey):
        res = []
        for kd, p in frames:
            if kd != kind:
                continue
            try:
                r = json.loads(p)
            except ValueError:
                continue
            r["card_index"] = r["idx"] if tkey == "idx" and "idx" in r else (place(r[tkey]) if tkey in r else None)
            res.append(r)
        return res

    return {
        "frames": frames,
        "index": idx,
        "silent": silent,
        "jumps": jumps,
        "anchors": anchors,
        "board": jsons(K_BOARD, "host_t"),
        "bridge": jsons(K_BRIDGE, "t"),
        "bands": jsons(K_BANDS, "idx"),
        "bad_frames": bad,
        "impossible": impossible,
        "impossible_first": first,
        "range_errors": range_errors,
        "max_residual": max_residual,
        "reserved_nonzero": reserved_nonzero,
    }


OK_SUBTYPES = ("PCM_16", "PCM_24", "PCM_32", "FLOAT", "DOUBLE")  # anything narrower leaves no room for the 256-count grid


def header_shortfall(path):
    """-> (claimed audio bytes, bytes really present) when a RIFF/WAVE file's data chunk claims more than the file holds,
    else None. (libsndfile silently reports the shorter real length, so the header has to be read directly.)"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return None
                size = struct.unpack("<I", ch[4:])[0]
                if ch[:4] == b"data":
                    have = os.path.getsize(path) - f.tell()
                    return (size, have) if size != 0xFFFFFFFF and size > have else None
                f.seek(size + (size & 1), 1)
    except OSError:
        return None


def run(argv=None, out=print, err=lambda m: print(m, file=sys.stderr)):
    """CLI body. Exit codes: 0 clean; 1 unusable recording (too few channels / wrong rate, as before); 3 the decode found
    impossible values, non-zero reserved channels or corrupt frames (the path is not preserving the format); 4 the file
    cannot be read; 5 the recording is narrower than 16 bits."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("wav")
    ap.add_argument("--out", help="write the board + bridge records, with card_index, as JSONL")
    a = ap.parse_args(argv)
    import soundfile as sf

    try:
        info = sf.info(a.wav)
        x, rate = sf.read(a.wav, dtype="float64", always_2d=True)
    except (sf.LibsndfileError, RuntimeError, OSError) as e:
        err(f"{a.wav}: cannot read as audio ({e})")
        return 4
    if info.subtype not in OK_SUBTYPES:
        err(f"{a.wav}: {info.subtype} recording; the cable stream needs at least 16 bits (PCM_16, PCM_24, PCM_32, FLOAT)")
        return 5
    short = header_shortfall(a.wav)
    if short:
        err(f"WARNING {a.wav}: the header claims {short[0]} bytes of audio but the file holds {short[1]} (truncated recording); decoding what is there")
    if x.shape[1] < 13:
        err(f"{a.wav}: {x.shape[1]} channel(s); the cable stream has 16")
        return 1
    if x.shape[1] < 16:
        err(f"WARNING {a.wav}: {x.shape[1]} channel(s), the cable stream has 16; reserved channels not checked")
    if rate != RATE:
        err(f"{a.wav}: {rate} Hz, expected {RATE}")
        return 1
    d = decode(np.rint(x * 8388608.0).astype(np.int64))
    n = len(x)
    out(f"{n} samples ({n / RATE:.1f} s); silent rows (sink underruns) {int(d['silent'].sum())}; index jumps {len(d['jumps'])} {d['jumps'][:5]}")
    out(
        f"anchors {len(d['anchors'])}; board records {len(d['board'])}; bridge records {len(d['bridge'])}; "
        f"beam-band records {len(d['bands'])}; corrupt frames {d['bad_frames']}"
    )
    bad = bool(d["impossible"] or d["reserved_nonzero"] or d["bad_frames"])
    if bad:
        err(
            f"WARNING the recording path is not preserving the cable-stream format: impossible values {d['impossible']} "
            f"(first at row {d['impossible_first']}, largest distance from the grid {d['max_residual']} counts), "
            f"non-zero reserved samples {d['reserved_nonzero']}; "
            f"corrupt frames {d['bad_frames']}, index jumps {len(d['jumps'])}"
        )
    if a.out:
        with open(a.out, "w") as f:
            for r in d["board"] + d["bridge"] + d["bands"]:
                f.write(json.dumps(r) + "\n")
    return 3 if bad else 0


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
