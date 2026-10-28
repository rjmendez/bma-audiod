#!/usr/bin/env python3
"""Every PowerShell file under pc/ must parse. A parse error (for example "$root:" inside a double-quoted string, which PowerShell reads as a
drive-qualified variable) makes the whole script refuse to run, and the other PowerShell tests only parse the parts they cut out.

    python test_ps1_parse.py        (exit 0 and a final 'N checks, 0 failure(s)' line; exits 77 with 'SKIP' when powershell.exe is missing)"""

import glob, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
fails, n = [], 0


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def parse_errors(path):
    """-> list of 'line N: message' from PowerShell's own parser (empty = parses). The path is embedded in the command text
    (powershell -Command appends extra arguments to the command line instead of binding them to $args), and any stderr
    output is an error of the checker itself, never an empty answer."""
    lit = path.replace("'", "''")
    ps = (
        "$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('" + lit + "',[ref]$t,[ref]$e);"
        "$e | ForEach-Object { 'line ' + $_.Extent.StartLineNumber + ': ' + $_.Message }"
    )
    r = subprocess.run([PS, "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or r.stderr.strip():
        raise RuntimeError("the PowerShell parse helper failed: rc=%s stderr=%s" % (r.returncode, r.stderr.strip()[:300]))
    return [x for x in r.stdout.splitlines() if x.strip()]


PS = shutil.which("powershell") or shutil.which("pwsh")
if not PS:
    print("SKIP: powershell not found")
    sys.exit(77)

files = sorted(glob.glob(os.path.join(HERE, "*.ps1")))
check("the pc/ directory has PowerShell files to check", len(files) >= 4, str(files))
for f in files:
    errs = parse_errors(f)
    check("parses: " + os.path.basename(f), errs == [], "; ".join(errs[:3]))

# the checker itself: a file with the bug class that broke install_task.ps1 is reported, a braced twin is not
import tempfile

with tempfile.TemporaryDirectory() as d:
    bad, good = os.path.join(d, "bad.ps1"), os.path.join(d, "good.ps1")
    open(bad, "w").write('$root = "x"\nWrite-Warning ("in $root: fallbacks")\n')
    open(good, "w").write('$root = "x"\nWrite-Warning ("in ${root}: fallbacks")\n')
    e_bad, e_good = parse_errors(bad), parse_errors(good)
    check("the checker reports the drive-qualified-variable bug (line 2)", len(e_bad) == 1 and e_bad[0].startswith("line 2:"), str(e_bad))
    check("positive twin: the braced form parses", e_good == [], str(e_good))

print(f"\n{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
