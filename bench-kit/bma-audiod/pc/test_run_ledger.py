#!/usr/bin/env python3
"""Runs pc/test_run_ledger.ps1 (Pester-free, Windows PowerShell 5.1) in a temp directory, then re-reads the ledger files it left
behind with json.loads and checks exact decoded values, so the PowerShell JSON writer is judged by a real JSON parser and not
only by string comparison. Needs powershell.exe (Windows). Run:  python -I test_run_ledger.py   (exit 0 = all pass)"""
import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
n_checks, fails = 0, []


def check(name, cond, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [" + str(detail) + "]"))
    if not cond:
        fails.append(name)


def rows(path):
    with open(path, "rb") as f:
        raw = f.read()
    return [json.loads(l.decode("utf-8")) for l in raw.split(b"\n") if l]


work = tempfile.mkdtemp(prefix="run_ledger_test_")
try:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-File", os.path.join(HERE, "test_run_ledger.ps1"), "-Work", work], capture_output=True, text=True, timeout=120)
    check("the PowerShell smoke test exits 0 and reports no failure", r.returncode == 0 and " 0 failure(s)" in r.stdout and "FAIL " not in r.stdout, r.stdout[-600:] + r.stderr[-300:])
    L = rows(os.path.join(work, "history", "run_ledger.jsonl"))
    check("main ledger: 6 lines decode as JSON", len(L) == 6, len(L))
    check("the version with quotes and a backslash round-trips through the PowerShell escaper",
          L[0]["version"] == 'abc123 fix "quotes" in C:\\x\\y', L[0]["version"])
    check("start event decodes to exact values", L[0] == {"k": "run_event", "v": 1, "t": 1792900000.123, "proc": "sink", "ev": "start", "pid": 4242,
                                                           "exit_code": None, "exit_code_known": False, "uptime_s": None, "version": 'abc123 fix "quotes" in C:\\x\\y'}, L[0])
    check("exit code -1073741523 (STATUS_DLL_NOT_FOUND family) is an exact negative integer", (L[1]["exit_code"], L[1]["exit_code_known"], L[1]["uptime_s"]) == (-1073741523, True, 12.3), L[1])
    check("an unknown exit code is null and known false; 0 is known", (L[2]["exit_code"], L[2]["exit_code_known"], L[3]["exit_code"], L[3]["exit_code_known"]) == (None, False, 0, True))
    check("a missing VERSION file gives version null", L[5]["version"] is None, L[5])
    G = rows(os.path.join(work, "guard", "history", "run_ledger.jsonl"))
    sup = [x for x in G if x["ev"] == "suppressed"]
    check("crash-loop guard: 3 analysis exits, one suppressed record with count 2, then the next-hour start",
          [x["ev"] for x in G if x["proc"] == "analysis"] == ["exit", "exit", "exit", "suppressed", "start"] and len(sup) == 1 and sup[0]["count"] == 2, G)
    R1, R2 = rows(os.path.join(work, "rot", "history", "run_ledger.jsonl.1")), rows(os.path.join(work, "rot", "history", "run_ledger.jsonl"))
    check("rotation: backup then main hold pids 1-6 in order, nothing lost or duplicated", [x["pid"] for x in R1 + R2] == [1, 2, 3, 4, 5, 6], [x["pid"] for x in R1 + R2])
    P = rows(os.path.join(work, "proc", "history", "run_ledger.jsonl"))
    check("real child: start pid equals exit pid, exit code 7 known (handle touched); the child without the touch has exit_code null",
          P[0]["pid"] == P[1]["pid"] and (P[1]["exit_code"], P[1]["exit_code_known"]) == (7, True) and (P[2]["proc"], P[2]["exit_code"], P[2]["exit_code_known"]) == ("child_nohandle", None, False), P)
    # every line is valid against the key set of the contract (the schema check itself runs in contracts/test_contracts.py)
    keys = {"k", "v", "t", "proc", "ev", "pid", "exit_code", "exit_code_known", "uptime_s", "version"}
    check("every ledger line has the contract's 10 keys (plus count on suppressed)", all(keys <= set(x) <= keys | {"count"} for x in L + G + R1 + R2 + P))
finally:
    shutil.rmtree(work, ignore_errors=True)

print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
