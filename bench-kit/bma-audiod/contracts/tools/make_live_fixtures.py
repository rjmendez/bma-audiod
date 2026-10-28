#!/usr/bin/env python3
"""Writes fixtures/ui_live_examples.json (contract ui-live 1.0.0) with the reference code of ref_binary.py: BMW1 frames, RFC 6455 server and client frames, the
handshake vector, the text control frames. Synthetic values only. Run from contracts/:  python tools/make_live_fixtures.py"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import ref_binary as R  # noqa: E402

MASK = bytes([0x37, 0xFA, 0x21, 0x3D])


def beams(fill):
    return [[first] + [f] * (R.LIVE_BINS - 1) for f, first in fill]


def main():
    fill = [(30 + 6 * b, b) for b in range(9)]
    frames_valid = []
    for name, seq, t_us, flags, f in (("complete row, all beams present", 1234, 1792951400100000, 0, fill),
                                      ("gated row (flag bit 0), coarse bins", 1235, 1792951400200000, R.LIVE_FLAG_GATED, [(90, 90)] * 9),
                                      ("beam 8 absent: zero filled, flag bit 3", 1236, 1792951400300000, R.LIVE_FLAG_MISSING, fill[:8] + [(0, 0)]),
                                      ("gap and zero flags together, seq at the u32 limit", 4294967295, 1792951400400000, R.LIVE_FLAG_GAP | R.LIVE_FLAG_ZERO, fill)):
        frames_valid.append({"name": name, "seq": seq, "t_us": t_us, "flags": flags, "beams": f, "hex": R.build_live(seq, t_us, flags, beams(f)).hex()})
    good = R.build_live(1, 1792951400100000, 0, beams(fill))

    def bad(name, frame, code):
        return {"name": name, "hex": frame.hex(), "code": code}
    import struct
    hdr = lambda **kw: good[:4] + struct.pack("<BBHIQHH", kw.get("typ", 1), kw.get("nb", 9), kw.get("nbins", 46), 1, 1792951400100000, kw.get("flags", 0), kw.get("res", 0)) + good[24:]
    frames_invalid = [bad("shorter than the header", good[:20], "short"), bad("wrong magic", b"BMWX" + good[4:], "magic"), bad("type 2", hdr(typ=2), "type"),
                      bad("8 beams declared", hdr(nb=8), "beams"), bad("45 bins declared", hdr(nbins=45), "bins"), bad("reserved flag bit 4", hdr(flags=0x10), "flags"),
                      bad("reserved field not zero", hdr(res=1), "reserved"), bad("one byte short", good[:-1], "length"), bad("one byte too long", good + b"\x00", "length")]
    server = []
    for name, op, payload in (("text hello", R.WS_TEXT, json.dumps({"k": "hello", "v": 1, "tier": "standard", "beams": 9, "n_bins": 46, "rows_per_s": 10}, separators=(",", ":")).encode()),
                              ("text reset", R.WS_TEXT, b'{"k":"reset","rev":3}'), ("ping", R.WS_PING, b"bma"), ("pong echo of 2 bytes", R.WS_PONG, b"hi"),
                              ("close 1000", R.WS_CLOSE, (1000).to_bytes(2, "big")), ("close 1002 with a reason", R.WS_CLOSE, (1002).to_bytes(2, "big") + b"client frames must be masked")):
        server.append({"name": name, "opcode": op, "payload_hex": payload.hex(), "hex": R.ws_encode(op, payload).hex()})
    server.append({"name": "binary frame of 438 bytes uses the 16-bit length", "opcode": R.WS_BINARY, "payload_hex": good.hex(), "hex": R.ws_encode(R.WS_BINARY, good).hex()})
    cv = []
    for name, op, payload in (("ping, 2 bytes", R.WS_PING, b"hi"), ("pong, empty", R.WS_PONG, b""), ("close 1000", R.WS_CLOSE, (1000).to_bytes(2, "big")), ("ping, 125 bytes", R.WS_PING, bytes(range(125)))):
        cv.append({"name": name, "opcode": op, "payload_hex": payload.hex(), "mask_hex": MASK.hex(), "hex": R.ws_encode(op, payload, MASK).hex()})

    def cbad(name, raw, code, close):
        return {"name": name, "hex": raw.hex(), "code": code, "close_code": close}
    ci = [cbad("unmasked ping", R.ws_encode(R.WS_PING, b"hi"), "unmasked", 1002),
          cbad("masked ping with RSV1 set", bytes([R.ws_encode(R.WS_PING, b"hi", MASK)[0] | 0x40]) + R.ws_encode(R.WS_PING, b"hi", MASK)[1:], "reserved_bits", 1002),
          cbad("masked ping with FIN clear (fragment)", bytes([R.ws_encode(R.WS_PING, b"hi", MASK)[0] & 0x7F]) + R.ws_encode(R.WS_PING, b"hi", MASK)[1:], "fragmented", 1002),
          cbad("masked text frame (data from the client)", R.ws_encode(R.WS_TEXT, b"x", MASK), "opcode", 1003),
          cbad("masked binary frame", R.ws_encode(R.WS_BINARY, b"x", MASK), "opcode", 1003),
          cbad("masked ping of 126 bytes (16-bit length)", R.ws_encode(R.WS_PING, bytes(126), MASK), "payload_too_big", 1009)]
    doc = {"note": "ui-live 1.0.0 examples, synthetic values; built by tools/make_live_fixtures.py with ref_binary.py. beams: [fill, first] per beam = bin 0 is `first`, bins 1-45 are `fill`.",
           "frames": {"valid": frames_valid, "invalid": frames_invalid},
           "handshake": {"key": "dGhlIHNhbXBsZSBub25jZQ==", "accept": R.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "source": "RFC 6455 section 1.3"},
           "server_frames": server, "client_frames": {"valid": cv, "invalid": ci},
           "text": [{"name": "hello", "json": {"k": "hello", "v": 1, "tier": "standard", "beams": 9, "n_bins": 46, "rows_per_s": 10}}, {"name": "reset", "json": {"k": "reset", "rev": 3}}]}
    with open(os.path.join(HERE, "..", "fixtures", "ui_live_examples.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main()
