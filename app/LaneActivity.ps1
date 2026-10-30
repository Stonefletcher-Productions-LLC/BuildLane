# Observe actual configured jobs independently of local button clicks and status-file handoffs.
$script:ActivityJob=$null; $script:ActivitySnapshot=$null; $script:ActivityError=''
$script:ActivityIdentity=''; $script:NextActivityRead=[datetime]::MinValue
$script:ActivityWorker={
    param($Request)
    $ErrorActionPreference='Stop'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $Headers=@{Authorization="Bearer $($Request.Token)";Accept='application/vnd.github+json';'User-Agent'='Buildlane-Desktop'}
    function Read([string]$Path) { Invoke-RestMethod -Uri "https://api.github.com/repos/$($Request.Repo)$Path" -Headers $Headers -TimeoutSec 15 }
    try {
        $Runs=@()
        foreach ($Status in @('in_progress','queued','waiting')) {
            for ($Page=1;$Page -le 3;$Page++) {
                $Response=Read "/actions/runs?status=$Status&per_page=100&page=$Page"
                if ($null -eq $Response.workflow_runs) { throw 'Missing workflow results.' }
                $Batch=@($Response.workflow_runs)
                $Runs+=@($Batch | Where-Object { $Path=([string]$_.path -split '@',2)[0]; @($Request.Targets | Where-Object { $Path -eq ".github/workflows/$($_.workflow)" }).Count })
                if ($Batch.Count -lt 100) { break }
                if ($Page -eq 3) { throw 'Too many active workflows to classify safely.' }
            }
        }
        $Runs=@($Runs | Sort-Object @{Expression={[long]$_.id}} -Unique)
        if ($Runs.Count -gt 6) { throw 'Too many matching workflows to classify safely.' }
        $Candidates=@()
        foreach ($Run in $Runs) {
            if ([string]$Run.id -notmatch '^\d+$' -or [string]$Run.run_attempt -notmatch '^[1-9]\d*$') { throw 'Missing run identity.' }
            $Path=([string]$Run.path -split '@',2)[0]
            $Names=@($Request.Targets | Where-Object { $Path -eq ".github/workflows/$($_.workflow)" } | ForEach-Object job)
            $Jobs=@()
            for ($Page=1;$Page -le 2;$Page++) {
                $Response=Read "/actions/runs/$($Run.id)/attempts/$($Run.run_attempt)/jobs?per_page=100&page=$Page"
                if ($null -eq $Response.jobs) { throw 'Missing job results.' }
                $Batch=@($Response.jobs)
                if (@($Batch | Where-Object { [string]$_.run_id -ne [string]$Run.id }).Count) { throw 'Job identity mismatch.' }
                $Jobs+=@($Batch)
                if ($Batch.Count -lt 100) { break }
                if ($Page -eq 2) { throw 'Too many jobs to classify safely.' }
            }
            $Native=@($Jobs | Where-Object { $_.name -in $Names -and $_.status -in @('queued','in_progress','waiting','pending') -and -not $_.conclusion })
            if (-not $Native.Count) { continue } # Quick-check-only runs and completed/skipped native jobs are not active builds.
            $After=Read "/actions/runs/$($Run.id)"
            if ([string]$After.id -ne [string]$Run.id -or $After.run_attempt -ne $Run.run_attempt -or $After.head_sha -ne $Run.head_sha) { throw 'Run attempt changed while observing jobs.' }
            if ($After.status -notin @('queued','in_progress','waiting','pending','requested')) { continue }
            foreach ($Job in $Native) { $Candidates+=@(@{Run=$After;Job=$Job}) }
        }
        $Active=$Candidates | Sort-Object @{Expression={if ($_.Job.status -eq 'in_progress') { 0 } else { 1 }}},@{Expression={$_.Job.started_at};Descending=$true},@{Expression={[long]$_.Run.id};Descending=$true} | Select-Object -First 1
        @{AtUtc=[datetime]::UtcNow;Active=$Active;Count=$Candidates.Count;Repo=$Request.Repo}
    } catch {
        $Delay=60
        if ($_.Exception.Response) { $Retry=0; if ([int]::TryParse([string]$_.Exception.Response.Headers['Retry-After'],[ref]$Retry)) { $Delay=[math]::Max(60,$Retry) } }
        @{Error='Live job status could not be read. Showing the last known evidence.';RetrySeconds=$Delay}
    }
}
function Initialize-LaneActivity {
    $Identity="$Repo|$Profile|$($Config.activeJobs)"
    if ($Identity -ne $script:ActivityIdentity) {
        Stop-LaneActivity
        $script:ActivityIdentity=$Identity; $script:ActivitySnapshot=$null; $script:ActivityError=''; $script:NextActivityRead=[datetime]::MinValue
    }
}
function Start-LaneActivity([scriptblock]$Worker) {
    Initialize-LaneActivity
    if ($Config.setupRequired -or -not $Config.activeJobs -or $script:ActivityJob -or $script:RefreshJob -or $script:MoveJob -or $script:CommandJob -or $script:ActionJob -or $script:IntegrationJob -or [datetime]::UtcNow -lt $script:NextActivityRead) { return }
    if (-not $Worker -and ($SmokeTest -or -not $script:Token)) { return }
    $Request=@{Repo=$Repo;Token=$script:Token;Targets=@(Get-LaneActivityTargets $Config.activeJobs)}
    $Shell=[PowerShell]::Create()
    try {
        if (-not $Worker) { $Worker=$script:ActivityWorker }
        [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request)
        $script:ActivityJob=@{Shell=$Shell;Handle=$Shell.BeginInvoke();Generation=$script:ProjectGeneration;Identity=$script:ActivityIdentity;Repo=$Repo}
        $script:NextActivityRead=[datetime]::UtcNow.AddSeconds(15)
    } catch { $Shell.Dispose(); throw }
}
function Complete-LaneActivity {
    $Job=$script:ActivityJob
    if (-not $Job -or -not $Job.Handle.IsCompleted) { return }
    try { $Results=@($Job.Shell.EndInvoke($Job.Handle)); if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'No response' }; $Result=$Results[-1] }
    catch { $Result=@{Error='Live job status could not be read. Showing the last known evidence.';RetrySeconds=60} }
    finally { $Job.Shell.Dispose(); $script:ActivityJob=$null }
    if ($Job.Generation -ne $script:ProjectGeneration -or $Job.Identity -ne $script:ActivityIdentity -or $Job.Repo -ne $Repo) { return }
    if ($Result.Error) { $script:ActivityError=$Result.Error; $script:NextActivityRead=[datetime]::UtcNow.AddSeconds([math]::Max(15,[int]$Result.RetrySeconds)) }
    elseif ($Result.Repo -eq $Repo) { $script:ActivitySnapshot=$Result; $script:ActivityError='' }
    Update-View
}
function Stop-LaneActivity {
    if ($script:ActivityJob) { [void]$script:ActivityJob.Shell.BeginStop($null,$null); [void]$script:RetiredJobs.Add($script:ActivityJob); $script:ActivityJob=$null }
}
function Update-LaneActivityView($S,$View) {
    Initialize-LaneActivity
    $View.StepsHead.Visible=$true; $View.StepsBox.Visible=$true; $View.Queue.Visible=$true
    $View.QueueHint.Text='Drag the divider above to resize the queue. Double-click a PR to open it.'
    $View.PreviousResult.Text=''; $View.PreviousResult.Visible=$false; $View.ActivityNote.Text=''
    $script:DisplayedActivity=$null
    if (-not $Config.activeJobs) { return }
    $Snapshot=$script:ActivitySnapshot
    $Fresh=$Snapshot -and -not $script:ActivityError -and ([datetime]::UtcNow-$Snapshot.AtUtc).TotalSeconds -le 60
    $View.ActivityNote.ForeColor=if ($Fresh) { $Grey } else { $Orange }
    $View.ActivityNote.Text=if ($script:ActivityError) { $script:ActivityError } elseif (-not $Snapshot) { 'Checking live build jobs. The local report below may describe an earlier run.' } else { "GitHub jobs observed $($Snapshot.AtUtc.ToLocalTime().ToString('HH:mm:ss'))." }
    $Active=if ($Snapshot) { $Snapshot.Active }
    $ReportAt=[DateTimeOffset]::MinValue; $HasReport=$S -and [DateTimeOffset]::TryParse([string]$S.updated,[ref]$ReportAt)
    if ($Active -and $HasReport -and $ReportAt.UtcDateTime -gt $Snapshot.AtUtc -and ($S.done -or [string]$S.runId -ne [string]$Active.Run.id)) { $Active=$null }
    if ($Active) {
        $Started=[DateTimeOffset]::MinValue
        $HasStart=[DateTimeOffset]::TryParse([string]$Active.Job.started_at,[ref]$Started) -and $Started.Year -gt 2000
        $AttemptMatches=($null -eq $S.runAttempt -or [string]$S.runAttempt -eq [string]$Active.Run.run_attempt)
        $LocalCurrent=$HasReport -and -not $S.done -and [string]$S.runId -eq [string]$Active.Run.id -and $AttemptMatches -and $HasStart -and $ReportAt.UtcDateTime -ge $Started.UtcDateTime
        if ($LocalCurrent) {
            $View.ActivityNote.Text+=" Run #$($Active.Run.id), attempt $($Active.Run.run_attempt), job $($Active.Job.name)."
            return
        }
        if ($S) { $View.PreviousResult.Text="Previous local report: $($S.what), run #$($S.runId), $(if ($S.done) { $S.done } else { 'unfinished; current status unconfirmed' })."; $View.PreviousResult.Visible=$true }
        $script:DisplayedActivity=$Active
        $Step=@($Active.Job.steps | Where-Object status -eq 'in_progress' | Select-Object -First 1)
        $Stage=if ($Active.Job.status -ne 'in_progress') { 'Queued / waiting' } elseif ($Step.Count -and $Step[0].name -match '(checkout|Set up job|Verify checked|Reset generated)') { 'Preparing' } else { 'Job running' }
        $View.Title.Text="$(if (-not $Fresh) { 'Last observed' } else { $Stage }): $($Active.Run.display_title)"
        $View.Title.ForeColor=if ($Fresh) { $Blue } else { $Orange }
        $View.Sub.Text="Run #$($Active.Run.id), attempt $($Active.Run.run_attempt); $($Active.Job.name). Workflow revision $($Active.Run.head_sha)."
        $View.JobLabel.Text="GitHub job: $($Active.Job.name) ($($Active.Job.status))"
        $View.StepLabel.Text=if ($Step.Count) { "Observed step: $($Step[0].name)" } else { 'Runner phase not reported yet.' }
        Set-Bar $JobBar -1 $View; Set-Bar $StepBar -1 $View
        $View.Time.Text="Job status observed $($Snapshot.AtUtc.ToLocalTime().ToString('HH:mm:ss')); waiting for this run's local report."
        $View.Waiting.Text=if ($Active.Job.status -ne 'in_progress') { 'Waiting in GitHub. A more specific runner/concurrency reason has not been reported.' } else { 'This job has started. Its runner report has not caught up yet.' }
        if (-not $Fresh) { $View.Waiting.Text='Live status is unconfirmed; the GitHub job above is the last observation.' }
        $View.StepsHead.Visible=$false; $View.StepsBox.Visible=$false
        $View.QueueHead.Text='Queue: unknown for this run'; $View.Queue.Visible=$false
        $View.QueueHint.Text='Waiting for this run to publish its queue. The previous report does not establish the current queue.'
    } elseif ($S -and $S.done) {
        $View.Title.Text="Last reported: $($S.what) ($($S.done))"
        $View.QueueHead.Text='Queue: unknown from a completed report'; $View.Queue.Visible=$false
        $View.QueueHint.Text='A completed local report does not establish the current queue.'
        if ($Fresh) { $View.ActivityNote.Text+=' No configured active build job was observed in this snapshot.' }
    }
}
function Get-LaneDisplayedRunUrl {
    if ($script:DisplayedActivity) { return "https://github.com/$Repo/actions/runs/$($script:DisplayedActivity.Run.id)/attempts/$($script:DisplayedActivity.Run.run_attempt)" }
    $script:State.runUrl
}
function Get-LaneCurrentRunForControl {
    if ($script:DisplayedActivity) {
        if ($script:ActivityError -or -not $script:ActivitySnapshot -or ([datetime]::UtcNow-$script:ActivitySnapshot.AtUtc).TotalSeconds -gt 60) { return $null }
        return @{runId=$script:DisplayedActivity.Run.id;what=$script:DisplayedActivity.Run.display_title;done=''}
    }
    $script:State
}
