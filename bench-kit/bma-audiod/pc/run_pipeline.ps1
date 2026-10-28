# Supervises the BMA live pipeline on this PC:
#   Dante card -> dante_bridge.py -> (UDP :48200) -> single-channel classifier (WSL)
#                                 \-> (UDP :48300, all 8 beams) -> analysis host (WSL): dsp, spatial, tagging,
#                                     embeddings + novelty, CLAP zero-shot, multi-beam BirdNET. Metadata only; wildlife clips are visual-only (spectrogram + signatures) unless $root\enable_clip_audio.txt exists (see below).
# Started at logon by the scheduled task 'BMA-Dante-Pipeline' (install_task.ps1). Restarts any part that
# exits. Everything it needs is in $root\app (a pinned copy, VERSION says which commit).
# Logs: $root\logs\bridge.log, classifier.log, analyzer.log, detections.jsonl, supervisor.log;
#       analysis metadata: $root\analysis\*.jsonl, status.json; NO audio is written unless $root\enable_clip_audio.txt exists (then --clip-audio: 24 h clips, confirmed-voice clips purged): wildlife detections above 0.85 certainty, away from speech, leave only a spectrogram PNG (nothing below 3.5 kHz) and signatures under $root\analysis\clips (analysis\clips.py);
#       the read-only web dashboard (ui\bma_ui.py, port 48500, LAN + tailnet) logs to ui.log / ui.err;
#       loss telemetry (app\loss_log.py) logs to loss_log.log / loss_log.err and appends to history\loss.jsonl;
#       nightly backup 03:00 local (app\backup_data.ps1 -> backup\yyyyMMdd, 14 days) logs to backup.log / backup.err; daily digest 06:00 local (app\daily_digest.py -> reports\digest\yyyyMMdd.txt) logs to digest.log / digest.err;
#       the hourly ambient temperature fetch (app\ambient_temp.py, only when app\geo_county.json exists) logs to ambient.log /
#       ambient.err and appends to history\ambient_temp.jsonl.
#
# Nothing here is tied to a machine: this script lives in the data dir ($root) next to app\ and logs\, and every
# path is derived from that. The few machine-specific values go in ONE optional, untracked file next to this script,
# bma.local.ps1 (template: bma.local.example.ps1). The same names are also read from the environment.
#   $BMA_PYTHON          bridge, dashboard, board collector (default: the first python on PATH)
#   $BMA_SINK_PYTHON     cable sink (numpy + sounddevice); the sink only starts when this is set
#   $BMA_BOARD           the board's IP address or MAC address
#   $BMA_DSPD_TOKEN_FILE file with the dspd bearer token (dashboard)
#   $BMA_MODEL_DIR       analysis model files (default: $root\models)
#   $BMA_WSL_PYTHON      python inside WSL for the analyzer and classifier (default: python3 on the WSL PATH). Added to
#                        the allowed set because the analyzer's torch/onnxruntime live in a venv that cannot be derived.
#   $BMA_BEAM_SHIFT      optional: 8 comma-separated integers (|k| <= 4), the analyzer's per-beam sample-shift correction
#                        (host.py --beam-shift; k = read that beam k samples later, +1 for a beam that arrives one sample
#                        late). Unset = no flag is passed and the analyzer changes nothing. Example: 1,0,0,0,0,0,0,-1
# Fixed, not configurable: the beam-band log, tonal masks and dashboard rollups live in $root\reports; the dashboard
# accepts this computer's name and, when tailscale is on PATH, this node's tailnet name.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$local = Join-Path $PSScriptRoot 'bma.local.ps1'
if (Test-Path $local) { . $local }
function Setting([string]$name) {
    $v = Get-Variable -Name $name -ValueOnly -ErrorAction SilentlyContinue
    if ($v) { return $v }
    return [Environment]::GetEnvironmentVariable($name)
}
$app  = Join-Path $root 'app'
$logs = Join-Path $root 'logs'
$reports = Join-Path $root 'reports'
New-Item -ItemType Directory -Force -Path $logs, $reports | Out-Null
# the children inherit these: the board collector, cable sink and dashboard read them
$env:BMA_DATA_DIR = $root
foreach ($n in 'BMA_BOARD', 'BMA_DSPD_TOKEN_FILE') { $v = Setting $n; if ($v) { [Environment]::SetEnvironmentVariable($n, $v) } }
# The bridge receives MULTICAST, which Windows Firewall only delivers to a program that has an inbound Allow
# rule on the interface's profile. A link flap or power cycle re-classifies the direct card link as Public,
# and an interpreter with an explicit Public BLOCK rule (Block beats Allow) silently stopped receiving multicast
# while unicast ARC/mDNS still worked. Set BMA_PYTHON to an interpreter that already has an Allow rule on Public.
# It needs numpy.
$py = Setting 'BMA_PYTHON'
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Error 'no python: set BMA_PYTHON in bma.local.ps1 or put python on PATH'; exit 2 }
$port = 48200
$aport = 48300
$sport = 48400                                         # the Windows audio sink (virtual cable), loopback
$sinkPy = Setting 'BMA_SINK_PYTHON'                    # numpy + sounddevice (WASAPI)
$uiPort = 48500                                        # the read-only web dashboard (LAN + tailnet, see Start-Ui)
$wslPy = Setting 'BMA_WSL_PYTHON'
$specPort = 48410                                      # bma_spectrum.py listens here for the bridge's 8-beam BMAA (loopback only)
$specUiPort = 48520                                    # the dashboard listens here for spectrum rows (loopback only, 127.0.0.1)

# run ledger: process start/exit events with exit codes and the app VERSION -> $root\history\run_ledger.jsonl (run_ledger.ps1).
# Losing the helper must not stop the supervisor, so a failed load leaves harmless no-op stand-ins.
try { . (Join-Path $PSScriptRoot 'run_ledger.ps1'); Initialize-RunLedger -Root $root -App $app }
catch {
    function Write-RunEvent { }
    function Register-Run { }
    function Register-Exit { }
    function Rotate-IfBig { }
}

# supervisor.log used to grow without bound (5 MB after two days of a crash loop); keep it under 5 MB plus one backup
$script:logWrites = 0
function Log([string]$m) {
    $f = Join-Path $logs 'supervisor.log'
    $script:logWrites++
    if ($script:logWrites % 50 -eq 1) { Rotate-IfBig -Path $f -CapBytes 5242880 }
    Add-Content -Path $f -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $m)
}

# Start-Process joins the argument array with spaces and does not quote: quote anything that may hold a space
function Q([string]$s) { return '"' + $s + '"' }

# the WSL view of a Windows path (the analyzer and classifier run in WSL); cached once WSL answers
$script:wslCache = @{}
function ConvertTo-WslPath([string]$p) {
    if ($script:wslCache.ContainsKey($p)) { return $script:wslCache[$p] }
    $r = & wsl.exe -e wslpath -a -u $p 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $r) { Log "wslpath failed for $p"; return $null }
    $r = ([string]($r | Select-Object -First 1)).Trim()
    $script:wslCache[$p] = $r
    return $r
}

# the analyzer's --beam-shift value from BMA_BEAM_SHIFT: '' when unset; a malformed value is logged and NOT passed (the
# analyzer would refuse it and the supervisor would restart it forever)
function Get-BeamShift {
    $v = Setting 'BMA_BEAM_SHIFT'
    if (-not $v) { return '' }
    $v = ([string]$v).Trim()
    if ($v -notmatch '^-?[0-4](,-?[0-4]){7}$') { Log "BMA_BEAM_SHIFT '$v' ignored: need 8 comma-separated integers, each -4..4"; return '' }
    return $v
}

# '1' when <root>\enable_clip_audio.txt exists (read at every analyzer start), else '': run_analyzer.sh's 4th positional (--clip-audio)
function Get-ClipAudio {
    if (-not (Test-Path -LiteralPath (Join-Path $root 'enable_clip_audio.txt'))) { return '' }
    Log 'clip audio ON (enable_clip_audio.txt)'
    return '1'
}

# <root>\analyzer_args.txt (read at every analyzer start): one host.py argument per line, blank lines and # lines ignored. A line that is a Windows
# absolute path becomes its WSL path; a line holding a double quote or ending in a backslash cannot be quoted safely through wsl.exe and is skipped.
# Only the count is logged, never the values.
function Get-AnalyzerArgs {
    $f = Join-Path $root 'analyzer_args.txt'
    if (-not (Test-Path -LiteralPath $f)) { return @() }
    $out = @(); $skipped = 0
    foreach ($ln in (Get-Content -LiteralPath $f)) {
        $v = ([string]$ln).Trim().TrimStart([char]0xFEFF)
        if (-not $v -or $v.StartsWith('#')) { continue }
        if ($v.Contains('"') -or $v.EndsWith('\')) { $skipped++; continue }
        if ($v -match '^[A-Za-z]:[\\/]') {
            $w = ConvertTo-WslPath $v
            if (-not $w) { $skipped++; continue }
            $v = $w
        }
        $out += $v
    }
    Log "analyzer_args.txt: $($out.Count) arg(s) accepted, $skipped skipped"
    return $out
}

# the part of the wsl.exe argument list after the env prefix: bash <app>/<script> <port> [python [extra [extra2 [more...]]]]. The python slot is
# filled with python3 when only an extra argument is given, the extra slot with "" when only extra2 is (run_analyzer.sh's third
# positional is the beam shift, the fourth the clip-audio switch)
function Get-WslLauncherTail([string]$wApp, [string]$script, [int]$p, [string]$wslPython, [string]$extra, [string]$extra2 = '', [string[]]$more = @()) {
    $t = @('bash', (Q "$wApp/$script"), "$p")
    $any = $extra -or $extra2 -or $more.Count
    if ($wslPython) { $t += (Q $wslPython) } elseif ($any) { $t += 'python3' }
    if ($any) { $t += (Q $extra) }
    if ($extra2 -or $more.Count) { $t += (Q $extra2) }
    foreach ($m in $more) { $t += (Q $m) }
    return $t
}

# run one of the app's WSL launchers: bash <app>/<script> <port> [python [extra]], with the data and model dirs in its environment
function Start-WslLauncher([string]$script, [int]$p, [string]$extra = '', [string]$extra2 = '', [string[]]$more = @()) {
    $wApp = ConvertTo-WslPath $app
    $wRoot = ConvertTo-WslPath $root
    if (-not $wApp -or -not $wRoot) { return $null }
    $envArgs = @('env', ('BMA_DATA_DIR=' + $wRoot))
    $m = Setting 'BMA_MODEL_DIR'
    if ($m) {
        New-Item -ItemType Directory -Force -Path $m | Out-Null
        $wm = ConvertTo-WslPath $m
        if ($wm) { $envArgs += ('BMA_MODEL_DIR=' + $wm) }
    }
    $a = @('-e') + ($envArgs | ForEach-Object { Q $_ }) + (Get-WslLauncherTail $wApp $script $p $wslPy $extra $extra2 $more)
    Start-Process -FilePath 'wsl.exe' -PassThru -WindowStyle Hidden -ArgumentList $a
}

function Start-Bridge {
    Log "starting bridge"
    # the logon task runs this supervisor (and so its children) at BELOW_NORMAL priority; the bridge must not be
    # starved by other work or it drops packets, so it is raised to High below (AboveNormal still lost 0.1-30 % under CPU contention)
    # the spectrum target costs the bridge one more sendto per block (about 2.3 points of a core, pc\SPECTRUM.md), so it is added only
    # while the spectrum process is running at bridge start; otherwise the bridge sends nothing there and the dashboard shows "no data"
    $bridgeArgs = @('-I', (Q (Join-Path $app 'dante_bridge.py')), '--channels', 'auto', '--to', "wsl:$port",
                    '--all-to', "wsl:$aport", '--all-to', "127.0.0.1:$sport")
    if ($sp -and -not $sp.HasExited) { $bridgeArgs += @('--all-to', "127.0.0.1:$specPort") } else { Log "spectrum process not running: the bridge starts without the spectrum target" }
    $bridgeArgs += @('--stats-s', '30', '--status-file', (Q (Join-Path $root 'bridge_status.json')), '--state-file', (Q (Join-Path $root 'card_ip.txt')))
    $p = Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList $bridgeArgs `
        -RedirectStandardOutput (Join-Path $logs 'bridge.log') -RedirectStandardError (Join-Path $logs 'bridge.err')
    try { $p.PriorityClass = 'High' } catch { Log "could not raise bridge priority: $_" }
    $p
}

function Start-Classifier {
    Log "starting classifier (WSL)"
    Rotate-ChildLogs @('detections.jsonl', 'classifier.log')
    Start-WslLauncher 'run_classifier.sh' $port
}

function Start-BoardDebug {
    Log "starting board debug collector"
    # meters + DSP-monitor lines + @health housekeeping from the board's own daemon; board named by $BMA_BOARD (MAC or IP)
    Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList @('-I', (Q (Join-Path $app 'bma_debug_rx.py')), 'auto', '--secs', '0', '--quiet',
                        '--out', (Q (Join-Path $root 'analysis\board_debug.jsonl'))) `
        -RedirectStandardOutput (Join-Path $logs 'board_debug.log') -RedirectStandardError (Join-Path $logs 'board_debug.err')
}

function Start-CableSink {
    Log "starting cable sink"
    # 8 beams + sample-index track + time-stamped control-channel debug data as 16 ch @ 48 kHz (cable-stream 2.0.0: the
    # index and data tracks are 12-bit symbols on channels 9-13, count (u - 2048) << 8) into the
    # VB-Cable "CABLE In 16 Ch" device (record it from "CABLE Output"; decode with bma_cable_decode.py; check the path with bma_cable_selftest.py, sink stopped)
    Start-Process -FilePath $sinkPy -PassThru -WindowStyle Hidden `
        -ArgumentList @('-I', (Q (Join-Path $app 'bma_cable_sink.py')), '--listen', "127.0.0.1:$sport",
                        '--board-debug', (Q (Join-Path $root 'analysis\board_debug.jsonl')),
                        '--bridge-status', (Q (Join-Path $root 'bridge_status.json')),
                        '--bands-log', (Q (Join-Path $reports 'beam_bands.jsonl')),
                        '--status-file', (Q (Join-Path $root 'sink_status.json'))) `
        -RedirectStandardOutput (Join-Path $logs 'sink.log') -RedirectStandardError (Join-Path $logs 'sink.err')
}

function Start-Spectrum {
    Log "starting spectrum rows (loopback only, standard tier)"
    # magnitude-only band rows for the dashboard's spectrum view (spectrum-packet 1.0.0): audio in on 127.0.0.1:$specPort, rows out to
    # the dashboard on 127.0.0.1:$specUiPort. It refuses a non-loopback address. The detail tier (--detail here, --spectrum-detail on
    # the dashboard) stays off: it is a local diagnostic, enabled by hand.
    Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList @('-I', (Q (Join-Path $app 'bma_spectrum.py')), '--listen', "127.0.0.1:$specPort", '--to', "127.0.0.1:$specUiPort") `
        -RedirectStandardOutput (Join-Path $logs 'spectrum.log') -RedirectStandardError (Join-Path $logs 'spectrum.err')
}

function Start-Analyzer {
    Log "starting analysis host (WSL)"
    Rotate-ChildLogs @('analyzer.log')
    # WSL caps UDP receive buffers at 208 KB, which drops ~0.2% of the 8-beam stream when the host is busy.
    # Volatile, VM-local, and only a ceiling (sockets still ask for what they need).
    # (sysctl is not on root's PATH in this distro; write the proc file directly)
    & wsl.exe -u root -e sh -c "echo 8388608 > /proc/sys/net/core/rmem_max" *> $null
    Start-WslLauncher 'run_analyzer.sh' $aport (Get-BeamShift) (Get-ClipAudio) @(Get-AnalyzerArgs)
}

# the board's current IPv4 address when $BMA_BOARD is a MAC address (its DHCP address changes at every boot); '' if unknown
function Resolve-BoardIp {
    $b = Setting 'BMA_BOARD'
    if (-not $b) { return '' }
    if ($b -notmatch '^[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}$') { return $b }
    $mac = $b.ToLower().Replace(':', '-')
    try {
        $n = Get-NetNeighbor -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.LinkLayerAddress -and $_.LinkLayerAddress.ToLower() -eq $mac } | Select-Object -First 1
        if ($n) { return $n.IPAddress }
    } catch { Log "board lookup failed: $_" }
    return ''
}

# this node's tailnet DNS name (lower case, no trailing dot) when tailscale is installed and up; '' otherwise
function Get-TailnetName {
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if (-not $ts) { return '' }
    try {
        $j = (& $ts.Source status --json 2>$null) -join "`n" | ConvertFrom-Json
        return ([string]$j.Self.DNSName).TrimEnd('.').ToLower()
    } catch { return '' }
}

# the dashboard login token (ui\AUTH.md): created ONCE, never overwritten. 48 hex characters from the .NET RNG, written without a BOM
# or a trailing newline to a file only the current user can read (inheritance removed, one grant). Returns $true when it created the
# file. The ACL is applied to the empty file BEFORE the token is written, and a failed ACL deletes the file (fail closed: the
# dashboard then refuses to start without a login instead of serving with a readable token).
function Initialize-DashboardToken([string]$path) {
    if (Test-Path -LiteralPath $path) { return $false }
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $hex = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    [System.IO.File]::WriteAllBytes($path, [byte[]]@())
    & icacls.exe $path /inheritance:r /grant:r "${me}:(F)" *> $null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        Log "could not restrict the permissions of $path (icacls exit $LASTEXITCODE); no dashboard token was created"
        return $false
    }
    [System.IO.File]::WriteAllBytes($path, [System.Text.Encoding]::ASCII.GetBytes($hex))
    Log "dashboard token file created at $path (read it with show_dashboard_token.ps1; the token is not logged)"
    return $true
}

# The dashboard's HTTPS files: tls\dashboard_cert.pem + tls\dashboard_key.pem (made and renewed by setup_tls.ps1, in a directory only this user can read),
# or the older <root>\dashboard_cert.pem + dashboard_key.pem. Returns @(cert, key) or $null (no TLS: both files of one pair must exist).
function Get-TlsFiles([string]$root) {
    foreach ($dir in @((Join-Path $root 'tls'), $root)) {
        $c = Join-Path $dir 'dashboard_cert.pem'; $k = Join-Path $dir 'dashboard_key.pem'
        if ((Test-Path -LiteralPath $c) -and (Test-Path -LiteralPath $k)) { return @($c, $k) }
    }
    return $null
}

# Optional dashboard sign-in features, each switched on by a file in the data dir (no environment variable, nothing on by default):
#   allowed_users.txt  with at least one login -> --tailnet-identity --allowed-users <file> (trust Tailscale-User-Login from `tailscale serve` only; ui\AUTH.md)
#   enable_passkeys.txt (any content) and tls\tls_name.txt -> --passkey-origin https://<name> and https://<name>:<port> (passkeys need HTTPS)
#   enable_ui_next.txt (any content) -> --ui-next (the next dashboard under /next/; same login, ui-http 1.11.0)
# Returns the extra argument list (already quoted).
function Get-UiOptionalArgs([string]$root, [int]$port) {
    $extra = @()
    $au = Join-Path $root 'allowed_users.txt'
    if ((Test-Path -LiteralPath $au) -and (@(Get-Content -LiteralPath $au -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') }).Count -gt 0)) {
        $extra += @('--tailnet-identity', '--allowed-users', (Q $au))
    }
    $nm = Join-Path (Join-Path $root 'tls') 'tls_name.txt'
    if ((Test-Path -LiteralPath (Join-Path $root 'enable_passkeys.txt')) -and (Test-Path -LiteralPath $nm)) {
        $n = ([System.IO.File]::ReadAllText($nm)).Trim()
        if ($n -match '^[a-z0-9.-]+$') { $extra += @('--passkey-origin', "https://$n", '--passkey-origin', "https://${n}:$port") }
    }
    if (Test-Path -LiteralPath (Join-Path $root 'enable_ui_next.txt')) { $extra += '--ui-next' }
    return $extra
}

# Once a day: setup_tls.ps1 renews the Tailscale certificate when it is within 30 days of expiry (only when setup_tls.ps1 installed one here,
# i.e. tls\tls_name.txt exists). Exit code 10 = a new certificate was installed: the caller restarts the dashboard, which reads the files at start.
# Returns the exit code, or -1 when nothing was run. The script prints no key material; its one line is copied to supervisor.log.
function Invoke-TlsRenewal([string]$root) {
    $script = Join-Path $root 'setup_tls.ps1'
    if (-not ((Test-Path -LiteralPath (Join-Path (Join-Path $root 'tls') 'tls_name.txt')) -and (Test-Path -LiteralPath $script))) { return -1 }
    $outF = Join-Path $logs 'tls.out'
    $p = Start-Process -FilePath 'powershell.exe' -PassThru -WindowStyle Hidden -RedirectStandardOutput $outF -RedirectStandardError (Join-Path $logs 'tls.err') `
        -ArgumentList @('-NoProfile', '-NonInteractive', '-File', (Q $script), '-Root', (Q $root))
    if (-not $p.WaitForExit(180000)) { try { $p.Kill() } catch { }; Log 'TLS renewal (setup_tls.ps1) took over 3 minutes and was stopped'; return 1 }
    $line = (Get-Content -LiteralPath $outF -ErrorAction SilentlyContinue | Select-Object -Last 1)
    if ($p.ExitCode -ne 0 -and $line) { Log ("TLS: " + $line) }
    return $p.ExitCode
}

# The daily check of the main loop: renew when due and, when a new certificate was installed (exit 10), stop the dashboard process so the
# supervisor's normal restart loads it. Returns $true when it stopped the dashboard.
function Invoke-DailyTlsCheck([string]$root, $ui) {
    if ((Invoke-TlsRenewal $root) -ne 10) { return $false }
    Log 'a new dashboard TLS certificate was installed; restarting the dashboard to load it'
    if ($ui -and -not $ui.HasExited) { try { $ui.Kill(); return $true } catch { Log "could not stop the dashboard: $_" } }
    return $false
}

function Start-Ui {
    # read-only dashboard (standard library only): reads the pipeline's files, GETs the board; rollups go to $reports\ui.
    # It listens on all interfaces: every network here is private (the board and Dante links are point-to-point; the rest is
    # the LAN and the tailnet) and the inbound Windows Firewall rule "BMA dashboard 48500" limits it to the local subnets and
    # the tailnet. Every request needs the login token in $root\dashboard_token.txt (created here at the first run; without
    # the file the dashboard refuses to start on a non-loopback bind), so keep that rule as a second layer. Host names are
    # allowlisted (this computer's name and its tailnet name); IP literals are accepted with --allow-ip-hosts (they cannot be
    # DNS-rebound, and DHCP addresses change). HTTPS is on when tls\dashboard_cert.pem + tls\dashboard_key.pem exist (setup_tls.ps1 makes them from a Tailscale certificate and
    # renews them, see below) or the older dashboard_cert.pem + dashboard_key.pem in $root; allowed_users.txt / enable_passkeys.txt switch on tailnet identity / passkeys (Get-UiOptionalArgs).
    $tokenFile = Join-Path $root 'dashboard_token.txt'
    [void](Initialize-DashboardToken $tokenFile)
    $hostNames = @($env:COMPUTERNAME.ToLower())
    $tn = Get-TailnetName
    if ($tn) { $hostNames += $tn }
    Log ("starting web dashboard (0.0.0.0:$uiPort, host names: " + ($hostNames -join ', ') + ")")
    Rotate-ChildLogs @('ui.err')
    # --spectrum-port: rows from bma_spectrum.py arrive on 127.0.0.1 only. /api/spectrum is served only to an AUTHENTICATED client (the dashboard token of
    # Initialize-DashboardToken; without a login nobody is, and the dashboard shows "sign in required"); the detail tier also needs --spectrum-detail, which is not passed here.
    $uiArgs = @('-I', (Q (Join-Path $app 'ui\bma_ui.py')), '--bind', '0.0.0.0', '--port', "$uiPort", '--allow-ip-hosts', '--spectrum-port', "$specUiPort",
                '--pipeline-root', (Q $root), '--reports-dir', (Q $reports), '--rollup-dir', (Q (Join-Path $reports 'ui')),
                '--auth-token-file', (Q $tokenFile))
    $tlsFiles = Get-TlsFiles $root
    if ($tlsFiles) { $uiArgs += @('--tls-cert', (Q $tlsFiles[0]), '--tls-key', (Q $tlsFiles[1])) }
    $uiArgs += Get-UiOptionalArgs $root $uiPort
    $ip = Resolve-BoardIp
    if ($ip) { $uiArgs += @('--board-url', "http://${ip}:8470") } else { $uiArgs += '--no-board' }
    foreach ($n in $hostNames) { $uiArgs += @('--allow-host', $n) }
    Start-Process -FilePath $py -PassThru -WindowStyle Hidden -ArgumentList $uiArgs `
        -RedirectStandardOutput (Join-Path $logs 'ui.log') -RedirectStandardError (Join-Path $logs 'ui.err')
}

# The ambient loop's clock: UTC and monotonic (a Stopwatch added to the UTC time at first use). Local Get-Date jumps at a DST
# change or an NTP / manual step and would delay the next fetch or the hung-instance kill by the size of the step.
function Get-MonoUtcNow {
    if ($null -eq $script:monoSw) { $script:monoSw = [Diagnostics.Stopwatch]::StartNew(); $script:monoBase = [datetime]::UtcNow }
    return $script:monoBase.Add($script:monoSw.Elapsed)
}

# What to log about a finished ambient fetch, or $null when it needs no line. The tool always exits 0 (a failure is one counted
# stderr line, overwritten by the next run), so the supervisor copies that line, and a nonzero exit code, into supervisor.log.
# The stderr line carries counts only (never a coordinate or a path).
function Get-AmbientNote([int]$ExitCode, [string]$ErrText) {
    $e = ([string]$ErrText).Trim() -replace '\s+', ' '
    if ($e.Length -gt 300) { $e = $e.Substring(0, 300) }
    if ($ExitCode -ne 0) { return "ambient temperature fetch exited (code $ExitCode): $e" }
    if ($e) { return "ambient temperature fetch reported: $e" }
    return $null
}

# A feed that stopped producing hours: the history file is missing or its newest write is older than $MaxAgeH hours (the reader
# treats a temperature older than 3 h as unknown). Returns the line to log, or $null when the feed is fresh.
function Get-AmbientStaleNote([bool]$Exists, [datetime]$LastWriteUtc, [datetime]$NowUtc, [double]$MaxAgeH = 3) {
    if (-not $Exists) { return "ambient temperature feed has no history file yet" }
    $age = ($NowUtc - $LastWriteUtc).TotalHours
    if ($age -gt $MaxAgeH) { return ("ambient temperature feed is stale: newest record written {0:N1} h ago" -f $age) }
    return $null
}

# What the hourly ambient-temperature loop should do now. Pure (no process, no clock read) so the scheduling is testable:
#   'kill'  an instance has run longer than $KillAfterS (hung: it has a 15 s network timeout, so it is stuck)
#   'wait'  an instance is still running (never two at once, even when the next hour is due)
#   'start' none is running, the feed is configured ($HasGeo: app\geo_county.json and app\ambient_temp.py exist) and $Now >= $Next
#   'none'  nothing to do (not configured, or not due yet)
function Get-AmbientAction([bool]$HasGeo, [bool]$Running, [datetime]$Now, [datetime]$Next, [datetime]$StartedAt, [int]$KillAfterS = 120) {
    if ($Running) {
        if (($Now - $StartedAt).TotalSeconds -gt $KillAfterS) { return 'kill' }
        return 'wait'
    }
    if ($HasGeo -and $Now -ge $Next) { return 'start' }
    return 'none'
}

function Start-AmbientTemp {
    Log "starting ambient temperature fetch"
    # one fetch of the last hours of 2 m air temperature (county-level location from app\geo_county.json, rounded by the tool
    # before the request); appends to history\ambient_temp.jsonl and exits. Output files are overwritten by the next run.
    Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList @('-I', (Q (Join-Path $app 'ambient_temp.py')), '--once',
                        '--geo-config', (Q (Join-Path $app 'geo_county.json')), '--out', (Q (Join-Path $root 'history'))) `
        -RedirectStandardOutput (Join-Path $logs 'ambient.log') -RedirectStandardError (Join-Path $logs 'ambient.err')
}

# loss telemetry (app\loss_log.py, loss-log contract): one process, every 30 s one record to history\loss.jsonl. Restart delay: 10 s, doubled
# per exit that came within 300 s of the start, up to 300 s; a run of 300 s or more resets it to 10 s.
function Get-LossLogDelay([int]$PrevDelayS, [double]$RanS) {
    if ($RanS -ge 300) { return 10 }
    return [int][math]::Min(300, [math]::Max(10, $PrevDelayS * 2))
}

# restart delay of a supervised child (classifier, analyzer, bridge, board collector): 0 after a run of 300 s or more, else 30 s, doubled per
# exit that came within 300 s of the start, up to 900 s
function Get-RestartDelay([int]$PrevDelayS, [double]$RanS) {
    if ($RanS -ge 300) { return 0 }
    return [int][math]::Min(900, [math]::Max(30, $PrevDelayS * 2))
}

# seconds a process ran (exit time minus start time); 0 when the process cannot tell
function Get-RunSeconds($p) {
    try { return [math]::Round(($p.ExitTime - $p.StartTime).TotalSeconds, 1) } catch { return 0.0 }
}

# the supervisor.log line of an exited child: exit code, local exit time and uptime
function Get-ExitLine([string]$Name, $p, [string]$Extra = '') {
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $at = try { $p.ExitTime.ToString('yyyy-MM-dd HH:mm:ss', $inv) } catch { 'unknown' }
    return ("{0} exited (code {1}) at {2} after {3} s{4}" -f $Name, $p.ExitCode, $at, (Get-RunSeconds $p).ToString('0.0', $inv), $Extra)
}

# size caps of the child logs, applied when the child (re)starts: a file a live child holds open cannot be rotated (a rename leaves the writer on
# the renamed file, a truncate leaves NUL padding), so only the closed file of a stopped child is
$script:ChildLogCaps = [ordered]@{ 'detections.jsonl' = 16777216; 'classifier.log' = 16777216; 'analyzer.log' = 16777216; 'ui.err' = 2097152 }
function Rotate-ChildLogs([string[]]$Names) {
    foreach ($n in $Names) { Rotate-IfBig -Path (Join-Path $logs $n) -CapBytes $script:ChildLogCaps[$n] }
}

# one periodic health line for supervisor.log; loss above 2 % is a warning
function Get-CpuPct([double]$PrevCpuS, [double]$CpuS, [double]$WallS) {
    if ($WallS -le 0 -or $CpuS -lt $PrevCpuS) { return $null }
    return [math]::Round(100.0 * ($CpuS - $PrevCpuS) / $WallS, 1)
}
# the bridge's loss_pct from its status file; $null when the file is missing, unreadable or older than $MaxAgeS
function Get-BridgeLossPct([string]$StatusFile, [datetime]$NowUtc, [double]$MaxAgeS = 90) {
    try {
        $f = Get-Item -LiteralPath $StatusFile -ErrorAction Stop
        if (($NowUtc - $f.LastWriteTimeUtc).TotalSeconds -gt $MaxAgeS) { return $null }
        $v = (Get-Content -LiteralPath $StatusFile -Raw -ErrorAction Stop | ConvertFrom-Json).loss_pct
        if ($v -is [double] -or $v -is [decimal] -or $v -is [int] -or $v -is [long]) { return [double]$v }
    } catch { }
    return $null
}
function Format-HealthLine($CpuPct, $CommitPct, $LossPct) {
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $f = { param($x) if ($null -eq $x) { 'n/a' } else { ([double]$x).ToString('0.0', $inv) + ' %' } }
    $line = "health: bridge cpu {0}, commit {1}, bridge loss {2}" -f (& $f $CpuPct), (& $f $CommitPct), (& $f $LossPct)
    if ($null -ne $LossPct -and $LossPct -gt 2.0) { $line += ' WARNING: bridge loss above 2 %' }
    return $line
}
function Get-CommitPct {
    try { return [double](Get-CimInstance -ClassName Win32_PerfFormattedData_PerfOS_Memory -ErrorAction Stop).PercentCommittedBytesInUse } catch { return $null }
}
$script:healthPrev = $null
function Write-HealthLine($bridge) {
    $cpu = $null
    try {
        if ($bridge -and -not $bridge.HasExited) {
            $bridge.Refresh()
            $now = [datetime]::UtcNow; $c = $bridge.TotalProcessorTime.TotalSeconds
            if ($script:healthPrev -and $script:healthPrev.Id -eq $bridge.Id) { $cpu = Get-CpuPct $script:healthPrev.Cpu $c ($now - $script:healthPrev.T).TotalSeconds }
            $script:healthPrev = @{ Id = $bridge.Id; Cpu = $c; T = $now }
        }
    } catch { }
    Log (Format-HealthLine $cpu (Get-CommitPct) (Get-BridgeLossPct (Join-Path $root 'bridge_status.json') ([datetime]::UtcNow)))
}

function Start-LossLog {
    Log "starting loss log"
    Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList @('-I', (Q (Join-Path $app 'loss_log.py')), '--root', (Q $root), '--every-s', '30') `
        -RedirectStandardOutput (Join-Path $logs 'loss_log.log') -RedirectStandardError (Join-Path $logs 'loss_log.err')
}

# Once at start and then daily: the live ON/OFF state of every feature of the registry (docs\FEATURES.md) by app\feature_state.py (read-only, always exit 0).
# Its table goes to logs\features.log (OFF rows first, overwritten each run) and reports\features.txt; the count line is copied to supervisor.log.
# Returns the exit code, or -1 when app\feature_state.py is not deployed.
function Invoke-FeatureState([string]$root) {
    $tool = Join-Path $app 'feature_state.py'
    if (-not (Test-Path -LiteralPath $tool)) { return -1 }
    $outF = Join-Path $logs 'features.log'
    $p = Start-Process -FilePath $py -PassThru -WindowStyle Hidden -RedirectStandardOutput $outF -RedirectStandardError (Join-Path $logs 'features.err') `
        -ArgumentList @('-I', (Q $tool), '--root', (Q $root), '--write')
    if (-not $p.WaitForExit(30000)) { try { $p.Kill() } catch { }; Log 'feature state (feature_state.py) took over 30 seconds and was stopped'; return 1 }
    $first = Get-Content -LiteralPath $outF -TotalCount 1 -ErrorAction SilentlyContinue
    if ($first) { Log ('features: ' + $first) }
    return $p.ExitCode
}

# once-a-day jobs (nightly backup at 03:00 local, daily digest at 06:00 local): due when the local hour has reached $Hour and the job has not started
# yet on this local date ($LastStart: when it last started, MinValue = never). A supervisor started after the hour runs it at once.
function Get-DailyDue([datetime]$Now, [int]$Hour, [datetime]$LastStart) {
    return (($Now.Hour -ge $Hour) -and ($LastStart.Date -lt $Now.Date))
}

# the day of the last clean full backup, from <root>\backup\last_backup.txt (written by backup_data.ps1); MinValue when absent or unreadable.
# Read from disk, not remembered: a supervisor restart neither repeats a finished night nor forgets an unfinished one.
function Get-BackupLast([string]$Root) {
    $mf = Join-Path (Join-Path $Root 'backup') 'last_backup.txt'
    $t = Get-Content -LiteralPath $mf -TotalCount 1 -ErrorAction SilentlyContinue
    $d = [datetime]::MinValue
    if ($t -and [datetime]::TryParseExact(([string]$t).Trim(), 'yyyyMMdd', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$d)) { return $d }
    return [datetime]::MinValue
}

# app\backup_data.ps1: copies the small irreplaceable data to <root>\backup\<yyyyMMdd>, keeps 14 days (no audio, no token).
# -LabelsOnly (hourly): labels\labels.jsonl only, logs to backup_labels.log / backup_labels.err
function Start-Backup([switch]$LabelsOnly) {
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Q (Join-Path $app 'backup_data.ps1')), '-Root', (Q $root))
    $n = 'backup'
    if ($LabelsOnly) { $a += '-LabelsOnly'; $n = 'backup_labels' } else { Log "starting nightly backup" }
    Start-Process -FilePath 'powershell.exe' -PassThru -WindowStyle Hidden -ArgumentList $a `
        -RedirectStandardOutput (Join-Path $logs "$n.log") -RedirectStandardError (Join-Path $logs "$n.err")
}

# app\daily_digest.py: the text digest of the previous local day -> <root>\reports\digest\<yyyyMMdd>.txt
function Start-Digest {
    Log "starting daily digest"
    Start-Process -FilePath $py -PassThru -WindowStyle Hidden `
        -ArgumentList @('-I', (Q (Join-Path $app 'daily_digest.py')), '--root', (Q $root), '--day', (Get-Date).AddDays(-1).ToString('yyyyMMdd', [Globalization.CultureInfo]::InvariantCulture)) `
        -RedirectStandardOutput (Join-Path $logs 'digest.log') -RedirectStandardError (Join-Path $logs 'digest.err')
}

Log ("supervisor up, app version: " + (Get-Content (Join-Path $app 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1))
[void](Write-RunEvent -Proc 'supervisor' -Ev 'supervisor_up' -ProcId $PID)
$b = $null; $c = $null; $z = $null; $d = $null; $k = $null; $kNext = [datetime]::MinValue; $u = $null; $uNext = [datetime]::MinValue
$sp = $null; $spNext = [datetime]::MinValue
$tlsNext = (Get-MonoUtcNow).AddMinutes(2)     # TLS certificate renewal check: shortly after start, then daily
$featNext = Get-MonoUtcNow                      # feature state (registry ON/OFF list): at start, then daily
$l = $null; $lStart = [datetime]::MinValue; $lNext = [datetime]::MinValue; $lDelay = 5
$cDelay = 0; $cNext = [datetime]::MinValue; $zDelay = 0; $zNext = [datetime]::MinValue; $bDelay = 0; $bNext = [datetime]::MinValue; $dDelay = 0; $dNext = [datetime]::MinValue   # restart backoff (Get-RestartDelay)
$hNext = (Get-MonoUtcNow).AddMinutes(5)         # health line every 5 minutes
$bk = $null; $bkStart = [datetime]::MinValue; $bkLast = [datetime]::MinValue; $bl = $null; $blNext = [datetime]::MinValue; $dg = $null; $dgStart = [datetime]::MinValue; $dgLast = [datetime]::MinValue
$m = $null; $mStart = [datetime]::MinValue; $mNext = (Get-MonoUtcNow).AddSeconds(30)   # ambient temperature: first run shortly after start, then every 60 min
while ($true) {
    # receivers first: they must be listening before the bridge sends
    if ($c -and $c.HasExited) {
        Register-Exit 'classifier' $c
        $cDelay = Get-RestartDelay $cDelay (Get-RunSeconds $c)
        Log (Get-ExitLine 'classifier' $c "; restart in $cDelay s")
        $cNext = (Get-MonoUtcNow).AddSeconds($cDelay); $c = $null
    }
    if (-not $c -and (Get-MonoUtcNow) -ge $cNext) {
        $c = Start-Classifier
        Register-Run 'classifier' $c
        Start-Sleep -Seconds 20   # model preload
    }
    if ($z -and $z.HasExited) {
        Register-Exit 'analysis' $z
        $zDelay = Get-RestartDelay $zDelay (Get-RunSeconds $z)
        Log (Get-ExitLine 'analysis host' $z "; restart in $zDelay s")
        $zNext = (Get-MonoUtcNow).AddSeconds($zDelay); $z = $null
    }
    if (-not $z -and (Get-MonoUtcNow) -ge $zNext) {
        $z = Start-Analyzer
        Register-Run 'analysis' $z
        Start-Sleep -Seconds 5
    }
    # the spectrum rows process is optional (needs numpy and the pinned bma_spectrum.py), retried at most once a minute; it must listen before the bridge sends
    if ((-not $sp -or $sp.HasExited) -and (Test-Path (Join-Path $app 'bma_spectrum.py')) -and (Get-Date) -ge $spNext) {
        if ($sp) { Register-Exit 'spectrum' $sp }
        if ($sp) { Log (Get-ExitLine 'spectrum rows' $sp '; see logs\spectrum.err') }
        $sp = Start-Spectrum
        Register-Run 'spectrum' $sp
        $spNext = (Get-Date).AddSeconds(60)
    }
    if ($b -and $b.HasExited) {
        Register-Exit 'bridge' $b
        $bDelay = Get-RestartDelay $bDelay (Get-RunSeconds $b)
        Log (Get-ExitLine 'bridge' $b "; restart in $bDelay s")
        $bNext = (Get-MonoUtcNow).AddSeconds($bDelay); $b = $null
    }
    if (-not $b -and (Get-MonoUtcNow) -ge $bNext) {
        $b = Start-Bridge
        Register-Run 'bridge' $b
    }
    if ($d -and $d.HasExited) {
        Register-Exit 'board_debug' $d
        $dDelay = Get-RestartDelay $dDelay (Get-RunSeconds $d)
        Log (Get-ExitLine 'board debug collector' $d "; restart in $dDelay s")
        $dNext = (Get-MonoUtcNow).AddSeconds($dDelay); $d = $null
    }
    if (-not $d -and (Get-MonoUtcNow) -ge $dNext) {
        New-Item -ItemType Directory -Force -Path (Join-Path $root 'analysis') | Out-Null
        $d = Start-BoardDebug
        Register-Run 'board_debug' $d
    }
    # the audio sink is optional (needs BMA_SINK_PYTHON and the VB-Cable device set to 16 ch) and retried at most once a minute
    if ((-not $k -or $k.HasExited) -and $sinkPy -and (Test-Path $sinkPy) -and (Get-Date) -ge $kNext) {
        if ($k) { Register-Exit 'sink' $k }
        if ($k) { Log (Get-ExitLine 'cable sink' $k '; see logs\sink.err') }
        $k = Start-CableSink
        Register-Run 'sink' $k
        $kNext = (Get-Date).AddSeconds(60)
    }
    # the dashboard is optional (needs the pinned ui\ folder) and, like the sink, retried at most once every 30 s
    if ((-not $u -or $u.HasExited) -and (Test-Path (Join-Path $app 'ui\bma_ui.py')) -and (Get-Date) -ge $uNext) {
        if ($u) { Register-Exit 'ui' $u }
        if ($u) { Log (Get-ExitLine 'dashboard' $u '; see logs\ui.err') }
        $u = Start-Ui
        Register-Run 'ui' $u
        $uNext = (Get-Date).AddSeconds(30)
    }
    # loss telemetry: optional (needs the pinned app\loss_log.py), restarted with a growing delay
    if ($l -and $l.HasExited) {
        Register-Exit 'loss_log' $l
        $lDelay = Get-LossLogDelay $lDelay ((Get-MonoUtcNow) - $lStart).TotalSeconds
        Log (Get-ExitLine 'loss log' $l "; see logs\loss_log.err; restart in $lDelay s")
        $lNext = (Get-MonoUtcNow).AddSeconds($lDelay)
        $l = $null
    }
    if (-not $l -and (Test-Path (Join-Path $app 'loss_log.py')) -and (Get-MonoUtcNow) -ge $lNext) {
        $l = Start-LossLog
        Register-Run 'loss_log' $l
        $lStart = Get-MonoUtcNow
    }
    # the ambient temperature fetch is optional (needs app\geo_county.json) and periodic, not supervised like the others: it runs
    # once an hour, is never started while the previous one still runs, and is killed after 2 minutes
    if ($m -and $m.HasExited) {
        Register-Exit 'ambient' $m
        $errTxt = Get-Content -Raw -Path (Join-Path $logs 'ambient.err') -ErrorAction SilentlyContinue
        $note = Get-AmbientNote -ExitCode $m.ExitCode -ErrText $errTxt
        if ($note) { Log $note }
        $hist = Join-Path (Join-Path $root 'history') 'ambient_temp.jsonl'
        $histOk = Test-Path $hist
        $histT = if ($histOk) { (Get-Item $hist).LastWriteTimeUtc } else { [datetime]::MinValue }
        $stale = Get-AmbientStaleNote -Exists $histOk -LastWriteUtc $histT -NowUtc (Get-MonoUtcNow)
        if ($stale) { Log $stale }
        $m = $null
    }
    $hasGeo = (Test-Path (Join-Path $app 'geo_county.json')) -and (Test-Path (Join-Path $app 'ambient_temp.py'))
    $act = Get-AmbientAction -HasGeo $hasGeo -Running ($null -ne $m) -Now (Get-MonoUtcNow) -Next $mNext -StartedAt $mStart
    if ($act -eq 'kill') {
        Log "ambient temperature fetch hung for over 2 minutes, killing it"
        try { $m.Kill() } catch { Log "could not kill the ambient fetch: $_" }
    } elseif ($act -eq 'start') {
        $m = Start-AmbientTemp
        Register-Run 'ambient' $m
        $mStart = Get-MonoUtcNow
        $mNext = $mStart.AddMinutes(60)
    }
    # nightly backup (03:00 local) and daily digest (06:00 local): optional (need the pinned app\ files), one process each, never two at once, stopped after 30 min
    foreach ($job in @(@{ n = 'backup'; f = 'backup_data.ps1'; hour = 3 }, @{ n = 'digest'; f = 'daily_digest.py'; hour = 6 })) {
        $p = if ($job.n -eq 'backup') { $bk } else { $dg }
        $st = if ($job.n -eq 'backup') { $bkStart } else { $dgStart }
        if ($p -and $p.HasExited) {
            Register-Exit $job.n $p
            if ($p.ExitCode -ne 0) { Log ("{0} exited (code {1}); see logs\{0}.err" -f $job.n, $p.ExitCode) }
            $p = $null
        } elseif ($p -and ((Get-Date) - $st).TotalMinutes -gt 30) {
            Log ("{0} ran over 30 minutes, killing it" -f $job.n)
            try { $p.Kill() } catch { Log "could not kill $($job.n): $_" }
        } elseif (-not $p -and (Test-Path (Join-Path $app $job.f)) -and -not ($job.n -eq 'backup' -and ((Get-Date) - $st).TotalMinutes -lt 30) -and (Get-DailyDue (Get-Date) $job.hour $(if ($job.n -eq 'backup') { Get-BackupLast $root } else { $dgLast }))) {
            $p = if ($job.n -eq 'backup') { Start-Backup } else { Start-Digest }
            Register-Run $job.n $p
            $st = Get-Date
            if ($job.n -eq 'backup') { $bkLast = $st } else { $dgLast = $st }
        }
        if ($job.n -eq 'backup') { $bk = $p; $bkStart = $st } else { $dg = $p; $dgStart = $st }
    }
    # hourly labels-only backup (the labels are the owner's work: a night is too long): one process, started when the previous one is done
    if ($bl -and $bl.HasExited) {
        Register-Exit 'backup_labels' $bl
        if ($bl.ExitCode -ne 0) { Log (Get-ExitLine 'labels backup' $bl '; see logs\backup_labels.err') }
        $bl = $null
    }
    if (-not $bl -and -not $bk -and (Test-Path (Join-Path $app 'backup_data.ps1')) -and (Get-MonoUtcNow) -ge $blNext) {
        $bl = Start-Backup -LabelsOnly
        Register-Run 'backup_labels' $bl
        $blNext = (Get-MonoUtcNow).AddMinutes(60)
    }
    if ((Get-MonoUtcNow) -ge $hNext) {
        $hNext = (Get-MonoUtcNow).AddMinutes(5)
        Write-HealthLine $b
    }
    if ((Get-MonoUtcNow) -ge $tlsNext) {
        $tlsNext = (Get-MonoUtcNow).AddHours(24)
        [void](Invoke-DailyTlsCheck $root $u)
    }
    if ((Get-MonoUtcNow) -ge $featNext) {
        $featNext = (Get-MonoUtcNow).AddHours(24)
        [void](Invoke-FeatureState $root)
    }
    Start-Sleep -Seconds 10
}
