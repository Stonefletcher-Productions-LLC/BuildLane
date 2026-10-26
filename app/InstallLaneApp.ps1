# Install the reusable desktop client without changing runner services or queue state.
param(
    [string]$Destination = "$env:LOCALAPPDATA\Programs\Buildlane",
    [string]$Profile = (Join-Path $PSScriptRoot 'BuildLane.profile.json'),
    [string]$ShortcutName = 'Buildlane',
    [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
    [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$Config = Get-Content -LiteralPath $Profile -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Config.name -or $Config.repo -notmatch '^[\w.-]+/[\w.-]+$') { throw 'Profile requires a name and owner/repository.' }
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$LauncherHash = (Get-FileHash (Join-Path $PSScriptRoot 'BuildLaneLauncher.cs')).Hash
$Receipt = Join-Path $Destination 'launcher.sha256'
$NeedsCompile = -not (Test-Path (Join-Path $Destination 'Buildlane.exe')) -or -not (Test-Path $Receipt) -or (Get-Content $Receipt -ErrorAction SilentlyContinue) -ne $LauncherHash
foreach ($Name in @('LaneApp.ps1', 'LaneProfiles.ps1', 'LaneGitHub.ps1', 'LaneRefresh.ps1','LaneMoves.ps1', 'LaneProjectsUi.ps1', 'BuildLane.ico', 'BuildLaneLauncher.cs')) {
    $Source = Join-Path $PSScriptRoot $Name
    $Target = Join-Path $Destination $Name
    if ([IO.Path]::GetFullPath($Source) -ne [IO.Path]::GetFullPath($Target)) { Copy-Item -LiteralPath $Source -Destination $Target -Force }
}
$TargetProfile = Join-Path $Destination 'BuildLane.profile.json'
if ([IO.Path]::GetFullPath($Profile) -ne [IO.Path]::GetFullPath($TargetProfile)) { Copy-Item -LiteralPath $Profile -Destination $TargetProfile -Force }
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$Exe = Join-Path $Destination 'Buildlane.exe'
if ($NeedsCompile) {
& $Compiler /nologo /target:winexe /reference:System.Windows.Forms.dll "/win32icon:$Destination\BuildLane.ico" "/out:$Exe" (Join-Path $Destination 'BuildLaneLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Buildlane launcher compilation failed.' }
Set-Content -LiteralPath $Receipt -Value $LauncherHash
}
if (-not $NoShortcut) {
    $Link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $Desktop "$ShortcutName.lnk"))
    $Link.TargetPath = $Exe; $Link.WorkingDirectory = $Destination
    if ($PSBoundParameters.ContainsKey('Profile') -or $ShortcutName -ne 'Buildlane') { $Link.Arguments = '"' + $TargetProfile + '"' }
    else { $Link.Arguments = '' }
    $Link.IconLocation = "$Destination\BuildLane.ico,0"
    $Link.Description = "Buildlane - $($Config.name)"
    $Link.Save()
}
Write-Output $Exe
