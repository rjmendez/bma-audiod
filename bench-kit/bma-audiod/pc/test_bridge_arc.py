#!/usr/bin/env python3
"""Hermetic test of dante_bridge's ARC logic against a fake card on 127.0.0.1 (no real card touched).

Checks: build_create is byte-exact against the vector captured from the board (daemon arc.rs test);
a card with an EMPTY table gets exactly one create then the flow is re-queried; a card that already
has a flow is never sent a create; --create-channels none never creates.
Run: python -I test_bridge_arc.py   (needs UDP 4440 free on 127.0.0.1)
"""

import os, socket, struct, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dante_bridge as b  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def hexb(s):
    return bytes.fromhex(s.replace(" ", ""))


WANT_CREATE = hexb(
    "2729 0048 08c1 2201 0000 0101 0010 0000 0001 0002 0000 0000 0000 0000 0000 0008 0001 0002 0003 0004 0005 0006 0007 0008 0034 0000 0a00 0000 0000 0000 0000 0000 0000 0000 0001 0000"
)
FLOW_RESP = hexb(
    "2729 006e 08c3 2200 0001 1001 002c 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0001 0002 0000 bb80 0000 0018 0001 0008 0050 0001 0002 0003 0004 0005 0006 0007 0008 0058 0802 10e1 efff d06c 0a00 0001 0000 0000 0010 006c 000f 4240 0000 0000 3100"
)
EMPTY_RESP = hexb("2729 002c 08b9 2200 0001 1000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000 0000")


class FakeCard(threading.Thread):
    def __init__(self, has_flow):
        super().__init__(daemon=True)
        self.has_flow = has_flow
        self.creates = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 4440))
        self.sock.settimeout(0.2)
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                d, a = self.sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                break
            op, tid = struct.unpack(">H", d[6:8])[0], struct.unpack(">H", d[4:6])[0]
            if op == 0x2201:
                self.creates.append(d)
                self.has_flow = True
                r = bytearray(EMPTY_RESP)
            elif op == 0x2200:
                r = bytearray(FLOW_RESP if self.has_flow else EMPTY_RESP)
            else:
                continue
            struct.pack_into(">H", r, 4, tid)
            self.sock.sendto(bytes(r), a)

    def close(self):
        self.stop = True
        time.sleep(0.3)
        self.sock.close()


check("build_create is byte-exact against the board capture", b.build_create(1, [1, 2, 3, 4, 5, 6, 7, 8], 0x08C1) == WANT_CREATE)
check("build_query matches the captured query vector", b.build_query(1, 0x08B9) == hexb("2729 0010 08b9 2200 0000 0001 0001 0000"))

c = FakeCard(has_flow=False)
c.start()
r = b.discover("127.0.0.1", "127.0.0.1", 1, create_chans=[1, 2, 3, 4, 5, 6, 7, 8])
check("empty card: flow created, then found", r == ("127.0.0.1", "127.0.0.1", ("239.255.208.108", 4321)), str(r))
check("empty card: exactly one create sent", len(c.creates) == 1, str(len(c.creates)))
check(
    "empty card: the create asks for channels 1-8 multicast",
    len(c.creates) == 1 and struct.unpack(">H", c.creates[0][30:32])[0] == 8 and struct.unpack(">H", c.creates[0][18:20])[0] == 2,
)
c.close()

c = FakeCard(has_flow=True)
c.start()
r = b.discover("127.0.0.1", "127.0.0.1", 1, create_chans=[1, 2, 3, 4, 5, 6, 7, 8])
check("card with a flow: found, nothing created", r and r[2] == ("239.255.208.108", 4321) and not c.creates, str(c.creates))
c.close()

c = FakeCard(has_flow=False)
c.start()
r = b.discover("127.0.0.1", "127.0.0.1", 1, create_chans=None)
check("--create-channels none: never creates", r is None and not c.creates, str(r))
c.close()

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
