#!/usr/bin/env python3
"""Mutation check for pc/protect_root.ps1: each mutation edits one behaviour in a COPY of pc/ and test_protect_root.py must then exit 1.
The unmutated copy must exit 0 first.   python mutation_protect_root.py [name-substring]"""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    ("inheritance is kept", "$acl.SetAccessRuleProtection($true, $false)", "$acl.SetAccessRuleProtection($false, $false)"),
    ("existing entries are kept", "foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }", ""),
    (
        "SYSTEM is not granted",
        "(New-Object System.Security.Principal.SecurityIdentifier ([System.Security.Principal.WellKnownSidType]::LocalSystemSid), $null),",
        "",
    ),
    (
        "Administrators are not granted",
        "    (New-Object System.Security.Principal.SecurityIdentifier ([System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid), $null)\n",
        "    $me\n",
    ),
    ("the owner is not granted", "$principals = @(\n    $me,", "$principals = @(\n    $null,"),
    ("the root itself is skipped", "Add-Target $Root '.'\n", ""),
    ("app is skipped", "if (Test-Path -LiteralPath $app -PathType Container) { Add-Target $app 'app' }", ""),
    (
        "the markers are skipped",
        "Get-ChildItem -LiteralPath $Root -File -Filter 'enable_*.txt'",
        "Get-ChildItem -LiteralPath $Root -File -Filter 'enable_none.txt'",
    ),
    ("analyzer_args.txt is skipped", "foreach ($f in 'run_pipeline.ps1', 'analyzer_args.txt') {", "foreach ($f in 'run_pipeline.ps1') {"),
    (
        "directories do not pass the entries down",
        "[System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' }",
        "[System.Security.AccessControl.InheritanceFlags]::None }",
    ),
    (
        "target names are cut out of the full path",
        'Write-Output "protected $($t.Name)"',
        "Write-Output \"protected $($t.Path.Substring($Root.Length).TrimStart(''))\"",
    ),
    (
        "a missing root is not refused",
        'if (-not (Test-Path -LiteralPath $Root -PathType Container)) { [Console]::Error.WriteLine("Root is not a directory"); exit 2 }',
        "",
    ),
]


def run(d):
    p = subprocess.run([sys.executable, os.path.join(d, "test_protect_root.py")], capture_output=True, text=True, timeout=900)
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_pr_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        os.makedirs(base)
        for f in ("protect_root.ps1", "test_protect_root.py", "install_task.ps1"):
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
            p = os.path.join(d, "protect_root.ps1")
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
