# A move snapshots its project and selection. Only the UI timer touches controls.
$script:MoveJob=$null
$script:MoveWorker={
    param($Request,$Progress)
    $ErrorActionPreference='Stop'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $Succeeded=@(); $Failed=@(); $DispatchError=''
    $Headers=@{Authorization="Bearer $($Request.Token)";Accept='application/vnd.github+json';'User-Agent'='Buildlane-Desktop'}
    foreach ($Number in $Request.Numbers) {
        $Progress.Message="Moving PR #$Number ($($Progress.Done + 1) of $($Request.Numbers.Count))..."
        try {
            $Body=@{milestone=$Request.Milestone} | ConvertTo-Json -Compress
            Invoke-RestMethod -Method PATCH -Uri "https://api.github.com/repos/$($Request.Repo)/issues/$Number" -Headers $Headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($Body)) -TimeoutSec 20 | Out-Null
            $Succeeded+=$Number
        } catch {
            $Failed+=$Number
            # Stop a rate-limited batch instead of issuing more writes while GitHub refuses them.
            $Code=0
            if ($_.Exception.Response) { $Code=[int]$_.Exception.Response.StatusCode }
            if ($Code -in @(403,429)) {
                $Failed+=@($Request.Numbers | Select-Object -Skip ($Progress.Done+1))
                $Progress.Message='GitHub refused further requests. Remaining items stay selected.'
                break
            }
        }
        $Progress.Done++
    }
    if ($Succeeded.Count) {
        $Progress.Message='Moves finished. Notifying the build lane...'
        try {
            $Body=@{ref=$Request.Branch;inputs=@{task='lane'}} | ConvertTo-Json -Compress
            Invoke-RestMethod -Method POST -Uri "https://api.github.com/repos/$($Request.Repo)/actions/workflows/$($Request.Workflow)/dispatches" -Headers $Headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($Body)) -TimeoutSec 20 | Out-Null
        } catch { $DispatchError='The lane could not be notified. The moves were saved; refresh before trying another action.' }
    }
    @{Succeeded=@($Succeeded);Failed=@($Failed);DispatchError=$DispatchError}
}
function Start-LaneMove([int[]]$Numbers,$Milestone,[string]$Target,[scriptblock]$Worker) {
    if ($script:MoveJob) { return }
    if (-not $Numbers.Count) { throw 'Tick the PRs to move first.' }
    if ($Config.setupRequired) { throw 'Save your project first.' }
    if (-not $Worker -and -not $script:Token) { throw 'Connect GitHub before moving PRs.' }
    if ($SmokeTest -and -not $Worker) { throw 'Live moves are disabled in smoke tests.' }
    Stop-LaneRefresh
    $script:DataRevision++
    $Request=@{Repo=$Repo;Branch=$Config.branch;Workflow=$Workflow;Token=$script:Token;Numbers=@($Numbers | Select-Object -Unique);Milestone=$Milestone}
    $Progress=[hashtable]::Synchronized(@{Done=0;Message='Starting move...'})
    $Shell=[PowerShell]::Create()
    try {
        if (-not $Worker) { $Worker=$script:MoveWorker }
        [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request).AddArgument($Progress)
        $Handle=$Shell.BeginInvoke()
        $script:MoveJob=@{Shell=$Shell;Handle=$Handle;Progress=$Progress;Target=$Target;Numbers=$Request.Numbers}
    } catch { $Shell.Dispose(); throw }
    $RelButtons.Enabled=$false; $MoveBar.Enabled=$false; $PrList.Enabled=$false; $ProjectRoot.Enabled=$false; $GhButton.Enabled=$false
    $MoveStatus.Text="Moving $($Request.Numbers.Count) PR(s) to $Target... You can keep browsing while this finishes."
    $MoveStatus.ForeColor=$Blue
}
function Complete-LaneMove {
    $Job=$script:MoveJob
    if (-not $Job) { return }
    if (-not $Job.Handle.IsCompleted) { $MoveStatus.Text=$Job.Progress.Message; return }
    try {
        $Results=@($Job.Shell.EndInvoke($Job.Handle))
        if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'Move worker did not return a result.' }
        $Result=$Results[-1]
    } catch { $Result=@{Succeeded=@();Failed=$Job.Numbers;DispatchError='The result could not be confirmed. Refresh before retrying.'} }
    finally { $Job.Shell.Dispose(); $script:MoveJob=$null }
    foreach ($Item in $PrList.Items) {
        if ($Item.Tag -in $Result.Succeeded) { $Item.Checked=$false; $Item.SubItems[2].Text=$Job.Target }
    }
    $RelButtons.Enabled=$true; $MoveBar.Enabled=$true; $PrList.Enabled=$true; $ProjectRoot.Enabled=$true; $GhButton.Enabled=$true
    $Failures=@($Result.Failed)
    $MoveStatus.Text="Moved $(@($Result.Succeeded).Count) of $($Job.Numbers.Count) PR(s) to $($Job.Target)."
    if ($Failures.Count) { $MoveStatus.Text+=" Not confirmed: $(($Failures | ForEach-Object { "#$_" }) -join ', '). These remain selected." }
    if ($Result.DispatchError) { $MoveStatus.Text+=" $($Result.DispatchError)" }
    $MoveStatus.ForeColor=if ($Failures.Count -or $Result.DispatchError) { $Orange } else { $Green }
    Start-LaneRefresh
}
