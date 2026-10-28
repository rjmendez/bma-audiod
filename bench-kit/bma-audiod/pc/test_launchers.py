#!/usr/bin/env python3
"""Tests for the two WSL/Linux launchers, analysis/run_analyzer.sh and pc/run_classifier.sh: where they put data and
models, which interpreter they run, and which optional flags they add. The REAL scripts run under real bash; only the
program they launch (host.py / bma_classify.py / the interpreter) is a RECORDING stand-in that writes its argv, its
working directory and the relevant environment to a file. HOME is a scratch directory, the UDP ports are unused test
ports (the scripts `pkill` by port).

Needs POSIX bash (run it in WSL or Linux):  python test_launchers.py"""

import json, os, shutil, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZER = os.path.join(HERE, "..", "analysis", "run_analyzer.sh")
CLASSIFIER = os.path.join(HERE, "run_classifier.sh")
APORT, CPORT = 48399, 48299

n_checks, fails = 0, []


def check(name, ok, detail=""):
    global n_checks
    n_checks += 1
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [" + detail + "]"))
    if not ok:
        fails.append(name)


RECORDER = """\
import json, os, sys
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(), "exe": sys.executable,
       "env": {k: os.environ.get(k) for k in ("BMA_DATA_DIR", "BMA_MODEL_DIR", "HF_HOME", "TMPDIR", "PYTHONPATH", "HOME")}}
with open(os.environ["REC"], "w") as f:
    json.dump(rec, f)
print("detection-line")
"""


def put(path, text, exe=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="\n") as f:
        f.write(text)
    if exe:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def launch(script_src, name, app_dir, prog, args, env_extra, cwd, home):
    """Copy the real launcher into app_dir with a recording `prog`; run it; return (record dict | None, completed)."""
    os.makedirs(app_dir, exist_ok=True)
    os.makedirs(home, exist_ok=True)
    shutil.copy(script_src, os.path.join(app_dir, name))
    put(os.path.join(app_dir, prog), RECORDER)
    rec = os.path.join(home, "rec.json")
    env = {"PATH": os.environ["PATH"], "HOME": home, "REC": rec}
    env.update(env_extra)
    r = subprocess.run(["bash", os.path.join(app_dir, name)] + args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    data = json.load(open(rec)) if os.path.exists(rec) else None
    return data, r


def val(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def run():
    py = sys.executable
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)

        # ---- analyzer, live layout: <data>/app/run_analyzer.sh, nothing set -> data dir is the parent of app/
        d = os.path.join(td, "live")
        app = os.path.join(d, "app")
        home = os.path.join(td, "home1")
        os.makedirs(home)
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py], {}, td, home)
        check("analyzer live layout: the launcher ran and the interpreter ran host.py", rec is not None, r.stderr[-300:])
        if rec:
            a = rec["argv"]
            check(
                "analyzer live layout: argv after the script name: listen, out, panns-dir (exact)",
                a == ["--listen", f"0.0.0.0:{APORT}", "--out", os.path.join(d, "analysis"), "--panns-dir", os.path.join(d, "models", "panns_data")],
                str(a),
            )
            check("analyzer live layout: working directory is the app dir", rec["cwd"] == app, rec["cwd"])
            check("analyzer live layout: BMA_DATA_DIR exported = the parent of app/", rec["env"]["BMA_DATA_DIR"] == d, str(rec["env"]))
            check("analyzer live layout: BMA_MODEL_DIR defaults to <data>/models", rec["env"]["BMA_MODEL_DIR"] == os.path.join(d, "models"))
            check(
                "analyzer live layout: HF_HOME and TMPDIR live under the model dir",
                (rec["env"]["HF_HOME"], rec["env"]["TMPDIR"]) == (os.path.join(d, "models", "hf"), os.path.join(d, "models", "tmp")),
                str(rec["env"]),
            )
            check("analyzer live layout: no overlay dir -> PYTHONPATH untouched", rec["env"]["PYTHONPATH"] is None, str(rec["env"]["PYTHONPATH"]))
            check(
                "analyzer live layout: no geo file -> no --geo-config; no tonal mask -> no --tonal-mask",
                "--geo-config" not in a and "--tonal-mask" not in a,
                str(a),
            )
            check(
                "analyzer live layout: HOME of the process is the model dir, so panns_inference finds <model>/panns_data",
                rec["env"]["HOME"] == os.path.join(d, "models"),
                str(rec["env"]["HOME"]),
            )
            check("analyzer live layout: nothing was written into the invoking user's home", os.listdir(home) == ["rec.json"], str(os.listdir(home)))
            check(
                "analyzer live layout: the stdout went to <data>/logs/analyzer.log", open(os.path.join(d, "logs", "analyzer.log")).read() == "detection-line\n"
            )

        # ---- analyzer with the optional files present (positive twin of the 'absent' checks above)
        put(os.path.join(app, "geo_county.json"), "{}")
        put(os.path.join(app, "tonal_mask.json"), "{}")
        os.makedirs(os.path.join(d, "models", "overlay"))
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py], {}, td, home)
        if rec:
            a = rec["argv"]
            check(
                "analyzer with geo + mask files: --geo-config and --tonal-mask carry the app copies",
                val(a, "--geo-config") == os.path.join(app, "geo_county.json") and val(a, "--tonal-mask") == os.path.join(app, "tonal_mask.json"),
                str(a),
            )
            check(
                "analyzer with an overlay dir: PYTHONPATH is that dir",
                rec["env"]["PYTHONPATH"] == os.path.join(d, "models", "overlay"),
                str(rec["env"]["PYTHONPATH"]),
            )
        else:
            check("analyzer with optional files ran", False, r.stderr[-300:])
        check(
            "analyzer with geo + mask files but no mixscore.json: no --mixscore, no --mixscore-every (the opt-in plugin stays off)",
            rec is not None and "--mixscore" not in rec["argv"] and "--mixscore-every" not in rec["argv"],
            str(rec and rec["argv"]),
        )

        # ---- the opt-in mixscore plugin: <app>/mixscore.json present = --mixscore, an "every" value becomes --mixscore-every
        put(os.path.join(app, "mixscore.json"), "{}\n")
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py], {}, td, home)
        check(
            "analyzer with mixscore.json '{}': --mixscore once, no --mixscore-every (host default 4 applies)",
            rec is not None and rec["argv"].count("--mixscore") == 1 and "--mixscore-every" not in rec["argv"],
            str(rec and rec["argv"]) + r.stderr[-200:],
        )
        put(os.path.join(app, "mixscore.json"), '{\r\n  "every": 2\r\n}\r\n')
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py], {}, td, home)
        check(
            'analyzer with mixscore.json {"every": 2} (CRLF, multi-line): --mixscore and --mixscore-every 2 (exact)',
            rec is not None
            and rec["argv"].count("--mixscore") == 1
            and val(rec["argv"], "--mixscore-every") == "2"
            and rec["argv"].count("--mixscore-every") == 1,
            str(rec and rec["argv"]) + r.stderr[-200:],
        )
        os.remove(os.path.join(app, "mixscore.json"))
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py], {}, td, home)
        check("analyzer after deleting mixscore.json: --mixscore is gone again", rec is not None and "--mixscore" not in rec["argv"], str(rec and rec["argv"]))

        # ---- analyzer, repo layout (script not in a dir named app): BMA_DATA_DIR and BMA_MODEL_DIR decide
        repo_app = os.path.join(td, "repo", "analysis")
        data = os.path.join(td, "elsewhere", "d")
        models = os.path.join(td, "elsewhere", "m")
        rec, r = launch(
            ANALYZER, "run_analyzer.sh", repo_app, "host.py", [str(APORT), py], {"BMA_DATA_DIR": data, "BMA_MODEL_DIR": models}, td, os.path.join(td, "home2")
        )
        check(
            "analyzer with BMA_DATA_DIR/BMA_MODEL_DIR: out under the data dir, panns-dir under the model dir",
            rec is not None
            and val(rec["argv"], "--out") == os.path.join(data, "analysis")
            and val(rec["argv"], "--panns-dir") == os.path.join(models, "panns_data"),
            str(rec and rec["argv"]) + r.stderr[-200:],
        )
        check("analyzer with BMA_MODEL_DIR: HF_HOME is <model>/hf", rec is not None and rec["env"]["HF_HOME"] == os.path.join(models, "hf"))
        check(
            "analyzer with BMA_MODEL_DIR: HOME is that model dir (positive twin of the live-layout HOME check)",
            rec is not None and rec["env"]["HOME"] == models,
            str(rec and rec["env"]["HOME"]),
        )

        # ---- analyzer, repo layout, nothing set: ./data relative to the working directory
        work = os.path.join(td, "work")
        os.makedirs(work)
        rec, r = launch(ANALYZER, "run_analyzer.sh", os.path.join(td, "repo2", "analysis"), "host.py", [str(APORT), py], {}, work, os.path.join(td, "home3"))
        check(
            "analyzer repo layout, nothing set: out is <cwd>/data/analysis",
            rec is not None and val(rec["argv"], "--out") == os.path.join(work, "data", "analysis"),
            str(rec and rec["argv"]) + r.stderr[-200:],
        )

        # ---- the optional beam shift: third positional argument -> --beam-shift, absent otherwise
        check(
            "analyzer without a beam-shift argument: no --beam-shift (the runs above already passed python only)",
            rec is not None and "--beam-shift" not in rec["argv"],
            str(rec and rec["argv"]),
        )
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py, "1,0,0,0,0,0,0,-1"], {}, td, home)
        check(
            "analyzer with a beam-shift argument: --beam-shift carries it verbatim, once",
            rec is not None and val(rec["argv"], "--beam-shift") == "1,0,0,0,0,0,0,-1" and rec["argv"].count("--beam-shift") == 1,
            str(rec and rec["argv"]) + r.stderr[-200:],
        )
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py, ""], {}, td, home)
        check(
            "analyzer with an empty beam-shift argument: no --beam-shift (empty = unset)",
            rec is not None and "--beam-shift" not in rec["argv"],
            str(rec and rec["argv"]),
        )

        # ---- the 4th positional (clip-audio) and the extra arguments after it
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py, "", "1"], {}, td, home)
        check(
            "analyzer with 4th argument 1: --clip-audio once, no --beam-shift",
            rec is not None and rec["argv"].count("--clip-audio") == 1 and "--beam-shift" not in rec["argv"],
            str(rec and rec["argv"]) + r.stderr[-200:],
        )
        for v4 in ("0", "", "yes"):
            rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py, "", v4], {}, td, home)
            check(
                f"analyzer with 4th argument {v4!r}: no --clip-audio (only 1 turns it on)",
                rec is not None and "--clip-audio" not in rec["argv"],
                str(rec and rec["argv"]),
            )
        rec, r = launch(
            ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py, "1,0,0,0,0,0,0,-1", "1", "--perch-model", "/m d/x.onnx", "--label=a b"], {}, td, home
        )
        a_ = rec["argv"] if rec else []
        tail = a_[a_.index("--beam-shift") :] if "--beam-shift" in a_ else []
        check(
            "analyzer extra arguments: appended verbatim (spaces kept) after --beam-shift and --clip-audio, in order",
            tail[:4] == ["--beam-shift", "1,0,0,0,0,0,0,-1", "--clip-audio", "--perch-model"] and tail[4:] == ["/m d/x.onnx", "--label=a b"],
            str(a_) + r.stderr[-200:],
        )
        rec0, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py], {}, td, home)
        rec, r = launch(ANALYZER, "run_analyzer.sh", app, "host.py", [str(APORT), py, "", ""], {}, td, home)
        check(
            "analyzer with empty 3rd and 4th arguments and no extras: the host.py command line equals the one with python only",
            rec is not None and rec0 is not None and rec["argv"] == rec0["argv"],
            str(rec and rec["argv"]),
        )

        # ---- default interpreter is python3 on PATH
        bindir = os.path.join(td, "bin")
        marker = os.path.join(td, "shim_used")
        put(os.path.join(bindir, "python3"), "#!/bin/sh\ntouch " + marker + "\nexec " + py + ' "$@"\n', exe=True)
        rec, r = launch(
            ANALYZER,
            "run_analyzer.sh",
            os.path.join(td, "repo3", "analysis"),
            "host.py",
            [str(APORT)],
            {"PATH": bindir + os.pathsep + os.environ["PATH"], "BMA_DATA_DIR": os.path.join(td, "d3")},
            td,
            os.path.join(td, "home4"),
        )
        check(
            "analyzer without a python argument runs python3 from PATH (the recording shim)",
            rec is not None and rec["argv"][0] == "--listen" and os.path.exists(marker),
            str(rec) + r.stderr[-200:],
        )

        # ---- classifier
        cd = os.path.join(td, "clive")
        capp = os.path.join(cd, "app")
        rec, r = launch(CLASSIFIER, "run_classifier.sh", capp, "bma_classify.py", [str(CPORT), py], {}, td, os.path.join(td, "home5"))
        check(
            "classifier live layout: argv after the script name is udp/slot/min-conf (exact, no geo flag)",
            rec is not None and rec["argv"] == ["--udp", f"0.0.0.0:{CPORT}", "--slot", "0", "--min-conf", "0.25"],
            str(rec and rec["argv"]) + r.stderr[-200:],
        )
        check(
            "classifier live layout: detections went to <data>/logs/detections.jsonl",
            os.path.exists(os.path.join(cd, "logs", "detections.jsonl")) and open(os.path.join(cd, "logs", "detections.jsonl")).read() == "detection-line\n",
        )
        put(os.path.join(capp, "geo_county.json"), "{}")
        rec, r = launch(CLASSIFIER, "run_classifier.sh", capp, "bma_classify.py", [str(CPORT), py], {}, td, os.path.join(td, "home5"))
        check(
            "classifier with geo file: --geo-config carries the app copy (positive twin)",
            rec is not None and val(rec["argv"], "--geo-config") == os.path.join(capp, "geo_county.json"),
            str(rec and rec["argv"]),
        )
        rec, r = launch(
            CLASSIFIER,
            "run_classifier.sh",
            os.path.join(td, "r4", "classify"),
            "bma_classify.py",
            [str(CPORT), py],
            {"BMA_DATA_DIR": os.path.join(td, "cd2")},
            td,
            os.path.join(td, "home6"),
        )
        check(
            "classifier with BMA_DATA_DIR: logs land under that dir",
            rec is not None and os.path.exists(os.path.join(td, "cd2", "logs", "detections.jsonl")),
            str(r.stderr[-200:]),
        )


if os.name == "nt" or not shutil.which("bash"):
    print("SKIPPED: needs POSIX bash (run this in WSL or Linux); 0 checks ran")
    sys.exit(0)
run()
print(f"\n{n_checks} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
