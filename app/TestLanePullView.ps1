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
    Assert ($Queue.Visible -and $Queue.Items.Count -eq 3 -and $QueueHead.Text -match '^Pull requests:') 'Completed report hid current PRs.'
    # Merged PRs are hidden until asked for; open work is grouped: needs you, ready, then waiting for review.
    Assert ((@($Queue.Items | ForEach-Object {$_.Tag}) -join ',') -eq '3,1,2') 'Open PRs were not grouped by what they need.'
    Assert ($QueueHead.Text -match '1 need you' -and $QueueHead.Text -match '1 ready' -and $QueueHead.Text -match '1 waiting for review' -and $QueueHead.Text -match 'builds on') "Summary line is wrong: $($QueueHead.Text)"
    Assert ($Queue.Items[0].ForeColor -eq $Orange -and $Queue.Items[0].SubItems[2].Text -match 'Checks failed') 'Failed checks are not called out.'
    $PullShowMerged.Checked=$true
    Assert ($Queue.Items.Count -eq 4 -and $Queue.Items[3].Tag -eq 4 -and $Queue.Items[3].ForeColor -eq $Grey -and $PullShowMerged.Text -match '\(1\)') 'Show recent merges did not list merged PRs last, in grey.'
    $PullShowMerged.Checked=$false
    Assert ($Queue.Items.Count -eq 3) 'Hiding merges kept merged PRs.'
    Assert ($Title.Text -match '^Release 0.3.0') 'Exact run/attempt binding did not name release.'
    $Before=$Queue.Items[0]; 1..10 | ForEach-Object { Update-View }
    Assert ([object]::ReferenceEquals($Before,$Queue.Items[0])) 'Unchanged refresh rebuilt PR rows.'
    Set-LanePullResult @{Error='Fixture offline';RetrySeconds=60}
    Assert ($Queue.Items.Count -eq 3 -and $QueueHint.Text -match 'Last known') 'API failure erased cached evidence.'
    $script:PullError=''; Update-View
    ($Queue.Items | Where-Object Tag -eq 1).Selected=$true
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
    # Drive the actual header event, including numbers that differ from lexical order and a refresh that replaces rows.
    $SortPrs=@(
        @{number=100;title='Beta';headRefOid=('b'*40);baseRefName='Test-Staging';isDraft=$false;reviewDecision='APPROVED';mergeable='CONFLICTING';mergeStateStatus='DIRTY';commits=@{nodes=@(@{commit=@{statusCheckRollup=@{state='SUCCESS'}}})}},
        @{number=2;title='Zeta';headRefOid=('b'*40);baseRefName='Test-Staging';isDraft=$true;reviewDecision='APPROVED';mergeable='MERGEABLE';mergeStateStatus='CLEAN';commits=@{nodes=@(@{commit=@{statusCheckRollup=@{state='SUCCESS'}}})}},
        @{number=10;title='alpha';headRefOid=('b'*40);baseRefName='Test-Staging';isDraft=$false;reviewDecision='APPROVED';mergeable='MERGEABLE';mergeStateStatus='CLEAN';commits=@{nodes=@(@{commit=@{statusCheckRollup=@{state='SUCCESS'}}})}}
    )
    function Refresh-SortRows { Set-LanePullResult @{Repo=$Repo;Branch='Test-Staging';AtUtc=[datetime]::UtcNow;Pulls=$SortPrs;Merged=@()} }
    function Click-QueueHeader([int]$Column) {
        $Method=[Windows.Forms.ListView].GetMethod('OnColumnClick',[Reflection.BindingFlags]'Instance,NonPublic')
        $Event = New-Object Windows.Forms.ColumnClickEventArgs($Column)
        [void]$Method.Invoke($Queue,[object[]]@($Event.PSObject.BaseObject))
    }
    function Assert-QueueOrder([string]$Expected) { Assert ((@($Queue.Items | ForEach-Object {$_.Tag}) -join ',') -eq $Expected) "Wrong queue order, expected $Expected" }
    Refresh-SortRows
    Assert ($Queue.HeaderStyle -eq 'Clickable') 'Queue headers cannot be clicked.'
    ($Queue.Items | Where-Object Tag -eq 10).Selected=$true
    Click-QueueHeader 0; Assert-QueueOrder '2,10,100'
    Click-QueueHeader 0; Assert-QueueOrder '100,10,2'
    Assert ($Queue.Columns[0].Text.EndsWith([string][char]0x2193)) 'Descending arrow is missing.'
    Click-QueueHeader 1; Assert-QueueOrder '10,100,2'
    Click-QueueHeader 1; Assert-QueueOrder '2,100,10'
    Click-QueueHeader 2; Assert-QueueOrder '2,100,10'
    Click-QueueHeader 2; Assert-QueueOrder '10,100,2'
    $SortPrs[0].title='Updated beta'; Refresh-SortRows
    Assert-QueueOrder '10,100,2'
    Assert ($Queue.SelectedItems.Count -eq 1 -and $Queue.SelectedItems[0].Tag -eq 10) 'Refresh lost the selected PR.'
    Assert ($Queue.Columns[2].Text.EndsWith([string][char]0x2193) -and $Queue.Columns[1].Text -eq 'Title') 'Refresh lost sort direction or left a stale arrow.'
    $SortPrs=@(); Refresh-SortRows
    Assert ($Queue.Items.Count -eq 1 -and -not $Queue.Items[0].Tag) 'Sorted empty queue failed.'
    $SortPrs=@($Prs[0]); Refresh-SortRows; Click-QueueHeader 0
    Assert ($Queue.Items.Count -eq 1 -and $Queue.Items[0].Tag -eq 1) 'Single-row sorting failed.'
    Set-LanePullResult @{Repo=$Repo;Branch='Test-Staging';AtUtc=[datetime]::UtcNow;Pulls=$Prs;Merged=@()}
    Click-QueueHeader 1
    if ($OutDirectory) { [void][IO.Directory]::CreateDirectory($OutDirectory); Resize-Content; Set-PaneHeights; [Windows.Forms.Application]::DoEvents(); $Bitmap=New-Object Drawing.Bitmap($Form.Width,$Form.Height); try {$Form.DrawToBitmap($Bitmap,(New-Object Drawing.Rectangle(0,0,$Form.Width,$Form.Height)));$Bitmap.Save((Join-Path $OutDirectory 'open-prs.png'))} finally {$Bitmap.Dispose()} }
    $script:IntegrationSnapshot.State.requests[0].worker.attempt=3; Update-View
    Assert ($Title.Text -notmatch '^Release 0.3.0') 'Mismatched attempt acquired release identity.'
    'PASS: completed build retains live PRs, cached errors, stable rows, exact release identity.'
} finally { Stop-LaneRefresh; if ($Form) {$Form.Dispose()}; if ($Timer) {$Timer.Dispose()}; if ($RefreshTimer) {$RefreshTimer.Dispose()}; Remove-Item -LiteralPath $FixtureRoot -Recurse -Force }
