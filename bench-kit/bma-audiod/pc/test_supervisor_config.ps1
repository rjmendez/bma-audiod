# Tests for the configuration part of run_pipeline.ps1 (paths derived from the script's own location, bma.local.ps1,
# BMA_* settings) WITHOUT starting any process: the real script is parsed, its leading settings statements and its
# helper functions (Q, Resolve-BoardIp, ConvertTo-WslPath) are cut out of the real file by the PowerShell parser and run
# against a scratch directory. Only Get-NetNeighbor (the OS neighbour table) is replaced by a canned one and Log by a
# recorder; nothing here starts the supervisor, WSL services or touches a live data dir.
#   powershell.exe -NoProfile -File test_supervisor_config.ps1 -Work <empty temp directory outside the repo>
# Exits 0 when every check passes, 1 otherwise.
param([Parameter(Mandatory = $true)][string]$Work)
$ErrorActionPreference = 'Stop'
$script:n = 0; $script:bad = 0; $script:skipped = 0
function Check([string]$name, $ok, [string]$detail = '') {
    $script:n++
    if ($ok) { Write-Host "PASS $name" } else { $script:bad++; Write-Host "FAIL $name  [$detail]" }
}

function Skip-Check([string]$name, [string]$why) { $script:skipped++; Write-Host "SKIP ${name}: $why" }
# $true only when wsl.exe converts the Windows directory $Path to its /mnt/<drive>/... form (a distro is installed and works); $Invoke runs the wsl call
function Test-WslConverts([scriptblock]$Invoke = { param($p) & wsl.exe -e wslpath -a -u $p 2>$null }, [string]$Path = $env:SystemRoot) {
    $want = '/mnt/' + $Path.Substring(0, 1).ToLower() + '/' + $Path.Substring(3).Replace('\', '/')
    try { $r = & $Invoke $Path } catch { return $false }
    return ("$(@($r)[0])".Trim() -ceq $want)
}

$src = Join-Path $PSScriptRoot 'run_pipeline.ps1'
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($src, [ref]$tokens, [ref]$errs)
if ($errs.Count) { Write-Host "FAIL run_pipeline.ps1 does not parse: $($errs[0].Message)"; exit 1 }
$stmts = $ast.EndBlock.Statements
$firstTry = 0
for ($i = 0; $i -lt $stmts.Count; $i++) { if ($stmts[$i] -is [System.Management.Automation.Language.TryStatementAst]) { $firstTry = $i; break } }
$headText = ($stmts[0..($firstTry - 1)] | ForEach-Object { $_.Extent.Text }) -join "`n"
$funcs = @{}
foreach ($f in $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) { $funcs[$f.Name] = $f.Extent.Text }

. (Join-Path $PSScriptRoot 'run_ledger.ps1')    # the real Rotate-IfBig, as the supervisor has it
$capsStmt = $ast.FindAll({ param($a) $a -is [System.Management.Automation.Language.AssignmentStatementAst] -and $a.Left.Extent.Text -ceq '$script:ChildLogCaps' }, $true) | Select-Object -First 1
if ($capsStmt) { Invoke-Expression $capsStmt.Extent.Text }

New-Item -ItemType Directory -Force -Path $Work | Out-Null
$vars = 'BMA_PYTHON', 'BMA_SINK_PYTHON', 'BMA_BOARD', 'BMA_DSPD_TOKEN_FILE', 'BMA_MODEL_DIR', 'BMA_DATA_DIR', 'BMA_BEAM_SHIFT'
$saved = @{}; foreach ($v in $vars) { $saved[$v] = [Environment]::GetEnvironmentVariable($v) }
function Reset-Env { foreach ($v in $vars) { [Environment]::SetEnvironmentVariable($v, $null) } }

# run the settings head with $PSScriptRoot = a fresh scratch dir; returns a hashtable of what it set
function Run-Head([string]$dir, [string]$localText) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    if ($localText) { Set-Content -Path (Join-Path $dir 'bma.local.ps1') -Value $localText -Encoding ASCII }
    Set-Content -Path (Join-Path $dir 'head.ps1') -Value ($headText + "`n[pscustomobject]@{ root = `$root; app = `$app; logs = `$logs; reports = `$reports; py = `$py; sinkPy = `$sinkPy; wslPy = `$wslPy }") -Encoding ASCII
    return (& (Join-Path $dir 'head.ps1'))
}

$pyOnPath = (Get-Command python -ErrorAction SilentlyContinue).Source

# 1. nothing configured: everything derives from the script's own directory
Reset-Env
$d1 = Join-Path $Work 'one'
$r = Run-Head $d1 ''
Check 'no config: root is the directory the script lives in' ($r.root -eq $d1) "$($r.root)"
Check 'no config: app, logs and reports sit under it' (($r.app -eq (Join-Path $d1 'app')) -and ($r.logs -eq (Join-Path $d1 'logs')) -and ($r.reports -eq (Join-Path $d1 'reports'))) "$($r.app) $($r.logs) $($r.reports)"
Check 'no config: logs and reports directories were created' ((Test-Path (Join-Path $d1 'logs')) -and (Test-Path (Join-Path $d1 'reports')))
Check 'no config: BMA_DATA_DIR handed to the children is the root' ([Environment]::GetEnvironmentVariable('BMA_DATA_DIR') -eq $d1) ([Environment]::GetEnvironmentVariable('BMA_DATA_DIR'))
Check 'no config: python is the first python on PATH' ($r.py -eq $pyOnPath) "$($r.py) vs $pyOnPath"
Check 'no config: the cable sink interpreter is unset (the sink then does not start)' (-not $r.sinkPy)
Check 'no config: no WSL python (the launchers use python3)' (-not $r.wslPy)

# 2. the local override file
Reset-Env
$d2 = Join-Path $Work 'two'
$local = "`$BMA_PYTHON = 'py-main'`n`$BMA_SINK_PYTHON = 'py-sink'`n`$BMA_BOARD = '192.0.2.7'`n`$BMA_DSPD_TOKEN_FILE = 'tok-file'`n`$BMA_WSL_PYTHON = 'wsl-py'"
$r = Run-Head $d2 $local
Check 'bma.local.ps1: BMA_PYTHON, BMA_SINK_PYTHON and BMA_WSL_PYTHON are used' (($r.py -eq 'py-main') -and ($r.sinkPy -eq 'py-sink') -and ($r.wslPy -eq 'wsl-py')) "$($r.py) $($r.sinkPy) $($r.wslPy)"
Check 'bma.local.ps1: the reports dir is not configurable, it stays <root>\reports' ($r.reports -eq (Join-Path $d2 'reports')) "$($r.reports)"
Check 'bma.local.ps1: BMA_BOARD and BMA_DSPD_TOKEN_FILE reach the environment of the children' (([Environment]::GetEnvironmentVariable('BMA_BOARD') -eq '192.0.2.7') -and ([Environment]::GetEnvironmentVariable('BMA_DSPD_TOKEN_FILE') -eq 'tok-file'))
Check 'bma.local.ps1: the root is still the script directory (no setting moves it)' ($r.root -eq $d2) "$($r.root)"

# 3. the environment is the fallback when there is no local file
Reset-Env
[Environment]::SetEnvironmentVariable('BMA_PYTHON', 'py-env'); [Environment]::SetEnvironmentVariable('BMA_SINK_PYTHON', 'sink-env')
$r = Run-Head (Join-Path $Work 'three') ''
Check 'environment: BMA_PYTHON and BMA_SINK_PYTHON are read when no local file sets them' (($r.py -eq 'py-env') -and ($r.sinkPy -eq 'sink-env')) "$($r.py) $($r.sinkPy)"
Reset-Env
[Environment]::SetEnvironmentVariable('BMA_PYTHON', 'py-env')
$r = Run-Head (Join-Path $Work 'four') "`$BMA_PYTHON = 'py-local'"
Check 'the local file beats the environment' ($r.py -eq 'py-local') "$($r.py)"

# 3b. the shipped template is a no-op when copied verbatim (every line commented out): same result as no file at all
Reset-Env
$tpl = Get-Content -Raw -Path (Join-Path $PSScriptRoot 'bma.local.example.ps1')
$r = Run-Head (Join-Path $Work 'tpl') $tpl
Check 'the example template copied verbatim sets nothing (python falls back to PATH, no sink, no WSL python)' (($r.py -eq $pyOnPath) -and (-not $r.sinkPy) -and (-not $r.wslPy)) "$($r.py) $($r.sinkPy) $($r.wslPy)"
Check 'the example template copied verbatim puts no board or token file in the environment' ((-not [Environment]::GetEnvironmentVariable('BMA_BOARD')) -and (-not [Environment]::GetEnvironmentVariable('BMA_DSPD_TOKEN_FILE'))) ''
Reset-Env
$tplOn = ($tpl -split "`n" | ForEach-Object { $_ -replace '^#(?=\$)', '' }) -join "`n"
$r = Run-Head (Join-Path $Work 'tpl2') $tplOn
Check 'template: BMA_BEAM_SHIFT is documented and uncommenting it yields a vector Get-BeamShift would pass' (($tpl -match '(?m)^#\$BMA_BEAM_SHIFT\s*=\s*''1,0,0,0,0,0,0,-1''') -and ($tplOn -match '(?m)^\$BMA_BEAM_SHIFT')) ''
Check 'positive twin: the same template with its lines uncommented DOES set the values' (($r.py -eq '<path to python.exe>') -and ($r.wslPy -eq '/path/in/wsl/bin/python') -and ([Environment]::GetEnvironmentVariable('BMA_BOARD') -eq '192.0.2.10')) "$($r.py) $($r.wslPy)"

# 4. helper functions, taken from the real file
$script:logged = @()
function Log([string]$m) { $script:logged += $m }
foreach ($name in 'Q', 'Setting', 'Resolve-BoardIp', 'ConvertTo-WslPath', 'Get-TailnetName', 'Get-BeamShift', 'Get-WslLauncherTail', 'Get-ClipAudio', 'Get-AnalyzerArgs', 'Start-WslLauncher', 'Start-Analyzer', 'Rotate-ChildLogs') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
$script:wslCache = @{}
Check 'Q wraps a value in double quotes' ((Q 'a b') -eq '"a b"') (Q 'a b')

Reset-Env
Check 'Resolve-BoardIp: nothing configured -> empty' ((Resolve-BoardIp) -eq '')
[Environment]::SetEnvironmentVariable('BMA_BOARD', '192.0.2.7')
Check 'Resolve-BoardIp: an IP address is returned as it is' ((Resolve-BoardIp) -eq '192.0.2.7') (Resolve-BoardIp)
function Get-NetNeighbor { param($AddressFamily, $ErrorAction)
    @([pscustomobject]@{ IPAddress = '192.0.2.9'; LinkLayerAddress = '00-00-5E-00-53-09' }, [pscustomobject]@{ IPAddress = '192.0.2.77'; LinkLayerAddress = '00-00-5E-00-53-01' }) }
[Environment]::SetEnvironmentVariable('BMA_BOARD', '00:00:5e:00:53:01')
Check 'Resolve-BoardIp: a MAC (colons, lower case) is looked up in the neighbour table' ((Resolve-BoardIp) -eq '192.0.2.77') (Resolve-BoardIp)
[Environment]::SetEnvironmentVariable('BMA_BOARD', '00-00-5E-00-53-55')
Check 'Resolve-BoardIp: a MAC that no neighbour has -> empty (positive twin of the lookup above)' ((Resolve-BoardIp) -eq '') (Resolve-BoardIp)

# BMA_BEAM_SHIFT -> the analyzer's --beam-shift value (the real Get-BeamShift reads it through the real Setting)
Reset-Env
$script:logged = @()
Check 'Get-BeamShift: unset -> empty and nothing logged (no flag is passed)' (((Get-BeamShift) -eq '') -and ($script:logged.Count -eq 0)) "[$(Get-BeamShift)]"
[Environment]::SetEnvironmentVariable('BMA_BEAM_SHIFT', '1,0,0,0,0,0,0,-1')
Check 'Get-BeamShift: a valid vector is returned exactly' ((Get-BeamShift) -ceq '1,0,0,0,0,0,0,-1') (Get-BeamShift)
$BMA_BEAM_SHIFT = '-4,4,0,0,0,0,0,0'
Check 'Get-BeamShift: the local-file variable beats the environment, limits +-4 accepted' ((Get-BeamShift) -ceq '-4,4,0,0,0,0,0,0') (Get-BeamShift)
Remove-Variable BMA_BEAM_SHIFT
foreach ($badv in '1,0,0', '5,0,0,0,0,0,0,0', '1,0,0,0,0,0,0,x', '1.5,0,0,0,0,0,0,0', '1,0,0,0,0,0,0,0,0') {
    $script:logged = @()
    [Environment]::SetEnvironmentVariable('BMA_BEAM_SHIFT', $badv)
    Check "Get-BeamShift: '$badv' is not passed and is logged" (((Get-BeamShift) -eq '') -and ($script:logged.Count -eq 1) -and ($script:logged[0] -like "*'$badv' ignored*")) "[$(Get-BeamShift)] $($script:logged -join '|')"
}
Reset-Env
Check 'Get-WslLauncherTail: no python, no extra -> bash script port (the classifier / unset case, exact)' ((((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '' '') -join ' ') -ceq 'bash "/a/app/run_analyzer.sh" 48300')) ((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '' '') -join ' ')
Check 'Get-WslLauncherTail: python and extra -> both, in order' ((((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '/v/py' '1,0,0,0,0,0,0,-1') -join ' ') -ceq 'bash "/a/app/run_analyzer.sh" 48300 "/v/py" "1,0,0,0,0,0,0,-1"')) ((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '/v/py' '1,0,0,0,0,0,0,-1') -join ' ')
Check 'Get-WslLauncherTail: extra without python fills the python slot with python3' ((((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '' '1,0,0,0,0,0,0,-1') -join ' ') -ceq 'bash "/a/app/run_analyzer.sh" 48300 python3 "1,0,0,0,0,0,0,-1"')) ((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '' '1,0,0,0,0,0,0,-1') -join ' ')
Check 'Get-WslLauncherTail: python only -> no extra argument' ((((Get-WslLauncherTail '/a/app' 'run_classifier.sh' 48200 '/v/py' '') -join ' ') -ceq 'bash "/a/app/run_classifier.sh" 48200 "/v/py"')) ((Get-WslLauncherTail '/a/app' 'run_classifier.sh' 48200 '/v/py' '') -join ' ')

# enable_clip_audio.txt -> run_analyzer.sh's 4th positional (--clip-audio). The real Get-ClipAudio, Start-WslLauncher and Start-Analyzer run;
# only wsl.exe and Start-Process are replaced by recorders (wslpath answers /w/<path>), nothing is started.
Check 'Get-WslLauncherTail: clip-audio only -> python3, an empty beam-shift slot, then 1 (the 4th positional keeps the 3rd slot)' ((((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '' '' '1') -join ' ') -ceq 'bash "/a/app/run_analyzer.sh" 48300 python3 "" "1"')) ((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '' '' '1') -join ' ')
Check 'Get-WslLauncherTail: python, beam shift and clip-audio -> all three in order' ((((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '/v/py' '1,0,0,0,0,0,0,-1' '1') -join ' ') -ceq 'bash "/a/app/run_analyzer.sh" 48300 "/v/py" "1,0,0,0,0,0,0,-1" "1"')) ((Get-WslLauncherTail '/a/app' 'run_analyzer.sh' 48300 '/v/py' '1,0,0,0,0,0,0,-1' '1') -join ' ')
$script:wslCalls = @(); $script:started = @()
function wsl.exe { $script:wslCalls += ,@($args); $global:LASTEXITCODE = 0; if ($args -contains 'wslpath') { return ('/w/' + (($args[-1]) -replace '[:\\]+', '/')) } }
function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList) $script:started += ,@($ArgumentList) }
$root = Join-Path $Work 'clipaudio'; $app = Join-Path $root 'app'; $logs = Join-Path $root 'logs'; $aport = 48300; $wslPy = ''
New-Item -ItemType Directory -Force -Path $root | Out-Null
Reset-Env
$script:wslCache = @{}
$wr = '/w/' + ($root -replace '[:\\]+', '/'); $wa = '/w/' + ($app -replace '[:\\]+', '/')
$base = "-e `"env`" `"BMA_DATA_DIR=$wr`" bash `"$wa/run_analyzer.sh`" 48300"
$script:logged = @(); $script:started = @()
Check 'Get-ClipAudio: no marker file -> empty and nothing logged' (((Get-ClipAudio) -eq '') -and ($script:logged.Count -eq 0)) "[$(Get-ClipAudio)]"
$null = Start-Analyzer
Check 'Start-Analyzer without the marker: the wsl.exe arguments are exactly today''s (no python slot, no 4th argument)' (($script:started.Count -eq 1) -and ((($script:started[0]) -join ' ') -ceq $base)) (($script:started | ForEach-Object { $_ -join ' ' }) -join ' | ')
Set-Content -Path (Join-Path $root 'enable_clip_audio.txt') -Value '' -Encoding ASCII
$script:logged = @()
Check 'Get-ClipAudio: marker file present -> 1 and the line "clip audio ON (enable_clip_audio.txt)" logged once' (((Get-ClipAudio) -ceq '1') -and ($script:logged.Count -eq 1) -and ($script:logged[0] -ceq 'clip audio ON (enable_clip_audio.txt)')) "$($script:logged -join '|')"
$script:started = @(); $script:logged = @()
$null = Start-Analyzer
Check 'Start-Analyzer with the marker: python3, an empty beam-shift slot and 1 are appended' (($script:started.Count -eq 1) -and ((($script:started[0]) -join ' ') -ceq ($base + ' python3 "" "1"'))) (($script:started | ForEach-Object { $_ -join ' ' }) -join ' | ')
Check 'Start-Analyzer with the marker logs the clip-audio line' (($script:logged | Where-Object { $_ -ceq 'clip audio ON (enable_clip_audio.txt)' }).Count -eq 1) "$($script:logged -join '|')"
Remove-Item (Join-Path $root 'enable_clip_audio.txt')
$script:started = @()
$null = Start-Analyzer
Check 'positive twin: after the marker is removed the next start is back to today''s arguments (the marker is read at every start)' (($script:started.Count -eq 1) -and ((($script:started[0]) -join ' ') -ceq $base)) (($script:started | ForEach-Object { $_ -join ' ' }) -join ' | ')
# analyzer_args.txt -> the arguments after the 4th positional, appended to host.py by run_analyzer.sh
$af = Join-Path $root 'analyzer_args.txt'
$script:logged = @()
Check 'Get-AnalyzerArgs: no file -> no arguments and nothing logged' ((@(Get-AnalyzerArgs).Count -eq 0) -and ($script:logged.Count -eq 0))
$pm = 'D:' + [char]92 + 'models' + [char]92 + 'p m' + [char]92 + 'x.onnx'
Set-Content -Path $af -Encoding ASCII -Value @('# comment', '', '  --perch-model  ', $pm, '--label', 'a b', 'bad"quote', ('C:' + [char]92 + 'dir' + [char]92), '--flag=1', '   ')
$script:logged = @()
$ga = @(Get-AnalyzerArgs)
Check 'Get-AnalyzerArgs: comments, blanks, bad lines dropped; a Windows path becomes its WSL path, spaces kept; one line = one token' (($ga -join '|') -ceq ('--perch-model|/w/D/models/p m/x.onnx|--label|a b|--flag=1')) ($ga -join '|')
Check 'Get-AnalyzerArgs: logs only the counts (5 accepted, 2 skipped), no value' (($script:logged.Count -eq 1) -and ($script:logged[0] -ceq 'analyzer_args.txt: 5 arg(s) accepted, 2 skipped')) ($script:logged -join '|')
$script:started = @()
$null = Start-Analyzer
Check 'Start-Analyzer with the file: python3, empty beam-shift and clip-audio slots, then each token quoted' (($script:started.Count -eq 1) -and ((($script:started[0]) -join ' ') -ceq ($base + ' python3 "" "" "--perch-model" "/w/D/models/p m/x.onnx" "--label" "a b" "--flag=1"'))) (($script:started | ForEach-Object { $_ -join ' ' }) -join ' | ')
Set-Content -Path (Join-Path $root 'enable_clip_audio.txt') -Value '' -Encoding ASCII
$script:started = @()
$null = Start-Analyzer
Check 'Start-Analyzer with both files: 1 in the clip-audio slot, tokens after it' (($script:started.Count -eq 1) -and ((($script:started[0]) -join ' ') -ceq ($base + ' python3 "" "1" "--perch-model" "/w/D/models/p m/x.onnx" "--label" "a b" "--flag=1"'))) (($script:started | ForEach-Object { $_ -join ' ' }) -join ' | ')
Remove-Item (Join-Path $root 'enable_clip_audio.txt')
Remove-Item $af
$script:started = @()
$null = Start-Analyzer
Check 'positive twin: with the file removed the next start is back to today''s arguments' (($script:started.Count -eq 1) -and ((($script:started[0]) -join ' ') -ceq $base)) (($script:started | ForEach-Object { $_ -join ' ' }) -join ' | ')
Remove-Item Function:\wsl.exe, Function:\Start-Process

# Get-TailnetName: the tailscale executable is replaced by a canned .cmd (a function shadows the Get-Command lookup of the name)
$script:tsExe = Join-Path $Work 'ts.cmd'
function Get-Command { param($Name, $ErrorAction) if ($Name -eq 'tailscale') { if (Test-Path $script:tsExe) { return [pscustomobject]@{ Source = $script:tsExe } } else { return $null } } else { Microsoft.PowerShell.Core\Get-Command @PSBoundParameters } }
Set-Content -Path $script:tsExe -Encoding ASCII -Value '@echo {"Self":{"DNSName":"Node-1.example.invalid."}}'
Check 'Get-TailnetName: the node DNS name, lower case, without the trailing dot' ((Get-TailnetName) -ceq 'node-1.example.invalid') (Get-TailnetName)
Set-Content -Path $script:tsExe -Encoding ASCII -Value '@echo not json'
Check 'Get-TailnetName: unparseable tailscale output -> empty' ((Get-TailnetName) -eq '') (Get-TailnetName)
Remove-Item $script:tsExe
Check 'Get-TailnetName: tailscale missing -> empty' ((Get-TailnetName) -eq '') (Get-TailnetName)
Remove-Item Function:\Get-Command

# 5. hourly ambient temperature fetch: the scheduling decision (pure function cut out of the real file) and the command line it
# builds (Start-Process replaced by a recorder: no process is started)
if (-not $funcs.ContainsKey('Get-AmbientAction')) { Check 'run_pipeline.ps1 defines Get-AmbientAction' $false } else {
    Invoke-Expression $funcs['Get-AmbientAction']
    $t0 = [datetime]'2026-10-26T12:00:00'
    $due = $t0.AddMinutes(-1)
    Check 'ambient: configured, nothing running, due -> start' ((Get-AmbientAction -HasGeo $true -Running $false -Now $t0 -Next $due -StartedAt ([datetime]::MinValue)) -eq 'start')
    Check 'ambient: configured, nothing running, exactly at the due time -> start' ((Get-AmbientAction -HasGeo $true -Running $false -Now $t0 -Next $t0 -StartedAt ([datetime]::MinValue)) -eq 'start')
    Check 'ambient: configured, nothing running, one second before the due time -> none' ((Get-AmbientAction -HasGeo $true -Running $false -Now $t0.AddSeconds(-1) -Next $t0 -StartedAt ([datetime]::MinValue)) -eq 'none')
    Check 'ambient: no geo config (or no tool): due but never started -> none' ((Get-AmbientAction -HasGeo $false -Running $false -Now $t0 -Next $due -StartedAt ([datetime]::MinValue)) -eq 'none')
    Check 'ambient: an instance running for 30 s while the next run is due -> wait (never two at once)' ((Get-AmbientAction -HasGeo $true -Running $true -Now $t0.AddSeconds(30) -Next $due -StartedAt $t0) -eq 'wait')
    Check 'ambient: an instance running for exactly 120 s -> wait' ((Get-AmbientAction -HasGeo $true -Running $true -Now $t0.AddSeconds(120) -Next $due -StartedAt $t0) -eq 'wait')
    Check 'ambient: an instance running for 121 s -> kill' ((Get-AmbientAction -HasGeo $true -Running $true -Now $t0.AddSeconds(121) -Next $due -StartedAt $t0) -eq 'kill')
    Check 'ambient: a hung instance is killed even when the geo config has been removed meanwhile' ((Get-AmbientAction -HasGeo $false -Running $true -Now $t0.AddSeconds(500) -Next $due -StartedAt $t0) -eq 'kill')
    # the loop's own bookkeeping, taken from the real file: the first run is shortly after start, the next one 60 minutes after a start
    $initOk = $false; $nextOk = $false
    foreach ($a in $ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        if ($a.Left.Extent.Text -eq '$mNext' -and $a.Right.Extent.Text -match 'AddSeconds\(30\)') { $initOk = $true }
        if ($a.Left.Extent.Text -eq '$mNext' -and $a.Right.Extent.Text -match '\$mStart\.AddMinutes\(60\)') { $nextOk = $true }
    }
    Check 'ambient: the first run is scheduled 30 s after the supervisor starts, later ones 60 minutes after each start' ($initOk -and $nextOk) "$initOk $nextOk"
    $mNext = $t0
    $starts = @()
    for ($min = 0; $min -le 130; $min++) {
        $now = $t0.AddMinutes($min)
        if ((Get-AmbientAction -HasGeo $true -Running $false -Now $now -Next $mNext -StartedAt $t0) -eq 'start') { $starts += $min; $mNext = $now.AddMinutes(60) }
    }
    Check 'ambient: simulated over 130 minutes the fetch starts at minutes 0, 60 and 120 and at no other minute' (($starts -join ',') -eq '0,60,120') ($starts -join ',')

    # the loop's clock is UTC and monotonic, not the local Get-Date (DST fall-back and clock steps would shift the schedule)
    foreach ($name in 'Get-MonoUtcNow', 'Get-AmbientNote', 'Get-AmbientStaleNote') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
    $c1 = Get-MonoUtcNow; $c2 = Get-MonoUtcNow
    Check 'ambient clock: UTC kind, never goes backwards, and agrees with the UTC wall clock to 5 s' (($c1.Kind -eq 'Utc') -and ($c2 -ge $c1) -and ([math]::Abs(([datetime]::UtcNow - $c2).TotalSeconds) -lt 5)) "$c1 $c2"
    $ticks = @(); foreach ($x in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -in '$mNext', '$mStart' }, $true)) { $ticks += $x.Right.Extent.Text }
    Check 'ambient loop: $mNext and $mStart are never taken from the local Get-Date (4 assignments: the MinValue init, two from Get-MonoUtcNow, one from $mStart)' (($ticks.Count -eq 4) -and (@($ticks | Where-Object { $_ -match 'Get-Date' }).Count -eq 0) -and (@($ticks | Where-Object { $_ -match 'Get-MonoUtcNow|\$mStart' }).Count -eq 3)) ($ticks -join ' | ')
    $call = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-AmbientAction' }, $true)
    Check 'ambient loop: the one Get-AmbientAction call passes -Now (Get-MonoUtcNow)' (($call.Count -eq 1) -and ($call[0].Extent.Text -match '-Now \(Get-MonoUtcNow\)')) ($call | ForEach-Object { $_.Extent.Text })
    foreach ($fn in 'Get-AmbientNote', 'Get-AmbientStaleNote') {
        $calls = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq $fn }, $true)
        Check "ambient loop: the exit handler calls $fn once, so a failure or a stale feed reaches supervisor.log" ($calls.Count -eq 1) $calls.Count
    }
    # DST fall-back on 2026-11-01 as the supervisor now sees it (UTC): start 05:30Z; 61 real minutes later the next fetch is due
    $s0 = [datetime]::SpecifyKind([datetime]'2026-11-01T05:30:00', 'Utc')
    Check 'ambient: across the US fall-back (UTC clock) a fetch started at 05:30Z is due again at 06:31Z, 61 real minutes later' ((Get-AmbientAction -HasGeo $true -Running $false -Now $s0.AddMinutes(61) -Next $s0.AddMinutes(60) -StartedAt $s0) -eq 'start') ''
    Check 'ambient: a fetch started at 05:30Z is killed 121 s later, not waited for' ((Get-AmbientAction -HasGeo $true -Running $true -Now $s0.AddSeconds(121) -Next $s0.AddMinutes(60) -StartedAt $s0) -eq 'kill') ''

    # failures reach supervisor.log: the tool exits 0 and leaves one stderr line that the next run overwrites
    Check 'ambient note: exit 0 and empty stderr -> nothing to log' ($null -eq (Get-AmbientNote -ExitCode 0 -ErrText "  `r`n")) ''
    Check 'ambient note: exit 0 but a failure line -> that line, trimmed' ((Get-AmbientNote -ExitCode 0 -ErrText "ambient_temp: failures: timeouts=1`r`n") -ceq 'ambient temperature fetch reported: ambient_temp: failures: timeouts=1') (Get-AmbientNote -ExitCode 0 -ErrText "ambient_temp: failures: timeouts=1`r`n")
    Check 'ambient note: a nonzero exit code with a usage error -> code and message' ((Get-AmbientNote -ExitCode 2 -ErrText 'error: bad') -ceq 'ambient temperature fetch exited (code 2): error: bad') (Get-AmbientNote -ExitCode 2 -ErrText 'error: bad')
    Check 'ambient note: a long stderr is cut to 300 characters' ((Get-AmbientNote -ExitCode 0 -ErrText ('x' * 1000)).Length -eq ('ambient temperature fetch reported: '.Length + 300)) ''
    $nowU = [datetime]::SpecifyKind([datetime]'2026-10-26T12:00:00', 'Utc')
    Check 'ambient stale: no history file -> a line' ((Get-AmbientStaleNote -Exists $false -LastWriteUtc ([datetime]::MinValue) -NowUtc $nowU) -ceq 'ambient temperature feed has no history file yet') ''
    Check 'ambient stale: newest write 3.5 h ago -> a line with the age' ((Get-AmbientStaleNote -Exists $true -LastWriteUtc $nowU.AddHours(-3.5) -NowUtc $nowU) -ceq 'ambient temperature feed is stale: newest record written 3.5 h ago') (Get-AmbientStaleNote -Exists $true -LastWriteUtc $nowU.AddHours(-3.5) -NowUtc $nowU)
    Check 'ambient stale: positive twin, newest write 2.9 h ago (and exactly 3 h) -> nothing' (($null -eq (Get-AmbientStaleNote -Exists $true -LastWriteUtc $nowU.AddHours(-2.9) -NowUtc $nowU)) -and ($null -eq (Get-AmbientStaleNote -Exists $true -LastWriteUtc $nowU.AddHours(-3) -NowUtc $nowU))) ''

    # the command line: python from $py, isolated mode, geo config and out dir derived from the app and data dirs, logs in <root>\logs
    foreach ($name in 'Start-AmbientTemp') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
    $script:started = @()
    function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError)
        $script:started += [pscustomobject]@{ File = $FilePath; Args = $ArgumentList; Out = $RedirectStandardOutput; Err = $RedirectStandardError } }
    $py = 'py-amb'; $app = Join-Path $Work 'amb\app'; $root = Join-Path $Work 'amb'; $logs = Join-Path $Work 'amb\logs'
    Start-AmbientTemp
    $s = $script:started
    $wantArgs = @('-I', ('"' + (Join-Path $app 'ambient_temp.py') + '"'), '--once', '--geo-config', ('"' + (Join-Path $app 'geo_county.json') + '"'), '--out', ('"' + (Join-Path $root 'history') + '"'))
    Check 'ambient: exactly one process is started, with the bridge python and the exact arguments (isolated, --once, geo config in app, out = <root>\history)' (($s.Count -eq 1) -and ($s[0].File -eq 'py-amb') -and (($s[0].Args -join '|') -eq ($wantArgs -join '|'))) (($s | ForEach-Object { $_.Args }) -join ' ')
    Check 'ambient: stdout goes to <root>\logs\ambient.log and stderr to ambient.err' (($s[0].Out -eq (Join-Path $logs 'ambient.log')) -and ($s[0].Err -eq (Join-Path $logs 'ambient.err'))) "$($s[0].Out) $($s[0].Err)"
    Remove-Item Function:\Start-Process
}

# the skip logic itself: recording stubs stand in for the wsl call
$script:probeCalls = @()
$pp = Join-Path $Work 'probe_dir'
$mnt = { param($p) '/mnt/' + $p.Substring(0, 1).ToLower() + '/' + $p.Substring(3).Replace('\', '/') }
$rOk = Test-WslConverts -Path $pp { param($p) $script:probeCalls += "ok:$p"; & $mnt $p }
$rNo = Test-WslConverts -Path $pp { param($p) $script:probeCalls += 'none'; $null }
$rJunk = Test-WslConverts -Path $pp { param($p) $script:probeCalls += 'junk'; 'Windows Subsystem for Linux has no installed distributions.' }
$rThrow = Test-WslConverts -Path $pp { param($p) $script:probeCalls += 'throw'; throw 'wsl.exe not found' }
$rWrong = Test-WslConverts -Path $pp { param($p) $script:probeCalls += 'wrong'; (& $mnt $p) + '/other' }
Check 'wsl probe: the exact converted path -> true; empty output, a no-distro message, a different path and an exception -> false; each stub called exactly once, with the path' (($rOk -eq $true) -and ($rNo -eq $false) -and ($rJunk -eq $false) -and ($rThrow -eq $false) -and ($rWrong -eq $false) -and (($script:probeCalls -join ',') -ceq ("ok:$pp,none,junk,throw,wrong"))) "$rOk $rNo $rJunk $rThrow $rWrong $($script:probeCalls -join ',')"
$s0 = $script:skipped; $n0 = $script:n
Skip-Check 'Skip-Check self-test (not a skipped check)' 'accounting probe'
Check 'skip accounting: a skip is counted as skipped, not as a check and not as a failure' (($script:skipped -eq ($s0 + 1)) -and ($script:n -eq $n0) -and ($script:bad -eq 0)) "$($script:skipped) $($script:n) $($script:bad)"
$script:skipped = $s0

if (Test-WslConverts) {
    $w = ConvertTo-WslPath $Work
    $drive = $Work.Substring(0, 1).ToLower()
    $want = '/mnt/' + $drive + '/' + $Work.Substring(3).Replace('\', '/')
    Check 'ConvertTo-WslPath: a Windows path becomes its /mnt/<drive>/ form' ($w -eq $want) "$w vs $want"
    $before = $script:logged.Count
    Check 'ConvertTo-WslPath: the answer is cached (a second call logs nothing and returns the same)' (((ConvertTo-WslPath $Work) -eq $w) -and ($script:logged.Count -eq $before))
} else {
    Skip-Check 'ConvertTo-WslPath: a Windows path becomes its /mnt/<drive>/ form' 'no WSL distro'
    Skip-Check 'ConvertTo-WslPath: the answer is cached (a second call logs nothing and returns the same)' 'no WSL distro'
}

# 5. the dashboard login token (Initialize-DashboardToken) and the way Start-Ui passes it; Start-Process is replaced by a recorder
foreach ($name in 'Initialize-DashboardToken', 'Get-TlsFiles', 'Get-UiOptionalArgs', 'Invoke-TlsRenewal', 'Invoke-DailyTlsCheck', 'Start-Ui', 'Rotate-ChildLogs') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
$tokDir = Join-Path $Work 'tok'
New-Item -ItemType Directory -Force -Path $tokDir | Out-Null
$tokA = Join-Path $tokDir 'a.txt'
$before = $script:logged.Count
$created = Initialize-DashboardToken $tokA
$bytesA = [System.IO.File]::ReadAllBytes($tokA)
$textA = [System.Text.Encoding]::ASCII.GetString($bytesA)
Check 'token: a missing file is created and the function says so' ($created -eq $true) "$created"
Check 'token: exactly 48 lowercase hex characters as bytes: no BOM, no trailing newline' (($bytesA.Length -eq 48) -and ($textA -cmatch '^[0-9a-f]{48}$')) "$($bytesA.Length) bytes"
$added = @($script:logged | Select-Object -Skip $before)
Check 'token: exactly one log line, naming the file and saying created, without the token' (($added.Count -eq 1) -and $added[0].Contains($tokA) -and $added[0].Contains('created') -and -not $added[0].Contains($textA)) ($added -join '|')
$fi = New-Object System.IO.FileInfo $tokA
$acl = if ($fi.PSObject.Methods['GetAccessControl']) { $fi.GetAccessControl() } else { [System.IO.FileSystemAclExtensions]::GetAccessControl($fi) }   # Get-Acl needs a module this host may fail to load
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.NTAccount]))
Check 'token: inheritance is removed and the only access rule is the current user with full control' (($acl.AreAccessRulesProtected) -and ($rules.Count -eq 1) -and ($rules[0].IdentityReference.Value -eq $me) -and ($rules[0].FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::FullControl) -eq [System.Security.AccessControl.FileSystemRights]::FullControl -and ($rules[0].AccessControlType -eq 'Allow')) "protected=$($acl.AreAccessRulesProtected) rules=$($rules.Count) $($rules | ForEach-Object { $_.IdentityReference.Value })"
$before = $script:logged.Count
$again = Initialize-DashboardToken $tokA
Check 'token: positive twin - an existing file is returned as it is: false, bytes unchanged, nothing logged' (($again -eq $false) -and ([System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($tokA)) -eq [System.Convert]::ToBase64String($bytesA)) -and ($script:logged.Count -eq $before))
$tokB = Join-Path $tokDir 'b.txt'
[void](Initialize-DashboardToken $tokB)
Check 'token: two files get different tokens' ([System.IO.File]::ReadAllText($tokB) -ne $textA)
function icacls.exe { cmd /c exit 5 }                                       # a failing icacls: the OS tool is replaced, the function under test is real
$tokC = Join-Path $tokDir 'c.txt'
$before = $script:logged.Count
$failed = Initialize-DashboardToken $tokC
Remove-Item Function:\icacls.exe
$addedC = @($script:logged | Select-Object -Skip $before)
Check 'token: when the permissions cannot be restricted no file is left behind (fail closed), false, one log line saying so' (($failed -eq $false) -and -not (Test-Path -LiteralPath $tokC) -and ($addedC.Count -eq 1) -and $addedC[0].Contains('no dashboard token was created')) ($addedC -join '|')

# Start-Ui: the arguments the supervisor hands the dashboard
$script:started = @()
function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError) $script:started += , @($ArgumentList); return $null }
function Get-TailnetName { return '' }
$root = Join-Path $Work 'uiroot'; $app = Join-Path $root 'app'; $logs = Join-Path $root 'logs'; $reports = Join-Path $root 'reports'; $py = 'py'; $uiPort = 48500
New-Item -ItemType Directory -Force -Path $root, $logs | Out-Null
Reset-Env
[void](Start-Ui)
$a1 = $script:started[0]
$i = [array]::IndexOf($a1, '--auth-token-file')
$tokU = Join-Path $root 'dashboard_token.txt'
Check 'Start-Ui: passes --auth-token-file "<root>\dashboard_token.txt" and no --no-auth' (($i -ge 0) -and ($a1[$i + 1] -eq (Q $tokU)) -and ($a1 -notcontains '--no-auth')) ($a1 -join ' ')
Check 'Start-Ui: the token file exists afterwards with 48 hex characters' ([System.IO.File]::ReadAllText($tokU) -cmatch '^[0-9a-f]{48}$')
Check 'Start-Ui: no certificate files in the root -> no TLS flags' (($a1 -notcontains '--tls-cert') -and ($a1 -notcontains '--tls-key'))
$tokUBytes = [System.IO.File]::ReadAllBytes($tokU)
Set-Content -Path (Join-Path $root 'dashboard_cert.pem') -Value 'x' -Encoding ASCII
[void](Start-Ui)
Check 'Start-Ui: a certificate without a key -> still no TLS flags' ($script:started[1] -notcontains '--tls-cert')
Set-Content -Path (Join-Path $root 'dashboard_key.pem') -Value 'x' -Encoding ASCII
[void](Start-Ui)
$a3 = $script:started[2]
$j = [array]::IndexOf($a3, '--tls-cert')
Check 'Start-Ui: certificate and key both present -> --tls-cert and --tls-key with their quoted paths' (($j -ge 0) -and ($a3[$j + 1] -eq (Q (Join-Path $root 'dashboard_cert.pem'))) -and ($a3[$j + 2] -eq '--tls-key') -and ($a3[$j + 3] -eq (Q (Join-Path $root 'dashboard_key.pem')))) ($a3 -join ' ')
Check 'Start-Ui: an existing token file is never rewritten by later starts' ([System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($tokU)) -eq [System.Convert]::ToBase64String($tokUBytes))
# TLS files and the optional sign-in features (setup_tls.ps1 writes tls\; allowed_users.txt / enable_passkeys.txt are the switches)
$troot = Join-Path $Work 'tlsroot'
New-Item -ItemType Directory -Force -Path (Join-Path $troot 'tls') | Out-Null
Check 'Get-TlsFiles: nothing installed -> $null' ($null -eq (Get-TlsFiles $troot))
Set-Content -Path (Join-Path $troot 'tls\dashboard_cert.pem') -Value 'x' -Encoding ASCII
Check 'Get-TlsFiles: a certificate without its key is not enough' ($null -eq (Get-TlsFiles $troot))
Set-Content -Path (Join-Path $troot 'dashboard_cert.pem') -Value 'x' -Encoding ASCII
Set-Content -Path (Join-Path $troot 'dashboard_key.pem') -Value 'x' -Encoding ASCII
$legacy = Get-TlsFiles $troot
Check 'Get-TlsFiles: the older <root>\dashboard_cert.pem + key pair is used when tls\ is incomplete' (($legacy[0] -eq (Join-Path $troot 'dashboard_cert.pem')) -and ($legacy[1] -eq (Join-Path $troot 'dashboard_key.pem'))) ($legacy -join '|')
Set-Content -Path (Join-Path $troot 'tls\dashboard_key.pem') -Value 'x' -Encoding ASCII
$new = Get-TlsFiles $troot
Check 'Get-TlsFiles: a complete tls\ pair wins over the older one' (($new[0] -eq (Join-Path $troot 'tls\dashboard_cert.pem')) -and ($new[1] -eq (Join-Path $troot 'tls\dashboard_key.pem'))) ($new -join '|')
$o0 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: no switch files -> no extra arguments' ($o0.Count -eq 0) ($o0 -join ' ')
Set-Content -Path (Join-Path $troot 'allowed_users.txt') -Value "# nobody yet`n`n" -Encoding ASCII
$o1 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: an allowed_users.txt with only comments and blanks does not turn identity on' ($o1.Count -eq 0) ($o1 -join ' ')
Set-Content -Path (Join-Path $troot 'allowed_users.txt') -Value "# owner`nalice@example.com`n" -Encoding ASCII
$o2 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: a login in allowed_users.txt -> --tailnet-identity --allowed-users "<file>" and NOT --require-identity' (($o2.Count -eq 3) -and ($o2[0] -eq '--tailnet-identity') -and ($o2[1] -eq '--allowed-users') -and ($o2[2] -eq (Q (Join-Path $troot 'allowed_users.txt')))) ($o2 -join ' ')
Set-Content -Path (Join-Path $troot 'enable_passkeys.txt') -Value '' -Encoding ASCII
$o3 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: enable_passkeys.txt without tls\tls_name.txt adds no passkey flag (passkeys need an https name)' (($o3 -notcontains '--passkey-origin') -and ($o3.Count -eq 3)) ($o3 -join ' ')
[System.IO.File]::WriteAllBytes((Join-Path $troot 'tls\tls_name.txt'), [System.Text.Encoding]::ASCII.GetBytes('node.tailnet.example'))
$o4 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: with the marker AND the remembered name -> two --passkey-origin (https://name and https://name:48500)' (($o4.Count -eq 7) -and ($o4[3] -eq '--passkey-origin') -and ($o4[4] -ceq 'https://node.tailnet.example') -and ($o4[5] -eq '--passkey-origin') -and ($o4[6] -ceq 'https://node.tailnet.example:48500')) ($o4 -join ' ')
[System.IO.File]::WriteAllBytes((Join-Path $troot 'tls\tls_name.txt'), [System.Text.Encoding]::ASCII.GetBytes('evil name; rm'))
$o5 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: a malformed remembered name is ignored (no passkey flag)' ($o5 -notcontains '--passkey-origin') ($o5 -join ' ')
Check 'Get-UiOptionalArgs: without enable_ui_next.txt there is no --ui-next' ($o5 -notcontains '--ui-next') ($o5 -join ' ')
Set-Content -Path (Join-Path $troot 'enable_ui_next.txt') -Value '' -Encoding ASCII
$o6 = @(Get-UiOptionalArgs $troot 48500)
Check 'Get-UiOptionalArgs: enable_ui_next.txt adds exactly one more argument, --ui-next, last (the other switches are unchanged)' (($o6.Count -eq $o5.Count + 1) -and ($o6[$o6.Count - 1] -ceq '--ui-next') -and ((($o6 | Select-Object -First $o5.Count) -join ' ') -ceq ($o5 -join ' '))) ($o6 -join ' ')
Remove-Item (Join-Path $troot 'enable_ui_next.txt')
# Start-Ui passes them: a login in allowed_users.txt and a complete tls\ pair in the uiroot
New-Item -ItemType Directory -Force -Path (Join-Path $root 'tls') | Out-Null
Set-Content -Path (Join-Path $root 'tls\dashboard_cert.pem') -Value 'x' -Encoding ASCII
Set-Content -Path (Join-Path $root 'tls\dashboard_key.pem') -Value 'x' -Encoding ASCII
Set-Content -Path (Join-Path $root 'allowed_users.txt') -Value "alice@example.com`n" -Encoding ASCII
[void](Start-Ui)
$a4 = $script:started[$script:started.Count - 1]
$k = [array]::IndexOf($a4, '--tls-cert')
Check 'Start-Ui: the tls\ pair is passed as --tls-cert/--tls-key (it wins over the older files)' (($k -ge 0) -and ($a4[$k + 1] -eq (Q (Join-Path $root 'tls\dashboard_cert.pem'))) -and ($a4[$k + 2] -eq '--tls-key') -and ($a4[$k + 3] -eq (Q (Join-Path $root 'tls\dashboard_key.pem')))) ($a4 -join ' ')
Check 'Start-Ui: allowed_users.txt turns on --tailnet-identity with the file path' (([array]::IndexOf($a4, '--tailnet-identity') -ge 0) -and ($a4[[array]::IndexOf($a4, '--allowed-users') + 1] -eq (Q (Join-Path $root 'allowed_users.txt')))) ($a4 -join ' ')
Check 'Start-Ui: positive twin - the first start (no switch files) had neither flag' (($a1 -notcontains '--tailnet-identity') -and ($a1 -notcontains '--passkey-origin'))
# the daily TLS renewal: Invoke-TlsRenewal (runs setup_tls.ps1 only when it set a certificate up here) and Invoke-DailyTlsCheck (restarts the dashboard on exit 10)
$script:procs = @(); $script:fakeExit = 0; $script:fakeOut = ''; $script:fakeFinishes = $true
function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError)
    $script:procs += , @($FilePath, $ArgumentList)
    Set-Content -Path $RedirectStandardOutput -Value $script:fakeOut -Encoding ASCII
    $o = [pscustomobject]@{ ExitCode = $script:fakeExit; Killed = $false }
    $o | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return $script:fakeFinishes }
    $o | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.Killed = $true }
    return $o
}
$rroot = Join-Path $Work 'renew'; $logs = Join-Path $rroot 'logs'
New-Item -ItemType Directory -Force -Path (Join-Path $rroot 'tls'), $logs | Out-Null
$script:procs = @()
Set-Content -Path (Join-Path $rroot 'setup_tls.ps1') -Value '# stands in' -Encoding ASCII
Check 'Invoke-TlsRenewal: setup_tls.ps1 is there but no tls_name.txt (it never installed a certificate here) -> -1 and nothing is started' ((Invoke-TlsRenewal $rroot) -eq -1 -and $script:procs.Count -eq 0)
Remove-Item -LiteralPath (Join-Path $rroot 'setup_tls.ps1')
[System.IO.File]::WriteAllBytes((Join-Path $rroot 'tls\tls_name.txt'), [System.Text.Encoding]::ASCII.GetBytes('node.tailnet.example'))
Check 'Invoke-TlsRenewal: the name is remembered but setup_tls.ps1 is not in the root -> -1 and nothing is started' ((Invoke-TlsRenewal $rroot) -eq -1 -and $script:procs.Count -eq 0)
Set-Content -Path (Join-Path $rroot 'setup_tls.ps1') -Value '# stands in' -Encoding ASCII
$before = $script:logged.Count
$rc = Invoke-TlsRenewal $rroot
$pa = $script:procs[0][1]
Check 'Invoke-TlsRenewal: starts powershell.exe -File "<root>\setup_tls.ps1" -Root "<root>" (no -Force, no secrets) and returns its exit code' (($rc -eq 0) -and ($script:procs[0][0] -eq 'powershell.exe') -and (($pa -join ' ') -ceq ('-NoProfile -NonInteractive -File ' + (Q (Join-Path $rroot 'setup_tls.ps1')) + ' -Root ' + (Q $rroot)))) ($pa -join ' ')
Check 'Invoke-TlsRenewal: exit 0 (current) logs nothing' ($script:logged.Count -eq $before)
$script:fakeExit = 10; $script:fakeOut = 'setup_tls: certificate for node.tailnet.example installed'
$rc = Invoke-TlsRenewal $rroot
$added = @($script:logged | Select-Object -Skip $before)
Check 'Invoke-TlsRenewal: exit 10 (installed) is returned and its one message line is logged' (($rc -eq 10) -and ($added.Count -eq 1) -and ($added[0] -ceq 'TLS: setup_tls: certificate for node.tailnet.example installed')) ($added -join '|')
$before = $script:logged.Count
$script:fakeExit = 1; $script:fakeOut = "line one`nsetup_tls: FAILED: tailscale cert failed (exit 1): boom"
$rc = Invoke-TlsRenewal $rroot
$added = @($script:logged | Select-Object -Skip $before)
Check 'Invoke-TlsRenewal: a failure (exit 1) is returned and its LAST output line is logged once, prefixed "TLS: "' (($rc -eq 1) -and ($added.Count -eq 1) -and ($added[0] -ceq 'TLS: setup_tls: FAILED: tailscale cert failed (exit 1): boom')) ($added -join '|')
$script:fakeFinishes = $false; $before = $script:logged.Count
$rc = Invoke-TlsRenewal $rroot
$added = @($script:logged | Select-Object -Skip $before)
Check 'Invoke-TlsRenewal: a run that does not finish in 3 minutes returns 1 and is logged as stopped' (($rc -eq 1) -and ($added.Count -ge 1) -and $added[0].Contains('took over 3 minutes')) ($added -join '|')
$script:fakeFinishes = $true
function New-FakeUi { $o = [pscustomobject]@{ HasExited = $false; Killed = 0 }; $o | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.Killed++ }; return $o }
$script:fakeExit = 10; $ui = New-FakeUi
Check 'Invoke-DailyTlsCheck: exit 10 stops the running dashboard exactly once and says so' (((Invoke-DailyTlsCheck $rroot $ui) -eq $true) -and ($ui.Killed -eq 1) -and (@($script:logged | Where-Object { $_ -like '*new dashboard TLS certificate was installed*' }).Count -ge 1))
foreach ($code in 0, 1, 3) {
    $script:fakeExit = $code; $ui = New-FakeUi
    Check "Invoke-DailyTlsCheck: exit $code leaves the dashboard running (positive twin of exit 10)" (((Invoke-DailyTlsCheck $rroot $ui) -eq $false) -and ($ui.Killed -eq 0))
}
$script:fakeExit = 10; $ui = New-FakeUi; $ui.HasExited = $true
Check 'Invoke-DailyTlsCheck: exit 10 with a dashboard that already exited kills nothing (the loop restarts it anyway)' (((Invoke-DailyTlsCheck $rroot $ui) -eq $false) -and ($ui.Killed -eq 0))
Remove-Item Function:\Start-Process

# show_dashboard_token.ps1 (owner's terminal helper): real script, fixed host list
$showDir = Join-Path $Work 'show'
New-Item -ItemType Directory -Force -Path $showDir | Out-Null
$ErrorActionPreference = 'Continue'                                         # a native command's stderr must not abort the test
$shown = & powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'show_dashboard_token.ps1') -Root $showDir -HostNames 'node.example.invalid,192.0.2.5' 2>&1
$ErrorActionPreference = 'Stop'
Check 'show_dashboard_token: no token file -> exit 1 and a message naming the file (no URL printed)' (($LASTEXITCODE -eq 1) -and (($shown -join ' ').Contains((Join-Path $showDir 'dashboard_token.txt'))) -and (-not (($shown -join ' ').Contains('?token='))))  ($shown -join '|')
[System.IO.File]::WriteAllBytes((Join-Path $showDir 'dashboard_token.txt'), [System.Text.Encoding]::ASCII.GetBytes("0123456789abcdef0123456789abcdef0123456789abcdef`r`n"))
$shown = @(& powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'show_dashboard_token.ps1') -Root $showDir -HostNames 'node.example.invalid,192.0.2.5')
Check 'show_dashboard_token: prints one http URL with ?token= per host, the token trimmed of its newline' (($LASTEXITCODE -eq 0) -and ($shown.Count -eq 2) -and ($shown[0] -ceq 'http://node.example.invalid:48500/?token=0123456789abcdef0123456789abcdef0123456789abcdef') -and ($shown[1] -ceq 'http://192.0.2.5:48500/?token=0123456789abcdef0123456789abcdef0123456789abcdef')) ($shown -join '|')
$shown = @(& powershell.exe -NoProfile -File (Join-Path $PSScriptRoot 'show_dashboard_token.ps1') -Root $showDir -HostNames 'node.example.invalid' -Scheme https -Port 8443)
Check 'show_dashboard_token: -Scheme https and -Port are honoured' (($shown.Count -eq 1) -and ($shown[0] -ceq 'https://node.example.invalid:8443/?token=0123456789abcdef0123456789abcdef0123456789abcdef')) ($shown -join '|')

# 6. loss telemetry: the restart delay (pure function cut out of the real file), the command line, and the loop wiring
foreach ($name in 'Get-LossLogDelay', 'Start-LossLog') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
if ($funcs.ContainsKey('Get-LossLogDelay')) {
    Check 'loss log delay: first quick exit after the initial 5 -> 10' ((Get-LossLogDelay 5 2) -eq 10) (Get-LossLogDelay 5 2)
    Check 'loss log delay: quick exits double it 10 -> 20 -> 40 -> 80 -> 160 -> 300 -> 300' ((((10, 20, 40, 80, 160, 300) | ForEach-Object { Get-LossLogDelay $_ 1 }) -join ',') -eq '20,40,80,160,300,300') (((10, 20, 40, 80, 160, 300) | ForEach-Object { Get-LossLogDelay $_ 1 }) -join ',')
    Check 'loss log delay: a run of exactly 300 s resets it to 10' ((Get-LossLogDelay 300 300) -eq 10)
    Check 'loss log delay: positive twin, a run of 299 s at delay 20 still doubles' ((Get-LossLogDelay 20 299) -eq 40)
}
if ($funcs.ContainsKey('Start-LossLog')) {
    $script:started = @()
    function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError)
        $script:started += [pscustomobject]@{ File = $FilePath; Args = $ArgumentList; Out = $RedirectStandardOutput; Err = $RedirectStandardError } }
    $py = 'py-loss'; $app = Join-Path $Work 'loss\app'; $root = Join-Path $Work 'loss'; $logs = Join-Path $Work 'loss\logs'
    Start-LossLog
    $s = $script:started
    $wantArgs = @('-I', ('"' + (Join-Path $app 'loss_log.py') + '"'), '--root', ('"' + $root + '"'), '--every-s', '30')
    Check 'loss log: exactly one process, the bridge python, exact arguments (isolated, --root = data dir, --every-s 30, no environment variable)' (($s.Count -eq 1) -and ($s[0].File -eq 'py-loss') -and (($s[0].Args -join '|') -eq ($wantArgs -join '|'))) (($s | ForEach-Object { $_.Args }) -join ' ')
    Check 'loss log: stdout to <root>\logs\loss_log.log, stderr to loss_log.err' (($s[0].Out -eq (Join-Path $logs 'loss_log.log')) -and ($s[0].Err -eq (Join-Path $logs 'loss_log.err'))) "$($s[0].Out) $($s[0].Err)"
    Remove-Item Function:\Start-Process
}
$callsLl = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-LossLogDelay' }, $true)
Check 'loss log loop: Get-LossLogDelay is called once from the loop, with the monotonic clock' (($callsLl.Count -eq 1) -and ($callsLl[0].Extent.Text -match 'Get-MonoUtcNow')) (($callsLl | ForEach-Object { $_.Extent.Text }) -join ';')
$regLl = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -in @('Register-Run', 'Register-Exit') -and $n.Extent.Text -match "'loss_log'" }, $true)
Check 'loss log loop: the start and the exit are both written to the run ledger' (($regLl | ForEach-Object { $_.GetCommandName() } | Sort-Object) -join ',' -eq 'Register-Exit,Register-Run') (($regLl | ForEach-Object { $_.Extent.Text }) -join ';')

# 7. feature state (registry ON/OFF list): Invoke-FeatureState (cut out of the real file) and the loop wiring
if (-not $funcs.ContainsKey('Invoke-FeatureState')) { Check 'run_pipeline.ps1 defines Invoke-FeatureState' $false } else { Invoke-Expression $funcs['Invoke-FeatureState'] }
if ($funcs.ContainsKey('Invoke-FeatureState')) {
    $script:fsStarted = @(); $script:fsExit = 0; $script:fsFinishes = $true; $script:fsOut = "3 features OFF, 5 ON, 1 UNKNOWN`nsecond line"
    function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError)
        $script:fsStarted += [pscustomobject]@{ File = $FilePath; Args = $ArgumentList; Out = $RedirectStandardOutput; Err = $RedirectStandardError }
        Set-Content -Path $RedirectStandardOutput -Value $script:fsOut -Encoding ASCII
        $o = [pscustomobject]@{ ExitCode = $script:fsExit; Killed = 0 }
        $o | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return $script:fsFinishes }
        $o | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.Killed++ }
        return $o
    }
    $py = 'py-feat'; $froot = Join-Path $Work 'feat'; $app = Join-Path $froot 'app'; $logs = Join-Path $froot 'logs'
    New-Item -ItemType Directory -Force -Path $app, $logs | Out-Null
    $script:logged = @()
    Check 'feature state: tool not deployed -> -1, nothing started, nothing logged' (((Invoke-FeatureState $froot) -eq -1) -and ($script:fsStarted.Count -eq 0) -and ($script:logged.Count -eq 0))
    Set-Content -Path (Join-Path $app 'feature_state.py') -Value '# stands in' -Encoding ASCII
    $rc = Invoke-FeatureState $froot
    $s = $script:fsStarted
    $wantArgs = @('-I', ('"' + (Join-Path $app 'feature_state.py') + '"'), '--root', ('"' + $froot + '"'), '--write')
    Check 'feature state: exactly one process, the bridge python, exact arguments (isolated, --root = data dir, --write, no environment variable)' (($rc -eq 0) -and ($s.Count -eq 1) -and ($s[0].File -eq 'py-feat') -and (($s[0].Args -join '|') -eq ($wantArgs -join '|'))) (($s | ForEach-Object { $_.Args }) -join ' ')
    Check 'feature state: stdout to <root>\logs\features.log, stderr to features.err' (($s[0].Out -eq (Join-Path $logs 'features.log')) -and ($s[0].Err -eq (Join-Path $logs 'features.err'))) "$($s[0].Out) $($s[0].Err)"
    Check 'feature state: exactly the count line is copied to supervisor.log' (($script:logged.Count -eq 1) -and ($script:logged[0] -ceq 'features: 3 features OFF, 5 ON, 1 UNKNOWN')) ($script:logged -join '|')
    $script:fsFinishes = $false; $script:logged = @()
    $rc = Invoke-FeatureState $froot
    Check 'feature state: a run that does not finish in 30 s returns 1 and is logged as stopped' (($rc -eq 1) -and ($script:logged.Count -eq 1) -and $script:logged[0].Contains('took over 30 seconds')) ($script:logged -join '|')
    $script:fsFinishes = $true; $script:logged = @(); $script:fsOut = ''
    $rc = Invoke-FeatureState $froot
    Check 'feature state: positive twin, a finished run with no output logs nothing' (($rc -eq 0) -and ($script:logged.Count -eq 0))
    Remove-Item Function:\Start-Process
}
$callsFs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Invoke-FeatureState' }, $true)
Check 'feature state loop: Invoke-FeatureState is called once, from the loop' ($callsFs.Count -eq 1) (($callsFs | ForEach-Object { $_.Extent.Text }) -join ';')
$loopFs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text -match 'Invoke-FeatureState' -and $n.Extent.Text -match '\$featNext' }, $true) | Sort-Object { $_.Extent.Text.Length } | Select-Object -First 1
Check 'feature state loop: due at $featNext on the monotonic clock, then rescheduled 24 h later' (($null -ne $loopFs) -and ($loopFs.Extent.Text -match '-ge \$featNext') -and ($loopFs.Extent.Text -match '\$featNext = \(Get-MonoUtcNow\)\.AddHours\(24\)')) "$($loopFs.Extent.Text)"
Check 'feature state loop: the first run is at supervisor start (not delayed)' ($ast.Extent.Text -match '(?m)^\$featNext = Get-MonoUtcNow\s')
# 7. nightly backup (03:00) and daily digest (06:00): the due rule (pure function cut out of the real file), the command lines, and the loop wiring
foreach ($name in 'Get-DailyDue', 'Start-Backup', 'Start-Digest') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
if ($funcs.ContainsKey('Get-DailyDue')) {
    $d0 = [datetime]'2026-10-27 00:00:00'
    $never = [datetime]::MinValue
    Check 'daily due: 02:59:59, never run -> not due' (-not (Get-DailyDue ([datetime]'2026-10-27 02:59:59') 3 $never))
    Check 'daily due: 03:00:00, never run -> due' (Get-DailyDue ([datetime]'2026-10-27 03:00:00') 3 $never)
    Check 'daily due: 03:00, last started the same date at 03:00 -> not due (once a day)' (-not (Get-DailyDue ([datetime]'2026-10-27 03:00:30') 3 ([datetime]'2026-10-27 03:00:00')))
    Check 'daily due: 23:59, last started the same date -> not due' (-not (Get-DailyDue ([datetime]'2026-10-27 23:59:59') 3 ([datetime]'2026-10-27 03:00:00')))
    Check 'daily due: 03:00 the next date, last started the previous date -> due' (Get-DailyDue ([datetime]'2026-10-28 03:00:00') 3 ([datetime]'2026-10-27 03:00:00'))
    Check 'daily due: supervisor started at 10:00, never run -> due at once (catch-up)' (Get-DailyDue ([datetime]'2026-10-27 10:00:00') 6 $never)
    Check 'daily due: 05:59 for the 06:00 job -> not due; 06:00 -> due' ((-not (Get-DailyDue ([datetime]'2026-10-27 05:59:59') 6 $never)) -and (Get-DailyDue ([datetime]'2026-10-27 06:00:00') 6 $never))
}
$script:started = @()
function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError)
    $script:started += [pscustomobject]@{ File = $FilePath; Args = $ArgumentList; Out = $RedirectStandardOutput; Err = $RedirectStandardError } }
$py = 'py-daily'; $app = Join-Path $Work 'daily\app'; $root = Join-Path $Work 'daily'; $logs = Join-Path $Work 'daily\logs'
if ($funcs.ContainsKey('Start-Backup')) {
    $script:started = @(); Start-Backup; $s = $script:started
    $wantArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $app 'backup_data.ps1') + '"'), '-Root', ('"' + $root + '"'))
    Check 'backup: one powershell process, exact arguments (-Root = data dir)' (($s.Count -eq 1) -and ($s[0].File -eq 'powershell.exe') -and (($s[0].Args -join '|') -eq ($wantArgs -join '|'))) (($s | ForEach-Object { $_.Args }) -join ' ')
    Check 'backup: output to logs\backup.log and backup.err' (($s[0].Out -eq (Join-Path $logs 'backup.log')) -and ($s[0].Err -eq (Join-Path $logs 'backup.err'))) "$($s[0].Out) $($s[0].Err)"
}
if ($funcs.ContainsKey('Start-Digest')) {
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $before = (Get-Date).AddDays(-1).ToString('yyyyMMdd', $inv)
    $script:started = @(); Start-Digest; $s = $script:started
    $after = (Get-Date).AddDays(-1).ToString('yyyyMMdd', $inv)
    $day = $s[0].Args[-1]
    Check 'digest: the day argument is the previous local date (yyyyMMdd)' (($day -ceq $before) -or ($day -ceq $after)) "$day vs $before"
    $wantArgs = @('-I', ('"' + (Join-Path $app 'daily_digest.py') + '"'), '--root', ('"' + $root + '"'), '--day', $day)
    Check 'digest: one process, the bridge python, exact arguments (isolated, --root, --day)' (($s.Count -eq 1) -and ($s[0].File -eq 'py-daily') -and (($s[0].Args -join '|') -eq ($wantArgs -join '|'))) (($s | ForEach-Object { $_.Args }) -join ' ')
    Check 'digest: output to logs\digest.log and digest.err' (($s[0].Out -eq (Join-Path $logs 'digest.log')) -and ($s[0].Err -eq (Join-Path $logs 'digest.err'))) "$($s[0].Out) $($s[0].Err)"
}
Remove-Item Function:\Start-Process
$dueCalls = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-DailyDue' }, $true)
Check 'daily jobs loop: Get-DailyDue is called once from the loop with the local clock' (($dueCalls.Count -eq 1) -and ($dueCalls[0].Extent.Text -match 'Get-Date')) (($dueCalls | ForEach-Object { $_.Extent.Text }) -join ';')
$jobs = [regex]::Matches($ast.Extent.Text, "n = '(\w+)'; f = '([\w.]+)'; hour = (\d+)") | ForEach-Object { $_.Groups[1].Value + ':' + $_.Groups[2].Value + ':' + $_.Groups[3].Value }
Check 'daily jobs loop: backup at hour 3 (backup_data.ps1) and digest at hour 6 (daily_digest.py), nothing else' (($jobs -join ',') -ceq 'backup:backup_data.ps1:3,digest:daily_digest.py:6') ($jobs -join ',')
$regJ = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -in @('Register-Run', 'Register-Exit') -and $n.Extent.Text -match '\$job\.n' }, $true)
Check 'daily jobs loop: the start and the exit are both written to the run ledger' (($regJ | ForEach-Object { $_.GetCommandName() } | Sort-Object) -join ',' -eq 'Register-Exit,Register-Run') (($regJ | ForEach-Object { $_.Extent.Text }) -join ';')


# 8. supervision: restart backoff, exit line, log rotation at start, health line, labels backup, backup marker, bridge priority
foreach ($name in 'Get-RestartDelay', 'Get-RunSeconds', 'Get-ExitLine', 'Get-CpuPct', 'Get-BridgeLossPct', 'Format-HealthLine', 'Get-BackupLast') { if (-not $funcs.ContainsKey($name)) { Check "run_pipeline.ps1 defines $name" $false } else { Invoke-Expression $funcs[$name] } }
if ($funcs.ContainsKey('Get-RestartDelay')) {
    $seq = (0, 30, 60, 120, 240, 480, 900) | ForEach-Object { Get-RestartDelay $_ 1 }
    Check 'restart delay: quick exits give 30, 60, 120, 240, 480, 900, 900 (first from 0)' (($seq -join ',') -ceq '30,60,120,240,480,900,900') ($seq -join ',')
    Check 'restart delay: a run of exactly 300 s resets it to 0' ((Get-RestartDelay 900 300) -eq 0)
    Check 'restart delay: positive twin, 299 s at 60 doubles to 120' ((Get-RestartDelay 60 299) -eq 120)
}
$pOk = [pscustomobject]@{ ExitCode = 3; StartTime = [datetime]'2026-10-27 10:00:00'; ExitTime = [datetime]'2026-10-27 10:02:05.4' }
Check 'run seconds: exit minus start, rounded to 0.1 (125.4)' ((Get-RunSeconds $pOk) -eq 125.4) (Get-RunSeconds $pOk)
Check 'exit line: code, local exit time and uptime, then the suffix' ((Get-ExitLine 'bridge' $pOk '; restart in 30 s') -ceq 'bridge exited (code 3) at 2026-10-27 10:02:05 after 125.4 s; restart in 30 s') (Get-ExitLine 'bridge' $pOk '; restart in 30 s')
$pNone = [pscustomobject]@{ ExitCode = 1 }
Check 'exit line: a process that cannot tell its times -> unknown and 0.0 s (counts as a quick exit)' (((Get-ExitLine 'x' $pNone) -ceq 'x exited (code 1) at unknown after 0.0 s') -and ((Get-RestartDelay 0 (Get-RunSeconds $pNone)) -eq 30)) (Get-ExitLine 'x' $pNone)
if ($funcs.ContainsKey('Get-CpuPct')) {
    Check 'cpu pct: 15 s of cpu in 60 s of wall time = 25.0' ((Get-CpuPct 100 115 60) -eq 25.0) (Get-CpuPct 100 115 60)
    Check 'cpu pct: a counter that went back or no wall time -> null' (($null -eq (Get-CpuPct 100 90 60)) -and ($null -eq (Get-CpuPct 100 115 0)))
}
$hroot = Join-Path $Work 'health'; New-Item -ItemType Directory -Force -Path $hroot | Out-Null
$bs = Join-Path $hroot 'bridge_status.json'
Set-Content -LiteralPath $bs -Value '{"loss_pct": 3.25, "pkts": 10}' -Encoding ASCII
$nowU = (Get-Item -LiteralPath $bs).LastWriteTimeUtc.AddSeconds(30)
Check 'bridge loss: a 30 s old status file gives its loss_pct (3.25)' ((Get-BridgeLossPct $bs $nowU) -eq 3.25) (Get-BridgeLossPct $bs $nowU)
Check 'bridge loss: older than 90 s -> null (positive twin: 90.0 s still counts)' (($null -eq (Get-BridgeLossPct $bs $nowU.AddSeconds(60.1))) -and ((Get-BridgeLossPct $bs $nowU.AddSeconds(60)) -eq 3.25))
Check 'bridge loss: a missing file -> null' ($null -eq (Get-BridgeLossPct (Join-Path $hroot 'nope.json') $nowU))
Set-Content -LiteralPath $bs -Value 'not json' -Encoding ASCII
Check 'bridge loss: unreadable JSON -> null' ($null -eq (Get-BridgeLossPct $bs ((Get-Item -LiteralPath $bs).LastWriteTimeUtc)))
Set-Content -LiteralPath $bs -Value '{"loss_pct": "x"}' -Encoding ASCII
Check 'bridge loss: a non-numeric loss_pct -> null' ($null -eq (Get-BridgeLossPct $bs ((Get-Item -LiteralPath $bs).LastWriteTimeUtc)))
Check 'health line: exact text, loss 0.5 is no warning' ((Format-HealthLine 12.34 41.5 0.5) -ceq 'health: bridge cpu 12.3 %, commit 41.5 %, bridge loss 0.5 %') (Format-HealthLine 12.34 41.5 0.5)
Check 'health line: loss 2.5 adds the warning' ((Format-HealthLine 12.34 41.5 2.5) -ceq 'health: bridge cpu 12.3 %, commit 41.5 %, bridge loss 2.5 % WARNING: bridge loss above 2 %') (Format-HealthLine 12.34 41.5 2.5)
Check 'health line: loss exactly 2.0 is no warning (positive twin of 2.5)' ((Format-HealthLine 1 1 2.0) -ceq 'health: bridge cpu 1.0 %, commit 1.0 %, bridge loss 2.0 %')
Check 'health line: unknown values print n/a and no warning' ((Format-HealthLine $null $null $null) -ceq 'health: bridge cpu n/a, commit n/a, bridge loss n/a')
# log rotation at child start: the real Rotate-IfBig over a temp logs dir
$logs = Join-Path $Work 'rot\logs'; New-Item -ItemType Directory -Force -Path $logs | Out-Null
function New-Sized([string]$n, [int]$bytes) { [IO.File]::WriteAllBytes((Join-Path $logs $n), (New-Object byte[] $bytes)) }
Check 'child log caps: detections.jsonl, classifier.log, analyzer.log 16 MiB; ui.err 2 MiB, nothing else' ((($script:ChildLogCaps.Keys | ForEach-Object { "$_=$($script:ChildLogCaps[$_])" }) -join ',') -ceq 'detections.jsonl=16777216,classifier.log=16777216,analyzer.log=16777216,ui.err=2097152') (($script:ChildLogCaps.Keys | ForEach-Object { "$_=$($script:ChildLogCaps[$_])" }) -join ',')
$script:ChildLogCaps = [ordered]@{ 'detections.jsonl' = 100; 'classifier.log' = 100; 'analyzer.log' = 100; 'ui.err' = 100 }
New-Sized 'detections.jsonl' 101; New-Sized 'classifier.log' 100; New-Sized 'analyzer.log' 500; New-Sized 'ui.err' 500
Rotate-ChildLogs @('detections.jsonl', 'classifier.log')
Check 'rotate: an over-cap file moves to .1, a file exactly at the cap stays, files not named stay' ((Test-Path (Join-Path $logs 'detections.jsonl.1')) -and -not (Test-Path (Join-Path $logs 'detections.jsonl')) -and (Test-Path (Join-Path $logs 'classifier.log')) -and -not (Test-Path (Join-Path $logs 'classifier.log.1')) -and (Test-Path (Join-Path $logs 'analyzer.log')) -and (Test-Path (Join-Path $logs 'ui.err'))) ((Get-ChildItem $logs).Name -join ',')
Rotate-ChildLogs @('analyzer.log'); Rotate-ChildLogs @('ui.err')
Check 'rotate: analyzer.log and ui.err rotate when named' ((Test-Path (Join-Path $logs 'analyzer.log.1')) -and (Test-Path (Join-Path $logs 'ui.err.1')) -and -not (Test-Path (Join-Path $logs 'ui.err'))) ((Get-ChildItem $logs).Name -join ',')
$rotBy = @{}
foreach ($f in 'Start-Classifier', 'Start-Analyzer', 'Start-Ui') { $rotBy[$f] = if ($funcs.ContainsKey($f)) { ([regex]::Match($funcs[$f], "Rotate-ChildLogs @\(([^)]*)\)")).Groups[1].Value } else { 'missing' } }
Check 'rotation is wired at the start of the classifier (detections.jsonl, classifier.log), analyzer (analyzer.log) and dashboard (ui.err)' (($rotBy['Start-Classifier'] -ceq "'detections.jsonl', 'classifier.log'") -and ($rotBy['Start-Analyzer'] -ceq "'analyzer.log'") -and ($rotBy['Start-Ui'] -ceq "'ui.err'")) (($rotBy.Keys | ForEach-Object { "$_=$($rotBy[$_])" }) -join ';')
# the backup marker and the hourly labels-only invocation
$broot = Join-Path $Work 'bk'; New-Item -ItemType Directory -Force -Path (Join-Path $broot 'backup') | Out-Null
Check 'backup marker: absent -> MinValue' ((Get-BackupLast $broot) -eq [datetime]::MinValue)
[IO.File]::WriteAllText((Join-Path $broot 'backup\last_backup.txt'), "20261026`n", [Text.Encoding]::ASCII)
Check 'backup marker: 20261026 -> that local date' ((Get-BackupLast $broot) -eq [datetime]'2026-10-26') (Get-BackupLast $broot)
[IO.File]::WriteAllText((Join-Path $broot 'backup\last_backup.txt'), "garbage`n", [Text.Encoding]::ASCII)
Check 'backup marker: garbage -> MinValue (the night is then due)' ((Get-BackupLast $broot) -eq [datetime]::MinValue)
Check 'backup marker: a night is due on the persisted marker (yesterday + 03:00 -> due; the same date -> not due)' ((Get-DailyDue ([datetime]'2026-10-27 03:00:00') 3 ([datetime]'2026-10-26')) -and -not (Get-DailyDue ([datetime]'2026-10-27 03:00:00') 3 ([datetime]'2026-10-27')))
$script:started = @()
function Start-Process { param($FilePath, [switch]$PassThru, $WindowStyle, $ArgumentList, $RedirectStandardOutput, $RedirectStandardError)
    $script:started += [pscustomobject]@{ File = $FilePath; Args = $ArgumentList; Out = $RedirectStandardOutput; Err = $RedirectStandardError } }
$py = 'py-sup'; $app = Join-Path $Work 'sup\app'; $root = Join-Path $Work 'sup'; $logs = Join-Path $Work 'sup\logs'
if ($funcs.ContainsKey('Start-Backup')) {
    $script:started = @(); Start-Backup -LabelsOnly; $s = $script:started
    $want = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $app 'backup_data.ps1') + '"'), '-Root', ('"' + $root + '"'), '-LabelsOnly')
    Check 'labels backup: powershell with the same arguments plus -LabelsOnly' (($s.Count -eq 1) -and (($s[0].Args -join '|') -ceq ($want -join '|'))) (($s | ForEach-Object { $_.Args }) -join ' ')
    Check 'labels backup: logs to backup_labels.log / backup_labels.err, not backup.log' (($s[0].Out -eq (Join-Path $logs 'backup_labels.log')) -and ($s[0].Err -eq (Join-Path $logs 'backup_labels.err'))) "$($s[0].Out) $($s[0].Err)"
}
Remove-Item Function:\Start-Process
$bridgeText = if ($funcs.ContainsKey('Start-Bridge')) { $funcs['Start-Bridge'] } else { '' }
Check 'bridge: started and then raised to High priority' ($bridgeText -match "PriorityClass = 'High'" -and $bridgeText -notmatch "PriorityClass = 'AboveNormal'")
$rd = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-RestartDelay' }, $true)
Check 'supervision loop: Get-RestartDelay is used for exactly the classifier, analysis host, bridge and board collector' ((($rd | ForEach-Object { $_.Extent.Text }) -join ';') -ceq 'Get-RestartDelay $cDelay (Get-RunSeconds $c);Get-RestartDelay $zDelay (Get-RunSeconds $z);Get-RestartDelay $bDelay (Get-RunSeconds $b);Get-RestartDelay $dDelay (Get-RunSeconds $d)') (($rd | ForEach-Object { $_.Extent.Text }) -join ';')
$el = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Get-ExitLine' }, $true)
Check 'supervision loop: every exit line is Get-ExitLine (8 children and the labels backup), none is a bare "exited (code" string' (($el.Count -eq 9) -and ($ast.Extent.Text -notmatch 'Log "[^"]*exited \(code \$\(')) "$($el.Count)"
Check 'health line: written once from the loop every 5 minutes on the monotonic clock' (($ast.Extent.Text -match '(?s)\$hNext = \(Get-MonoUtcNow\)\.AddMinutes\(5\)\s+Write-HealthLine \$b') -and ($ast.Extent.Text -match '\(Get-MonoUtcNow\) -ge \$hNext'))
Check 'labels backup loop: hourly, not while the nightly backup runs' (($ast.Extent.Text -match '-not \$bl -and -not \$bk -and \(Test-Path \(Join-Path \$app ''backup_data\.ps1''\)\) -and \(Get-MonoUtcNow\) -ge \$blNext') -and ($ast.Extent.Text -match '\$blNext = \(Get-MonoUtcNow\)\.AddMinutes\(60\)'))
Check 'nightly backup loop: due from the persisted marker (Get-BackupLast $root), retried at most every 30 minutes' (($ast.Extent.Text -match '\$job\.n -eq ''backup'' -and \(\(Get-Date\) - \$st\)\.TotalMinutes -lt 30') -and ($ast.Extent.Text -match '\{ Get-BackupLast \$root \}'))

foreach ($v in $vars) { [Environment]::SetEnvironmentVariable($v, $saved[$v]) }
Write-Host "`n$($script:n) checks, $($script:skipped) skipped, $($script:bad) failure(s)"
exit ([int]($script:bad -gt 0))
