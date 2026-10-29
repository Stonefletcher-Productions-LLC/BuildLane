# Native job handoff fixtures only. No real API calls, workflows or engine launches.
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-activity-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'; $StatusPath=Join-Path $FixtureRoot 'status.json'
    $Targets='ci.yml|Build and test;ci.yml|focused;compile-preview.yml|Compile only (not release validation)'
    @{name='Activity fixture';repo='example/activity';branch='main';workflow='lane.yml';activeJobs=$Targets;statusFile=$StatusPath;pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    $Old=@{runId='10';what='Previous compile preview';done='failure';phase=2;phases=@('Prepare','Compile','Tests');step='Report';started=[datetime]::UtcNow.AddMinutes(-10).ToString('o');updated=[datetime]::UtcNow.AddMinutes(-5).ToString('o');queue=@();runUrl='https://github.com/example/activity/actions/runs/10'}
    $Old | ConvertTo-Json -Depth 5 | Set-Content $StatusPath
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); Update-View
    Assert ($Title.Text -match '^Last reported:' -and $QueueHead.Text -match 'unknown') 'Completed report was shown as current queue evidence.'
    $script:Mode='normal'; $script:WrongJob=$false; $script:Calls=New-Object Collections.ArrayList
    $script:Native=@{id=30;run_attempt=2;path='.github/workflows/ci.yml';status='in_progress';head_sha=('a'*40);display_title='Integration batch';head_branch='lane/batch'}
    $script:Quick=@{id=20;run_attempt=1;path='.github/workflows/ci.yml';status='in_progress';head_sha=('b'*40);display_title='PR quick checks';head_branch='codex/other'}
    $script:Queued=@{id=40;run_attempt=1;path='.github/workflows/compile-preview.yml';status='queued';head_sha=('c'*40);display_title='Compile preview';head_branch='main'}
    function Invoke-RestMethod {
        param($Uri,$Headers,$TimeoutSec)
        [void]$script:Calls.Add($Uri)
        if ($Uri -match '/actions/runs\?status=([^&]+)') {
            $State=$Matches[1]; $Runs=@()
            if ($script:Mode -ne 'none') {
                if ($State -eq 'in_progress') { $Runs=@($script:Quick); if ($script:Mode -ne 'quick') { $Runs+=@($script:Native) } }
                if ($State -eq 'queued' -and $script:Mode -eq 'normal') { $Runs=@($script:Queued) }
            }
            return @{workflow_runs=$Runs}
        }
        if ($Uri -match '/runs/(\d+)/attempts/(\d+)/jobs\?') {
            $Id=$Matches[1]
            if ($Id -eq '20') { return @{jobs=@(@{id=200;run_id=20;name='Quick checks';status='in_progress'},@{id=201;run_id=20;name='Build and test';status='completed';conclusion='skipped'})} }
            if ($Id -eq '40') { return @{jobs=@(@{id=400;run_id=40;name='Compile only (not release validation)';status='queued';steps=@()})} }
            return @{jobs=@(@{id=300;run_id=$(if ($script:WrongJob) { 999 } else { 30 });name='Build and test';status='in_progress';started_at=[datetime]::UtcNow.AddSeconds(-20).ToString('o');steps=@(@{name='Run actions/checkout@v4';status='in_progress'})})}
        }
        if ($Uri -match '/runs/(\d+)$') {
            $Run=if ($Matches[1] -eq '40') { $script:Queued.Clone() } else { $script:Native.Clone() }
            if ($script:Mode -eq 'rerun') { $Run.run_attempt=3 }
            return $Run
        }
        throw "Unexpected endpoint $Uri"
    }
    $Request=@{Repo=$Repo;Token='fixture';Targets=@(Get-LaneActivityTargets $Targets)}
    $Result=& $script:ActivityWorker $Request
    Assert (-not $Result.Error -and $Result.Active.Run.id -eq 30 -and $Result.Count -eq 2) 'Running native job did not outrank queued preview or quick-only job.'
    $script:ActivitySnapshot=$Result; Update-View
    Assert ($Title.Text -match '^Preparing: Integration batch' -and $PreviousResult.Text -match 'Previous compile preview.*failure') 'New checkout did not replace stale failed report while preserving history.'
    Assert ($QueueHead.Text -match 'unknown' -and -not $Queue.Visible -and -not $StepsBox.Visible) 'Stale zero queue or fabricated phase progress was displayed.'
    Assert ((Get-LaneDisplayedRunUrl) -match '/runs/30/attempts/2$') 'Live log opens the previous run or wrong attempt.'
    Assert ((Get-LaneCurrentRunForControl).runId -eq 30) 'A stop confirmation would target the previous run.'
    $script:ActivityError='Fixture API unavailable'; Update-View
    Assert ($Title.Text -match '^Last observed' -and $ActivityNote.Text -match 'unavailable') 'API failure was shown as confirmed running/idle.'
    Assert ($null -eq (Get-LaneCurrentRunForControl)) 'Unconfirmed API evidence authorized stopping a stale run.'
    $script:ActivityError=''; $script:ActivitySnapshot.AtUtc=[datetime]::UtcNow.AddMinutes(-2); Update-View
    Assert ($Title.Text -match '^Last observed') 'Stale API evidence remained current.'
    $Current=$Old.Clone(); $Current.runId='31'; $Current.what='New runner report'; $Current.done=''; $Current.phase=1; $Current.updated=[datetime]::UtcNow.ToString('o')
    $Current | ConvertTo-Json -Depth 5 | Set-Content $StatusPath; Update-View
    Assert ($Title.Text -match 'New runner report' -and $Title.Text -notmatch 'Integration batch') 'Older API evidence overwrote a newer local run.'
    $script:Mode='quick'; $Result=& $script:ActivityWorker $Request
    Assert (-not $Result.Error -and -not $Result.Active) 'Quick-check-only workflow was treated as a native build.'
    $script:Mode='rerun'; $Result=& $script:ActivityWorker $Request
    Assert $Result.Error 'A moving run attempt was accepted.'
    $script:Mode='normal'; $script:WrongJob=$true; $Result=& $script:ActivityWorker $Request
    Assert $Result.Error 'Wrong run/job identity was accepted.'
    $script:WrongJob=$false
    Remove-Item Function:\Invoke-RestMethod
    # Automatic observer path works without any local command/button request.
    $Old | ConvertTo-Json -Depth 5 | Set-Content $StatusPath
    $script:NextActivityRead=[datetime]::MinValue
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LaneActivity -Worker { param($Request) Start-Sleep -Milliseconds 600; @{AtUtc=[datetime]::UtcNow;Repo=$Request.Repo;Active=$null;Count=0} }
    Assert ($Watch.ElapsedMilliseconds -lt 500 -and -not $script:CommandState) 'Observer blocked UI or needed a command.'
    $First=$script:ActivityJob; Start-LaneActivity -Worker { throw 'Duplicate read' }
    Assert ([object]::ReferenceEquals($First,$script:ActivityJob)) 'Activity reads overlapped.'
    $Until=[datetime]::UtcNow.AddSeconds(5)
    while ($script:ActivityJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneActivity; Start-Sleep -Milliseconds 20 }
    Assert (-not $script:ActivityJob -and $Title.Text -match '^Last reported:' -and $QueueHead.Text -match 'unknown') 'Empty observation invented an empty queue or retained old active job.'
    $script:NextActivityRead=[datetime]::MinValue
    Start-LaneActivity -Worker { param($Request) Start-Sleep -Milliseconds 200; @{AtUtc=[datetime]::UtcNow;Repo=$Request.Repo;Active=$null;Count=0} }
    $script:ProjectGeneration++; $Before=$script:ActivitySnapshot
    Start-Sleep -Milliseconds 300; Complete-LaneActivity
    Assert ([object]::ReferenceEquals($Before,$script:ActivitySnapshot)) 'Old-generation observation applied to a new project.'
    $script:NextActivityRead=[datetime]::MinValue
    Start-LaneActivity -Worker { Start-Sleep -Milliseconds 200; @{Error='retired'} }
    Stop-LaneRefresh; Start-Sleep -Milliseconds 300; Complete-LaneRefresh
    Assert (-not $script:ActivityJob -and -not $script:RetiredJobs.Count -and -not $script:UiErrors.Count) 'Retired observer leaked or a UI event threw.'
    'PASS: automatic actual-job handoff, stale failed JSON, checkout, quick-only exclusion, unknown queue, run/attempt binding, API failure/staleness and worker isolation.'
} finally {
    foreach ($Job in @($script:ActivityJob,$script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
