# Release writes and credential validation share one serialized background operation.
$script:ActionJob=$null
$script:ActionWorker={
    param($Request,$Progress)
    $ErrorActionPreference='Stop'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $Headers=@{Authorization="Bearer $($Request.Token)";Accept='application/vnd.github+json';'User-Agent'='Buildlane-Desktop'}
    function Call([string]$Method,[string]$Path,$Body=$null) {
        $Args=@{Method=$Method;Uri="https://api.github.com/repos/$($Request.Repo)$Path";Headers=$Headers;TimeoutSec=20}
        if ($null -ne $Body) { $Args.ContentType='application/json; charset=utf-8'; $Args.Body=[Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 5 -Compress)) }
        Invoke-RestMethod @Args
    }
    $Result=@{Operation=$Request.Operation;Moved=@();Saved=$false;Notified=$false;Stage='read'}
    try {
        if ($Request.Operation -eq 'connect') { Call GET '' | Out-Null; $Result.Connected=$true; return $Result }
        if ($Request.Operation -eq 'create') {
            $Progress.Message='Creating release...'; $Result.Stage='create'
            $Created=Call POST '/milestones' @{title=$Request.Title;description='lane: open'}
            $Result.Saved=$true; $Result.Number=$Created.number; return $Result
        }
        $M=Call GET "/milestones/$($Request.Number)"
        if ($M.state -eq 'closed') { $Result.Error='This release is already closed. Refresh before making another change.'; return $Result }
        if ([string]$M.description -match '(?m)^lane:\s*packaging\s*$') { $Result.Error='Packaging is running. Wait for the release owner before changing this release.'; return $Result }
        if ($Request.Operation -eq 'prepareQueue') {
            $Loose=@()
            for ($Page=1; $Page -le 100; $Page++) {
                $Progress.Message="Reading ready pull requests (page $Page)..."
                $Batch=@(Call GET "/pulls?state=open&base=$([uri]::EscapeDataString($(if ($Request.IntegrationBranch) { $Request.IntegrationBranch } else { $Request.Branch })))&per_page=100&page=$Page" | ForEach-Object { $_ })
                $Loose+=@($Batch | Where-Object { -not $_.milestone -and -not $_.draft -and @($_.labels | Where-Object name -in @('ready','fix-main')).Count })
                if ($Batch.Count -lt 100) { break }
                if ($Page -eq 100) { throw 'Too many pull requests to queue safely.' }
            }
            $Result.Milestone=$M; $Result.Loose=$Loose; return $Result
        }
        foreach ($Number in $Request.Numbers) {
            $Progress.Message="Moving PR #$Number before queuing..."; $Result.Stage="move PR #$Number"
            $Pr=Call GET "/pulls/$Number"
            if ($Pr.state -ne 'open' -or $Pr.draft -or $Pr.milestone -or -not @($Pr.labels | Where-Object name -in @('ready','fix-main')).Count) {
                $Result.Error="PR #$Number changed after queue preparation. Remaining moves and queuing were stopped. Refresh before retrying."
                return $Result
            }
            Call PATCH "/issues/$Number" @{milestone=$Request.Number} | Out-Null
            $Result.Moved+=@($Number)
        }
        $Progress.Message='Saving release state...'; $Result.Stage='milestone'
        if ($Request.Operation -eq 'close') { Call PATCH "/milestones/$($Request.Number)" @{state='closed'} | Out-Null }
        else {
            # Re-read after moves so a changed packaging state or description is not silently overwritten.
            $M=Call GET "/milestones/$($Request.Number)"
            if ($M.state -eq 'closed' -or [string]$M.description -match '(?m)^lane:\s*packaging\s*$') { $Result.Error='The release changed while preparing this action. Refresh and check its run.'; return $Result }
            $State=if ($Request.Operation -eq 'queue') { 'queued' } else { 'open' }
            $Rest=([string]$M.description -replace '(?m)^lane:.*$','').Trim()
            Call PATCH "/milestones/$($Request.Number)" @{description="lane: $State$(if ($Rest) { "`n`n$Rest" })"} | Out-Null
        }
        $Result.Saved=$true; $Result.Stage='dispatch'; $Progress.Message='Release saved. Notifying the lane...'
        Call POST "/actions/workflows/$($Request.Workflow)/dispatches" @{ref=$Request.Branch;inputs=@{task='lane'}} | Out-Null
        $Result.Notified=$true
    } catch { $Result.Error="The $($Result.Stage) response could not be confirmed. Refresh and check GitHub before retrying." }
    $Result
}
function Start-LaneAction([ValidateSet('create','prepareQueue','queue','unqueue','close','connect')][string]$Operation,$Milestone,[Alias('Title')][string]$ReleaseTitle,[int[]]$Numbers=@(),[string]$Token,[scriptblock]$Worker,$Context) {
    if ($script:ActionJob -or $script:MoveJob -or $script:CommandJob -or ($script:IntegrationJob -and $script:IntegrationJob.Kind -eq 'request') -or $script:IntegrationPending) { return }
    if ($Operation -ne 'connect' -and -not (Test-LaneLegacyOperationsAllowed)) { throw 'Legacy release mutations are unavailable in the current integration mode. Use authoritative integration controls.' }
    if ($Config.setupRequired) { throw 'Save your project first.' }
    if (-not $Token) { $Token=$script:Token }
    if (-not $Worker -and ($SmokeTest -or -not $Token)) { throw 'Connect GitHub before changing releases.' }
    Stop-LaneRefresh; $script:DataRevision++
    $Request=@{Operation=$Operation;Repo=$Repo;Branch=$Config.branch;IntegrationBranch=(Get-LaneIntegrationBranch);Workflow=$Workflow;Token=$Token;Number=$Milestone.number;Title=$ReleaseTitle;Numbers=@($Numbers)}
    $Progress=[hashtable]::Synchronized(@{Message='Contacting GitHub...'})
    $Shell=[PowerShell]::Create()
    try {
        if (-not $Worker) { $Worker=$script:ActionWorker }
        [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request).AddArgument($Progress)
        $script:ActionJob=@{Shell=$Shell;Handle=$Shell.BeginInvoke();Generation=$script:ProjectGeneration;Request=$Request;Progress=$Progress;Context=$Context}
    } catch { $Shell.Dispose(); throw }
    $RelButtons.Enabled=$false; $MoveBar.Enabled=$false; $ProjectRoot.Enabled=$false
    $MoveStatus.Text='Contacting GitHub... You can keep browsing.'; $MoveStatus.ForeColor=$Blue
    Update-View
}
function Complete-LaneAction {
    $Job=$script:ActionJob
    if (-not $Job) { return }
    if (-not $Job.Handle.IsCompleted) { $MoveStatus.Text=$Job.Progress.Message; return }
    try {
        $Results=@($Job.Shell.EndInvoke($Job.Handle))
        if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'No confirmed response.' }
        $Result=$Results[-1]
    } catch { $Result=@{Error='The result could not be confirmed. Refresh and check GitHub before retrying.'} }
    finally { $Job.Shell.Dispose(); $script:ActionJob=$null }
    if ($Job.Generation -ne $script:ProjectGeneration) { return }
    $RelButtons.Enabled=$true; $MoveBar.Enabled=$true; $ProjectRoot.Enabled=$true
    $MoveStatus.Text=if ($Result.Error) { [string]$Result.Error } elseif ($Job.Request.Operation -eq 'connect') { 'GitHub connection verified.' } else { 'Release change saved.' }
    if ($Result.Error -and @($Result.Moved).Count) { $MoveStatus.Text+=" Confirmed PR moves: $(@($Result.Moved) -join ', ')." }
    if ($Result.Error -and $Result.Saved) { $MoveStatus.Text+=' Release state saved; lane notification is not confirmed.' }
    $MoveStatus.ForeColor=if ($Result.Error) { $Orange } else { $Green }
    if ($Job.Request.Operation -eq 'connect') {
        $C=$Job.Context
        if ($Result.Connected) {
            try {
                [void][IO.Directory]::CreateDirectory((Split-Path $TokenFile))
                $Protected=ConvertTo-SecureString $Job.Request.Token -AsPlainText -Force | ConvertFrom-SecureString
                [IO.File]::WriteAllText($TokenFile,$Protected)
                $script:Token=$Job.Request.Token; $script:TokenChecked=$true
                if ($C -and -not $C.Dialog.IsDisposed) { $C.Dialog.Close() }
            } catch { $MoveStatus.Text='Connection verified, but the token could not be saved.'; $MoveStatus.ForeColor=$Orange }
        }
        if ($C -and -not $C.Dialog.IsDisposed) { $C.Ok.Enabled=$true; $C.Forget.Enabled=$true; $C.Box.Enabled=$true; $C.Label.Text=$MoveStatus.Text }
    } elseif (-not $Result.Error -and $Job.Request.Operation -eq 'prepareQueue') { Confirm-LaneQueue $Result }
    if ($Result.Notified) {
        $script:CommandState=@{Kind='lane';Phase='accepted';RequestedAtUtc=[datetime]::UtcNow;BaselineRunId=$script:State.runId;BaselinePhase=$script:State.phase}
        $MoveStatus.Text+=' Lane notification accepted; runner progress is reported separately.'
    }
    Update-View
    if (-not $script:ActionJob) { Start-LaneRefresh }
}
function Confirm-LaneQueue($Prepared) {
    $M=$Prepared.Milestone; $Numbers=@()
    if (@($Prepared.Loose).Count) {
        $L=($Prepared.Loose | ForEach-Object { "#$($_.number) $($_.title)" }) -join "`n"
        $A=[Windows.Forms.MessageBox]::Show("These ready PRs are in no release yet:`n`n$L`n`nPut them in $($M.title) too?",'Queue release build','YesNoCancel','Question')
        if ($A -eq 'Cancel') { $MoveStatus.Text='Queue cancelled. No changes were sent.'; return }
        if ($A -eq 'Yes') { $Numbers=@($Prepared.Loose | ForEach-Object number) }
    }
    $Q=[Windows.Forms.MessageBox]::Show("Queue the $($M.title) release build?`n`nThe lane prioritizes this release, may stop a batch building other changes, then runs full tests and packaging. Only ready PRs are included.",'Queue release build','OKCancel','Question')
    if ($Q -ne 'OK') { $MoveStatus.Text='Queue cancelled. No changes were sent.'; return }
    Start-LaneAction queue -Milestone $M -Numbers $Numbers
}
