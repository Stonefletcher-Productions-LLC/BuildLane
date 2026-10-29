# Profile persistence stays outside the installation so upgrades never replace user settings.
function Test-LaneProfile($Value) {
    foreach ($Key in @('name','repo','branch','workflow','statusFile','pauseFile','runnerPath','drive','serviceFilter')) {
        if ([string]::IsNullOrWhiteSpace([string]$Value.$Key)) { throw "Enter a value for $Key." }
    }
    if ($Value.planWorkflow -and [string]$Value.planWorkflow -notmatch '^[A-Za-z0-9_.-]+\.ya?ml$') { throw 'Planning workflow must be a YAML filename, for example build-plan-shadow.yml.' }
    if ($Value.repo -notmatch '^[\w.-]+/[\w.-]+$') { throw 'Repository must be owner/repository.' }
    if ($Value.drive -notmatch '^[A-Za-z]$') { throw 'Drive must be one letter, for example E.' }
    foreach ($Key in @('statusFile','pauseFile','runnerPath')) {
        if ($Value.$Key -notmatch '^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+\\)' -or $Value.$Key -match '["<>|]') { throw "$Key must be an absolute Windows path." }
    }
    if ([string]$Value.statusFile -eq [string]$Value.pauseFile) { throw 'Status and pause files must be different.' }
    foreach ($Key in @('cachePort','minimumFreeGB','cleanBelowGB')) {
        $Number = 0
        if (-not [int]::TryParse([string]$Value.$Key, [ref]$Number) -or $Number -lt 0) { throw "$Key must be a non-negative whole number." }
    }
    if ([int]$Value.cachePort -gt 65535) { throw 'Cache port must be 0 through 65535.' }
    if ([int]$Value.cleanBelowGB -lt [int]$Value.minimumFreeGB) { throw 'Cleanup threshold must be at least the minimum free space.' }
}
function Read-LaneProfile([string]$Path) {
    $Value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    foreach ($Key in @('statusFile','pauseFile','runnerPath')) { $Value.$Key=[Environment]::ExpandEnvironmentVariables([string]$Value.$Key) }
    Test-LaneProfile $Value
    $Value
}
function Save-LaneProfile([string]$Path, $Value, [string]$ExpectedHash) {
    Test-LaneProfile $Value
    if (Test-Path -LiteralPath $Path) {
        if (-not $ExpectedHash -or (Get-FileHash -LiteralPath $Path).Hash -ne $ExpectedHash) { throw 'This profile changed on disk. Reopen it before saving.' }
    } elseif ($ExpectedHash) { throw 'This profile was removed. Save it as a new project.' }
    $Folder = Split-Path ([IO.Path]::GetFullPath($Path))
    [void][IO.Directory]::CreateDirectory($Folder)
    $Temporary = Join-Path $Folder ([guid]::NewGuid().ToString() + '.tmp')
    try {
        [IO.File]::WriteAllText($Temporary, ($Value | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($Temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($Temporary, $Path) }
    } finally { if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force } }
}

# Accept repository links pasted from a browser, but never arbitrary hosts or URL tails.
function ConvertTo-LaneRepository([string]$Text) {
    $Name=$Text.Trim().TrimEnd('/')
    if ($Name -match '^https://github\.com/([^/?#]+/[^/?#]+)$') { $Name=$Matches[1] }
    $Name=$Name -replace '\.git$', ''
    if ($Name -notmatch '^[\w.-]+/[\w.-]+$') { throw 'Enter owner/repository or an https://github.com/owner/repository link.' }
    $Name
}
function New-LaneProjectDefaults {
    $Folder=Join-Path $env:LOCALAPPDATA 'Buildlane\Runner'
    [pscustomobject]@{name='';repo='';branch='main';workflow='lane.yml';planWorkflow='';statusFile=(Join-Path $Folder 'status.json');pauseFile=(Join-Path $Folder 'PAUSED');runnerPath=$Folder;drive=([IO.Path]::GetPathRoot($Folder)).Substring(0,1);serviceFilter='*your-project*';cachePort=0;minimumFreeGB=12;cleanBelowGB=25}
}
