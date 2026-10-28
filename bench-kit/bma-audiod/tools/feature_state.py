#!/usr/bin/env python3
"""Live ON/OFF state of every feature in docs/FEATURES.md (the feature registry). Read-only, stdlib only, exit code 0 always.

    python -I feature_state.py --root <data root> [--json] [--write] [--registry docs/FEATURES.md]

Reads <root>\\analysis\\status.json (plugins, clips), <root>\\analyzer_args.txt, the marker files (<root>, <root>\\tls, <root>\\app),
<root>\\run_pipeline.ps1 (the flags the supervisor passes), <root>\\bma.local.ps1 and the last lines of <root>\\logs\\supervisor.log.
The running dashboard's command line is not read: its flags come from the supervisor script and the files it tests.
Output: one line `N features OFF, M ON, K UNKNOWN`, then OFF rows first, UNKNOWN, ON. --write also writes <root>\\reports\\features.txt.
A feature that is ON while its Approved cell in the registry is `pending` (or `N`) adds a `WARNING ON but Approved=...` line after the second line;
the registry is --registry, else ../docs/FEATURES.md, else FEATURES.md next to this file (none found: no warning line).
Every row of docs/FEATURES.md needs an entry in FEATURES below (scripts/test_features_registry.py enforces it)."""
import argparse, json, os, re, sys, time

OFF, ON, UNKNOWN = "OFF", "ON", "UNKNOWN"


class Ctx:
    def __init__(self, root):
        self.root = root
        self.app = os.path.join(root, "app")
        self.status = self._json(os.path.join(root, "analysis", "status.json"))
        self.args = self._args(os.path.join(root, "analyzer_args.txt"))
        self.ps1 = self._text(os.path.join(root, "run_pipeline.ps1"))
        self.localps = self._text(os.path.join(root, "bma.local.ps1"))
        self.supervisor_up = self._last_start(os.path.join(root, "logs", "supervisor.log"))

    @staticmethod
    def _text(p):
        try:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def _json(self, p):
        try:
            return json.loads(self._text(p))
        except ValueError:
            return {}

    def _args(self, p):
        out = []
        for ln in self._text(p).splitlines():
            v = ln.strip().lstrip("﻿")
            if v and not v.startswith("#"):
                out.append(v)
        return out

    @staticmethod
    def _last_start(p):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 262144))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return ""
        ups = [ln for ln in lines if " supervisor up" in ln]
        return ups[-1][:19] if ups else ""

    def exists(self, *parts):
        return os.path.exists(os.path.join(self.root, *parts))

    def has_arg(self, flag):
        return any(a == flag or a.startswith(flag + "=") for a in self.args)

    def arg_val(self, flag):
        for i, a in enumerate(self.args):
            if a == flag:
                nxt = self.args[i + 1] if i + 1 < len(self.args) else ""
                return "" if nxt.startswith("--") else nxt          # a switch has no value: the next line is another flag
            if a.startswith(flag + "="):
                return a.split("=", 1)[1]
        return None

    def status_age(self):
        t = self.status.get("t")
        return f"{max(0, int(time.time() - t))} s" if isinstance(t, (int, float)) and not isinstance(t, bool) else "unknown"

    def removed(self):
        """plugin names removed by `--no X` lines of analyzer_args.txt"""
        out = []
        for i, a in enumerate(self.args):
            if a == "--no" and i + 1 < len(self.args):
                out.append(self.args[i + 1])
            elif a.startswith("--no="):
                out.append(a[5:])
        return out

    def plugin(self, name):
        p = (self.status.get("plugins") or {}).get(name)
        return p if isinstance(p, dict) else None

    def supervisor_passes(self, flag):
        code = " ".join(ln for ln in self.ps1.splitlines() if not ln.lstrip().startswith("#"))    # a flag named in a comment is not passed
        return ("'" + flag + "'") in code

    def allowed_logins(self):
        return [ln for ln in self._text(os.path.join(self.root, "allowed_users.txt")).splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _plugin_state(name, flag_off, marker=None):
    """opt-in plugin: the running analyzer's status.json decides; without it the arguments and the marker."""
    def f(c):
        p = c.plugin(name)
        if p is not None:
            if p.get("enabled"):
                return ON, "plugin running (status.json)" + (f", device {p['device']}" if p.get("device") else "")
            return OFF, str(p.get("reason") or "plugin not running (status.json)")
        if not c.status:
            return UNKNOWN, "no analysis/status.json"
        if (flag_off and c.has_arg(flag_off)) or (marker and os.path.exists(os.path.join(c.app, marker))):
            return ON, "configured, plugin absent from status.json"
        return OFF, "not configured"
    return f


def _removable(name):
    def f(c):
        p = c.plugin(name)
        if p is not None:
            return (ON, "plugin running (status.json)") if p.get("enabled") else (OFF, str(p.get("reason") or "disabled (status.json)"))
        if name in c.removed():
            return OFF, "--no " + name + " in analyzer_args.txt"
        return (ON, "default, no status.json") if not c.status else (OFF, "absent from status.json")
    return f


def _arg(flag, *more):
    def f(c):
        got = [x for x in (flag,) + more if c.has_arg(x)]
        if got:
            return ON, "analyzer_args.txt: " + ", ".join(f"{x} {c.arg_val(x)}".strip() for x in got)
        return OFF, "not in analyzer_args.txt"
    return f


def _sup(flag, *more):
    def f(c):
        if not c.ps1:
            return UNKNOWN, "no run_pipeline.ps1 in the data root"
        got = [x for x in (flag,) + more if c.supervisor_passes(x)]
        return (ON, "supervisor passes " + ", ".join(got)) if got else (OFF, "supervisor does not pass it")
    return f


def _clip_audio(c):
    marker, arg = c.exists("enable_clip_audio.txt"), c.has_arg("--clip-audio")
    cl = c.status.get("clips") if isinstance(c.status.get("clips"), dict) else None
    if cl is not None and "audio" in cl:
        if cl["audio"]:
            return ON, "analyzer is storing audio (status.json clips.audio)" + (", enable_clip_audio.txt present" if marker else "")
        if marker or arg:
            return OFF, "configured but the running analyzer has audio off: ON at the next analyzer start"
        return OFF, "visual-only clips"
    return (ON, "configured, no status.json") if (marker or arg) else (OFF, "no marker, no status.json")


def _app_marker(name, flag, *more):
    def f(c):
        if os.path.exists(os.path.join(c.app, name)):
            return ON, "app\\" + name + " present"
        if c.has_arg(flag):
            return ON, "analyzer_args.txt: " + flag
        return OFF, "app\\" + name + " absent"
    return f


def _beam_shift(c):
    m = re.search(r"BMA_BEAM_SHIFT\s*=\s*['\"]([^'\"]+)['\"]", c.localps)
    if c.has_arg("--beam-shift"):
        return ON, "analyzer_args.txt: --beam-shift " + str(c.arg_val("--beam-shift"))
    if m and any(x != "0" for x in m.group(1).split(",")):
        return ON, "bma.local.ps1 BMA_BEAM_SHIFT " + m.group(1)
    if os.environ.get("BMA_BEAM_SHIFT"):
        return ON, "BMA_BEAM_SHIFT in this process environment (the supervisor's may differ)"
    return OFF, "no shift configured"


def _tls(c):
    for d in (os.path.join(c.root, "tls"), c.root):
        if os.path.exists(os.path.join(d, "dashboard_cert.pem")) and os.path.exists(os.path.join(d, "dashboard_key.pem")):
            return ON, "certificate and key found in " + ("tls\\" if d != c.root else "<root>")
    return OFF, "no certificate and key pair"


def _tailnet(c):
    n = len(c.allowed_logins())
    return (ON, f"allowed_users.txt with {n} login(s)") if n else (OFF, "allowed_users.txt absent or without a login")


def _passkeys(c):
    if c.exists("enable_passkeys.txt") and c.exists("tls", "tls_name.txt"):
        return ON, "enable_passkeys.txt and tls\\tls_name.txt present"
    return OFF, "enable_passkeys.txt " + ("present" if c.exists("enable_passkeys.txt") else "absent") + ", tls\\tls_name.txt " + ("present" if c.exists("tls", "tls_name.txt") else "absent")


def _ui_next(c):
    return (ON, "enable_ui_next.txt present") if c.exists("enable_ui_next.txt") else (OFF, "enable_ui_next.txt absent")


def _analyzer_args_file(c):
    if not c.exists("analyzer_args.txt"):
        return OFF, "analyzer_args.txt absent"
    return ON, f"{len(c.args)} line(s)"


def _by_hand(c):
    return OFF, "run by hand, never scheduled"


def _board(c):
    return UNKNOWN, "board URL is resolved at dashboard start and not logged"


def _classifier(c):
    if not c.ps1:
        return UNKNOWN, "no run_pipeline.ps1 in the data root"
    return (ON, "supervisor starts it (run_classifier.sh)") if "run_classifier.sh" in c.ps1 else (OFF, "supervisor does not start it")


def _embedding_store(c):
    n = c.status.get("embeddings_stored")
    if not isinstance(n, int) or isinstance(n, bool):
        return UNKNOWN, "no embeddings_stored in status.json"
    return ON, f"status.json embeddings_stored {n} since the analyzer start"


def _always_off(why):
    return lambda c: (OFF, why)


def _clips_on(c):
    cl = c.status.get("clips")
    if isinstance(cl, dict) and "enabled" in cl:
        return (ON if cl["enabled"] else OFF), "status.json clips.enabled"
    return (OFF, "--no clips in analyzer_args.txt") if "clips" in c.removed() else (ON, "default, no status.json")


# first key of the registry row -> (Feature name as in docs/FEATURES.md, table, detector)
FEATURES = {
    "--mixscore": ("Mixscore plugin", "off", _plugin_state("mixscore", "--mixscore", "mixscore.json")),
    "--tags-pad-s": ("Pad lane (experimental)", "off", _arg("--tags-pad-s", "--clap-pad-s")),
    "--perch-model": ("Perch shadow plugin", "off", _plugin_state("perch", "--perch-model")),
    "--perch-device": ("Perch device", "off", lambda c: (ON, "analyzer_args.txt: --perch-device " + str(c.arg_val("--perch-device"))) if c.has_arg("--perch-device") else (OFF, "default cpu")),
    "--perch-ort-dir": ("Perch ONNX runtime dir", "off", _arg("--perch-ort-dir")),
    "--perch-embeddings": ("Perch embeddings", "off", lambda c: (ON, "analyzer_args.txt: --perch-embeddings") if c.has_arg("--perch-embeddings") else (OFF, "not in analyzer_args.txt")),
    "--clip-audio": ("Clip audio", "off", _clip_audio),
    "--clip-pcen": ("Clip PCEN spectrogram", "off", _arg("--clip-pcen")),
    "--clip-min-conf": ("Clip confidence gate", "off", lambda c: (ON, "analyzer_args.txt: --clip-min-conf " + str(c.arg_val("--clip-min-conf")) + " (default 0.85)") if c.has_arg("--clip-min-conf") else (OFF, "default 0.85")),
    "--clip-skip-out-of-region": ("Clip out-of-region skip", "off", _arg("--clip-skip-out-of-region")),
    "--clips-dir": ("Clip path overrides", "off", _arg("--clips-dir", "--clip-exclude")),
    "analyzer_args.txt": ("Analyzer args file", "off", _analyzer_args_file),
    "--tonal-mask": ("Tonal mask", "off", _app_marker("tonal_mask.json", "--tonal-mask")),
    "--geo-config": ("County species filter", "off", _app_marker("geo_county.json", "--geo-config")),
    "--taxa": ("Analyzer path overrides", "off", _arg("--taxa", "--history-dir", "--calibration")),
    "--secs": ("Analyzer self-exit timer", "off", _arg("--secs")),
    "--indices-without-speech-gate": ("Indices without speech gate", "off", _arg("--indices-without-speech-gate")),
    "--beam-shift": ("Beam shift", "off", _beam_shift),
    "--spectrum-port": ("Spectrum rows to dashboard", "off", _sup("--spectrum-port")),
    "--detail": ("Spectrum detail tier", "off", _sup("--detail", "--spectrum-detail")),
    "--ui-next": ("Next dashboard", "off", _ui_next),
    "--all-to": ("Bridge spectrum fan-out", "off", _sup("--all-to")),
    "--iface-ip": ("Bridge manual addressing", "off", _sup("--iface-ip", "--card")),
    "--tls-cert": ("Dashboard TLS", "off", _tls),
    "--tailnet-identity": ("Tailnet identity login", "off", _tailnet),
    "--require-identity": ("Identity only", "off", _sup("--require-identity")),
    "--passkey-origin": ("Passkeys", "off", _passkeys),
    "--allow-ip-hosts": ("Dashboard IP hosts", "off", _sup("--allow-ip-hosts")),
    "--auth-token-file": ("Dashboard token file", "off", _sup("--auth-token-file")),
    "--no-auth": ("Dashboard without login", "off", _sup("--no-auth")),
    "--board-url": ("Dashboard board panel", "off", _board),
    "--labels-dir": ("Dashboard misc", "off", _sup("--labels-dir", "--slot-fix-at")),
    "--review-allow-no-clip": ("Review without clips", "off", _sup("--review-allow-no-clip")),
    "--record": ("Cable sink recording", "off", _by_hand),
    "tools/loss_report.py": ("Report tools by hand", "off", _by_hand),
    "--no dsp": ("DSP plugin", "on", _removable("dsp")),
    "--no spatial": ("Spatial plugin", "on", _removable("spatial")),
    "--no tags": ("Tags plugin", "on", _removable("tags")),
    "--no clap": ("CLAP plugin", "on", _removable("clap")),
    "--no birds": ("Birds plugin", "on", _removable("birds")),
    "--no coherence": ("Coherence plugin", "on", _removable("coherence")),
    "--no indices": ("Indices plugin", "on", _removable("indices")),
    "--no bursts": ("Bursts plugin", "on", _removable("bursts")),
    "--no-artifact-gate": ("Artifact flags (dashboard)", "on", lambda c: (OFF, "supervisor passes --no-artifact-gate") if c.ps1 and c.supervisor_passes("--no-artifact-gate") else (ON, "default, the supervisor does not pass --no-artifact-gate")),
    "--no clips": ("Wildlife clips", "on", _clips_on),
    "classify/bma_classify.py": ("Legacy classifier", "on", _classifier),
    "analysis/plugins.py": ("PANNs embedding store", "on", _embedding_store),
}


def collect(root):
    c = Ctx(root)
    rows = []
    for key, (name, table, fn) in FEATURES.items():
        try:
            state, detail = fn(c)
        except Exception as e:                                  # one broken detector must not hide the others
            state, detail = UNKNOWN, f"detector failed: {type(e).__name__}"
        rows.append({"key": key, "name": name, "table": table, "state": state, "detail": detail})
    return c, rows


def _registry_path(path=None):
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (path, os.path.join(here, "..", "docs", "FEATURES.md"), os.path.join(here, "FEATURES.md")):
        if p and os.path.isfile(p):
            return p
    return None


def load_approved(path=None):
    """-> {feature name: Approved cell} from the tables of the registry; {} when it cannot be read."""
    p = _registry_path(path)
    if p is None:
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    out, col = {}, None
    for ln in lines:
        if not ln.startswith("|"):
            col = None
            continue
        cells = [x.strip() for x in re.split(r"(?<!\\)\|", ln.strip().strip("|"))]
        if cells[0] == "Feature" and "Approved" in cells:
            col = cells.index("Approved")
        elif col is not None and len(cells) > col and cells[0] and not set(cells[0]) <= set("-: "):
            out[cells[0]] = cells[col]
    return out


def approval_warnings(rows, approved):
    """-> warning lines for the features that are ON while the registry says pending or N."""
    out = []
    for cell in ("pending", "N"):
        names = [r["name"] for r in rows if r["state"] == ON and approved.get(r["name"]) == cell]
        if names:
            out.append("WARNING ON but Approved=%s (%d): %s" % (cell, len(names), ", ".join(names)))
    return out


def render(c, rows, warnings=()):
    order = {OFF: 0, UNKNOWN: 1, ON: 2}
    rows = sorted(rows, key=lambda r: order[r["state"]])        # stable: registry order inside each state
    n = {s: sum(1 for r in rows if r["state"] == s) for s in (OFF, ON, UNKNOWN)}
    out = [f"{n[OFF]} features OFF, {n[ON]} ON, {n[UNKNOWN]} UNKNOWN",
           f"as of {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}; supervisor started {c.supervisor_up or 'unknown'}; status.json age {c.status_age()}; registry docs/FEATURES.md"]
    out += list(warnings)
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        out.append(f"{r['state']:<8}{r['name']:<{w + 2}}{r['detail']}")
    return "\n".join(out) + "\n", n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.environ.get("BMA_DATA_DIR", "."), help="data root (default $BMA_DATA_DIR or .)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("--write", action="store_true", help="also write <root>/reports/features.txt")
    ap.add_argument("--registry", default=None, help="docs/FEATURES.md (default: the repo's docs, or FEATURES.md beside this file)")
    a = ap.parse_args(argv)
    try:
        c, rows = collect(a.root)
        warn = approval_warnings(rows, load_approved(a.registry))
        text, n = render(c, rows, warn)
        if a.write:
            try:
                os.makedirs(os.path.join(a.root, "reports"), exist_ok=True)
                with open(os.path.join(a.root, "reports", "features.txt"), "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
            except OSError as e:
                print(f"feature_state: could not write reports/features.txt: {type(e).__name__}", file=sys.stderr)
        if a.json:
            order = {OFF: 0, UNKNOWN: 1, ON: 2}
            print(json.dumps({"off": n[OFF], "on": n[ON], "unknown": n[UNKNOWN], "supervisor_started": c.supervisor_up, "warnings": warn,
                              "features": sorted(rows, key=lambda r: order[r["state"]])}, indent=1))
        else:
            sys.stdout.write(text)
    except Exception as e:                                      # exit code 0 always: this runs from the supervisor
        print(f"feature_state: failed: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
