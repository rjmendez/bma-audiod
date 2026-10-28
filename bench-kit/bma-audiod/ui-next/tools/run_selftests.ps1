# Runs static/selftest.html in headless Chrome, Edge and Firefox against the mock server and stores one JSON per browser/autoplay policy.
#   powershell -NoProfile -File run_selftests.ps1 [-OutDir <dir>] [-Port 48600]
# Each browser gets its own throw-away profile; only processes started with those profiles are stopped. Run one browser at a time (this script does).
# "autoplay" = the browser is told to allow audio without a gesture (flag / prefs); "default" = the stock policy (AudioContext stays suspended).
param([string]$OutDir = (Join-Path $env:TEMP 'uinext_selftest'), [int]$Port = 48600)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ui = Split-Path -Parent $here
$py = (Get-Command python -ErrorAction Stop).Source
New-Item -ItemType Directory -Force $OutDir | Out-Null
$marker = 'uinext_st_'
function Stop-Mine { Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $marker } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }
$srv = Start-Process -FilePath $py -ArgumentList @((Join-Path $ui 'mock_server.py'), '--port', $Port, '--results-dir', $OutDir) -PassThru -WindowStyle Hidden
Start-Sleep 4
function Run-One($name, $exe, $argList, $maxSec) {
    Stop-Mine
    $p = Start-Process -FilePath $exe -ArgumentList $argList -PassThru -WindowStyle Hidden
    $t0 = Get-Date; $got = $false
    while (((Get-Date) - $t0).TotalSeconds -lt $maxSec) {
        Start-Sleep -Milliseconds 1000
        try { $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/selftests" -TimeoutSec 3; if ($r.PSObject.Properties.Name -contains $name) { $got = $true; break } } catch { }
    }
    Stop-Mine
    "{0}: result received={1} after {2} s" -f $name, $got, [int]((Get-Date) - $t0).TotalSeconds
}
$url = "http://127.0.0.1:$Port/selftest.html?b="
$chrome = Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'
$edge = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
$ff = Join-Path $env:ProgramFiles 'Mozilla Firefox\firefox.exe'
try {
    foreach ($b in @(@('chrome', $chrome), @('edge', $edge))) {
        if (-not (Test-Path $b[1])) { continue }
        $bf = @('--headless=new', '--window-size=900,700', '--no-first-run', '--no-default-browser-check')
        $d1 = Join-Path $env:TEMP ($marker + $b[0] + '_a_' + (Get-Random)); Run-One ($b[0] + '-autoplay') $b[1] ($bf + @("--user-data-dir=$d1", '--autoplay-policy=no-user-gesture-required', ($url + $b[0] + '-autoplay'))) 70
        $d2 = Join-Path $env:TEMP ($marker + $b[0] + '_d_' + (Get-Random)); Run-One ($b[0] + '-default') $b[1] ($bf + @("--user-data-dir=$d2", ($url + $b[0] + '-default'))) 70
    }
    if (Test-Path $ff) {
        foreach ($mode in 'autoplay', 'default') {
            $d = Join-Path $env:TEMP ($marker + 'firefox_' + $mode + '_' + (Get-Random)); New-Item -ItemType Directory -Force $d | Out-Null
            $prefs = @('user_pref("browser.shell.checkDefaultBrowser", false);', 'user_pref("datareporting.policy.dataSubmissionEnabled", false);')
            if ($mode -eq 'autoplay') { $prefs += @('user_pref("media.autoplay.default", 0);', 'user_pref("media.autoplay.blocking_policy", 0);', 'user_pref("media.autoplay.block-webaudio", false);') }
            Set-Content -Path (Join-Path $d 'user.js') -Encoding ascii -Value $prefs
            Run-One ('firefox-' + $mode) $ff @('--headless', '--no-remote', '--profile', $d, ($url + 'firefox-' + $mode)) 90
        }
    }
} finally { Stop-Mine; if ($srv -and -not $srv.HasExited) { Stop-Process -Id $srv.Id -Force } }
"results in $OutDir (selftest_<browser>-<policy>.json)"
