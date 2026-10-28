#!/usr/bin/env python3
"""Completeness guard for CI: .github/workflows/ci.yml (triggers, tiers, label gate, only run_ci.py calls, no third-party actions)
and scripts/ci_manifest.toml (every test file has exactly one entry or an explicit skip with a reason; every path exists; valid values).
Pure file checks, no network. Reads the workflow with PyYAML when installed and always with the minimal reader below; both must agree.
Run: python scripts/test_ci_workflow.py"""
import copy
import os
import re
import sys
import tempfile
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(BMA))
WF = os.path.join(ROOT, ".github", "workflows", "ci.yml")
MANIFEST = os.path.join(HERE, "ci_manifest.toml")

fails, n = [], 0


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


# ---- minimal YAML reader: block maps, block lists of maps, flow lists, quoted/plain scalars; no block scalars, anchors or multi-line values

def scalar(v):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    return int(v) if re.fullmatch(r"-?\d+", v) else v


def mini_yaml(text):
    lines = []
    for raw in text.splitlines():
        s = raw.strip()
        if s and not s.startswith("#"):
            assert s not in ("|", ">") and not s.endswith(": |"), f"block scalar not supported: {raw}"
            lines.append([len(raw) - len(raw.lstrip(" ")), s])

    def block(i, ind):
        if lines[i][1].startswith("- "):
            out = []
            while i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("- "):
                lines[i] = [ind + 2, lines[i][1][2:]]
                val, i = block(i, ind + 2)
                out.append(val)
            return out, i
        out = {}
        while i < len(lines) and lines[i][0] == ind and not lines[i][1].startswith("- "):
            if ": " in lines[i][1]:
                key, _, rest = lines[i][1].partition(": ")
            else:
                key, rest = lines[i][1].rstrip(":"), ""
            i += 1
            if rest.strip():
                out[key] = scalar(rest)
            elif i < len(lines) and (lines[i][0] > ind or (lines[i][0] == ind and lines[i][1].startswith("- "))):
                out[key], i = block(i, lines[i][0])
            else:
                out[key] = None
        return out, i

    return block(0, lines[0][0])[0]


def load_both(text):
    """-> (doc from the minimal reader, doc from PyYAML or None)."""
    mini = mini_yaml(text)
    try:
        import yaml
    except ImportError:
        return mini, None
    return mini, yaml.safe_load(text)


def norm(doc):
    """PyYAML reads the key `on` as True."""
    d = dict(doc)
    if True in d:
        d["on"] = d.pop(True)
    return d


def runs(job):
    return [s["run"].strip() for s in job["steps"] if "run" in s]


def uses(doc):
    return [s["uses"] for j in doc["jobs"].values() for s in j["steps"] if "uses" in s]


ALLOWED_USES = ("actions/checkout@v4", "actions/setup-python@v5")
SYSCTL = "sudo sysctl -w net.core.rmem_max=8388608"
PIP = re.compile(r"^python -m pip install -r (scripts/requirements-ci(?:-windows)?\.txt)$")
RUN_CI = re.compile(r"^python scripts/run_ci\.py (.*)$")
JOBS = ["plan", "guards", "fast-linux", "fast-windows", "full-linux", "full-windows"]


def workflow_problems(doc, exists):
    """-> defects of the parsed workflow against the CI design. `exists(path)`: kit-relative file check."""
    doc = norm(doc)
    bad = []
    jobs = doc["jobs"]
    if sorted(jobs) != sorted(JOBS):
        return [f"jobs are {sorted(jobs)}"]
    on = doc["on"]
    if sorted(on) != ["pull_request", "push", "workflow_dispatch"]:
        bad.append(f"triggers are {sorted(on)}")
    else:
        if on["pull_request"].get("types") != ["opened", "synchronize", "reopened", "ready_for_review", "labeled"]:
            bad.append("pull_request types differ from the design")
        if on["push"].get("branches") != ["master"]:
            bad.append("push branches must be [master]")
        inp = on["workflow_dispatch"]["inputs"]
        if sorted(inp) != ["areas", "tier"] or inp["tier"].get("options") != ["fast", "full"] or inp["tier"].get("default") != "fast":
            bad.append("workflow_dispatch inputs differ from the design (tier: fast|full default fast, areas)")
    conc = doc.get("concurrency") or {}
    if "github.ref" not in str(conc.get("group")) or "github.event_name == 'pull_request'" not in str(conc.get("cancel-in-progress")):
        bad.append("concurrency must group by ref and cancel in progress only for pull_request")
    if (doc.get("permissions") or {}).get("contents") != "read":
        bad.append("permissions must be contents: read")
    if ((doc.get("defaults") or {}).get("run") or {}).get("working-directory") != "bench-kit/bma-audiod":
        bad.append("default working directory must be the audio kit")
    bad += [f"third-party or unpinned action {u}" for u in uses(doc) if u not in ALLOWED_USES]
    for name, job in jobs.items():
        if job.get("runs-on") != ("windows-2022" if "windows" in name else "ubuntu-24.04"):
            bad.append(f"{name}: runs-on {job.get('runs-on')}")
        if not isinstance(job.get("timeout-minutes"), int):
            bad.append(f"{name}: no timeout-minutes")
        py = [s["with"]["python-version"] for s in job["steps"] if "python-version" in s.get("with", {})]
        if py != ["3.13" if "windows" in name else "3.12"]:
            bad.append(f"{name}: python-version {py}")
        for s in job["steps"]:
            w = s.get("with", {})
            if s.get("uses") == "actions/setup-python@v5" and name != "plan":
                if w.get("cache") != "pip":
                    bad.append(f"{name}: setup-python without cache: pip")
                dep = str(w.get("cache-dependency-path", "")).replace("bench-kit/bma-audiod/", "")
                if not exists(dep):
                    bad.append(f"{name}: cache-dependency-path {dep!r} missing")
        for r in runs(job):
            m = PIP.match(r)
            if m:
                if not exists(m.group(1)):
                    bad.append(f"{name}: {m.group(1)} missing")
            elif r == SYSCTL:
                if name != "full-linux":
                    bad.append(f"{name}: sysctl belongs to full-linux only")
            elif not RUN_CI.match(r):
                bad.append(f"{name}: step is not a run_ci.py call, a requirements install or the sysctl: {r}")
        if name != "plan" and job.get("needs") != "plan":
            bad.append(f"{name}: must need plan (draft and label gate)")
    for name, tier, os_, retry in (("fast-linux", "fast", "linux", False), ("fast-windows", "fast", "windows", False),
                                   ("full-linux", "full", "linux", True), ("full-windows", "full", "windows", True)):
        calls = [r for r in runs(jobs[name]) if RUN_CI.match(r)]
        if len(calls) != 1 or f"--tier {tier} " not in calls[0] or f"--os {os_} " not in calls[0]:
            bad.append(f"{name}: needs exactly one run_ci.py call with --tier {tier} --os {os_}")
        elif retry != ("--retry-timing 1" in calls[0]):
            bad.append(f"{name}: --retry-timing 1 belongs to the full tier only")
    plan = [r for r in runs(jobs["plan"]) if "run_ci.py" in r]
    if len(plan) != 1 or "--plan" not in plan[0]:
        bad.append("plan: must run run_ci.py --plan")
    if [r for r in runs(jobs["guards"]) if RUN_CI.match(r)] != ["python scripts/run_ci.py --tier fast --os linux --areas guards"]:
        bad.append("guards: must run exactly the guards area")
    if SYSCTL not in runs(jobs["full-linux"]):
        bad.append("full-linux: missing the rmem_max sysctl")
    for name in ("full-linux", "full-windows"):
        cond = str(jobs[name].get("if"))
        bad += [f"{name}: if lacks {g}" for g in ("github.event_name == 'push'", "workflow_dispatch", "inputs.tier == 'full'", "ci:full") if g not in cond]
    for name, key in (("fast-linux", "linux"), ("fast-windows", "windows")):
        cond = str(jobs[name].get("if"))
        if "ci:full" not in cond or "inputs.tier == 'full'" not in cond or "github.event_name != 'push'" not in cond:
            bad.append(f"{name}: if must exclude push, tier=full dispatch and the ci:full label (the full job covers them)")
        if f"outputs.{key} != '0'" not in cond:
            bad.append(f"{name}: if must depend on the plan count")
    pcond = str(jobs["plan"].get("if"))
    if "draft != true" not in pcond or "ci:full" not in pcond:
        bad.append("plan: if must skip drafts and labels other than ci:full")
    return bad


# ---- manifest completeness

AREAS = ("analysis", "ui", "pc", "tools", "contracts", "scripts", "classify", "control", "docker", "guards")
TIERS = ("fast", "full")
OSES = ("linux", "windows", "both")
NEEDS = ("none", "torch", "onnx", "browser", "node", "powershell", "pwsh", "wsl")
PY_NEED = re.compile(r"py:[A-Za-z_][\w.]*")
KEYS = {"path", "area", "tier", "os", "timing_sensitive", "needs", "timeout", "args", "work", "cwd"}
DIR_AREA = {"ui-next": "ui"}
SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", ".venv", "venv"}


def test_files(base):
    """-> every test_*.py / test_*.js / test_*.ps1 under the kit, kit-relative with '/'."""
    out = []
    for d, dirs, files in os.walk(base):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if re.fullmatch(r"test_.*\.(py|js|ps1)", f):
                out.append(os.path.relpath(os.path.join(d, f), base).replace(os.sep, "/"))
    return sorted(out)


def manifest_problems(man, files, exists):
    """-> defects: a test file with no entry or two, an entry whose path is missing, an invalid value, an unknown rule area."""
    bad = []
    skips = man.get("skips", [])
    entries = man["tests"] + skips
    seen = {}
    for e in entries:
        seen[e["path"]] = seen.get(e["path"], 0) + 1
    bad += [f"{p}: {c} manifest entries" for p, c in sorted(seen.items()) if c > 1]
    bad += [f"{p}: test file has no manifest entry or skip" for p in files if p not in seen]
    bad += [f"{e['path']}: path does not exist" for e in entries if not exists(e["path"])]
    bad += [f"{e['path']}: skip without a reason" for e in skips if not str(e.get("reason", "")).strip()]
    for e in man["tests"]:
        p = e["path"]
        if e.get("area") not in AREAS:
            bad.append(f"{p}: area {e.get('area')!r}")
        elif e["area"] != "guards" and DIR_AREA.get(p.split("/")[0], p.split("/")[0]) != e["area"]:
            bad.append(f"{p}: area {e['area']} is not its directory")
        if e.get("tier") not in TIERS:
            bad.append(f"{p}: tier {e.get('tier')!r}")
        if e.get("os") not in OSES:
            bad.append(f"{p}: os {e.get('os')!r}")
        if e.get("needs", "none") not in NEEDS and not PY_NEED.fullmatch(str(e.get("needs"))):
            bad.append(f"{p}: needs {e.get('needs')!r}")
        if not isinstance(e.get("timing_sensitive", False), bool):
            bad.append(f"{p}: timing_sensitive must be a bool")
        if e.get("timing_sensitive") and e.get("tier") != "full":
            bad.append(f"{p}: a timing_sensitive test belongs to the full tier")
        if e.get("area") == "guards" and e.get("tier") != "fast":
            bad.append(f"{p}: guards are fast")
        t = e.get("timeout", 300)
        if not (isinstance(t, int) and not isinstance(t, bool) and t > 0):
            bad.append(f"{p}: timeout must be a positive int")
        if e.get("cwd") and not p.startswith(e["cwd"].rstrip("/") + "/"):
            bad.append(f"{p}: cwd {e['cwd']} must contain the script")
        if p.endswith(".ps1") and e.get("os") != "windows":
            bad.append(f"{p}: a .ps1 test runs on windows")
        if set(e) - KEYS:
            bad.append(f"{p}: unknown keys {sorted(set(e) - KEYS)}")
    for r in man["rules"]:
        bad += [f"rule {r.get('prefix', r.get('suffix'))}: unknown area {a}" for a in r["areas"] if a not in AREAS]
    return bad


text = read(WF)
check("ci.yml is LF only", "\r" not in text)
mini, py = load_both(text)
engine = "yaml+mini" if py else "mini"
exists = lambda p: os.path.exists(os.path.join(BMA, p))  # noqa: E731
eq("the workflow matches the CI design (minimal reader)", workflow_problems(mini, exists), [])
if py:
    eq("the workflow matches the CI design (PyYAML)", workflow_problems(py, exists), [])
    eq("the minimal reader and PyYAML agree on the whole document", norm(mini), norm(py))
else:
    print("NOTE PyYAML is not installed: the minimal reader alone was used")

mtext = read(MANIFEST)
man = tomllib.loads(mtext)
check("ci_manifest.toml is LF only", "\r" not in mtext)
files = test_files(BMA)
check("the kit has test files to cover", len(files) > 50, str(len(files)))
eq("every test file has one manifest entry or a skip, every path exists, every value is valid", manifest_problems(man, files, exists), [])
eq("the manifest has the guards entries", sorted({e["path"] for e in man["tests"] if e["area"] == "guards"} & {"scripts/check_private_paths.py", "contracts/check_contracts.py"}),
   ["contracts/check_contracts.py", "scripts/check_private_paths.py"])


# mutations of the workflow checker, each against the unmutated twin
def mutated(fn):
    d = copy.deepcopy(norm(mini))
    fn(d)
    return workflow_problems(d, exists)


def has(problems, frag):
    return any(frag in p for p in problems)


check("twin: the unmutated workflow has no problems", workflow_problems(mini, exists) == [])
check("mutation: a per-test step is reported", has(mutated(lambda d: d["jobs"]["fast-linux"]["steps"].append({"run": "python analysis/test_labels.py"})), "not a run_ci.py call"))
check("mutation: a third-party action is reported", has(mutated(lambda d: d["jobs"]["guards"]["steps"].append({"uses": "dorny/paths-filter@v3"})), "third-party"))
check("mutation: a full job without the label gate is reported", has(mutated(lambda d: d["jobs"]["full-linux"].update({"if": "${{ github.event_name == 'push' || inputs.tier == 'full' }}"})), "full-linux: if lacks ci:full"))
check("mutation: a fast job that also runs under the label is reported", has(mutated(lambda d: d["jobs"]["fast-linux"].update({"if": "${{ needs.plan.outputs.linux != '0' }}"})), "must exclude"))
check("mutation: a fast-tier call in the full job is reported", has(mutated(lambda d: d["jobs"]["full-windows"]["steps"].__setitem__(-1, {"run": "python scripts/run_ci.py --tier fast --os windows --areas all"})), "--tier full"))
check("mutation: no retry in the full job is reported", has(mutated(lambda d: d["jobs"]["full-linux"]["steps"].__setitem__(-1, {"run": "python scripts/run_ci.py --tier full --os linux --areas all"})), "--retry-timing 1 belongs"))
check("mutation: a retry in a fast job is reported", has(mutated(lambda d: d["jobs"]["fast-linux"]["steps"].__setitem__(-1, {"run": "python scripts/run_ci.py --tier fast --os linux --areas auto --retry-timing 1"})), "--retry-timing 1 belongs"))
check("mutation: the sysctl in a fast job is reported", has(mutated(lambda d: d["jobs"]["fast-linux"]["steps"].append({"run": SYSCTL})), "sysctl belongs"))
check("mutation: a pull_request trigger without the labeled type is reported", has(mutated(lambda d: d["on"]["pull_request"].update({"types": ["opened", "synchronize"]})), "types"))
check("mutation: cancel-in-progress for every event is reported", has(mutated(lambda d: d["concurrency"].update({"cancel-in-progress": True})), "concurrency"))
check("mutation: a missing timeout is reported", has(mutated(lambda d: d["jobs"]["guards"].pop("timeout-minutes")), "timeout-minutes"))
check("mutation: a missing requirements file is reported", has(workflow_problems(mini, lambda p: p != "scripts/requirements-ci.txt"), "requirements-ci.txt missing"))
check("mutation: a job without the plan dependency is reported", has(mutated(lambda d: d["jobs"]["guards"].pop("needs")), "must need plan"))
check("mutation: a removed job is reported", has(mutated(lambda d: d["jobs"].pop("full-windows")), "jobs are"))

# mutations of the manifest checker: independent of the files on disk (the file list and existence are synthetic)
fl = sorted(e["path"] for e in man["tests"])
any_ = lambda p: True  # noqa: E731
M = lambda: copy.deepcopy(man)  # noqa: E731
NEW = "analysis/test_new_zz.py"
eq("mutation: a new test file without an entry is reported", manifest_problems(man, fl + [NEW], any_), [f"{NEW}: test file has no manifest entry or skip"])
m2 = M()
m2["tests"].append({"path": NEW, "area": "analysis", "tier": "fast", "os": "both"})
eq("twin: the same file with an entry is clean", manifest_problems(m2, fl + [NEW], any_), [])
eq("mutation: a manifest path that does not exist is reported", manifest_problems(m2, fl, lambda p: p != NEW), [f"{NEW}: path does not exist"])
m3 = M()
m3["tests"].append(dict(m3["tests"][0]))
eq("mutation: a duplicate entry is reported", manifest_problems(m3, fl, any_), [f"{m3['tests'][0]['path']}: 2 manifest entries"])
m4 = M()
m4["skips"] = [{"path": "analysis/test_zz_skip.py", "reason": " "}]
eq("mutation: a skip without a reason is reported", manifest_problems(m4, fl, any_), ["analysis/test_zz_skip.py: skip without a reason"])
m5 = M()
m5["skips"] = [{"path": "analysis/test_zz_skip.py", "reason": "needs a thing"}]
eq("twin: a skip with a reason covers its file", manifest_problems(m5, fl + ["analysis/test_zz_skip.py"], any_), [])
for key, val, frag in (("tier", "slow", "tier 'slow'"), ("os", "mac", "os 'mac'"), ("needs", "gpu", "needs 'gpu'"), ("area", "nowhere", "area 'nowhere'"), ("timeout", 0, "timeout must be")):
    m6 = M()
    m6["tests"][0][key] = val
    r6 = manifest_problems(m6, fl, any_)
    check(f"mutation: invalid {key} is reported", has(r6, frag), str(r6))
m7 = M()
m7["tests"][0].update({"timing_sensitive": True, "tier": "fast"})
check("mutation: a timing_sensitive fast test is reported", has(manifest_problems(m7, fl, any_), "belongs to the full tier"))
m8 = M()
m8["tests"][0]["area"] = "ui" if m8["tests"][0]["area"] != "ui" else "pc"
check("mutation: an area that is not the directory is reported", has(manifest_problems(m8, fl, any_), "is not its directory"))

UN = "ui-next/test/test_zz_new.py"
eq("mutation: an unregistered test under ui-next/test is reported", manifest_problems(man, fl + [UN], any_), [f"{UN}: test file has no manifest entry or skip"])
m9 = M()
m9["tests"].append({"path": UN, "area": "ui", "tier": "fast", "os": "both", "cwd": "ui-next/test"})
eq("twin: registered under area ui with its cwd, it is clean", manifest_problems(m9, fl + [UN], any_), [])
m9["tests"][-1]["area"] = "ui-next"
check("mutation: ui-next is not an area (the test area is ui)", has(manifest_problems(m9, fl + [UN], any_), "area 'ui-next'"))
m9["tests"][-1].update({"area": "ui", "cwd": "ui"})
check("mutation: a cwd that does not contain the script is reported", has(manifest_problems(m9, fl + [UN], any_), "must contain the script"))
check("the real kit's ui-next/test files are all registered", all(f in {e["path"] for e in man["tests"]} for f in fl if f.startswith("ui-next/")) and any(f.startswith("ui-next/test/") for f in fl))

# the walker finds test files only
tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp, "analysis"))
os.makedirs(os.path.join(tmp, "node_modules"))
os.makedirs(os.path.join(tmp, "ui-next", "test"))
for rel in ("analysis/test_stray.py", "analysis/helper.py", "node_modules/test_dep.js", "ui-next/test/test_x.py", "ui-next/test/browsertest.py"):
    open(os.path.join(tmp, rel), "w").close()
eq("the walker finds test files, not helpers or node_modules", test_files(tmp), ["analysis/test_stray.py", "ui-next/test/test_x.py"])

print(f"\n{n} checks, {len(fails)} failure(s) [{engine}]")
sys.exit(1 if fails else 0)
