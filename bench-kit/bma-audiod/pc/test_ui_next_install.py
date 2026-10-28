#!/usr/bin/env python3
"""The dashboard's install steps put the ui-next assets where bma_ui.py --ui-next looks for them, and the installed layout starts with the flag.
Runs the REAL ui block of pc/install_task.ps1 (from `$uiApp = Join-Path $app 'ui'` to the `$ver =` line) in PowerShell with $src = a copy of the repo's
ui, ui-next and analysis trees and $app = a scratch dir, then starts the installed bma_ui.py with --ui-next. Also checks the dashboard image's COPY lines.
Exit 77 (SKIP) without PowerShell.

    python test_ui_next_install.py     (PASS/FAIL lines, 'N checks, M failure(s)')"""
import hashlib, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BMA, "ui"))
import bma_ui as U  # noqa: E402

n, fails = 0, []


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [%s]" % detail), flush=True)
    if not ok:
        fails.append(name)


PS = shutil.which("powershell") or shutil.which("pwsh")
if not PS or os.name != "nt":
    print("SKIP: needs Windows PowerShell (the installer uses backslash paths); nothing ran, this is not a pass")
    sys.exit(77)
INSTALLER = open(os.path.join(HERE, "install_task.ps1"), encoding="utf-8").read()
a, b = INSTALLER.index("$uiApp = Join-Path $app 'ui'"), INSTALLER.index("$ver = (git")
BLOCK = INSTALLER[a:b]


def tree_hashes(root):
    return {os.path.relpath(os.path.join(d, f), root).replace("\\", "/"): hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest() for d, _, fs in os.walk(root) for f in fs}


def install(block, tmp):
    src, app = os.path.join(tmp, "src"), os.path.join(tmp, "app")
    for t in ("ui", "ui-next", "analysis"):
        shutil.copytree(os.path.join(BMA, t), os.path.join(src, t), ignore=shutil.ignore_patterns("__pycache__", "test*", "bench", "tools", "*.png"))
    os.makedirs(app, exist_ok=True)
    script = os.path.join(tmp, "run.ps1")
    with open(script, "w", encoding="utf-8") as f:
        f.write("$ErrorActionPreference = 'Stop'\n$src = '%s'\n$app = '%s'\n" % (src, app) + block)
    r = subprocess.run([PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script], capture_output=True, text=True, timeout=120)
    return src, app, r


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(ui_dir, flag):
    port = free_port()
    p = subprocess.Popen([sys.executable, "-I", os.path.join(ui_dir, "bma_ui.py"), "--port", str(port), "--pipeline-root", os.path.join(os.path.dirname(ui_dir), "data"), "--no-board", "--spectrum-port", str(free_udp_port())] + (["--ui-next"] if flag else []),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(60):
        if p.poll() is not None:
            return None, p, port
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return True, p, port
        except OSError:
            time.sleep(0.3)
    return None, p, port


def get(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


tmp = tempfile.mkdtemp(prefix="uinext_inst_")
try:
    src, app, r = install(BLOCK, tmp)
    check("the installer's ui block runs without error", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    want = {"next/" + k: v for k, v in tree_hashes(os.path.join(src, "ui-next", "static")).items() if k in U.NEXT_FILES}
    got = {k: v for k, v in tree_hashes(os.path.join(app, "ui")).items() if k.startswith("next/")}
    check("the pinned app tree holds exactly the %d ui-next files of NEXT_FILES, byte for byte" % len(U.NEXT_FILES), got == want and len(got) == len(U.NEXT_FILES), "%d files, missing %s" % (len(got), sorted(set(want) - set(got))))
    ui_dir = os.path.join(app, "ui")
    os.makedirs(os.path.join(app, "data"), exist_ok=True)
    ok, p, port = start(ui_dir, True)
    try:
        check("installed layout: bma_ui.py --ui-next starts and serves /next/ and /next/engine/dsp.js (200)", ok and (get(port, "/next/"), get(port, "/next/engine/dsp.js")) == (200, 200), (p.poll(), p.stderr.read()[-200:] if p.poll() is not None else ""))
        check("installed layout: ui/ws.py is in the pinned tree and the live route exists (401 without a login on this open server, not 404)", os.path.isfile(os.path.join(ui_dir, "ws.py")) and get(port, "/api/v2/live") == 401, str(get(port, "/api/v2/live")))
    finally:
        p.kill()
        p.wait()
    ok0, p0, port0 = start(ui_dir, False)
    try:
        check("positive twin: the same installed tree without the flag starts and answers /next/ and /api/v2/live with 404", ok0 and (get(port0, "/next/"), get(port0, "/api/v2/live")) == (404, 404))
    finally:
        p0.kill()
        p0.wait()
    # the same install without the copy: the flag refuses to start (this is what a deploy that forgot the assets would do)
    bad_block = "\n".join(l for l in BLOCK.splitlines() if "ui-next\\static" not in l and "$nx" not in l) + "\n"
    tmp2 = tempfile.mkdtemp(prefix="uinext_inst2_")
    try:
        _s, app2, r2 = install(bad_block, tmp2)
        os.makedirs(os.path.join(app2, "data"), exist_ok=True)
        ok2, p2, _ = start(os.path.join(app2, "ui"), True)
        err = p2.stderr.read() if p2.poll() is not None else ""
        p2.kill()
        check("without the copy lines the flag refuses to start and names the missing assets (exit 2)", r2.returncode == 0 and ok2 is None and p2.returncode == 2 and "--ui-next: assets not found" in err, "rc %s %s" % (p2.returncode, err[-160:]))
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)
    # without ws.py the flag also refuses to start, naming the live socket
    no_ws = "\n".join(l for l in BLOCK.splitlines() if "ui\\ws.py" not in l) + "\n"
    tmp3 = tempfile.mkdtemp(prefix="uinext_inst3_")
    try:
        _s3, app3, r3 = install(no_ws, tmp3)
        os.makedirs(os.path.join(app3, "data"), exist_ok=True)
        ok3, p3, _ = start(os.path.join(app3, "ui"), True)
        err3 = p3.stderr.read() if p3.poll() is not None else ""
        p3.kill()
        check("without the ws.py copy line the flag refuses to start and names ui/ws.py (exit 2)", r3.returncode == 0 and ok3 is None and p3.returncode == 2 and "ui/ws.py" in err3, "rc %s %s" % (p3.returncode, err3[-160:]))
    finally:
        shutil.rmtree(tmp3, ignore_errors=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# the image: COPY lines of docker/dashboard.Dockerfile
copies = [ln.split()[1:] for ln in open(os.path.join(BMA, "docker", "dashboard.Dockerfile"), encoding="utf-8").read().splitlines() if ln.startswith("COPY ")]
check("image: ui-next/static/ is copied to /app/ui/next/, next to bma_ui.py in /app/ui/ (WORKDIR) where --ui-next looks", ["ui-next/static/", "/app/ui/next/"] in copies and any(c[-1] == "/app/ui/" and "ui/bma_ui.py" in c for c in copies), str(copies))
check("image: positive twin - the current dashboard's ui/static/ copy is still there", ["ui/static/", "/app/ui/static/"] in copies)
print("\n%d checks, %d failure(s)" % (n, len(fails)))
sys.exit(1 if fails else 0)
