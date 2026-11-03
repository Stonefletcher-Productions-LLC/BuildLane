# Installs Fletchery for this Windows user (no administrator rights): builds Fletchery.exe if needed, copies the app,
# the backend (fletchery\) and the screens (ui\) to %LOCALAPPDATA%\Programs\Fletchery, checks Python, sets the project
# profile, and adds Desktop and Start menu shortcuts. Re-run it to update: a running Fletchery picks up the new backend
# between passes (it restarts itself, never mid-session); reopen the window for a new Fletchery.exe.
#   powershell -NoProfile -ExecutionPolicy Bypass -File shell\Install-Fletchery.ps1 -Profile C:\path\Project.profile.json
# -Profile is bound to $ProjectProfile: $Profile is a PowerShell automatic variable.
param([Alias('Profile')][string]$ProjectProfile, [string]$Python, [switch]$Start, [switch]$NoShortcuts)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path $PSScriptRoot -Parent
$Dest = Join-Path $env:LOCALAPPDATA 'Programs\Fletchery'
$AppData = Join-Path $env:APPDATA 'Fletchery'
New-Item -ItemType Directory -Force $Dest, (Join-Path $AppData 'Projects') | Out-Null

# --- build the app if it is not built yet ---
if (-not (Test-Path (Join-Path $PSScriptRoot 'bin\Fletchery.exe'))) { & (Join-Path $PSScriptRoot 'Build-Fletchery.ps1') }

# --- Python 3.10+ with the Claude Agent SDK ---
if (-not $Python) {
    foreach ($Candidate in @('py', 'python')) {
        $C = Get-Command $Candidate -ErrorAction SilentlyContinue
        if (-not $C -or $C.Source -like '*\WindowsApps\*') { continue }  # the Store placeholder is not Python
        $Exe = if ($Candidate -eq 'py') { (& py -3 -c 'import sys; print(sys.executable)' 2>$null) } else { (& python -c 'import sys; print(sys.executable)' 2>$null) }
        if ($Exe -and (Test-Path $Exe)) { $Python = $Exe; break }
    }
}
if (-not $Python -or -not (Test-Path $Python)) { throw 'Python 3.10 or newer was not found. Install it from python.org (tick "Add to PATH"), then run this again, or pass -Python <path>.' }
# no double quotes inside: Windows PowerShell 5.1 drops them when it passes arguments to a native program
$Ver = & $Python -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))"
if (-not $Ver -or [version]$Ver -lt [version]'3.10') { throw "Fletchery needs Python 3.10 or newer; $Python is $Ver." }
& $Python -c 'import claude_agent_sdk' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing the Claude Agent SDK for this user'
    & $Python -m pip install --user --quiet claude-agent-sdk
    if ($LASTEXITCODE -ne 0) { throw 'pip could not install claude-agent-sdk.' }
}

# --- copy the app ---
Copy-Item (Join-Path $PSScriptRoot 'bin\*') $Dest -Force
foreach ($Part in 'fletchery', 'ui') {
    $Target = Join-Path $Dest $Part
    New-Item -ItemType Directory -Force $Target | Out-Null
    # robocopy: only changed files, so a running backend sees a real change (and restarts between passes) only on update
    robocopy (Join-Path $Repo $Part) $Target /MIR /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $Part failed (robocopy $LASTEXITCODE)." }
}
$global:LASTEXITCODE = 0

# --- settings: profile, Python, backend folder ---
$SettingsFile = Join-Path $AppData 'settings.json'
$Settings = if (Test-Path $SettingsFile) { Get-Content $SettingsFile -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if ($ProjectProfile) {
    if (-not (Test-Path $ProjectProfile)) { throw "Profile not found: $ProjectProfile" }
    $Name = (Get-Content $ProjectProfile -Raw | ConvertFrom-Json).name
    $Copy = Join-Path $AppData "Projects\$Name.profile.json"
    if ((Resolve-Path $ProjectProfile).Path -ne $Copy) { Copy-Item $ProjectProfile $Copy -Force }
    $Settings | Add-Member -NotePropertyName profile -NotePropertyValue $Copy -Force
}
$Settings | Add-Member -NotePropertyName python -NotePropertyValue $Python -Force
$Settings | Add-Member -NotePropertyName backend -NotePropertyValue $Dest -Force
$Settings | ConvertTo-Json | Set-Content $SettingsFile -Encoding UTF8
if (-not $Settings.profile) { Write-Warning 'No project profile yet: run this again with -Profile <file>.' }

# --- shortcuts ---
if (-not $NoShortcuts) {
    $Shell = New-Object -ComObject WScript.Shell
    foreach ($Dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'))) {
        $Lnk = $Shell.CreateShortcut((Join-Path $Dir 'Fletchery.lnk'))
        $Lnk.TargetPath = Join-Path $Dest 'Fletchery.exe'
        $Lnk.WorkingDirectory = $Dest
        $Lnk.Description = 'Fletchery: task queue, Claude workers and the Build Lane'
        $Lnk.Save()
    }
}
Write-Host "Installed Fletchery in $Dest (Python $Ver at $Python; profile $($Settings.profile))"
if ($Start) { Start-Process (Join-Path $Dest 'Fletchery.exe') }
