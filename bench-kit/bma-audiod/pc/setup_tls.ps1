# Gets, installs and renews the dashboard's HTTPS certificate from Tailscale (`tailscale cert`). Safe to run any number of times
# (idempotent) and by hand or from the supervisor (run_pipeline.ps1 runs it once a day when -Name was set up here before).
#
#   setup_tls.ps1 [-Root <data-dir>] [-Name <node>.<tailnet-domain>] [-RenewBeforeDays 30] [-Force] [-CheckOnly] [-Tailscale <path-to-tailscale>]
#
#   -Root             the supervisor's data dir (default: the directory this script is in). Certificate material lives ONLY in
#                     <Root>\tls\ (dashboard_cert.pem, dashboard_key.pem and tls_name.txt): a directory whose permissions are limited to the
#                     current user (inheritance removed). Nothing is written to the repository, the logs or the console.
#   -Name             the node's full MagicDNS name. Default: this node's name from `tailscale status --json` (Self.DNSName); once
#                     installed the name is remembered in tls_name.txt so the supervisor can renew without arguments.
#   -RenewBeforeDays  renew when the installed certificate has fewer days left (default 30; Let's Encrypt certificates last 90 days).
#   -Force            fetch a new certificate even if the installed one is valid (mind the Let's Encrypt rate limits).
#   -CheckOnly        decide and report, change nothing, do not call tailscale.
#   -Tailscale        the tailscale executable (default: the first tailscale on PATH).
#
# Exit codes: 0 certificate is current (nothing changed); 10 a certificate was installed or renewed (RESTART THE DASHBOARD: it reads the
# files once at start; the supervisor restarts it when its process exits); 3 -CheckOnly and a certificate is missing or due; 1 failure
# (the previously installed certificate is left untouched); 2 not applicable (no tailscale, or no node name).
#
# Prerequisites (once, in the Tailscale admin console): MagicDNS and "HTTPS Certificates" enabled for the tailnet. Note that enabling HTTPS
# publishes the node names of the tailnet in the public Certificate Transparency logs (https://tailscale.com/kb/1153/enabling-https).
# `tailscale cert` may need an elevated or operator-enabled session (`tailscale set --operator=<your user>` once, from an administrator shell).
# Certificates from `tailscale cert` are NOT renewed by Tailscale itself: this script is the renewal.
param([string]$Root = $PSScriptRoot, [string]$Name = '', [int]$RenewBeforeDays = 30, [switch]$Force, [switch]$CheckOnly, [string]$Tailscale = '')
$ErrorActionPreference = 'Stop'

function Say([string]$m) { Write-Host ("setup_tls: " + $m) }

# 'install' (nothing usable installed), 'renew' (expires within the threshold or is for another name), 'ok'. Pure: no I/O.
function Get-TlsDecision([bool]$Exists, [string]$InstalledName, [datetime]$NotAfterUtc, [datetime]$NowUtc, [string]$WantName, [int]$RenewBeforeDays) {
    if (-not $Exists) { return 'install' }
    if ($InstalledName -ne $WantName) { return 'renew' }
    if (($NotAfterUtc - $NowUtc).TotalDays -lt $RenewBeforeDays) { return 'renew' }
    return 'ok'
}

# the first CERTIFICATE block of a PEM file as an X509Certificate2 (works on Windows PowerShell 5.1 and PowerShell 7)
function Read-PemCertificate([string]$path) {
    $t = [System.IO.File]::ReadAllText($path)
    $m = [regex]::Match($t, '-----BEGIN CERTIFICATE-----(?<b>[^-]+)-----END CERTIFICATE-----')
    if (-not $m.Success) { throw "no certificate in $path" }
    $der = [System.Convert]::FromBase64String(($m.Groups['b'].Value -replace '\s', ''))
    return New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList (, $der)
}

function Get-CertNames($cert) {
    $names = @()
    foreach ($e in $cert.Extensions) {
        if ($e.Oid.Value -eq '2.5.29.17') { $names += ($e.Format($true) -split "`r?`n" | ForEach-Object { if ($_ -match 'DNS Name=(.+)$') { $Matches[1].Trim().ToLower() } }) }
    }
    $cn = $cert.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::DnsName, $false)
    if ($cn) { $names += $cn.ToLower() }
    return $names
}

# the directory (and with it every file created in it) is readable only by the current user; fails closed
# an elevated session creates objects with Administrators, SYSTEM and OWNER RIGHTS entries: remove them (S-1-5-32-544, S-1-5-18, S-1-3-4) and the broad groups (S-1-5-11, S-1-1-0)
function Remove-OtherEntries([string]$path, [string]$me) {
    & icacls.exe $path /remove:g '*S-1-5-32-544' '*S-1-5-18' '*S-1-3-4' '*S-1-5-11' '*S-1-1-0' *> $null
    if ($LASTEXITCODE -ne 0) { throw "could not remove other principals from $path (icacls exit $LASTEXITCODE)" }
}

function Protect-Directory([string]$dir) {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $dir /inheritance:r /grant:r "${me}:(OI)(CI)(F)" *> $null
    if ($LASTEXITCODE -ne 0) { throw "could not restrict the permissions of $dir (icacls exit $LASTEXITCODE)" }
    Remove-OtherEntries $dir $me
}

function Protect-File([string]$file) {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $file /inheritance:r /grant:r "${me}:(F)" *> $null
    if ($LASTEXITCODE -ne 0) { throw "could not restrict the permissions of $file (icacls exit $LASTEXITCODE)" }
    Remove-OtherEntries $file $me
}

$tlsDir = Join-Path $Root 'tls'
$certFile = Join-Path $tlsDir 'dashboard_cert.pem'
$keyFile = Join-Path $tlsDir 'dashboard_key.pem'
$nameFile = Join-Path $tlsDir 'tls_name.txt'

if (-not $Name -and (Test-Path -LiteralPath $nameFile)) { $Name = ([System.IO.File]::ReadAllText($nameFile)).Trim() }
$ts = $Tailscale
if (-not $ts) { $c = Get-Command tailscale -ErrorAction SilentlyContinue; if ($c) { $ts = $c.Source } }
if (-not $Name -and $ts -and (Test-Path -LiteralPath $ts)) {
    try { $Name = ([string]((& $ts status --json 2>$null) -join "`n" | ConvertFrom-Json).Self.DNSName).TrimEnd('.') } catch { $Name = '' }
}
$Name = $Name.Trim().TrimEnd('.').ToLower()
if (-not $Name -or $Name -notmatch '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$') { Say "no usable node name (give -Name <node>.<tailnet-domain>, or start tailscale)"; exit 2 }
if (-not $CheckOnly -and (-not $ts -or -not (Test-Path -LiteralPath $ts))) { Say "tailscale was not found (install it, or pass -Tailscale <path>)"; exit 2 }

# what is installed now
$exists = (Test-Path -LiteralPath $certFile) -and (Test-Path -LiteralPath $keyFile)
$instName = ''; $notAfter = [datetime]::MinValue
if ($exists) {
    try {
        $cur = Read-PemCertificate $certFile
        $notAfter = $cur.NotAfter.ToUniversalTime()
        $instName = if ((Get-CertNames $cur) -contains $Name) { $Name } else { '' }
    } catch { $exists = $false }                      # an unreadable certificate counts as not installed
}
$decision = Get-TlsDecision -Exists $exists -InstalledName $instName -NotAfterUtc $notAfter -NowUtc ([datetime]::UtcNow) -WantName $Name -RenewBeforeDays $RenewBeforeDays
if ($Force) { $decision = if ($exists) { 'renew' } else { 'install' } }
if ($decision -eq 'ok') { Say ("certificate for $Name is current (valid until {0:yyyy-MM-dd}); nothing to do" -f $notAfter); exit 0 }
if ($CheckOnly) { Say "certificate for $Name needs action: $decision"; exit 3 }

# fetch into a scratch directory with the same restricted permissions, validate, then swap in. The old files stay until the new ones are proven.
$scratch = Join-Path $tlsDir 'new'
try {
    New-Item -ItemType Directory -Force -Path $tlsDir | Out-Null
    Protect-Directory $tlsDir
    if (Test-Path -LiteralPath $scratch) { Remove-Item -LiteralPath $scratch -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $scratch | Out-Null
    Protect-Directory $scratch
    $newCert = Join-Path $scratch 'c.pem'; $newKey = Join-Path $scratch 'k.pem'
    $ErrorActionPreference = 'Continue'                                           # Windows PowerShell 5.1 turns a native command's stderr into a terminating error under 'Stop'
    $out = & $ts cert --cert-file $newCert --key-file $newKey $Name 2>&1
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -ne 0) {
        $msg = (($out | ForEach-Object { "$_" }) -join ' ') -replace '\s+', ' '
        if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
        throw "tailscale cert failed (exit $LASTEXITCODE): $msg"                   # tailscale's own message holds no key material
    }
    if (-not (Test-Path -LiteralPath $newCert) -or -not (Test-Path -LiteralPath $newKey)) { throw "tailscale cert did not write both files" }
    $cert = Read-PemCertificate $newCert
    $key = [System.IO.File]::ReadAllText($newKey)
    if ($key -notmatch '-----BEGIN [A-Z ]*PRIVATE KEY-----') { throw "the new key file is not a PEM private key" }
    if ((Get-CertNames $cert) -notcontains $Name) { throw "the new certificate is not for $Name" }
    if ($cert.NotAfter.ToUniversalTime() -le [datetime]::UtcNow.AddDays(1)) { throw "the new certificate is already (almost) expired" }
    Protect-File $newCert; Protect-File $newKey
    Move-Item -LiteralPath $newKey -Destination $keyFile -Force
    Move-Item -LiteralPath $newCert -Destination $certFile -Force
    [System.IO.File]::WriteAllBytes($nameFile, [System.Text.Encoding]::ASCII.GetBytes($Name))
    Protect-File $keyFile; Protect-File $certFile; Protect-File $nameFile
    Say ("certificate for $Name installed in $tlsDir, valid until {0:yyyy-MM-dd} ($decision); restart the dashboard to use it" -f $cert.NotAfter.ToUniversalTime())
    $code = 10
} catch {
    Say ("FAILED: " + $_.Exception.Message + "; the installed certificate (if any) was not touched")
    $code = 1
} finally {
    if (Test-Path -LiteralPath $scratch) { Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue }
}
exit $code
