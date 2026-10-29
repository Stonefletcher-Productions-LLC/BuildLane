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
    Assert ($Form.Icon -and $Form.ShowIcon -and $Form.ShowInTaskbar) 'Window branding is missing.'
    Assert ([LaneTaskbar]::WindowProperty($Form.Handle,5) -eq [LaneTaskbar]::AppId) 'Taskbar identity is still inherited from PowerShell.'
    Assert ([LaneTaskbar]::WindowProperty($Form.Handle,3) -eq ((Join-Path $PSScriptRoot 'BuildLane.ico')+',0')) 'Taskbar relaunch icon is wrong.'
    Assert ([LaneTaskbar]::WindowProperty($Form.Handle,2) -eq $RelaunchCommand) 'Taskbar relaunch command is wrong.'
    $TaskbarLink=Join-Path $FixtureRoot 'Buildlane test.lnk'
    $Shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut($TaskbarLink)
    $Shortcut.TargetPath=Join-Path $env:WINDIR 'notepad.exe'
    $Shortcut.Save()
    [LaneTaskbar]::ConfigureShortcut($TaskbarLink)
    Assert ([LaneTaskbar]::ShortcutIdentity($TaskbarLink) -eq [LaneTaskbar]::AppId) 'Shortcut identity differs from the running window.'

    Assert ($Repo -eq 'example/another-product' -and $Branch -eq 'develop') 'Project settings did not load.'
    Assert ($TokenFile -notmatch 'LifeShare') 'Project credentials were not isolated.'
    Assert ($Title.Text -eq 'No build has reported yet') 'Missing status is not handled.'
    $Fixture = @{ updated = (Get-Date).ToString('o'); started = (Get-Date).AddMinutes(-1).ToString('o'); phase = 0
        phases = @('Build', 'Test'); what = 'sample'; step = 'Build'; progress = @{done=1;total=2}
        queue = @(@{number=7; title='Sample change'; state='waiting'}); notes=@(); done='' }
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($JobBar.Value -eq 250 -and $StepBar.Value -eq 500) "Progress is incorrect: job=$($JobBar.Value), step=$($StepBar.Value), status=$($Sub.Text), file=$JsonFile"
    $Fixture.queue=@(
        @{number=7;title='Next release';state='held: v0.2.2 is being packaged first'},
        @{number=8;title='Assigned later';state='held: v0.2.2 is being packaged first; in v0.3.0'},
        @{number=9;title='Actual blocker';state='held: main is red'},
        @{number=10;title='Conflict';state='waits for the next batch: it conflicts with #11'},
        @{number=11;title='Working';state='in the batch, building now'},
        @{number=12;title='Release freeze';state='held: after the v0.2.2 freeze'}
    )
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($Queue.Items[0].SubItems[2].Text -eq 'Waiting for v0.2.2 to finish' -and $Queue.Items[0].ForeColor -eq $Grey) 'Release wait still looks like an error or claims packaging has started.'
    Assert ($Queue.Items[1].SubItems[2].Text -eq 'Waiting for v0.2.2 to finish; in v0.3.0') 'Release assignment was lost from wait wording.'
    Assert ($Queue.Items[2].ForeColor -eq $Orange -and $Queue.Items[3].ForeColor -eq $Orange) 'Actual blockers lost their warning color.'
    Assert ($Queue.Items[4].ForeColor -eq $Blue) 'Active work lost its progress color.'
    Assert ($Queue.Items[5].ForeColor -eq $Grey -and $Queue.Items[5].SubItems[2].Text -match 'release freeze to end') 'Planned release freeze looks like an error.'
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
    # The cancelled release must not be confused with a later successful branch run.
    $Fixture.what='release v0.2.4 (package build)'; $Fixture.runId='40716658133'
    $Fixture.phases=@('Wait for the PC','Quick checks','Compile','Content imports','Tests','Package')
    $Fixture.phase=4; $Fixture.done='cancelled'
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($Title.Text -match 'v0.2.4.*cancelled' -and $Sub.Text -match '40716658133') 'Cancelled release identity was lost.'
    Assert ($StepsBox.Controls[4].Text -match 'cancelled' -and $StepsBox.Controls[5].ForeColor -eq $Grey) 'Cancelled tests or unreported package look successful.'
    Assert ($JobBar.Value -eq 0) 'Cancelled run displays full completion.'
    $Fixture.what='branch codex/creator-meshy-validation'; $Fixture.runId='40719403752'; $Fixture.done='success'
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    Update-View
    Assert ($Title.Text -match 'creator-meshy-validation: passed' -and $Title.Text -notmatch '0.2.4') 'Later success was attributed to the release.'
    Assert ($Sub.Text -match '40719403752' -and $Sub.Text -notmatch '40716658133') 'Run identity became stale.'
    Assert ($StepsBox.Controls[5].ForeColor -eq $Grey -and $StepsBox.Controls[5].Text -match 'not reported') 'Successful tests falsely certify packaging.'
    Assert ($StepsBox.Controls[3].ForeColor -eq $Grey) 'Skipped content import inferred as successful.'
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
    # The pulse count only proves the loop ran at all: a fast job legitimately finishes in two iterations, so asserting
    # more than that made this check fail whenever the machine was quick, not when the refresh was wrong.
    Assert ($Pulses -ge 1 -and -not $script:RefreshJob -and $RelMsg.Text -match '^Updated') 'Background refresh failed to complete.'
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

    # A queued release must show when it ships, and a broken or absent deadline must never fabricate a time.
    Assert ($null -eq (Get-CutDeadline @{ description = 'lane: queued' })) 'A release without a deadline reported one.'
    $Future = (Get-Date).ToUniversalTime().AddHours(2).ToString('yyyy-MM-ddTHH:mm:ssZ')
    $Cut = Get-CutDeadline @{ description = "lane: queued`nlane-deadline: $Future" }
    Assert ($Cut -and [math]::Abs(($Cut.ToUniversalTime() - [datetime]::Parse($Future).ToUniversalTime()).TotalSeconds) -lt 2) "Cut deadline was not read: got '$Cut' from '$Future'."
    Assert ($null -eq (Get-CutDeadline @{ description = 'lane: queued' + "`n" + 'lane-deadline: tomorrow-ish' })) 'A malformed deadline was accepted.'
    $Queued = @{ description = "lane: queued`nlane-deadline: $Future" }
    Assert ((Format-RelState $Queued 'queued') -match 'ships by \d\d:\d\d$') "Queued wording does not name the cut time: $(Format-RelState $Queued 'queued')"
    $Past = (Get-Date).ToUniversalTime().AddMinutes(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
    Assert ((Format-RelState @{ description = "lane: queued`nlane-deadline: $Past" } 'queued') -match 'cut reached') 'An expired cut still promises a future time.'
    Assert ((Format-RelState @{ description = 'lane: queued' } 'queued') -eq '▶ queued: landing its PRs') 'A release without a deadline lost its plain wording.'
    Assert ((Format-RelState @{ description = 'lane: open' } 'closed') -eq '✓ closed') 'Closed wording changed.'

    # A rogue producer (a PR's quick checks share the runner and the status file) can write a one-phase status.
    # Presenting that as "Step 1 of 1" for a running batch is worse than showing the standard phases.
    @{ updated = (Get-Date).ToString('o'); started = (Get-Date).AddMinutes(-3).ToString('o'); phase = 0
       phases = @('Quick checks'); what = 'quick checks'; step = 'UI text and save fields'; done = '' } |
        ConvertTo-Json -Depth 5 | Set-Content $FixtureStatus
    $script:StepsKey = $null
    Update-View
    Assert ($JobLabel.Text -match 'Step \d+ of 6:') "One-phase status was rendered as a job: $($JobLabel.Text)"

    # Panes: the queue fills a resizable pane, and its height is remembered.
    Assert ($Queue.Dock -eq 'Fill' -and $Queue.Parent -eq $QueuePanel -and $QueuePanel.Parent -eq $NowSplit.Panel2) 'The queue is not in its own resizable pane.'
    Assert (-not $NowSplit.IsSplitterFixed) 'The queue divider cannot be dragged.'
    $script:LaneLayout['queue'] = 176
    Set-PaneHeights
    Assert ([math]::Abs(($NowSplit.Height - $NowSplit.SplitterWidth - $NowSplit.SplitterDistance) - 176) -le 2) "Queue pane height was not applied: $($NowSplit.SplitterDistance)."
    Save-PaneHeights
    Assert ((Get-Content $LayoutFile -Raw) -match 'queue=17[4-8]') 'Queue pane height was not remembered.'

    # Nothing changed, nothing rebuilt: the 3-second refresh must not recreate the same labels.
    $script:StepsKey = $null
    Update-View
    $FirstStep = $StepsBox.Controls[0]
    Update-View
    Assert ([object]::ReferenceEquals($FirstStep, $StepsBox.Controls[0])) 'Step labels are rebuilt even when the phase did not change.'

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
