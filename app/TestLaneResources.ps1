$ErrorActionPreference = 'Stop'
$Root = Join-Path ([IO.Path]::GetTempPath()) ('LaneResources-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($Root) | Out-Null
$Runner = Join-Path $PSScriptRoot 'Invoke-LaneResource.ps1'
$Shell = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
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
$First = $null
try {
    $First = Start-Runner 'gpu' ('-NoProfile -File "{0}" -Root "{1}"' -f $Fixture,$Root) 'first'
    $Deadline = (Get-Date).AddSeconds(15)
    while (-not (Test-Path (Join-Path $Root 'running.json')) -and (Get-Date) -lt $Deadline) { Start-Sleep -Milliseconds 100 }
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
