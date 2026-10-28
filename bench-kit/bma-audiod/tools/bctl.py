# control-only telnet helper: run one shell command on the BMA board, print output
import socket, sys, time, random

host = sys.argv[1]
cmd = sys.argv[2]
to = float(sys.argv[3]) if len(sys.argv) > 3 else 20
port = int(sys.argv[4]) if len(sys.argv) > 4 else 23
tag = "Z%06d" % random.randint(0, 999999)
s = socket.create_connection((host, port), timeout=5)
s.settimeout(0.5)


def rd(t):
    b = b""
    end = time.time() + t
    while time.time() < end:
        try:
            d = s.recv(65536)
            if not d:
                break
            b += d
        except socket.timeout:
            pass
        if ("__E" + "ND" + tag).encode() in b.split(b"echo", 1)[-1]:
            break
    return b


rd(1.0)
s.sendall((cmd + "; echo __E''ND" + tag + "__\n").encode())
out = rd(to).replace(b"\xff\xfb\x01", b"").decode("latin1")
out = out.split("__E''ND" + tag + "__", 1)[-1]
print(out.split("__END" + tag + "__")[0].strip("\r\n"))
