# All network requests and long operations below are fixtures.
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-actions-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'
    @{name='Action fixture';repo='example/actions';branch='develop';workflow='lane.yml';statusFile="$FixtureRoot/status.json";pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
    $script:Calls=New-Object Collections.ArrayList; $script:Failure=''; $script:Packaging=$false
    function Invoke-RestMethod {
        param($Method,$Uri,$Headers,$TimeoutSec,$ContentType,$Body)
        $Data=if ($Body) { [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json }
        [void]$script:Calls.Add(@{Method=$Method;Uri=$Uri;Data=$Data})
        if ($script:Failure -and $Uri -like $script:Failure -and $Method -ne 'GET') { throw 'Fixture uncertain response' }
        if ($Method -eq 'GET' -and $Uri -like '*/milestones/*') { return @{number=7;title='v1';state='open';description=$(if ($script:Packaging) { 'lane: packaging' } else { "lane: open`n`nlane-deadline: 2026-11-01T10:00:00Z`nKeep this note" })} }
        if ($Uri -like '*/pulls?*') {
            if ($Uri -match 'page=1$') { return @(1..100 | ForEach-Object { @{number=$_;draft=$true} }) }
            return @(@{number=101;title='Ready';draft=$false;labels=@(@{name='ready'})})
        }
        if ($Uri -like '*/milestones' -and $Method -eq 'POST') { return @{number=8} }
    }
    $Request=@{Operation='prepareQueue';Repo='example/actions';Branch='develop';Workflow='lane.yml';Token='fixture';Number=7;Title='New release';Numbers=@()}
    $Progress=[hashtable]::Synchronized(@{Message=''})
    $R=& $script:ActionWorker $Request $Progress
    Assert (-not $R.Error -and $R.Loose.Count -eq 1 -and $R.Loose[0].number -eq 101) 'Queue preparation did not paginate.'
    Assert (-not @($script:Calls | Where-Object Method -ne GET).Count) 'Preparation changed release data before confirmation.'
    $script:Calls.Clear(); $Request.Operation='queue'; $Request.Numbers=@(1,2); $script:Failure='*/issues/2'
    $R=& $script:ActionWorker $Request $Progress
    Assert ($R.Error -and ($R.Moved -join ',') -eq '1' -and -not $R.Saved) 'Partial moves were lost or release queued after failure.'
    Assert (-not @($script:Calls | Where-Object Method -eq POST).Count) 'Failed moves dispatched the lane.'
    $script:Calls.Clear(); $script:Failure='*/dispatches'; $Request.Numbers=@()
    $R=& $script:ActionWorker $Request $Progress
    Assert ($R.Error -and $R.Saved -and -not $R.Notified) 'Dispatch failure lost the confirmed milestone write.'
    Assert (@($script:Calls | Where-Object Method -eq POST).Count -eq 1) 'An uncertain dispatch was automatically resent.'
    $Patch=@($script:Calls | Where-Object Method -eq PATCH)[0]
    Assert ($Patch.Data.description -match 'lane: queued' -and $Patch.Data.description -match 'Keep this note' -and $Patch.Data.description -match 'lane-deadline:') 'Release metadata was overwritten.'
    foreach ($Operation in @('create','queue','unqueue','close','connect')) {
        $script:Failure=''; $script:Calls.Clear(); $Request.Operation=$Operation
        $R=& $script:ActionWorker $Request $Progress
        Assert (-not $R.Error) "$Operation worker failed."
        if ($Operation -in @('queue','unqueue','close')) { Assert $R.Notified "$Operation did not notify the lane." }
    }
    $script:Packaging=$true; $Request.Operation='close'; $script:Calls.Clear()
    $R=& $script:ActionWorker $Request $Progress
    Assert ($R.Error -and -not @($script:Calls | Where-Object Method -ne GET).Count) 'A packaging release was modified.'
    Remove-Item Function:\Invoke-RestMethod
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop()
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LaneAction create -Title 'Fixture' -Worker { param($Request,$Progress) Start-Sleep -Milliseconds 800; @{Saved=$true} }
    $Latency=$Watch.ElapsedMilliseconds
    Assert ($Latency -lt 500 -and -not $RelButtons.Enabled -and -not $PauseButton.Enabled) 'Release action blocked rendering or allowed overlapping writes.'
    $First=$script:ActionJob
    Start-LaneAction close -Worker { throw 'Duplicate' }
    Start-LaneCommand -Worker { throw 'Overlapping command' }
    Start-LaneMove @(1) 7 'Release' -Worker { throw 'Overlapping move' }
    Assert ([object]::ReferenceEquals($First,$script:ActionJob) -and -not $script:CommandJob -and -not $script:MoveJob) 'A competing write started.'
    $Rejected=$false; try { Use-LaneProject $Profile } catch { $Rejected=$true }; Assert $Rejected 'Project changed during a release write.'
    $Form.Close(); Assert (-not $Form.IsDisposed) 'Form closed during a release write.'
    $Until=[datetime]::UtcNow.AddSeconds(5)
    while ($script:ActionJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneAction; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
    Assert (-not $script:ActionJob -and $MoveStatus.Text -match 'saved') 'Confirmed result not rendered.'
    # Connect verifies access in the same background path, then saves only to the fixture credential path.
    $script:TokenFile=Join-Path $FixtureRoot 'fixture.token'
    Start-LaneAction connect -Token 'fixture-token' -Worker { param($Request,$Progress) Start-Sleep -Milliseconds 100; @{Connected=$true} }
    while ($script:ActionJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneAction; Start-Sleep -Milliseconds 20 }
    Assert ($script:Token -eq 'fixture-token' -and (Test-Path $TokenFile)) 'Verified fixture credential was not saved.'
    Assert ((Get-Content $TokenFile -Raw) -notmatch 'fixture-token') 'Credential was stored as plaintext.'
    Assert (-not $script:UiErrors.Count) 'A UI event threw.'
    "PASS: release preparation/pagination, all write paths, preserved metadata, partial failures, packaging guard, responsive start ${Latency}ms, serialization and background connection."
} finally {
    foreach ($Job in @($script:ActionJob,$script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
