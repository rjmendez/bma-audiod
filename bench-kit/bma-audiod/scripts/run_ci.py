#!/usr/bin/env python3
"""Run the CI tests listed in scripts/ci_manifest.toml: one tier, one OS, the areas a change touches. Stdlib only.

    python scripts/run_ci.py --tier fast --os linux --areas auto --base master      changed areas, fast tier
    python scripts/run_ci.py --tier full --os linux --areas all --retry-timing 1     everything for this OS
    python scripts/run_ci.py --tier fast --areas auto --base master --list           print the selection, run nothing
    python scripts/run_ci.py --tier fast --areas guards                              the always-on guards

--tier fast runs the fast entries; --tier full runs fast and full entries. --areas: auto (changed files via --base, or
--changed), all (every area except guards), guards, or a comma list. A file no rule matches selects every area.
A rule is {prefix and/or suffix, areas, only?, linux_only?} in the manifest. Statuses: PASS, FAIL, SKIP (exit 77 or an unavailable `needs`, with the reason), FLAKY-PASS (a timing_sensitive entry that
failed and passed on a re-run: --retry-timing N allows N re-runs). Exit 0 unless a test FAILed (1); usage error 2.
Writes a table to $GITHUB_STEP_SUMMARY when set, and JSON with --report FILE."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
MANIFEST = os.path.join(HERE, "ci_manifest.toml")
PREFIX = "bench-kit/bma-audiod/"
TIERS = ("fast", "full")
OSES = ("linux", "windows", "both")
NEEDS = ("none", "torch", "onnx", "browser", "node", "powershell", "pwsh", "wsl")
EXIT_SKIP = 77


def load_manifest(path=MANIFEST):
    with open(path, "rb") as f:
        return tomllib.load(f)


def host_os():
    return "windows" if sys.platform == "win32" else "linux"


def changed_files(base, repo=BMA):
    """-> repo-relative '/'-separated paths changed between the merge base of BASE and HEAD, and HEAD."""
    r = subprocess.run(["git", "-C", repo, "diff", "--name-only", f"{base}...HEAD"], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git diff {base}...HEAD failed: {r.stderr.strip()}")
    return [p.strip() for p in r.stdout.splitlines() if p.strip()]


def rule_matches(r, f):
    return all(f.startswith(r["prefix"]) if k == "prefix" else f.endswith(r["suffix"]) for k in ("prefix", "suffix") if k in r)


def areas_for(files, rules, all_areas, os_name="linux"):
    """-> (sorted areas, files no rule matched). A rule matches when its `prefix` (kit-relative) and `suffix` both hold.
    The first matching rule with only = true decides a file alone; otherwise matches are unioned. A rule with
    linux_only = true (a reverse dependency) is ignored for windows. A file no rule matches selects every area."""
    out, unmatched = set(), []
    live = [r for r in rules if not (r.get("linux_only") and os_name == "windows")]
    for f in files:
        f = f[len(PREFIX) :] if f.startswith(PREFIX) else f
        only = next((r for r in live if r.get("only") and rule_matches(r, f)), None)
        hits = [only] if only else [r for r in live if rule_matches(r, f)]
        if not hits:
            unmatched.append(f)
            out |= set(all_areas)
        for r in hits:
            out |= set(r["areas"])
    return sorted(out), unmatched


def select(entries, tier, os_name, areas):
    """-> entries of the given areas that this tier and OS run, in manifest order."""
    tiers = ("fast",) if tier == "fast" else TIERS
    return [e for e in entries if e["area"] in areas and e["tier"] in tiers and e["os"] in (os_name, "both")]


def probe_torch():
    return subprocess.run([sys.executable, "-c", "import torch"], capture_output=True).returncode == 0


def probe_onnx():
    return subprocess.run([sys.executable, "-c", "import onnx, onnxruntime"], capture_output=True).returncode == 0


def probe_browser():
    if shutil.which("msedge") or shutil.which("msedge.exe"):
        return True
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var)
        if base and os.path.exists(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")):
            return True
    return False


PROBES = {
    "torch": (probe_torch, "torch is not installed"),
    "onnx": (probe_onnx, "onnx and onnxruntime are not installed"),
    "browser": (probe_browser, "Microsoft Edge is not installed"),
    "node": (lambda: shutil.which("node") is not None, "node is not on PATH"),
    "powershell": (lambda: shutil.which("powershell.exe") is not None, "powershell.exe is not on PATH"),
    "pwsh": (lambda: shutil.which("pwsh") is not None or shutil.which("powershell") is not None, "no pwsh or powershell on PATH"),
    "wsl": (lambda: shutil.which("wsl.exe") is not None, "wsl.exe is not on PATH"),
}


def long_path(p):
    """The long form of a Windows path (the runner's temp dir may be an 8.3 short name; the ps1 tests compare paths)."""
    if sys.platform != "win32":
        return p
    import ctypes

    buf = ctypes.create_unicode_buffer(32768)
    return buf.value if ctypes.windll.kernel32.GetLongPathNameW(p, buf, len(buf)) else p


def work_dir(e):
    """-> the scratch directory an entry gets for {tmp} in its args: <RUNNER_TEMP or the temp dir>/<work or the file stem>; not created."""
    base = long_path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    return os.path.join(base, e.get("work") or os.path.splitext(os.path.basename(e["path"]))[0])


def valid_need(need):
    return need in NEEDS or re.fullmatch(r"py:[A-Za-z_][\w.]*", need) is not None


def need_status(need, probes):
    """-> (available, reason when not). `py:<module>` is an import probe; the others are in PROBES."""
    if need.startswith("py:"):
        mod = need[3:]
        ok = probes[need]() if need in probes else subprocess.run([sys.executable, "-c", f"import {mod}"], capture_output=True).returncode == 0
        return ok, f"{mod} is not installed"
    return probes[need](), PROBES[need][1]


def command(e):
    """-> argv. `args` entries may contain {tmp}, the entry's work_dir. With `cwd` (kit-relative) the script path is relative to it."""
    args = [a.replace("{tmp}", work_dir(e)) for a in e.get("args", [])]
    p = os.path.relpath(e["path"], e["cwd"]).replace(os.sep, "/") if e.get("cwd") else e["path"]
    if p.endswith(".js"):
        return ["node", p, *args]
    if p.endswith(".ps1"):
        return ["powershell.exe", "-NoProfile", "-File", p, *args]
    return [sys.executable, p, *args]


def run_process(cmd, timeout, cwd=BMA):
    """-> (exit code or None on timeout, combined output)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as ex:
        out = ex.stdout or b""
        return None, out.decode("utf-8", "replace") if isinstance(out, bytes) else out


def skip_reason(output):
    for ln in reversed(output.strip().splitlines()):
        if "SKIP" in ln:
            return ln.strip()[:160]
    return "exit 77"


def execute(e, probes, run=run_process, retry_timing=0, clock=time.monotonic):
    """-> result dict {path, area, status, seconds, reason, attempts, output}. `probes`: need -> bool; `run`: (cmd, timeout) -> (code, output)."""
    need = e.get("needs", "none")
    ok, why = need_status(need, probes) if need != "none" else (True, "")
    if not ok:
        return {"path": e["path"], "area": e["area"], "status": "SKIP", "seconds": 0.0, "reason": why, "attempts": 0, "output": ""}
    work = work_dir(e)
    shutil.rmtree(work, ignore_errors=True)
    try:
        cmd = command(e)
        attempts, t0 = 0, clock()
        while True:
            attempts += 1
            code, out = run(cmd, e.get("timeout", 300), os.path.join(BMA, e["cwd"]) if e.get("cwd") else BMA)
            if code == 0:
                status = "FLAKY-PASS" if attempts > 1 else "PASS"
                reason = f"passed on attempt {attempts}" if attempts > 1 else ""
                break
            if code == EXIT_SKIP:
                status, reason = "SKIP", skip_reason(out)
                break
            if e.get("timing_sensitive") and attempts <= retry_timing:
                continue
            status = "FAIL"
            reason = f"timeout after {e.get('timeout', 300)}s" if code is None else f"exit {code}"
            break
        return {
            "path": e["path"],
            "area": e["area"],
            "status": status,
            "seconds": round(clock() - t0, 1),
            "reason": reason,
            "attempts": attempts,
            "output": out,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def summary_markdown(results, title):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = [f"### {title}", "", "| status | count |", "|---|---|"] + [f"| {k} | {counts[k]} |" for k in sorted(counts)]
    lines += ["", f"total {sum(r['seconds'] for r in results):.0f} s", "", "| test | area | status | s | note |", "|---|---|---|---|---|"]
    lines += [f"| {r['path']} | {r['area']} | {r['status']} | {r['seconds']} | {r['reason']} |" for r in results]
    return "\n".join(lines) + "\n"


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tier", choices=TIERS, required=True)
    ap.add_argument("--os", dest="os_name", choices=("linux", "windows"), default=host_os())
    ap.add_argument("--areas", default="auto")
    ap.add_argument("--base", help="git ref to diff against for --areas auto")
    ap.add_argument("--changed", nargs="*", help="changed files instead of git diff")
    ap.add_argument("--retry-timing", type=int, default=0, help="re-runs allowed for a failing timing_sensitive entry")
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--list", action="store_true", help="print the selection, run nothing")
    ap.add_argument("--plan", action="store_true", help="print linux=N and windows=N test counts for this tier and areas (for $GITHUB_OUTPUT), run nothing")
    ap.add_argument("--manifest", default=MANIFEST)
    return ap.parse_args(argv)


def resolve_areas(a, man):
    """-> (areas, note) or raises ValueError."""
    known = sorted({e["area"] for e in man["tests"]})
    work = [x for x in known if x != "guards"]
    if a.areas == "all":
        return work, "all areas"
    if a.areas == "auto":
        if a.changed is None and not a.base:
            raise ValueError("--areas auto needs --base or --changed")
        files = a.changed if a.changed is not None else changed_files(a.base)
        areas, unmatched = areas_for(files, man["rules"], work, a.os_name)
        note = f"{len(files)} changed file(s)" + (f"; no rule for {unmatched}: every area selected" if unmatched else "")
        return areas, note
    areas = [x for x in a.areas.split(",") if x]
    bad = [x for x in areas if x not in known]
    if bad:
        raise ValueError(f"unknown area(s) {bad}; known: {known}")
    return areas, "named"


def main(argv=None, probes=None, run=run_process, out=sys.stdout):
    a = parse_args(argv)
    probes = probes or {k: v[0] for k, v in PROBES.items()}
    man = load_manifest(a.manifest)
    try:
        areas, note = resolve_areas(a, man)
    except (ValueError, RuntimeError) as ex:
        print(f"run_ci: {ex}", file=sys.stderr)
        return 2
    chosen = select(man["tests"], a.tier, a.os_name, areas)
    if a.plan:
        counts = {}
        for o in ("linux", "windows"):
            oareas, _ = resolve_areas(argparse.Namespace(**{**vars(a), "os_name": o}), man)
            counts[o] = len(select(man["tests"], a.tier, o, oareas))
        print(f"linux={counts['linux']}\nwindows={counts['windows']}", file=out)
        return 0
    print(f"tier {a.tier}, os {a.os_name}, areas {','.join(areas) or '(none)'} ({note}); {len(chosen)} test(s)", file=out)
    if a.list:
        for e in chosen:
            print(
                f"{e['area']:<9} {e['tier']:<5} {e['os']:<8} {'timing' if e.get('timing_sensitive') else '-':<6} {e.get('needs', 'none'):<10} {e['path']}",
                file=out,
            )
        return 0
    results = []
    for e in chosen:
        r = execute(e, probes, run, a.retry_timing)
        results.append(r)
        print(f"{r['status']:<10} {r['seconds']:>7.1f}s  {r['path']}" + (f"  [{r['reason']}]" if r["reason"] else ""), file=out, flush=True)
        if r["status"] in ("FAIL", "FLAKY-PASS"):
            print("    " + "\n    ".join(r["output"].strip().splitlines()[-25:]), file=out, flush=True)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FLAKY-PASS", "SKIP", "FAIL")}
    print(
        f"\n{counts['PASS']} pass, {counts['FLAKY-PASS']} flaky-pass, {counts['SKIP']} skip, {counts['FAIL']} fail in {sum(r['seconds'] for r in results):.0f} s",
        file=out,
    )
    title = f"{a.tier} tier, {a.os_name}, areas: {','.join(areas) or 'none'}"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8", newline="\n") as f:
            f.write(summary_markdown(results, title))
    if a.report:
        with open(a.report, "w", encoding="utf-8", newline="\n") as f:
            json.dump(
                {
                    "tier": a.tier,
                    "os": a.os_name,
                    "areas": areas,
                    "counts": counts,
                    "results": [{k: v for k, v in r.items() if k != "output"} for r in results],
                },
                f,
                indent=1,
            )
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
