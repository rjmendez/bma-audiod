# Installs (or replaces) the PC-side live pipeline: pins a copy of the bridge + classifier into
# <Root>\app with a VERSION file, and registers the logon task 'BMA-Dante-Pipeline'.
# Usage: install_task.ps1 -Root <data-dir> [-Repo <repo root>]     (-Repo defaults to the checkout this script is in)
# Local, untracked files (all optional) next to the tracked ones are carried over:
#   pc\bma.local.ps1               machine settings for the supervisor (template: bma.local.example.ps1); never overwrites
#                                  an existing <Root>\bma.local.ps1
#   classify\geo_county.local.json the county-level location for the BirdNET filter and the hourly ambient
#                                  temperature fetch (template: geo_county.example.json); without it an existing <Root>\app\geo_county.json is left alone
# Remove with:  Unregister-ScheduledTask -TaskName BMA-Dante-Pipeline -Confirm:$false
param([Parameter(Mandatory = $true)][string]$Root, [string]$Repo = '')
$ErrorActionPreference = 'Stop'
$root = $Root
$app  = Join-Path $root 'app'
New-Item -ItemType Directory -Force -Path $app, (Join-Path $root 'logs') | Out-Null

if ($Repo) { $src = Join-Path $Repo 'bench-kit\bma-audiod' } else { $src = Split-Path $PSScriptRoot -Parent }
Copy-Item (Join-Path $src 'pc\dante_bridge.py')        $app -Force
Copy-Item (Join-Path $src 'pc\bma_cable_sink.py')      $app -Force
Copy-Item (Join-Path $src 'pc\bma_cable_decode.py')    $app -Force
Copy-Item (Join-Path $src 'pc\bma_cable_selftest.py')  $app -Force
# magnitude-only spectrum rows for the dashboard (loopback only; see pc\bma_spectrum.py and contracts\specs\spectrum-packet.md)
Copy-Item (Join-Path $src 'pc\bma_spectrum.py')        $app -Force
Copy-Item (Join-Path $src 'classify\bma_classify.py')  $app -Force
# BirdNET location filter: the county-level config (delete app\geo_county.json to turn it off) and the labels the
# location model cannot judge (without it the filter never drops anything, it only flags)
Copy-Item (Join-Path $src 'classify\birdnet_coverage.json') $app -Force
$geo = Join-Path $src 'classify\geo_county.local.json'
if (Test-Path $geo) { Copy-Item $geo (Join-Path $app 'geo_county.json') -Force }
Copy-Item (Join-Path $src 'pc\run_pipeline.ps1')       $root -Force
Copy-Item (Join-Path $src 'pc\run_ledger.ps1')         $root -Force   # dot-sourced by run_pipeline.ps1 (start/exit ledger)
Copy-Item (Join-Path $src 'pc\setup_tls.ps1') $root -Force          # Tailscale certificate install/renewal for the dashboard (run by hand once; the supervisor renews)
Copy-Item (Join-Path $src 'pc\protect_root.ps1') $root -Force      # owner + SYSTEM + Administrators only on the root, app, the supervisor script and the switch files (run below)
Copy-Item (Join-Path $src 'pc\show_dashboard_token.ps1') $root -Force   # prints the dashboard URL with the login token (owner's terminal only)
# <Root>\dashboard_token.txt (the dashboard login) is created by run_pipeline.ps1 on its first run; this installer never writes or overwrites it
$localCfg = Join-Path $src 'pc\bma.local.ps1'
if ((Test-Path $localCfg) -and -not (Test-Path (Join-Path $root 'bma.local.ps1'))) { Copy-Item $localCfg $root }
# Without bma.local.ps1 every machine-specific setting falls back to a default that is usually wrong (python on PATH for the
# bridge, python3 for the analyzer, <data-dir>\models, no board, no sink): say so loudly instead of installing quietly.
if (-not (Test-Path (Join-Path $root 'bma.local.ps1'))) {
    Write-Warning ("no bma.local.ps1 in ${root}: the supervisor will use these fallbacks: BMA_PYTHON = first python on PATH; " +
        "BMA_SINK_PYTHON unset (cable sink off); BMA_BOARD unset (no board address); BMA_DSPD_TOKEN_FILE unset; " +
        "BMA_MODEL_DIR = $root\models; BMA_WSL_PYTHON unset (python3 in WSL). Copy pc\bma.local.example.ps1 to " +
        "$root\bma.local.ps1 and set what this machine needs.")
}
# shared metadata-history writer (sink, board collector and analysis host import it from their own directory)
Copy-Item (Join-Path $src 'pc\history_log.py')         $app -Force
Copy-Item (Join-Path $src 'pc\run_classifier.sh') $app -Force
# the analyzer's files (host, plugins, history modules, wildlife clips with their data, tonal mask, launcher, Perch) are listed ONCE, in
# analysis\plugins.manifest.json (support_files + pc_only_files + the files of every plugin deployed to the PC); host.py imports clips.py, which
# imports clip_features.py / clip_render.py and reads clip_exclude.json next to itself (owner policy 2026-10-26: visual-only; audio is off unless <root>\enable_clip_audio.txt exists, which run_pipeline.ps1 turns into --clip-audio).
# tonal_mask.json is the mask of the unit's own electronic lines (delete tonal_mask.json from the app directory to turn it off); analyzer_history.py writes the analyzer_sample
# history (run-history) and imports hist.py from the same directory
# >>> plugin-manifest files
$manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $src 'analysis\plugins.manifest.json') | ConvertFrom-Json
$analysisFiles = @($manifest.support_files) + @($manifest.pc_only_files) + @($manifest.plugins | Where-Object { @($_.deploy) -contains 'pc' } | ForEach-Object { @($_.files) })
foreach ($f in $analysisFiles) { Copy-Item (Join-Path $src "analysis\$f") $app -Force }
# <<< plugin-manifest files
# taxon table for the group stamp (made by ui\make_taxa.py); without it the analyzer stamps group null and counts it
if (Test-Path (Join-Path $src 'ui\taxa.json')) { Copy-Item (Join-Path $src 'ui\taxa.json') $app -Force }
Copy-Item (Join-Path $src 'tools\bma_debug_rx.py') $app -Force
# hourly ambient air temperature fetch (ambient-temp contract; run_pipeline.ps1 starts it only when app\geo_county.json exists);
# it imports history_log.py from its own directory
Copy-Item (Join-Path $src 'tools\ambient_temp.py') $app -Force
# persistent loss telemetry (loss-log contract; run_pipeline.ps1 starts it when app\loss_log.py exists); imports history_log.py from its own directory
Copy-Item (Join-Path $src 'tools\loss_log.py') $app -Force
# live ON/OFF state of every registry feature (docs\FEATURES.md); run_pipeline.ps1 runs it at start and daily when app\feature_state.py exists
Copy-Item (Join-Path $src 'tools\feature_state.py') $app -Force
# the registry next to it (feature_state.py and the digest read the Approved column to warn about a feature that is ON without an owner decision)
Copy-Item (Join-Path $src 'docs\FEATURES.md') $app -Force
# lists audio files older than 24 h under the data root (read only, run by hand: python -I app\audio_sweep_check.py --root <root> [--extra <dir>])
Copy-Item (Join-Path $src 'tools\audio_sweep_check.py') $app -Force
# nightly backup (03:00) and daily digest (06:00), started by run_pipeline.ps1 when the file exists; the digest imports perch_report.py and labels.py from its own directory
foreach ($f in 'pc\backup_data.ps1', 'tools\daily_digest.py', 'tools\perch_report.py', 'analysis\labels.py', 'analysis\events.py') { Copy-Item (Join-Path $src $f) $app -Force }
# the read-only web dashboard (stdlib only; static assets are loaded by name, so the layout must stay ui\static\)
$uiApp = Join-Path $app 'ui'
New-Item -ItemType Directory -Force -Path (Join-Path $uiApp 'static') | Out-Null
Copy-Item (Join-Path $src 'ui\bma_ui.py') $uiApp -Force
Copy-Item (Join-Path $src 'ui\passkeys.py') $uiApp -Force   # WebAuthn for the dashboard (optional import: without it passkeys are off)
Copy-Item (Join-Path $src 'ui\ws.py') $uiApp -Force        # the live spectrum WebSocket (GET /api/v2/live, --ui-next only; --ui-next refuses to start without it)
Copy-Item (Join-Path $src 'ui\auth.py') $uiApp -Force      # the dashboard's login (imported by bma_ui.py: without it the dashboard does not start)
Copy-Item (Join-Path $src 'analysis\artifact_gate.py') $uiApp -Force   # the advisory artifact flags (pure, stdlib; optional import: without it the dashboard shows no flags and status.artifact_gate.available is false)
foreach ($f in 'analysis\labels.py', 'analysis\events.py', 'analysis\review_queue.py', 'ui\review.py') { Copy-Item (Join-Path $src $f) $uiApp -Force }    # Review tab: label store, queue, routes (bma_ui.py imports them from its own directory)
Copy-Item (Join-Path $src 'ui\taxa.json') $uiApp -Force    # taxonomic groups (genus table); a missing file shows in /api/status taxa.error and files every label under other-bio
$nx = Join-Path $uiApp 'next'    # the next dashboard: bma_ui.py --ui-next serves it under /next/ from <ui dir>\next, same file names
New-Item -ItemType Directory -Force -Path (Join-Path $nx 'engine') | Out-Null
foreach ($f in 'index.html', 'app.css', 'app.js', 'review.js', 'adapter.js') { Copy-Item (Join-Path $src "ui-next\static\$f") $nx -Force }
Get-ChildItem (Join-Path $src 'ui-next\static\engine') -File | ForEach-Object { Copy-Item $_.FullName (Join-Path $nx 'engine') -Force }
Get-ChildItem (Join-Path $src 'ui\static') -File | ForEach-Object { Copy-Item $_.FullName (Join-Path $uiApp 'static') -Force }    # ALL static assets (index.html, app.js, app.css, tip.js, spectrum.js, ...)
$ver = (git -C $src rev-parse --short HEAD) + ' ' + (git -C $src log -1 --format=%s)
$dirty = (git -C $src status --porcelain -- pc classify analysis ui)
if ($dirty) { $ver += ' (UNCOMMITTED CHANGES AT INSTALL)' }
Set-Content -Path (Join-Path $app 'VERSION') -Value $ver
Write-Host "pinned: $ver"
# nobody else may replace what the supervisor runs (the root inherits Modify for Authenticated Users from its parent otherwise)
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'protect_root.ps1') -Root $root
if ($LASTEXITCODE -ne 0) { Write-Warning "protect_root.ps1 did not finish (exit $LASTEXITCODE): the root keeps its inherited permissions" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $root 'run_pipeline.ps1') + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'BMA-Dante-Pipeline' -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'BMA360 Dante card -> bridge -> classifier (WSL). See bench-kit/bma-audiod/pc.' -Force | Out-Null
Get-ScheduledTask -TaskName 'BMA-Dante-Pipeline' | Select-Object TaskName, State | Format-Table -AutoSize
