#!/usr/bin/env python3
"""Test tool: replay a mono 24/16-bit WAV as a Dante media flow (UDP, 9-byte header, 16 frames/packet,
real-time paced at 3000 pkt/s) to ADDR:PORT, so dante_bridge.py --bind can be tested end to end.

  fake_dante_sender.py clip.wav 127.0.0.1:4321 [--nch 1] [--drop-every N] [--secs S]

--drop-every N omits every Nth packet (a hole in the card clock) to exercise gap handling.
Not used in production; it never touches the real card.
"""

import argparse, socket, struct, sys, time, wave


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("dest")
    ap.add_argument("--nch", type=int, default=1)
    ap.add_argument("--drop-every", type=int, default=0)
    ap.add_argument("--secs", type=float, default=0.0, help="stop after this many seconds (default: whole clip)")
    a = ap.parse_args()
    w = wave.open(a.wav, "rb")
    sw, ch, sr = w.getsampwidth(), w.getnchannels(), w.getframerate()
    raw = w.readframes(w.getnframes())
    # first channel only -> signed 24-bit big-endian per frame
    step = sw * ch
    frames = []
    for i in range(0, len(raw) - step + 1, step):
        s = raw[i : i + sw]
        v = int.from_bytes(s, "little", signed=True)
        if sw == 2:
            v <<= 8
        elif sw == 4:
            v >>= 8
        frames.append(v)
    if sr != 48000:  # linear resample to the card's 48 kHz (test tool; numpy required)
        import numpy as np

        x = np.asarray(frames, dtype=np.float64)
        n_out = int(len(x) * 48000 / sr)
        frames = [int(v) for v in np.interp(np.arange(n_out) * sr / 48000.0, np.arange(len(x)), x)]
        print(f"resampled {sr} -> 48000 Hz ({len(frames)} frames)", flush=True)
    host, _, port = a.dest.rpartition(":")
    dst = (host, int(port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n_pk = len(frames) // 16
    if a.secs:
        n_pk = min(n_pk, int(a.secs * 3000))
    sec0, t0 = 1000, time.perf_counter()
    sent = dropped = 0
    for k in range(n_pk):
        absf = sec0 * 48000 + k * 16
        if a.drop_every and k % a.drop_every == a.drop_every - 1:
            dropped += 1
        else:
            body = bytearray()
            for f in range(16):
                v = frames[k * 16 + f] & 0xFFFFFF
                three = bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
                body += three * a.nch
            pkt = bytes([0x02]) + struct.pack(">II", absf // 48000, absf % 48000) + bytes(body)
            sock.sendto(pkt, dst)
            sent += 1
        # real-time pacing: packet k is due at k * 16/48000 s
        due = t0 + (k + 1) * (16 / 48000)
        while True:
            d = due - time.perf_counter()
            if d <= 0:
                break
            if d > 0.002:
                time.sleep(d - 0.001)
    print(f"sent {sent} packets ({sent * 16 / 48000:.1f} s of audio), omitted {dropped}", flush=True)


if __name__ == "__main__":
    main()
