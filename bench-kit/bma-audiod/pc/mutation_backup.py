#!/usr/bin/env python3
"""Mutation check for pc/backup_data.ps1: each mutation edits one behaviour in a COPY of pc/ and test_backup.py must then exit 1.
The unmutated copy must exit 0 first.   python mutation_backup.py [name-substring]"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    ("audio flac is copied", "$_.Extension -in '.json', '.png'", "$_.Extension -in '.json', '.png', '.flac'"),
    ("audio wav is copied", "$_.Extension -in '.json', '.png'", "$_.Extension -in '.json', '.png', '.wav'"),
    ("the dashboard token is copied", "Add-If (Join-Path $Root 'analyzer_args.txt')", "Add-If (Join-Path $Root 'dashboard_token.txt')"),
    ("bma.local.ps1 is copied", "Add-If (Join-Path $Root 'analyzer_args.txt')", "Add-If (Join-Path $Root 'bma.local.ps1')"),
    ("labels are not copied", r"Add-If (Join-Path $Root 'labels\labels.jsonl')", ""),
    ("the clip index is not copied", "Add-If (Join-Path $c 'index.jsonl')", ""),
    ("sidecars are not copied", "'.json', '.png'", "'.none'"),
    ("retention keeps one day too few", "-($Keep - 1)", "-($Keep - 2)"),
    ("retention keeps one day too many", "-($Keep - 1)", "-$Keep"),
    ("retention removes nothing", "if ($dd -lt $oldest) {", "if ($false) {"),
    ("retention removes everything dated", "if ($dd -lt $oldest) {", "if ($true) {"),
    ("retention removes later-dated folders too", "if ($dd -lt $oldest) {", "if ($dd -lt $oldest -or $dd -gt $dayDate) {"),
    ("history json files are not copied", "$_.Extension -in '.jsonl', '.json'", "$_.Extension -in '.jsonl'"),
    ("the novelty state is not copied", "Add-If (Join-Path $Root 'analysis\\novelty_state.pkl')", ""),
    ("labels-only copies everything", "if (-not $LabelsOnly) {", "if ($true) {"),
    ("labels-only runs the retention", "if ($LabelsOnly) {\n    Write-Output", "if ($false) {\n    Write-Output"),
    ("the marker is not written", "if ($failed -eq 0) {\n    try { New-Item", "if ($false) {\n    try { New-Item"),
    ("the marker is written after a failed copy", "if ($failed -eq 0) {\n    try { New-Item", "if ($true) {\n    try { New-Item"),
    ("a failed copy is not counted", "} catch { $failed++ }\n}\n\nif ($LabelsOnly)", "} catch { }\n}\n\nif ($LabelsOnly)"),
]


def run(d):
    p = subprocess.run([sys.executable, os.path.join(d, "test_backup.py")], capture_output=True, text=True, timeout=900)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_bk_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        os.makedirs(base)
        for f in ("backup_data.ps1", "test_backup.py"):
            shutil.copy(os.path.join(HERE, f), base)
        rc, _ = run(base)
        print("baseline exit %d (must be 0)" % rc)
        if rc != 0:
            return 1
        for name, a, b in M:
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, "backup_data.ps1")
            s = open(p, newline="", encoding="utf-8").read().replace("\r\n", "\n")
            if s.count(a) != 1:
                print("BROKEN mutation (pattern found %d times): %s" % (s.count(a), name))
                bad += 1
                continue
            open(p, "w", newline="", encoding="utf-8").write(s.replace(a, b))
            rc, fl = run(d)
            print(("RED  " if rc == 1 else "NOT RED (exit %d) " % rc) + name + ("  <- " + fl[0][:110] if fl else ""))
            bad += rc != 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
