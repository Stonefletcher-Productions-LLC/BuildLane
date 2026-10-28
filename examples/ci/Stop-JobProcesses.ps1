# Stops what a cancelled build step left behind. Windows ends a killed step's shell, not its
# children: an editor or commandlet that step started keeps running, holds the workspace's DLLs
# and logs open, and every later job on the same runner fails to link or write. The usual symptom
# is LNK1104, "<file> cannot be opened; it is being used by another process" - the build looks
# broken while the code is fine.
#
# Two independent matchers, both scoped to one workspace:
#   command line  a process whose command line names this workspace. Precise, but it only sees
#                 processes the current account can inspect.
#   probe path    a file under -ProbePath that cannot be opened for writing. Decisive, and it
#                 still reports a lock held by a process this account cannot see, such as one
#                 left by a Windows service in another session.
#
# The calling process and all of its ancestors are never matched, so a step cannot stop itself or
# the runner that started it. No process is matched by name alone; editors and jobs from other
# checkouts and other accounts are left untouched.
#
# -List reports and exits 1 without stopping anything: use it to wait before starting work, or to
# explain why a job cannot start. Without -List it stops what it matched, waits for the files to
# settle, and exits 1 only if something survived or a probe path is still locked.
#
# Usage: Stop-JobProcesses.ps1 -Workspace <path> [-ProbePath <path[]>] [-GraceSeconds 0]
#                              [-MaxWaitSeconds 30] [-List]
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string]$Workspace,
    [string[]]$ProbePath = @(),
    [ValidateRange(0,3600)][int]$GraceSeconds = 0,
    [ValidateRange(0,3600)][int]$MaxWaitSeconds = 30,
    [switch]$List
)
$ErrorActionPreference = 'Stop'

$WorkspaceItem = Get-Item -LiteralPath $Workspace -Force -ErrorAction Stop
$WorkspacePath = $WorkspaceItem.FullName.TrimEnd('\')
$WorkspacePrefix = "$WorkspacePath\"

$Probes = @()
foreach ($Path in $ProbePath) {
    $Item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $Item) { Write-Host "NOTE probe path does not exist, skipped: $Path"; continue }
    $Files = if ($Item.PSIsContainer) { @(Get-ChildItem -LiteralPath $Item.FullName -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 100) } else { @($Item) }
    if ($Files.Count -eq 0) { Write-Host "NOTE probe folder holds no files, skipped: $($Item.FullName)"; continue }
    $Probes += $Files
}

# The caller and its ancestors are off limits, whatever their command line says.
$Processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
$ParentOf = @{}
foreach ($Process in $Processes) { $ParentOf[[int]$Process.ProcessId] = [int]$Process.ParentProcessId }
$Protected = New-Object 'System.Collections.Generic.HashSet[int]'
$Walk = $PID
while ($Walk -gt 0 -and $ParentOf.ContainsKey($Walk) -and $Protected.Add($Walk)) { $Walk = $ParentOf[$Walk] }

function Test-RefersToWorkspace([string]$CommandLine) {
    if (-not $CommandLine) { return $false }
    $Text = $CommandLine -replace '/','\'
    if ($Text.IndexOf($WorkspacePrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
    # Also accept the bare path when it ends a token, as in -project "...\LifeShare.uproject".
    $At = $Text.IndexOf($WorkspacePath, [StringComparison]::OrdinalIgnoreCase)
    while ($At -ge 0) {
        $Next = $At + $WorkspacePath.Length
        if ($Next -ge $Text.Length -or @('\','"',"'",' ') -contains [string]$Text[$Next]) { return $true }
        $At = $Text.IndexOf($WorkspacePath, $At + 1, [StringComparison]::OrdinalIgnoreCase)
    }
    return $false
}

function Get-LockedProbe {
    foreach ($File in $Probes) {
        try { $Stream = [IO.File]::Open($File.FullName,'Open','ReadWrite','None'); $Stream.Dispose() }
        catch [System.UnauthorizedAccessException] { }   # not ours to write: says nothing about a lock
        catch [System.IO.IOException] { return $File.FullName }
        catch { }
    }
    return $null
}

$Owned = @($Processes | Where-Object {
    -not $Protected.Contains([int]$_.ProcessId) -and (Test-RefersToWorkspace $_.CommandLine)
} | ForEach-Object {
    [pscustomobject]@{ ProcessId = [int]$_.ProcessId; Name = $_.Name; Started = $_.CreationDate }
})

function Write-Finding([string]$Locked) {
    foreach ($Item in $Owned) { Write-Host ("HELD {0} PID {1}, started {2}" -f $Item.Name, $Item.ProcessId, $Item.Started) }
    if ($Locked) { Write-Host "HELD an uninspectable process is using $Locked" }
}

$Locked = Get-LockedProbe
if ($Owned.Count -eq 0 -and -not $Locked) { Write-Host "Free: $WorkspacePath"; exit 0 }

if ($List) {
    Write-Host "Busy: $WorkspacePath"
    Write-Finding $Locked
    exit 1
}

if ($GraceSeconds -gt 0) {
    Write-Host "Waiting $GraceSeconds s for $($Owned.Count) process(es) to exit"
    $Deadline = [DateTime]::UtcNow.AddSeconds($GraceSeconds)
    do {
        $Owned = @($Owned | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
        if ($Owned.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)
}

$Refused = @()
foreach ($Item in $Owned) {
    try { Stop-Process -Id $Item.ProcessId -Force -ErrorAction Stop; Write-Host "Stopped $($Item.Name) PID $($Item.ProcessId)" }
    catch { $Refused += $Item; Write-Host "CANNOT STOP $($Item.Name) PID $($Item.ProcessId): $($_.Exception.Message)" }
}

$Deadline = [DateTime]::UtcNow.AddSeconds($MaxWaitSeconds)
$Left = @($Owned | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
while ($Left.Count -gt 0 -and [DateTime]::UtcNow -lt $Deadline) {
    Start-Sleep -Milliseconds 250
    $Left = @($Left | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
}

# Handles are released just after the process object goes away, so keep probing before judging.
if ($Probes.Count -gt 0) {
    $Deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(5, $MaxWaitSeconds))
    do {
        $Locked = Get-LockedProbe
        if (-not $Locked) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)
}

if ($Left.Count -eq 0 -and -not $Locked) { Write-Host "Cleared: $WorkspacePath"; exit 0 }

foreach ($Item in $Left) { Write-Host "STILL RUNNING $($Item.Name) PID $($Item.ProcessId)" }
if ($Locked) { Write-Host "STILL LOCKED $Locked" }
if ($Refused.Count -gt 0) { Write-Host "Windows refused to stop $($Refused.Count) process(es). A process a Windows service started runs in another session: stop that service, or run this step from an elevated shell." }
exit 1
