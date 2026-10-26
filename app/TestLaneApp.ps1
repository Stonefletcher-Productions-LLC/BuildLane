# Run with Windows PowerShell -STA. No GitHub calls, queue changes or service changes.
$ErrorActionPreference = 'Stop'
$FixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('buildlane-test-' + [guid]::NewGuid())
New-Item -ItemType Directory $FixtureRoot | Out-Null
try {
    $FixtureProfile = Join-Path $FixtureRoot 'project.json'
    $FixtureStatus = Join-Path $FixtureRoot 'status.json'
    @{
        name = 'Another Product'; repo = 'example/another-product'; branch = 'develop'; workflow = 'delivery.yml'
        statusFile = $FixtureStatus; pauseFile = "$FixtureRoot\PAUSED"; runnerPath = $FixtureRoot
        drive = 'C'; serviceFilter = '*buildlane-test-nonexistent*'; cachePort = 0; minimumFreeGB = 1; cleanBelowGB = 2
    } | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot\LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }
    Update-View
    Assert ($Form.Text -eq 'Buildlane | Another Product') 'Project branding leaked.'
    Assert ($Repo -eq 'example/another-product' -and $Branch -eq 'develop') 'Project settings did not load.'
    Assert ($TokenFile -notmatch 'LifeShare') 'Project credentials were not isolated.'
    Assert ($Title.Text -eq 'No build has reported yet') 'Missing status is not handled.'
    $Fixture = @{ updated = (Get-Date).ToString('o'); started = (Get-Date).AddMinutes(-1).ToString('o'); phase = 0
        phases = @('Build', 'Test'); what = 'sample'; step = 'Build'; progress = @{done=1;total=2}
        queue = @(@{number=7; title='Sample change'; state='waiting'}); notes=@(); done='' }
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($JobBar.Value -eq 250 -and $StepBar.Value -eq 500) "Progress is incorrect: job=$($JobBar.Value), step=$($StepBar.Value), status=$($Sub.Text), file=$JsonFile"
    Set-Content $FixtureStatus '{incomplete'
    Update-View
    Assert ($script:State.what -eq 'sample' -and $Sub.Text -match 'last valid') 'Malformed status did not preserve the last valid update.'
    $Fixture.done = 'failure'
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($Title.Text -match 'FAILED') 'Failure state is missing.'
    $Fixture.done = 'success'
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($Title.Text -match 'passed') 'Success state is missing.'
    Set-Content $PauseFile 'test fixture only'
    Update-View
    Assert ($PauseButton.Text -eq 'Resume builds') 'Pause state is incorrect.'
    Assert ($SystemTab.Controls.Contains($SystemRoot)) 'Machine health tab is missing.'
    $SavedPath=Join-Path $FixtureRoot 'saved.json'
    $Other=Read-LaneProfile $FixtureProfile
    $Other.name='Saved project'; $Other.repo='example/saved'
    Save-LaneProfile $SavedPath $Other ''
    Assert ((Read-LaneProfile $SavedPath).name -eq 'Saved project') 'Profile round trip failed.'
    $ConflictDetected=$false
    try { Save-LaneProfile $SavedPath $Other 'stale-hash' } catch { $ConflictDetected=$true }
    Assert $ConflictDetected 'Profile overwrite conflict was missed.'
    $Other.cachePort=70000; $Rejected=$false
    try { Test-LaneProfile $Other } catch { $Rejected=$true }
    Assert $Rejected 'Invalid port was accepted.'
    $Other.cachePort=0
    # Keep the real UI message loop moving while a deliberately slow refresh runs.
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop()
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LaneRefresh -Worker { Start-Sleep -Milliseconds 500; @{Open=@();Closed=@();Prs=@()} }
    Assert ($Watch.ElapsedMilliseconds -lt 400) 'Refresh blocked the UI thread.'
    $FirstJob=$script:RefreshJob
    Start-LaneRefresh -Worker { throw 'Duplicate worker must not run.' }
    Assert ([object]::ReferenceEquals($FirstJob,$script:RefreshJob)) 'Refresh requests overlapped.'
    $Pulses=0
    while ($script:RefreshJob -and $Watch.ElapsedMilliseconds -lt 10000) {
        [Windows.Forms.Application]::DoEvents(); $Pulses++; Complete-LaneRefresh; Start-Sleep -Milliseconds 20
    }
    Assert ($Pulses -gt 2 -and -not $script:RefreshJob -and $RelMsg.Text -match '^Updated') 'Background refresh failed to complete.'
    Start-LaneRefresh -Worker { @{Error='Rate limit fixture';RetrySeconds=120} }
    $Watch.Restart()
    while ($script:RefreshJob -and $Watch.ElapsedMilliseconds -lt 10000) { Complete-LaneRefresh; Start-Sleep -Milliseconds 20 }
    Assert ($script:NextRefresh -gt [datetime]::UtcNow.AddSeconds(90)) 'Rate limit backoff is missing.'
    $script:NextRefresh=[datetime]::MinValue
    Start-LaneRefresh -Worker { Start-Sleep -Milliseconds 500; @{Token='old-project-token';Open=@();Closed=@();Prs=@()} }
    Use-LaneProject $SavedPath
    Assert ($Repo -eq 'example/saved' -and -not $script:Token) 'Project switch leaked credentials.'
    Start-Sleep -Milliseconds 600; Complete-LaneRefresh
    Assert (-not $script:Token -and $script:Milestones.Count -eq 0) 'Stale project refresh was applied.'
    Assert ([IO.File]::ReadAllText((Join-Path $FixtureRoot 'current.txt')) -eq $SavedPath) 'Current project was not persisted.'
    Edit-LaneProject $SavedPath
    Assert ($script:ProjectFields['repo'].Text -eq 'example/saved') 'Project editor did not load settings.'
    $Form.Close()
    Write-Output 'PASS: status, profile validation/persistence/conflicts, responsive background refresh, coalescing, backoff and stale-project isolation.'
} finally {
    if ($Timer) { $Timer.Stop(); $Timer.Dispose() }
    if ($RefreshTimer) { $RefreshTimer.Stop(); $RefreshTimer.Dispose() }
    if (Get-Command Stop-LaneRefresh -ErrorAction SilentlyContinue) { Stop-LaneRefresh }
    if ($Form) { $Form.Dispose() }
    # Only the unique directory created by this test is eligible for cleanup.
    $ResolvedFixture=[IO.Path]::GetFullPath($FixtureRoot)
    if ($ResolvedFixture.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase) -and (Split-Path $ResolvedFixture -Leaf) -like 'buildlane-test-*') { Remove-Item -LiteralPath $ResolvedFixture -Recurse -Force }
}
