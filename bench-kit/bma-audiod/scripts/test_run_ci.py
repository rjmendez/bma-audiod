#!/usr/bin/env python3
"""Tests for scripts/run_ci.py: area selection, tier/os selection, retry and FLAKY-PASS, skips, exit codes, plan output.
The process runner is a recording stub (no test is spawned); expected values are written by hand.
Run: python scripts/test_run_ci.py"""

import importlib.util
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("run_ci", os.path.join(HERE, "run_ci.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

fails, n = [], 0


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


MAN = R.load_manifest()
RULES = MAN["rules"]
WORK = sorted({e["area"] for e in MAN["tests"]} - {"guards"})
K = "bench-kit/bma-audiod/"


def areas(files, os_name="linux"):
    return R.areas_for(files, RULES, WORK, os_name)[0]


# area selection from changed files (real manifest rules)
eq("docs-only change selects no area", areas([K + "docs/CI.md", K + "analysis/README.md"]), [])
eq("a ui change selects ui and its dependents", areas([K + "ui/bma_ui.py"]), ["docker", "pc", "scripts", "ui"])
eq("a ui change on windows selects only ui", areas([K + "ui/bma_ui.py"], "windows"), ["ui"])
eq("a pc change on windows selects only pc", areas([K + "pc/bma_spectrum.py"], "windows"), ["pc"])
eq("an analysis change on windows selects no other area", areas([K + "analysis/host.py"], "windows"), ["analysis"])
eq(
    "a contracts change selects contracts, analysis, tools, ui, scripts",
    areas([K + "contracts/check_contracts.py"]),
    ["analysis", "contracts", "scripts", "tools", "ui"],
)
eq("a feature registry change selects scripts and tools", areas([K + "docs/FEATURES.md"]), ["scripts", "tools"])
eq("a workflow change selects scripts", areas([".github/workflows/ci.yml"]), ["scripts"])
eq("a mixed change is the union", areas([K + "docs/CI.md", K + "tools/daily_digest.py", K + "pc/x.ps1"]), ["docker", "pc", "scripts", "tools"])
eq("no changed file selects nothing", areas([]), [])
eq("a file no rule matches selects every area", R.areas_for(["Makefile"], RULES, WORK), (WORK, ["Makefile"]))
syn = [
    {"prefix": "a/", "areas": ["x"]},
    {"prefix": "a/b/", "areas": ["y"]},
    {"suffix": ".md", "areas": []},
    {"prefix": "z/", "areas": ["w"], "linux_only": True},
]
eq("synthetic: rules are unioned", R.areas_for(["a/b/c.py"], syn, ["x", "y", "w"])[0], ["x", "y"])
eq("synthetic: a suffix rule with no areas selects nothing", R.areas_for(["a/b/c.md"], syn[2:3], ["x"])[0], [])
onl = [{"prefix": "c/", "suffix": ".md", "areas": ["c"], "only": True}, {"suffix": ".md", "areas": [], "only": True}, {"prefix": "c/", "areas": ["x"]}]
eq("synthetic: an only rule decides alone (prefix and suffix both hold)", R.areas_for(["c/n.md"], onl, ["x", "c"])[0], ["c"])
eq("synthetic: the first only rule wins over a later only rule", R.areas_for(["d/n.md"], onl, ["x", "c"])[0], [])
eq("synthetic: a non-md file in c/ skips the only rules (twin)", R.areas_for(["c/n.py"], onl, ["x", "c"])[0], ["x"])
eq("synthetic: linux_only applies on linux", R.areas_for(["z/q"], syn, ["x"], "linux")[0], ["w"])
eq("synthetic: linux_only is ignored on windows, so the file is unmatched (twin)", R.areas_for(["z/q"], syn, ["x"], "windows"), (["x"], ["z/q"]))

# tier and os selection
E = [
    {"path": "a.py", "area": "x", "tier": "fast", "os": "both"},
    {"path": "b.py", "area": "x", "tier": "full", "os": "linux"},
    {"path": "c.py", "area": "y", "tier": "fast", "os": "windows"},
    {"path": "d.py", "area": "x", "tier": "full", "os": "windows"},
]
paths = lambda es: [e["path"] for e in es]  # noqa: E731
eq("fast tier, linux, area x", paths(R.select(E, "fast", "linux", ["x"])), ["a.py"])
eq("full tier, linux, area x adds the full entries", paths(R.select(E, "full", "linux", ["x"])), ["a.py", "b.py"])
eq("full tier, windows, areas x and y", paths(R.select(E, "full", "windows", ["x", "y"])), ["a.py", "c.py", "d.py"])
eq("an area not named selects nothing", paths(R.select(E, "full", "linux", ["q"])), [])

# manifest shape
check("the manifest has tests and rules", len(MAN["tests"]) > 20 and len(RULES) > 5)
eq("manifest tier values", sorted({e["tier"] for e in MAN["tests"]} - set(R.TIERS)), [])
eq("manifest os values", sorted({e["os"] for e in MAN["tests"]} - set(R.OSES)), [])
eq("manifest needs values", sorted({e.get("needs", "none") for e in MAN["tests"] if not R.valid_need(e.get("needs", "none"))}), [])

# command lines
os.environ["RUNNER_TEMP"] = os.path.join(tempfile.gettempdir(), "rt")
W = lambda name: os.path.join(R.long_path(os.environ["RUNNER_TEMP"]), name)  # noqa: E731
eq("a .py entry runs the current python", R.command({"path": "x/t.py", "args": ["--q"]}), [sys.executable, "x/t.py", "--q"])
eq("a .js entry runs node", R.command({"path": "x/t.js"}), ["node", "x/t.js"])
eq(
    "a .ps1 entry runs powershell.exe -NoProfile -File with its args",
    R.command({"path": "x/t.ps1", "args": ["-Work", "{tmp}"], "work": "sup"}),
    ["powershell.exe", "-NoProfile", "-File", "x/t.ps1", "-Work", W("sup")],
)
eq("{tmp} defaults to the file stem under RUNNER_TEMP", R.command({"path": "x/t_one.ps1", "args": ["-Work", "{tmp}"]})[-1], W("t_one"))
eq("an entry without {tmp} gets no scratch argument (twin)", R.command({"path": "x/t.ps1"}), ["powershell.exe", "-NoProfile", "-File", "x/t.ps1"])
if sys.platform == "win32":
    import ctypes

    sb = ctypes.create_unicode_buffer(300)
    ctypes.windll.kernel32.GetShortPathNameW(tempfile.gettempdir(), sb, 300)
    eq("a short 8.3 temp dir is expanded to the long path", R.long_path(sb.value).lower(), R.long_path(tempfile.gettempdir()).lower())
# old workflow parity (git show master:.github/workflows/ci.yml): supervisor_config and run_ledger got -Work <RUNNER_TEMP>/sup and /ledger
byp = {e["path"]: e for e in MAN["tests"]}
eq(
    "supervisor_config gets -Work <RUNNER_TEMP>/sup",
    R.command(byp["pc/test_supervisor_config.ps1"]),
    ["powershell.exe", "-NoProfile", "-File", "pc/test_supervisor_config.ps1", "-Work", W("sup")],
)
eq(
    "run_ledger.ps1 gets -Work <RUNNER_TEMP>/ledger",
    R.command(byp["pc/test_run_ledger.ps1"]),
    ["powershell.exe", "-NoProfile", "-File", "pc/test_run_ledger.ps1", "-Work", W("ledger")],
)

# execute: recording stub runner
ALL = {k: (lambda: True) for k in R.PROBES}


class Stub:
    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, cmd, timeout, cwd=None):
        self.calls.append((cmd[1:], timeout))
        self.cwds = getattr(self, "cwds", []) + [cwd]
        return self.outcomes.pop(0)


T = {"path": "t.py", "area": "x", "tier": "fast", "os": "both", "timeout": 5}
TS = {**T, "timing_sensitive": True}
s = Stub((0, "ok"))
r = R.execute(T, ALL, s)
eq("a passing test is PASS after one call", (r["status"], r["attempts"], s.calls), ("PASS", 1, [(["t.py"], 5)]))
s = Stub((1, "boom"))
r = R.execute(T, ALL, s, retry_timing=1)
eq("a failing deterministic test is FAIL without a re-run", (r["status"], r["reason"], r["attempts"], len(s.calls)), ("FAIL", "exit 1", 1, 1))
s = Stub((1, "slow"), (0, "ok"))
r = R.execute(TS, ALL, s, retry_timing=1)
eq("a timing test that fails then passes is FLAKY-PASS", (r["status"], r["attempts"], r["reason"], len(s.calls)), ("FLAKY-PASS", 2, "passed on attempt 2", 2))
s = Stub((1, "slow"), (0, "ok"))
r = R.execute(TS, ALL, s, retry_timing=0)
eq("a timing test without --retry-timing is FAIL after one call", (r["status"], len(s.calls)), ("FAIL", 1))
s = Stub((1, "a"), (1, "b"))
r = R.execute(TS, ALL, s, retry_timing=1)
eq("a timing test failing twice is FAIL after exactly two calls", (r["status"], r["attempts"], len(s.calls)), ("FAIL", 2, 2))
s = Stub((1, "a"), (1, "b"), (0, "c"))
r = R.execute(TS, ALL, s, retry_timing=2)
eq("--retry-timing 2 allows two re-runs: FLAKY-PASS on attempt 3", (r["status"], r["attempts"]), ("FLAKY-PASS", 3))
s = Stub((0, "ok"))
eq("a timing test passing first time is a plain PASS (twin)", R.execute(TS, ALL, s, retry_timing=1)["status"], "PASS")
s = Stub((None, "partial"))
r = R.execute(T, ALL, s)
eq("a timeout is FAIL naming the limit", (r["status"], r["reason"]), ("FAIL", "timeout after 5s"))
s = Stub((77, "setup\nSKIP: Edge is missing\n"))
r = R.execute(T, ALL, s)
eq("exit 77 is an explicit SKIP with the test's own reason", (r["status"], r["reason"]), ("SKIP", "SKIP: Edge is missing"))
s = Stub((77, "no reason line"))
eq("exit 77 without a SKIP line says exit 77", R.execute(T, ALL, s)["reason"], "exit 77")
s = Stub((0, "ok"))
r = R.execute({**T, "needs": "torch"}, {**ALL, "torch": lambda: False}, s)
eq("an unavailable need is SKIP with the reason and the test is not run", (r["status"], r["reason"], s.calls), ("SKIP", "torch is not installed", []))
s = Stub((0, "ok"))
r = R.execute({**T, "needs": "torch"}, ALL, s)
eq("an available need runs the test (twin)", (r["status"], len(s.calls)), ("PASS", 1))
for need, why in (
    ("onnx", "onnx and onnxruntime are not installed"),
    ("browser", "Microsoft Edge is not installed"),
    ("node", "node is not on PATH"),
    ("powershell", "powershell.exe is not on PATH"),
    ("pwsh", "no pwsh or powershell on PATH"),
    ("wsl", "wsl.exe is not on PATH"),
):
    eq(f"skip reason for needs={need}", R.execute({**T, "needs": need}, {**ALL, need: lambda: False}, Stub())["reason"], why)

# cwd: the script path is relative to it and the runner is told the directory
eq(
    "an entry with cwd runs its script by the name relative to cwd",
    R.command({"path": "ui-next/test/t.py", "cwd": "ui-next/test", "args": ["--browser", "edge"]}),
    [sys.executable, "t.py", "--browser", "edge"],
)
s = Stub((0, "ok"))
R.execute({**T, "path": "d/t.py", "cwd": "d"}, ALL, s)
eq("the runner is called with the kit-relative cwd joined to the kit", s.cwds, [os.path.join(R.BMA, "d")])
s = Stub((0, "ok"))
R.execute(T, ALL, s)
eq("an entry without cwd runs in the kit (twin)", s.cwds, [R.BMA])
# py:<module> needs
eq(
    "py:module is a valid need, a bare unknown name is not",
    (R.valid_need("py:birdnetlib"), R.valid_need("py:"), R.valid_need("birdnetlib")),
    (True, False, False),
)
s = Stub((0, "ok"))
r = R.execute({**T, "needs": "py:birdnetlib"}, {**ALL, "py:birdnetlib": lambda: False}, s)
eq("a missing python module is SKIP with the reason and the test is not run", (r["status"], r["reason"], s.calls), ("SKIP", "birdnetlib is not installed", []))
s = Stub((0, "ok"))
eq("an installed python module runs the test (twin)", R.execute({**T, "needs": "py:birdnetlib"}, {**ALL, "py:birdnetlib": lambda: True}, s)["status"], "PASS")
eq(
    "the real import probe: a stdlib module is available, a made-up one is not",
    (R.need_status("py:json", {})[0], R.need_status("py:no_such_module_zz", {})),
    (True, (False, "no_such_module_zz is not installed")),
)

# main: temp manifest, stub runner, exit codes
tmp = tempfile.mkdtemp()
mpath = os.path.join(tmp, "m.toml")
with open(mpath, "w", encoding="utf-8", newline="\n") as f:
    f.write(
        'rules = [{ prefix = "x/", areas = ["x"] }, { prefix = "y/", areas = ["y"] }, { suffix = ".md", areas = [] }]\n'
        "tests = [\n"
        '  { path = "x/test_a.py", area = "x", tier = "fast", os = "both" },\n'
        '  { path = "x/test_b.py", area = "x", tier = "full", os = "both", timing_sensitive = true },\n'
        '  { path = "y/test_c.py", area = "y", tier = "fast", os = "linux", needs = "torch" },\n'
        '  { path = "g/check.py", area = "guards", tier = "fast", os = "both" },\n'
        "]\n"
    )


def go(argv, probes=ALL, *outcomes):
    s = Stub(*outcomes)
    buf = io.StringIO()
    code = R.main(["--manifest", mpath, "--os", "linux", *argv], probes, s, buf)
    return code, buf.getvalue(), s


code, out, s = go(["--tier", "fast", "--areas", "x"], ALL, (0, ""))
eq("fast tier runs only the fast entry; exit 0", (code, [c[0] for c in s.calls]), (0, [["x/test_a.py"]]))
check("the summary line counts one pass", "1 pass, 0 flaky-pass, 0 skip, 0 fail" in out, out)
code, out, s = go(["--tier", "full", "--areas", "all", "--retry-timing", "1"], ALL, (0, ""), (1, "flaky"), (0, ""), (0, ""))
eq(
    "full tier, all areas: a, b (retried), c; guards excluded; exit 0",
    (code, [c[0][0] for c in s.calls]),
    (0, ["x/test_a.py", "x/test_b.py", "x/test_b.py", "y/test_c.py"]),
)
check("the flaky test is reported FLAKY-PASS", "FLAKY-PASS" in out and "1 flaky-pass" in out, out)
code, out, s = go(["--tier", "full", "--areas", "x"], ALL, (0, ""), (1, "bad"))
eq("a failing test makes exit 1 and prints its output", (code, "bad" in out, "1 fail" in out), (1, True, True))
code, out, s = go(["--tier", "fast", "--areas", "y"], {**ALL, "torch": lambda: False})
eq("an all-skip run exits 0 and names the reason", (code, "SKIP" in out, "torch is not installed" in out, s.calls), (0, True, True, []))
code, out, s = go(["--tier", "fast", "--areas", "guards"], ALL, (0, ""))
eq("guards run when named", (code, [c[0] for c in s.calls]), (0, [["g/check.py"]]))
code, out, s = go(["--tier", "fast", "--areas", "nope"])
eq("an unknown area is a usage error (exit 2), nothing runs", (code, s.calls), (2, []))
code, out, s = go(["--tier", "fast", "--areas", "auto"])
eq("--areas auto without --base or --changed is a usage error (exit 2)", code, 2)
code, out, s = go(["--tier", "fast", "--areas", "auto", "--changed", "x/foo.py", "README.md"], ALL, (0, ""))
eq("auto with --changed selects x and runs its fast test", (code, [c[0] for c in s.calls]), (0, [["x/test_a.py"]]))
code, out, s = go(["--tier", "fast", "--areas", "auto", "--changed", "notes.md"])
eq("auto with a docs-only change runs nothing and exits 0", (code, s.calls, "0 test(s)" in out), (0, [], True))
code, out, s = go(["--tier", "full", "--areas", "all", "--list"])
eq("--list prints one line per selected test and runs nothing", (code, s.calls, len([ln for ln in out.splitlines() if ln.endswith(".py")])), (0, [], 3))
check(
    "--list shows the timing marker and needs",
    any("timing" in ln and "x/test_b.py" in ln for ln in out.splitlines()) and any("torch" in ln for ln in out.splitlines()),
    out,
)
code, out, s = go(["--tier", "full", "--areas", "all", "--plan"])
eq("--plan prints exact key=value lines for GITHUB_OUTPUT", out, "linux=3\nwindows=2\n")
rep = os.path.join(tmp, "r.json")
code, out, s = go(["--tier", "fast", "--areas", "x", "--report", rep], ALL, (0, ""))
doc = json.load(open(rep, encoding="utf-8"))
eq(
    "the JSON report carries tier, areas, counts and per-test rows without output",
    (doc["tier"], doc["areas"], doc["counts"], [(r["path"], r["status"]) for r in doc["results"]], "output" in doc["results"][0]),
    ("fast", ["x"], {"PASS": 1, "FLAKY-PASS": 0, "SKIP": 0, "FAIL": 0}, [("x/test_a.py", "PASS")], False),
)
sm = os.path.join(tmp, "sum.md")
os.environ["GITHUB_STEP_SUMMARY"] = sm
go(["--tier", "fast", "--areas", "x"], ALL, (0, ""))
del os.environ["GITHUB_STEP_SUMMARY"]
md = open(sm, encoding="utf-8").read()
check("the step summary has the status table and the test row", "| PASS | 1 |" in md and "| x/test_a.py | x | PASS |" in md, md)

print(f"\n{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
