#!/usr/bin/env python3
"""Runs static/bench/features.html in each installed browser (headless, fresh profile) against the mock server and prints a feature matrix.
The page reports by HTTP POST, so no automation protocol is needed (Firefox has dropped CDP). One browser at a time.
    python cross_browser.py [--browsers edge,chrome,firefox] [--json OUT.json]  ->  markdown matrix on stdout"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
import cdp           # noqa: E402
import mock_server   # noqa: E402

PF = os.environ.get("ProgramFiles", "")
EXES = {"edge": cdp.find_edge(), "chrome": os.path.join(PF, "Google", "Chrome", "Application", "chrome.exe"), "firefox": os.path.join(PF, "Mozilla Firefox", "firefox.exe")}


def launch(name, url, prof):
    exe = EXES[name]
    args = ["--headless", "--no-remote", "--profile", prof, url] if name == "firefox" else ["--headless=new", "--user-data-dir=" + prof, "--no-first-run", "--no-default-browser-check", url]
    arg = ",".join("'%s'" % x.replace("'", "''") for x in args)
    subprocess.run(["powershell", "-NoProfile", "-Command", "Start-Process -FilePath '%s' -ArgumentList @(%s) -WindowStyle Hidden" % (exe, arg)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill(prof):
    subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + prof + "*' } | ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--browsers", default="edge,chrome,firefox")
    ap.add_argument("--json", default="")
    ap.add_argument("--timeout", type=float, default=90.0)
    a = ap.parse_args()
    srv = mock_server.Server(("127.0.0.1", 0), mock_server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    got = {}
    for name in a.browsers.split(","):
        if not os.path.exists(EXES.get(name, "")):
            got[name] = {"missing": True}
            continue
        prof = tempfile.mkdtemp(prefix="uinext_%s_" % name)
        t0 = time.time()
        launch(name, "http://127.0.0.1:%d/bench/features.html?id=%s" % (port, name), prof)
        while time.time() - t0 < a.timeout:
            r = [x for x in mock_server.REPORTS if x.get("id") == name]
            if r:
                got[name] = r[-1]
                break
            time.sleep(1)
        else:
            got[name] = {"timeout": True}
        kill(prof)
        shutil.rmtree(prof, ignore_errors=True)
        time.sleep(1)
    if a.json:
        json.dump(got, open(a.json, "w", encoding="utf-8"), indent=1)
    names = [n for n in a.browsers.split(",")]
    feats = []
    for n in names:
        for k in (got.get(n, {}).get("results") or {}):
            if k not in feats:
                feats.append(k)
    cell = lambda n, k: ("n/a" if got.get(n, {}).get("missing") else "no report" if not got.get(n, {}).get("results") else "-" if k not in got[n]["results"] else
                         "info" if got[n]["results"][k]["ok"] is None else "pass" if got[n]["results"][k]["ok"] else "FAIL")
    print("| Feature | " + " | ".join(names) + " |\n|---|" + "---|" * len(names))
    for k in feats:
        print("| %s | %s |" % (k, " | ".join(cell(n, k) for n in names)))
    print()
    for n in names:
        r = got.get(n, {})
        print("%s: %s" % (n, r.get("ua", "missing" if r.get("missing") else "no report")))
    print()
    for n in names:
        for k, v in (got.get(n, {}).get("results") or {}).items():
            if v["ok"] is not True and v.get("detail") or (k.startswith("webgl2: context")):
                print("%s | %s | %s" % (n, k, v.get("detail")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
