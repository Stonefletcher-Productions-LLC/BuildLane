# Installs the Buildlane local runner for the signed-in Windows user and starts it at logon.
# The runner runs the repository's GitHub Actions workflows on this PC (see runner\README.md).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File runner\Install-LocalRunner.ps1 [-Config <file>] [-Start]
#   powershell -NoProfile -ExecutionPolicy Bypass -File runner\Install-LocalRunner.ps1 -Uninstall
param(
    [string]$Config = (Join-Path $env:USERPROFILE 'buildlane-local\runner.json'),
    [string]$Destination = (Join-Path $env:USERPROFILE 'buildlane-local\runner'),
    [string]$TaskName = 'Buildlane local runner',
    [switch]$Start,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

# Stops the task and any runner process started from $Destination (by command line), and nothing else.
function Stop-LocalRunner {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    $Mine = Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($Destination, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
    foreach ($P in $Mine) { Write-Host "Stopping runner process $($P.ProcessId)"; Stop-Process -Id $P.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Stop-LocalRunner
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    Write-Host "Removed the scheduled task '$TaskName'. Run history and config are kept."
    return
}

$Node = Get-Command node -ErrorAction SilentlyContinue
if (-not $Node) { throw 'Node.js 18 or newer is required (https://nodejs.org). Install it, then run this again.' }
$Version = (& $Node.Source -v).Trim().TrimStart('v')
if ([int]($Version.Split('.')[0]) -lt 18) { throw "Node.js 18 or newer is required; found $Version." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git for Windows is required.' }
if (-not (Test-Path 'C:\Program Files\Git\bin\bash.exe')) { Write-Warning 'Git Bash was not found in C:\Program Files\Git. Set bashPath in the config for workflows written for Linux runners.' }

$Source = $PSScriptRoot
# An MSIX-packaged app (Claude desktop, for one) sees a private copy of AppData; files written there from
# inside it are invisible to the scheduled task, which runs outside the package.
if ($env:LOCALAPPDATA -like '*\Packages\*' -or $Config -like '*\Packages\*\LocalCache\*') { Write-Warning "This shell runs inside a packaged app, so AppData is virtualised here. Keep the config outside AppData (the default, $env:USERPROFILE\buildlane-local)." }
Stop-LocalRunner
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
foreach ($Item in 'buildlane-runner.cjs', 'lib', 'vendor', 'actions', 'runner.example.json', 'README.md') {
    Copy-Item -LiteralPath (Join-Path $Source $Item) -Destination $Destination -Recurse -Force
}

if (-not (Test-Path -LiteralPath $Config)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Config) | Out-Null
    Copy-Item -LiteralPath (Join-Path $Source 'runner.example.json') -Destination $Config
    Write-Host "Wrote a starting config to $Config. It starts in shadow mode (reads GitHub, never writes)."
}

$Script = Join-Path $Destination 'buildlane-runner.cjs'
$Log = Join-Path (Split-Path $Config) 'runner-console.log'
# Task Scheduler mangles quoting and redirection in an inline -Command, so the task runs a small launcher file.
$Launcher = Join-Path $Destination 'Start-LocalRunner.ps1'
$Quote = { param($s) "'" + ($s -replace "'", "''") + "'" }
@(
    '# Written by Install-LocalRunner.ps1: starts the Buildlane local runner and keeps its console output.'
    "`$Node = $(& $Quote $Node.Source)"
    "`$Script = $(& $Quote $Script)"
    "`$Config = $(& $Quote $Config)"
    "`$Log = $(& $Quote $Log)"
    'Set-Location -LiteralPath (Split-Path -LiteralPath $Script)'
    '"$(Get-Date -Format o) starting" | Out-File -LiteralPath $Log -Append -Encoding utf8'
    '& $Node $Script serve --config $Config --quiet --parent-pid $PID 2>&1 | Out-File -LiteralPath $Log -Append -Encoding utf8'
    '"$(Get-Date -Format o) exited with $LASTEXITCODE" | Out-File -LiteralPath $Log -Append -Encoding utf8'
    'exit $LASTEXITCODE'
) -join "`r`n" | Set-Content -LiteralPath $Launcher -Encoding ASCII
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Launcher`"" -WorkingDirectory $Destination
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -Priority 7
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Runs GitHub Actions workflows on this PC (Buildlane local runner).' -Force | Out-Null
Write-Host "Registered '$TaskName' (starts at logon). Config: $Config"
if ($Start) { Start-ScheduledTask -TaskName $TaskName; Write-Host 'Started.' }
