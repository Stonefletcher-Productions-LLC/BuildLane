# One background read at a time; results are applied only on the UI thread.
$script:RefreshJob = $null
$script:RetiredJobs = New-Object Collections.ArrayList
$script:ProjectGeneration = 0
$script:DataRevision = 0
$script:NextRefresh = [datetime]::MinValue
function Stop-LaneRefresh {
    $script:ProjectGeneration++
    if ($script:RefreshJob) {
        [void]$script:RefreshJob.Shell.BeginStop($null, $null)
        [void]$script:RetiredJobs.Add($script:RefreshJob)
        $script:RefreshJob = $null
    }
}
function Start-LaneRefresh([string]$Kind = 'releases', [scriptblock]$Worker) {
    if ($Config.setupRequired -and -not $Worker) { return }
    if ($script:RefreshJob -or ($SmokeTest -and -not $Worker)) { return }
    if ([datetime]::UtcNow -lt $script:NextRefresh) {
        $RelMsg.Text = "Retry available at $($script:NextRefresh.ToLocalTime().ToString('HH:mm:ss')). Showing previous results."
        return
    }
    $Shell = [PowerShell]::Create()
    if ($Worker) { [void]$Shell.AddScript($Worker.ToString()) }
    else {
        [void]$Shell.AddScript([IO.File]::ReadAllText((Join-Path $PSScriptRoot 'LaneGitHub.ps1')))
        [void]$Shell.AddArgument($Repo).AddArgument($Config.branch).AddArgument($TokenFile).AddArgument($script:Token).AddArgument($Kind)
    }
    $script:RefreshJob = @{ Shell=$Shell; Handle=$Shell.BeginInvoke(); Kind=$Kind; Generation=$script:ProjectGeneration; Revision=$script:DataRevision }
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
    $RelButtons.Enabled = $true; $MoveBar.Enabled = $true
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
