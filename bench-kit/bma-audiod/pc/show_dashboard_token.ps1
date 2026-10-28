# Prints the dashboard URL(s) with the login token, to open on a phone (or to bookmark). THIS PRINTS THE SECRET: run it only in your
# own terminal, never in a shared screen, a log, a ticket or a chat. Opening a URL once sets a 30 day cookie in that browser.
# Usage: show_dashboard_token.ps1 [-Root <data-dir>] [-Port 48500] [-Scheme http|https] [-HostNames a,b]   (one comma-separated value)
#   -Root       where dashboard_token.txt lives (default: the directory this script is in, i.e. the supervisor's data dir)
#   -Scheme     use https when dashboard_cert.pem + dashboard_key.pem are installed in <Root> (see ui\AUTH.md); default http
#   -HostNames  names or addresses to print (default: this computer's name, its tailnet name when tailscale is up, and its
#               private IPv4 addresses)
param([string]$Root = $PSScriptRoot, [int]$Port = 48500, [ValidateSet('http', 'https')][string]$Scheme = 'http', [string]$HostNames = '')

function Get-DashboardUrls([string]$Token, [int]$Port, [string]$Scheme, [string[]]$HostNames) {
    foreach ($h in $HostNames) {
        if ($h) { "${Scheme}://${h}:${Port}/?token=${Token}" }
    }
}

function Get-DefaultHostNames {
    $out = @($env:COMPUTERNAME.ToLower())
    $ts = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($ts) {
        try {
            $j = (& $ts.Source status --json 2>$null) -join "`n" | ConvertFrom-Json
            $n = ([string]$j.Self.DNSName).TrimEnd('.').ToLower()
            if ($n) { $out += $n }
            foreach ($a in $j.Self.TailscaleIPs) { if ($a -match '^\d+\.\d+\.\d+\.\d+$') { $out += $a } }
        } catch { }
    }
    try {
        $out += @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
            ForEach-Object { $_.IPAddress })
    } catch { }
    return $out | Select-Object -Unique
}

if ($MyInvocation.InvocationName -ne '.') {
    $tokenFile = Join-Path $Root 'dashboard_token.txt'
    if (-not (Test-Path -LiteralPath $tokenFile)) {
        [Console]::Error.WriteLine("no token file at ${tokenFile}: it is created when the supervisor (run_pipeline.ps1) first starts the dashboard")
        exit 1
    }
    $token = [System.IO.File]::ReadAllText($tokenFile).Trim()
    $names = @($HostNames -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($names.Count -eq 0) { $names = @(Get-DefaultHostNames) }
    Get-DashboardUrls $token $Port $Scheme $names
}
