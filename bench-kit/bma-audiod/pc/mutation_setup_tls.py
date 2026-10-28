#!/usr/bin/env python3
"""Mutation check for pc/setup_tls.ps1: each mutation edits one behaviour in a COPY of pc/, runs test_setup_tls.py against it and
requires exit 1 (the suite must go red). The unmutated copy must exit 0 first.

    python mutation_setup_tls.py [name-substring]        Exit 0 only if the baseline is green and every mutation was caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    ("the directory is not restricted", "Protect-Directory $tlsDir\n", "\n"),
    (
        "no ACL protection at all (directory and files)",
        [
            "Protect-Directory $tlsDir\n",
            "Protect-Directory $scratch\n",
            "Protect-File $newCert; Protect-File $newKey\n",
            "Protect-File $keyFile; Protect-File $certFile; Protect-File $nameFile\n",
        ],
        "\n",
    ),
    ("renew when MORE than the threshold is left", "-lt $RenewBeforeDays) { return 'renew' }", "-gt $RenewBeforeDays) { return 'renew' }"),
    ("a certificate for another name is accepted", "if ((Get-CertNames $cert) -notcontains $Name) {", "if ($false) {"),
    ("an almost expired certificate is accepted", "if ($cert.NotAfter.ToUniversalTime() -le [datetime]::UtcNow.AddDays(1)) {", "if ($false) {"),
    ("a failing tailscale cert is ignored", "    if ($LASTEXITCODE -ne 0) {\n        $msg =", "    if ($false) {\n        $msg ="),
    ("-Force is ignored", "if ($Force) { $decision", "if ($false) { $decision"),
    ("the installed name is not compared", "if ($InstalledName -ne $WantName) { return 'renew' }", ""),
    (
        "the new files replace the old before they are validated",
        "    $cert = Read-PemCertificate $newCert\n",
        "    Copy-Item -LiteralPath $newCert -Destination $certFile -Force\n    $cert = Read-PemCertificate $newCert\n",
    ),
    ("-CheckOnly installs anyway", "if ($CheckOnly) { Say", "if ($false) { Say"),
]


def run(d):
    p = subprocess.run([sys.executable, "-I", os.path.join(d, "test_setup_tls.py")], capture_output=True, text=True, timeout=900)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_tls_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        shutil.copytree(HERE, base, ignore=shutil.ignore_patterns("__pycache__"))
        rc, _ = run(base)
        print(f"baseline exit {rc} (must be 0)")
        if rc != 0:
            return 1
        for name, a, b in M:
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, "setup_tls.ps1")
            s = open(p, newline="", encoding="utf-8").read()
            pats = a if isinstance(a, list) else [a]
            if any(s.count(x) != 1 for x in pats):
                print(f"BROKEN mutation (pattern counts {[s.count(x) for x in pats]}): {name}")
                bad += 1
                continue
            for x in pats:
                s = s.replace(x, b)
            open(p, "w", newline="", encoding="utf-8").write(s)
            rc, fl = run(d)
            print(("RED  " if rc == 1 else "NOT RED (exit %d) " % rc) + name + ("  <- " + fl[0][:120] if fl else ""))
            bad += rc != 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
