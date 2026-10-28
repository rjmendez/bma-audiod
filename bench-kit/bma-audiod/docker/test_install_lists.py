#!/usr/bin/env python3
"""Install and image file lists against analysis/plugins.manifest.json and against what analysis/host.py really needs. A module the
analyzer needs but the list forgot fails at run time on the deployed machine, so the lists are checked here.

Since the plugin manifest, pc/install_task.ps1 reads the manifest (a PowerShell block between `plugin-manifest files` markers). The
Dockerfile keeps a literal COPY of the analysis files (no build-time step that cannot be run here); this test is its drift gate.
So this test checks three things:
  1. the manifest is complete: the import closure of host.py PLUS every module the manifest names (plugin modules are imported by name, at run
     time, so host.py alone no longer imports them: a closure from host.py alone would pass vacuously) is exactly the set of Python modules
     the manifest deploys to the PC and, minus the optional Perch plugin, to the image.
  2. the installers really install what the manifest says: the effective (source, destination) pairs of install_task.ps1 (the manifest block
     run in PowerShell with Copy-Item replaced by a recorder) and of the Dockerfile (its COPY lines) contain
     exactly the manifest's analysis files, and the literal non-analysis files the manifest lists in extra_files.
  3. the Dockerfile and the image still do not offer Perch: perch.py is not copied (no onnxruntime, no --perch-* flags in entrypoint.sh).
Each assertion has a positive twin: the same checker run on a list with one module removed names exactly that module.
Run:  python test_install_lists.py   (exit 0 = pass)"""

import ast, copy, importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
ANALYSIS = os.path.join(BMA, "analysis")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MF = _load("bma_manifest", os.path.join(ANALYSIS, "manifest.py"))
G = _load("bma_manifest_harness", os.path.join(ANALYSIS, "manifest_harness.py"))  # its ps1 / Dockerfile readers are the "effective pairs" extractors
M = MF.load()

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def local_imports(path):
    """-> {module names imported anywhere in the file (also inside functions) that exist as analysis/<name>.py}."""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            names.add(n.module.split(".")[0])
    return {m for m in names if os.path.isfile(os.path.join(ANALYSIS, m + ".py"))}


def closure(roots):
    todo, seen = list(roots), set()
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        todo += sorted(local_imports(os.path.join(ANALYSIS, m + ".py")) - seen)
    return seen


def manifest_roots(m):
    """host plus every module the manifest names: the plugin's own module and the module of its make function."""
    roots = {"host"}
    for e in m["plugins"]:
        roots.add(e["module"])
        roots.add(e["make"].partition(":")[0])
    return roots


def py_stems(pairs):
    return {d[:-3] for _s, d in pairs if d.endswith(".py")}


def pc_stems(m):
    return py_stems(MF.install_pairs(m, "pc"))


def image_stems(m):
    return py_stems(MF.install_pairs(m, "image"))


OPTIONAL = {e["module"] for e in M["plugins"] if "image" not in e["deploy"]}  # deployed only where the plugin is offered (perch)
NEEDED = closure(manifest_roots(M))
HOST_ONLY = closure(["host"])

check(
    "the manifest's files exist: every support file, plugin file and extra file is on disk",
    all(os.path.isfile(os.path.join(ANALYSIS, f)) for f in M["support_files"] + M["pc_only_files"] + [f for e in M["plugins"] for f in e["files"]])
    and all(os.path.isfile(os.path.join(BMA, x["src"])) for x in M["extra_files"]),
    "",
)
check(
    "vacuity guard: the import closure of host.py ALONE is smaller than the one that starts from the manifest's modules (plugins are imported by name, not by an import line)",
    HOST_ONLY < NEEDED and {"coherence", "bursts", "indices", "perch", "mixscore", "core_make"} <= NEEDED - HOST_ONLY,
    sorted(NEEDED - HOST_ONLY),
)
check(
    "the closure is the 20 modules the analyzer needs next to it (host, plugins, core_make, wiring, manifest, hist, taxa, runmeta, rollup, daily, analyzer_history, coherence,"
    " indices, bursts, mixscore, clips, clip_features, clip_render, pcen, pad_lane) plus perch, which is imported lazily",
    NEEDED - OPTIONAL
    == {
        "host",
        "plugins",
        "core_make",
        "wiring",
        "manifest",
        "hist",
        "taxa",
        "runmeta",
        "rollup",
        "daily",
        "analyzer_history",
        "coherence",
        "indices",
        "bursts",
        "mixscore",
        "pad_lane",
        "clips",
        "clip_features",
        "clip_render",
        "pcen",
    }
    and OPTIONAL == {"perch"}
    and "perch" in NEEDED,
    sorted(NEEDED),
)
check(
    "the manifest deploys every module of the closure to the PC (perch included) and no other .py",
    pc_stems(M) == NEEDED,
    (sorted(NEEDED - pc_stems(M)), sorted(pc_stems(M) - NEEDED)),
)
check(
    "the manifest deploys every module of the closure except perch to the image and no other .py",
    image_stems(M) == NEEDED - OPTIONAL,
    (sorted(NEEDED - OPTIONAL - image_stems(M)), sorted(image_stems(M) - NEEDED)),
)

# positive twins on the manifest: the same checks on a manifest with one module removed name exactly that module
m_no_coh = copy.deepcopy(M)
for e in m_no_coh["plugins"]:
    if e["name"] == "coherence":
        e["files"] = []
check(
    "positive twin: with coherence.py removed from the manifest's file lists the closure check reports exactly coherence (PC and image)",
    NEEDED - pc_stems(m_no_coh) == {"coherence"} and NEEDED - OPTIONAL - image_stems(m_no_coh) == {"coherence"},
    sorted(NEEDED - pc_stems(m_no_coh)),
)
m_no_perch = copy.deepcopy(M)
for e in m_no_perch["plugins"]:
    if e["name"] == "perch":
        e["deploy"] = []
check(
    "positive twin: with perch removed from deploy.pc the PC check reports exactly perch, and the image check (which never had it) still passes",
    NEEDED - pc_stems(m_no_perch) == {"perch"} and image_stems(m_no_perch) == NEEDED - OPTIONAL,
    sorted(NEEDED - pc_stems(m_no_perch)),
)
m_ghost = copy.deepcopy(M)
m_ghost["plugins"][0]["files"] = ["nosuchplugin.py"]
check(
    "positive twin: a manifest that lists a module the closure never needs (nosuchplugin.py) is reported as a surplus, exactly that module",
    pc_stems(m_ghost) - NEEDED == {"nosuchplugin"},
    sorted(pc_stems(m_ghost) - NEEDED),
)

# 2. the installers: effective (source, destination) pairs
want_pc = {(f"analysis/{s}", d) for s, d in MF.install_pairs(M, "pc")}
want_img = {(f"analysis/{s}", f"/app/{d}") for s, d in MF.install_pairs(M, "image")}
ps1 = {tuple(p) for p in G.ps1_pairs()}
dock = {tuple(p) for p in G.docker_pairs()}
ps1_an = {p for p in ps1 if p[0].startswith("analysis/")}
dock_an = {p for p in dock if p[0].startswith("analysis/")}
check(
    "install_task.ps1 installs exactly the analysis files the manifest deploys to the PC (the manifest block run in PowerShell with a recording Copy-Item)",
    ps1_an == want_pc,
    (sorted(want_pc - ps1_an), sorted(ps1_an - want_pc)),
)
check(
    "analyzer.Dockerfile COPYs exactly the analysis files the manifest deploys to the image",
    dock_an == want_img,
    (sorted(want_img - dock_an), sorted(dock_an - want_img)),
)
check(
    "install_task.ps1 and the Dockerfile also copy the literal non-analysis files the manifest lists in extra_files",
    all((s, d) in ps1 for s, d in MF.extra_pairs(M, "pc")) and all((s, f"/app/{d}") in dock for s, d in MF.extra_pairs(M, "image")),
    (sorted(p for p in MF.extra_pairs(M, "pc") if p not in ps1), sorted(p for p in MF.extra_pairs(M, "image") if (p[0], "/app/" + p[1]) not in dock)),
)
want_pc_no_coh = {(f"analysis/{s}", d) for s, d in MF.install_pairs(m_no_coh, "pc")}
check(
    "installer positive twin: against the manifest without coherence the real installer is reported as surplus by exactly coherence.py",
    ps1_an - want_pc_no_coh == {("analysis/coherence.py", "coherence.py")} and want_pc_no_coh - ps1_an == set(),
    sorted(ps1_an - want_pc_no_coh),
)

# 3. perch is a PC-only plugin
dock_text = open(os.path.join(HERE, "analyzer.Dockerfile"), "r", encoding="utf-8").read()
check(
    "the image does not offer Perch: perch.py is installed on the PC and in no Dockerfile destination, no onnxruntime in requirements-analyzer.txt, no perch in entrypoint.sh",
    ("analysis/perch.py", "perch.py") in ps1
    and not any("perch" in p[0] or "perch" in p[1] for p in dock)
    and "onnxruntime" not in open(os.path.join(HERE, "requirements-analyzer.txt")).read()
    and "perch" not in open(os.path.join(HERE, "entrypoint.sh")).read(),
    sorted(dock),
)
check(
    "the Dockerfile has no step that needs a build tool this repository cannot run: no RUN --mount, no manifest.py invocation",
    "--mount" not in dock_text and "manifest.py install" not in dock_text,
    "",
)
# drift gate, positive twin: the same extraction on the Dockerfile text with one COPY entry removed names exactly that file
_dock_missing = dock_text.replace(" analysis/pcen.py", "", 1)
check(
    "Dockerfile drift positive twin: the Dockerfile text without analysis/pcen.py in its COPY is reported as missing exactly pcen.py",
    _dock_missing != dock_text
    and {p for p in G.docker_pairs_text(_dock_missing) if p[0].startswith("analysis/")} ^ dock_an == {("analysis/pcen.py", "/app/pcen.py")},
    "",
)

# the live installer normally runs under Windows PowerShell 5.1 (powershell.exe), whose ConvertFrom-Json and array flattening differ from pwsh 7:
# the same block must give the same pairs there (skipped, and said so, where powershell.exe 5.1 does not exist)
import shutil

_w51 = shutil.which("powershell")
if _w51 and os.name == "nt":
    _p51, _p7 = {tuple(p) for p in G.ps1_pairs(_w51)}, {tuple(p) for p in G.ps1_pairs(shutil.which("pwsh") or _w51)}
    check(
        "install_task.ps1 manifest block under Windows PowerShell 5.1 copies the same (source, destination) pairs as under pwsh, and they include the manifest's PC files (positive twin: a made-up file is absent)",
        _p51 == _p7 and len(_p51) >= 35 and ("analysis/host.py", "host.py") in _p51 and ("analysis/nosuchplugin.py", "nosuchplugin.py") not in _p51,
        str(sorted(_p51 ^ _p7)),
    )
else:
    print("SKIP install_task.ps1 under Windows PowerShell 5.1: powershell.exe not available here")

print(f"\n{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
