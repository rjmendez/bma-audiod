#!/usr/bin/env python3
"""Tests of the plugin manifest (analysis/plugins.manifest.json, analysis/manifest.py, analysis/wiring.py, analysis/core_make.py).
Script style (check(name, ok, detail)); stdlib + numpy; exit 0 = all passed. Every negative has a positive twin on the same fixture.

  1 the real manifest is valid and its text is clean (no BOM, CR, tab)           5 derivations: help text, docstring, flags, gates, meta counters, installs
  2 validation errors name the entry (duplicate, missing file, heavy, hooks, ...)  6 contract tables, README block
  3 PowerShell reads the same manifest (pwsh and Windows PowerShell 5.1)          7 wiring.py: the declared hooks, with recording stubs
  4 load_modules / gates / build with fail-soft messages                          8 the HOWTO proven: a throwaway demo plugin touches only the allowed files"""

import copy, hashlib, io, json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import manifest as M  # noqa: E402
import wiring as W  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    if not ok:
        fails.append(name)


def raises(fn, exc, text):
    """-> True when fn() raises exc whose message contains `text` (an unrelated error on the way in does not count)."""
    try:
        fn()
    except exc as e:
        return text in str(e)
    return False


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


RAW = read(M.MANIFEST)
REAL = M.loads(RAW)


def mut(fn):
    m = copy.deepcopy(REAL)
    fn(m)
    return m


def ent(m, name):
    return M.by_name(m)[name]


# ============================================================================================ 1 the real manifest
try:
    M.validate(REAL)
    _valid, _why = True, ""
except M.ManifestError as _e:
    _valid, _why = False, str(_e)
check("1 the real manifest validates against the files on disk (structure, modules, make functions, heavy flags, hooks both ways)", _valid, _why)
check(
    "1 registration order is the order status.json and the process loop had before the manifest: dsp spatial tags clap tags_pad clap_pad birds coherence perch mixscore bursts indices (the pad lane sits where pad-lane registered it: after clap)",
    [e["name"] for e in REAL["plugins"]]
    == ["dsp", "spatial", "tags", "clap", "tags_pad", "clap_pad", "birds", "coherence", "perch", "mixscore", "bursts", "indices"],
    str([e["name"] for e in REAL["plugins"]]),
)
check(
    "1 the file is clean: UTF-8 without BOM, LF only, no tab (check_text accepts it)",
    M.check_text(RAW) is None and not RAW.startswith(b"\xef\xbb\xbf") and b"\r" not in RAW and b"\t" not in RAW,
)
for label, bad in (
    ("a BOM", b"\xef\xbb\xbf" + RAW),
    ("CRLF line ends", RAW.replace(b"\n", b"\r\n")),
    ("a tab", RAW.replace(b'  "manifest_version"', b'\t"manifest_version"')),
):
    check(
        f"1 {label} is refused with a message naming it (twin: the clean bytes load)",
        raises(lambda b=bad: M.loads(b), M.ManifestError, {"a BOM": "BOM", "CRLF line ends": "CR", "a tab": "tab"}[label]) and M.loads(RAW) == REAL,
        label,
    )
check("1 invalid JSON is a ManifestError, not a traceback", raises(lambda: M.loads(b"{not json"), M.ManifestError, "not valid JSON"))


# ============================================================================================ 2 validation errors name the entry
def dup(m):
    m["plugins"].append(copy.deepcopy(ent(m, "bursts")))


check(
    "2 a duplicate plugin name is refused naming it (twin: the real manifest passes)",
    raises(lambda: M.validate(mut(dup)), M.ManifestError, "duplicate plugin name 'bursts'") and M.validate(REAL) is None,
)
check(
    "2 an unknown module is refused naming the entry and the file (twin: the real one passes)",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "indices").update(module="nosuchmodule"))),
        M.ManifestError,
        "plugin 'indices': module file nosuchmodule.py not found",
    ),
)
check(
    "2 a listed file that is not on disk is refused naming the entry",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "bursts").update(files=["bursts.py", "bursts_gone.py"]))),
        M.ManifestError,
        "plugin 'bursts': file bursts_gone.py not found",
    ),
)
check(
    "2 a make function that does not exist is refused naming it",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "bursts").update(make="bursts:no_such_make"))),
        M.ManifestError,
        "plugin 'bursts': make 'bursts:no_such_make' does not name a function",
    ),
)
check(
    "2 heavy contradicting the class attribute is refused (manifest heavy=False, class Indices.heavy is True)",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "indices").update(heavy=False))),
        M.ManifestError,
        "plugin 'indices': manifest says heavy=False but class Indices.heavy is True",
    ),
)
check(
    "2 heavy twin: flipping a LIGHT plugin the other way (bursts heavy=True) is refused too",
    raises(lambda: M.validate(mut(lambda m: ent(m, "bursts").update(heavy=True))), M.ManifestError, "class Bursts.heavy is False"),
)

tmp = tempfile.mkdtemp(prefix="bma_manifest_test_")
an_copy = os.path.join(tmp, "analysis_heavy")
shutil.copytree(HERE, an_copy, ignore=shutil.ignore_patterns("__pycache__", "test_*"))
src_ind = read(os.path.join(an_copy, "indices.py"), "r")
assert "    heavy = True\n" in src_ind
with open(os.path.join(an_copy, "indices.py"), "w", encoding="utf-8", newline="\n") as f:
    f.write(src_ind.replace("    heavy = True\n", "    heavy = False\n", 1))
check(
    "2 mutation: indices.py edited to heavy = False in a temp copy -> the unchanged manifest is refused (the manifest says heavy true); the pristine tree passes",
    raises(lambda: M.validate(REAL, an_copy), M.ManifestError, "plugin 'indices': manifest says heavy=True but class Indices.heavy is False")
    and M.validate(REAL, HERE) is None,
)

check(
    "2 hook declared but the class has no such method: coherence declared consumes_speech -> exact message",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "coherence")["hooks"].update(consumes_speech=True))),
        M.ManifestError,
        "plugin 'coherence': hook consumes_speech is declared but class Coherence has no method note_speech()",
    ),
)
check(
    "2 method present but undeclared: bursts without consumes_speech -> exact message (this replaces the old silent hasattr skip)",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "bursts")["hooks"].update(consumes_speech=False))),
        M.ManifestError,
        "plugin 'bursts': class Bursts has method note_speech() but the manifest does not declare hook consumes_speech",
    ),
)
check(
    "2 every hook of the real manifest is declared exactly where the method exists (cost: tags_pad, clap_pad, mixscore; observes: mixscore; consumes_tag: indices; consumes_speech: bursts)",
    [
        (e["name"], sorted(k for k in ("consumes_speech", "consumes_tag", "observes", "cost") if M.hooks(e).get(k) is True))
        for e in REAL["plugins"]
        if any(M.hooks(e).get(k) for k in ("consumes_speech", "consumes_tag", "observes", "cost"))
    ]
    == [("tags_pad", ["cost"]), ("clap_pad", ["cost"]), ("mixscore", ["cost", "observes"]), ("bursts", ["consumes_speech"]), ("indices", ["consumes_tag"])],
    "",
)

lo_bad = mut(lambda m: m["legacy_order"].update(no_help=m["legacy_order"]["no_help"] + ["nosuchplugin"]))
lo_dup = mut(lambda m: m["legacy_order"].update(outputs=m["legacy_order"]["outputs"] + ["dsp"]))
check(
    "2 legacy_order naming an unknown plugin is refused (twin: `novelty` is allowed in outputs, the real file passes)",
    raises(lambda: M.validate(lo_bad), M.ManifestError, "legacy_order.no_help names unknown plugin(s) ['nosuchplugin']")
    and "novelty" in REAL["legacy_order"]["outputs"]
    and M.validate(REAL) is None,
)
check(
    "2 legacy_order listing a plugin twice is refused", raises(lambda: M.validate(lo_dup), M.ManifestError, "legacy_order.outputs lists ['dsp'] more than once")
)
check(
    "2 an unknown cli_slot is refused (geo/plugins/tail are the three)",
    raises(lambda: M.validate(mut(lambda m: ent(m, "bursts").update(cli_slot="top"))), M.ManifestError, "cli_slot 'top'"),
)
check(
    "2 a flag type outside int/float/str/flag is refused",
    raises(lambda: M.validate(mut(lambda m: ent(m, "bursts")["flags"][0].update(type="list"))), M.ManifestError, "flag --bursts-interval-s has type 'list'"),
)
check(
    "2 a flag declared by two plugins is refused naming both",
    raises(
        lambda: M.validate(mut(lambda m: ent(m, "indices")["flags"][0].update(name="--bursts-interval-s"))),
        M.ManifestError,
        "flag --bursts-interval-s is declared by both 'bursts' and 'indices'",
    ),
)
check(
    "2 an unknown deploy target is refused",
    raises(lambda: M.validate(mut(lambda m: ent(m, "bursts").update(deploy=["pc", "cloud"]))), M.ManifestError, "unknown deploy target(s) ['cloud']"),
)
check(
    "2 a requires naming a plugin that does not exist is refused",
    raises(lambda: M.validate(mut(lambda m: ent(m, "mixscore").update(requires=["tags", "ghost"]))), M.ManifestError, "unknown plugin 'ghost'"),
)


# a flag that collides with a CORE flag is caught by argparse itself when the real host builds its parser: run the host's --help on a temp copy
def host_help(analysis_dir, manifest_obj):
    d = os.path.join(tmp, "help_" + hashlib.md5(json.dumps(manifest_obj, sort_keys=True).encode()).hexdigest()[:8])
    shutil.copytree(HERE, d, ignore=shutil.ignore_patterns("__pycache__", "test_*"))
    with open(os.path.join(d, "plugins.manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(manifest_obj, indent=2) + "\n")
    return subprocess.run([sys.executable, "-I", os.path.join(d, "host.py"), "--help"], capture_output=True, text=True, timeout=120, cwd=d)


collide = mut(lambda m: ent(m, "bursts")["flags"][0].update(name="--listen"))
ok_h, bad_h = host_help(HERE, REAL), host_help(HERE, collide)
check(
    "2 a plugin flag that collides with a core flag (--listen) stops the host at argument parsing with argparse's own message (twin: the real manifest prints the help, exit 0)",
    ok_h.returncode == 0 and "--bursts-interval-s" in ok_h.stdout and bad_h.returncode != 0 and "conflicting option string: --listen" in bad_h.stderr,
    (bad_h.returncode, bad_h.stderr[-200:]),
)

# deploy: perch is a PC-only plugin
check(
    "2 deploy: perch.py is in the PC install list and not in the image list; every other plugin file is in both",
    ("perch.py", "perch.py") in M.install_pairs(REAL, "pc")
    and ("perch.py", "perch.py") not in M.install_pairs(REAL, "image")
    and all((f, f) in M.install_pairs(REAL, t) for e in REAL["plugins"] if e["name"] != "perch" for f in e["files"] for t in M.TARGETS),
    "",
)
no_perch_pc = mut(lambda m: ent(m, "perch").update(deploy=[]))
check(
    "2 deploy twin: perch removed from deploy.pc drops perch.py from the PC list (and nothing else)",
    set(M.install_pairs(REAL, "pc")) - set(M.install_pairs(no_perch_pc, "pc")) == {("perch.py", "perch.py")},
    "",
)

# ============================================================================================ 3 PowerShell reads the same manifest
import shutil as _sh  # noqa: E402

shells = [s for s in (_sh.which("pwsh"), _sh.which("powershell")) if s]
want_lines = [f"{e['name']}:{','.join(e['files'])}:{','.join(e['deploy'])}" for e in REAL["plugins"]] + [
    "support:" + ",".join(REAL["support_files"] + REAL["pc_only_files"])
]
ps_script = (
    "$m = Get-Content -Raw -Encoding UTF8 '" + M.MANIFEST.replace("'", "''") + "' | ConvertFrom-Json\n"
    "foreach ($p in $m.plugins) { $p.name + ':' + (@($p.files) -join ',') + ':' + (@($p.deploy) -join ',') }\n"
    "'support:' + ((@($m.support_files) + @($m.pc_only_files)) -join ',')\n"
)
if shells:
    for sh in shells:
        r = subprocess.run([sh, "-NoProfile", "-NonInteractive", "-Command", ps_script], capture_output=True, text=True, timeout=120)
        got = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        check(
            f"3 {os.path.basename(sh)} ConvertFrom-Json gives the same plugin names, order, file lists, deploy targets and support files as Python (one-element and empty arrays included)",
            r.returncode == 0 and got == want_lines,
            (r.returncode, r.stderr[:200], [g for g in got if g not in want_lines][:2]),
        )
else:
    # no PowerShell here: the same properties are checked by a stdlib re-parse of the raw text (a skip would leave the path untested)
    again = json.loads(RAW.decode("utf-8"))
    check(
        "3 (no PowerShell found) the stdlib re-parse yields the same order, files and deploy as the loader",
        [f"{e['name']}:{','.join(e['files'])}:{','.join(e['deploy'])}" for e in again["plugins"]] == want_lines[:-1],
        "",
    )


# ============================================================================================ 4 import / gate / build behaviour
class A:  # args stand-in
    def __init__(self, **kw):
        self.__dict__.update(kw)


perch, mixs = ent(REAL, "perch"), ent(REAL, "mixscore")
rows = []
for flag_val, no, entry, name in (
    (None, [], perch, "perch"),
    (None, ["perch"], perch, "perch"),
    ("m.onnx", [], perch, "perch"),
    ("m.onnx", ["perch"], perch, "perch"),
    (False, [], mixs, "mixscore"),
    (False, ["mixscore"], mixs, "mixscore"),
    (True, [], mixs, "mixscore"),
    (True, ["mixscore"], mixs, "mixscore"),
):
    a = A(perch_model=flag_val, mixscore=flag_val)
    rows.append((name, flag_val, no, M.gate_closed(entry, a, no)))
check(
    "4 gates reproduce the old precedences exactly: perch off without --perch-model (`--no perch` alone: the standard `disabled by --no` path, reason None here), mixscore off by default EVEN with --no mixscore",
    rows
    == [
        ("perch", None, [], "disabled, no --perch-model"),
        ("perch", None, ["perch"], None),
        ("perch", "m.onnx", [], None),
        ("perch", "m.onnx", ["perch"], None),
        ("mixscore", False, [], "off by default (--mixscore)"),
        ("mixscore", False, ["mixscore"], "off by default (--mixscore)"),
        ("mixscore", True, [], None),
        ("mixscore", True, ["mixscore"], None),
    ],
    str(rows),
)
check("4 an entry without a gate is never gated", M.gate_closed(ent(REAL, "dsp"), A(), []) is None)
tp, cp = ent(REAL, "tags_pad"), ent(REAL, "clap_pad")
prow = [(M.gate_closed(tp, A(tags_pad_s=v), no), M.gate_silent(tp)) for v, no in ((0.0, []), (0.0, ["tags_pad"]), (10.0, []), (10.0, ["tags_pad"]))]
check(
    "4 the pad lane gate: --tags-pad-s 0 closes it SILENTLY (no status entry, as before the manifest), also with --no tags_pad; with 10 it is open, and `--no tags_pad` then takes the standard `disabled by --no` path",
    prow == [("off by default (--tags-pad-s)", True), ("off by default (--tags-pad-s)", True), (None, True), (None, True)]
    and M.gate_closed(cp, A(clap_pad_s=0.0), []) == "off by default (--clap-pad-s)"
    and M.gate_closed(cp, A(clap_pad_s=7.5), []) is None
    and M.gate_silent(cp)
    and not M.gate_silent(perch)
    and not M.gate_silent(mixs),
    str(prow),
)
check(
    "4 the pad lane is not advertised in `--no` help or in the docstring Outputs (as in the pad-lane branch), but its flags are in --help",
    "tags_pad" not in M.no_help_text(REAL)
    and "clap_pad" not in M.no_help_text(REAL)
    and "tags_pad" not in M.fill_docstring("{outputs}", REAL)
    and M.no_help_names(REAL) == M.no_help_names(mut(lambda m: ent(m, "tags_pad").update(in_no_help=False)))
    and "tags_pad" in M.no_help_text(mut(lambda m: ent(m, "tags_pad").pop("in_no_help"))),
    "",
)


class Q:  # a running production plugin (stand-in)
    def __init__(self, name):
        self.name = name


import pad_lane as PL0  # noqa: E402

made = []
_real_pt, _real_pc = PL0.PadTagger, PL0.PadClap
PL0.PadTagger = lambda data_dir, dev, pad_s, share=None: made.append(("tags_pad", data_dir, dev, pad_s, share)) or "T"
PL0.PadClap = lambda dev, pad_s, share=None: made.append(("clap_pad", dev, pad_s, share)) or "C"
try:
    qt, qc = Q("tags"), Q("clap")
    got_t = M.build.__globals__["resolve"](tp["make"])(M.Ctx(A(panns_dir="pd", tags_pad_s=10.0), None, "cpu", "r", "c", "o", {"tags", "clap"}, {}, [qc, qt]))
    got_c = M.build.__globals__["resolve"](cp["make"])(M.Ctx(A(clap_pad_s=10.0), None, "cuda", "r", "c", "o", {"tags", "clap"}, {}, [qc, qt]))
    got_n = M.build.__globals__["resolve"](tp["make"])(M.Ctx(A(panns_dir="pd", tags_pad_s=6.0), None, "cpu", "r", "c", "o", set(), {}))
finally:
    PL0.PadTagger, PL0.PadClap = _real_pt, _real_pc
check(
    "4 the pad factories pass the arguments of the old host lambdas and share the running production plugin's model (twin: none running -> share None)",
    (got_t, got_c, got_n) == ("T", "C", "T")
    and made == [("tags_pad", "pd", "cpu", 10.0, qt), ("clap_pad", "cuda", 10.0, qc), ("tags_pad", "pd", "cpu", 6.0, None)],
    str(made),
)

# optional/required import handling on a throwaway module
sys.path.insert(0, tmp)
with open(os.path.join(tmp, "fake_good_mod.py"), "w", newline="\n") as f:
    f.write("X = 1\n")
with open(os.path.join(tmp, "fake_bad_mod.py"), "w", newline="\n") as f:
    f.write('raise ImportError("synthetic import failure")\n')


def with_modules(good, bad, mode_bad, **extra):
    m = copy.deepcopy(REAL)
    m["plugins"] = [dict(copy.deepcopy(ent(REAL, "bursts")), name="g", module=good, import_="x") for _ in range(0)]
    e1 = dict(copy.deepcopy(ent(REAL, "bursts")), name="g1", module=good, **{"import": "optional"})
    e2 = dict(copy.deepcopy(ent(REAL, "bursts")), name="b1", module=bad, **{"import": mode_bad}, **extra)
    m["plugins"] = [e1, e2]
    return m


failed = M.load_modules(with_modules("fake_good_mod", "fake_bad_mod", "optional"))
check(
    "4 an optional module that cannot be imported is recorded with the legacy texts: warn `WARN: bursts module not importable: synthetic import failure`, reason `ImportError: synthetic import failure`; the importable one is not (positive twin)",
    list(failed) == ["b1"]
    and failed["b1"]["warn"] == "WARN: bursts module not importable: synthetic import failure"
    and failed["b1"]["reason"] == "ImportError: synthetic import failure",
    str(failed),
)
failed2 = M.load_modules(
    with_modules(
        "fake_good_mod", "fake_bad_mod", "optional", import_warn="WARN: indices module not loaded: {type}: {error}", import_reason="indices.py not importable"
    )
)
check(
    "4 the indices style templates give `WARN: indices module not loaded: ImportError: synthetic import failure` and the literal reason `indices.py not importable`",
    failed2["b1"]["warn"] == "WARN: indices module not loaded: ImportError: synthetic import failure"
    and failed2["b1"]["reason"] == "indices.py not importable",
    str(failed2),
)
check(
    "4 a REQUIRED module that cannot be imported propagates (the host stops, as the old top-level import did); the same call with the good module returns {} (positive twin)",
    raises(lambda: M.load_modules(with_modules("fake_good_mod", "fake_bad_mod", "required")), ImportError, "synthetic import failure")
    and M.load_modules(with_modules("fake_good_mod", "fake_good_mod", "required")) == {},
)
long_err = "x" * 400
with open(os.path.join(tmp, "fake_long_mod.py"), "w", newline="\n") as f:
    f.write(f'raise ImportError("{long_err}")\n')
check(
    "4 the recorded reason is capped at 200 characters, as the old `[:200]`",
    len(M.load_modules(with_modules("fake_good_mod", "fake_long_mod", "optional"))["b1"]["reason"]) == 200,
)


class Rec:  # a plugin stand-in recording which hooks it has
    name = "bursts"
    note_speech = None


bursts_e = ent(REAL, "bursts")
check(
    "4 verify_instance: an instance without a declared hook method fails with a named HookError; one with the method passes (positive twin)",
    raises(lambda: M.verify_instance(bursts_e, Rec()), M.HookError, "manifest declares hook consumes_speech for plugin bursts but Rec has no note_speech()")
    and M.verify_instance(bursts_e, type("Ok", (), {"note_speech": lambda self, a, b, c: None, "flush": lambda self: []})()) is None
    and raises(lambda: M.verify_instance(bursts_e, type("NoFlush", (), {"note_speech": lambda self, a, b, c: None})()), M.HookError, "hook flush"),
)


# meta counters: names, sums, only_when_running
class St:
    def __init__(self, **kw):
        self.kw = kw

    def stats(self):
        return self.kw


running = {"coherence": St(errors=1, nonfinite=2), "bursts": St(errors=4), "indices": St(errors=8, nonfinite=16, untagged=32), "mixscore": St(errors=64)}
rows = [(n, None if p is None else sum(p.stats()[k] for k in keys), o) for n, p, keys, o in M.meta_counters(REAL, running)]
check(
    "4 meta counters: coherence_errors 3 (errors+nonfinite), mixscore_errors 64 (only when running), bursts_errors 4, indices_errors 56 (errors+nonfinite+untagged), in manifest order",
    rows == [("coherence_errors", 3, False), ("mixscore_errors", 64, True), ("bursts_errors", 4, False), ("indices_errors", 56, False)],
    str(rows),
)
running.pop("mixscore")
rows2 = [(n, p is None, o) for n, p, keys, o in M.meta_counters(REAL, running)]
check(
    "4 meta counters twin: without the mixscore instance its row is (absent, only_when_running) so the host skips it, the others stay",
    ("mixscore_errors", True, True) in rows2 and rows2[0] == ("coherence_errors", False, False),
    str(rows2),
)

# ============================================================================================ 5 derivations
GOLD_NO = "disable a plugin: dsp spatial tags clap birds coherence indices bursts mixscore perch (or `clips`: no wildlife clip capture)"
check("5 the --no help text is the old hand-written one, byte for byte", M.no_help_text(REAL) == GOLD_NO, M.no_help_text(REAL))
check(
    "5 outputs list: the old docstring order (novelty between tags and clap, perch last, mixscore absent)",
    M.outputs_names(REAL) == ["dsp", "spatial", "tags", "novelty", "clap", "birds", "coherence", "indices", "bursts", "perch"],
    str(M.outputs_names(REAL)),
)
check(
    "5 docstring: `{outputs}` becomes the old line `dsp.jsonl  spatial.jsonl ... perch.jsonl   + emb_YYYYMMDD.f16/.jsonl` (twin: no placeholder, no change)",
    M.fill_docstring("a {outputs} b", REAL)
    == "a dsp.jsonl  spatial.jsonl  tags.jsonl  novelty.jsonl  clap.jsonl  birds.jsonl  coherence.jsonl  indices.jsonl  bursts.jsonl  perch.jsonl   + emb_YYYYMMDD.f16/.jsonl b"
    and M.fill_docstring("plain", REAL) == "plain",
)
new_plugin = mut(lambda m: m["plugins"].append(dict(copy.deepcopy(ent(m, "bursts")), name="zeta", outputs=["zeta.jsonl"])))
check(
    "5 a plugin the legacy lists do not name is appended after the legacy order (help and outputs), in manifest order",
    M.no_help_names(new_plugin)[-1] == "zeta"
    and M.no_help_names(new_plugin)[:-1] == M.no_help_names(REAL)
    and M.outputs_names(new_plugin)[-1] == "zeta"
    and M.outputs_names(new_plugin)[:-1] == M.outputs_names(REAL),
)
import argparse  # noqa: E402

ap = argparse.ArgumentParser()
for slot in M.CLI_SLOTS:
    M.add_flags(ap, REAL, slot)
dest = {a.option_strings[0]: (a.type, a.default, a.metavar, type(a).__name__) for a in ap._actions if a.option_strings and a.option_strings[0] != "-h"}
check(
    "5 flags: the sixteen plugin flags (the twelve legacy ones, master's --perch-device and --perch-ort-dir, the pad lane's --tags-pad-s and --clap-pad-s) exist with the legacy types, defaults, metavar and action (twin: a flag of the other kind is a store_true)",
    dest["--mixscore"] == (None, False, None, "_StoreTrueAction")
    and dest["--mixscore-every"] == (int, 4, "N", "_StoreAction")
    and dest["--coherence-interval-s"] == (float, 60.0, None, "_StoreAction")
    and dest["--perch-model"] == (str, None, None, "_StoreAction")
    and dest["--perch-device"] == (str, "cpu", None, "_StoreAction")
    and dest["--perch-ort-dir"] == (str, None, None, "_StoreAction")
    and dest["--perch-threads"] == (int, 4, None, "_StoreAction")
    and dest["--perch-min-logit"] == (float, 0.0, None, "_StoreAction")
    and dest["--bursts-interval-s"] == (float, 60.0, None, "_StoreAction")
    and dest["--indices-interval-s"] == (float, 300.0, None, "_StoreAction")
    and dest["--indices-hop-s"] == (float, 30.0, None, "_StoreAction")
    and dest["--indices-without-speech-gate"] == (None, False, None, "_StoreTrueAction")
    and dest["--perch-embeddings"][3] == "_StoreTrueAction"
    and dest["--tags-pad-s"] == (float, 0.0, "S", "_StoreAction")
    and dest["--clap-pad-s"] == (float, 0.0, "S", "_StoreAction")
    and len(dest) == 16,
    str(dest),
)
slot_flags = {
    s: [a.option_strings[0] for a in (lambda p: (M.add_flags(p, REAL, s), p._actions)[1])(argparse.ArgumentParser(add_help=False)) if a.option_strings]
    for s in M.CLI_SLOTS
}
check(
    "5 flags per slot: geo = mixscore flags; plugins = coherence, perch, bursts, indices in manifest order; tail = the pad lane (--tags-pad-s, --clap-pad-s)",
    slot_flags
    == {
        "geo": ["--mixscore", "--mixscore-every"],
        "plugins": [
            "--coherence-interval-s",
            "--perch-model",
            "--perch-labels",
            "--perch-embeddings",
            "--perch-threads",
            "--perch-device",
            "--perch-ort-dir",
            "--perch-min-logit",
            "--bursts-interval-s",
            "--indices-interval-s",
            "--indices-hop-s",
            "--indices-without-speech-gate",
        ],
        "tail": ["--tags-pad-s", "--clap-pad-s"],
    },
    str(slot_flags),
)

# install: real copy and dry run
dst = os.path.join(tmp, "installed")
buf = io.StringIO()
M.install(REAL, "image", dst, out=buf)
want_files = sorted(d for _s, d in M.install_pairs(REAL, "image"))
check(
    "5 install --target image copies exactly the manifest's image files, byte-identical (perch.py and run_analyzer.sh are not among them)",
    sorted(os.listdir(dst)) == want_files
    and all(read(os.path.join(dst, f)) == read(os.path.join(HERE, f)) for f in want_files)
    and "perch.py" not in os.listdir(dst)
    and "run_analyzer.sh" not in os.listdir(dst),
    str(sorted(set(os.listdir(dst)) ^ set(want_files))),
)
dst2 = os.path.join(tmp, "dry")
buf2 = io.StringIO()
M.install(REAL, "pc", dst2, dry_run=True, out=buf2)
lines = buf2.getvalue().splitlines()
check(
    "5 install --dry-run prints `analysis/<f><TAB><dest>/<f>` for every pc file (perch.py and run_analyzer.sh included) and creates nothing",
    not os.path.exists(dst2)
    and sorted(lines) == sorted(f"analysis/{s}\t{dst2.replace(chr(92), '/')}/{d}" for s, d in M.install_pairs(REAL, "pc"))
    and any(l.startswith("analysis/perch.py\t") for l in lines)
    and any(l.startswith("analysis/run_analyzer.sh\t") for l in lines),
    str(lines[:2]),
)
cli = subprocess.run(
    [sys.executable, "-I", os.path.join(HERE, "manifest.py"), "install", "--target", "image", "--dest", os.path.join(tmp, "cli_dry"), "--dry-run"],
    capture_output=True,
    text=True,
)
check(
    "5 the command line `manifest.py install --dry-run` (the install list of one target, one `source<TAB>destination` line per file) prints the same list",
    cli.returncode == 0 and len(cli.stdout.splitlines()) == len(want_files),
    (cli.returncode, cli.stderr[:200]),
)

# ============================================================================================ 6 contracts and README
sch, spc, fxs, liv = M.contract_tables(REAL)
check(
    "6 contract tables: the six plugin interfaces (pad-lane included), their schema/spec/fixture/live rows, exactly as the hand-written check_contracts rows were",
    sch
    == {
        "pad-lane": "pad_lane.schema.json",
        "beam-coherence": "beam_coherence.schema.json",
        "perch": "perch.schema.json",
        "mixscore": "mixscore.schema.json",
        "burst-stats": "burst_stats.schema.json",
        "acoustic-indices": "acoustic_indices.schema.json",
    }
    and spc
    == {
        "pad-lane": "pad-lane.md",
        "beam-coherence": "beam-coherence.md",
        "perch": "perch.md",
        "mixscore": "mixscore.md",
        "burst-stats": "burst-stats.md",
        "acoustic-indices": "acoustic-indices.md",
    }
    and fxs
    == {
        "tags_pad.jsonl": "pad-lane",
        "clap_pad.jsonl": "pad-lane",
        "beam_coherence.jsonl": "beam-coherence",
        "perch.jsonl": "perch",
        "mixscore.jsonl": "mixscore",
        "burst_stats.jsonl": "burst-stats",
        "acoustic_indices.jsonl": "acoustic-indices",
    }
    and liv
    == {
        "analysis/tags_pad.jsonl": ("pad-lane", True),
        "analysis/clap_pad.jsonl": ("pad-lane", True),
        "analysis/coherence.jsonl": ("beam-coherence", True),
        "analysis/perch.jsonl": ("perch", True),
        "analysis/mixscore.jsonl": ("mixscore", True),
        "analysis/bursts.jsonl": ("burst-stats", True),
        "analysis/indices.jsonl": ("acoustic-indices", True),
    },
    str((sch, liv)),
)
check(
    "6 a contract declared twice is refused",
    raises(
        lambda: M.contracts(mut(lambda m: ent(m, "indices").update(contract=copy.deepcopy(ent(m, "bursts")["contract"])))),
        M.ManifestError,
        "contract 'burst-stats' is declared twice",
    ),
)
readme = read(os.path.join(HERE, "README.md"), "r")
check(
    "6 README.md holds the generated plugin table exactly as `manifest.py readme` prints it (regenerate with `manifest.py readme --write`)",
    M.readme_block(REAL).rstrip("\n") in readme,
    "",
)
changed = mut(lambda m: ent(m, "bursts")["readme"].update(window="2 s"))
check(
    "6 README twin: a manifest whose bursts row says `2 s` no longer matches the README (the test would be red until regenerated), and regenerating changes only the block",
    M.readme_block(changed).rstrip("\n") not in readme
    and M.readme_text(readme, REAL) == readme
    and M.readme_text(readme, changed) != readme
    and M.readme_text(readme, changed).replace("| 2 s |", "| 1 s; one record per `--bursts-interval-s` (60) |") == readme,
    "",
)
check("6 README without the markers is refused", raises(lambda: M.readme_text("no markers", REAL), M.ManifestError, "no generated plugin-table block"))


# ============================================================================================ 7 wiring with recording stubs
class Plug:
    def __init__(self, name, win_s=5.0, **kw):
        self.name, self.win_s, self.calls = name, win_s, []
        self.__dict__.update(kw)

    def rec(self, what, *a):
        self.calls.append((what,) + a)


class Bursts(Plug):
    def note_speech(self, a, b, c):
        self.rec("note_speech", a, b, c)


class Indices(Plug):
    def note_tag(self, t, active, win):
        self.rec("note_tag", t, active, win)


class Mixscore(Plug):
    def observe(self, src, recs, t):
        self.rec("observe", src, len(recs), t)
        if getattr(self, "boom", False):
            raise ValueError("boom")
        return [{"k": "mixscore", "t": t}] if recs else []


class Perch(Plug):
    def feed_birds(self, t, recs, info):
        self.rec("feed_birds", t, len(recs), info)


class Guard:
    def __init__(self):
        self.calls = []

    def note(self, a, b, c):
        self.calls.append((a, b, c))


class Clips:
    def __init__(self):
        self.calls = []

    def on_plugin(self, name, recs, x, t):
        self.calls.append((name, len(recs), x, t))


class Out:
    def __init__(self):
        self.w = []

    def write(self, name, rec):
        self.w.append((name, rec))


class Store:
    def __init__(self):
        self.added = []

    def add(self, emb, meta):
        self.added.append((emb, meta))


class Nov:
    def add(self, emb, t):
        return [{"k": "novelty", "t": t}]


def mk(plug, guard=None, clips=None):
    out, store = Out(), Store()
    status = {"plugins": {p.name: {"enabled": True, "records": 0, "errors": 0, "last_error": None} for p in plug}}
    shared = {} if guard is None else {"speech_guard": guard}
    return W.Wiring(REAL, plug, out=out, status=status, clips=clips, store=store, novelty=Nov(), shared=shared), out, store, status


tags = Plug("tags", 5.0)
b, ix, mx, perch_p = Bursts("bursts"), Indices("indices"), Mixscore("mixscore"), Perch("perch")
guard, clips = Guard(), Clips()
w, out, store, status = mk([tags, b, ix, mx, perch_p], guard, clips)
rec_t = {"k": "tags", "t": 100.0, "speech_active": True, "top": [["Speech", 0.9]]}
st = status["plugins"]["tags"]
w.after_process(tags, [rec_t], "EMB", "X", 100.0, st)
check(
    "7 tags feed: the speech verdict goes to the bursts plugin (note_speech(95.0, 100.0, True)), the indices plugin (note_tag(100.0, True, 5.0)) and the speech guard (note(95.0, 100.0, True)), exactly once each",
    b.calls == [("note_speech", 95.0, 100.0, True)] and ix.calls == [("note_tag", 100.0, True, 5.0)] and guard.calls == [(95.0, 100.0, True)],
    str((b.calls, ix.calls, guard.calls)),
)
check(
    "7 tags feed: speech_fraction 0.1 after one speech window, the embedding stored with its top-1 label, the novelty record written, mixscore observed tags (one observe call) and the clip service got the window",
    st["speech_fraction"] == 0.1
    and store.added == [("EMB", {"t": 100.0, "top1_json": '"Speech"'})]
    and ("novelty", {"k": "novelty", "t": 100.0}) in out.w
    and mx.calls == [("observe", "tags", 1, 100.0)]
    and clips.calls == [("tags", 1, "X", 100.0)],
    str((st, store.added, out.w, mx.calls, clips.calls)),
)
check(
    "7 undeclared plugins are never called: the perch stand-in (no feed from tags) and the tags plugin itself saw nothing",
    perch_p.calls == [] and tags.calls == [],
    str((perch_p.calls, tags.calls)),
)


# positive twin on the same fixture: a plugin that is NOT declared gets nothing even though it has the method
class Sneaky(Plug):
    def note_speech(self, a, b_, c):
        self.rec("note_speech", a, b_, c)


sneak = Sneaky("coherence")  # coherence's manifest entry does not declare consumes_speech
w2, _o2, _s2, status2 = mk([tags, sneak, b])
w2.after_process(tags, [rec_t], None, "X", 100.0, status2["plugins"]["tags"])
check(
    "7 declaration, not hasattr, decides: a coherence stand-in that HAS note_speech but did not declare it is not called, the declared bursts is (twin on the same window)",
    sneak.calls == [] and b.calls[-1] == ("note_speech", 95.0, 100.0, True),
    str((sneak.calls, b.calls)),
)
b.calls.clear()
w.after_process(tags, [], None, "X", 105.0, st)
check(
    "7 an empty tagger record list feeds nobody (no note_speech, no note_tag, no guard note) but the clip service still gets the window (every window, also with zero records)",
    b.calls == [] and ix.calls == [("note_tag", 100.0, True, 5.0)] and guard.calls == [(95.0, 100.0, True)] and clips.calls[-1] == ("tags", 0, "X", 105.0),
    str((b.calls, ix.calls, guard.calls, clips.calls[-1])),
)
# birds -> perch and clips, window_info to daily
birds = Plug("birds", 3.0, window_info={"beams_classified": 3})
w3, out3, _s3, status3 = mk([birds, perch_p], None, clips)
w3.after_process(birds, [{"k": "birds"}, {"k": "birds"}], None, "Y", 200.0, status3["plugins"]["birds"])
check(
    "7 birds feed: the perch plugin gets feed_birds(200.0, 2 records, window_info) and the clip service gets the birds window",
    perch_p.calls == [("feed_birds", 200.0, 2, {"beams_classified": 3})] and clips.calls[-1] == ("birds", 2, "Y", 200.0),
    str((perch_p.calls, clips.calls[-1])),
)
dsp_stub = Plug("dsp", window_info={"x": 1})
w4, _o4, _s4, status4 = mk([birds, dsp_stub], None, clips)
perch_p.calls.clear()
w4.after_process(birds, [{"k": "birds"}], None, "Y", 201.0, status4["plugins"]["birds"])
check(
    "7 birds feed twin: with no perch plugin running nothing is fed (and nothing raises); window_info goes to the daily summary for birds only",
    perch_p.calls == [] and w4.window_info(birds) == {"beams_classified": 3} and w4.window_info(dsp_stub) is None,
    "",
)
# fan-out: observers get everyone else's records; a raising observer is counted and does not stop the host; the source is never its own observer
mx.calls.clear()
mx.boom = True
w5, out5, _s5, status5 = mk([tags, mx])
w5.after_process(tags, [rec_t], None, "X", 300.0, status5["plugins"]["tags"])
check(
    "7 fan-out: an observer that raises is counted (errors 1, last_error `ValueError: boom`) and the host goes on (the tags feed after it still ran: speech_fraction set)",
    status5["plugins"]["mixscore"]["errors"] == 1
    and status5["plugins"]["mixscore"]["last_error"] == "ValueError: boom"
    and status5["plugins"]["tags"]["speech_fraction"] == 0.1,
    str(status5),
)
mx.boom = False
w6, out6, _s6, status6 = mk([tags, mx])
w6.after_process(tags, [rec_t], None, "X", 301.0, status6["plugins"]["tags"])
w6.after_process(mx, [{"k": "mixscore"}], None, "X", 301.0, status6["plugins"]["mixscore"])
check(
    "7 fan-out twin: the observer's returned records are written under its own name and counted (records 1); it does not observe itself",
    out6.w == [("mixscore", {"k": "mixscore", "t": 301.0})]
    and status6["plugins"]["mixscore"]["records"] == 1
    and mx.calls[-1] == ("observe", "tags", 1, 301.0),
    str((out6.w, status6["plugins"]["mixscore"], mx.calls)),
)


# stats_in_status for perch, dispatch lists
class PerchStats(Perch):
    def stats(self):
        return {"gated": 7}


ps = PerchStats("perch")
w7, _o7, _s7, status7 = mk([ps])
w7.after_process(ps, [], None, "X", 1.0, status7["plugins"]["perch"])
check("7 stats_in_status: the perch plugin's stats() are merged into its status entry (gated 7)", status7["plugins"]["perch"]["gated"] == 7, str(status7))
fl = W.consumers(REAL, [Plug("coherence", flush=1), Plug("dsp"), Plug("bursts"), Plug("unknown_stub")], "flush")
check(
    "7 dispatch lists come from the manifest: flush = the declared plugins in start order (coherence, bursts), a plugin with an unknown name and dsp are not in it",
    [p.name for p in fl] == ["coherence", "bursts"],
    str([p.name for p in fl]),
)
check(
    "7 feed_speech module function (host.feed_speech uses it): ignores an empty record list and passes (t - win_s, t, active)",
    (lambda p: (W.feed_speech([p], tags, [], 10.0), W.feed_speech([p], tags, [{"speech_active": False}], 10.0), p.calls)[2])(Bursts("bursts"))
    == [("note_speech", 5.0, 10.0, False)],
    "",
)


# ============================================================================================ 8 the HOWTO, proven with a throwaway demo plugin
def tree_hashes(root):
    res = {}
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            res[os.path.relpath(p, root).replace(os.sep, "/")] = hashlib.sha256(read(p)).hexdigest()
    return res


DEMO_PY = '''"""Throwaway demo plugin of test_manifest.py: one record per window with the RMS of the 8-beam window."""
import numpy as np


class Demo:
    name = "demo"
    win_s, hop_s = 1.0, 1.0
    heavy = False

    def __init__(self, scale):
        self.scale, self.errors = scale, 0

    def process(self, x, t_wall):
        return [{"k": "demo", "v": 1, "t": t_wall, "rms": round(float(np.sqrt((np.asarray(x, dtype=np.float64) ** 2).mean())) * self.scale, 9)}], None

    def stats(self):
        return {"errors": self.errors}

    def flush(self):
        return []


def make(ctx):
    return Demo(ctx.args.demo_scale)
'''
DEMO_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:bma:contract:demo-stats",
    "title": "demo records (analysis/demo.jsonl)",
    "description": "Throwaway contract of the HOWTO test.",
    "x-contract-version": "1.0.0",
    "x-discriminator": {"field": "k", "map": {"demo": "demo"}},
    "x-files": {"demo.jsonl": ["demo"]},
    "oneOf": [{"$ref": "#/$defs/demo"}],
    "$defs": {
        "demo": {
            "type": "object",
            "required": ["k", "v", "t", "rms"],
            "additionalProperties": False,
            "properties": {"k": {"const": "demo"}, "v": {"const": 1}, "t": {"type": "number"}, "rms": {"type": "number", "minimum": 0}},
        }
    },
}
DEMO_SPEC = "contract: demo-stats\ncontract_version: 1.0.0\nstatus: stable\nproducer: analysis/demo.py\n\n# demo-stats\n\nThrowaway.\n"
DEMO_FIXTURE = "".join(
    json.dumps(r, separators=(",", ":")) + "\n" for r in ({"k": "demo", "v": 1, "t": 1.0, "rms": 0.25}, {"k": "demo", "v": 1, "t": 2.0, "rms": 0.5})
)
DEMO_ENTRY = {
    "name": "demo",
    "module": "demo",
    "files": ["demo.py"],
    "import": "optional",
    "make": "demo:make",
    "heavy": False,
    "deploy": ["pc", "image"],
    "outputs": ["demo.jsonl"],
    "flags": [{"name": "--demo-scale", "type": "float", "default": 1.0, "help": "demo plugin: multiply the reported RMS by this (default 1)"}],
    "cli_slot": "tail",
    "hooks": {"flush": True},
    "meta_counters": [{"name": "demo_errors", "sum": ["errors"], "only_when_running": False}],
    "contract": {
        "name": "demo-stats",
        "version": "1.0.0",
        "schema": "demo.schema.json",
        "spec": "demo-stats.md",
        "fixtures": ["demo.jsonl"],
        "live": [{"file": "analysis/demo.jsonl", "optional": True}],
        "tests": [],
    },
    "readme": {"label": "`demo`", "window": "1 s", "text": "throwaway demo plugin of the HOWTO test", "deps": "numpy"},
}

work = os.path.join(tmp, "demo_tree")
os.makedirs(work)
for sub in ("analysis", "contracts", "docker"):
    shutil.copytree(os.path.join(BMA, sub), os.path.join(work, sub), ignore=shutil.ignore_patterns("__pycache__", "tools"))
before = tree_hashes(work)


# the HOWTO steps: new files ...
def put(rel, text):
    p = os.path.join(work, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


put("analysis/demo.py", DEMO_PY)
put("contracts/schemas/demo.schema.json", json.dumps(DEMO_SCHEMA, indent=1) + "\n")
put("contracts/specs/demo-stats.md", DEMO_SPEC)
put("contracts/fixtures/demo.jsonl", DEMO_FIXTURE)
put(
    "contracts/changelog.d/demo-stats.md",
    "## demo-stats 1.0.0 - 2026-10-27 (new; throwaway demo plugin)\n\n- demo-stats 1.0.0 - new interface: demo.jsonl, one record per window.\n",
)
# ... one manifest entry, and the generated README block
mpath = os.path.join(work, "analysis", "plugins.manifest.json")
mdemo = json.loads(read(mpath, "r"))
mdemo["plugins"].append(DEMO_ENTRY)
with open(mpath, "w", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(mdemo, indent=2, ensure_ascii=False) + "\n")
gen = subprocess.run([sys.executable, "-I", os.path.join(work, "analysis", "manifest.py"), "readme", "--write"], capture_output=True, text=True)
# ... and the one literal list that stays: the image's COPY in docker/analyzer.Dockerfile (no docker binary here, so no build-time step derives it).
# Forgetting it is caught by docker/test_install_lists.py; the same extraction shows it here (twin: before the edit demo.py is missing, after it nothing is)
import manifest_harness as G0  # noqa: E402

_dpath = os.path.join(work, "docker", "analyzer.Dockerfile")
_dtext = read(_dpath, "r")
_img = {(f"analysis/{s_}", f"/app/{d_}") for s_, d_ in M.install_pairs(M.load(mpath), "image")}
_missing_before = _img - {p_ for p_ in G0.docker_pairs_text(_dtext) if p_[0].startswith("analysis/")}
with open(_dpath, "w", encoding="utf-8", newline="\n") as f:
    f.write(_dtext.replace(" analysis/pcen.py", " analysis/pcen.py analysis/demo.py", 1))
_missing_after = _img - {p_ for p_ in G0.docker_pairs_text(read(_dpath, "r")) if p_[0].startswith("analysis/")}
check(
    "8 the Dockerfile COPY is the one literal list: without the edit the drift is exactly demo.py, with it there is none",
    _missing_before == {("analysis/demo.py", "/app/demo.py")} and _missing_after == set(),
    (sorted(_missing_before), sorted(_missing_after)),
)
after = tree_hashes(work)
added = sorted(set(after) - set(before))
modified = sorted(k for k in before if after.get(k) != before[k])
check(
    "8 HOWTO: adding a plugin touched exactly these files (new: module, schema, spec, fixture, changelog fragment; edited: the manifest, the generated README block and the image COPY line of the Dockerfile) and no host.py, installer, check_contracts.py or test_contracts.py",
    gen.returncode == 0
    and added
    == [
        "analysis/demo.py",
        "contracts/changelog.d/demo-stats.md",
        "contracts/fixtures/demo.jsonl",
        "contracts/schemas/demo.schema.json",
        "contracts/specs/demo-stats.md",
    ]
    and modified == ["analysis/README.md", "analysis/plugins.manifest.json", "docker/analyzer.Dockerfile"],
    (gen.stderr[-200:], added, modified),
)
M2 = M.load(mpath)
M.validate(M2, os.path.join(work, "analysis"))
check("8 the demo manifest validates against the demo tree (module, make, heavy, flush hook both ways)", True)
check(
    "8 install lists: demo.py is in the PC and the image list, and the README table has a demo row (twin: the pristine manifest has neither)",
    ("demo.py", "demo.py") in M.install_pairs(M2, "pc")
    and ("demo.py", "demo.py") in M.install_pairs(M2, "image")
    and "| `demo` | 1 s |" in read(os.path.join(work, "analysis", "README.md"), "r")
    and ("demo.py", "demo.py") not in M.install_pairs(REAL, "pc")
    and "| `demo` |" not in readme,
    "",
)
chk = subprocess.run([sys.executable, "-I", os.path.join(work, "contracts", "check_contracts.py")], capture_output=True, text=True, cwd=work)
chk0 = subprocess.run([sys.executable, "-I", os.path.join(BMA, "contracts", "check_contracts.py")], capture_output=True, text=True, cwd=BMA)
import re  # noqa: E402

n_now, n_before = (int(re.search(r"checked (\d+) record", r.stdout).group(1)) for r in (chk, chk0))
check(
    "8 check_contracts on the demo tree: exit 0, 0 drift, and it validated the 2 demo fixture records (record count +2 against the pristine tree: the new contract is really checked)",
    chk.returncode == 0 and "0 drift finding(s)" in chk.stdout and chk0.returncode == 0 and n_now == n_before + 2,
    (chk.stdout[-300:], chk.stderr[-300:], n_now, n_before),
)
bad_dir = os.path.join(tmp, "demo_bad")
shutil.copytree(work, bad_dir)
with open(os.path.join(bad_dir, "contracts", "fixtures", "demo.jsonl"), "a", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps({"k": "demo", "v": 1, "t": 3.0, "rms": -1}) + "\n")
chk_bad = subprocess.run([sys.executable, "-I", os.path.join(bad_dir, "contracts", "check_contracts.py")], capture_output=True, text=True, cwd=bad_dir)
check(
    "8 twin: a demo fixture record with rms -1 makes check_contracts exit 1 naming demo.jsonl and $.rms",
    chk_bad.returncode == 1 and "DRIFT demo.jsonl" in chk_bad.stdout and "$.rms" in chk_bad.stdout,
    chk_bad.stdout[-300:],
)

sys.path.insert(0, os.path.join(BMA, "analysis"))
import manifest_harness as G  # noqa: E402

port = G.free_port()
out_dir, hist_dir = os.path.join(tmp, "demo_out"), os.path.join(tmp, "demo_hist")
cmd = [
    sys.executable,
    "-I",
    os.path.join(work, "analysis", "host.py"),
    "--listen",
    f"127.0.0.1:{port}",
    "--out",
    out_dir,
    "--history-dir",
    hist_dir,
    "--classify-dir",
    os.path.join(BMA, "classify"),
    "--no",
    "tags",
    "--no",
    "clap",
    "--no",
    "birds",
    "--no",
    "indices",
    "--demo-scale",
    "2.0",
    "--secs",
    "8",
]
hh = subprocess.run([sys.executable, "-I", os.path.join(work, "analysis", "host.py"), "--help"], capture_output=True, text=True, timeout=120, cwd=work)
check(
    "8 the demo tree's host --help lists --demo-scale and `demo` in the --no help, with the flag at the end (cli_slot tail); the pristine help has neither",
    "--demo-scale" in hh.stdout
    and "mixscore perch demo" in " ".join(hh.stdout.split())
    and "--demo-scale" not in ok_h.stdout
    and hh.stdout.index("--beam-shift") < hh.stdout.index("--demo-scale"),
    hh.stderr[-200:],
)
proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, cwd=work)
try:
    if G.wait_listening(port, proc):
        G.stream(port, 5)
    proc.wait(timeout=90)
finally:
    if proc.poll() is None:
        proc.kill()
        proc.wait()
err = proc.stderr.read()
stj = json.load(open(os.path.join(out_dir, "status.json")))
drec = [json.loads(l) for l in open(os.path.join(out_dir, "demo.jsonl"))] if os.path.exists(os.path.join(out_dir, "demo.jsonl")) else []
check(
    "8 the demo tree's host RUNS the plugin: exit 0, status.plugins.demo enabled with windows >= 3 and errors 0, meta.demo_errors 0, demo.jsonl holds records of kind demo with the --demo-scale applied (rms > 0), flushed at shutdown",
    proc.returncode == 0
    and stj["plugins"]["demo"]["enabled"] is True
    and stj["plugins"]["demo"]["windows"] >= 3
    and stj["plugins"]["demo"]["errors"] == 0
    and stj["meta"]["demo_errors"] == 0
    and list(stj["plugins"]).index("demo") == list(stj["plugins"]).index("indices") + 1
    and len(drec) >= 3
    and all(r["k"] == "demo" and r["rms"] > 0 for r in drec),
    (proc.returncode, err[-300:], stj["plugins"].get("demo")),
)
import jsonschema  # noqa: E402

sch_d = json.load(open(os.path.join(work, "contracts", "schemas", "demo.schema.json")))
bad_recs = [r for r in drec if list(jsonschema.Draft202012Validator(sch_d["$defs"]["demo"]).iter_errors(r))]
check(
    "8 every record the demo plugin wrote validates against the demo schema (twin: a record with a negative rms does not)",
    bad_recs == [] and list(jsonschema.Draft202012Validator(sch_d["$defs"]["demo"]).iter_errors(dict(drec[0], rms=-1))) != [],
    str(bad_recs[:1]),
)
shutil.rmtree(tmp, ignore_errors=True)

print(f"{len(fails)} failure(s)")
sys.exit(1 if fails else 0)
