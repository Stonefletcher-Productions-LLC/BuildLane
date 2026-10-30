# Live PR evidence is independent of a runner's historical queue.
$script:PullSnapshot=$null; $script:PullError=''; $script:PullIdentity=''; $script:NextPullRead=[datetime]::MinValue
function Initialize-LanePulls {
    $Identity="$Repo|$Profile|$(Get-LaneIntegrationBranch)"
    if ($Identity -ne $script:PullIdentity) {
        $script:PullIdentity=$Identity; $script:PullSnapshot=$null; $script:PullError=''; $script:NextPullRead=[datetime]::MinValue; $script:PullRowsKey=''
    }
}
function Start-LanePullRead {
    Initialize-LanePulls
    if ($SmokeTest -or -not $script:Token -or [datetime]::UtcNow -lt $script:NextPullRead) { return }
    Start-LaneRefresh 'pulls'
    if ($script:RefreshJob -and $script:RefreshJob.Kind -eq 'pulls') { $script:NextPullRead=[datetime]::UtcNow.AddSeconds(30) }
}
function Set-LanePullResult($Result) {
    if ($Result.Error) { $script:PullError=$Result.Error; $script:NextPullRead=[datetime]::UtcNow.AddSeconds([math]::Max(30,[int]$Result.RetrySeconds)) }
    elseif ($Result.Repo -eq $Repo) { $script:PullSnapshot=$Result; $script:PullError='' }
    Update-View
}
function Get-LanePullState($Pr) {
    $Checks=[string](@($Pr.commits.nodes)[0].commit.statusCheckRollup.state)
    $Review=switch ([string]$Pr.reviewDecision) { 'APPROVED' {'approved'} 'CHANGES_REQUESTED' {'changes requested'} 'REVIEW_REQUIRED' {'review required'} default {'review unconfirmed'} }
    $Action=if ($Pr.isDraft) { 'Draft' } elseif ($Pr.mergeable -eq 'CONFLICTING') { 'Resolve conflicts' }
        elseif ($Checks -in @('FAILURE','ERROR')) { 'Fix checks' } elseif ($Pr.reviewDecision -eq 'CHANGES_REQUESTED') { 'Address review' }
        elseif ($Pr.reviewDecision -ne 'APPROVED') { 'Review required' } elseif ($Checks -ne 'SUCCESS') { 'Waiting for checks' }
        elseif ($Pr.mergeStateStatus -ne 'CLEAN') { 'Merge eligibility unconfirmed' } else { 'Ready for operator merge' }
    "$Action | $Review | checks: $(if ($Checks) { $Checks.ToLowerInvariant() } else { 'unreported' }) | target: $($Pr.baseRefName)"
}
function Update-LanePullView($View) {
    Initialize-LanePulls
    $Snapshot=$script:PullSnapshot
    if (-not $Config.integrationWorkflow -and -not $Snapshot -and -not $script:PullError) { return } # Legacy report until first independent observation.
    $View.Queue.Visible=$true
    $View.QueueHead.Text=if ($Snapshot) { "Open pull requests ($(@($Snapshot.Pulls).Count)) and recent merges ($(@($Snapshot.Merged).Count))" } else { 'Open pull requests - checking GitHub' }
    $Fresh=$Snapshot -and -not $script:PullError -and ([datetime]::UtcNow-$Snapshot.AtUtc).TotalSeconds -le 90
    $View.QueueHint.Text=if ($script:PullError) { "GitHub unavailable: $($script:PullError) Last known results; merge eligibility unconfirmed." } elseif ($Snapshot) { "$(if (-not $Fresh) { 'Last known' } else { 'Observed' }) $($Snapshot.AtUtc.ToLocalTime().ToString('HH:mm:ss')). Recent merges to $($Snapshot.Branch). Drag divider to resize; double-click to open. Ready does not mean queued for automatic merge." } else { 'Current PRs come from GitHub, independently of completed build reports. Connect GitHub if needed.' }
    $Rows=@()
    if ($Snapshot) {
        foreach ($Pr in $Snapshot.Pulls) { $Rows+=@(@{number=$Pr.number;title=$Pr.title;state=(Get-LanePullState $Pr);merged=$false}) }
        foreach ($Pr in $Snapshot.Merged) { $Rows+=@(@{number=$Pr.number;title=$Pr.title;state="Merged to $($Pr.baseRefName) at $($Pr.mergedAt)";merged=$true}) }
    }
    $Key=$Rows | ConvertTo-Json -Depth 4 -Compress
    if ($Key -eq $script:PullRowsKey) { return }
    $script:PullRowsKey=$Key
    $Selected=if ($Queue.SelectedItems.Count) { $Queue.SelectedItems[0].Tag }
    $Queue.BeginUpdate()
    try {
        $Queue.Items.Clear()
        foreach ($Row in $Rows) {
            $Item=New-Object Windows.Forms.ListViewItem("#$($Row.number)"); [void]$Item.SubItems.Add([string]$Row.title); [void]$Item.SubItems.Add([string]$Row.state); $Item.Tag=$Row.number; $Item.ToolTipText=$Row.state
            $Item.ForeColor=if ($Row.merged) { $Green } elseif ($Row.state -match '^Fix|^Resolve|^Address') { $Orange } else { $Ink }
            [void]$Queue.Items.Add($Item); if ($Selected -eq $Row.number) { $Item.Selected=$true }
        }
        if (-not $Rows.Count) { [void]$Queue.Items.Add((New-Object Windows.Forms.ListViewItem($(if ($Snapshot) { 'No open PRs or recent merges.' } else { 'Waiting for GitHub...' })))) }
    } finally { $Queue.EndUpdate() }
}
function Update-LaneReleaseHeading($S,$View) {
    $Run=if ($script:DisplayedActivity) { [string]$script:DisplayedActivity.Run.id } else { [string]$S.runId }
    $Attempt=if ($script:DisplayedActivity) { [string]$script:DisplayedActivity.Run.run_attempt } else { [string]$S.runAttempt }
    if ($Run -notmatch '^[1-9]\d*$' -or $Attempt -notmatch '^[1-9]\d*$') { return }
    $Requests=@($script:IntegrationSnapshot.State.requests | Where-Object { $_.version -and [string]$_.worker.runId -eq $Run -and [string]$_.worker.attempt -eq $Attempt })
    if ($Requests.Count -ne 1) { return }
    $View.Title.Text="Release $($Requests[0].version) - $($View.JobLabel.Text)"
}
