# Live PR evidence is independent of a runner's historical queue.
$script:PullSnapshot=$null; $script:PullError=''; $script:PullIdentity=''; $script:NextPullRead=[datetime]::MinValue
function Get-LanePullBranch {
    # Read-only history can retain the known mode while mutation eligibility expires.
    if ($script:IntegrationSnapshot.Initialized -and $script:IntegrationSnapshot.State.integrationMode -eq 'MergeToStagingThenValidate') { return 'Test-Staging' }
    $Config.branch
}
function Initialize-LanePulls {
    $Identity="$Repo|$Profile|$(Get-LanePullBranch)"
    if ($Identity -ne $script:PullIdentity) {
        $script:PullIdentity=$Identity; $script:PullSnapshot=$null; $script:PullError=''; $script:NextPullRead=[datetime]::MinValue; $script:PullRowsKey=''; $script:PullActionMessage=''
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
function Test-LanePullReviewed($Pr) {
    if ($Pr.reviewDecision -eq 'CHANGES_REQUESTED') { return $false }
    if ($Pr.reviewDecision -eq 'APPROVED') { return $true }
    [bool]@($script:IntegrationSnapshot.State.reviews | Where-Object { $_.source -eq 'controller' -and $_.decision -eq 'approved' -and $_.prNumber -eq $Pr.number -and $_.head -eq $Pr.headRefOid -and $_.reviewer -and $_.reason }).Count
}
function Get-LanePullState($Pr) {
    $Checks=[string](@($Pr.commits.nodes)[0].commit.statusCheckRollup.state)
    $Review=switch ([string]$Pr.reviewDecision) { 'APPROVED' {'approved'} 'CHANGES_REQUESTED' {'changes requested'} 'REVIEW_REQUIRED' {'review required'} default {'review unconfirmed'} }
    $Reviewed=Test-LanePullReviewed $Pr
    if ($Reviewed -and $Pr.reviewDecision -ne 'APPROVED') { $Review='controller review approved' }
    $Action=if ($Pr.isDraft) { 'Draft' } elseif ($Pr.mergeable -eq 'CONFLICTING') { 'Resolve conflicts' }
        elseif ($Checks -in @('FAILURE','ERROR')) { 'Fix checks' } elseif ($Pr.reviewDecision -eq 'CHANGES_REQUESTED') { 'Address review' }
        elseif (-not $Reviewed) { 'Review required' } elseif ($Checks -ne 'SUCCESS') { 'Waiting for checks' }
        elseif ($Pr.mergeStateStatus -ne 'CLEAN') { 'Merge eligibility unconfirmed' } else { 'Ready for operator merge' }
    $Queued=@($script:IntegrationSnapshot.State.mergeQueue | Where-Object { $_.prNumber -eq $Pr.number -and $_.status -notin @('cancelled','closed') } | Select-Object -Last 1)
    if ($Queued.Count) { $Action=if ($Queued[0].head -ne $Pr.headRefOid) { 'Queue refers to an older head; review again' } else { "Merge queue: $($Queued[0].status) - $($Queued[0].reason)" } }
    "$Action | $Review | checks: $(if ($Checks) { $Checks.ToLowerInvariant() } else { 'unreported' }) | target: $($Pr.baseRefName)"
}
# One plain group per PR so the list answers "what is merging, what needs me, what is waiting". Glyph plus words, never colour alone.
$script:LaneGlyph=@{Play=[string][char]0x25B6;Warn=[string][char]0x26A0;Circle=[string][char]0x25CB;Tick=[string][char]0x2713;Dot=[string][char]0x00B7}
function Get-LanePullQueued($Pr) {
    @($script:IntegrationSnapshot.State.mergeQueue | Where-Object { $_.prNumber -eq $Pr.number -and $_.status -notin @('cancelled','closed','merged') } | Select-Object -Last 1)
}
function Get-LanePullRow($Pr) {
    $G=$script:LaneGlyph
    $Checks=[string](@($Pr.commits.nodes)[0].commit.statusCheckRollup.state)
    $Queued=Get-LanePullQueued $Pr
    if ($Queued.Count) {
        if ($Queued[0].head -ne $Pr.headRefOid) { return @{Rank=1;Group='Needs you';Text="$($G.Warn) Queued, but the PR changed: approve the new head";Color=$Orange} }
        $Short=switch ([string]$Queued[0].status) { 'queued' {'waiting for checks'} 'waiting-checks' {'waiting for checks'} 'waiting-conflict' {'blocked by a conflict'} 'needs-review' {'needs review again'} 'attention' {'needs attention'} default { [string]$Queued[0].status } }
        if ($Queued[0].status -in @('waiting-conflict','needs-review','attention')) { return @{Rank=1;Group='Needs you';Text="$($G.Warn) Queued, $Short";Color=$Orange} }
        return @{Rank=0;Group='Merging';Text="$($G.Play) Queued to merge, $Short";Color=$Blue}
    }
    if ($Pr.isDraft) { return @{Rank=3;Group='Waiting';Text="$($G.Circle) Draft";Color=$Grey} }
    if ($Pr.mergeable -eq 'CONFLICTING') { return @{Rank=1;Group='Needs you';Text="$($G.Warn) Conflicts: update the branch";Color=$Orange} }
    if ($Checks -in @('FAILURE','ERROR')) { return @{Rank=1;Group='Needs you';Text="$($G.Warn) Checks failed";Color=$Orange} }
    if ($Pr.reviewDecision -eq 'CHANGES_REQUESTED') { return @{Rank=1;Group='Needs you';Text="$($G.Warn) Changes requested";Color=$Orange} }
    if (-not (Test-LanePullReviewed $Pr)) { return @{Rank=3;Group='Waiting';Text="$($G.Circle) Needs review before it can merge";Color=$Ink} }
    if ($Checks -ne 'SUCCESS') { return @{Rank=2;Group='Ready';Text="$($G.Circle) Approved, checks running";Color=$Ink} }
    @{Rank=2;Group='Ready';Text="$($G.Tick) Ready: press Merge now or Merge when checks pass";Color=$Blue}
}
# One line saying what the AI merge fixer (a GitHub Actions workflow) is doing, so its work is visible without GitHub.
function Get-LaneAgentLine($Snapshot) {
    $A=if ($Snapshot) { $Snapshot.Agent }
    if (-not $A) { return '' }
    if ($A.Error) { return "AI merge fixer: status unavailable ($($A.Error))" }
    $Last=@($A.Runs)[0]
    if (-not $Last) { return 'AI merge fixer (Claude): set up, has not run yet. It runs when the integration branch moves.' }
    $When=$Last.createdAt; try { $When=([datetime]::Parse($Last.createdAt)).ToLocalTime().ToString('HH:mm') } catch {}
    if ($Last.status -ne 'completed') { return "AI merge fixer (Claude): $($script:LaneGlyph.Play) working now on stuck PRs (started $When). Open AI fixer log to watch." }
    $Outcome=switch ($Last.conclusion) { 'success' {'finished'} 'skipped' {'nothing to fix'} 'cancelled' {'cancelled'} default { "$($script:LaneGlyph.Warn) FAILED, open AI fixer log" } }
    "AI merge fixer (Claude): idle. Last run $When, $Outcome. Fixed PRs get a Claude comment and need a fresh review."
}
function Update-LanePullView($View) {
    Initialize-LanePulls
    if ($PullActions) { Update-LanePullActions }
    $Snapshot=$script:PullSnapshot
    if (-not $Config.integrationWorkflow -and -not $Snapshot -and -not $script:PullError) { return } # Legacy report until first independent observation.
    $View.Queue.Visible=$true
    $G=$script:LaneGlyph
    $Open=@(); $Merged=@()
    if ($Snapshot) {
        foreach ($Pr in $Snapshot.Pulls) { $R=Get-LanePullRow $Pr; $Open+=@(@{number=$Pr.number;title=$Pr.title;state=$R.Text;tip=(Get-LanePullState $Pr);rank=$R.Rank;group=$R.Group;color=$R.Color}) }
        foreach ($Pr in $Snapshot.Merged) {
            $When=[string]$Pr.mergedAt; try { $When=([datetime]::Parse($Pr.mergedAt)).ToLocalTime().ToString('ddd HH:mm') } catch {}
            $Merged+=@(@{number=$Pr.number;title=$Pr.title;state="$($G.Tick) Merged into $($Pr.baseRefName), $When";tip="Merged to $($Pr.baseRefName) at $($Pr.mergedAt)";rank=4;group='Merged';color=$Grey})
        }
    }
    $Count=@{}; foreach ($Name in @('Merging','Needs you','Ready','Waiting')) { $Count[$Name]=@($Open | Where-Object { $_.group -eq $Name }).Count }
    # The head is the at-a-glance answer: is merging on, are builds on, and how many PRs sit in each group.
    $Parts=@()
    if ($PullActions -and $Config.integrationWorkflow) {
        $ModeFresh=$script:IntegrationSnapshot -and -not $script:IntegrationError -and ([datetime]::UtcNow-$script:IntegrationSnapshot.AtUtc).TotalSeconds -le 60
        $Parts+=$(if (-not $ModeFresh) { 'merges: status unconfirmed' } elseif ($script:IntegrationSnapshot.State.integrationPaused -is [bool] -and $script:IntegrationSnapshot.State.integrationPaused -eq $false) { 'merges running' } else { 'merges paused' })
    }
    $Parts+=$(if (Test-Path $PauseFile) { 'builds paused' } else { 'builds on' })
    if ($Snapshot) {
        $Parts+="$($Count['Merging']) queued to merge"; $Parts+="$($Count['Needs you']) need you"; $Parts+="$($Count['Ready']) ready"; $Parts+="$($Count['Waiting']) waiting for review"
    } else { $Parts+='checking GitHub' }
    $View.QueueHead.Text="Pull requests: $($Parts -join "  $($G.Dot)  ")"
    $Fresh=$Snapshot -and -not $script:PullError -and ([datetime]::UtcNow-$Snapshot.AtUtc).TotalSeconds -le 90
    $View.QueueHint.Text=if ($script:PullError) { "GitHub unavailable: $($script:PullError) Last known results; merge eligibility unconfirmed." } elseif ($Snapshot) { "$(if (-not $Fresh) { 'Last known' } else { 'Updated' }) $($Snapshot.AtUtc.ToLocalTime().ToString('HH:mm:ss')). Queued PRs merge by themselves once checks pass, while merges are running. Pausing builds does not pause merges. Double-click a PR to open it; click a header to sort." } else { 'Current PRs come from GitHub, independently of completed build reports. Connect GitHub if needed.' }
    $AgentLine=Get-LaneAgentLine $Snapshot
    if ($AgentLine) { $View.QueueHint.Text="$AgentLine`n$($View.QueueHint.Text)" }
    if ($PullAgentLog) { $Show=[bool]($Snapshot -and $Snapshot.Agent); if ($PullAgentLog.Visible -ne $Show) { $PullAgentLog.Visible=$Show } }
    if ($PullActions) {
        $Message=if ($script:IntegrationPending -and $script:IntegrationPending.Operation -in @('stage','queue-merge','cancel-merge','pause-integration','resume-integration')) { 'Waiting for the controller result. This is not a merge confirmation; no automatic retry.' } elseif ($script:PullActionMessage) { $script:PullActionMessage } elseif ($script:IntegrationError) { $script:IntegrationError } elseif (Get-LaneSelectedPull) { Get-LanePullActionReason (Get-LaneSelectedPull) } else { 'Select a PR to see what it is waiting for and which buttons apply.' }
        $View.QueueHint.Text="$Message`n$($View.QueueHint.Text)"
    }
    if ($PullShowMerged) {
        $PullShowMerged.Text="Show recent merges ($($Merged.Count))"
        if (-not $PullShowMerged.Visible) { $PullShowMerged.Visible=$true }
    }
    $Rows=@($Open | Sort-Object @{Expression={$_.rank}}, @{Expression={$_.number}})
    if (-not $PullShowMerged -or $PullShowMerged.Checked -or -not $Config.integrationWorkflow) { $Rows+=$Merged }
    $Key=$Rows | ConvertTo-Json -Depth 4 -Compress
    if ($Key -eq $script:PullRowsKey) { return }
    $script:PullRowsKey=$Key
    $Selected=if ($Queue.SelectedItems.Count) { $Queue.SelectedItems[0].Tag }
    $Queue.BeginUpdate()
    try {
        $Queue.Items.Clear()
        foreach ($Row in $Rows) {
            $Item=New-Object Windows.Forms.ListViewItem("#$($Row.number)"); [void]$Item.SubItems.Add([string]$Row.title); [void]$Item.SubItems.Add([string]$Row.state); $Item.Tag=$Row.number; $Item.ToolTipText=$Row.tip
            $Item.ForeColor=$Row.color; $Item.UseItemStyleForSubItems=$true
            [void]$Queue.Items.Add($Item); if ($Selected -eq $Row.number) { $Item.Selected=$true }
        }
        if (-not $Rows.Count) { [void]$Queue.Items.Add((New-Object Windows.Forms.ListViewItem($(if ($Snapshot) { 'No open PRs.' } else { 'Waiting for GitHub...' })))) }
        if ($Queue.ListViewItemSorter) { $Queue.Sort() }
    } finally { $Queue.EndUpdate() }
}
function Update-LaneReleaseHeading($S,$View) {
    $Run=if ($script:DisplayedActivity) { [string]$script:DisplayedActivity.Run.id } else { [string]$S.runId }
    $Attempt=if ($script:DisplayedActivity) { [string]$script:DisplayedActivity.Run.run_attempt } else { [string]$S.runAttempt }
    if ($Run -notmatch '^[1-9]\d*$' -or $Attempt -notmatch '^[1-9]\d*$') { return }
    $Requests=@($script:IntegrationSnapshot.State.requests | Where-Object { $_.version -and [string]$_.worker.runId -eq $Run -and [string]$_.worker.attempt -eq $Attempt })
    if ($Requests.Count -ne 1) { return }
    $Outcome=if ($View.JobLabel.Text) { $View.JobLabel.Text } else { switch ([string]$S.done) { 'success' {'passed'} 'failure' {'FAILED'} 'cancelled' {'cancelled'} default { [string]$S.done } } }
    $View.Title.Text="Release $($Requests[0].version): $Outcome"
}
