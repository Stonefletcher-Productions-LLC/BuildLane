# Run in hosted Windows CI. Fixtures never dispatch live requests or launch engine jobs.
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-release-requests-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'
    @{name='Release request fixture';repo='example/releases';branch='main';workflow='lane.yml';integrationWorkflow='staging-controller.yml';statusFile="$FixtureRoot/status.json";pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
    function Drain-Release {
        $Until=[datetime]::UtcNow.AddSeconds(5)
        while ($script:IntegrationJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneIntegration; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
        Assert (-not $script:IntegrationJob) 'Release worker did not finish.'
    }
    function Apply-ReleasePayload($Payload) {
        $Json=$Payload | ConvertTo-Json -Depth 12 -Compress
        $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
        $script:NextIntegrationRead=[datetime]::MinValue
        Start-LaneIntegrationRead -Worker $Worker; Drain-Release
    }
    $Form.Show(); [Windows.Forms.Application]::DoEvents(); $Timer.Stop(); $RefreshTimer.Stop(); $Tabs.SelectedTab=$RelTab
    Update-View
    Assert ($RelSplit.Visible -and -not $StagingReleaseRoot.Visible) 'Unconfigured mode replaced the legacy release flow.'
    $State=@{schemaVersion=1;integrationMode='MergeToStagingThenValidate';work=@();checkpoints=@();controllerRequests=@();releaseRequests=@()}
    $Payload=@{Initialized=$true;Repo=$Repo;Revision=('a'*40);AtUtc=[datetime]::UtcNow;State=$State}
    Apply-ReleasePayload $Payload
    $ReleaseVersion.Text='0.3.0'
    Assert ($StagingReleaseRoot.Visible -and -not $RelSplit.Visible -and -not $ReleaseQueue.Enabled) 'Staging used the milestone-only queue or guessed release eligibility.'
    $Observation=@{integrationMode='MergeToStagingThenValidate';controllerRef='main';capabilities=@{release=$true};capabilityReasons=@{release=@()};releaseTarget=@{sourceSha=('b'*40)}}
    $script:IntegrationInspect=@{AtUtc=[datetime]::UtcNow;Observation=$Observation}
    Update-LaneReleaseRequestsView
    Assert $ReleaseQueue.Enabled 'Fresh explicit eligibility did not enable the frozen release.'
    $ReleaseVersion.Text='0.3.0; Start-Process nope'
    Assert (-not $ReleaseQueue.Enabled) 'Invalid version enabled a release request.'
    $ReleaseVersion.Text='0.3.0'
    $Observation.capabilityReasons.release=@('another release is packaging'); Update-LaneReleaseRequestsView
    Assert (-not $ReleaseQueue.Enabled -and $ReleaseRequestMessage.Text -match 'packaging') 'Packaging exclusion was not explained.'
    $Observation.capabilityReasons.release=@()
    Set-Content $PauseFile 'Fixture paused'; Update-LaneReleaseRequestsView
    Assert (-not $ReleaseQueue.Enabled -and $ReleaseRequestMessage.Text -match 'paused') 'Paused lane allowed a release.'
    Remove-Item -LiteralPath $PauseFile
    $script:IntegrationSnapshot.AtUtc=[datetime]::UtcNow.AddMinutes(-2); Update-LaneReleaseRequestsView
    Assert (-not $ReleaseQueue.Enabled -and $ReleaseTargetLabel.Text -match 'Last inspected') 'Stale release target stayed actionable.'
    $script:IntegrationSnapshot.AtUtc=[datetime]::UtcNow; Update-LaneReleaseRequestsView
    $script:ReleasePost=$null
    function Invoke-RestMethod {
        param($Method,$Uri,$Headers,$TimeoutSec,$ContentType,$Body)
        if ($Method -ne 'POST') { throw 'Unexpected request in transport fixture' }
        $script:ReleasePost=[Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json
    }
    $Id=[guid]::NewGuid().ToString()
    $Accepted=& $script:IntegrationWorker @{Kind='request';Operation='release';RequestId=$Id;Revision=('a'*40);Target=('b'*40);Version='0.3.0';Repo=$Repo;Token='fixture';Workflow='staging-controller.yml'}
    $Details=$script:ReleasePost.inputs.details | ConvertFrom-Json
    Assert ($Accepted.Accepted -and $script:ReleasePost.ref -eq 'main' -and $script:ReleasePost.inputs.operation -eq 'release' -and $script:ReleasePost.inputs.expected_revision -eq ('a'*40) -and $Details.target -eq ('b'*40) -and $Details.version -eq '0.3.0') 'Release transport lost trusted controller, revision, target or version.'
    Remove-Item Function:\Invoke-RestMethod
    Start-LaneIntegrationRequest release -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Release
    $Pending=$script:IntegrationPending
    Assert ($Pending.Operation -eq 'release' -and $Pending.Target -eq ('b'*40) -and $Pending.Version -eq '0.3.0' -and -not $ReleaseQueue.Enabled) 'Acceptance lost frozen identity or enabled duplicate requests.'
    $Wrong=@{requestId=$Pending.RequestId;operation='release';status='completed';binding=(@{operation='release';target=('b'*40);version='0.3.1'} | ConvertTo-Json -Compress)}
    $State.controllerRequests=@($Wrong)
    $State.releaseRequests=@(@{requestId=$Pending.RequestId;target=('b'*40);version='0.3.0';status='queued';fullTests='pending';package='pending'})
    Apply-ReleasePayload $Payload
    Assert ($script:IntegrationPending -and $script:IntegrationError -match 'binding') 'Wrong release version was acknowledged.'
    $Wrong.binding=(@{operation='release';target=('b'*40);version='0.3.0'} | ConvertTo-Json -Compress)
    $State.releaseRequests[0].target='c'*40; Apply-ReleasePayload $Payload
    Assert ($script:IntegrationPending -and $script:IntegrationError -match 'matching frozen') 'Wrong frozen source was acknowledged.'
    $State.releaseRequests[0].target='b'*40; Apply-ReleasePayload $Payload
    Assert (-not $script:IntegrationPending -and $ReleaseRequests.Items.Count -eq 1 -and $ReleaseRequests.Items[0].SubItems[2].Text -eq 'queued' -and $ReleaseRequests.Items[0].SubItems[3].Text -eq 'pending' -and $ReleaseRequests.Items[0].SubItems[4].Text -eq 'pending') 'Queued release was shown as full tests or package passing.'
    $State.releaseRequests[0].status='completed'; $State.releaseRequests[0].fullTests='success'; $State.releaseRequests[0].package='skipped'
    $State.releaseRequests[0].runId=123; $State.releaseRequests[0].attempt=2
    Apply-ReleasePayload $Payload
    $ReleaseRequests.Items[0].Selected=$true; Update-LaneReleaseRequestSelection
    Assert ($ReleaseRequests.Items[0].SubItems[4].Text -eq 'skipped' -and $ReleaseOpen.Enabled -and $ReleaseRequests.Items[0].SubItems[5].Text -eq '123 / 2') 'Skipped packaging or exact run attempt was hidden.'
    $State.integrationMode='ValidateBeforeMerge'; Apply-ReleasePayload $Payload
    Assert ($RelSplit.Visible -and -not $StagingReleaseRoot.Visible) 'Switching back did not restore legacy releases.'
    Assert (-not $script:UiErrors.Count) 'A release UI handler threw.'
    'PASS: staging release UI, legacy mode, frozen version/source/revision/UUID transport, pause/packaging/stale guards, durable acknowledgement and exact separate Full/package outcomes.'
} finally {
    foreach ($Job in @($script:IntegrationJob,$script:ActivityJob,$script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
