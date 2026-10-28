# Pester-free smoke test of run_ledger.ps1 (and of the Start-Process exit-code fact it exists for).
#   powershell.exe -NoProfile -File test_run_ledger.ps1 -Work <empty temp directory outside the repo>
# Exits 0 when every check passes, 1 otherwise. Never touches a live data dir: everything lives under -Work.
param([Parameter(Mandatory = $true)][string]$Work)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'run_ledger.ps1')
$script:fails = 0
$script:n = 0
function Check([string]$name, $got, $want) {
    $script:n++
    if ($got -ceq $want) { Write-Host "PASS $name" }
    else { $script:fails++; Write-Host "FAIL $name"; Write-Host "  got : $got"; Write-Host "  want: $want" }
}
function Lines([string]$p) { if (Test-Path -LiteralPath $p) { return @([System.IO.File]::ReadAllLines($p)) } else { return @() } }

New-Item -ItemType Directory -Force -Path (Join-Path $Work 'app') | Out-Null
$ver = 'abc123 fix "quotes" in C:\x\y'
[System.IO.File]::WriteAllText((Join-Path $Work 'app\VERSION'), $ver + "`nsecond line is ignored`n", (New-Object System.Text.UTF8Encoding($false)))
$ledger = Join-Path $Work 'history\run_ledger.jsonl'
Initialize-RunLedger -Root $Work -App (Join-Path $Work 'app')

# ---- exact lines
Check 'start line' (Write-RunEvent -Proc 'sink' -Ev 'start' -ProcId 4242 -NowMs 1792900000123) $true
Check 'exit line with a large negative code' (Write-RunEvent -Proc 'sink' -Ev 'exit' -ProcId 4242 -ExitCode -1073741523 -UptimeS 12.34 -NowMs 1792900012460) $true
Check 'exit line with an unknown code' (Write-RunEvent -Proc 'ui' -Ev 'exit' -ProcId 77 -ExitCode $null -NowMs 1792900013000) $true
Check 'exit line with code 0 is known, not null' (Write-RunEvent -Proc 'bridge' -Ev 'exit' -ProcId 78 -ExitCode 0 -UptimeS 5 -NowMs 1792900014000) $true
Check 'supervisor_up line' (Write-RunEvent -Proc 'supervisor' -Ev 'supervisor_up' -ProcId 9 -NowMs 1792900015000) $true
$v = '"abc123 fix \"quotes\" in C:\\x\\y"'
$L = Lines $ledger
Check 'five lines written' $L.Count 5
Check 'line 1' $L[0] ('{"k":"run_event","v":1,"t":1792900000.123,"proc":"sink","ev":"start","pid":4242,"exit_code":null,"exit_code_known":false,"uptime_s":null,"version":' + $v + '}')
Check 'line 2' $L[1] ('{"k":"run_event","v":1,"t":1792900012.460,"proc":"sink","ev":"exit","pid":4242,"exit_code":-1073741523,"exit_code_known":true,"uptime_s":12.3,"version":' + $v + '}')
Check 'line 3 (unknown exit code stays null)' $L[2] ('{"k":"run_event","v":1,"t":1792900013.000,"proc":"ui","ev":"exit","pid":77,"exit_code":null,"exit_code_known":false,"uptime_s":null,"version":' + $v + '}')
Check 'line 4 (exit code 0)' $L[3] ('{"k":"run_event","v":1,"t":1792900014.000,"proc":"bridge","ev":"exit","pid":78,"exit_code":0,"exit_code_known":true,"uptime_s":5.0,"version":' + $v + '}')
Check 'line 5' $L[4] ('{"k":"run_event","v":1,"t":1792900015.000,"proc":"supervisor","ev":"supervisor_up","pid":9,"exit_code":null,"exit_code_known":false,"uptime_s":null,"version":' + $v + '}')
Check 'no BOM, no CR in the file' (([System.IO.File]::ReadAllBytes($ledger)[0] -eq 123) -and (-not ([System.IO.File]::ReadAllText($ledger).Contains("`r")))) $true

# ---- a missing VERSION file gives "version":null
Remove-Item -LiteralPath (Join-Path $Work 'app\VERSION')
[void](Write-RunEvent -Proc 'sink' -Ev 'start' -ProcId 1 -NowMs 1792900016000)
$L = Lines $ledger
Check 'missing VERSION -> null' $L[5] '{"k":"run_event","v":1,"t":1792900016.000,"proc":"sink","ev":"start","pid":1,"exit_code":null,"exit_code_known":false,"uptime_s":null,"version":null}'

# ---- crash-loop guard: 3 events per process per hour, then counted, summary at the hour change
$W2 = Join-Path $Work 'guard'
Initialize-RunLedger -Root $W2 -App $W2 -MaxPerHour 3
$h0 = [long]([math]::Floor(1792900000 / 3600.0) * 3600000)
$res = @()
foreach ($i in 0..4) { $res += (Write-RunEvent -Proc 'analysis' -Ev 'exit' -ProcId $i -ExitCode 127 -NowMs ($h0 + 1000 * $i)) }
Check 'first 3 written, next 2 suppressed' ($res -join ',') 'True,True,True,False,False'
Check 'another process is not affected by the guard' (Write-RunEvent -Proc 'bridge' -Ev 'start' -ProcId 5 -NowMs ($h0 + 5000)) $true
Check 'supervisor_up is never suppressed' (Write-RunEvent -Proc 'supervisor' -Ev 'supervisor_up' -ProcId 6 -NowMs ($h0 + 6000)) $true
Check 'lines before the hour changes' (Lines (Join-Path $W2 'history\run_ledger.jsonl')).Count 5
Check 'next hour: event written' (Write-RunEvent -Proc 'analysis' -Ev 'start' -ProcId 9 -NowMs ($h0 + 3600000 + 10)) $true
$G = Lines (Join-Path $W2 'history\run_ledger.jsonl')
Check 'suppressed summary + the new event added' $G.Count 7
Check 'suppressed summary line' $G[5] ('{"k":"run_event","v":1,"t":' + (Format-UnixTime ($h0 + 3599999)) + ',"proc":"analysis","ev":"suppressed","pid":null,"exit_code":null,"exit_code_known":false,"uptime_s":null,"count":2,"version":null}')
Check 'summary is written once' ((@($G | Where-Object { $_ -like '*"ev":"suppressed"*' })).Count) 1

# ---- rotation at a tiny cap: nothing is lost, the main file restarts small
$W3 = Join-Path $Work 'rot'
Initialize-RunLedger -Root $W3 -App $W3 -CapBytes 400
foreach ($i in 1..6) { [void](Write-RunEvent -Proc 'sink' -Ev 'start' -ProcId $i -NowMs (1792900000000 + 1000 * $i)) }
$main = Lines (Join-Path $W3 'history\run_ledger.jsonl')
$old = Lines (Join-Path $W3 'history\run_ledger.jsonl.1')
Check 'rotation: main + backup hold all 6 events' ($main.Count + $old.Count) 6
Check 'rotation: a backup exists' ($old.Count -gt 0) $true
Check 'rotation: the main file is under the cap plus one line' ((Get-Item (Join-Path $W3 'history\run_ledger.jsonl')).Length -lt 600) $true

# waits until the process id is gone, through a separate Get-Process object: it never touches the Process object under test (its Handle and ExitCode state are what is tested)
function Wait-Gone([int]$ProcId, [int]$Seconds = 60) {
    $end = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $end -and (Get-Process -Id $ProcId -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 100 }
}

# ---- Register-Run / Register-Exit against a real short-lived child with redirected output
$W4 = Join-Path $Work 'proc'
Initialize-RunLedger -Root $W4 -App $W4
$p = Start-Process -FilePath cmd.exe -ArgumentList @('/c', 'exit 7') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Work 'o1.txt') -RedirectStandardError (Join-Path $Work 'e1.txt')
Register-Run 'child' $p
Wait-Gone $p.Id
Register-Exit 'child' $p
$P4 = Lines (Join-Path $W4 'history\run_ledger.jsonl')
Check 'Register-Run + Register-Exit wrote 2 lines' $P4.Count 2
Check 'start line carries the real pid' ($P4[0] -like ('*"proc":"child","ev":"start","pid":' + $p.Id + ',*')) $true
Check 'exit line carries the real exit code 7 (handle touched at start)' ($P4[1] -like '*"ev":"exit","pid":*,"exit_code":7,"exit_code_known":true,*') $true
# the bug the Handle touch fixes: without it the exit code of the same kind of child is empty on Windows PowerShell 5.1
$q = Start-Process -FilePath cmd.exe -ArgumentList @('/c', 'exit 7') -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Work 'o2.txt') -RedirectStandardError (Join-Path $Work 'e2.txt')
Wait-Gone $q.Id
Write-Host "info: ExitCode without the Handle touch = [$($q.ExitCode)] (PowerShell $($PSVersionTable.PSVersion))"
Register-Exit 'child_nohandle' $q
$Q4 = Lines (Join-Path $W4 'history\run_ledger.jsonl')
if ($PSVersionTable.PSVersion.Major -lt 6) {
    Check 'without the Handle touch the exit code is unknown (Windows PowerShell 5.1)' ($Q4[2] -like '*"exit_code":null,"exit_code_known":false,*') $true
}

# ---- a ledger that cannot be written must not throw
[System.IO.File]::WriteAllText((Join-Path $Work 'blocker'), 'a file, so no directory can be created below it')
Initialize-RunLedger -Root (Join-Path $Work 'blocker\nope') -App $Work
Check 'unwritable ledger path returns false without throwing' (Write-RunEvent -Proc 'sink' -Ev 'start' -ProcId 1) $false

Write-Host "$($script:n) checks, $($script:fails) failure(s)"
if ($script:fails -gt 0) { exit 1 }
exit 0
