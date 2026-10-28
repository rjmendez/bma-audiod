# Restricts the pipeline root to its owner, SYSTEM and Administrators, so no other local account can replace what the supervisor runs.
#   powershell -NoProfile -File protect_root.ps1 -Root <data dir>
# Protected (inheritance removed, every other entry dropped; the access list is set, not appended to): <Root>, <Root>\app, run_pipeline.ps1,
# analyzer_args.txt and enable_*.txt in <Root>. Everything created below them inherits the same list.
# Not touched: files outside <Root>. Run by install_task.ps1; safe to repeat.
# Output: one line per target, then a count. Exit 0 ok, 1 an ACL could not be set, 2 usage error.
param([Parameter(Mandatory = $true)][string]$Root)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Root -PathType Container)) { [Console]::Error.WriteLine("Root is not a directory"); exit 2 }
$Root = (Resolve-Path -LiteralPath $Root).Path

$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$principals = @(
    $me,
    (New-Object System.Security.Principal.SecurityIdentifier ([System.Security.Principal.WellKnownSidType]::LocalSystemSid), $null),
    (New-Object System.Security.Principal.SecurityIdentifier ([System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid), $null)
)

function Protect-Path([string]$path) {
    $isDir = Test-Path -LiteralPath $path -PathType Container
    $item = if ($isDir) { New-Object System.IO.DirectoryInfo $path } else { New-Object System.IO.FileInfo $path }   # .NET, not Get-Acl: no module load
    $acl = $item.GetAccessControl()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }
    $inherit = if ($isDir) { [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [System.Security.AccessControl.InheritanceFlags]::None }
    foreach ($sid in $principals) {
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inherit, 'None', 'Allow')))
    }
    $item.SetAccessControl($acl)
}

# each target keeps the name it is reported under; a name is never cut out of the full path, because Resolve-Path and Get-ChildItem can spell the
# same root differently (8.3 short and long names)
$targets = New-Object System.Collections.Generic.List[object]
function Add-Target([string]$path, [string]$name) { $targets.Add([pscustomobject]@{ Path = $path; Name = $name }) }
Add-Target $Root '.'
$app = Join-Path $Root 'app'
if (Test-Path -LiteralPath $app -PathType Container) { Add-Target $app 'app' }
foreach ($f in 'run_pipeline.ps1', 'analyzer_args.txt') {
    $p = Join-Path $Root $f
    if (Test-Path -LiteralPath $p -PathType Leaf) { Add-Target $p $f }
}
Get-ChildItem -LiteralPath $Root -File -Filter 'enable_*.txt' | ForEach-Object { if ($_.Name -like 'enable_*.txt') { Add-Target $_.FullName $_.Name } }

$failed = 0
foreach ($t in $targets) {
    try { Protect-Path $t.Path; Write-Output "protected $($t.Name)" } catch { $failed++; Write-Output "FAILED $($t.Name)"; [Console]::Error.WriteLine($_.Exception.Message) }
}
Write-Output ("protect_root: {0} targets, {1} failed" -f $targets.Count, $failed)
exit ([int]($failed -gt 0))
