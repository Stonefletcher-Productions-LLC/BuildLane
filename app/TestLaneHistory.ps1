$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-history-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
try {
    $global:LaneHistoryWrongJob=$false; $global:LaneHistoryCalls=New-Object Collections.ArrayList; $global:LaneHistoryRerun=$false
    function Invoke-WebRequest {
        param($Uri,$Headers,$TimeoutSec,[switch]$UseBasicParsing)
        [void]$global:LaneHistoryCalls.Add($Uri)
        if ($Uri -match '/runs/42$') { $Data=@{id=42;run_attempt=$(if ($global:LaneHistoryRerun -and @($global:LaneHistoryCalls | Where-Object { $_ -match '/jobs\?' }).Count) { 3 } else { 2 });head_sha=('a'*40);status='completed';conclusion='cancelled';created_at='2026-10-28T12:00:00Z';run_started_at='2026-10-29T12:00:10Z'} }
        elseif ($Uri -match '/runs/42/attempts/2/jobs\?per_page=100&page=1$') {
            $Data=@{jobs=@(@{run_id=$(if ($global:LaneHistoryWrongJob) { 999 } else { 42 });name='Build';conclusion='cancelled';started_at='2026-10-29T12:00:30Z';completed_at='2026-10-29T12:02:00Z';steps=@(
                @{name='Compile';conclusion='success';started_at='2026-10-29T12:00:30Z';completed_at='2026-10-29T12:01:00Z'},
                @{name='Tests';conclusion='cancelled';started_at='2026-10-29T12:01:00Z';completed_at='2026-10-29T12:02:00Z'},
                @{name='Package';conclusion='skipped';started_at='2026-10-29T12:02:00Z';completed_at='2026-10-29T12:02:00Z'}
            )})}
            $Data.jobs+=@(1..99 | ForEach-Object { @{run_id=42;name="Pending job $_";status='queued';steps=@()} })
        } elseif ($Uri -match '/runs/42/attempts/2/jobs\?per_page=100&page=2$') {
            $Data=@{jobs=@(@{run_id=42;name='No timestamps';status='queued';steps=@()})}
        } else { throw "Unexpected fixture URL: $Uri" }
        @{Content=($Data | ConvertTo-Json -Depth 8)}
    }
    $Result=& "$PSScriptRoot/LaneGitHub.ps1" -Repository example/history -AccessToken fixture -Kind history -RunId 42
    Assert (-not $Result.Error) 'History reader failed.'
    $Record=$Result.History
    Assert ($Record.attempt -eq 2 -and $Record.beforeJobsSeconds -eq 20 -and $Record.jobSpanSeconds -eq 90) 'Exact-attempt timing calculations included time between reruns.'
    Assert ($Record.jobs[0].steps[0].seconds -eq 30 -and $Record.jobs[0].steps[1].result -eq 'cancelled') 'Step times or failure visibility changed.'
    Assert ($null -eq $Record.jobs[0].steps[2].seconds -and $Record.jobs[0].steps[2].result -eq 'skipped') 'Skipped package was counted as executed.'
    Assert ($Record.jobs.Count -eq 101 -and @($global:LaneHistoryCalls | Where-Object { $_ -match 'page=2$' }).Count -eq 1) 'Job pagination did not collect the second page.'
    Assert ($null -eq $Record.jobs[100].seconds) 'Missing timestamps were converted to zero duration.'
    $global:LaneHistoryWrongJob=$true
    $Bad=& "$PSScriptRoot/LaneGitHub.ps1" -Repository example/history -AccessToken fixture -Kind history -RunId 42
    Assert ($Bad.Error -and -not $Bad.History) 'Wrong-run job contaminated history.'
    $global:LaneHistoryWrongJob=$false; $global:LaneHistoryCalls.Clear(); $global:LaneHistoryRerun=$true
    $Bad=& "$PSScriptRoot/LaneGitHub.ps1" -Repository example/history -AccessToken fixture -Kind history -RunId 42
    Assert ($Bad.Error -match 'attempt changed' -and -not $Bad.History) 'A rerun race was stored as the current attempt.'
    Remove-Item Function:\Invoke-WebRequest
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'
    @{name='History fixture';repo='example/history';branch='main';workflow='lane.yml';statusFile="$FixtureRoot/status.json";pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); $Tabs.SelectedTab=$HistoryTab
    Initialize-LaneHistory
    $script:State=@{runId='42';started='2026-10-29T12:00:00Z';done='cancelled';what='Fixture build'}
    $Job=@{RunId='42';What='Fixture build';Report='42|started|cancelled'}
    Set-LaneHistoryResult $Job @{History=$Record}
    Assert ($script:HistoryRecords.Count -eq 1 -and (Test-Path $script:HistoryFile)) 'Completed attempt not persisted.'
    Assert ($HistoryDetails.Text -match 'cancelled' -and $HistoryDetails.Text -match 'Package.*skipped.*not reported') 'History UI lost cancellation/skipped evidence.'
    $Record.attempt=1; Set-LaneHistoryResult $Job @{History=$Record}
    # Recreate the record so attempt identities cannot alias in the fixture.
    $Record=$Result.History | ConvertTo-Json -Depth 8 | ConvertFrom-Json
    $Record.attempt=2; Set-LaneHistoryResult $Job @{History=$Record}
    Assert (@($script:HistoryRecords | Where-Object id -eq '42').Count -eq 2) 'A rerun attempt overwrote the earlier attempt.'
    foreach ($N in 43..65) {
        $Copy=$Record | ConvertTo-Json -Depth 8 | ConvertFrom-Json; $Copy.id=[string]$N
        Set-LaneHistoryResult @{RunId=[string]$N;What='Fixture';Report='fixture'} @{History=$Copy}
    }
    Assert ($script:HistoryRecords.Count -eq 20) 'History retention is unbounded.'
    $Before=$script:HistoryRecords[0].id
    Set-LaneHistoryResult @{RunId='999';What='Wrong'} @{History=$Record}
    Assert ($script:HistoryRecords[0].id -eq $Before) 'Mismatched response replaced valid history.'
    $OldFile=$script:HistoryFile; $script:Repo='example/other'; Initialize-LaneHistory
    Assert (-not $script:HistoryRecords.Count -and $OldFile -ne $script:HistoryFile) 'History leaked between projects.'
    $script:Repo='example/history'; Initialize-LaneHistory
    Assert ($script:HistoryRecords.Count -eq 20) 'Saved history did not survive reload.'
    $script:NextHistory=[datetime]::MinValue
    Start-LaneHistory -Worker { Start-Sleep -Milliseconds 250; @{History=@{id='42';attempt=3;repo='example/history';status='completed';conclusion='failure';what='stale';jobs=@()}} }
    $Old=$script:RefreshJob
    Start-LaneHistory -Worker { throw 'Duplicate history read' }
    Assert ([object]::ReferenceEquals($Old,$script:RefreshJob)) 'History reads overlapped.'
    Stop-LaneRefresh
    Start-Sleep -Milliseconds 350; Complete-LaneRefresh
    Assert (-not @($script:HistoryRecords | Where-Object attempt -eq 3).Count -and -not $script:RetiredJobs.Count) 'Stale history response survived retirement or leaked its worker.'
    Assert ((Get-LaneWaitingText @{phase=0;detail='Packaging run #99 owns the runner'} $false) -match 'Packaging run #99') 'Reported preparation reason was lost.'
    Assert ((Get-LaneWaitingText @{phase=0} $false) -match 'has not reported a reason') 'Unknown waiting reason was invented.'
    Assert ((Get-LaneWaitingText @{phase=0;waitingReason='Waiting for packaging';blockingRunId=99;waitingSince='2026-10-29T12:00:00Z'} $false) -match 'blocking run #99') 'Explicit blocker identity was lost.'
    Assert ((Get-LaneWaitingText @{phase=2;step='Compile'} $false) -eq '') 'Active work was labeled waiting.'
    Assert ((Get-LaneWaitingText @{phase=2;step='Compile';waitingReason='Stale package wait';waitingPhase=0;waiting=$true} $false) -eq '') 'Stale wait fields survived phase advancement.'
    Assert ((Get-LaneWaitingText @{phase=4;waiting=$true;waitingPhase=4;waitingReason='Waiting for multiplayer clients'} $false) -match 'multiplayer clients') 'Explicit current wait inside a later phase was hidden.'
    Assert ((Get-LaneWaitingText $null $true) -match 'paused locally') 'Local pause reason missing.'
    Assert (-not $script:UiErrors.Count) 'A UI event threw.'
    'PASS: exact run/attempt reader, real step times, cancelled/skipped results, bounded persistent history, project isolation and truthful waiting reasons.'
} finally {
    foreach ($Job in @($script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
