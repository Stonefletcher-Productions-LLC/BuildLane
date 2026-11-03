# Server-owned integration mode. Requests never optimistically change the displayed mode.
$script:IntegrationJob=$null; $script:IntegrationSnapshot=$null; $script:IntegrationInspect=$null
$script:IntegrationPending=$null; $script:IntegrationIdentity=''; $script:IntegrationError=''
$script:NextIntegrationRead=[datetime]::MinValue
$script:IntegrationChoiceDirty=$false; $script:SettingIntegrationChoice=$false
$script:IntegrationWorker={
    param($Request)
    $ErrorActionPreference='Stop'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $Headers=@{Authorization="Bearer $($Request.Token)";Accept='application/vnd.github+json';'User-Agent'='Buildlane-Desktop'}
    function Call([string]$Method,[string]$Path,$Body=$null) {
        $Args=@{Method=$Method;Uri="https://api.github.com/repos/$($Request.Repo)$Path";Headers=$Headers;TimeoutSec=15}
        if ($null -ne $Body) { $Args.ContentType='application/json; charset=utf-8'; $Args.Body=[Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 6 -Compress)) }
        Invoke-RestMethod @Args
    }
    try {
        if ($Request.Kind -eq 'request') {
            $Inputs=@{operation=$Request.Operation;request_id=$Request.RequestId}
            if ($Request.Operation -ne 'inspect') { $Inputs.expected_revision=$Request.Revision }
            if ($Request.Operation -eq 'set-mode') { $Inputs.mode=$Request.Mode }
            if ($Request.Operation -eq 'checkpoint') { $Inputs.details=(@{target=$Request.Target} | ConvertTo-Json -Compress) }
            if ($Request.Operation -eq 'release') { $Inputs.details=(@{target=$Request.Target;version=$Request.Version} | ConvertTo-Json -Compress) }
            if ($Request.Operation -in @('stage','queue-merge')) { $Inputs.pr=[string]$Request.PrNumber; $Inputs.head=$Request.Head }
            if ($Request.Operation -eq 'cancel-merge') { $Inputs.details=(@{queue_id=$Request.QueueId} | ConvertTo-Json -Compress) }
            Call POST "/actions/workflows/$([uri]::EscapeDataString($Request.Workflow))/dispatches" @{ref='main';inputs=$Inputs} | Out-Null
            return @{Accepted=$true;RequestId=$Request.RequestId}
        }
        try { $Before=Call GET '/git/ref/heads/lane/staging-state' }
        catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404) { return @{Initialized=$false;Repo=$Request.Repo;AtUtc=[datetime]::UtcNow} }; throw }
        $Revision=[string]$Before.object.sha
        if ($Revision -notmatch '^[a-f0-9]{40}$') { throw 'Invalid state ref revision.' }
        $File=Call GET "/contents/state.json?ref=$Revision"
        if ($File.encoding -ne 'base64' -or [long]$File.size -gt 1MB) { throw 'Unsupported or oversized state file.' }
        $State=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(([string]$File.content -replace '\s',''))) | ConvertFrom-Json
        if ($State.schemaVersion -ne 1 -or $State.integrationMode -notin @('ValidateBeforeMerge','MergeToStagingThenValidate') -or $null -eq $State.work -or $null -eq $State.checkpoints) { throw 'Unsupported controller state.' }
        $After=Call GET '/git/ref/heads/lane/staging-state'
        if ([string]$After.object.sha -ne $Revision) { return @{Error='Integration state changed while loading. Refreshing is safe; no mode change was sent.';RetrySeconds=2} }
        @{Initialized=$true;Revision=$Revision;State=$State;Repo=$Request.Repo;AtUtc=[datetime]::UtcNow}
    } catch {
        $Delay=60
        if ($_.Exception.Response) { $Retry=0; if ([int]::TryParse([string]$_.Exception.Response.Headers['Retry-After'],[ref]$Retry)) { $Delay=[math]::Max(60,$Retry) } }
        @{Error=$(if ($Request.Kind -eq 'request') { 'The controller request could not be confirmed. Check its durable result before retrying; it will not be resent automatically.' } else { 'Integration state could not be verified. Mode changes stay disabled.' });RetrySeconds=$Delay}
    }
}
$IntegrationTab=New-Object Windows.Forms.TabPage('Integration'); $IntegrationTab.BackColor=$Paper
[void]$Tabs.TabPages.Add($IntegrationTab)
$IntegrationRoot=New-Object Windows.Forms.TableLayoutPanel
$IntegrationRoot.Dock='Fill'; $IntegrationRoot.AutoScroll=$true; $IntegrationRoot.ColumnCount=1; $IntegrationRoot.Padding=New-Object Windows.Forms.Padding(24)
$IntegrationTab.Controls.Add($IntegrationRoot)
[void](Add-Row (New-Label 'Integration mode' $FontTitle) 0 $IntegrationRoot)
$IntegrationCurrent=Add-Row (New-Label 'Reading the backend mode...' $FontHead) 12 $IntegrationRoot
$IntegrationBranch=Add-Row (New-Label '' $FontBody) 4 $IntegrationRoot
$IntegrationHelp=Add-Row (New-Label 'Validate before merge builds changes first. Merge to staging, then validate uses reviewed fast checks for staging merges and explicit native checkpoints before promotion.' $FontBody) 12 $IntegrationRoot
$IntegrationChoice=Add-Row (New-Object Windows.Forms.ComboBox) 12 $IntegrationRoot
$IntegrationChoice.DropDownStyle='DropDownList'; $IntegrationChoice.Items.AddRange(@('Validate before merge','Merge to staging, then validate')); $IntegrationChoice.SelectedIndex=0
$IntegrationChoice.AccessibleName='Requested integration mode'
$IntegrationControls=Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 12 $IntegrationRoot; $IntegrationControls.AutoSize=$true
$IntegrationRefresh=New-Button 'Refresh backend eligibility' {
    try { if ($script:IntegrationPending) { Start-LaneIntegrationRead -Force } else { Start-LaneIntegrationRequest inspect } }
    catch { $IntegrationMessage.Text=$_.Exception.Message; $IntegrationMessage.ForeColor=$Orange }
} $IntegrationControls
$IntegrationApply=New-Button 'Change integration mode' {
    try { Start-LaneIntegrationRequest set-mode } catch { $IntegrationMessage.Text=$_.Exception.Message; $IntegrationMessage.ForeColor=$Orange }
} $IntegrationControls
$IntegrationCheckpoint=New-Button 'Request checkpoint' {
    try { Start-LaneIntegrationRequest checkpoint } catch { $IntegrationMessage.Text=$_.Exception.Message; $IntegrationMessage.ForeColor=$Orange }
} $IntegrationControls
$IntegrationTarget=Add-Row (New-Label '' $FontSmall $Grey) 8 $IntegrationRoot
$IntegrationCheckpointReason=Add-Row (New-Label '' $FontBody $Orange) 4 $IntegrationRoot
$IntegrationOpen=New-Button 'Open controller runs' { if ($Config.integrationWorkflow) { Start-Process "https://github.com/$Repo/actions/workflows/$([uri]::EscapeDataString($Config.integrationWorkflow))" } } $IntegrationControls
$IntegrationBlock=Add-Row (New-Label '' $FontBody $Orange) 12 $IntegrationRoot
$IntegrationMessage=Add-Row (New-Label '' $FontBody $Blue) 8 $IntegrationRoot
$IntegrationDetails=Add-Row (New-Label '' $FontSmall $Grey) 12 $IntegrationRoot
[void](Add-Row (New-Label 'Full release tests and packaging remain required in both modes. Changing mode does not retarget PRs, move branches, clear queued work or rewrite validation receipts.' $FontSmall $Grey) 20 $IntegrationRoot)
$IntegrationChoice.Add_SelectedIndexChanged({ if (-not $script:SettingIntegrationChoice) { $script:IntegrationChoiceDirty=$true }; Update-LaneIntegrationView })
function Get-LaneModeLabel([string]$Mode) { if ($Mode -eq 'MergeToStagingThenValidate') { 'Merge to staging, then validate' } else { 'Validate before merge' } }
function Initialize-LaneIntegration {
    $Identity="$Repo|$Profile|$($Config.integrationWorkflow)"
    if ($Identity -eq $script:IntegrationIdentity) { return }
    Stop-LaneIntegration
    $script:IntegrationIdentity=$Identity; $script:IntegrationSnapshot=$null; $script:IntegrationInspect=$null
    $script:IntegrationPending=$null; $script:IntegrationError=''; $script:NextIntegrationRead=[datetime]::MinValue
    $IntegrationMessage.Text=''; $script:IntegrationChoiceDirty=$false
    $script:ReleaseRowsKey=''; $script:ReleaseRequestResult=''; if ($ReleaseVersion) { $ReleaseVersion.Clear() }
    $script:SettingIntegrationChoice=$true; $IntegrationChoice.SelectedIndex=0; $script:SettingIntegrationChoice=$false
}
function Test-LaneModeChangeAllowed {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    $Target=@('ValidateBeforeMerge','MergeToStagingThenValidate')[$IntegrationChoice.SelectedIndex]
    $Fresh=$S -and $S.Initialized -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60
    $Inspected=$I -and ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -le 60 -and $I.Observation.integrationMode -eq $S.State.integrationMode -and $I.Observation.controllerRef -eq 'main'
    $Allowed=$Inspected -and $I.Observation.capabilities.changeMode -is [bool] -and $I.Observation.capabilities.changeMode -eq $true -and -not @($I.Observation.blockingReasons).Count
    [bool]($Fresh -and $Allowed -and -not @($S.State.work).Count -and $Target -ne $S.State.integrationMode -and -not $script:IntegrationPending)
}
function Get-LaneCapabilityReasons([string]$Capability) {
    @($script:IntegrationInspect.Observation.capabilityReasons.$Capability | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
}
function Test-LaneCheckpointAllowed {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    [bool]($S -and $S.Initialized -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60 -and
        $S.State.integrationMode -eq 'MergeToStagingThenValidate' -and $I -and ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -le 60 -and
        $I.Observation.integrationMode -eq $S.State.integrationMode -and $I.Observation.controllerRef -eq 'main' -and
        $I.Observation.staging.headSha -match '^[a-f0-9]{40}$' -and $I.Observation.capabilities.checkpoint -is [bool] -and
        $I.Observation.capabilities.checkpoint -eq $true -and -not @(Get-LaneCapabilityReasons checkpoint).Count -and
        -not @($S.State.work).Count -and -not $script:IntegrationPending -and -not (Test-Path -LiteralPath $PauseFile))
}
function Update-LaneIntegrationView {
    Initialize-LaneIntegration
    $Configured=-not $Config.setupRequired -and -not [string]::IsNullOrWhiteSpace([string]$Config.integrationWorkflow)
    $Busy=$script:IntegrationJob -or $script:MoveJob -or $script:CommandJob -or $script:ActionJob
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    $Verified=$S -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60
    $IntegrationCurrent.Text=if (-not $Configured) { 'Integration backend not configured' } elseif (-not $S) { 'Current integration mode has not been verified' } elseif (-not $S.Initialized) { 'Backend not initialized (validation before merge)' } else { "$(if ($Verified) { 'Current mode' } else { 'Last confirmed mode' }): $(Get-LaneModeLabel $S.State.integrationMode)" }
    $IntegrationHelp.Text=if ($S -and $S.Initialized -and $S.State.integrationMode -eq 'MergeToStagingThenValidate') { 'Reviewed changes go to Test-Staging after fast checks. Request a checkpoint when ready to compile and test a frozen revision; promote only validated checkpoints. Resume allows work but does not request a checkpoint.' } else { 'Validate before merge builds changes first. Merge to staging, then validate uses reviewed fast checks for staging merges and explicit native checkpoints before promotion.' }
    $IntegrationBranch.Text=if ($I -and $S -and $S.Initialized -and $I.Observation.integrationMode -eq $S.State.integrationMode) { "Integration branch: $($I.Observation.integrationBranch). Controller: $($I.Observation.controllerRef)." } else { 'A fresh backend inspection reports the integration branch and available actions.' }
    $IntegrationChoice.Enabled=($Configured -and $S -and $S.Initialized -and -not $Busy -and -not $script:IntegrationPending)
    $IntegrationRefresh.Enabled=($Configured -and $S -and $S.Initialized -and -not $Busy)
    $IntegrationApply.Enabled=($Configured -and -not $Busy -and (Test-LaneModeChangeAllowed))
    $IntegrationOpen.Enabled=$Configured
    $IntegrationCheckpoint.Enabled=($Configured -and -not $Busy -and (Test-LaneCheckpointAllowed))
    $IntegrationTarget.Text=if ($I -and $I.Observation.staging.headSha -match '^[a-f0-9]{40}$') { "Checkpoint target: $($I.Observation.staging.headSha)" } else { 'Checkpoint target has not been verified.' }
    $IntegrationCheckpointReason.Text=if ($script:IntegrationPending -and $script:IntegrationPending.Operation -eq 'checkpoint') { 'Waiting for the checkpoint request result. Acceptance is not a test pass.' } elseif (Test-Path -LiteralPath $PauseFile) { 'Checkpoint waits: builds are paused. Resume builds first.' } elseif ($I -and @(Get-LaneCapabilityReasons checkpoint).Count) { 'Checkpoint blocked: '+(@(Get-LaneCapabilityReasons checkpoint) -join '; ') } elseif (-not $IntegrationCheckpoint.Enabled) { 'Request a fresh backend inspection. Checkpoints require staging mode, a verified target, an idle lane and explicit backend eligibility.' } else { 'This requests compilation and tests for the exact revision above. Full release coverage remains required before release.' }
    $IntegrationBlock.Text=if (-not $Configured) { 'Configure the integration controller workflow in Projects > Advanced settings.' } elseif ($script:IntegrationError) { $script:IntegrationError } elseif (-not $S -or -not $S.Initialized) { 'The release owner must initialize and verify the backend before these controls can change it.' } elseif ($script:IntegrationPending) { "Waiting for the server result of request $($script:IntegrationPending.RequestId). No automatic retry." } elseif (@($S.State.work).Count) { 'Outstanding work blocks a mode change: '+(@($S.State.work) -join '; ') } elseif ($I -and @($I.Observation.blockingReasons).Count) { 'Mode change blocked: '+(@($I.Observation.blockingReasons) -join '; ') } elseif (-not $I -or ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -gt 60) { 'Refresh backend eligibility before changing mode.' } else { '' }
    if ($S -and $S.Initialized) { $IntegrationDetails.Text="State revision: $($S.Revision)`nVerified at $($S.AtUtc.ToLocalTime().ToString('HH:mm:ss')). Recorded outstanding work: $(@($S.State.work).Count). Checkpoint records: $(@($S.State.checkpoints).Count)." } else { $IntegrationDetails.Text='' }
    Update-LaneReleaseRequestsView
    if ($Configured -and -not (Test-LaneLegacyOperationsAllowed)) { $RelButtons.Enabled=$false; $MoveBar.Enabled=$false }
    elseif (-not $script:MoveJob -and -not $script:ActionJob -and -not $script:CommandJob -and -not $script:RefreshJob -and -not $script:IntegrationPending) { $RelButtons.Enabled=$true; $MoveBar.Enabled=$true }
}
function Start-LaneIntegrationWorker($Request,[scriptblock]$Worker) {
    $Shell=[PowerShell]::Create()
    try {
        if (-not $Worker) { $Worker=$script:IntegrationWorker }
        [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request)
        $script:IntegrationJob=@{Shell=$Shell;Handle=$Shell.BeginInvoke();Kind=$Request.Kind;Generation=$script:ProjectGeneration;Identity=$script:IntegrationIdentity;RequestId=$Request.RequestId}
    } catch { $Shell.Dispose(); throw }
}
function Start-LaneIntegrationRead([scriptblock]$Worker,[switch]$Force) {
    Initialize-LaneIntegration
    if ($Config.setupRequired -or -not $Config.integrationWorkflow -or $script:IntegrationJob -or $script:RefreshJob -or $script:ActivityJob -or $script:MoveJob -or $script:CommandJob -or $script:ActionJob -or [datetime]::UtcNow -lt $script:NextIntegrationRead) { return }
    if (-not $Worker -and ($SmokeTest -or -not $script:Token)) { return }
    Start-LaneIntegrationWorker @{Kind='read';Repo=$Repo;Token=$script:Token} $Worker
    $script:NextIntegrationRead=[datetime]::UtcNow.AddSeconds($(if ($script:IntegrationPending) { 2 } else { 15 }))
}
function Start-LaneIntegrationRequest([ValidateSet('inspect','set-mode','checkpoint','release','stage','queue-merge','cancel-merge','pause-integration','resume-integration')][string]$Operation,[scriptblock]$Worker,[int]$PrNumber,[string]$Head,[string]$QueueId) {
    if ($script:IntegrationJob -or $script:IntegrationPending -or $script:CommandJob -or $script:MoveJob -or $script:ActionJob) { return }
    if ($Config.setupRequired -or -not $Config.integrationWorkflow -or -not $script:IntegrationSnapshot.Initialized) { throw 'Initialize and verify the integration backend first.' }
    if (-not $Worker -and ($SmokeTest -or -not $script:Token)) { throw 'Connect GitHub before sending a controller request.' }
    if ($Operation -eq 'set-mode' -and -not (Test-LaneModeChangeAllowed)) { throw 'The backend has not confirmed that this mode change is currently allowed. Refresh eligibility.' }
    if ($Operation -eq 'checkpoint' -and -not (Test-LaneCheckpointAllowed)) { throw 'The backend has not confirmed that this checkpoint is currently allowed. Refresh eligibility.' }
    if ($Operation -eq 'release' -and -not (Test-LaneReleaseRequestAllowed)) { throw 'The backend has not confirmed release eligibility for this frozen target and version.' }
    if ($Operation -in @('pause-integration','resume-integration') -and -not (Test-LaneMergePauseAllowed $Operation)) { throw 'Refresh merge eligibility before changing the server merge pause.' }
    if ($Operation -in @('stage','queue-merge','cancel-merge')) {
        $Selected=Get-LaneSelectedPull
        if (-not (Test-LanePullActionAllowed $Operation $Selected)) { throw 'Refresh merge eligibility and select a reviewed PR at its current head.' }
        if ($Operation -ne 'cancel-merge' -and ($Selected.number -ne $PrNumber -or $Selected.headRefOid -ne $Head)) { throw 'The selected PR changed. Refresh before merging.' }
        if ($Operation -eq 'cancel-merge' -and (Get-LaneSelectedMergeQueue).queueId -ne $QueueId) { throw 'The queued merge changed. Refresh before cancelling.' }
    }
    $Version=if ($Operation -eq 'release') { $ReleaseVersion.Text.Trim() } else { '' }
    $Target=if ($Operation -eq 'checkpoint') { [string]$script:IntegrationInspect.Observation.staging.headSha } elseif ($Operation -eq 'release') { Get-LaneReleaseTarget } else { '' }
    $Mode=if ($Operation -eq 'set-mode') { @('ValidateBeforeMerge','MergeToStagingThenValidate')[$IntegrationChoice.SelectedIndex] } else { '' }
    $RequestId=[guid]::NewGuid().ToString()
    $Request=@{Kind='request';Operation=$Operation;RequestId=$RequestId;Revision=$script:IntegrationSnapshot.Revision;Mode=$Mode;Target=$Target;Version=$Version;Repo=$Repo;Workflow=$Config.integrationWorkflow;Token=$script:Token}
    $Request.PrNumber=$PrNumber; $Request.Head=$Head; $Request.QueueId=$QueueId
    Stop-LaneRefresh
    $script:IntegrationPending=@{RequestId=$RequestId;Operation=$Operation;Mode=$Mode;Target=$Target;Version=$Version;BeforeMode=$script:IntegrationSnapshot.State.integrationMode;RequestedAtUtc=[datetime]::UtcNow}
    $script:IntegrationPending.PrNumber=$PrNumber; $script:IntegrationPending.Head=$Head; $script:IntegrationPending.QueueId=$QueueId
    Start-LaneIntegrationWorker $Request $Worker
    if ($Operation -eq 'release') { $script:ReleaseRequestResult="Sending release $Version for $Target; request $RequestId." }
    $IntegrationMessage.Text='Sending a revision-bound controller request...'; $IntegrationMessage.ForeColor=$Blue
    Update-View
}
function Complete-LaneIntegration {
    $Job=$script:IntegrationJob
    if (-not $Job -or -not $Job.Handle.IsCompleted) { return }
    try { $Results=@($Job.Shell.EndInvoke($Job.Handle)); if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'No confirmed result' }; $Result=$Results[-1] }
    catch { $Result=@{Error='Integration response could not be confirmed. Read the backend result before retrying.';RetrySeconds=60} }
    finally { $Job.Shell.Dispose(); $script:IntegrationJob=$null }
    if ($Job.Generation -ne $script:ProjectGeneration -or $Job.Identity -ne $script:IntegrationIdentity) { return }
    if ($Result.Error) { $script:IntegrationError=$Result.Error; $script:NextIntegrationRead=[datetime]::UtcNow.AddSeconds([math]::Max(2,[int]$Result.RetrySeconds)) }
    elseif ($Job.Kind -eq 'request') {
        if (-not $Result.Accepted -or $Result.RequestId -ne $Job.RequestId) { $script:IntegrationError='Controller acceptance identity did not match. Read the backend result before retrying.' }
        else { $IntegrationMessage.Text='Controller request accepted. The displayed mode changes only after authoritative readback.'; $script:IntegrationError=''; $script:NextIntegrationRead=[datetime]::MinValue }
    } elseif ($Result.Repo -eq $Repo) {
        $script:IntegrationSnapshot=$Result; $script:IntegrationError=''
        if ($Result.Initialized -and -not $script:IntegrationChoiceDirty) {
            $script:SettingIntegrationChoice=$true; $IntegrationChoice.SelectedIndex=$(if ($Result.State.integrationMode -eq 'MergeToStagingThenValidate') { 1 } else { 0 }); $script:SettingIntegrationChoice=$false
        }
        $P=$script:IntegrationPending
        if ($P -and $Result.Initialized) {
            $Records=@($Result.State.controllerRequests | Where-Object { $_.requestId -eq $P.RequestId -and $_.operation -eq $P.Operation })
            if ($Records.Count -gt 1) { $script:IntegrationError='Duplicate controller request records are ambiguous. Mode controls stay disabled.' }
            elseif ($Records.Count -eq 1) {
                $Record=$Records[0]
                try { $Binding=$Record.binding | ConvertFrom-Json } catch { $Binding=$null }
                $Bound=$Binding -and $Binding.operation -eq $P.Operation -and ($P.Operation -ne 'set-mode' -or $Binding.requested -eq $P.Mode) -and ($P.Operation -notin @('checkpoint','release') -or $Binding.target -eq $P.Target) -and ($P.Operation -ne 'release' -or $Binding.version -eq $P.Version)
                if ($P.Operation -in @('stage','queue-merge')) { $Bound=$Bound -and [int]$Binding.number -eq $P.PrNumber -and $Binding.head -eq $P.Head }
                if ($P.Operation -eq 'cancel-merge') { $Bound=$Bound -and $Binding.queueId -eq $P.QueueId }
                if (-not $Bound) { $script:IntegrationError='The durable request binding differs from this action. No retry was sent.' }
                elseif ($Record.status -in @('failed','attention')) {
                    if ($P.Operation -eq 'release') { $script:ReleaseRequestResult="Release request $($Record.status): $($Record.message)" }
                    if ($P.Operation -in @('stage','queue-merge','cancel-merge','pause-integration','resume-integration')) { $script:PullActionMessage="Merge request $($Record.status): $($Record.message)" }
                    $IntegrationMessage.Text="Controller $($Record.status): $($Record.message)"; $IntegrationMessage.ForeColor=$Orange; $script:IntegrationPending=$null; $script:IntegrationInspect=$null
                }
                elseif ($Record.status -eq 'completed') {
                    if ($P.Operation -eq 'inspect' -and $Record.observation.initialized -eq $true -and $Record.observation.integrationMode -eq $Result.State.integrationMode) {
                        $script:IntegrationInspect=@{RequestId=$P.RequestId;Observation=$Record.observation;AtUtc=[datetime]::UtcNow}
                        $IntegrationMessage.Text='Backend eligibility verified. Current mode is reported by the server.'; $script:IntegrationPending=$null
                    } elseif ($P.Operation -eq 'set-mode' -and $Result.State.integrationMode -eq $P.Mode) {
                        $IntegrationMessage.Text="Server confirmed mode: $(Get-LaneModeLabel $P.Mode)."; $script:IntegrationPending=$null; $script:IntegrationInspect=$null
                        $script:IntegrationChoiceDirty=$false; $script:SettingIntegrationChoice=$true
                        $IntegrationChoice.SelectedIndex=$(if ($P.Mode -eq 'MergeToStagingThenValidate') { 1 } else { 0 }); $script:SettingIntegrationChoice=$false
                    } elseif ($P.Operation -eq 'checkpoint') {
                        $Requests=@($Result.State.requests | Where-Object { $_.requestId -eq $P.RequestId -and $_.target -eq $P.Target })
                        if ($Requests.Count -eq 1) {
                            $IntegrationMessage.Text="Checkpoint requested for $($P.Target). Recorded state: $($Requests[0].status). Compilation and test results are still separate."
                            $script:IntegrationPending=$null; $script:IntegrationInspect=$null
                        } else { $script:IntegrationError='The completed request has no matching frozen checkpoint record. Inspect the controller; no retry was sent.' }
                    } elseif ($P.Operation -eq 'release') {
                        $Releases=@(Get-LaneReleaseRecords $Result.State | Where-Object { $_.requestId -eq $P.RequestId -and $_.target -eq $P.Target -and $_.version -eq $P.Version })
                        if ($Releases.Count -eq 1) {
                            $script:ReleaseRequestResult="Release $($P.Version) request recorded. Follow its current Full tests and Package results below."
                            $IntegrationMessage.Text=$script:ReleaseRequestResult
                            $script:IntegrationPending=$null; $script:IntegrationInspect=$null
                        } else { $script:IntegrationError='The completed release request has no matching frozen version and target record. Inspect the controller; no retry was sent.' }
                    } elseif ($P.Operation -in @('stage','queue-merge','cancel-merge')) {
                        Complete-LanePullRequest $P $Result.State
                    } elseif ($P.Operation -in @('pause-integration','resume-integration') -and $Result.State.integrationPaused -is [bool] -and $Result.State.integrationPaused -eq ($P.Operation -eq 'pause-integration')) {
                        $script:PullActionMessage=if ($Result.State.integrationPaused) { 'Server confirmed: merges paused. Build pause is separate.' } else { 'Server confirmed: merges running. Queued reviewed heads can now be processed.' }
                        $script:IntegrationPending=$null; $script:IntegrationInspect=$null
                    } else { $script:IntegrationError='The completed request does not match the authoritative mode. Refresh and inspect the controller; no retry was sent.' }
                }
            }
        }
    }
    Update-View
}
function Stop-LaneIntegration {
    if ($script:IntegrationJob) { [void]$script:IntegrationJob.Shell.BeginStop($null,$null); [void]$script:RetiredJobs.Add($script:IntegrationJob); $script:IntegrationJob=$null }
}
function Get-LaneIntegrationBranch {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    if ($Config.integrationWorkflow -and (-not $S -or $script:IntegrationError -or ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -gt 60)) { return $null }
    if ($S -and $S.Initialized -and $S.State.integrationMode -eq 'MergeToStagingThenValidate') {
        if ($I -and $I.Observation.integrationMode -eq $S.State.integrationMode -and $I.Observation.integrationBranch -eq 'Test-Staging') { return 'Test-Staging' }
        return $null
    }
    $Config.branch
}
function Test-LaneLegacyOperationsAllowed {
    if (-not $Config.integrationWorkflow) { return $true }
    $S=$script:IntegrationSnapshot
    [bool]($S -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60 -and (-not $S.Initialized -or $S.State.integrationMode -eq 'ValidateBeforeMerge') -and -not $script:IntegrationPending)
}
