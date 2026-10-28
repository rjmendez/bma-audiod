# Run ledger helper for run_pipeline.ps1 (contract: run-history, contracts/specs/run-history.md, record kind run_event).
# Dot-source it:  . (Join-Path $PSScriptRoot 'run_ledger.ps1')   then   Initialize-RunLedger -Root $root -App $app
# One JSON line per process start / exit of the supervised processes, appended to <root>\history\run_ledger.jsonl.
# ASCII only, no here-strings, no backtick continuations (so CRLF and LF checkouts both parse), Windows PowerShell 5.1 safe.
# Every function swallows its own errors: a bug in the ledger must never stop the pipeline supervisor.

$script:LedgerPath = $null
$script:LedgerApp = $null
$script:LedgerCap = 8MB
$script:LedgerMaxPerHour = 60          # start + exit events per process per UTC hour before the rest are only counted
$script:LedgerHours = @{}              # proc -> @{ hour = <unix hour>; n = <events written>; suppressed = <events dropped> }

function Initialize-RunLedger {
    param([string]$Root, [string]$App, [long]$CapBytes = 8388608, [int]$MaxPerHour = 60)
    try {
        $dir = Join-Path $Root 'history'
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $script:LedgerPath = Join-Path $dir 'run_ledger.jsonl'
        $script:LedgerApp = $App
        $script:LedgerCap = $CapBytes
        $script:LedgerMaxPerHour = $MaxPerHour
        $script:LedgerHours = @{}
    } catch { }
}

function ConvertTo-JsonString {
    # JSON string literal, ASCII only: quote, backslash, control characters and everything above 0x7e are escaped
    param($s)
    if ($null -eq $s) { return 'null' }
    $s = [string]$s
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $s.ToCharArray()) {
        $c = [int]$ch
        if ($c -eq 34) { [void]$sb.Append('\"') }
        elseif ($c -eq 92) { [void]$sb.Append('\\') }
        elseif ($c -eq 10) { [void]$sb.Append('\n') }
        elseif ($c -eq 13) { [void]$sb.Append('\r') }
        elseif ($c -eq 9) { [void]$sb.Append('\t') }
        elseif ($c -lt 32 -or $c -gt 126) { [void]$sb.Append(('\u{0:x4}' -f $c)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Get-AppVersion {
    # first line of <app>\VERSION (install_task.ps1 replaces the file at every deploy, so read it at each event), or $null
    param([string]$App = $script:LedgerApp)
    try {
        $f = Join-Path $App 'VERSION'
        if (Test-Path -LiteralPath $f) {
            $lines = [System.IO.File]::ReadAllLines($f)
            if ($lines.Length -gt 0) { return $lines[0].Trim() }
        }
    } catch { }
    return $null
}

function Format-UnixTime {
    param([long]$Ms)
    return ([double]$Ms / 1000.0).ToString('0.000', [System.Globalization.CultureInfo]::InvariantCulture)
}

function Rotate-IfBig {
    # size-rotate a file the supervisor alone writes: > cap bytes -> FILE.1 (replaced). Never throws.
    param([string]$Path, [long]$CapBytes)
    try {
        if (Test-Path -LiteralPath $Path) {
            if ((Get-Item -LiteralPath $Path).Length -gt $CapBytes) {
                if (Test-Path -LiteralPath ($Path + '.1')) { Remove-Item -LiteralPath ($Path + '.1') -Force }
                Move-Item -LiteralPath $Path -Destination ($Path + '.1') -Force
            }
        }
    } catch { }
}

function Add-LedgerLine {
    param([string]$Line)
    Rotate-IfBig -Path $script:LedgerPath -CapBytes $script:LedgerCap
    [System.IO.File]::AppendAllText($script:LedgerPath, $Line + "`n", (New-Object System.Text.UTF8Encoding($false)))
}

function Write-RunEvent {
    # Ev: start | exit | supervisor_up. ExitCode: $null when unknown (never 0 by default). UptimeS: on exit, $null if unknown.
    # NowMs: Unix milliseconds, only for tests. Returns $true when a line was written, $false when suppressed or on error.
    param([string]$Proc, [string]$Ev, $ProcId = $null, $ExitCode = $null, $UptimeS = $null, $NowMs = $null)
    try {
        if (-not $script:LedgerPath) { return $false }
        if ($null -eq $NowMs) { $NowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
        $NowMs = [long]$NowMs
        $hour = [long][math]::Floor($NowMs / 3600000.0)
        # close the summary of every process whose hour is over (a process that stopped crashing still gets its summary)
        foreach ($name in @($script:LedgerHours.Keys)) {
            $h = $script:LedgerHours[$name]
            if ($h.hour -lt $hour -and $h.suppressed -gt 0) {
                $sline = '{"k":"run_event","v":1,"t":' + (Format-UnixTime ($h.hour * 3600000 + 3599999)) + ',"proc":' + (ConvertTo-JsonString $name) + ',"ev":"suppressed","pid":null,"exit_code":null,"exit_code_known":false,"uptime_s":null,"count":' + $h.suppressed + ',"version":' + (ConvertTo-JsonString (Get-AppVersion)) + '}'
                Add-LedgerLine $sline
                $h.suppressed = 0
            }
        }
        if ($Ev -ne 'supervisor_up') {
            if (-not $script:LedgerHours.ContainsKey($Proc) -or $script:LedgerHours[$Proc].hour -ne $hour) {
                $script:LedgerHours[$Proc] = @{ hour = $hour; n = 0; suppressed = 0 }
            }
            $h = $script:LedgerHours[$Proc]
            if ($h.n -ge $script:LedgerMaxPerHour) { $h.suppressed = $h.suppressed + 1; return $false }
            $h.n = $h.n + 1
        }
        $pidText = 'null'
        if ($null -ne $ProcId) { $pidText = ([long]$ProcId).ToString([System.Globalization.CultureInfo]::InvariantCulture) }
        $codeText = 'null'; $known = 'false'
        if ($null -ne $ExitCode) { $codeText = ([long]$ExitCode).ToString([System.Globalization.CultureInfo]::InvariantCulture); $known = 'true' }
        $upText = 'null'
        if ($null -ne $UptimeS) { $upText = ([double]$UptimeS).ToString('0.0', [System.Globalization.CultureInfo]::InvariantCulture) }
        $line = '{"k":"run_event","v":1,"t":' + (Format-UnixTime $NowMs) + ',"proc":' + (ConvertTo-JsonString $Proc) + ',"ev":' + (ConvertTo-JsonString $Ev) + ',"pid":' + $pidText + ',"exit_code":' + $codeText + ',"exit_code_known":' + $known + ',"uptime_s":' + $upText + ',"version":' + (ConvertTo-JsonString (Get-AppVersion)) + '}'
        Add-LedgerLine $line
        return $true
    } catch { return $false }
}

function Register-Run {
    # right after Start-Process -PassThru: touch .Handle so the exit code is still readable after the process has exited
    # (with redirected output Start-Process keeps no handle open and ExitCode comes back empty), then record the start
    param([string]$Proc, $P)
    try {
        if ($null -eq $P) { return }
        $null = $P.Handle
        [void](Write-RunEvent -Proc $Proc -Ev 'start' -ProcId $P.Id)
    } catch { }
}

function Register-Exit {
    param([string]$Proc, $P)
    try {
        if ($null -eq $P) { return }
        $code = $null; $up = $null
        try { $code = $P.ExitCode } catch { }
        try { $up = ($P.ExitTime - $P.StartTime).TotalSeconds } catch { }
        [void](Write-RunEvent -Proc $Proc -Ev 'exit' -ProcId $P.Id -ExitCode $code -UptimeS $up)
    } catch { }
}
