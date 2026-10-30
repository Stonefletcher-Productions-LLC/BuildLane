param([string]$OutDirectory)
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-pulls-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'; $StatusPath=Join-Path $FixtureRoot 'status.json'
    @{name='PR overview fixture';repo='example/pulls';branch='main';workflow='lane.yml';integrationWorkflow='staging-controller.yml';activeJobs='ci.yml|Build and test';statusFile=$StatusPath;pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
    @{runId='123';runAttempt=2;what='Staging checkpoint 11111111-2222-3333-4444-555555555555';done='success';phase=5;phases=@('Wait','Checks','Compile','Imports','Tests','Package');started=[datetime]::UtcNow.AddMinutes(-5).ToString('o');updated=[datetime]::UtcNow.ToString('o');queue=@()} | ConvertTo-Json | Set-Content $StatusPath
    $Form.Show(); [Windows.Forms.Application]::DoEvents(); $Timer.Stop(); $RefreshTimer.Stop(); $Tabs.SelectedTab=$NowTab
    Initialize-LaneIntegration
    $script:IntegrationSnapshot=@{Initialized=$true;AtUtc=[datetime]::UtcNow;Revision=('a'*40);State=@{integrationMode='MergeToStagingThenValidate';requests=@(@{version='0.3.0';worker=@{runId=123;attempt=2}})}}
    Initialize-LanePulls
    $Prs=@()
    foreach ($N in 1..3) { $Prs+=@(@{number=$N;title=@('Creator improvements','Street lighting','Vehicle polish')[$N-1];headRefOid=('b'*40);baseRefName='Test-Staging';isDraft=$false;reviewDecision=$(if ($N -eq 2) {'REVIEW_REQUIRED'} else {'APPROVED'});mergeable='MERGEABLE';mergeStateStatus='CLEAN';commits=@{nodes=@(@{commit=@{statusCheckRollup=@{state=$(if ($N -eq 3) {'FAILURE'} else {'SUCCESS'})}}})}}) }
    Set-LanePullResult @{Repo=$Repo;Branch='Test-Staging';AtUtc=[datetime]::UtcNow;Pulls=$Prs;Merged=@(@{number=4;title='Completed neck fix';baseRefName='Test-Staging';mergedAt='2026-10-30T00:00:00Z'})}
    Assert ($Queue.Visible -and $Queue.Items.Count -eq 4 -and $QueueHead.Text -match 'Open pull requests') 'Completed report hid current PRs.'
    Assert ($Title.Text -match '^Release 0.3.0') 'Exact run/attempt binding did not name release.'
    $Before=$Queue.Items[0]; 1..10 | ForEach-Object { Update-View }
    Assert ([object]::ReferenceEquals($Before,$Queue.Items[0])) 'Unchanged refresh rebuilt PR rows.'
    Set-LanePullResult @{Error='Fixture offline';RetrySeconds=60}
    Assert ($Queue.Items.Count -eq 4 -and $QueueHint.Text -match 'Last known') 'API failure erased cached evidence.'
    $script:PullError=''; Update-View
    if ($OutDirectory) { [void][IO.Directory]::CreateDirectory($OutDirectory); Resize-Content; Set-PaneHeights; [Windows.Forms.Application]::DoEvents(); $Bitmap=New-Object Drawing.Bitmap($Form.Width,$Form.Height); try {$Form.DrawToBitmap($Bitmap,(New-Object Drawing.Rectangle(0,0,$Form.Width,$Form.Height)));$Bitmap.Save((Join-Path $OutDirectory 'open-prs.png'))} finally {$Bitmap.Dispose()} }
    $script:IntegrationSnapshot.State.requests[0].worker.attempt=3; Update-View
    Assert ($Title.Text -notmatch '^Release 0.3.0') 'Mismatched attempt acquired release identity.'
    'PASS: completed build retains live PRs, cached errors, stable rows, exact release identity.'
} finally { Stop-LaneRefresh; if ($Form) {$Form.Dispose()}; if ($Timer) {$Timer.Dispose()}; if ($RefreshTimer) {$RefreshTimer.Dispose()}; Remove-Item -LiteralPath $FixtureRoot -Recurse -Force }
