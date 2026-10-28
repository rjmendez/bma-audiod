# Nightly copy of the small irreplaceable data into <Root>\backup\<yyyyMMdd>; older dated folders are removed.
#   powershell -NoProfile -File backup_data.ps1 -Root <data dir> [-Day yyyyMMdd] [-Keep 14] [-LabelsOnly]
# Copied (allowlist): labels\labels.jsonl, history\*.jsonl and history\*.json, analysis\novelty_state.pkl, analysis\clips\index.jsonl, analysis\clips\**\*.json and *.png (sidecars), analyzer_args.txt.
# Never copied: audio, the dashboard token, bma.local.ps1, tls\, anything else.
# Retention: folders named yyyyMMdd dated within the last -Keep days up to -Day stay; other folders in backup\ are not touched.
# -LabelsOnly (hourly): copies labels\labels.jsonl only; no retention, no marker.
# A full run with no failed copy writes <Root>\backup\last_backup.txt (the Day): the supervisor reads it, so a restart neither repeats nor skips a night.
# Output: one line, counts only. Exit 0 ok, 1 a copy failed, 2 usage error.
param([Parameter(Mandatory = $true)][string]$Root, [string]$Day = '', [int]$Keep = 14, [switch]$LabelsOnly)
$ErrorActionPreference = 'Stop'
$inv = [Globalization.CultureInfo]::InvariantCulture
if (-not $Day) { $Day = (Get-Date).ToString('yyyyMMdd', $inv) }
$dayDate = [datetime]::MinValue
if (-not [datetime]::TryParseExact($Day, 'yyyyMMdd', $inv, [Globalization.DateTimeStyles]::None, [ref]$dayDate)) { [Console]::Error.WriteLine("Day must be yyyyMMdd"); exit 2 }
if ($Keep -lt 1) { [Console]::Error.WriteLine("Keep must be >= 1"); exit 2 }
if (-not (Test-Path -LiteralPath $Root -PathType Container)) { [Console]::Error.WriteLine("Root is not a directory"); exit 2 }
$Root = (Resolve-Path -LiteralPath $Root).Path
$backupRoot = Join-Path $Root 'backup'
$dest = Join-Path $backupRoot $Day

$files = New-Object System.Collections.Generic.List[object]
function Add-File([string]$path, [string]$rel) { $files.Add([pscustomobject]@{ Path = $path; Rel = $rel }) }
function Add-If([string]$rel) {
    $p = Join-Path $Root $rel
    if (Test-Path -LiteralPath $p -PathType Leaf) { Add-File $p $rel }
}
function Add-Tree([string]$relDir, [string[]]$extensions, [switch]$Recurse) {
    $dir = Join-Path $Root $relDir
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return }
    $base = (Get-Item -LiteralPath $dir).FullName.TrimEnd('\', '/')
    Get-ChildItem -LiteralPath $dir -File -Recurse:$Recurse | ForEach-Object {
        if ($_.Extension -in $extensions) { Add-File $_.FullName ($relDir + $_.FullName.Substring($base.Length)) }
    }
}
Add-If 'labels\labels.jsonl'
if (-not $LabelsOnly) {
    Add-If 'analyzer_args.txt'
    Add-If 'analysis\novelty_state.pkl'
    Add-Tree 'history' @('.jsonl', '.json')
    Add-If 'analysis\clips\index.jsonl'
    Add-Tree 'analysis\clips' @('.json', '.png') -Recurse
}

$n = 0; $bytes = 0; $failed = 0
foreach ($f in $files) {
    $to = Join-Path $dest $f.Rel
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
        Copy-Item -LiteralPath $f.Path -Destination $to -Force
        $n++; $bytes += (Get-Item -LiteralPath $to).Length
    } catch { $failed++ }
}

if ($LabelsOnly) {
    Write-Output ("backup {0} labels: {1} files, {2} bytes, {3} failed" -f $Day, $n, $bytes, $failed)
    exit ([int]($failed -gt 0))
}
$removed = 0
if (Test-Path -LiteralPath $backupRoot -PathType Container) {
    $oldest = $dayDate.AddDays(-($Keep - 1))
    foreach ($d in (Get-ChildItem -LiteralPath $backupRoot -Directory)) {
        $dd = [datetime]::MinValue
        if ($d.Name -notmatch '^\d{8}$') { continue }
        if (-not [datetime]::TryParseExact($d.Name, 'yyyyMMdd', $inv, [Globalization.DateTimeStyles]::None, [ref]$dd)) { continue }
        if ($dd -lt $oldest) {
            try { Remove-Item -LiteralPath $d.FullName -Recurse -Force; $removed++ } catch { $failed++ }
        }
    }
}
if ($failed -eq 0) {
    try { New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null; [IO.File]::WriteAllText((Join-Path $backupRoot 'last_backup.txt'), "$Day`n", [Text.Encoding]::ASCII) } catch { $failed++ }
}
Write-Output ("backup {0}: {1} files, {2} bytes, {3} old removed, {4} failed" -f $Day, $n, $bytes, $removed, $failed)
exit ([int]($failed -gt 0))
