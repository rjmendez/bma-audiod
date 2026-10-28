#!/usr/bin/env python3
"""Tests for pc/setup_tls.ps1 (Tailscale certificate install and renewal). The REAL script runs in Windows PowerShell against a scratch data
dir; only the `tailscale` executable is replaced by a small .cmd that copies prepared certificates (made by OpenSSL) and logs its calls.
Real tailscale is never invoked (-Tailscale is always given). Certificate material stays in the scratch dir.

    python test_setup_tls.py     (PASS/FAIL lines, a final 'N checks, 0 failure(s)' line; exit 77 + SKIP when powershell.exe or openssl is missing)"""

import datetime, json, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "setup_tls.ps1")
NAME = "node.tailnet.example"
fails, n = [], 0


def check(name, ok, detail=""):
    global n
    n += 1
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


PS = shutil.which("powershell")
OSSL = shutil.which("openssl") or next(
    (c for c in (os.path.join(os.path.dirname(os.path.dirname(shutil.which("git") or "")), "usr", "bin", "openssl.exe"),) if os.path.isfile(c)), None
)
if not PS or not OSSL or os.name != "nt":
    print("SKIP: needs Windows PowerShell and openssl (%s, %s); nothing ran, this is not a pass" % (PS, OSSL))
    sys.exit(77)


def make_cert(d, label, name, days):
    crt, key = os.path.join(d, label + ".crt"), os.path.join(d, label + ".key")
    subprocess.run(
        [
            OSSL,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            key,
            "-out",
            crt,
            "-days",
            str(days),
            "-subj",
            "/CN=" + name,
            "-addext",
            "subjectAltName=DNS:" + name,
        ],
        check=True,
        capture_output=True,
    )
    return crt, key


def fake_tailscale(d):
    cmd = os.path.join(d, "fake_tailscale.cmd")
    lines = [
        "@echo off",
        'echo %*>> "@D@\\calls.log"',
        'if "%1"=="status" goto status',
        'set /p mode=<"@D@\\mode.txt"',
        'if "%mode%"=="fail" goto fail',
        'copy /y "@D@\\%mode%.crt" %3 >nul',
        'copy /y "@D@\\%mode%.key" %5 >nul',
        "exit /b 0",
        ":fail",
        "echo boom: not allowed 1>&2",
        "exit /b 1",
        ":status",
        'echo {"Self":{"DNSName":"@N@."}}',
        "exit /b 0",
    ]
    open(cmd, "w", newline="").write("\r\n".join(lines).replace("@D@", d).replace("@N@", NAME) + "\r\n")
    return cmd


def setup(root, ts, *extra):
    p = subprocess.run(
        [PS, "-NoProfile", "-NonInteractive", "-File", SCRIPT, "-Root", root, "-Tailscale", ts, *extra], capture_output=True, text=True, timeout=180
    )
    return p.returncode, (p.stdout + p.stderr).strip()


def calls(d):
    p = os.path.join(d, "calls.log")
    return [l for l in open(p).read().splitlines() if l.strip()] if os.path.exists(p) else []


def cert_calls(d):
    return [c for c in calls(d) if c.startswith("cert ")]


def read(p):
    return open(p, "rb").read()


def acl(path):
    ps = (
        "$a = (New-Object System.IO.FileInfo '%s').GetAccessControl(); if (Test-Path -LiteralPath '%s' -PathType Container) { $a = (New-Object System.IO.DirectoryInfo '%s').GetAccessControl() }; [pscustomobject]@{protected = $a.AreAccessRulesProtected; ids = @($a.Access | ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)} | ConvertTo-Json -Compress"
        % ((path.replace("'", "''"),) * 3)
    )
    p = subprocess.run([PS, "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout)


def me():
    return subprocess.run(
        [PS, "-NoProfile", "-Command", "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name"], capture_output=True, text=True
    ).stdout.strip()


def main():
    work = tempfile.mkdtemp(prefix="bma_tls_")
    try:
        fx, root = os.path.join(work, "fx"), os.path.join(work, "root")
        os.makedirs(fx)
        os.makedirs(root)
        good = make_cert(fx, "good", NAME, 80)
        soon = make_cert(fx, "soon", NAME, 10)
        other = make_cert(fx, "wrongname", "other.tailnet.example", 80)
        old = make_cert(fx, "almost", NAME, 1)
        ts = fake_tailscale(fx)
        mode = lambda m: open(os.path.join(fx, "mode.txt"), "w").write(m)
        tls = os.path.join(root, "tls")
        crt, key = os.path.join(tls, "dashboard_cert.pem"), os.path.join(tls, "dashboard_key.pem")
        user = me()

        mode("good")
        rc, out = setup(root, ts, "-CheckOnly", "-Name", NAME)
        check(
            "-CheckOnly with nothing installed: exit 3 naming the action, nothing written, tailscale not called",
            (rc, "needs action: install" in out, os.path.exists(tls), calls(fx)) == (3, True, False, []),
            f"{rc} {out}",
        )
        rc, out = setup(root, ts, "-Name", NAME)
        want_date = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=80)).date()
        m = re.search(r"valid until (\d{4}-\d{2}-\d{2}) \(install\); restart the dashboard to use it$", out)
        check(
            "first run installs: exit 10 and one message with the node name, the directory, the expiry date (80 days) and 'restart the dashboard'",
            rc == 10
            and out.startswith("setup_tls: certificate for %s installed in %s, valid until " % (NAME, tls))
            and m is not None
            and abs((datetime.date.fromisoformat(m.group(1)) - want_date).days) <= 1,
            f"{rc} {out}",
        )
        check(
            "the installed files are byte for byte what tailscale produced, and the name is remembered",
            (read(crt), read(key), read(os.path.join(tls, "tls_name.txt"))) == (read(good[0]), read(good[1]), NAME.encode()),
        )
        check(
            "nothing secret reaches the console: no 'PRIVATE KEY' and no key line in the output",
            "PRIVATE KEY" not in out and read(good[1]).splitlines()[1].decode() not in out,
        )
        check("the scratch directory is gone", not os.path.exists(os.path.join(tls, "new")))
        for what, p in (("tls directory", tls), ("private key", key), ("certificate", crt)):
            a = acl(p)
            check(
                "ACL of the %s: inheritance removed and ONLY the current user has access (%s)" % (what, user),
                a["protected"] is True and [i.lower() for i in a["ids"]] == [user.lower()],
                str(a),
            )
        check(
            "tailscale was asked exactly once, for this name, with files in the restricted scratch dir",
            len(cert_calls(fx)) == 1 and cert_calls(fx)[0].endswith(" " + NAME) and ("--cert-file " + os.path.join(tls, "new")) in cert_calls(fx)[0],
            str(calls(fx)),
        )

        rc, out = setup(root, ts)
        check(
            "second run (no -Name: remembered) is a no-op: exit 0, 'is current', tailscale NOT asked again, files unchanged",
            (rc, "is current" in out, len(cert_calls(fx)), read(crt)) == (0, True, 1, read(good[0])),
            f"{rc} {out}",
        )
        rc, out = setup(root, ts, "-CheckOnly")
        check("-CheckOnly on a current certificate: exit 0", rc == 0 and "is current" in out, f"{rc} {out}")

        shutil.copy(soon[0], crt)  # an installed certificate with 10 days left (< 30)
        shutil.copy(soon[1], key)
        rc, out = setup(root, ts, "-CheckOnly")
        check(
            "-CheckOnly with 10 days left: exit 3 (renewal due) and nothing asked",
            (rc, "needs action: renew" in out, len(cert_calls(fx))) == (3, True, 1),
            f"{rc} {out}",
        )
        rc, out = setup(root, ts, "-RenewBeforeDays", "5")
        check("positive twin: the same certificate with -RenewBeforeDays 5 is still fine (exit 0, no call)", (rc, len(cert_calls(fx))) == (0, 1), f"{rc} {out}")
        rc, out = setup(root, ts)
        check(
            "10 days left: renewed (exit 10, '(renew)') and the new files are the 80 day certificate",
            (rc, "(renew)" in out, read(crt), read(key), len(cert_calls(fx))) == (10, True, read(good[0]), read(good[1]), 2),
            f"{rc} {out}",
        )
        rc, out = setup(root, ts, "-Force")
        check("-Force renews a valid certificate (exit 10, third call)", (rc, len(cert_calls(fx))) == (10, 3), f"{rc} {out}")

        shutil.copy(other[0], crt)  # a valid (80 day) certificate, but for ANOTHER name: must be replaced, not kept
        shutil.copy(other[1], key)
        rc, out = setup(root, ts)
        check(
            "an installed certificate for another name is renewed even though it is far from expiry (exit 10, '(renew)', the right one installed)",
            (rc, "(renew)" in out, read(crt), read(key)) == (10, True, read(good[0]), read(good[1])),
            f"{rc} {out}",
        )

        # failures leave the installed certificate alone
        shutil.copy(soon[0], crt)
        shutil.copy(soon[1], key)
        before = (read(crt), read(key))
        for label, md, why in (
            ("tailscale cert fails", "fail", "FAILED: tailscale cert failed (exit 1): boom: not allowed"),
            ("the certificate is for another name", "wrongname", "is not for " + NAME),
            ("the certificate is almost expired", "almost", "already (almost) expired"),
        ):
            mode(md)
            rc, out = setup(root, ts)
            check(
                "%s: exit 1 with the reason, the installed certificate and key are untouched, no scratch left" % label,
                rc == 1 and why in out and (read(crt), read(key)) == before and not os.path.exists(os.path.join(tls, "new")),
                f"{rc} {out}",
            )
        mode("good")
        rc, out = setup(root, ts)
        check("positive twin: with a good certificate available the same installation renews (exit 10)", rc == 10 and read(crt) == read(good[0]), f"{rc} {out}")

        rc, out = setup(root, os.path.join(fx, "no_such_tailscale.exe"), "-Name", NAME, "-Force")
        check(
            "no tailscale executable: exit 2 and the installed files are untouched",
            (rc, "tailscale was not found" in out, read(crt)) == (2, True, read(good[0])),
            f"{rc} {out}",
        )
        rc, out = setup(root, ts, "-Name", "bad name")
        check("an invalid node name: exit 2", (rc, "no usable node name" in out) == (2, True), f"{rc} {out}")
        r2 = os.path.join(work, "root2")
        os.makedirs(r2)
        rc, out = setup(r2, ts)
        check(
            "with no -Name and nothing remembered the name comes from `tailscale status` (the fake says %s) and the install works" % NAME,
            rc == 10 and ("certificate for %s installed" % NAME) in out,
            f"{rc} {out}",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


main()
print(f"\n{n} checks, {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
