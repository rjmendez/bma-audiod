#!/usr/bin/env python3
"""Screenshots of the prototype per breakpoint and view (headless Edge over the mock server), and a pixel diff against a baseline folder.

    python shots.py OUT_DIR [--browser edge|chrome] [--diff BASELINE_DIR] [--max-diff 0.02] [--views live,review,det,sys] [--widths 360,390,768,1280,1920]

Exit 0; with --diff, exit 1 when any image differs in more than --max-diff of its pixels (a channel delta above 24 counts). Stdlib only (PNG decoder below).
Live data is synthetic and deterministic per row, but the waterfall clock moves: Live shots differ slightly between runs, so the default tolerance is 2 %."""
import argparse, os, struct, sys, threading, time, zlib
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import cdp           # noqa: E402
import mock_server   # noqa: E402

SIZES = {360: (360, 740, True), 390: (390, 844, True), 768: (768, 1024, False), 1280: (1280, 900, False), 1920: (1920, 1080, False)}


def png_pixels(path):
    """-> (width, height, bytes RGB) of an 8-bit non-interlaced RGB/RGBA PNG."""
    with open(path, "rb") as f:
        d = f.read()
    assert d[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, w = 8, b"", 0
    while pos < len(d):
        n, typ = struct.unpack(">I4s", d[pos:pos + 8]); body = d[pos + 8:pos + 8 + n]; pos += 12 + n
        if typ == b"IHDR":
            w, h, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and ctype in (2, 6) and inter == 0, "unsupported PNG"
            bpp = 3 if ctype == 2 else 4
        elif typ == b"IDAT":
            idat += body
    raw = zlib.decompress(idat); stride = w * bpp; out = bytearray(); prev = bytearray(stride); i = 0
    for _ in range(h):
        ft = raw[i]; line = bytearray(raw[i + 1:i + 1 + stride]); i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0; b = prev[x]; c = prev[x - bpp] if x >= bpp else 0
            if ft == 1: line[x] = (line[x] + a) & 255
            elif ft == 2: line[x] = (line[x] + b) & 255
            elif ft == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c); pr = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[x] = (line[x] + pr) & 255
        out += line; prev = line
    if bpp == 4:
        out = bytearray(b for k, b in enumerate(out) if k % 4 != 3)
    return w, h, bytes(out)


def diff_ratio(a_path, b_path, tol=24):
    wa, ha, a = png_pixels(a_path); wb, hb, b = png_pixels(b_path)
    if (wa, ha) != (wb, hb):
        return 1.0
    bad = sum(1 for i in range(0, len(a), 3) if abs(a[i] - b[i]) > tol or abs(a[i + 1] - b[i + 1]) > tol or abs(a[i + 2] - b[i + 2]) > tol)
    return bad / (wa * ha)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out")
    ap.add_argument("--browser", default="edge", choices=("edge", "chrome"))
    ap.add_argument("--diff", default="")
    ap.add_argument("--max-diff", type=float, default=0.02)
    ap.add_argument("--views", default="live,review,det,sys")
    ap.add_argument("--widths", default="360,390,768,1280,1920")
    ap.add_argument("--settle", type=float, default=4.0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    srv = mock_server.Server(("127.0.0.1", 0), mock_server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % srv.server_address[1]
    br = cdp.Browser(9335, exe=cdp.browser_exe(a.browser))
    bad, names = [], []
    try:
        for w in [int(x) for x in a.widths.split(",")]:
            vw, vh, mob = SIZES[w]
            br.viewport(vw, vh, mobile=mob, dpr=1)
            for v in a.views.split(","):
                br.go(base + "#" + v, a.settle)
                br.eval("document.querySelector('[data-view=%s]') && document.querySelector('[data-view=%s]').click()" % (v, v))
                br.ws.pump(1.0)
                name = "%s_%d.png" % (v, w); names.append(name)
                size = br.shot(os.path.join(a.out, name))
                msg = "%s %d bytes" % (name, size)
                if a.diff and os.path.exists(os.path.join(a.diff, name)):
                    r = diff_ratio(os.path.join(a.diff, name), os.path.join(a.out, name))
                    msg += "  diff %.4f" % r
                    if r > a.max_diff:
                        bad.append(name)
                print(msg, flush=True)
    finally:
        br.close()
    if bad:
        print("DIFF over %.3f: %s" % (a.max_diff, ", ".join(bad)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
