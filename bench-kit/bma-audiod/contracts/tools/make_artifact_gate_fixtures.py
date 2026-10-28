#!/usr/bin/env python3
"""Regenerate contracts/fixtures/artifact_gate_report.json by running the REAL tools/artifact_replay.py on the synthetic snapshot of
tools/artifact_synth.py (invented data: a made-up day, no real timestamp, temperature or label count):

    python -I make_artifact_gate_fixtures.py [out_dir]      (default: ../fixtures)

The run is the one with a sweep of 3, 5 and 12 C over the whole snapshot (valid: not degraded). Standard library only."""

import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.abspath(os.path.join(HERE, "..", "..", "tools"))
sys.path.insert(0, TOOLS)
import artifact_replay as R  # noqa: E402
import artifact_synth as S  # noqa: E402

FILE_NAME = "artifact_gate_report.json"


def build(out_dir):
    work = tempfile.mkdtemp(prefix="agfix_")
    try:
        snap, rep = os.path.join(work, "snap"), os.path.join(work, "rep")
        S.build(snap)
        rc = R.main(["--data-dir", snap, "--out", rep, "--since", "0", "--sweep", "3,5,12"])
        if rc != 0:
            raise SystemExit("artifact_replay.py failed on the synthetic snapshot (exit %d)" % rc)
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, FILE_NAME)
        with open(os.path.join(rep, "artifact_replay.json"), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        return dst
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print("wrote", build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "fixtures")))
