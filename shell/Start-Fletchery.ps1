# Runs the Fletchery backend without the desktop app, and starts it again when it exits to reload.
#
# The backend exits with code 75 after new code is installed (it drains running sessions first). The desktop
# app restarts only a backend it started itself, so a backend started bare (`python -m fletchery run`) stays
# down after an install. This script is that supervisor: exit 75 starts it again at once, a crash starts it
# again after 5 seconds (giving up after 4 crashes in 5 minutes), and exit 0 (Ctrl+C, Quit) stays stopped.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File shell\Start-Fletchery.ps1 -Profile C:\path\Project.profile.json
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',
#       '<this file>','-Profile','<profile>','-LogDir','<state folder>'   # detached, outlives the shell
#
# -FletcheryHome sets FLETCHERY_HOME (where the project list and settings live) when that is not %APPDATA%\Fletchery,
# for example inside an app container. The backend refuses to start beside another one (fletchery.lock).
param(
    [Parameter(Mandatory = $true)][Alias('Profile')][string]$ProjectProfile,
    [string]$Python = 'python',
    [string]$Backend = (Join-Path $env:LOCALAPPDATA 'Programs\Fletchery'),
    [string]$LogDir = (Join-Path $env:LOCALAPPDATA 'Fletchery\logs'),
    [string]$FletcheryHome = ''
)
$ErrorActionPreference = 'Continue'
if (-not (Test-Path $ProjectProfile)) { throw "No profile at $ProjectProfile" }
New-Item -ItemType Directory -Force $LogDir | Out-Null
if ($FletcheryHome) { $env:FLETCHERY_HOME = $FletcheryHome }
$env:PYTHONUNBUFFERED = '1'; $env:PYTHONIOENCODING = 'utf-8'; $env:PYTHONUTF8 = '1'
$journal = Join-Path $LogDir 'backend-supervisor.log'
function Note([string]$Text) { "$(Get-Date -Format o) $Text" | Add-Content $journal }

$crashes = @()
while ($true) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $out = Join-Path $LogDir 'backend-stdout.log'; $err = Join-Path $LogDir 'backend-stderr.log'
    foreach ($f in @($out, $err)) {
        if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) { Move-Item $f ($f -replace '\.log$', "-$stamp.log") -Force }
    }
    Note 'starting backend'
    $p = Start-Process -FilePath $Python -ArgumentList @('-m', 'fletchery', 'run', '--profile', "`"$ProjectProfile`"") `
        -WorkingDirectory $Backend -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    $null = $p.Handle   # Windows PowerShell only fills ExitCode if the handle was opened while it ran
    try { $p.PriorityClass = 'BelowNormal' } catch {}
    $p.WaitForExit()
    $code = $p.ExitCode
    Note "backend exited $code"
    if ($code -eq 75) { continue }   # new code installed: reload right away
    if ($code -eq 0) { break }       # a clean stop stays stopped
    $now = Get-Date
    $crashes = @($crashes | Where-Object { ($now - $_).TotalMinutes -lt 5 }) + $now
    if ($crashes.Count -gt 3) { Note '4 crashes in 5 minutes; not starting again'; break }
    Start-Sleep -Seconds 5
}
