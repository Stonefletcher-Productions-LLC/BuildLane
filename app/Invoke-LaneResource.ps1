# Shared, opt-in reservations across worktrees for this Windows account.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateNotNullOrEmpty()][string[]]$Resource,
    [string]$Owner,
    [string]$Purpose,
    [string]$Executable,
    [string]$Arguments = '',
    [string]$WorkingDirectory = (Get-Location).Path,
    [ValidateRange(0,86400)][int]$WaitSeconds = 0,
    [string]$ReservationDirectory = "$env:LOCALAPPDATA\Buildlane\Reservations",
    [switch]$Status
)
$ErrorActionPreference = 'Stop'
$Names = @($Resource | Sort-Object -Unique)
foreach ($Name in $Names) {
    if ($Name -cnotmatch '^[a-z0-9][a-z0-9_-]{0,63}$') { throw "Use a lowercase resource name: $Name" }
}
[IO.Directory]::CreateDirectory($ReservationDirectory) | Out-Null
function Open-Reservation([string]$Name) {
    [IO.File]::Open((Join-Path $ReservationDirectory "$Name.lock"),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read)
}
if ($Status) {
    foreach ($Name in $Names) {
        $Held = $false; $Details = $null
        try { $Probe = Open-Reservation $Name; $Probe.Dispose() }
        catch [IO.IOException] { $Held = $true }
        if ($Held) {
            try { $Details = Get-Content -LiteralPath (Join-Path $ReservationDirectory "$Name.json") -Raw | ConvertFrom-Json }
            catch { $Details = @{ note = 'Owner is acquiring the reservation; retry status.' } }
        }
        [pscustomobject]@{ resource=$Name; held=$Held; owner=$Details }
    }
    return
}
if (-not $Owner -or -not $Purpose -or -not $Executable) { throw 'Owner, Purpose and Executable are required to run a job.' }
$Executable = (Get-Command $Executable -CommandType Application -ErrorAction Stop).Source
$WorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
if (-not ('LaneJob' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'LaneJob.cs') }
$Deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
$Handles = @()
try {
    do {
        $Busy = $null
        foreach ($Name in $Names) {
            try { $Handles += Open-Reservation $Name }
            catch [IO.IOException] { $Busy = $Name; break }
        }
        if (-not $Busy) { break }
        foreach ($Handle in $Handles) { $Handle.Dispose() }; $Handles = @()
        if ([DateTime]::UtcNow -ge $Deadline) { throw "Resource '$Busy' is busy. Inspect -Status or retry with -WaitSeconds." }
        Start-Sleep -Milliseconds 250
    } while ($true)
    $Receipt = [ordered]@{ owner=$Owner; purpose=$Purpose; pid=$PID; startedUtc=[DateTime]::UtcNow.ToString('o'); resources=$Names; executable=$Executable; workingDirectory=$WorkingDirectory }
    foreach ($Name in $Names) {
        $Path = Join-Path $ReservationDirectory "$Name.json"
        $Temp = "$Path.$PID.tmp"
        [IO.File]::WriteAllText($Temp,($Receipt | ConvertTo-Json -Depth 4))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($Temp,$Path,[NullString]::Value) }
        else { [IO.File]::Move($Temp,$Path) }
    }
    Write-Host "Reserved $($Names -join ', ') for $Owner : $Purpose"
    $Result = [LaneJob]::Run($Executable,$Arguments,$WorkingDirectory)
} finally {
    foreach ($Handle in $Handles) { $Handle.Dispose() }
}
exit $Result
