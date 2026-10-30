# Commands and machine queries never run on the Forms thread. The timers apply their snapshots.
$script:CommandJob=$null
$script:CommandState=$null
$script:HealthJob=$null
$script:NextControllerPoll=[datetime]::MinValue
$script:CommandWorker={
    param($Request)
    $ErrorActionPreference='Stop'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $Headers=@{Authorization="Bearer $($Request.Token)";Accept='application/vnd.github+json';'User-Agent'='Buildlane-Desktop'}
    $Args=@{Method='POST';Uri="https://api.github.com/repos/$($Request.Repo)$($Request.Path)";Headers=$Headers;TimeoutSec=20}
    if ($null -ne $Request.Body) { $Args.ContentType='application/json; charset=utf-8'; $Args.Body=[Text.Encoding]::UTF8.GetBytes(($Request.Body | ConvertTo-Json -Compress -Depth 5)) }
    Invoke-RestMethod @Args | Out-Null
    @{AcceptedAtUtc=[datetime]::UtcNow}
}
function Start-LaneCommand([string]$Kind='lane',[string]$Path,[hashtable]$Body,[scriptblock]$Worker) {
    if ($script:CommandJob -or $script:MoveJob -or $script:ActionJob -or $script:IntegrationJob -or $script:IntegrationPending) { return }
    if ($Kind -ne 'cancel' -and -not (Test-LaneLegacyOperationsAllowed)) { throw 'Use explicit integration checkpoints in the current mode.' }
    if (-not $Worker -and ($SmokeTest -or -not $script:Token)) { throw 'Connect GitHub before sending a lane request.' }
    Stop-LaneRefresh
    $script:DataRevision++
    $script:CommandState=@{Kind=$Kind;Phase='sending';RequestedAtUtc=[datetime]::UtcNow;BaselineRunId=$script:State.runId;BaselinePhase=$script:State.phase}
    $Request=@{Repo=$Repo;Token=$script:Token;Path=$Path;Body=$Body}
    $Shell=[PowerShell]::Create()
    try {
        if (-not $Worker) { $Worker=$script:CommandWorker }
        [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request)
        $script:CommandJob=@{Shell=$Shell;Handle=$Shell.BeginInvoke();Generation=$script:ProjectGeneration}
    } catch { $Shell.Dispose(); $script:CommandState.Phase='error'; $script:CommandState.Error=$_.Exception.Message; throw }
    $RelButtons.Enabled=$false; $MoveBar.Enabled=$false; $ProjectRoot.Enabled=$false
    Update-View
}
function Complete-LaneCommand {
    $Job=$script:CommandJob
    if (-not $Job -or -not $Job.Handle.IsCompleted) { return }
    try {
        $Results=@($Job.Shell.EndInvoke($Job.Handle))
        if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'The lane request could not be confirmed.' }
        $Result=$Results[-1]
    } catch { $Result=@{Error=$_.Exception.Message} }
    finally { $Job.Shell.Dispose(); $script:CommandJob=$null }
    if ($Job.Generation -ne $script:ProjectGeneration) { return }
    $RelButtons.Enabled=$true; $MoveBar.Enabled=$true; $ProjectRoot.Enabled=$true
    if ($Result.Error) { $script:CommandState.Phase='error'; $script:CommandState.Error=$Result.Error }
    else {
        $script:CommandState.Phase='accepted'
        $script:CommandState.AcceptedAtUtc=$Result.AcceptedAtUtc
        $script:NextControllerPoll=[datetime]::MinValue
    }
    Update-View
}
function Update-LaneCommandFeedback {
    $C=$script:CommandState
    if (-not $C) { return }
    $Word=if ($C.Kind -eq 'resume') { 'Resume' } elseif ($C.Kind -eq 'cancel') { 'Stop' } else { 'Lane check' }
    $Age=[math]::Max(0,[int]([datetime]::UtcNow-$C.RequestedAtUtc).TotalSeconds)
    if ($C.Phase -eq 'sending') { $PauseState.Text="$Word requested; sending to GitHub..."; $PauseState.ForeColor=$Blue; return }
    if ($C.Phase -eq 'error') { $PauseState.Text="$Word notification not confirmed. Check GitHub before retrying."; $PauseState.ForeColor=$Orange; return }
    $S=$script:State
    if ($S -and $S.updated) {
        $ReportAt=[DateTimeOffset]::MinValue; $StartedAt=[DateTimeOffset]::MinValue
        $ValidReport=[DateTimeOffset]::TryParse([string]$S.updated,[ref]$ReportAt)
        $ValidStart=[DateTimeOffset]::TryParse([string]$S.started,[ref]$StartedAt)
        $NewRun=$S.runId -and $S.runId -ne $C.BaselineRunId -and $ValidStart -and $StartedAt.UtcDateTime -ge $C.RequestedAtUtc.AddSeconds(-1)
        $Cancelled=$C.Kind -eq 'cancel' -and $S.runId -eq $C.BaselineRunId -and $S.done
        $Resumed=$S.runId -eq $C.BaselineRunId -and [int]$C.BaselinePhase -eq 0 -and [int]$S.phase -gt 0
        if ($ValidReport -and $ReportAt.UtcDateTime -ge $C.RequestedAtUtc -and ($NewRun -or $Resumed -or $Cancelled)) {
            $C.Phase='runner'; $C.RunnerReportedAtUtc=$ReportAt.UtcDateTime
            # The main status panel now carries the authoritative runner phase.
            return
        }
    }
    if ($C.Phase -eq 'runner') { return }
    if ($C.Controller) {
        $Run=$C.Controller
        $PauseState.Text=if ($Run.status -eq 'queued' -or $Run.status -eq 'pending') { "Latest lane controller queued (${Age}s since click)" }
            elseif ($Run.status -eq 'in_progress') { 'Latest lane controller running; waiting for runner report' }
            elseif ($Run.conclusion -eq 'success') { 'Lane check completed; waiting for runner status' }
            else { "Lane controller ended: $($Run.conclusion). Check its log." }
    } else { $PauseState.Text="$Word accepted; waiting for lane report (${Age}s)" }
    $PauseState.ForeColor=if ($C.ObservationError) { $Orange } else { $Blue }
}
function Start-LaneControllerRead {
    $C=$script:CommandState
    if (-not $C -or $C.Kind -eq 'cancel' -or $C.Phase -notin @('accepted','controller') -or $script:RefreshJob -or [datetime]::UtcNow -lt $script:NextControllerPoll) { return }
    if ($C.Controller -and $C.Controller.status -eq 'completed') { return }
    Start-LaneRefresh -Kind 'controller'
    if ($script:RefreshJob -and $script:RefreshJob.Kind -eq 'controller') { $script:NextControllerPoll=[datetime]::UtcNow.AddSeconds(5) }
}
function Start-LaneHealth([scriptblock]$Worker) {
    if ($script:HealthJob -or $Config.setupRequired -or ($SmokeTest -and -not $Worker)) { return }
    if ($script:HealthChecked -and $script:HealthProfile -eq $Profile -and ([datetime]::UtcNow-$script:HealthChecked).TotalSeconds -lt 30) { return }
    $Request=@{Drive=$Drive;ServiceFilter=$Config.serviceFilter;CachePort=$Config.cachePort}
    if (-not $Worker) { $Worker={
        param($Request)
        $Result=@{AtUtc=[datetime]::UtcNow;Health=@()}
        try { $D=Get-PSDrive $Request.Drive -ErrorAction Stop; $Result.FreeGB=[math]::Round($D.Free/1GB,1); $Result.TotalGB=[math]::Round(($D.Free+$D.Used)/1GB); $Result.UsedFraction=1-$D.Free/($D.Free+$D.Used) } catch { $Result.DiskError="Drive $($Request.Drive): unavailable" }
        $Result.Health=@(Get-Service 'actions.runner.*' -ErrorAction SilentlyContinue | Where-Object Name -like $Request.ServiceFilter | ForEach-Object { "$($_.DisplayName) runner $($_.Status.ToString().ToLower())" })
        if (-not $Result.Health.Count) { $Result.Health=@('No matching runner services found') }
        if ($Request.CachePort) { $Listening=Get-NetTCPConnection -LocalPort $Request.CachePort -State Listen -ErrorAction SilentlyContinue; $Result.Health+=if ($Listening) { 'Cache endpoint listening' } else { 'Cache endpoint not listening' } }
        $Result
    } }
    $Shell=[PowerShell]::Create()
    try { [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request); $script:HealthJob=@{Shell=$Shell;Handle=$Shell.BeginInvoke();Generation=$script:ProjectGeneration;Profile=$Profile} }
    catch { $Shell.Dispose(); throw }
}
function Complete-LaneHealth {
    $Job=$script:HealthJob
    if (-not $Job -or -not $Job.Handle.IsCompleted) { return }
    try { $Results=@($Job.Shell.EndInvoke($Job.Handle)); if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'Machine health could not be read.' }; $Result=$Results[-1] }
    catch { $Result=@{AtUtc=[datetime]::UtcNow;DiskError='Machine health unavailable';Health=@('Machine health unavailable')} }
    finally { $Job.Shell.Dispose(); $script:HealthJob=$null }
    if ($Job.Generation -ne $script:ProjectGeneration) { return }
    $script:CachedMachine=$Result; $script:HealthChecked=[datetime]::UtcNow; $script:HealthProfile=$Job.Profile
    Update-View
}
