$script:HistoryIdentity=''
$script:HistoryRecords=@()
$script:LiveHistory=$null
$script:NextHistory=[datetime]::MinValue
$script:HistorySettledReport=''
$HistoryTab=New-Object Windows.Forms.TabPage('Timing history'); $HistoryTab.BackColor=$Paper
[void]$Tabs.TabPages.Add($HistoryTab)
$HistoryRoot=New-Object Windows.Forms.TableLayoutPanel
$HistoryRoot.Dock='Fill'; $HistoryRoot.Padding=New-Object Windows.Forms.Padding(24); $HistoryRoot.ColumnCount=1; $HistoryRoot.RowCount=4
[void]$HistoryRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle('AutoSize')))
[void]$HistoryRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle('AutoSize')))
[void]$HistoryRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle('Percent',40)))
[void]$HistoryRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle('Percent',60)))
$HistoryTab.Controls.Add($HistoryRoot)
$HistoryMessage=New-Label 'Timings come from GitHub for reported run IDs. Connect GitHub to load them.' $FontSmall $Grey
$HistoryMessage.Dock='Top'; [void]$HistoryRoot.Controls.Add($HistoryMessage,0,0)
$HistoryButtons=New-Object Windows.Forms.FlowLayoutPanel; $HistoryButtons.AutoSize=$true; $HistoryButtons.Dock='Top'
[void]$HistoryRoot.Controls.Add($HistoryButtons,0,1)
[void](New-Button 'Refresh timings' { $script:HistorySettledReport=''; Start-LaneHistory } $HistoryButtons)
$HistoryList=New-Object Windows.Forms.ListView
$HistoryList.Dock='Fill'; $HistoryList.View='Details'; $HistoryList.FullRowSelect=$true; $HistoryList.Font=$FontSmall
[void]$HistoryList.Columns.Add('Run / attempt',170); [void]$HistoryList.Columns.Add('Result',130); [void]$HistoryList.Columns.Add('Reported build',300)
[void]$HistoryRoot.Controls.Add($HistoryList,0,2)
$HistoryDetails=New-Object Windows.Forms.TextBox
$HistoryDetails.Dock='Fill'; $HistoryDetails.Multiline=$true; $HistoryDetails.ReadOnly=$true; $HistoryDetails.ScrollBars='Vertical'; $HistoryDetails.Font=$FontSmall
[void]$HistoryRoot.Controls.Add($HistoryDetails,0,3)
function Initialize-LaneHistory {
    $Identity="$Repo|$Profile"
    if ($script:HistoryIdentity -eq $Identity) { return }
    $script:HistoryIdentity=$Identity; $script:HistoryRecords=@(); $script:LiveHistory=$null
    $script:NextHistory=[datetime]::MinValue; $script:HistorySettledReport=''
    $Hash=[Security.Cryptography.SHA256]::Create()
    try { $Key=([BitConverter]::ToString($Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Identity)))).Replace('-','').ToLowerInvariant() }
    finally { $Hash.Dispose() }
    $script:HistoryFile=Join-Path $ProfileDirectory "timings-$Key.json"
    if (Test-Path -LiteralPath $script:HistoryFile) {
        try {
            $Cache=Get-Content -LiteralPath $script:HistoryFile -Raw | ConvertFrom-Json
            if ($Cache.identity -ne $Identity) { throw 'Wrong project history.' }
            $script:HistoryRecords=@($Cache.records | Where-Object { $_.repo -eq $Repo -and $_.status -eq 'completed' } | Select-Object -First 20)
        } catch { $HistoryMessage.Text='Saved timing history could not be read. Fresh GitHub timings can still be loaded.' }
    }
    $HistoryList.Items.Clear(); $HistoryDetails.Text='Select a run to see its exact job and step timings.'
    Set-LaneHistoryView
}
function Format-LaneSeconds($Seconds) {
    $Number=0.0
    if ($null -eq $Seconds -or -not [double]::TryParse([string]$Seconds,[ref]$Number) -or [double]::IsNaN($Number) -or [double]::IsInfinity($Number) -or $Number -lt 0) { return 'not reported' }
    '{0:n1}s' -f $Number
}
function Show-LaneHistoryDetails {
    if (-not $HistoryList.SelectedItems.Count) { return }
    $R=$HistoryList.SelectedItems[0].Tag
    $Lines=@("Run #$($R.id), attempt $($R.attempt): $($R.conclusion)$($R.status -replace '^completed$','')", "Source: $($R.sha)", "Before first job: $(Format-LaneSeconds $R.beforeJobsSeconds) (from $($R.beforeJobsBasis); includes scheduling/preparation)", "Job span: $(Format-LaneSeconds $R.jobSpanSeconds) (parallel jobs overlap)", 'Job / step | result | duration')
    foreach ($J in $R.jobs) {
        $Lines+="$($J.name) | $($J.result) | $(Format-LaneSeconds $J.seconds)"
        foreach ($S in $J.steps) { $Lines+="    $($S.name) | $($S.result) | $(Format-LaneSeconds $S.seconds)" }
    }
    $Lines+='Skipped steps have no execution duration. Timings do not certify other runs or packaging.'
    $HistoryDetails.Text=$Lines -join "`r`n"
}
$HistoryList.Add_SelectedIndexChanged({ Show-LaneHistoryDetails })
function Set-LaneHistoryView {
    $Selected=if ($HistoryList.SelectedItems.Count) { "$($HistoryList.SelectedItems[0].Tag.id):$($HistoryList.SelectedItems[0].Tag.attempt)" } else { '' }
    $Rows=@(if ($script:LiveHistory) { $script:LiveHistory })+@($script:HistoryRecords | Where-Object { -not $script:LiveHistory -or $_.id -ne $script:LiveHistory.id -or $_.attempt -ne $script:LiveHistory.attempt })
    $HistoryList.BeginUpdate(); $HistoryList.Items.Clear()
    foreach ($R in $Rows) {
        $Item=New-Object Windows.Forms.ListViewItem("#$($R.id) / $($R.attempt)")
        [void]$Item.SubItems.Add($(if ($R.status -eq 'completed') { [string]$R.conclusion } else { [string]$R.status }))
        [void]$Item.SubItems.Add([string]$R.what); $Item.Tag=$R
        [void]$HistoryList.Items.Add($Item)
        if ("$($R.id):$($R.attempt)" -eq $Selected) { $Item.Selected=$true }
    }
    $HistoryList.EndUpdate()
    if (-not $HistoryList.SelectedItems.Count -and $HistoryList.Items.Count) { $HistoryList.Items[0].Selected=$true }
    Show-LaneHistoryDetails
}
function Start-LaneHistory([scriptblock]$Worker) {
    Initialize-LaneHistory
    $S=$script:State
    if (-not $S -or [string]$S.runId -notmatch '^\d+$' -or [datetime]::UtcNow -lt $script:NextHistory) { return }
    $Report="$($S.runId)|$($S.started)|$($S.done)"
    if (-not $Worker -and $Tabs.SelectedTab -ne $HistoryTab -and (-not $S.done -or $script:HistorySettledReport -eq $Report)) { return }
    Start-LaneRefresh -Kind history -Worker $Worker
    if ($script:RefreshJob -and $script:RefreshJob.Kind -eq 'history') { $script:NextHistory=[datetime]::UtcNow.AddSeconds(30) }
}
function Set-LaneHistoryResult($Job,$Result) {
    if ($Result.Error) {
        $HistoryMessage.Text='Timing refresh failed. Previous results retained.'
        $script:NextHistory=[datetime]::UtcNow.AddSeconds([math]::Max(30,[int]$Result.RetrySeconds)); return
    }
    $R=$Result.History
    if (-not $R -or [string]$R.id -ne [string]$Job.RunId -or $R.repo -ne $Repo -or -not $R.attempt) { $HistoryMessage.Text='Timing response identity did not match the requested run. Previous results retained.'; return }
    $R.what=$Job.What
    if ($R.status -eq 'completed') {
        $script:HistoryRecords=@($R)+@($script:HistoryRecords | Where-Object { $_.id -ne $R.id -or $_.attempt -ne $R.attempt } | Select-Object -First 19)
        try {
            [void][IO.Directory]::CreateDirectory($ProfileDirectory)
            $Temp=$script:HistoryFile+'.tmp'
            [IO.File]::WriteAllText($Temp,(@{identity=$script:HistoryIdentity;records=@($script:HistoryRecords)} | ConvertTo-Json -Depth 10))
            Move-Item -LiteralPath $Temp -Destination $script:HistoryFile -Force
        } catch { $HistoryMessage.Text='Timings loaded, but local history could not be saved.'; Set-LaneHistoryView; return }
        $script:HistorySettledReport=$Job.Report
    }
    if ([string]$script:State.runId -eq [string]$R.id) { $script:LiveHistory=$R }
    $HistoryMessage.Text='GitHub job and step timings. Last 20 completed attempts retained per project; failures and cancellations stay visible.'
    Set-LaneHistoryView
}
function Get-LaneWaitingText($S,[bool]$Paused) {
    if ($Paused) { return 'Waiting: builds are paused locally. Resume allows new work.' }
    if (-not $S) { return 'Waiting: no runner report has arrived. Check the configured status path.' }
    if ($S.done) { return '' }
    if ($S.waitingReason) {
        $Text="Waiting: $($S.waitingReason)"
        if ([string]$S.blockingRunId -match '^\d+$') { $Text+=" (blocking run #$($S.blockingRunId))" }
        $Since=[DateTimeOffset]::MinValue
        if ([DateTimeOffset]::TryParse([string]$S.waitingSince,[ref]$Since) -and $Since.UtcDateTime -le [datetime]::UtcNow) { $Text+=" | $(Format-LaneSeconds ([datetime]::UtcNow-$Since.UtcDateTime).TotalSeconds) reported wait" }
        return $Text
    }
    if ([int]$S.phase -eq 0) {
        if ($S.detail) { return "Waiting / preparation: $($S.detail)" }
        if ($S.step) { return "Waiting / preparation: $($S.step). The runner has not reported a more specific reason." }
        return 'Waiting: the runner has not reported a reason yet. Open its live log for details.'
    }
    return ''
}
