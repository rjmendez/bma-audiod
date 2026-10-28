#!/usr/bin/env python3
"""Mutation check for the dashboard TLS / sign-in parts of pc/run_pipeline.ps1: each mutation edits one behaviour in a COPY of pc/, runs
test_supervisor_config.ps1 (Windows PowerShell) against it and requires exit 1. The unmutated copy must exit 0 first.

    python mutation_supervisor.py [name-substring]        Exit 0 only if the baseline is green and every mutation was caught."""

import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
M = [
    (
        "the clip-audio marker is ignored",
        "if (-not (Test-Path -LiteralPath (Join-Path $root 'enable_clip_audio.txt'))) { return '' }",
        "if ($true) { return '' }",
    ),
    ("analyzer_args.txt is ignored", "if (-not (Test-Path -LiteralPath $f)) { return @() }", "if ($true) { return @() }"),
    ("a Windows path in analyzer_args.txt is not converted", "            $v = $w\n", "            $v = $v\n"),
    ("the tls directory is ignored", "foreach ($dir in @((Join-Path $root 'tls'), $root)) {", "foreach ($dir in @($root)) {"),
    ("the older root pair is ignored", "foreach ($dir in @((Join-Path $root 'tls'), $root)) {", "foreach ($dir in @((Join-Path $root 'tls'))) {"),
    (
        "identity flags carry --require-identity",
        "'--tailnet-identity', '--allowed-users', (Q $au))",
        "'--tailnet-identity', '--allowed-users', (Q $au), '--require-identity')",
    ),
    ("a comment-only allowed_users.txt counts as a login", "Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') }", "Where-Object { $_.Trim() }"),
    ("the passkey origin without the port", "'--passkey-origin', \"https://${n}:$port\"", "'--passkey-origin', \"https://${n}\""),
    (
        "passkeys without the remembered name",
        "if ((Test-Path -LiteralPath (Join-Path $root 'enable_passkeys.txt')) -and (Test-Path -LiteralPath $nm)) {",
        "if (Test-Path -LiteralPath (Join-Path $root 'enable_passkeys.txt')) {",
    ),
    ("the ui-next marker is ignored", "if (Test-Path -LiteralPath (Join-Path $root 'enable_ui_next.txt')) { $extra += '--ui-next' }", ""),
    (
        "any exit code restarts the dashboard",
        "if ((Invoke-TlsRenewal $root) -ne 10) { return $false }",
        "if ((Invoke-TlsRenewal $root) -lt 0) { return $false }",
    ),
    (
        "renewal runs without setup_tls having installed anything",
        "if (-not ((Test-Path -LiteralPath (Join-Path (Join-Path $root 'tls') 'tls_name.txt')) -and (Test-Path -LiteralPath $script))) { return -1 }",
        "if (-not (Test-Path -LiteralPath $script)) { return -1 }",
    ),
    ("a failure is not logged", 'if ($p.ExitCode -ne 0 -and $line) { Log ("TLS: " + $line) }', "if ($false) { }"),
    (
        "a hung renewal is not stopped",
        "if (-not $p.WaitForExit(180000)) { try { $p.Kill() } catch { }; Log 'TLS renewal (setup_tls.ps1) took over 3 minutes and was stopped'; return 1 }",
        "",
    ),
    ("loss log: the delay never doubles", "[math]::Max(10, $PrevDelayS * 2)", "[math]::Max(10, $PrevDelayS)"),
    ("loss log: the delay is not capped at 300 s", "[math]::Min(300, [math]::Max(10", "[math]::Min(600, [math]::Max(10"),
    ("loss log: a long run does not reset the delay", "if ($RanS -ge 300) { return 10 }", "if ($RanS -ge 9999) { return 10 }"),
    ("loss log: the interval flag changes", "'--every-s', '30')", "'--every-s', '60')"),
    ("loss log: the exit is not written to the ledger", "Register-Exit 'loss_log' $l", "$null = $l"),
    ("feature state: runs without --write", "'--root', (Q $root), '--write')", "'--root', (Q $root))"),
    ("feature state: runs without the isolated flag", "-ArgumentList @('-I', (Q $tool), '--root'", "-ArgumentList @((Q $tool), '--root'"),
    ("feature state: runs although the tool is not deployed", "if (-not (Test-Path -LiteralPath $tool)) { return -1 }", ""),
    ("feature state: the count line is not logged", "if ($first) { Log ('features: ' + $first) }", ""),
    (
        "feature state: a hung run is not stopped",
        "try { $p.Kill() } catch { }; Log 'feature state (feature_state.py) took over 30 seconds and was stopped'; return 1",
        "return 1",
    ),
    ("feature state: rescheduled every hour instead of daily", "$featNext = (Get-MonoUtcNow).AddHours(24)", "$featNext = (Get-MonoUtcNow).AddHours(1)"),
    ("feature state: the first run is delayed", "$featNext = Get-MonoUtcNow  ", "$featNext = (Get-MonoUtcNow).AddHours(24)  "),
    ("feature state: never called from the loop", "        [void](Invoke-FeatureState $root)", "        $null = $root"),
    ("daily: due one hour late", "($Now.Hour -ge $Hour)", "($Now.Hour -gt $Hour)"),
    ("daily: runs more than once a day", " -and ($LastStart.Date -lt $Now.Date)", ""),
    ("daily: the backup hour changes", "hour = 3 }", "hour = 4 }"),
    ("daily: the digest hour changes", "hour = 6 }", "hour = 7 }"),
    ("daily: the digest reports today", "(Get-Date).AddDays(-1).ToString('yyyyMMdd'", "(Get-Date).AddDays(0).ToString('yyyyMMdd'"),
    (
        "daily: the backup is not given the data dir",
        "'-File', (Q (Join-Path $app 'backup_data.ps1')), '-Root', (Q $root))",
        "'-File', (Q (Join-Path $app 'backup_data.ps1')))",
    ),
    ("daily: the exit is not written to the ledger", "Register-Exit $job.n $p", "$null = $p"),
    ("restart delay: never doubles", "[math]::Max(30, $PrevDelayS * 2)", "[math]::Max(30, $PrevDelayS)"),
    ("restart delay: cap 30 min instead of 15", "[math]::Min(900, [math]::Max(30", "[math]::Min(1800, [math]::Max(30"),
    ("restart delay: a long run does not reset it", "if ($RanS -ge 300) { return 0 }", "if ($RanS -ge 99999) { return 0 }"),
    ("restart delay: the analyzer restarts at once", "$zDelay = Get-RestartDelay $zDelay (Get-RunSeconds $z)", "$zDelay = 0"),
    ("exit line: no uptime", "(Get-RunSeconds $p).ToString('0.0', $inv), $Extra)", "'0.0', $Extra)"),
    ("exit line: the bridge exit is not the new line", "Log (Get-ExitLine 'bridge' $b \"; restart in $bDelay s\")", 'Log "bridge exited"'),
    ("bridge: priority is not raised", "$p.PriorityClass = 'AboveNormal'", "$p.PriorityClass = 'Normal'"),
    ("health: the warning threshold is 5 %", "$LossPct -gt 2.0", "$LossPct -gt 5.0"),
    ("health: a stale status file still counts", "[double]$MaxAgeS = 90", "[double]$MaxAgeS = 9000000"),
    ("health: never written from the loop", "        Write-HealthLine $b", "        $null = $b"),
    ("rotation: the ui.err cap is 2 GiB", "'ui.err' = 2097152 }", "'ui.err' = 2097152000 }"),
    ("rotation: the analyzer log is not rotated at start", "    Rotate-ChildLogs @('analyzer.log')" + chr(10), ""),
    ("backup: the night is due on the in-memory time, not the marker", "{ Get-BackupLast $root }", "{ $bkLast }"),
    (
        "backup: the hourly run is not labels-only",
        "if ($LabelsOnly) { $a += '-LabelsOnly'; $n = 'backup_labels' }",
        "if ($LabelsOnly) { $n = 'backup_labels' }",
    ),
    ("backup: a failed night is retried every loop pass", "-lt 30) -and (Get-DailyDue", "-lt -1) -and (Get-DailyDue"),
]
# mutations of the test's own WSL skip logic (applied to test_supervisor_config.ps1, same exit-1 requirement)
T = [
    (
        "wsl probe: always true (a distro-less runner would run the WSL checks and fail, a stub answer is ignored)",
        'return ("$(@($r)[0])".Trim() -ceq $want)',
        "return $true",
    ),
    (
        "wsl probe: an exception from the wsl call counts as a working WSL",
        "try { $r = & $Invoke $Path } catch { return $false }",
        "try { $r = & $Invoke $Path } catch { return $true }",
    ),
    (
        "skip path: a skip is counted as a check (a vacuous pass)",
        "function Skip-Check([string]$name, [string]$why) { $script:skipped++;",
        "function Skip-Check([string]$name, [string]$why) { $script:skipped++; $script:n++;",
    ),
    (
        "skip path: a skip is not counted at all",
        "function Skip-Check([string]$name, [string]$why) { $script:skipped++;",
        "function Skip-Check([string]$name, [string]$why) {",
    ),
]


def run(d, w):
    p = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", os.path.join(d, "test_supervisor_config.ps1"), "-Work", w], capture_output=True, text=True, timeout=900
    )
    return p.returncode, [l for l in p.stdout.splitlines() if l.startswith("FAIL")][:2], [l for l in p.stdout.splitlines() if " checks, " in l]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tmp = tempfile.mkdtemp(prefix="bma_sup_mut_")
    bad = 0
    try:
        base = os.path.join(tmp, "base")
        shutil.copytree(HERE, base, ignore=shutil.ignore_patterns("__pycache__"))
        rc, _, summ = run(base, os.path.join(tmp, "w0"))
        print(f"baseline exit {rc} (must be 0) {summ}")
        if rc != 0:
            return 1
        for i, (name, a, b, fn) in enumerate([m + ("run_pipeline.ps1",) for m in M] + [m + ("test_supervisor_config.ps1",) for m in T]):
            if only and only not in name:
                continue
            d = os.path.join(tmp, "m")
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base, d)
            p = os.path.join(d, fn)
            s = open(p, newline="", encoding="utf-8").read()
            if s.count(a) != 1:
                print(f"BROKEN mutation (pattern found {s.count(a)} times): {name}")
                bad += 1
                continue
            open(p, "w", newline="", encoding="utf-8").write(s.replace(a, b))
            rc, fl, _ = run(d, os.path.join(tmp, "w%d" % (i + 1)))
            print(("RED  " if rc == 1 else "NOT RED (exit %d) " % rc) + name + ("  <- " + fl[0][:120] if fl else ""))
            bad += rc != 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


sys.exit(main())
