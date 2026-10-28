$ErrorActionPreference = 'Stop'
$Root = Join-Path ([IO.Path]::GetTempPath()) ('LaneProcesses-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($Root) | Out-Null
$Script = Join-Path (Split-Path $PSScriptRoot -Parent) 'examples\ci\Stop-JobProcesses.ps1'
$Other = Join-Path ([IO.Path]::GetTempPath()) ('LaneProcesses-' + [guid]::NewGuid().ToString('N') + '-other')
[IO.Directory]::CreateDirectory($Other) | Out-Null
$Shell = (Get-Process -Id $PID).Path
$Fixture = Join-Path $Root 'fixture.ps1'
$Bystander = Join-Path $Other 'bystander.ps1'
# The job under test: a step's shell that spawned a child of its own and keeps a file open. Nothing
# ends it for us - the wrapper that started it is killed first, exactly like a cancelled CI step.
@'
param([string]$Root)
$Project = Join-Path $Root 'project.uproject'
Set-Content -LiteralPath $Project -Value '{}'
$Grandchild = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList ('-NoProfile -File "{0}" -Root "{1}"' -f (Join-Path $Root 'sleep.ps1'),$Root) -WindowStyle Hidden -PassThru
$Hold = [IO.File]::Open((Join-Path $Root 'held.dll'),'OpenOrCreate','ReadWrite','None')
@{pid=$PID;grandchild=$Grandchild.Id} | ConvertTo-Json | Set-Content (Join-Path $Root 'running.json')
Start-Sleep -Seconds 120
'@ | Set-Content -LiteralPath $Fixture
# A second, unrelated job whose path merely starts with the same text: it must never be matched.
@'
param([string]$Root)
@{pid=$PID} | ConvertTo-Json | Set-Content (Join-Path $Root 'running.json')
Start-Sleep -Seconds 120
'@ | Set-Content -LiteralPath $Bystander
'param([string]$Root) Start-Sleep -Seconds 120' | Set-Content -LiteralPath (Join-Path $Root 'sleep.ps1')
function Assert([bool]$Ok,[string]$Message) { if (-not $Ok) { throw $Message } }
function Wait-For([string]$Path,[int]$Seconds = 20) {
    $Deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (Test-Path -LiteralPath $Path) -and (Get-Date) -lt $Deadline) { Start-Sleep -Milliseconds 100 }
    Assert (Test-Path -LiteralPath $Path) "Timed out waiting for $Path"
    Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}
$Job = $null; $BystanderJob = $null
try {
    $Job = Start-Process $Shell -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Root "{1}"' -f $Fixture,$Root) -WindowStyle Hidden -PassThru
    $Running = Wait-For (Join-Path $Root 'running.json')
    $BystanderJob = Start-Process $Shell -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Root "{1}"' -f $Bystander,$Other) -WindowStyle Hidden -PassThru
    $Waiting = Wait-For (Join-Path $Other 'running.json')

    & $Script -Workspace $Root -List | Out-Null
    Assert ($LASTEXITCODE -eq 1) '-List did not report the busy workspace.'
    Assert ([bool](Get-Process -Id $Running.pid -ErrorAction SilentlyContinue)) '-List stopped a process.'
    Assert ([bool](Get-Process -Id $Running.grandchild -ErrorAction SilentlyContinue)) '-List stopped a grandchild.'

    & $Script -Workspace $Root -ProbePath (Join-Path $Root 'held.dll') -MaxWaitSeconds 30 | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'Cleanup reported failure on a workspace it could clear.'
    Assert (-not [bool](Get-Process -Id $Running.pid -ErrorAction SilentlyContinue)) 'The job survived cleanup.'
    Assert (-not [bool](Get-Process -Id $Running.grandchild -ErrorAction SilentlyContinue)) 'The grandchild survived cleanup.'
    $Free = $false
    try { $Probe = [IO.File]::Open((Join-Path $Root 'held.dll'),'Open','ReadWrite','None'); $Probe.Dispose(); $Free = $true } catch { }
    Assert $Free 'The file the job held is still locked.'
    Assert ([bool](Get-Process -Id $Waiting.pid -ErrorAction SilentlyContinue)) 'An unrelated job with a similar path was stopped.'

    & $Script -Workspace $Root -ProbePath (Join-Path $Root 'held.dll') -List | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'The cleared workspace still reports as busy.'

    # The probe alone has to catch a lock nobody can attribute to a process.
    $Hold = [IO.File]::Open((Join-Path $Root 'held.dll'),'Open','ReadWrite','None')
    try {
        & $Script -Workspace $Root -ProbePath (Join-Path $Root 'held.dll') -List | Out-Null
        Assert ($LASTEXITCODE -eq 1) 'A locked probe file was reported as free.'
    } finally { $Hold.Dispose() }

    Write-Host "PROCESS CHECKS PASSED: listing, job and grandchild cleanup, file lock release, unrelated jobs untouched. Evidence: $Root"
} finally {
    foreach ($Process in @($Job,$BystanderJob)) {
        if ($Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue }
    }
}
