#!/usr/bin/env python3
"""Tests for the --allow-ip-hosts Host rule of bma_ui (a LAN/tailnet bind without a hand-kept list of DHCP addresses).
An IP literal cannot be DNS-rebound, so literals on our port are accepted when asked for; every NAME stays on the explicit
allowlist. Same conventions as test_bma_ui.py (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line, non-zero exit on failure).

    python test_bma_ui_lan.py"""

import http.client, json, os, shutil, socket, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bma_ui as U  # noqa: E402

fails, n_checks = [], 0


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def get_status(port, host, path="/api/status"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    c.putrequest("GET", path, skip_host=True)
    if host is not None:
        c.putheader("Host", host)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def tiny_app(root):
    pr, rp = os.path.join(root, "pipeline"), os.path.join(root, "reports")
    for d in (os.path.join(pr, "analysis"), os.path.join(pr, "logs"), rp):
        os.makedirs(d, exist_ok=True)
    board = U.Board("http://127.0.0.1:9", os.path.join(root, "no_token"), enabled=False)
    return U.App(pr, rp, os.path.join(root, "rollups"), board, window_h=48)


def serve(app, allow_ip_hosts):
    srv = U.Server(("127.0.0.1", 0), app, set(), U.load_assets(), allow_ip_hosts=allow_ip_hosts)
    srv.allowed_hosts = U.allowed_hosts_for(srv.server_address[1], ["ui.example"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run():
    # 1. the pure rule, exact truth table (port 48500)
    P = 48500
    yes = [
        "192.0.2.5:48500",
        "198.51.100.1:48500",
        "198.51.100.11:48500",
        "203.0.113.19:48500",
        "[fd00::1]:48500",
        "[::1]:48500",
        "[FD00::A]:48500",
        " 192.0.2.5:48500 ",
    ]
    no = [
        "192.0.2.5",
        "192.0.2.5:48501",
        "192.0.2.5:1",
        "192.0.2.5:048500",
        "192.0.2.5:48500:1",
        "evil.example:48500",
        "1.2.3.4.evil.example:48500",
        "999.1.1.1:48500",
        "192.168.1:48500",
        "::1:48500",
        "[fd00::1]",
        "[fd00::1]:48501",
        "[fd00::1%eth0]:48500",
        "[fd00::1:48500",
        "localhost:48500",
        ":48500",
        "",
        "192.0.2.5:abc",
    ]
    for h in yes:
        check(f"is_ip_literal_host({h!r}) is True", U.is_ip_literal_host(h, P) is True)
    for h in no:
        check(f"is_ip_literal_host({h!r}) is False", U.is_ip_literal_host(h, P) is False)

    # 2. the server: flag on accepts literals on its own port, still refuses names, wrong ports and everything malformed
    root = tempfile.mkdtemp(prefix="bma_ui_lan_")
    try:
        app = tiny_app(root)
        on, off = serve(app, True), serve(app, False)
        pon, poff = on.server_address[1], off.server_address[1]
        for host in (f"192.0.2.5:{pon}", f"198.51.100.11:{pon}", f"[fd00::1]:{pon}", f"ui.example:{pon}", f"localhost:{pon}", f"127.0.0.1:{pon}"):
            s, _ = get_status(pon, host)
            check(f"flag ON: Host {host!r} is served (200)", s == 200, str(s))
        for host in (
            "192.0.2.5",
            f"192.0.2.5:{pon + 1}",
            f"evil.example:{pon}",
            f"1.2.3.4.evil.example:{pon}",
            f"999.1.1.1:{pon}",
            f"::1:{pon}",
            f"[fd00::1%eth0]:{pon}",
            "",
        ):
            s, b = get_status(pon, host)
            check(f"flag ON: Host {host!r} is refused (403 host_not_allowed)", s == 403 and b"host_not_allowed" in b, f"{s} {b[:80]!r}")
        for host in (f"192.0.2.5:{poff}", f"[fd00::1]:{poff}"):
            s, b = get_status(poff, host)
            check(f"flag OFF (positive twin of the ON case): Host {host!r} is refused (403)", s == 403 and b"host_not_allowed" in b, str(s))
        for host in (f"ui.example:{poff}", f"localhost:{poff}", f"127.0.0.1:{poff}"):
            s, _ = get_status(poff, host)
            check(f"flag OFF: allowlisted Host {host!r} is still served (200)", s == 200, str(s))
        s, _ = get_status(pon, f"192.0.2.5:{pon}", "/api/status?x=1")
        check("flag ON: a literal Host does not change the other rules (an unknown query parameter is still a 4xx, not a 200)", 400 <= s < 500, str(s))
        c = http.client.HTTPConnection("127.0.0.1", pon, timeout=15)
        c.putrequest("POST", "/api/status", skip_host=True)
        c.putheader("Host", f"192.0.2.5:{pon}")
        c.putheader("Content-Length", "0")
        c.endheaders()
        r = c.getresponse()
        r.read()
        check("flag ON: POST with a literal Host is still 405 (read-only)", r.status == 405, str(r.status))
        c.close()
        on.shutdown()
        off.shutdown()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 3. wiring: the real process, started through main() with and without --allow-ip-hosts
    for flag in (True, False):
        d = tempfile.mkdtemp(prefix="bma_ui_lan_proc_")
        port = free_port()
        for sub in ("pipeline\\analysis", "pipeline\\logs", "reports", "rollups"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        args = [
            sys.executable,
            "-I",
            os.path.join(HERE, "bma_ui.py"),
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-board",
            "--pipeline-root",
            os.path.join(d, "pipeline"),
            "--reports-dir",
            os.path.join(d, "reports"),
            "--rollup-dir",
            os.path.join(d, "rollups"),
        ]
        if flag:
            args.append("--allow-ip-hosts")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            up = False
            for _ in range(80):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    up = True
                    break
                except OSError:
                    time.sleep(0.25)
            s_ip, _ = get_status(port, f"203.0.113.7:{port}") if up else (-1, b"")
            s_lo, _ = get_status(port, f"localhost:{port}") if up else (-1, b"")
            check(
                f"wiring: the real server started {'WITH' if flag else 'WITHOUT'} --allow-ip-hosts {'serves' if flag else 'refuses'} a literal Host ({200 if flag else 403}), localhost is 200",
                up and s_ip == (200 if flag else 403) and s_lo == 200,
                f"up={up} ip={s_ip} localhost={s_lo}",
            )
        finally:
            proc.terminate()
            proc.wait(timeout=15)
            shutil.rmtree(d, ignore_errors=True)
    hp = subprocess.run([sys.executable, "-I", os.path.join(HERE, "bma_ui.py"), "--help"], capture_output=True, text=True).stdout
    check("--help documents --allow-ip-hosts", "--allow-ip-hosts" in hp)


run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
