# Fixture-only responsiveness checks. No API, service or lane mutations.
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-feedback-'+[guid]::NewGuid())
New-Item -ItemType Directory $FixtureRoot | Out-Null
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'
    @{name='Feedback fixture';repo='example/fixture';branch='main';workflow='lane.yml';statusFile="$FixtureRoot/status.json";pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Condition,$Message) { if (-not $Condition) { throw $Message } }
    function Drain-Command {
        $Until=[datetime]::UtcNow.AddSeconds(5)
        while ($script:CommandJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneCommand; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
        Assert (-not $script:CommandJob) 'Command worker did not finish.'
    }
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop()
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LaneCommand -Kind resume -Worker { param($Request) Start-Sleep -Milliseconds 900; @{AcceptedAtUtc=[datetime]::UtcNow} }
    $StartMs=$Watch.ElapsedMilliseconds
    Assert ($StartMs -lt 500) "Command blocked the window for ${StartMs}ms."
    Assert ($PauseState.Text -match 'sending' -and -not $PauseButton.Enabled) 'Pending feedback or duplicate-click guard missing.'
    $First=$script:CommandJob
    Start-LaneCommand -Worker { throw 'Duplicate must not run' }
    Assert ([object]::ReferenceEquals($First,$script:CommandJob)) 'Duplicate command started.'
    Start-LaneMove @(1) 7 'Other release' -Worker { throw 'Move must not run' }
    Assert (-not $script:MoveJob) 'A move overlapped a command.'
    $Rejected=$false; try { Use-LaneProject $Profile } catch { $Rejected=$true }
    Assert $Rejected 'Project switched while sending.'
    $Form.Close(); Assert (-not $Form.IsDisposed) 'Window closed with a write in flight.'
    Drain-Command
    Assert ($PauseState.Text -match 'accepted; waiting' -and $PauseButton.Enabled) 'Acceptance was presented as a running build.'
    $script:CommandState.Controller=@{status='queued'}; Update-View
    Assert ($PauseState.Text -match 'controller queued') 'Queued controller not distinguished.'
    $script:CommandState.Controller=@{status='completed';conclusion='success'}; Update-View
    Assert ($PauseState.Text -match 'waiting for runner') 'Controller success was presented as build completion.'
    Start-LaneCommand -Worker { throw 'Fixture timeout after POST' }
    Drain-Command
    Assert ($PauseState.Text -match 'not confirmed') 'Ambiguous failure invited unsafe automatic retry.'
    $script:CommandState.Phase='accepted'
    $script:State=@{updated='malformed';started='also malformed';runId=123;phase=1}
    Update-LaneCommandFeedback
    Assert ($script:CommandState.Phase -eq 'accepted') 'Invalid timestamps implied runner progress.'
    $script:State=$null
    Start-LaneRefresh -Kind controller -Worker { Start-Sleep -Milliseconds 200; @{Controller=@{id=99;status='completed';conclusion='success'}} }
    $OldRead=$script:RefreshJob
    Start-LaneCommand -Worker { @{AcceptedAtUtc=[datetime]::UtcNow} }
    Assert (-not $script:RefreshJob -and $OldRead.Generation -ne $script:ProjectGeneration) 'New command did not retire the old read.'
    Drain-Command
    # Even a same-generation result must belong to the exact command that launched it.
    Start-LaneRefresh -Kind controller -Worker { Start-Sleep -Milliseconds 200; @{Controller=@{id=99;status='completed';conclusion='success'}} }
    $script:CommandState=@{Kind='resume';Phase='accepted';RequestedAtUtc=[datetime]::UtcNow}
    Start-Sleep -Milliseconds 300; Complete-LaneRefresh
    Assert (-not $script:CommandState.Controller -and $script:CommandState.Phase -eq 'accepted') 'Prior command response overwrote newer feedback.'
    $Watch.Restart()
    Start-LaneHealth -Worker { param($Request) Start-Sleep -Milliseconds 900; @{Health=@('fixture healthy');FreeGB=20;TotalGB=100;UsedFraction=0.8} }
    Update-View
    $HealthMs=$Watch.ElapsedMilliseconds
    Assert ($HealthMs -lt 500) "Machine queries blocked rendering for ${HealthMs}ms."
    $Until=[datetime]::UtcNow.AddSeconds(5)
    while ($script:HealthJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneHealth; Start-Sleep -Milliseconds 20 }
    Assert ($Health.Text -match 'fixture healthy') 'Completed health snapshot not rendered.'
    $script:HealthChecked=$null
    Start-LaneHealth -Worker { Start-Sleep -Milliseconds 300; @{Health=@('STALE HEALTH')} }
    Stop-LaneRefresh
    Start-Sleep -Milliseconds 400; Complete-LaneRefresh; Complete-LaneHealth
    Assert (-not $script:CachedMachine -and -not $script:CommandState) 'Stale project state survived a generation change.'
    Assert ($Timer.Interval -eq 1000) 'Local report polling is slower than one second.'
    Assert (-not $script:RetiredJobs.Count -and -not $script:RefreshJob -and -not $script:HealthJob -and -not $script:CommandJob) 'A background worker survived completion.'
    Assert (-not $script:UiErrors.Count) 'A UI handler threw.'
    "PASS: immediate command feedback ${StartMs}ms; render during slow health ${HealthMs}ms; duplicate guards, uncertain writes, controller/runner distinction and stale-result isolation."
} finally {
    # Also dispose workers when an assertion fails before the normal drain completes.
    $Jobs=@($script:CommandJob,$script:HealthJob,$script:RefreshJob)+@($script:RetiredJobs)
    foreach ($Job in $Jobs) {
        if ($Job -and $Job.Shell) {
            try { if (-not $Job.Handle.IsCompleted) { $Job.Shell.Stop() } }
            finally { $Job.Shell.Dispose() }
        }
    }
    $script:CommandJob=$null; $script:HealthJob=$null; $script:RefreshJob=$null
    if ($script:RetiredJobs) { $script:RetiredJobs.Clear() }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
