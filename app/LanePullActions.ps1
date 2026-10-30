# All merge mutations go through the trusted controller, never directly through GitHub merge APIs.
$PullActions=New-Object Windows.Forms.FlowLayoutPanel
$PullActions.AutoSize=$true; $PullActions.Dock='Fill'; $PullActions.WrapContents=$true
$QueuePanel.RowCount=4
$QueuePanel.SetRow($QueueHint,3)
[void]$QueuePanel.RowStyles.Add((New-Object Windows.Forms.RowStyle('AutoSize')))
[void]$QueuePanel.Controls.Add($PullActions,0,2)
$PullRefresh=New-Button 'Refresh merge eligibility' {
    try { $script:NextPullRead=[datetime]::MinValue; if ($script:IntegrationPending) { Start-LaneIntegrationRead -Force } else { Start-LaneIntegrationRequest inspect } }
    catch { $script:PullActionMessage=$_.Exception.Message; Update-View }
} $PullActions
$PullMerge=New-Button 'Merge now' { try { Start-LanePullAction stage } catch { $script:PullActionMessage=$_.Exception.Message; Update-View } } $PullActions
$PullAuto=New-Button 'Merge when checks pass' { try { Start-LanePullAction queue-merge } catch { $script:PullActionMessage=$_.Exception.Message; Update-View } } $PullActions
$PullCancel=New-Button 'Cancel queued merge' { try { Start-LanePullAction cancel-merge } catch { $script:PullActionMessage=$_.Exception.Message; Update-View } } $PullActions
$MergePause=New-Button 'Resume merges' {
    try { Start-LaneIntegrationRequest $(if ($script:IntegrationSnapshot.State.integrationPaused -is [bool] -and $script:IntegrationSnapshot.State.integrationPaused -eq $false) { 'pause-integration' } else { 'resume-integration' }) }
    catch { $script:PullActionMessage=$_.Exception.Message; Update-View }
} $PullActions
$PullMerge.Enabled=$false; $PullAuto.Enabled=$false; $PullCancel.Enabled=$false
# Merged PRs are history, not work: hidden unless asked for, so the list only shows what is still moving.
$PullShowMerged=New-Object Windows.Forms.CheckBox
$PullShowMerged.Text='Show recent merges'; $PullShowMerged.AutoSize=$true; $PullShowMerged.Checked=$false; $PullShowMerged.Font=$FontBody
$PullShowMerged.Margin=New-Object Windows.Forms.Padding(12, 10, 0, 0)
$PullShowMerged.Add_CheckedChanged({ $script:PullRowsKey=''; Update-View })
[void]$PullActions.Controls.Add($PullShowMerged)
$Queue.Add_SelectedIndexChanged({ Update-LanePullActions })
function Get-LaneSelectedPull {
    if (-not $Queue.SelectedItems.Count) { return $null }
    $Number=$Queue.SelectedItems[0].Tag
    $Found=@($script:PullSnapshot.Pulls | Where-Object { $_.number -eq $Number })
    if ($Found.Count -eq 1) { return $Found[0] }
}
function Get-LaneSelectedMergeQueue {
    if (-not $Queue.SelectedItems.Count) { return $null }
    $Number=$Queue.SelectedItems[0].Tag
    $Found=@($script:IntegrationSnapshot.State.mergeQueue | Where-Object { $_.prNumber -eq $Number -and $_.status -in @('queued','waiting-checks','waiting-conflict','needs-review','attention') })
    if ($Found.Count -eq 1) { return $Found[0] }
}
function Update-LanePullActions {
    if (-not $PullActions) { return }
    $Visible=[bool]$Config.integrationWorkflow
    if ($PullActions.Visible -ne $Visible) { $PullActions.Visible=$Visible }
    $Pr=Get-LaneSelectedPull
    $PullMerge.Enabled=Test-LanePullActionAllowed stage $Pr
    $PullAuto.Enabled=Test-LanePullActionAllowed queue-merge $Pr
    $PullCancel.Enabled=Test-LanePullActionAllowed cancel-merge $Pr
    $PullRefresh.Enabled=[bool]($Config.integrationWorkflow -and -not $script:IntegrationJob -and -not $script:IntegrationPending -and -not $script:CommandJob -and -not $script:ActionJob -and -not $script:MoveJob)
    $Running=$script:IntegrationSnapshot.State.integrationPaused -is [bool] -and $script:IntegrationSnapshot.State.integrationPaused -eq $false
    $MergePause.Text=if ($Running) { 'Pause merges' } else { 'Resume merges' }
    $MergePause.Enabled=Test-LaneMergePauseAllowed $(if ($Running) { 'pause-integration' } else { 'resume-integration' })
}
function Test-LaneMergePauseAllowed([string]$Operation) {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    if (-not $Config.integrationWorkflow -or -not $S.Initialized -or $script:IntegrationError -or ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -gt 60 -or -not $I -or ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -gt 60 -or $I.Observation.controllerRef -ne 'main' -or $I.Observation.integrationMode -ne $S.State.integrationMode) { return $false }
    if ($script:IntegrationJob -or $script:IntegrationPending -or $script:MoveJob -or $script:CommandJob -or $script:ActionJob) { return $false }
    $Capability=if ($Operation -eq 'pause-integration') { $I.Observation.capabilities.pauseIntegration } else { $I.Observation.capabilities.resumeIntegration }
    [bool]($Capability -is [bool] -and $Capability)
}
function Test-LanePullActionAllowed([string]$Operation,$Pr) {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    $Fresh=$S -and $S.Initialized -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60 -and $I -and ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -le 60
    if (-not $Fresh -or -not $Config.integrationWorkflow -or $S.State.integrationMode -ne 'MergeToStagingThenValidate' -or $I.Observation.integrationMode -ne $S.State.integrationMode -or $I.Observation.controllerRef -ne 'main') { return $false }
    if ($script:IntegrationJob -or $script:IntegrationPending -or $script:MoveJob -or $script:CommandJob -or $script:ActionJob) { return $false }
    if ($Operation -eq 'cancel-merge') { return [bool]($I.Observation.capabilities.cancelMerge -is [bool] -and $I.Observation.capabilities.cancelMerge -and (Get-LaneSelectedMergeQueue)) }
    if (-not $Pr -or $Pr.isDraft -or $Pr.baseRefName -ne 'Test-Staging' -or $Pr.headRefOid -notmatch '^[a-f0-9]{40}$' -or -not (Test-LanePullReviewed $Pr)) { return $false }
    if (-not $script:PullSnapshot -or $script:PullError -or ([datetime]::UtcNow-$script:PullSnapshot.AtUtc).TotalSeconds -gt 90 -or (Get-LaneSelectedMergeQueue)) { return $false }
    if ($Operation -eq 'queue-merge') { return [bool]($I.Observation.capabilities.queueMerge -is [bool] -and $I.Observation.capabilities.queueMerge) }
    $Checks=[string](@($Pr.commits.nodes)[0].commit.statusCheckRollup.state)
    [bool]($S.State.integrationPaused -is [bool] -and $S.State.integrationPaused -eq $false -and $I.Observation.capabilities.stage -is [bool] -and $I.Observation.capabilities.stage -and $Checks -eq 'SUCCESS' -and $Pr.mergeable -eq 'MERGEABLE' -and $Pr.mergeStateStatus -eq 'CLEAN')
}
function Get-LanePullActionReason($Pr) {
    if ($Pr.isDraft) { return 'Finish the draft before merging.' }
    if (-not (Test-LanePullReviewed $Pr)) { return 'Review required at this exact head. Open the PR for review; successful checks are not approval.' }
    if ($Pr.baseRefName -ne 'Test-Staging') { return 'This PR targets another branch. Staging merge controls do not retarget it.' }
    $Queued=Get-LaneSelectedMergeQueue
    if ($Queued) { return "Queued merge: $($Queued.status). $($Queued.reason) Cancel only removes this merge request, not a build." }
    if ($script:IntegrationSnapshot.State.integrationPaused -ne $false) { return 'Merges are paused. Reviewed changes can be queued; Resume merges allows the server to process them. Pause builds is separate.' }
    if ($PullMerge.Enabled) { return 'Reviewed and checks successful. Merge now still rechecks the exact head on the server.' }
    if ($PullAuto.Enabled) { return 'Queue this reviewed head. The server waits for checks and rechecks review, conflicts and merge-pause state; a new head requires review again.' }
    'Refresh merge eligibility. The backend must explicitly permit this operation; stale or busy state disables it.'
}
function Start-LanePullAction([string]$Operation,[scriptblock]$Worker) {
    $Pr=Get-LaneSelectedPull; $Queued=Get-LaneSelectedMergeQueue
    if (-not (Test-LanePullActionAllowed $Operation $Pr)) { throw (Get-LanePullActionReason $Pr) }
    $script:PullActionMessage=''
    if ($Operation -eq 'cancel-merge') { Start-LaneIntegrationRequest $Operation -QueueId $Queued.queueId -Worker $Worker }
    else { Start-LaneIntegrationRequest $Operation -PrNumber $Pr.number -Head $Pr.headRefOid -Worker $Worker }
}
function Complete-LanePullRequest($Pending,$State) {
    $Matches=@()
    if ($Pending.Operation -eq 'stage') { $Matches=@($State.merges | Where-Object { $_.prNumber -eq $Pending.PrNumber -and $_.head -eq $Pending.Head -and $_.result -match '^[a-f0-9]{40}$' }) }
    elseif ($Pending.Operation -eq 'queue-merge') { $Matches=@($State.mergeQueue | Where-Object { $_.queueId -eq $Pending.RequestId -and $_.prNumber -eq $Pending.PrNumber -and $_.head -eq $Pending.Head }) }
    else { $Matches=@($State.mergeQueue | Where-Object { $_.queueId -eq $Pending.QueueId -and $_.status -eq 'cancelled' }) }
    if ($Matches.Count -ne 1) { $script:IntegrationError='The controller response has no unique matching merge record. No retry was sent.'; return }
    $script:PullActionMessage=switch ($Pending.Operation) {
        'stage' { "Controller confirmed PR #$($Pending.PrNumber) merged at $($Matches[0].result)." }
        'queue-merge' { "Merge request recorded for PR #$($Pending.PrNumber). Follow its current merge queue state in the row above." }
        'cancel-merge' { 'Queued merge cancelled. Builds were not cancelled.' }
    }
    $script:IntegrationPending=$null; $script:IntegrationInspect=$null; $script:NextPullRead=[datetime]::MinValue
}
