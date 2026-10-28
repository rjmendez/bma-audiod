#!/usr/bin/env python3
"""Drift test for docs/FEATURES.md, the feature registry: no opt-in, off-by-default or removable feature may exist undeclared.

    python scripts/test_features_registry.py        (exit 0 = every check passed; a failure names the exact row to add or remove)
    python scripts/test_features_registry.py --list (print what the registry must cover)

What is compared (all read from the real sources, nothing is run):
  flags      every option of the seven argparse parsers (analysis/host.py, analysis/clips.py add_args, ui/bma_ui.py make_parser,
             pc/bma_spectrum.py, pc/dante_bridge.py, classify/bma_classify.py, pc/bma_cable_sink.py) that is store_true / store_const, defaults to None, False, 0, 0.0, "" or [],
             or whose help says "default off" / "off by default" / "opt-in", needs a row;
  markers    every quoted *.txt / *.json / *.pem name on a Test-Path line of pc/run_pipeline.ps1 and every `[ -f "$APP/x.json" ]` of
             analysis/run_analyzer.sh needs a row;
  plugins    every plugin named in the `--no` help of host.py needs a row keyed `--no <plugin>`;
  tools      every tools/*_report.py and analysis/*_report.py (run by hand) needs a row keyed by its path;
  stale      every key of every row must still be a flag, a marker, a plugin, an existing tool or an existing file (path relative to bench-kit/bma-audiod);
  state      tools/feature_state.py must know exactly the rows of the registry (its FEATURES keys == the first key of every row).
Adding a feature: add one row to docs/FEATURES.md (keys in backticks in the second column) and one entry to tools/feature_state.py FEATURES."""

import argparse, ast, importlib.util, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # bench-kit/bma-audiod
DOC = os.path.join(ROOT, "docs", "FEATURES.md")
PY_SOURCES = {  # AST-read parsers (the parser is built inside main())
    "host.py": os.path.join(ROOT, "analysis", "host.py"),
    "bma_spectrum.py": os.path.join(ROOT, "pc", "bma_spectrum.py"),
    "dante_bridge.py": os.path.join(ROOT, "pc", "dante_bridge.py"),
    "bma_classify.py": os.path.join(ROOT, "classify", "bma_classify.py"),
    "bma_cable_sink.py": os.path.join(ROOT, "pc", "bma_cable_sink.py"),
}
PS1 = os.path.join(ROOT, "pc", "run_pipeline.ps1")
SH = os.path.join(ROOT, "analysis", "run_analyzer.sh")
OFF_HELP = re.compile(r"default:? off|off by default|opt-in|default all 0|\bis off without\b", re.I)
COMPUTED = object()  # a default that is an expression, not a literal: never "off" by value


def ast_flags(src, source="?"):
    """-> [{flag, action, default, help, source}] for every `<x>.add_argument("--flag", ...)` call in src (a non-literal default is COMPUTED)."""
    out = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("--")]
        if not names:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        action = kw["action"].value if isinstance(kw.get("action"), ast.Constant) else "store"
        if action in ("store_true", "store_const"):
            default = False
        elif "default" in kw:
            try:
                default = ast.literal_eval(kw["default"])
            except (ValueError, SyntaxError):
                default = COMPUTED
        else:
            default = None
        try:
            help_ = str(ast.literal_eval(kw["help"])) if "help" in kw else ""
        except (ValueError, SyntaxError):
            help_ = ""
        for n in names:
            out.append({"flag": n, "action": action, "default": default, "help": help_, "source": source})
    return out


def real_flags(parser, source):
    out = []
    for a in parser._actions:
        for o in a.option_strings:
            if o.startswith("--") and o != "--help":
                action = {"_StoreTrueAction": "store_true", "_StoreConstAction": "store_const", "_AppendAction": "append"}.get(type(a).__name__, "store")
                out.append({"flag": o, "action": action, "default": a.default, "help": a.help or "", "source": source})
    return out


def is_off(f):
    """True when the flag is an opt-in switch or defaults to nothing (see the module docstring)."""
    if f["action"] in ("store_true", "store_const") or OFF_HELP.search(f["help"]):
        return True
    d = f["default"]
    if d is COMPUTED:
        return False
    return d is None or d == "" or d == [] or (isinstance(d, (int, float)) and d == 0)


def load(name, sub):
    sys.path.insert(0, os.path.join(ROOT, sub))
    try:
        spec = importlib.util.spec_from_file_location("_feat_" + name[:-3], os.path.join(ROOT, sub, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.pop(0)


def manifest_module():
    spec = importlib.util.spec_from_file_location("_feat_manifest", os.path.join(ROOT, "analysis", "manifest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MF = manifest_module()


def manifest_flags(m):
    """The plugin flags of host.py are not in host.py's text any more: the manifest (analysis/plugins.manifest.json) declares them and
    manifest.add_flags puts them on the host's parser. Read them from a REAL parser built by that function, one call per cli slot."""
    ap = argparse.ArgumentParser(add_help=False)
    for slot in MF.CLI_SLOTS:
        MF.add_flags(ap, m, slot)
    return real_flags(ap, "host.py")


def all_flags(overrides=None, manifest=None):
    """Every flag of the seven parsers. overrides = {file name: source text} replaces that file (read by AST instead of imported);
    manifest = a manifest dict to read the plugin flags of host.py from (default: the real one)."""
    overrides = overrides or {}
    out = []
    for name, path in PY_SOURCES.items():
        src = overrides[name] if name in overrides else open(path, encoding="utf-8", newline="").read()
        out += ast_flags(src, name)
        if name == "host.py":
            out += manifest_flags(manifest if manifest is not None else MF.load())
    for name, sub in (("bma_ui.py", "ui"), ("clips.py", "analysis")):
        if name in overrides:
            out += ast_flags(overrides[name], name)
        elif name == "bma_ui.py":
            out += real_flags(load(name, sub).make_parser({}), name)
        else:
            ap = argparse.ArgumentParser()
            load(name, sub).add_args(ap)
            out += real_flags(ap, name)
    return out


def marker_names(ps1_text, sh_text):
    """-> (switch markers, every file name mentioned). Switch markers = *.txt|json|pem names that reach a Test-Path: written on the
    Test-Path line, or assigned to a variable (`$v = Join-Path ... 'x.txt'`) that a Test-Path line uses; plus `[ -f "$APP/x.json" ]`."""
    ext = r"[A-Za-z0-9_]+\.(?:txt|json|pem)"
    sw, anyn, var = set(), set(), {}
    for m in re.finditer(r"\$(\w+)\s*=\s*Join-Path[^;\n]*?['\"](" + ext + r")['\"]", ps1_text):
        var.setdefault(m.group(1), set()).add(m.group(2))
    for ln in ps1_text.splitlines():
        names = re.findall(r"['\"](" + ext + r")['\"]", ln)
        anyn.update(names)
        if "Test-Path" in ln:
            sw.update(names)
            for v in re.findall(r"\$(\w+)", ln):
                sw.update(var.get(v, ()))
    for m in re.finditer(r'-f\s+"\$APP/(' + ext + r')"', sh_text):
        sw.add(m.group(1))
        anyn.add(m.group(1))
    return sw, anyn


def plugin_names(manifest):
    """Plugin names listed in the `--no` help of host.py (plus `clips`). The help is generated from the manifest (manifest.no_help_text),
    the very function host.py passes to add_argument("--no", help=...)."""
    help_ = MF.no_help_text(manifest)
    m = re.search(r"disable a plugin:([a-z ]+?)(?:\(|$)", help_)
    return (m.group(1).split() if m else []) + (["clips"] if "`clips`" in help_ else [])


def report_tools(root=ROOT):
    out = []
    for sub in ("tools", "analysis"):
        d = os.path.join(root, sub)
        out += [f"{sub}/{n}" for n in sorted(os.listdir(d)) if n.endswith("_report.py") and not n.startswith("test_")]
    return out


def parse_doc(text):
    """-> [{table, name, keys, cells, line}] from the markdown tables of FEATURES.md (header row `Feature | Keys | ...`)."""
    rows, table = [], None
    for i, ln in enumerate(text.splitlines(), 1):
        if ln.startswith("## "):
            table = "on" if "ON by default" in ln else ("off" if "Opt-in" in ln else None)
            continue
        if table and ln.startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", ln):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if cells[0] == "Feature":
                continue
            rows.append({"table": table, "name": cells[0], "keys": re.findall(r"`([^`]+)`", cells[1]), "cells": cells, "line": i})
    return rows


def check(doc_text, flags, ps1_text, sh_text, manifest, tools, existing_tools):
    """-> sorted list of exact problem lines (empty = the registry and the code agree)."""
    rows = parse_doc(doc_text)
    keys = {k for r in rows for k in r["keys"]}
    problems = []
    sw, anyn = marker_names(ps1_text, sh_text)
    plugins = plugin_names(manifest)
    flag_names = {f["flag"] for f in flags}
    for f in flags:
        if is_off(f) and f["flag"] not in keys and not (f["flag"] == "--no" and any(k.startswith("--no ") for k in keys)):
            problems.append(
                f"MISSING ROW: flag {f['flag']} ({f['source']}, default {f['default']!r}) is off by default or opt-in: add a row with key `{f['flag']}` to docs/FEATURES.md"
            )
    for m in sorted(sw):
        if m not in keys:
            problems.append(f"MISSING ROW: marker file {m} switches a feature in the supervisor or launcher: add a row with key `{m}` to docs/FEATURES.md")
    for p in plugins:
        if f"--no {p}" not in keys:
            problems.append(f"MISSING ROW: plugin {p} can be removed with --no {p}: add a row with key `--no {p}` to docs/FEATURES.md")
    for t in tools:
        if t not in keys:
            problems.append(f"MISSING ROW: tool {t} is run by hand: add a row with key `{t}` to docs/FEATURES.md")
    for r in rows:
        if not r["keys"]:
            problems.append(f"BAD ROW: line {r['line']} ({r['name']}) has no key in backticks in its Keys column")
        for k in r["keys"]:
            if not (
                k in flag_names or k in anyn or k in existing_tools or os.path.isfile(os.path.join(ROOT, k)) or (k.startswith("--no ") and k[5:] in plugins)
            ):
                problems.append(
                    f"STALE ROW: line {r['line']} ({r['name']}) names `{k}`, which is no longer a flag, marker file, plugin, tool or file: remove or fix the key in docs/FEATURES.md"
                )
    return sorted(problems)


def state_problems(doc_text, state):
    """state = {first key: Feature name} as tools/feature_state.py FEATURES holds it."""
    state_keys = list(state)
    docs = {r["keys"][0]: r["name"] for r in parse_doc(doc_text) if r["keys"]}
    first = set(docs)
    out = [
        f"STATE NAME: row `{k}` is `{docs[k]}` in docs/FEATURES.md but `{state[k]}` in tools/feature_state.py: make them equal"
        for k in sorted(first & set(state_keys))
        if docs[k] != state[k]
    ]
    out += [f"STATE MISSING: row `{k}` of docs/FEATURES.md has no entry in tools/feature_state.py FEATURES: add one" for k in sorted(first - set(state_keys))]
    out += [
        f"STATE STALE: tools/feature_state.py FEATURES has `{k}` but docs/FEATURES.md has no row starting with that key: remove it or add the row"
        for k in sorted(set(state_keys) - first)
    ]
    return out


def rd(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def real_inputs():
    return dict(
        doc_text=rd(DOC), flags=all_flags(), ps1_text=rd(PS1), sh_text=rd(SH), manifest=MF.load(), tools=report_tools(), existing_tools=set(report_tools())
    )


def load_state_module():
    spec = importlib.util.spec_from_file_location("feature_state", os.path.join(ROOT, "tools", "feature_state.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ tests
fails, n = [], 0


def ck(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def run_tests():
    inp = real_inputs()
    base = check(**inp)
    ck("the real registry and the real sources agree (no problem lines)", base == [], "\n".join(base))
    sk = {k: v[0] for k, v in load_state_module().FEATURES.items()}
    sp = state_problems(inp["doc_text"], sk)
    ck("tools/feature_state.py FEATURES == the first key of every registry row", sp == [], "\n".join(sp))

    # --- the extractor, on a fixture with known answers (exact values)
    fx = (
        "import argparse\nap = argparse.ArgumentParser()\n"
        'ap.add_argument("--sw", action="store_true", help="x")\n'
        'ap.add_argument("--none")\n'
        'ap.add_argument("--zero", type=int, default=0)\n'
        'ap.add_argument("--zerof", type=float, default=0.0)\n'
        'ap.add_argument("--empty", default="")\n'
        'ap.add_argument("--lst", action="append", default=[])\n'
        'ap.add_argument("--helped", default="x", help="enable y (default OFF)")\n'
        'ap.add_argument("--optin", default=3, help="an opt-in thing")\n'
        'ap.add_argument("--five", type=int, default=5, help="plain")\n'
        'ap.add_argument("--comp", default=os.path.join("a", "b"))\n'
        'ap.add_argument("--s", "--long-s", default=None)\n'
    )
    got = {f["flag"]: is_off(f) for f in ast_flags(fx)}
    want = {
        "--sw": True,
        "--none": True,
        "--zero": True,
        "--zerof": True,
        "--empty": True,
        "--lst": True,
        "--helped": True,
        "--optin": True,
        "--five": False,
        "--comp": False,
        "--s": True,
        "--long-s": True,
    }
    ck("extractor: off-by-default classification of every default kind (exact)", got == want, str(got))
    ck("extractor: a literal non-zero default and a computed default are not off (positive twin)", got["--five"] is False and got["--comp"] is False)

    # --- the real parsers: the hand-built AST path agrees with real argparse for the two parsers that can be imported
    ui_real = {f["flag"]: (f["action"] == "store_true", f["default"]) for f in real_flags(load("bma_ui.py", "ui").make_parser({}), "ui")}
    ui_ast = {f["flag"] for f in ast_flags(rd(os.path.join(ROOT, "ui", "bma_ui.py")), "ui")}
    ck("extractor == real argparse: bma_ui flag names", set(ui_real) == ui_ast, str(set(ui_real) ^ ui_ast))
    names = {f["flag"] for f in inp["flags"]}
    for must in ("--mixscore", "--perch-model", "--clip-audio", "--tailnet-identity", "--detail", "--all-to", "--spectrum-detail", "--clip-pcen"):
        ck(f"the seven parsers expose {must}", must in names)

    # --- mutation 1: remove a row -> red with the exact row to add
    for key, expect in (
        ("--mixscore", "MISSING ROW: flag --mixscore"),
        ("enable_clip_audio.txt", "MISSING ROW: marker file enable_clip_audio.txt"),
        ("--spectrum-detail", "MISSING ROW: flag --spectrum-detail"),
        ("tools/perch_report.py", "MISSING ROW: tool tools/perch_report.py"),
    ):
        m = check(**{**inp, "doc_text": _drop_row(inp["doc_text"], key)})
        ck(f"mutation: removing the row of {key} -> red, naming it", any(p.startswith(expect) and f"`{key}`" in p for p in m), "\n".join(m[:3]))
    m = check(**{**inp, "doc_text": _drop_row(inp["doc_text"], "--no dsp")})
    ck(
        "mutation: removing the row of plugin dsp -> red with the key `--no dsp`",
        any(p.startswith("MISSING ROW: plugin dsp") and "`--no dsp`" in p for p in m),
        "\n".join(m[:3]),
    )
    ck("positive twin: the unmutated registry has no MISSING ROW for those keys", not any("--mixscore" in p or "enable_clip_audio" in p for p in base))

    # --- mutation 2: a fake flag in each parser kind -> red, naming flag and file
    host_src = rd(PY_SOURCES["host.py"])  # the core flags of host.py (the plugin flags come from the manifest)
    fake = host_src.replace('ap.add_argument("--secs"', 'ap.add_argument("--fake-new-thing", action="store_true")\n    ap.add_argument("--secs"', 1)
    ck("the fake-flag edit really changed host.py's text", fake != host_src)
    m = check(**{**inp, "flags": all_flags({"host.py": fake})})
    ck(
        "mutation: a new store_true flag in host.py -> exactly one problem, the row to add",
        m
        == [
            "MISSING ROW: flag --fake-new-thing (host.py, default False) is off by default or opt-in: add a row with key `--fake-new-thing` to docs/FEATURES.md"
        ],
        "\n".join(m),
    )
    fake_default = host_src.replace('ap.add_argument("--secs"', 'ap.add_argument("--fake-path", default=None)\n    ap.add_argument("--secs"', 1)
    m = check(**{**inp, "flags": all_flags({"host.py": fake_default})})
    ck("mutation: a new default-None flag in host.py -> red", len(m) == 1 and "--fake-path" in m[0], "\n".join(m))
    plain = host_src.replace('ap.add_argument("--secs"', 'ap.add_argument("--fake-tuned", type=int, default=7)\n    ap.add_argument("--secs"', 1)
    ck("positive twin: a new flag with a non-empty default needs no row", check(**{**inp, "flags": all_flags({"host.py": plain})}) == [])
    fm = json.loads(json.dumps(inp["manifest"]))
    next(e for e in fm["plugins"] if e["name"] == "bursts")["flags"].append({"name": "--fake-plugin-flag", "type": "flag", "help": "x"})
    m = check(**{**inp, "flags": all_flags(manifest=fm), "manifest": fm})
    ck(
        "mutation: a new store_true flag declared in the manifest (not in host.py's text) -> exactly one problem, the row to add",
        m
        == [
            "MISSING ROW: flag --fake-plugin-flag (host.py, default False) is off by default or opt-in: add a row with key `--fake-plugin-flag` to docs/FEATURES.md"
        ],
        "\n".join(m),
    )
    fm2 = json.loads(json.dumps(inp["manifest"]))
    next(e for e in fm2["plugins"] if e["name"] == "bursts")["flags"].append({"name": "--fake-tuned-plugin", "type": "int", "default": 7, "help": "x"})
    ck("positive twin: a new manifest flag with a non-empty default needs no row", check(**{**inp, "flags": all_flags(manifest=fm2), "manifest": fm2}) == [])
    ck(
        "the real parser the manifest builds carries master's --perch-device and --perch-ort-dir",
        {"--perch-device", "--perch-ort-dir"} <= {f["flag"] for f in all_flags()},
    )
    ui_src = rd(os.path.join(ROOT, "ui", "bma_ui.py"))
    ui_fake = ui_src.replace("    return ap\n", '    ap.add_argument("--fake-ui", action="store_true")\n    return ap\n', 1)
    ck("the fake-flag edit really changed bma_ui.py's text", ui_fake != ui_src)
    m = check(**{**inp, "flags": all_flags({"bma_ui.py": ui_fake})})
    ck("mutation: a new flag in the bma_ui parser -> red naming the file", any("--fake-ui (bma_ui.py" in p for p in m), "\n".join(m))
    sp_src = rd(PY_SOURCES["bma_spectrum.py"])
    sp_fake = sp_src.replace('ap.add_argument("--detail"', 'ap.add_argument("--fake-spec", action="store_true")\n    ap.add_argument("--detail"', 1)
    m = check(**{**inp, "flags": all_flags({"bma_spectrum.py": sp_fake})})
    ck("mutation: a new flag in the spectrum parser -> red", any("--fake-spec (bma_spectrum.py" in p for p in m), "\n".join(m))
    br_src = rd(PY_SOURCES["dante_bridge.py"])
    br_fake = br_src.replace('ap.add_argument("--slot"', 'ap.add_argument("--fake-bridge", action="store_true")\n    ap.add_argument("--slot"', 1)
    m = check(**{**inp, "flags": all_flags({"dante_bridge.py": br_fake})})
    ck("mutation: a new flag in the bridge parser -> red", any("--fake-bridge (dante_bridge.py" in p for p in m), "\n".join(m))
    cl_src = rd(os.path.join(ROOT, "analysis", "clips.py"))
    cl_fake = cl_src.replace(
        '    ap.add_argument("--clip-audio"', '    ap.add_argument("--fake-clip", action="store_true")\n    ap.add_argument("--clip-audio"', 1
    )
    ck("the fake-flag edit really changed clips.py's text", cl_fake != cl_src)
    m = check(**{**inp, "flags": [f for f in all_flags() if f["source"] != "clips.py"] + ast_flags(cl_fake, "clips.py")})
    ck("mutation: a new flag in clips.add_args -> red", any("--fake-clip (clips.py" in p for p in m), "\n".join(m))

    # --- mutation 3: a new marker and a new plugin and a new tool
    ps1_fake = inp["ps1_text"] + "\nif (Test-Path -LiteralPath (Join-Path $root 'enable_fake.txt')) { }\n"
    m = check(**{**inp, "ps1_text": ps1_fake})
    ck(
        "mutation: a new Test-Path marker in run_pipeline.ps1 -> exactly one problem",
        m
        == [
            "MISSING ROW: marker file enable_fake.txt switches a feature in the supervisor or launcher: add a row with key `enable_fake.txt` to docs/FEATURES.md"
        ],
        "\n".join(m),
    )
    ps1_state = inp["ps1_text"] + "\n$x = Join-Path $root 'bridge_state_fake.json'\n"
    ck("positive twin: a file name that is not on a Test-Path line needs no row", check(**{**inp, "ps1_text": ps1_state}) == [])
    sh_fake = inp["sh_text"] + '\n[ -f "$APP/fake_mask.json" ] && EXTRA+=(--x)\n'
    m = check(**{**inp, "sh_text": sh_fake})
    ck(
        'mutation: a new `[ -f "$APP/x.json" ]` marker in run_analyzer.sh -> red',
        m
        == [
            "MISSING ROW: marker file fake_mask.json switches a feature in the supervisor or launcher: add a row with key `fake_mask.json` to docs/FEATURES.md"
        ],
        "\n".join(m),
    )
    plug_m = json.loads(json.dumps(inp["manifest"]))
    plug_m["plugins"].append(dict(next(e for e in plug_m["plugins"] if e["name"] == "bursts"), name="zzfake", flags=[], contract=None))
    ck(
        "the fake-plugin edit really changed the manifest's `--no` help",
        MF.no_help_text(plug_m) != MF.no_help_text(inp["manifest"]) and "zzfake" in MF.no_help_text(plug_m),
    )
    m = check(**{**inp, "manifest": plug_m})
    ck(
        "mutation: a new plugin in the --no help -> red with the key `--no zzfake`",
        len(m) == 1 and m[0].startswith("MISSING ROW: plugin zzfake") and "`--no zzfake`" in m[0],
        "\n".join(m),
    )
    m = check(**{**inp, "tools": inp["tools"] + ["tools/fake_report.py"]})
    ck(
        "mutation: a new *_report.py tool -> red with its path",
        m == ["MISSING ROW: tool tools/fake_report.py is run by hand: add a row with key `tools/fake_report.py` to docs/FEATURES.md"],
        "\n".join(m),
    )

    # --- the classifier and the cable sink parsers
    ck("the seven parsers expose the classifier and cable sink flags", {"--udp", "--lat", "--record"} <= names)
    cf_src = rd(PY_SOURCES["bma_classify.py"])
    cf_fake = cf_src.replace('    p.add_argument("--dtype"', '    p.add_argument("--fake-classify", action="store_true")\n    p.add_argument("--dtype"', 1)
    ck("the fake-flag edit really changed bma_classify.py's text", cf_fake != cf_src)
    m = check(**{**inp, "flags": all_flags({"bma_classify.py": cf_fake})})
    ck(
        "mutation: a new flag in the classifier parser -> exactly one problem naming the file",
        m
        == [
            "MISSING ROW: flag --fake-classify (bma_classify.py, default False) is off by default or opt-in: add a row with key `--fake-classify` to docs/FEATURES.md"
        ],
        "\n".join(m),
    )
    m = check(**{**inp, "doc_text": _drop_row(inp["doc_text"], "--record")})
    ck(
        "mutation: removing the cable sink recording row -> red naming `--record`",
        any(p.startswith("MISSING ROW: flag --record (bma_cable_sink.py") for p in m),
        "\n".join(m),
    )
    m = check(**{**inp, "doc_text": _drop_row(inp["doc_text"], "--udp")})
    ck(
        "mutation: removing the legacy classifier row -> red naming `--udp`",
        any(p.startswith("MISSING ROW: flag --udp (bma_classify.py") for p in m),
        "\n".join(m),
    )
    ghost = inp["doc_text"].replace("`analysis/plugins.py`", "`analysis/no_such_file.py`", 1)
    ck("the ghost-path edit really changed the doc", ghost != inp["doc_text"])
    m = check(**{**inp, "doc_text": ghost})
    ck(
        "mutation: a row keyed by a file that does not exist -> STALE ROW; a path that exists is accepted (positive twin)",
        len(m) == 1 and m[0].startswith("STALE ROW") and "`analysis/no_such_file.py`" in m[0] and not any("`analysis/plugins.py`" in p for p in base),
        "\n".join(m),
    )

    # --- mutation 4: stale rows
    stale = inp["doc_text"].replace("`--clip-pcen`", "`--clip-pcenn`", 1)
    ck("the stale-key edit really changed the doc", stale != inp["doc_text"])
    m = check(**{**inp, "doc_text": stale})
    ck(
        "mutation: a row naming a removed flag -> STALE ROW with the line and key, and its real twin is missing",
        any(p.startswith("STALE ROW") and "`--clip-pcenn`" in p for p in m) and any(p.startswith("MISSING ROW: flag --clip-pcen ") for p in m),
        "\n".join(m),
    )
    gone_m = json.loads(json.dumps(inp["manifest"]))
    for e in gone_m["plugins"]:
        for fl in e.get("flags", []):
            if fl["name"] == "--perch-threads":
                fl["name"] = "--perch-thr"
    ck("the flag rename really changed the manifest", gone_m != inp["manifest"])
    m = check(**{**inp, "flags": all_flags(manifest=gone_m), "manifest": gone_m})
    ck("mutation: a flag renamed in the code -> its row is STALE", any(p.startswith("STALE ROW") and "`--perch-threads`" in p for p in m), "\n".join(m))
    nokey = inp["doc_text"].replace("| Clip PCEN spectrogram | `--clip-pcen` |", "| Clip PCEN spectrogram |  |", 1)
    ck("the empty-key edit really changed the doc", nokey != inp["doc_text"])
    m = check(**{**inp, "doc_text": nokey})
    ck("mutation: a row without keys -> BAD ROW", any(p.startswith("BAD ROW") and "Clip PCEN spectrogram" in p for p in m), "\n".join(m))

    # --- the live-state table
    ck(
        "state: a doc row without a feature_state entry -> red naming the key",
        state_problems(inp["doc_text"], {k: v for k, v in sk.items() if k != "--mixscore"})
        == ["STATE MISSING: row `--mixscore` of docs/FEATURES.md has no entry in tools/feature_state.py FEATURES: add one"],
    )
    ck(
        "state: a feature_state entry without a doc row -> red naming the key",
        state_problems(inp["doc_text"], {**sk, "--ghost": "Ghost"})
        == ["STATE STALE: tools/feature_state.py FEATURES has `--ghost` but docs/FEATURES.md has no row starting with that key: remove it or add the row"],
    )
    ck(
        "state: a differing Feature name -> red naming both",
        state_problems(inp["doc_text"], {**sk, "--mixscore": "Mixscore"})
        == ["STATE NAME: row `--mixscore` is `Mixscore plugin` in docs/FEATURES.md but `Mixscore` in tools/feature_state.py: make them equal"],
    )
    # every row's cells: Default is ON or OFF, Approved is Y, N or pending, the table has all columns
    rows = parse_doc(inp["doc_text"])
    ck("every row has 7 columns", all(len(r["cells"]) == 7 for r in rows), str([r["name"] for r in rows if len(r["cells"]) != 7]))
    ck(
        "every Default cell is ON or OFF",
        all(r["cells"][3] in ("ON", "OFF") for r in rows),
        str([(r["name"], r["cells"][3]) for r in rows if r["cells"][3] not in ("ON", "OFF")]),
    )
    ck(
        "every Approved cell is Y, N or pending",
        all(r["cells"][6] in ("Y", "N", "pending") for r in rows),
        str([(r["name"], r["cells"][6]) for r in rows if r["cells"][6] not in ("Y", "N", "pending")]),
    )
    ck(
        "the opt-in table has only OFF defaults",
        all(r["cells"][3] == "OFF" for r in rows if r["table"] == "off"),
        str([r["name"] for r in rows if r["table"] == "off" and r["cells"][3] != "OFF"]),
    )
    ck("the removable table has only ON defaults", all(r["cells"][3] == "ON" for r in rows if r["table"] == "on"))
    ck("no row key is declared twice", len([k for r in rows for k in r["keys"]]) == len({k for r in rows for k in r["keys"]}))


def _drop_row(text, key):
    """The doc text without the row whose Keys cell holds `key` (exactly one row must go)."""
    lines = text.splitlines(keepends=True)
    keep = [ln for ln in lines if not (ln.startswith("|") and f"`{key}`" in (ln.split("|") + ["", "", ""])[2])]
    assert len(keep) == len(lines) - 1, f"{key}: {len(lines) - len(keep)} rows matched"
    return "".join(keep)


if __name__ == "__main__":
    if "--list" in sys.argv:
        inp = real_inputs()
        for f in inp["flags"]:
            if is_off(f):
                print(f["flag"], f["source"], "computed" if f["default"] is COMPUTED else repr(f["default"]))
        print(sorted(marker_names(inp["ps1_text"], inp["sh_text"])[0]), plugin_names(inp["manifest"]), inp["tools"])
        sys.exit(0)
    run_tests()
    print(f"\n{n} checks, {len(fails)} failure(s)")
    sys.exit(1 if fails else 0)
