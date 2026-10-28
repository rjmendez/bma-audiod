#!/usr/bin/env python3
"""Tests for pc/protect_root.ps1: the REAL script and the real icacls / ACL APIs against a scratch data dir (never the live root).

python test_protect_root.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 77 + SKIP when powershell.exe is missing)"""

import json, os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "protect_root.ps1")
fails, n = [], 0
SYSTEM_SID, ADMINS_SID, AUTH_USERS_SID = "S-1-5-18", "S-1-5-32-544", "S-1-5-11"


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


PS = shutil.which("powershell")
if not PS or os.name != "nt":
    print("SKIP: needs Windows PowerShell (%s); nothing ran, this is not a pass" % PS)
    sys.exit(77)


def ps(cmd):
    p = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=120)
    return p.stdout.strip()


def acl(path):
    """-> {protected, sids (sorted unique), rights (every rule's FileSystemRights text), inherited (any rule inherited)}."""
    q = path.replace("'", "''")
    out = ps(
        "$a = (New-Object System.IO.FileInfo '%s').GetAccessControl(); if (Test-Path -LiteralPath '%s' -PathType Container) { $a = (New-Object System.IO.DirectoryInfo '%s').GetAccessControl() }; [pscustomobject]@{protected = $a.AreAccessRulesProtected; "
        "sids = @($a.Access | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique); "
        "rights = @($a.Access | ForEach-Object { [string]$_.FileSystemRights } | Sort-Object -Unique); "
        "inherited = [bool](@($a.Access | Where-Object { $_.IsInherited }).Count)} | ConvertTo-Json -Compress" % (q, q, q)
    )
    return json.loads(out)


def run(root):
    r = subprocess.run([PS, "-NoProfile", "-File", SCRIPT, "-Root", root], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip().replace("\r\n", "\n"), r.stderr.strip()


def put(root, rel, data=b"x"):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def main():
    me = ps("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
    want = sorted({me, SYSTEM_SID, ADMINS_SID})
    work = tempfile.mkdtemp(prefix="bma_prot_")
    try:
        root = os.path.join(work, "root")
        put(root, "app/ui/auth.py", b"AUTH")
        put(root, "run_pipeline.ps1", b"P")
        put(root, "analyzer_args.txt", b"--x\n")
        put(root, "enable_clip_audio.txt", b"")
        put(root, "enable_passkeys.txt", b"")
        put(root, "enable_ui_next.txt", b"")
        put(root, "notes.txt", b"n")
        # the audit finding: Authenticated Users hold Modify on the root and so on everything below it
        subprocess.run(["icacls.exe", root, "/grant", "*%s:(OI)(CI)(M)" % AUTH_USERS_SID], capture_output=True, check=True, timeout=60)
        subprocess.run(
            ["icacls.exe", os.path.join(root, "enable_clip_audio.txt"), "/grant", "*%s:(M)" % AUTH_USERS_SID], capture_output=True, check=True, timeout=60
        )
        before = acl(os.path.join(root, "app", "ui", "auth.py"))
        check("before: Authenticated Users can modify app\\ui\\auth.py (the audit finding is reproduced)", AUTH_USERS_SID in before["sids"], before)
        before_marker = acl(os.path.join(root, "enable_clip_audio.txt"))
        check("before: an explicit entry for Authenticated Users sits on the enable marker", AUTH_USERS_SID in before_marker["sids"], before_marker)

        rc, out, err = run(root)
        want_out = chr(10).join(
            [
                "protected .",
                "protected app",
                "protected run_pipeline.ps1",
                "protected analyzer_args.txt",
                "protected enable_clip_audio.txt",
                "protected enable_passkeys.txt",
                "protected enable_ui_next.txt",
                "protect_root: 7 targets, 0 failed",
            ]
        )
        check(
            "exit 0 and exactly one line per target in order (root, app, run_pipeline.ps1, analyzer_args.txt, the three markers), then the count",
            (rc, err, out) == (0, "", want_out),
            (rc, out, err),
        )
        for rel in (".", "app", "run_pipeline.ps1", "analyzer_args.txt", "enable_clip_audio.txt", "enable_passkeys.txt", "enable_ui_next.txt"):
            p = root if rel == "." else os.path.join(root, rel)
            a = acl(p)
            check(
                "%s: inheritance removed and exactly owner + SYSTEM + Administrators remain (no Authenticated Users)" % rel,
                a["protected"] is True and a["sids"] == want and not a["inherited"],
                a,
            )
        a = acl(os.path.join(root, "app", "ui", "auth.py"))
        check("app\\ui\\auth.py (never named) inherits exactly the same list, nothing for Authenticated Users", a["sids"] == want and a["inherited"] is True, a)
        a = acl(os.path.join(root, "notes.txt"))
        check("a file in the root that is not named inherits the same list", a["sids"] == want, a)
        newf = put(root, "app/new_module.py", b"N")
        a = acl(newf)
        check("a file created after the run inherits the same list", a["sids"] == want, a)
        a = acl(root)
        check("every entry on the root is Full control (owner, SYSTEM, Administrators)", a["rights"] == ["FullControl"], a)
        check(
            "the owner can still read and write: auth.py is intact and a new file was created",
            open(os.path.join(root, "app", "ui", "auth.py"), "rb").read() == b"AUTH" and os.path.exists(newf),
        )

        rc2, out2, err2 = run(root)
        check(
            "a second run is the same: exit 0, same output, same list (idempotent)",
            (rc2, err2) == (0, "") and out2 == out and acl(root)["sids"] == want,
            (rc2, out2, err2),
        )

        # a root given in 8.3 short form (the CI runner's temp dir): Resolve-Path keeps it, Get-ChildItem spells the marker paths long
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        short = buf.value if ctypes.windll.kernel32.GetShortPathNameW(root, buf, 1024) else root
        if short.lower() == root.lower():
            print("SKIP 8.3 root: no short name for the temp directory on this volume")
        else:
            rc5, out5, err5 = run(short)
            check("a root given as an 8.3 short path reports the same target names", (rc5, err5, out5) == (0, "", want_out), (rc5, out5, err5))

        # a root without the optional parts: only the root itself
        bare = os.path.join(work, "bare")
        os.makedirs(bare)
        rc3, out3, err3 = run(bare)
        check(
            "a bare root: only the root is protected (1 target)",
            (rc3, err3, out3) == (0, "", "protected .\nprotect_root: 1 targets, 0 failed") and acl(bare)["sids"] == want,
            (rc3, out3, err3),
        )

        # files outside the root are untouched
        outside = put(work, "outside/keep.txt", b"k")
        before_o = acl(outside)
        run(root)
        check("a file outside the root keeps its list", acl(outside) == before_o, acl(outside))

        # install_task.ps1 pins the script and runs it against the root after the pin (commands found by the PowerShell parser)
        inst = os.path.join(HERE, "install_task.ps1").replace("'", "''")
        cmds = ps(
            "$e=$null;$t=$null;$a=[System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$t,[ref]$e);"
            "$a.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { '' + $_.Extent.StartLineNumber + ' ' + $_.Extent.Text }"
            % inst
        )
        lines = [l.split(" ", 1) for l in cmds.splitlines() if "protect_root.ps1" in l]
        copy_ln = [int(a_) for a_, b_ in lines if b_.startswith("Copy-Item") and "'pc\protect_root.ps1'" in b_ and b_.rstrip().endswith("-Force")]
        run_ln = [int(a_) for a_, b_ in lines if b_.startswith("& powershell.exe") and "-File (Join-Path $root 'protect_root.ps1') -Root $root" in b_]
        pin_ln = [int(l.split(" ", 1)[0]) for l in cmds.splitlines() if l.split(" ", 1)[1].startswith("Write-Host") and '"pinned: $ver"' in l]
        check(
            "install_task.ps1 copies protect_root.ps1 to the root and runs it with -Root $root once, after the pin",
            len(copy_ln) == 1 and len(run_ln) == 1 and len(pin_ln) == 1 and copy_ln[0] < pin_ln[0] < run_ln[0],
            (copy_ln, run_ln, pin_ln, lines),
        )
        rc4, _, err4 = run(os.path.join(work, "nope"))
        check("a missing root is exit 2 with its message", (rc4, err4) == (2, "Root is not a directory"), (rc4, err4))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d checks, %d failure(s)" % (n, len(fails)))
    return 1 if fails else 0


sys.exit(main())
