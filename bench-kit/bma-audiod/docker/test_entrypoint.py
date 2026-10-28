#!/usr/bin/env python3
"""Tests for docker/entrypoint.sh: the real script run by sh. The only substitute is the `python` it execs, a recording
shim on PATH that prints its argv, one argument per line, so the exact command line each role builds is asserted.
Hermetic (temp dir). POSIX only (exit 77 = skipped elsewhere). Run:  python test_entrypoint.py  (exit 0 = pass)."""

import os, shutil, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(HERE, "entrypoint.sh")
SH = shutil.which("sh")
if os.name != "posix" or not SH:
    print("SKIP: needs a POSIX sh")
    sys.exit(77)

n, fails = 0, []


def check(name, got, want):
    global n
    n += 1
    if got != want:
        fails.append(name)
        print(f"FAIL {name}:\n  got  {got!r}\n  want {want!r}")


root = tempfile.mkdtemp()
shim = os.path.join(root, "bin")
os.makedirs(shim)
with open(os.path.join(shim, "python"), "w") as f:
    f.write('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
os.chmod(os.path.join(shim, "python"), 0o755)
data, models = os.path.join(root, "data"), os.path.join(root, "models")
os.makedirs(data)


def run(args, **env):
    e = {"PATH": shim + os.pathsep + "/usr/bin:/bin", "BMA_DATA_DIR": data, "BMA_MODEL_DIR": models}
    e.update(env)
    e = {k: v for k, v in e.items() if v is not None}
    r = subprocess.run([SH, ENTRY] + args, capture_output=True, text=True, env=e, timeout=30)
    return r.returncode, r.stdout.splitlines(), r.stderr


# analyzer: explicit flags from the two directories, the image's mask
rc, out, err = run(["analyzer"])
check("analyzer rc", rc, 0)
check(
    "analyzer argv",
    out,
    [
        "/app/host.py",
        "--listen",
        "0.0.0.0:48300",
        "--out",
        data + "/analysis",
        "--history-dir",
        data + "/history",
        "--panns-dir",
        models + "/panns_data",
        "--tonal-mask",
        "/app/tonal_mask.json",
    ],
)
check("analyzer made its dirs", sorted(os.listdir(data)), ["analysis", "history", "logs"])
check("analyzer made the panns_data dir the image links to", os.listdir(models) if os.path.isdir(models) else None, ["panns_data"])
# positive twin: a mask under <data>/config replaces the image's, and extra arguments are appended
os.makedirs(os.path.join(data, "config"))
open(os.path.join(data, "config", "tonal_mask.json"), "w").close()
rc, out, err = run(["analyzer", "--device", "cpu"])
check("analyzer config mask + extra args", out[-4:], ["--tonal-mask", data + "/config/tonal_mask.json", "--device", "cpu"])
check("analyzer without BMA_MODEL_DIR fails loudly", run(["analyzer"], BMA_MODEL_DIR=None)[0] != 0, True)

# classifier: output goes to the log files; the geo filter appears only when its file exists
rc, out, err = run(["classifier"])
check("classifier rc", rc, 0)
check("classifier stdout is redirected, not printed", out, [])
with open(os.path.join(data, "logs", "detections.jsonl")) as f:
    check("classifier argv (no geo file)", f.read().splitlines(), ["/app/bma_classify.py", "--udp", "0.0.0.0:48200", "--slot", "0", "--min-conf", "0.25"])
open(os.path.join(data, "config", "geo_county.json"), "w").close()
run(["classifier"])
with open(os.path.join(data, "logs", "detections.jsonl")) as f:
    lines = f.read().splitlines()
check(
    "classifier argv (geo file present)",
    lines[-9:],
    ["/app/bma_classify.py", "--udp", "0.0.0.0:48200", "--slot", "0", "--min-conf", "0.25", "--geo-config", data + "/config/geo_county.json"],
)

# dashboard: the board panel is on only for an IPv4 BMA_BOARD
base = [
    "/app/ui/bma_ui.py",
    "--bind",
    "0.0.0.0",
    "--port",
    "48500",
    "--allow-ip-hosts",
    "--pipeline-root",
    data,
    "--reports-dir",
    data + "/reports",
    "--rollup-dir",
    data + "/reports/ui",
]
board = ["--board-url", "http://192.0.2.7:8470", "--token-file", "/run/secrets/dspd_token"]
rc, out, err = run(["dashboard"], BMA_BOARD="192.0.2.7")
check("dashboard with an IPv4 board", (rc, out), (0, base + board))
rc, out, err = run(["dashboard"], BMA_BOARD="")
check("dashboard with BMA_BOARD empty", (rc, out, err), (0, base + ["--no-board"], "BMA_BOARD not set: board panel off" + chr(10)))
rc, out, err = run(["dashboard"], BMA_BOARD="00-00-5E-00-53-01")
check("dashboard with a MAC is not a board address", (rc, out, err), (0, base + ["--no-board"], "BMA_BOARD is not an IPv4 address: board panel off" + chr(10)))
rc, out, err = run(["dashboard"], BMA_BOARD="board.example")
check("dashboard with a host name is not a board address", (rc, out), (0, base + ["--no-board"]))

# HTTPS: a complete <data>/tls pair adds --tls-cert/--tls-key; a lone file is named on stderr and nothing is added; neither = unchanged (the checks above)
tlsd = os.path.join(data, "tls")
os.makedirs(tlsd)
open(os.path.join(tlsd, "dashboard_cert.pem"), "w").write("x")
rc, out, err = run(["dashboard"], BMA_BOARD="")
check(
    "lone certificate: warned on stderr, no TLS flags",
    (rc, out, err),
    (
        0,
        base + ["--no-board"],
        "BMA_BOARD not set: board panel off"
        + chr(10)
        + f"TLS needs both {tlsd}/dashboard_cert.pem and {tlsd}/dashboard_key.pem (readable and non-empty): serving plain HTTP"
        + chr(10),
    ),
)
open(os.path.join(tlsd, "dashboard_key.pem"), "w").write("x")
rc, out, err = run(["dashboard"], BMA_BOARD="")
check(
    "certificate and key: --tls-cert and --tls-key with those paths, ahead of the other arguments",
    (rc, out),
    (0, base + ["--tls-cert", tlsd + "/dashboard_cert.pem", "--tls-key", tlsd + "/dashboard_key.pem", "--no-board"]),
)
open(os.path.join(tlsd, "dashboard_key.pem"), "w").close()
rc, out, err = run(["dashboard"], BMA_BOARD="")
check("an empty key file is not a pair: warned, no TLS flags", (rc, out), (0, base + ["--no-board"]))
shutil.rmtree(tlsd)

# dashboard token file: a readable, non-empty file is silent; an unreadable, missing or empty one is named on stderr
# (the panel still starts, without a token). BMA_TOKEN_MOUNT moves the mount point so the test can create the files.
tokdir = os.path.join(root, "tok")
os.makedirs(tokdir)
good = os.path.join(tokdir, "good")
open(good, "w").write("secret" + chr(10))
empty = os.path.join(tokdir, "empty")
open(empty, "w").close()
locked = os.path.join(tokdir, "locked")
open(locked, "w").write("secret" + chr(10))
os.chmod(locked, 0)
uid = os.getuid()
rc, out, err = run(["dashboard"], BMA_BOARD="192.0.2.7", BMA_TOKEN_MOUNT=good)
check("token readable: no warning, the mount is passed as --token-file", (rc, out[-2:], err), (0, ["--token-file", good], ""))
rc, out, err = run(["dashboard"], BMA_BOARD="192.0.2.7", BMA_TOKEN_MOUNT=empty)
check(
    "token empty: named on stderr, panel args unchanged",
    (rc, out[-2:], err),
    (0, ["--token-file", empty], f"dspd token file {empty} is empty (BMA_DSPD_TOKEN_FILE unset?): the board panel runs without a token" + chr(10)),
)
missing = os.path.join(tokdir, "nope")
want_unreadable = lambda p: (
    f"dspd token file {p} is not readable by uid {uid}: make the host file readable by that uid (chown/chmod); the board panel runs without a token" + chr(10)
)
rc, out, err = run(["dashboard"], BMA_BOARD="192.0.2.7", BMA_TOKEN_MOUNT=missing)
check("token missing: named as not readable", (rc, err), (0, want_unreadable(missing)))
if uid != 0:  # root reads a mode-0 file anyway
    rc, out, err = run(["dashboard"], BMA_BOARD="192.0.2.7", BMA_TOKEN_MOUNT=locked)
    check("token mode 0 (the 0600-owned-by-someone-else case): named as not readable", (rc, err), (0, want_unreadable(locked)))
rc, out, err = run(["dashboard"], BMA_BOARD="", BMA_TOKEN_MOUNT=locked)
check("no board: the token file is not looked at", (rc, err), (0, "BMA_BOARD not set: board panel off" + chr(10)))
os.chmod(locked, 0o600)

# models role and unknown roles: the role is exec'd as a command
rc, out, err = run(["echo", "hello"])
check("other roles exec the command", (rc, out), (0, ["hello"]))

print(f"{chr(10)}{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
