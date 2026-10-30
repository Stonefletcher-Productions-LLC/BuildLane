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
    $Queue.Items[0].Selected=$true
    function Fresh-MergeEligibility {
        $script:IntegrationSnapshot.AtUtc=[datetime]::UtcNow
        $script:IntegrationInspect=@{AtUtc=[datetime]::UtcNow;Observation=@{integrationMode='MergeToStagingThenValidate';controllerRef='main';capabilities=@{stage=$true;queueMerge=$true;cancelMerge=$true;pauseIntegration=$true;resumeIntegration=$true}}}
        Update-LanePullActions
    }
    function Drain-Merge {
        $Until=[datetime]::UtcNow.AddSeconds(5)
        while ($script:IntegrationJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneIntegration; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
        Assert (-not $script:IntegrationJob) 'Merge worker did not finish.'
    }
    function Apply-MergeState {
        $Json=$script:IntegrationSnapshot | ConvertTo-Json -Depth 15 -Compress
        $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
        $script:NextIntegrationRead=[datetime]::MinValue
        Start-LaneIntegrationRead -Worker $Worker; Drain-Merge
    }
    # Exact-head approval, including controller review, is distinct from successful checks.
    Fresh-MergeEligibility
    Assert (-not $PullMerge.Enabled -and $PullAuto.Enabled -and $MergePause.Text -eq 'Resume merges') 'Missing remote pause state failed open.'
    $script:IntegrationSnapshot.State.integrationPaused=$false; Update-LanePullActions
    Assert ($PullMerge.Enabled -and $PullAuto.Enabled) 'Eligible approved PR did not enable merge controls.'
    $Prs[0].commits.nodes[0].commit.statusCheckRollup.state='PENDING'; Update-LanePullActions
    Assert (-not $PullMerge.Enabled -and $PullAuto.Enabled) 'Pending checks could merge now or could not be queued.'
    $Prs[0].reviewDecision='REVIEW_REQUIRED'; Update-LanePullActions
    Assert (-not $PullAuto.Enabled) 'Missing review enabled automatic merge.'
    $script:IntegrationSnapshot.State.reviews=@(@{source='controller';decision='approved';prNumber=1;head=('b'*40);reviewer='fixture-reviewer';reason='Exact source reviewed'})
    Update-LanePullActions
    Assert ($PullAuto.Enabled -and (Get-LanePullState $Prs[0]) -match 'controller review approved') 'Exact controller review was ignored.'
    $Prs[0].reviewDecision='CHANGES_REQUESTED'; Update-LanePullActions
    Assert (-not $PullAuto.Enabled) 'Requested changes did not override controller review.'
    $Prs[0].reviewDecision='APPROVED'
    $script:IntegrationInspect.AtUtc=[datetime]::UtcNow.AddMinutes(-2); Update-LanePullActions
    Assert (-not $PullAuto.Enabled -and -not $PullMerge.Enabled) 'Stale eligibility enabled merges.'
    Fresh-MergeEligibility
    $script:MergePost=$null
    function Invoke-RestMethod {
        param($Method,$Uri,$Headers,$TimeoutSec,$ContentType,$Body)
        if ($Method -ne 'POST') { throw 'Unexpected transport operation.' }
        $script:MergePost=[Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
    }
    $Transport=& $script:IntegrationWorker @{Kind='request';Operation='queue-merge';RequestId='fixture-id';Revision=('a'*40);PrNumber=1;Head=('b'*40);Repo=$Repo;Token='fixture';Workflow='staging-controller.yml'}
    Assert ($Transport.Accepted -and $script:MergePost.ref -eq 'main' -and $script:MergePost.inputs.pr -eq '1' -and $script:MergePost.inputs.head -eq ('b'*40) -and $script:MergePost.inputs.expected_revision -eq ('a'*40)) 'Merge transport lost exact head, PR or revision.'
    $Transport=& $script:IntegrationWorker @{Kind='request';Operation='cancel-merge';RequestId='cancel-id';Revision=('a'*40);QueueId='queue-id';Repo=$Repo;Token='fixture';Workflow='staging-controller.yml'}
    Assert (($script:MergePost.inputs.details | ConvertFrom-Json).queue_id -eq 'queue-id') 'Cancellation lost queue identity.'
    Remove-Item Function:\Invoke-RestMethod
    Start-LanePullAction queue-merge -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Merge
    $Pending=$script:IntegrationPending
    Assert ($Pending.PrNumber -eq 1 -and $Pending.Head -eq ('b'*40) -and -not $PullAuto.Enabled) 'Acceptance lost frozen PR or enabled duplicate submission.'
    $script:IntegrationSnapshot.Repo=$Repo
    $script:IntegrationSnapshot.State.controllerRequests=@(@{requestId=$Pending.RequestId;operation='queue-merge';status='completed';binding=(@{operation='queue-merge';number=1;head=('c'*40)} | ConvertTo-Json -Compress)})
    $script:IntegrationSnapshot.State.mergeQueue=@(@{queueId=$Pending.RequestId;prNumber=1;head=('b'*40);status='waiting-checks';reason='Checks pending'})
    Apply-MergeState
    Assert ($script:IntegrationPending -and $script:IntegrationError -match 'binding') 'Different head acknowledged as queued.'
    $script:IntegrationSnapshot.State.controllerRequests[0].binding=(@{operation='queue-merge';number=1;head=('b'*40)} | ConvertTo-Json -Compress)
    Apply-MergeState
    Assert (-not $script:IntegrationPending -and $script:PullActionMessage -match 'recorded' -and (Get-LanePullState $Prs[0]) -match 'waiting-checks') 'Exact durable queue record was not acknowledged.'
    Fresh-MergeEligibility
    Assert ($PullCancel.Enabled -and -not $PullAuto.Enabled) 'Queued merge cancellation unavailable or duplicate queue allowed.'
    $script:IntegrationSnapshot.State.mergeQueue[0].status='needs-review'
    $script:IntegrationSnapshot.State.mergeQueue[0].head='c'*40; Update-LanePullActions
    Assert ($PullCancel.Enabled) 'Changed-head needs-review queue could not be cancelled.'
    $QueueId=$script:IntegrationSnapshot.State.mergeQueue[0].queueId
    Start-LanePullAction cancel-merge -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Merge
    $Pending=$script:IntegrationPending
    $script:IntegrationSnapshot.State.controllerRequests=@(@{requestId=$Pending.RequestId;operation='cancel-merge';status='completed';binding=(@{operation='cancel-merge';queueId=$QueueId} | ConvertTo-Json -Compress)})
    $script:IntegrationSnapshot.State.mergeQueue[0].status='cancelled'
    Apply-MergeState
    Assert (-not $script:IntegrationPending -and $script:PullActionMessage -match 'Builds were not cancelled') 'Exact queued cancellation was not distinguished from build cancellation.'
    $Prs[0].commits.nodes[0].commit.statusCheckRollup.state='SUCCESS'; Fresh-MergeEligibility
    Start-LanePullAction stage -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Merge
    $Pending=$script:IntegrationPending
    $script:IntegrationSnapshot.State.controllerRequests=@(@{requestId=$Pending.RequestId;operation='stage';status='completed';binding=(@{operation='stage';number=1;head=('b'*40)} | ConvertTo-Json -Compress)})
    $script:IntegrationSnapshot.State | Add-Member -NotePropertyName merges -NotePropertyValue @(@{prNumber=1;head=('b'*40);result=('f'*40)}) -Force
    Apply-MergeState
    Assert (-not $script:IntegrationPending -and $script:PullActionMessage -match 'confirmed PR #1 merged') 'Stage completion lacked matching merge evidence.'
    Fresh-MergeEligibility
    Start-LanePullAction queue-merge -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Merge
    $Pending=$script:IntegrationPending
    $script:IntegrationSnapshot.State.controllerRequests=@(@{requestId=$Pending.RequestId;operation='queue-merge';status='attention';message='Current head needs review';binding=(@{operation='queue-merge';number=1;head=('b'*40)} | ConvertTo-Json -Compress)})
    Apply-MergeState
    Assert (-not $script:IntegrationPending -and $QueueHint.Text -match 'Current head needs review') 'Controller rejection was hidden from Overview.'
    Fresh-MergeEligibility
    Start-LaneIntegrationRequest pause-integration -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Merge
    $Pending=$script:IntegrationPending
    Assert ($script:IntegrationSnapshot.State.integrationPaused -eq $false) 'HTTP acceptance optimistically paused server merges.'
    $script:IntegrationSnapshot.State.controllerRequests=@(@{requestId=$Pending.RequestId;operation='pause-integration';status='completed';binding='{"operation":"pause-integration"}'})
    $script:IntegrationSnapshot.State.integrationPaused=$true
    Apply-MergeState
    Assert (-not $script:IntegrationPending -and $QueueHead.Text -match 'merges paused') 'Durable pause was not shown.'
    Fresh-MergeEligibility
    Assert (-not $PullMerge.Enabled -and $PullAuto.Enabled -and $MergePause.Enabled) 'Paused merge server blocked queueing or allowed merge now.'
    Start-LaneIntegrationRequest resume-integration -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Merge
    $Pending=$script:IntegrationPending
    $script:IntegrationSnapshot.State.controllerRequests=@(@{requestId=$Pending.RequestId;operation='resume-integration';status='completed';binding='{"operation":"resume-integration"}'})
    $script:IntegrationSnapshot.State.integrationPaused=$false
    Apply-MergeState
    Assert (-not $script:IntegrationPending -and $QueueHead.Text -match 'merges running') 'Durable resume was not shown.'
    $script:PullActionMessage=''; Fresh-MergeEligibility; Update-View
    if ($OutDirectory) { [void][IO.Directory]::CreateDirectory($OutDirectory); Resize-Content; Set-PaneHeights; [Windows.Forms.Application]::DoEvents(); $Bitmap=New-Object Drawing.Bitmap($Form.Width,$Form.Height); try {$Form.DrawToBitmap($Bitmap,(New-Object Drawing.Rectangle(0,0,$Form.Width,$Form.Height)));$Bitmap.Save((Join-Path $OutDirectory 'open-prs.png'))} finally {$Bitmap.Dispose()} }
    $script:IntegrationSnapshot.State.requests[0].worker.attempt=3; Update-View
    Assert ($Title.Text -notmatch '^Release 0.3.0') 'Mismatched attempt acquired release identity.'
    'PASS: completed build retains live PRs, cached errors, stable rows, exact release identity.'
} finally { Stop-LaneRefresh; if ($Form) {$Form.Dispose()}; if ($Timer) {$Timer.Dispose()}; if ($RefreshTimer) {$RefreshTimer.Dispose()}; Remove-Item -LiteralPath $FixtureRoot -Recurse -Force }
