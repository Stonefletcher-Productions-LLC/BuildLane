# Installs the Buildlane local runner for the signed-in Windows user and starts it at logon.
# The runner runs the repository's GitHub Actions workflows on this PC (see runner\README.md).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File runner\Install-LocalRunner.ps1 [-Config <file>] [-Start]
#   powershell -NoProfile -ExecutionPolicy Bypass -File runner\Install-LocalRunner.ps1 -Uninstall
param(
    [string]$Config = (Join-Path $env:LOCALAPPDATA 'Buildlane\LocalRunner\runner.json'),
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'Programs\Buildlane\runner'),
    [string]$TaskName = 'Buildlane local runner',
    [switch]$Start,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'

if ($Uninstall) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
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
$Command = "& '$($Node.Source)' '$Script' serve --config '$Config' *>> '$Log'"
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$Command`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -Priority 7
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Runs GitHub Actions workflows on this PC (Buildlane local runner).' -Force | Out-Null
Write-Host "Registered '$TaskName' (starts at logon). Config: $Config"
if ($Start) { Start-ScheduledTask -TaskName $TaskName; Write-Host 'Started.' }
