param([string]$OutDirectory)
# Controller transport and UI fixtures. No live dispatch, branch mutation or engine process.
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-integration-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'
    @{name='Integration fixture';repo='example/integration';branch='main';workflow='lane.yml';integrationWorkflow='staging-controller.yml';statusFile="$FixtureRoot/status.json";pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
    function Drain-Integration {
        $Until=[datetime]::UtcNow.AddSeconds(5)
        while ($script:IntegrationJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneIntegration; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
        Assert (-not $script:IntegrationJob) 'Controller worker did not finish.'
    }
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); $Tabs.SelectedTab=$IntegrationTab; Update-View
    Assert (-not $IntegrationApply.Enabled -and -not $IntegrationChoice.Enabled) 'Unknown backend enabled a local mode toggle.'
    $script:FixtureState=@{schemaVersion=1;integrationMode='ValidateBeforeMerge';work=@();checkpoints=@();reviews=@();controllerRequests=@()}
    $script:Calls=New-Object Collections.ArrayList; $script:RefCalls=0; $script:Moving=$false; $script:FailPost=$false
    function Invoke-RestMethod {
        param($Method,$Uri,$Headers,$TimeoutSec,$ContentType,$Body)
        $Data=if ($Body) { [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json }
        [void]$script:Calls.Add(@{Method=$Method;Uri=$Uri;Data=$Data})
        if ($Method -eq 'POST') { if ($script:FailPost) { throw 'Fixture uncertain POST' }; return }
        if ($Uri -match '/git/ref/heads/lane/staging-state$') { $script:RefCalls++; return @{object=@{sha=$(if ($script:Moving -and $script:RefCalls -gt 1) { 'b'*40 } else { 'a'*40 })}} }
        if ($Uri -match '/contents/state.json\?ref=a{40}$') { return @{encoding='base64';size=500;content=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($script:FixtureState | ConvertTo-Json -Depth 8)))} }
        throw "Unexpected endpoint: $Uri"
    }
    $Read=@{Kind='read';Repo=$Repo;Token='fixture'}
    $Result=& $script:IntegrationWorker $Read
    Assert ($Result.Initialized -and $Result.Revision -eq ('a'*40) -and $script:RefCalls -eq 2) 'State was not read at an immutable revision with ref recheck.'
    $script:RefCalls=0; $script:Moving=$true; $Bad=& $script:IntegrationWorker $Read
    Assert ($Bad.Error -and -not $Bad.State) 'Moving state ref was accepted.'
    $script:Moving=$false
    $Request=@{Kind='request';Operation='set-mode';Repo=$Repo;Token='fixture';Workflow='staging-controller.yml';RequestId=[guid]::NewGuid().ToString();Revision=('a'*40);Mode='MergeToStagingThenValidate'}
    $script:Calls.Clear(); $Accepted=& $script:IntegrationWorker $Request
    $Posts=@($script:Calls | Where-Object Method -eq POST)
    Assert ($Accepted.Accepted -and $Posts.Count -eq 1 -and $Posts[0].Data.ref -eq 'main') 'Controller dispatch was not pinned to trusted main.'
    Assert ($Posts[0].Data.inputs.expected_revision -eq ('a'*40) -and $Posts[0].Data.inputs.mode -eq 'MergeToStagingThenValidate' -and $Posts[0].Data.inputs.request_id -eq $Request.RequestId) 'Controller binding was lost.'
    $CheckpointRequest=@{}; foreach ($Key in $Request.Keys) { $CheckpointRequest[$Key]=$Request[$Key] }
    $CheckpointRequest.Operation='checkpoint'; $CheckpointRequest.Target='f'*40
    $script:Calls.Clear(); $CheckpointAccepted=& $script:IntegrationWorker $CheckpointRequest
    $CheckpointPost=@($script:Calls | Where-Object Method -eq POST)[0]
    $Details=$CheckpointPost.Data.inputs.details | ConvertFrom-Json
    Assert ($CheckpointAccepted.Accepted -and $Details.target -eq ('f'*40) -and $CheckpointPost.Data.inputs.expected_revision -eq ('a'*40) -and $CheckpointPost.Data.ref -eq 'main') 'Checkpoint target/revision/trusted workflow binding was lost.'
    $script:Calls.Clear(); $script:FailPost=$true; $Bad=& $script:IntegrationWorker $Request
    Assert ($Bad.Error -and @($script:Calls | Where-Object Method -eq POST).Count -eq 1) 'Uncertain mode request was resent automatically.'
    Remove-Item Function:\Invoke-RestMethod
    $script:NextIntegrationRead=[datetime]::MinValue
    Start-LaneIntegrationRead -Worker { param($Request) @{Initialized=$false;Repo=$Request.Repo;AtUtc=[datetime]::UtcNow} }
    Drain-Integration
    Assert ($IntegrationCurrent.Text -match 'not initialized' -and -not $IntegrationApply.Enabled) 'Missing backend invented an active staging mode.'
    $script:NextIntegrationRead=[datetime]::MinValue
    Start-LaneIntegrationRead -Worker { param($Request) @{Initialized=$true;Revision=('a'*40);Repo=$Request.Repo;AtUtc=[datetime]::UtcNow;State=@{schemaVersion=1;integrationMode='ValidateBeforeMerge';work=@();checkpoints=@();controllerRequests=@()}} }
    Drain-Integration
    $IntegrationChoice.SelectedIndex=1
    Assert (-not $IntegrationApply.Enabled -and $IntegrationCurrent.Text -match 'Validate before merge') 'A dropdown choice was treated as authoritative mode or eligibility.'
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LaneIntegrationRequest inspect -Worker { param($Request) Start-Sleep -Milliseconds 400; @{Accepted=$true;RequestId=$Request.RequestId} }
    Assert ($Watch.ElapsedMilliseconds -lt 500) 'Inspect blocked the Forms thread.'
    Drain-Integration
    $InspectId=$script:IntegrationPending.RequestId
    $script:NextIntegrationRead=[datetime]::MinValue
    Start-LaneIntegrationRead -Worker {
        param($Request)
        $Id=$Request.FixtureId
        @{Initialized=$true;Revision=('b'*40);Repo=$Request.Repo;AtUtc=[datetime]::UtcNow;State=@{schemaVersion=1;integrationMode='ValidateBeforeMerge';work=@();checkpoints=@();controllerRequests=@()}}
    }
    Drain-Integration
    # Supply a server result through the same completion path with the exact local UUID.
    $Observation=@{initialized=$true;revision=('a'*40);integrationMode='ValidateBeforeMerge';integrationBranch='main';controllerRef='main';capabilities=@{changeMode=$true;stage=$false};blockingReasons=@();work=@();checkpoints=@()}
    $Record=@{requestId=$InspectId;operation='inspect';status='completed';binding=(@{operation='inspect';actor='fixture';requested=''} | ConvertTo-Json -Compress);observation=$Observation}
    $Payload=@{Initialized=$true;Revision=('c'*40);Repo=$Repo;AtUtc=[datetime]::UtcNow;State=@{schemaVersion=1;integrationMode='ValidateBeforeMerge';work=@();checkpoints=@();controllerRequests=@($Record)}}
    # An injected worker snapshot is evaluated inside the isolated runspace as data, not code from a backend.
    $Json=$Payload | ConvertTo-Json -Depth 10 -Compress
    $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
    $script:NextIntegrationRead=[datetime]::MinValue; Start-LaneIntegrationRead -Worker $Worker; Drain-Integration
    Assert ($IntegrationApply.Enabled -and -not $script:IntegrationPending) 'Fresh UUID-bound inspect did not enable an allowed transition.'
    $script:IntegrationSnapshot.State.work=@('checkpoint active'); Update-LaneIntegrationView
    Assert (-not $IntegrationApply.Enabled -and $IntegrationBlock.Text -match 'checkpoint active') 'New outstanding work did not block a mode change.'
    $script:IntegrationSnapshot.State.work=@(); Update-LaneIntegrationView
    Start-LaneIntegrationRequest set-mode -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }
    Drain-Integration
    $ModeId=$script:IntegrationPending.RequestId
    Assert ($IntegrationCurrent.Text -match 'Validate before merge' -and -not $IntegrationApply.Enabled) 'HTTP acceptance optimistically changed mode.'
    $BadRecord=@{requestId=$ModeId;operation='set-mode';status='completed';binding=(@{operation='set-mode';requested='MergeToStagingThenValidate'} | ConvertTo-Json -Compress)}
    $Payload.State.controllerRequests=@($BadRecord)
    $Payload.Revision='d'*40
    $Json=$Payload | ConvertTo-Json -Depth 10 -Compress; $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
    $script:NextIntegrationRead=[datetime]::MinValue; Start-LaneIntegrationRead -Worker $Worker; Drain-Integration
    Assert ($script:IntegrationPending -and $IntegrationCurrent.Text -match 'Validate before merge') 'Completed record alone changed mode without authoritative state.'
    $Payload.State.integrationMode='MergeToStagingThenValidate'; $Payload.Revision='e'*40
    $Json=$Payload | ConvertTo-Json -Depth 10 -Compress; $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
    $script:NextIntegrationRead=[datetime]::MinValue; Start-LaneIntegrationRead -Worker $Worker; Drain-Integration
    Assert (-not $script:IntegrationPending -and $IntegrationCurrent.Text -match 'Current mode: Merge to staging' -and $IntegrationMessage.Text -match 'Server confirmed') 'Durable result and actual mode did not confirm the change.'
    Assert (-not (Test-LaneLegacyOperationsAllowed) -and -not $RelButtons.Enabled) 'Staging mode left legacy automatic-build release mutations enabled.'
    Assert ($null -eq (Get-LaneIntegrationBranch)) 'Missing current inspection guessed a PR base.'
    $script:IntegrationInspect=@{AtUtc=[datetime]::UtcNow;Observation=@{integrationMode='MergeToStagingThenValidate';integrationBranch='Test-Staging';controllerRef='main';capabilities=@{changeMode=$false;stage=$true};blockingReasons=@()}}
    Assert ((Get-LaneIntegrationBranch) -eq 'Test-Staging' -and $Config.branch -eq 'main') 'PR base and trusted controller ref were conflated.'
    Update-LaneIntegrationView
    Assert (-not $IntegrationCheckpoint.Enabled) 'Missing checkpoint capability enabled a build request.'
    $script:IntegrationInspect.Observation.capabilities.checkpoint=$true
    $script:IntegrationInspect.Observation.staging=@{headSha=('f'*40)}
    $script:IntegrationInspect.Observation.capabilityReasons=@{checkpoint=@('release packaging')}
    Update-LaneIntegrationView
    Assert (-not $IntegrationCheckpoint.Enabled -and $IntegrationCheckpointReason.Text -match 'release packaging') 'Packaging blocker was not shown.'
    $script:IntegrationInspect.Observation.capabilityReasons.checkpoint=@()
    Set-Content $PauseFile 'Fixture paused'; Update-LaneIntegrationView
    Assert (-not $IntegrationCheckpoint.Enabled -and $IntegrationCheckpointReason.Text -match 'paused') 'Local pause allowed a checkpoint.'
    Remove-Item -LiteralPath $PauseFile; Update-LaneIntegrationView
    Assert $IntegrationCheckpoint.Enabled 'Fresh explicit checkpoint capability did not enable the frozen target.'
    if ($OutDirectory) {
        [void][IO.Directory]::CreateDirectory($OutDirectory)
        [Windows.Forms.Application]::DoEvents()
        $Bitmap=New-Object Drawing.Bitmap($Form.Width,$Form.Height)
        try { $Form.DrawToBitmap($Bitmap,(New-Object Drawing.Rectangle(0,0,$Form.Width,$Form.Height))); $Bitmap.Save((Join-Path $OutDirectory 'integration-mode.png')) } finally { $Bitmap.Dispose() }
    }
    Start-LaneIntegrationRequest checkpoint -Worker { param($Request) @{Accepted=$true;RequestId=$Request.RequestId} }; Drain-Integration
    $CheckpointId=$script:IntegrationPending.RequestId
    Assert ($script:IntegrationPending.Target -eq ('f'*40) -and $IntegrationMessage.Text -notmatch 'passed') 'Checkpoint acceptance claimed validation or lost target.'
    $Payload.State.controllerRequests=@(@{requestId=$CheckpointId;operation='checkpoint';status='completed';binding=(@{operation='checkpoint';target=('e'*40)} | ConvertTo-Json -Compress)})
    $Payload.State.requests=@(@{requestId=$CheckpointId;target=('f'*40);status='dispatching'})
    $Payload.State.checkpoints=@()
    $Payload.AtUtc=[datetime]::UtcNow
    $Json=$Payload | ConvertTo-Json -Depth 10 -Compress; $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
    $script:NextIntegrationRead=[datetime]::MinValue; Start-LaneIntegrationRead -Worker $Worker; Drain-Integration
    Assert ($script:IntegrationPending -and $script:IntegrationError -match 'binding') 'Mismatched checkpoint target was accepted.'
    $Payload.State.controllerRequests[0].binding=(@{operation='checkpoint';target=('f'*40)} | ConvertTo-Json -Compress)
    $Json=$Payload | ConvertTo-Json -Depth 10 -Compress; $Worker=[scriptblock]::Create("param(`$Request) '$($Json.Replace("'","''"))' | ConvertFrom-Json")
    $script:NextIntegrationRead=[datetime]::MinValue; Start-LaneIntegrationRead -Worker $Worker; Drain-Integration
    Assert (-not $script:IntegrationPending -and $IntegrationMessage.Text -match 'Checkpoint requested' -and $IntegrationMessage.Text -match 'dispatching' -and -not @($script:IntegrationSnapshot.State.checkpoints).Count) 'Queued checkpoint was confused with a verified receipt.'
    $script:IntegrationSnapshot.AtUtc=[datetime]::UtcNow.AddMinutes(-2); Update-LaneIntegrationView
    Assert ($IntegrationCurrent.Text -match 'Last confirmed' -and -not $IntegrationApply.Enabled) 'Stale mode evidence stayed actionable/current.'
    $script:NextIntegrationRead=[datetime]::MinValue
    Start-LaneIntegrationRead -Worker { Start-Sleep -Milliseconds 600; throw 'Retired fixture read' }
    Update-View
    Assert ($script:IntegrationJob -and $PauseButton.Enabled -and $GhButton.Enabled) 'A read-only integration poll disabled user buttons.'
    Start-LaneCommand -Kind cancel -Worker { @{AcceptedAtUtc=[datetime]::UtcNow} }
    Assert ($script:CommandJob -and -not $script:IntegrationJob) 'User action could not retire the background read.'
    $Until=[datetime]::UtcNow.AddSeconds(5)
    while ($script:CommandJob -and [datetime]::UtcNow -lt $Until) { Complete-LaneCommand; Start-Sleep -Milliseconds 20 }
    Assert (-not $script:CommandJob) 'Foreground action did not finish after retiring read.'
    Assert (-not $script:UiErrors.Count) 'A UI handler threw.'
    'PASS: immutable state reads, revision/UUID controller binding, no optimistic mode, fresh capabilities, busy/stale guards, durable readback and staging PR-base separation.'
} finally {
    foreach ($Job in @($script:CommandJob,$script:IntegrationJob,$script:ActivityJob,$script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
