$ErrorActionPreference = 'Stop'
$Root = Join-Path ([IO.Path]::GetTempPath()) ('LaneResources-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($Root) | Out-Null
$Runner = Join-Path $PSScriptRoot 'Invoke-LaneResource.ps1'
$Shell = (Get-Process -Id $PID).Path
$Fixture = Join-Path $Root 'fixture.ps1'
@'
param([string]$Root)
$Child = Start-Process powershell.exe -ArgumentList '-NoProfile -Command Start-Sleep -Seconds 60' -WindowStyle Hidden -PassThru
@{pid=$PID;child=$Child.Id} | ConvertTo-Json | Set-Content (Join-Path $Root 'running.json')
Start-Sleep -Seconds 60
'@ | Set-Content -LiteralPath $Fixture
function Start-Runner([string]$Resources, [string]$Payload, [string]$Label) {
    $Command = "& '{0}' -Resource '{1}' -Owner test -Purpose test -ReservationDirectory '{2}' -Executable '{3}' -Arguments '{4}'" -f $Runner,$Resources,$Root,$Shell,$Payload
    $Command += '; exit $LASTEXITCODE'
    $Args = '-NoProfile -ExecutionPolicy Bypass -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    $Process = Start-Process $Shell -ArgumentList $Args -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Root "$Label.out") -RedirectStandardError (Join-Path $Root "$Label.err")
    $null = $Process.Handle
    return $Process
}
function Assert([bool]$Ok,[string]$Message) { if (-not $Ok) { throw $Message } }
function Write-StartupDiagnostics($Process) {
    $Process.Refresh()
    $Exit = if ($Process.HasExited) { "exited with code $($Process.ExitCode)" } else { 'still running at the 15-second startup deadline' }
    Write-Host "STARTUP FAILURE: wrapper PID $($Process.Id) $Exit; shell $Shell; evidence $Root"
    foreach ($Name in @('first.out','first.err')) {
        $Path=Join-Path $Root $Name
        $Tail=if (Test-Path -LiteralPath $Path) { (Get-Content -LiteralPath $Path -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { '<file missing>' }
        if (-not $Tail) { $Tail='<empty>' }
        if ($Tail.Length -gt 4096) { $Tail=$Tail.Substring($Tail.Length-4096) }
        Write-Host "${Name} (last 40 lines, at most 4096 characters):`n$Tail"
    }
    try {
        $Status=(& $Runner -Resource gpu -ReservationDirectory $Root -Status | ConvertTo-Json -Depth 6 -Compress) -join "`n"
        if ($Status.Length -gt 4096) { $Status=$Status.Substring(0,4096) }
        Write-Host "Reservation status: $Status"
    } catch { Write-Host "Reservation status unavailable: $($_.Exception.Message)" }
}
$First = $null
try {
    $First = Start-Runner 'gpu' ('-NoProfile -File "{0}" -Root "{1}"' -f $Fixture,$Root) 'first'
    $Deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Path (Join-Path $Root 'running.json')) -and (Get-Date) -lt $Deadline -and -not $First.HasExited) { Start-Sleep -Milliseconds 100 }
    if (-not (Test-Path (Join-Path $Root 'running.json'))) { Write-StartupDiagnostics $First }
    Assert (Test-Path (Join-Path $Root 'running.json')) 'Child did not start.'
    $Owned = Get-Content (Join-Path $Root 'running.json') -Raw | ConvertFrom-Json
    $State = & $Runner -Resource gpu -ReservationDirectory $Root -Status
    Assert ($State.held -and $State.owner.owner -eq 'test') 'Status did not report the holder.'
    $Second = Start-Runner 'gpu' '-NoProfile -Command exit 0' 'second'
    Assert ($Second.WaitForExit(15000)) 'Contender hung.'
    Assert ($Second.ExitCode -ne 0) 'Concurrent owner incorrectly acquired the resource.'
    # Another independent resource must remain available; preserve child exit code.
    $Other = Start-Runner 'build' '-NoProfile -Command exit 7' 'other'
    Assert ($Other.WaitForExit(15000)) 'Independent resource hung.'
    Assert ($Other.ExitCode -eq 7) 'Child exit code was not preserved.'
    Stop-Process -Id $First.Id -Force
    $First.WaitForExit()
    Start-Sleep -Milliseconds 500
    Assert (-not (Get-Process -Id $Owned.pid -ErrorAction SilentlyContinue)) 'Abandoned child survived wrapper termination.'
    Assert (-not (Get-Process -Id $Owned.child -ErrorAction SilentlyContinue)) 'Abandoned grandchild survived wrapper termination.'
    $Third = Start-Runner 'gpu' '-NoProfile -Command exit 0' 'third'
    Assert ($Third.WaitForExit(15000)) 'Resource was not recovered after owner death.'
    Assert ($Third.ExitCode -eq 0) 'Recovery failed.'
    $State = & $Runner -Resource gpu -ReservationDirectory $Root -Status
    Assert (-not $State.held) 'Stale receipt was mistaken for a live reservation.'
    Write-Host "RESOURCE CHECKS PASSED: contention, independent jobs, exit code, process-tree cleanup, recovery. Evidence: $Root"
} finally {
    if ($First -and -not $First.HasExited) { Stop-Process -Id $First.Id -Force }
}
