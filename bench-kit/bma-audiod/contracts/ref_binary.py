#!/usr/bin/env python3
"""Reference implementation of the BINARY contracts (specs/bmaa.md, specs/cable-stream.md).

Deliberately independent of pc/dante_bridge.py and pc/bma_cable_sink.py: plain `struct`, no numpy. The conformance
tests build packets with the production code and parse them here, and build them here and compare bytes, so a change
on either side that breaks the layout turns a test red. Contract versions: bmaa 1.0.0, cable-stream 2.0.0, spectrum-packet 1.0.0."""
import struct, zlib

BMAA_MAGIC = b"BMAA"
BMAA_VERSION = 1
FLAG_ZERO, FLAG_GAP_BEFORE = 0x01, 0x02
BMAA_SLOTS = 8
I24_MIN, I24_MAX = -(1 << 23), (1 << 23) - 1

BMD1_MAGIC = b"BMD1"
K_ANCHOR, K_BOARD, K_BRIDGE, K_BANDS = 1, 2, 3, 4          # K_BANDS since cable-stream 1.1.0
CABLE_CHANNELS = 16
COL_BEAM0, COL_IDX0, COL_IDX1, COL_IDX2, COL_DATA0, COL_DATA1 = 0, 8, 9, 10, 11, 12     # 0-based; channels 9-13


class ContractError(ValueError):
    """`code` is the stable machine-readable reason; str() is the human message."""

    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = code


# ----------------------------------------------------------------------------------------------- BMAA datagram
def build_bmaa(slot, flags, seq, block, samples):
    """Byte-exact BMAA datagram. seq and block are reduced modulo 2**32 (they wrap)."""
    if not 0 <= slot < BMAA_SLOTS:
        raise ContractError("slot", f"slot {slot} outside 0..{BMAA_SLOTS - 1}")
    for s in samples:
        if not I24_MIN <= s <= I24_MAX:
            raise ContractError("sample_range", f"sample {s} is not a signed 24-bit value")
    hdr = BMAA_MAGIC + bytes([BMAA_VERSION, 0, slot, flags & 0xFF])
    return hdr + struct.pack("<III", seq & 0xFFFFFFFF, block & 0xFFFFFFFF, len(samples)) + struct.pack(f"<{len(samples)}i", *samples)


def parse_bmaa(pkt):
    """-> dict(slot, flags, zero, gap_before, seq, block, n, samples) or raises ContractError(code, ...).
    Codes: short, magic, version, reserved, slot, length, sample_range. Unknown flag bits are kept in `flags` and ignored."""
    if len(pkt) < 20:
        raise ContractError("short", f"{len(pkt)} bytes, header is 20")
    if pkt[:4] != BMAA_MAGIC:
        raise ContractError("magic", f"got {pkt[:4]!r}")
    if pkt[4] != BMAA_VERSION:
        raise ContractError("version", f"version {pkt[4]}, this contract is {BMAA_VERSION}")
    if pkt[5] != 0:
        raise ContractError("reserved", f"byte 5 is {pkt[5]}, must be 0")
    slot, flags = pkt[6], pkt[7]
    if slot >= BMAA_SLOTS:
        raise ContractError("slot", f"slot {slot} outside 0..{BMAA_SLOTS - 1}")
    seq, block, n = struct.unpack_from("<III", pkt, 8)
    if len(pkt) != 20 + 4 * n:
        raise ContractError("length", f"{len(pkt)} bytes for n={n}, expected {20 + 4 * n}")
    samples = list(struct.unpack_from(f"<{n}i", pkt, 20))
    for s in samples:
        if not I24_MIN <= s <= I24_MAX:
            raise ContractError("sample_range", f"sample {s} is not a signed 24-bit value")
    return {"slot": slot, "flags": flags, "zero": bool(flags & FLAG_ZERO), "gap_before": bool(flags & FLAG_GAP_BEFORE),
            "seq": seq, "block": block, "n": n, "samples": samples}


# ----------------------------------------------------------------------------------------------- BMD1 data frames
def encode_frame(kind, payload):
    """b"BMD1", kind u8, length u16 BE, payload, crc32 u32 BE over kind|length|payload."""
    if not 0 <= kind <= 255 or len(payload) > 0xFFFF:
        raise ContractError("frame", "kind or payload size out of range")
    body = struct.pack(">BH", kind, len(payload)) + payload
    return BMD1_MAGIC + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def parse_frames(buf):
    """-> ([(kind, payload)], corrupt_count). Zero bytes between frames (idle track) are skipped; a frame whose CRC or
    length is wrong counts as corrupt and scanning resumes one byte after its magic."""
    out, bad, i = [], 0, 0
    while True:
        i = buf.find(BMD1_MAGIC, i)
        if i < 0 or i + 7 > len(buf):
            return out, bad
        kind, ln = struct.unpack_from(">BH", buf, i + 4)
        end = i + 7 + ln + 4
        if end <= len(buf):
            body = buf[i + 4:i + 7 + ln]
            if (zlib.crc32(body) & 0xFFFFFFFF) == struct.unpack_from(">I", buf, i + 7 + ln)[0]:
                out.append((kind, buf[i + 7:i + 7 + ln]))
                i = end
                continue
        bad += 1
        i += 1


def anchor_payload(index, utc_us):
    return struct.pack(">QQ", index, utc_us)


def parse_anchor(payload):
    if len(payload) != 16:
        raise ContractError("anchor_length", f"{len(payload)} bytes, expected 16")
    return struct.unpack(">QQ", payload)


# ----------------------------------------------------------------------------------------------- 16-channel rows
SYM_ZERO = 2048


def symbol_column(u):
    """12-bit symbol u (0..4095) -> the sample count that carries it: (u - 2048) << 8, a multiple of 256."""
    if not 0 <= u <= 4095:
        raise ContractError("symbol", f"symbol {u} outside 0..4095")
    return (u - SYM_ZERO) << 8


def symbol_from_column(count):
    """Sample count -> symbol, rounding: ((count + 128) >> 8) + 2048. Exact for an untouched sample and for any
    disturbance of less than 128 counts."""
    return ((count + 128) >> 8) + SYM_ZERO


def index_columns(index):
    """card sample index (u32; higher bits are not carried) -> the three column counts: bits 0-11, 12-23, 24-31."""
    index &= 0xFFFFFFFF
    return symbol_column(index & 0xFFF), symbol_column((index >> 12) & 0xFFF), symbol_column(index >> 24)


def index_from_columns(c0, c1, c2):
    return symbol_from_column(c0) | (symbol_from_column(c1) << 12) | (symbol_from_column(c2) << 24)


def is_silent_row(row):
    """The sink's underrun rows: all three index symbols read u = 2048 (all-zero counts). u = 2048 on the third is
    not a valid index byte (< 256), so no live row looks like this."""
    return all(symbol_from_column(row[c]) == SYM_ZERO for c in (COL_IDX0, COL_IDX1, COL_IDX2))


def data_columns(raw):
    """Data-track bytes -> (ch12, ch13) column counts per row, two bytes per row, one byte (u = value) per channel
    (raw length must be even)."""
    if len(raw) % 2:
        raise ContractError("data_length", "data track bytes must be a multiple of 2")
    return [(symbol_column(raw[i]), symbol_column(raw[i + 1])) for i in range(0, len(raw), 2)]


def decode_rows(rows):
    """rows: iterable of 16-int rows (signed 24-bit counts each). -> dict(index=[...], silent=[bool...], beams=[[8 ints]...],
    frames=[(kind, payload)], corrupt=int). Rows whose three index channels are all-zero samples (the silent-row marker)
    are the sink's underrun silence: they report index 0 and carry no data. Index 0 itself is a live row."""
    index, silent, beams, data = [], [], [], bytearray()
    for r in rows:
        if len(r) != CABLE_CHANNELS:
            raise ContractError("row_width", f"row has {len(r)} channels, expected {CABLE_CHANNELS}")
        sil = is_silent_row(r)
        index.append(0 if sil else index_from_columns(r[COL_IDX0], r[COL_IDX1], r[COL_IDX2]))
        silent.append(sil)
        beams.append(list(r[:8]))
        if not sil:
            data += bytes([symbol_from_column(r[COL_DATA0]) & 0xFF, symbol_from_column(r[COL_DATA1]) & 0xFF])
    frames, corrupt = parse_frames(bytes(data))
    return {"index": index, "silent": silent, "beams": beams, "frames": frames, "corrupt": corrupt}


# ----------------------------------------------------------------------------------------------- BMAS spectrum rows
# specs/spectrum-packet.md (spectrum-packet 1.0.0). Magnitudes only: nothing in this section handles samples.
BMAS_MAGIC = b"BMAS"
BMAS_VERSION = 1
BMAS_HEADER = 24
SPEC_FLAG_GATED, SPEC_FLAG_GAP, SPEC_FLAG_ZERO = 0x01, 0x02, 0x04
SPEC_TIERS = {0: {"name": "standard", "hop_ms": 100, "window": 4800, "n_bins": 46, "coarse_rows": 10},
              1: {"name": "detail", "hop_ms": 20, "window": 2400, "n_bins": 100, "coarse_rows": 50}}
SPEC_DB_FLOOR, SPEC_DB_STEP = -100.0, 0.5


def spectrum_band_edges(tier):
    """[(lo_hz, hi_hz)] of every band of a tier. standard: 50-250 Hz, 9 bands of 1/3 octave 250-2000 Hz, 36 bands of
    1/12 octave 2000-16000 Hz (46). detail: 4 bands of 250 Hz 0-1000 Hz, 96 bands of 1/24 octave 1000-16000 Hz (100)."""
    if tier == 0:
        e = [(50.0, 250.0)] + [(250.0 * 2 ** (k / 3), 250.0 * 2 ** ((k + 1) / 3)) for k in range(9)]
        return e + [(2000.0 * 2 ** (k / 12), 2000.0 * 2 ** ((k + 1) / 12)) for k in range(36)]
    if tier == 1:
        e = [(250.0 * k, 250.0 * (k + 1)) for k in range(4)]
        return e + [(1000.0 * 2 ** (k / 24), 1000.0 * 2 ** ((k + 1) / 24)) for k in range(96)]
    raise ContractError("tier", f"tier {tier} is not 0 or 1")


def spectrum_coarse_groups(tier):
    """Bins -> coarse band index. standard: bin 0 alone, bins 1-9 alone, bins 10-45 in groups of 4 (19 groups);
    detail: bins 0-3 alone, bins 4-99 in groups of 8 (16 groups). Every group above 250 Hz is one third octave."""
    if tier == 0:
        return [0] + list(range(1, 10)) + [10 + (i // 4) for i in range(36)]
    if tier == 1:
        return list(range(4)) + [4 + (i // 8) for i in range(96)]
    raise ContractError("tier", f"tier {tier} is not 0 or 1")


def quantize_db(db):
    """mean-square dB re full scale -> uint8: round half even of (db + 100) * 2, clamped to 0..255 (0 = at or below -100 dB)."""
    v = round((db - SPEC_DB_FLOOR) / SPEC_DB_STEP)
    return max(0, min(255, v))


def dequantize(v):
    return v * SPEC_DB_STEP + SPEC_DB_FLOOR


def coarsen_block(tier, rows):
    """The gating rule. rows: the uint8 rows (lists, all n_bins long) of ONE aligned block (10 standard rows = 1 s, 50 detail rows
    = 1 s) -> ONE uint8 row: per coarse group the mean LINEAR power over all the group's bins in all rows, back to dB, quantized."""
    import math
    t = SPEC_TIERS[tier]
    if len(rows) != t["coarse_rows"] or any(len(r) != t["n_bins"] for r in rows):
        raise ContractError("coarsen_shape", f"need {t['coarse_rows']} rows of {t['n_bins']} bins")
    groups = spectrum_coarse_groups(tier)
    out = []
    for g in sorted(set(groups)):
        vals = [10.0 ** (dequantize(r[i]) / 10.0) for r in rows for i in range(t["n_bins"]) if groups[i] == g]
        out.append(quantize_db(10.0 * math.log10(sum(vals) / len(vals))))
    return [out[g] for g in groups]


def build_spectrum(tier, flags, beam, row, t_us, bins):
    """Byte-exact BMAS datagram. row is reduced modulo 2**32."""
    if tier not in SPEC_TIERS:
        raise ContractError("tier", f"tier {tier} is not 0 or 1")
    t = SPEC_TIERS[tier]
    if not 0 <= beam <= 8:
        raise ContractError("beam", f"beam {beam} outside 0..8")
    if len(bins) != t["n_bins"]:
        raise ContractError("n_bins", f"{len(bins)} bins, tier {tier} has {t['n_bins']}")
    return (BMAS_MAGIC + bytes([BMAS_VERSION, tier, flags & 0xFF, beam]) + struct.pack("<IQHH", row & 0xFFFFFFFF, t_us, t["hop_ms"], len(bins))
            + bytes(bins))


def parse_spectrum(pkt):
    """-> dict(tier, flags, gated, gap, zero, beam, row, t_us, hop_ms, n_bins, bins) or raises ContractError(code, ...).
    Codes: short, magic, version, tier, flags, beam, hop, n_bins, length, gated_not_coarse."""
    if len(pkt) < BMAS_HEADER:
        raise ContractError("short", f"{len(pkt)} bytes, header is {BMAS_HEADER}")
    if pkt[:4] != BMAS_MAGIC:
        raise ContractError("magic", f"got {pkt[:4]!r}")
    if pkt[4] != BMAS_VERSION:
        raise ContractError("version", f"version {pkt[4]}, this contract is {BMAS_VERSION}")
    tier, flags, beam = pkt[5], pkt[6], pkt[7]
    if tier not in SPEC_TIERS:
        raise ContractError("tier", f"tier {tier} is not 0 or 1")
    if flags & ~0x07:
        raise ContractError("flags", f"reserved flag bits set in 0x{flags:02x}")
    if beam > 8:
        raise ContractError("beam", f"beam {beam} outside 0..8")
    row, t_us, hop, n = struct.unpack_from("<IQHH", pkt, 8)
    t = SPEC_TIERS[tier]
    if hop != t["hop_ms"]:
        raise ContractError("hop", f"hop_ms {hop}, tier {tier} is {t['hop_ms']}")
    if n != t["n_bins"]:
        raise ContractError("n_bins", f"n_bins {n}, tier {tier} has {t['n_bins']}")
    if len(pkt) != BMAS_HEADER + n:
        raise ContractError("length", f"{len(pkt)} bytes for n_bins={n}, expected {BMAS_HEADER + n}")
    bins = list(pkt[BMAS_HEADER:])
    if flags & SPEC_FLAG_GATED:
        groups = spectrum_coarse_groups(tier)
        for i in range(1, n):
            if groups[i] == groups[i - 1] and bins[i] != bins[i - 1]:
                raise ContractError("gated_not_coarse", f"a gated row must be constant inside a coarse group (bin {i})")
    return {"tier": tier, "flags": flags, "gated": bool(flags & SPEC_FLAG_GATED), "gap": bool(flags & SPEC_FLAG_GAP),
            "zero": bool(flags & SPEC_FLAG_ZERO), "beam": beam, "row": row, "t_us": t_us, "hop_ms": hop, "n_bins": n, "bins": bins}


# ----------------------------------------------------------------------------------------------- ui-live (specs/ui-live.md): BMW1 frames and RFC 6455 framing
import base64, hashlib

BMW1_MAGIC = b"BMW1"
BMW1_TYPE_ROW = 1
BMW1_HEADER = 24
LIVE_BEAMS, LIVE_BINS = 9, 46
LIVE_FLAG_GATED, LIVE_FLAG_GAP, LIVE_FLAG_ZERO, LIVE_FLAG_MISSING = 0x01, 0x02, 0x04, 0x08
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 1, 2, 8, 9, 10
WS_MAX_CLIENT_PAYLOAD = 125


def build_live(seq, t_us, flags, beams):
    """Byte-exact BMW1 frame. beams: 9 lists of 46 uint8 (beam 0 = the 8-beam power mean). seq is reduced modulo 2**32."""
    if len(beams) != LIVE_BEAMS or any(len(b) != LIVE_BINS for b in beams):
        raise ContractError("shape", f"need {LIVE_BEAMS} beams of {LIVE_BINS} bins")
    if flags & ~0x0F:
        raise ContractError("flags", f"reserved flag bits set in 0x{flags:04x}")
    return BMW1_MAGIC + struct.pack("<BBHIQHH", BMW1_TYPE_ROW, LIVE_BEAMS, LIVE_BINS, seq & 0xFFFFFFFF, t_us, flags, 0) + b"".join(bytes(b) for b in beams)


def parse_live(frame):
    """-> dict(seq, t_us, flags, gated, gap, zero, missing, beams) or raises ContractError(code, ...).
    Codes: short, magic, type, beams, bins, flags, reserved, length."""
    if len(frame) < BMW1_HEADER:
        raise ContractError("short", f"{len(frame)} bytes, header is {BMW1_HEADER}")
    if frame[:4] != BMW1_MAGIC:
        raise ContractError("magic", f"got {frame[:4]!r}")
    typ, nb, nbins, seq, t_us, flags, reserved = struct.unpack_from("<BBHIQHH", frame, 4)
    if typ != BMW1_TYPE_ROW:
        raise ContractError("type", f"type {typ}, this contract has {BMW1_TYPE_ROW}")
    if nb != LIVE_BEAMS:
        raise ContractError("beams", f"{nb} beams, expected {LIVE_BEAMS}")
    if nbins != LIVE_BINS:
        raise ContractError("bins", f"{nbins} bins, expected {LIVE_BINS}")
    if flags & ~0x0F:
        raise ContractError("flags", f"reserved flag bits set in 0x{flags:04x}")
    if reserved:
        raise ContractError("reserved", f"reserved field {reserved}, must be 0")
    if len(frame) != BMW1_HEADER + nb * nbins:
        raise ContractError("length", f"{len(frame)} bytes, expected {BMW1_HEADER + nb * nbins}")
    body = frame[BMW1_HEADER:]
    return {"seq": seq, "t_us": t_us, "flags": flags, "gated": bool(flags & 1), "gap": bool(flags & 2), "zero": bool(flags & 4), "missing": bool(flags & 8),
            "beams": [list(body[i * nbins:(i + 1) * nbins]) for i in range(nb)]}


def ws_accept_key(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")


def ws_encode(opcode, payload, mask_key=None):
    """One unfragmented frame (FIN set). mask_key None = server to client (unmasked); 4 bytes = client to server (masked)."""
    n = len(payload)
    h = bytes([0x80 | opcode])
    mbit = 0x80 if mask_key is not None else 0
    h += bytes([mbit | n]) if n < 126 else bytes([mbit | 126]) + struct.pack(">H", n) if n < 65536 else bytes([mbit | 127]) + struct.pack(">Q", n)
    if mask_key is None:
        return h + payload
    return h + mask_key + bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def ws_decode_client(data):
    """-> (opcode, payload, consumed) of the first client frame in `data`, or None when incomplete; raises ContractError(code, ...).
    Codes: unmasked, reserved_bits, fragmented, opcode, control_too_long, payload_too_big. Client frames may only be close, ping or pong, at most 125 bytes."""
    if len(data) < 2:
        return None
    b0, b1 = data[0], data[1]
    if b0 & 0x70:
        raise ContractError("reserved_bits", "RSV1-3 must be 0")
    if not b0 & 0x80:
        raise ContractError("fragmented", "FIN must be set")
    op = b0 & 0x0F
    if not b1 & 0x80:
        raise ContractError("unmasked", "client frames must be masked")
    n = b1 & 0x7F
    off = 2
    if n >= 126:
        raise ContractError("payload_too_big", f"payload length field {n}: client frames carry at most {WS_MAX_CLIENT_PAYLOAD} bytes")
    if op not in (WS_CLOSE, WS_PING, WS_PONG):
        raise ContractError("opcode", f"opcode {op}: the client may send only close, ping and pong")
    if len(data) < off + 4 + n:
        return None
    key = data[off:off + 4]
    payload = bytes(b ^ key[i % 4] for i, b in enumerate(data[off + 4:off + 4 + n]))
    return op, payload, off + 4 + n
