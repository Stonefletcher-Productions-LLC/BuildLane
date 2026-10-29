# One background read at a time; results are applied only on the UI thread.
$script:RefreshJob = $null
$script:RetiredJobs = New-Object Collections.ArrayList
$script:ProjectGeneration = 0
$script:DataRevision = 0
$script:NextRefresh = [datetime]::MinValue
function Stop-LaneRefresh {
    $script:ProjectGeneration++
    $script:CommandState=$null
    $script:CachedMachine=$null; $script:HealthChecked=$null
    if ($script:HealthJob) {
        [void]$script:HealthJob.Shell.BeginStop($null,$null)
        [void]$script:RetiredJobs.Add($script:HealthJob)
        $script:HealthJob=$null
    }
    if ($script:RefreshJob) {
        [void]$script:RefreshJob.Shell.BeginStop($null, $null)
        [void]$script:RetiredJobs.Add($script:RefreshJob)
        $script:RefreshJob = $null
    }
}
function Start-LaneRefresh([string]$Kind = 'releases', [scriptblock]$Worker) {
    if ($script:MoveJob -or $script:CommandJob -or $script:ActionJob -or $script:PlanJob) { return }
    if ($Config.setupRequired -and -not $Worker) { return }
    if ($script:RefreshJob -or ($SmokeTest -and -not $Worker)) { return }
    if ($Kind -notin @('controller','history') -and [datetime]::UtcNow -lt $script:NextRefresh) {
        $RelMsg.Text = "Retry available at $($script:NextRefresh.ToLocalTime().ToString('HH:mm:ss')). Showing previous results."
        return
    }
    $Shell = [PowerShell]::Create()
    if ($Worker) { [void]$Shell.AddScript($Worker.ToString()) }
    else {
        [void]$Shell.AddScript([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'LaneGitHub.ps1')))
        [void]$Shell.AddArgument($Repo).AddArgument($Config.branch).AddArgument($TokenFile).AddArgument($script:Token).AddArgument($Kind).AddArgument($Workflow).AddArgument($(if ($script:CommandState) { $script:CommandState.RequestedAtUtc } else { [datetime]::MinValue })).AddArgument([string]$script:State.runId)
    }
    $script:RefreshJob = @{ Shell=$Shell; Handle=$Shell.BeginInvoke(); Kind=$Kind; Generation=$script:ProjectGeneration; Revision=$script:DataRevision; Command=$script:CommandState; RunId=$script:State.runId; What=$script:State.what; Report="$($script:State.runId)|$($script:State.started)|$($script:State.done)" }
    if ($Kind -eq 'releases') {
        $RelMsg.Text = 'Refreshing GitHub...'; $RelMsg.ForeColor = $Grey
        $RelButtons.Enabled = $false; $MoveBar.Enabled = $false
    }
}
function Complete-LaneRefresh {
    foreach ($Old in @($script:RetiredJobs)) {
        if ($Old.Handle.IsCompleted) { $Old.Shell.Dispose(); [void]$script:RetiredJobs.Remove($Old) }
    }
    $Job = $script:RefreshJob
    if (-not $Job -or -not $Job.Handle.IsCompleted) { return }
    $script:RefreshJob = $null
    try {
        $Results = @($Job.Shell.EndInvoke($Job.Handle))
        if ($Job.Shell.HadErrors) { throw $Job.Shell.Streams.Error[0] }
        if (-not $Results.Count) { throw 'GitHub returned no results.' }
        $Result = $Results[-1]
    } catch { $Result = @{ Error=$_.Exception.Message; RetrySeconds=60 } }
    finally { $Job.Shell.Dispose() }
    if ($Job.Generation -ne $script:ProjectGeneration) { return }
    if ($Job.Kind -eq 'history') { Set-LaneHistoryResult $Job $Result; return }
    if ($Job.Kind -eq 'controller') {
        if (-not [object]::ReferenceEquals($Job.Command,$script:CommandState)) { return }
        if ($script:CommandState) {
            $script:CommandState.ObservationError=$Result.Error
            if ($Result.Error) { $script:NextControllerPoll=[datetime]::UtcNow.AddSeconds([math]::Max(5,[int]$Result.RetrySeconds)) }
            elseif ($Result.Controller) { $script:CommandState.Controller=$Result.Controller; $script:CommandState.Phase='controller' }
            Update-View
        }
        return
    }
    $RelButtons.Enabled = -not $script:CommandJob; $MoveBar.Enabled = -not $script:CommandJob
    if ($Job.Revision -ne $script:DataRevision) { Start-LaneRefresh; return }
    if ($Result.Error) {
        $script:NextRefresh = [datetime]::UtcNow.AddSeconds([math]::Max(1, [int]$Result.RetrySeconds))
        $RelMsg.Text = "$($Result.Error) Previous results retained. Retry after $($script:NextRefresh.ToLocalTime().ToString('HH:mm:ss'))."
        $RelMsg.ForeColor = $Orange
        return
    }
    if ($Result.Token) { $script:Token = $Result.Token; $script:TokenChecked = $true; $GhButton.Text = 'GitHub connected' }
    if ($Job.Kind -eq 'auth') { if ($Tabs.SelectedTab -eq $RelTab) { Start-LaneRefresh }; return }
    Set-ReleaseView $Result
}
