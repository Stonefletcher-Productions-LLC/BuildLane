# Staging releases are durable controller requests, separate from legacy milestones.
$script:ReleasePanelMode=$null; $script:ReleaseRowsKey=''
$StagingReleaseRoot=New-Object Windows.Forms.TableLayoutPanel
$StagingReleaseRoot.Dock='Fill'; $StagingReleaseRoot.AutoScroll=$true; $StagingReleaseRoot.ColumnCount=1
$StagingReleaseRoot.Padding=New-Object Windows.Forms.Padding(24)
[void]$StagingReleaseRoot.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
$StagingReleaseRoot.Visible=$false; $RelTab.Controls.Add($StagingReleaseRoot)
[void](Add-Row (New-Label 'Release a frozen staging revision' $FontTitle) 0 $StagingReleaseRoot)
[void](Add-Row (New-Label 'Queue one exact version and source revision. The backend must complete full tests and packaging for that release; an ordinary checkpoint or a queued request is not a release pass.' $FontBody) 8 $StagingReleaseRoot)
$ReleaseTargetLabel=Add-Row (New-Label 'Release target has not been verified.' $FontSmall $Grey) 12 $StagingReleaseRoot
$ReleaseVersionRow=Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 12 $StagingReleaseRoot
$ReleaseVersionRow.AutoSize=$true
[void]$ReleaseVersionRow.Controls.Add((New-Label 'Version (for example 0.3.0)' $FontBody))
$ReleaseVersion=New-Object Windows.Forms.TextBox; $ReleaseVersion.Width=160
$ReleaseVersion.AccessibleName='Frozen release version'; $ReleaseVersionRow.Controls.Add($ReleaseVersion)
$ReleaseRequestButtons=Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 8 $StagingReleaseRoot
$ReleaseRequestButtons.AutoSize=$true
$ReleaseEligibility=New-Button 'Refresh release eligibility' {
    try { if ($script:IntegrationPending) { Start-LaneIntegrationRead -Force } else { Start-LaneIntegrationRequest inspect } }
    catch { $ReleaseRequestMessage.Text=$_.Exception.Message; $ReleaseRequestMessage.ForeColor=$Orange }
} $ReleaseRequestButtons
$ReleaseQueue=New-Button 'Queue full tests and package' {
    try { Start-LaneIntegrationRequest release }
    catch { $ReleaseRequestMessage.Text=$_.Exception.Message; $ReleaseRequestMessage.ForeColor=$Orange }
} $ReleaseRequestButtons
$ReleaseOpen=New-Button 'Open selected build' {
    if ($ReleaseRequests.SelectedItems.Count) {
        $Record=$ReleaseRequests.SelectedItems[0].Tag
        if ([string]$Record.runId -match '^[1-9]\d*$' -and [string]$Record.attempt -match '^[1-9]\d*$') {
            Start-Process "https://github.com/$Repo/actions/runs/$($Record.runId)/attempts/$($Record.attempt)"
        }
    }
} $ReleaseRequestButtons
$ReleaseRequestMessage=Add-Row (New-Label '' $FontBody $Orange) 8 $StagingReleaseRoot
$ReleaseRequestResultLabel=Add-Row (New-Label '' $FontSmall $Blue) 4 $StagingReleaseRoot
$ReleaseRequests=Add-Row (New-Object Windows.Forms.ListView) 12 $StagingReleaseRoot
$ReleaseRequests.View='Details'; $ReleaseRequests.FullRowSelect=$true; $ReleaseRequests.MultiSelect=$false
$ReleaseRequests.HideSelection=$false; $ReleaseRequests.HeaderStyle='Nonclickable'; $ReleaseRequests.Height=240
foreach ($Column in @(@('Version',90),@('Source',120),@('Server state',160),@('Full tests',105),@('Package',105),@('Build / attempt',145))) { [void]$ReleaseRequests.Columns.Add($Column[0],$Column[1]) }
$ReleaseRequestDetails=Add-Row (New-Label '' $FontSmall $Grey) 8 $StagingReleaseRoot
$ReleaseVersion.Add_TextChanged({ Update-LaneReleaseRequestsView })
$ReleaseRequests.Add_SelectedIndexChanged({ Update-LaneReleaseRequestSelection })
function Test-LaneReleaseVersion([string]$Version) { [bool]($Version -match '^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$' -and $Version.Length -le 64) }
function Get-LaneReleaseTarget {
    $I=$script:IntegrationInspect
    if ($I -and $I.Observation.releaseTarget.sourceSha -match '^[a-f0-9]{40}$') { [string]$I.Observation.releaseTarget.sourceSha }
}
function Test-LaneReleaseRequestAllowed {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    [bool]($S -and $S.Initialized -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60 -and
        $S.State.integrationMode -eq 'MergeToStagingThenValidate' -and $I -and ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -le 60 -and
        $I.Observation.integrationMode -eq $S.State.integrationMode -and $I.Observation.controllerRef -eq 'main' -and
        (Get-LaneReleaseTarget) -and $I.Observation.capabilities.release -is [bool] -and $I.Observation.capabilities.release -eq $true -and
        -not @($I.Observation.capabilityReasons.release).Count -and -not @($S.State.work).Count -and
        -not $script:IntegrationPending -and -not (Test-Path -LiteralPath $PauseFile) -and (Test-LaneReleaseVersion $ReleaseVersion.Text.Trim()))
}
function Update-LaneReleaseRequestSelection {
    $Record=if ($ReleaseRequests.SelectedItems.Count) { $ReleaseRequests.SelectedItems[0].Tag }
    $ReleaseOpen.Enabled=[bool]($Record -and [string]$Record.runId -match '^[1-9]\d*$' -and [string]$Record.attempt -match '^[1-9]\d*$')
    $ReleaseRequestDetails.Text=if ($Record) { "Request: $($Record.requestId)`nVersion: $($Record.version). Frozen input: $($Record.target).`nTested output: $(if ($Record.outputSha) { $Record.outputSha } else { 'not reported' }). Tree: $(if ($Record.treeSha) { $Record.treeSha } else { 'not reported' }).`n$($Record.message)" } else { 'Select a release request to see its exact input, tested output and build attempt. Outcomes below are reported by the backend; missing results remain unreported.' }
}
function Update-LaneReleaseRequestsView {
    $S=$script:IntegrationSnapshot; $I=$script:IntegrationInspect
    $Staging=[bool]($Config.integrationWorkflow -and $S -and $S.Initialized -and $S.State.integrationMode -eq 'MergeToStagingThenValidate')
    if ($null -eq $script:ReleasePanelMode -or $Staging -ne $script:ReleasePanelMode) {
        $script:ReleasePanelMode=$Staging
        $RelSplit.Visible=-not $Staging; $StagingReleaseRoot.Visible=$Staging
        if ($Staging) { $StagingReleaseRoot.BringToFront() }
    }
    if (-not $Staging) { return }
    $Busy=$script:IntegrationJob -or $script:MoveJob -or $script:CommandJob -or $script:ActionJob
    $Target=Get-LaneReleaseTarget
    $Fresh=$S -and -not $script:IntegrationError -and ([datetime]::UtcNow-$S.AtUtc).TotalSeconds -le 60 -and $I -and ([datetime]::UtcNow-$I.AtUtc).TotalSeconds -le 60
    $ReleaseRequestResultLabel.Text=[string]$script:ReleaseRequestResult
    $ReleaseTargetLabel.Text=if ($Target) { "$(if ($Fresh) { 'Inspected release target' } else { 'Last inspected release target' }): $Target" } else { 'Release target has not been verified. Refresh release eligibility.' }
    $ReleaseQueue.Enabled=(-not $Busy -and (Test-LaneReleaseRequestAllowed))
    $ReleaseEligibility.Enabled=(-not $Busy)
    $ReleaseVersion.Enabled=(-not $script:IntegrationPending -and -not ($script:IntegrationJob -and $script:IntegrationJob.Kind -eq 'request'))
    $ReleaseRequestMessage.Text=if ($script:IntegrationPending -and $script:IntegrationPending.Operation -eq 'release') { "Waiting for release request $($script:IntegrationPending.RequestId), version $($script:IntegrationPending.Version). Acceptance does not mean full tests or packaging passed. No automatic retry." }
        elseif ($script:IntegrationError) { $script:IntegrationError }
        elseif (Test-Path -LiteralPath $PauseFile) { 'Release waits: builds are paused. Resume builds first.' }
        elseif ($I -and @($I.Observation.capabilityReasons.release).Count) { 'Release blocked: '+(@($I.Observation.capabilityReasons.release) -join '; ') }
        elseif (-not $Fresh -or -not $Target) { 'Refresh release eligibility to confirm a frozen target and current availability.' }
        elseif (@($S.State.work).Count) { 'Release waits for outstanding work: '+(@($S.State.work) -join '; ') }
        elseif (-not (Test-LaneReleaseVersion $ReleaseVersion.Text.Trim())) { 'Enter a version such as 0.3.0, without a leading v.' }
        elseif (-not $ReleaseQueue.Enabled) { 'The backend has not confirmed that a release can be queued. Full release coverage cannot be bypassed.' }
        else { 'This queues full tests and packaging for the exact version and revision above. Later staging changes are separate.' }
    $Records=@($S.State.releaseRequests | Where-Object { $null -ne $_ })
    $Key="$Repo|$Profile|"+($Records | ConvertTo-Json -Depth 12 -Compress)
    if ($Key -ne $script:ReleaseRowsKey) {
        $script:ReleaseRowsKey=$Key
        $Selected=if ($ReleaseRequests.SelectedItems.Count) { $ReleaseRequests.SelectedItems[0].Tag.requestId }
        $ReleaseRequests.BeginUpdate()
        try {
            $ReleaseRequests.Items.Clear()
            foreach ($Record in @($Records | Select-Object -Last 30)) {
                $Source=if ([string]$Record.target -match '^[a-f0-9]{40}$') { $Record.target.Substring(0,12) } else { 'unreported' }
                $Item=New-Object Windows.Forms.ListViewItem([string]$Record.version)
                [void]$Item.SubItems.Add($Source)
                [void]$Item.SubItems.Add($(if ($Record.status) { [string]$Record.status } else { 'unreported' }))
                [void]$Item.SubItems.Add($(if ($Record.fullTests) { [string]$Record.fullTests } else { 'unreported' }))
                [void]$Item.SubItems.Add($(if ($Record.package) { [string]$Record.package } else { 'unreported' }))
                [void]$Item.SubItems.Add($(if ([string]$Record.runId -match '^[1-9]\d*$' -and [string]$Record.attempt -match '^[1-9]\d*$') { "$($Record.runId) / $($Record.attempt)" } else { 'not assigned' }))
                $Item.Tag=$Record; [void]$ReleaseRequests.Items.Add($Item)
                if ($Record.requestId -eq $Selected) { $Item.Selected=$true }
            }
        } finally { $ReleaseRequests.EndUpdate() }
    }
    Update-LaneReleaseRequestSelection
}
