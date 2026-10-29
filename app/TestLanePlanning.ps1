# Fixture-only planner UI and HTTP contract. Never dispatches a real workflow.
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-planning-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'
    $ProfileData=@{name='Planning fixture';repo='example/planning';branch='main';workflow='lane.yml';statusFile="$FixtureRoot/status.json";pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2}
    $ProfileData | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
    function Drain-Plan {
        $Until=[datetime]::UtcNow.AddSeconds(5)
        while ($script:PlanJob -and [datetime]::UtcNow -lt $Until) { Complete-LanePlan; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20 }
        Assert (-not $script:PlanJob) 'Preview worker did not complete.'
    }
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); Update-View
    Assert (-not $PlanPreview.Enabled) 'An old profile enabled an unsupported planning backend.'
    $ProfileData.planWorkflow='../ci.yml'; $Rejected=$false; try { Test-LaneProfile $ProfileData } catch { $Rejected=$true }; Assert $Rejected 'Invalid workflow path accepted.'
    $ProfileData.planWorkflow='build-plan-shadow.yml'
    $Hash=(Get-FileHash $FixtureProfile).Hash; Save-LaneProfile $FixtureProfile $ProfileData $Hash
    $script:Config=Read-LaneProfile $FixtureProfile; Initialize-LanePlanning
    Assert $PlanPreview.Enabled 'Configured preview did not enable.'
    $PlanSource.Text='#42'; $PlanBase.Text='main'; $PlanTests.SelectedIndex=1; $PlanNames.Text='RunAnimTest, RunFlowTest'; $PlanCompile.SelectedIndex=1
    $Request=Get-LanePlanRequest
    Assert ($Request.Tests -eq 'RunAnimTest,RunFlowTest' -and $Request.TestMode -eq 'focused') 'Focused input was not normalized.'
    $script:Calls=New-Object Collections.ArrayList; $script:FailPost=$false; $script:FixtureRuns=@()
    function Invoke-RestMethod {
        param($Method,$Uri,$Headers,$TimeoutSec,$ContentType,$Body)
        $Data=if ($Body) { [Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json }
        [void]$script:Calls.Add(@{Method=$Method;Uri=$Uri;Data=$Data})
        if ($Method -eq 'POST') { if ($script:FailPost) { throw 'Fixture uncertain response' }; return }
        if ($Uri -match '/pulls/42$') { return @{head=@{sha=('a'*40)}} }
        if ($Uri -match '/commits/main$') { return @{sha=('b'*40)} }
        if ($Uri -match '/commits/1{40}$') { return @{sha=('1'*40)} }
        if ($Uri -match '/runs\?') { return @{workflow_runs=$script:FixtureRuns} }
        throw "Unexpected fixture endpoint $Uri"
    }
    $Progress=[hashtable]::Synchronized(@{Message=''})
    $Result=& $script:PlanWorker $Request $Progress
    Assert ($Result.Accepted -and $Result.Source -eq ('a'*40) -and $Result.Base -eq ('b'*40)) 'Revisions were not frozen.'
    $Posts=@($script:Calls | Where-Object Method -eq POST)
    Assert ($Posts.Count -eq 1 -and $Posts[0].Uri -match '/build-plan-shadow.yml/dispatches$') 'Wrong workflow or duplicate dispatch.'
    Assert ($Posts[0].Data.inputs.source -eq ('a'*40) -and $Posts[0].Data.inputs.request_id -eq $Request.RequestId -and $Posts[0].Data.inputs.test_mode -eq 'focused') 'Dispatch lost source/request/options identity.'
    Assert (-not (Test-Path $PauseFile)) 'Preview changed the local pause flag.'
    $script:Calls.Clear(); $script:FailPost=$true
    $Result=& $script:PlanWorker $Request $Progress
    Assert ($Result.Error -and $Result.Source -eq ('a'*40) -and @($script:Calls | Where-Object Method -eq POST).Count -eq 1) 'Uncertain POST retried or lost its frozen source.'
    $script:FailPost=$false; $Request.SourceRef='1'*40
    $Result=& $script:PlanWorker $Request $Progress
    Assert ($Result.Source -eq ('1'*40)) 'A numeric commit was mistaken for a PR number.'
    $Request.Kind='observe'; $Request.Source='a'*40; $Request.Base='b'*40
    $Run=@{id=99;run_attempt=1;event='workflow_dispatch';head_branch='main';head_sha=('c'*40);status='completed';conclusion='failure';display_title="Plan $($Request.RequestId) source $($Request.Source) base $($Request.Base) (auto)"}
    $Other=$Run.Clone(); $Other.display_title='Plan other-request source wrong base wrong (auto)'
    $script:FixtureRuns=@($Other); $Result=& $script:PlanWorker $Request $Progress
    Assert (-not $Result.Run) 'An unrelated latest run was selected.'
    $script:FixtureRuns=@($Run,$Run.Clone()); $Result=& $script:PlanWorker $Request $Progress
    Assert ($Result.Error -and -not $Result.Run) 'Duplicate UUID dispatch was silently resolved.'
    $Wrong=$Run.Clone(); $Wrong.display_title="Plan $($Request.RequestId) source wrong base wrong (auto)"
    $script:FixtureRuns=@($Wrong); $Result=& $script:PlanWorker $Request $Progress
    Assert ($Result.Error -and -not $Result.Run) 'Wrong source/base was accepted.'
    $script:FixtureRuns=@($Run); $Result=& $script:PlanWorker $Request $Progress
    Assert ($Result.Run.id -eq 99 -and $Result.Run.run_attempt -eq 1) 'Exact run/attempt was lost.'
    Remove-Item Function:\Invoke-RestMethod
    $PlanTier.SelectedIndex=1
    Assert ($PlanTests.SelectedIndex -eq 2 -and -not $PlanTests.Enabled -and -not $PlanNames.Enabled) 'Release did not force full coverage.'
    $PlanCompile.SelectedIndex=3; $Rejected=$false; try { Get-LanePlanRequest | Out-Null } catch { $Rejected=$true }; Assert $Rejected 'Release allowed no compilation without receipts.'
    $PlanTier.SelectedIndex=2; $PlanCompile.SelectedIndex=0
    Assert ((Get-LanePlanRequest).TestMode -eq 'full') 'Nightly did not retain full tests.'
    $PlanTier.SelectedIndex=0; $PlanTests.SelectedIndex=0; $PlanSource.Text='#42'
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LanePlan -Worker { param($Request,$Progress) Start-Sleep -Milliseconds 800; @{Accepted=$true;Source=('a'*40);Base=('b'*40)} }
    $Latency=$Watch.ElapsedMilliseconds
    Assert ($Latency -lt 500 -and -not $PlanPreview.Enabled) 'Starting a preview blocked the UI or permitted duplicates.'
    $First=$script:PlanJob; Start-LanePlan -Worker { throw 'Duplicate' }
    Start-LaneAction close -Worker { throw 'Competing write' }; Start-LaneCommand -Worker { throw 'Competing command' }
    Assert ([object]::ReferenceEquals($First,$script:PlanJob) -and -not $script:ActionJob -and -not $script:CommandJob) 'Preview overlapped a write.'
    $Rejected=$false; try { Use-LaneProject $Profile } catch { $Rejected=$true }; Assert $Rejected 'Project changed while sending.'
    $Form.Close(); Assert (-not $Form.IsDisposed) 'Window closed with dispatch in flight.'
    Drain-Plan
    Assert ($PlanStatus.Text -match 'accepted' -and $PlanSnapshot.Text -match ('a'*40)) 'Acceptance or frozen identity not shown.'
    Assert ((Get-LanePlanUrl) -match '/actions/workflows/build-plan-shadow.yml$') 'Pending request did not offer the workflow list.'
    Assert (-not $script:PlanState.Request.ContainsKey('Token')) 'Credential leaked into public request state.'
    $script:NextPlanRead=[datetime]::MinValue
    Start-LanePlanRead -Worker { param($Request,$Progress) @{Run=@{id=99;run_attempt=1;event='workflow_dispatch';head_branch='main';head_sha=('c'*40);status='completed';conclusion='failure';display_title="Plan $($Request.RequestId) source $($Request.Source) base $($Request.Base) (auto)"}} }
    Drain-Plan
    Assert ($PlanStatus.Text -match 'attempt 1 ended: failure' -and $PlanOpen.Enabled) 'Preview failure was hidden or certified as build success.'
    $script:NextPlanRead=[datetime]::MinValue
    Start-LanePlanRead -Force -Worker { param($Request,$Progress) @{Run=@{id=99;run_attempt=2;event='workflow_dispatch';head_branch='main';head_sha=('c'*40);status='completed';conclusion='success';display_title="Plan $($Request.RequestId) source $($Request.Source) base $($Request.Base) (auto)"}} }
    Drain-Plan
    Assert ($PlanSnapshot.Text -match '/1 completed failure' -and $PlanSnapshot.Text -match '/2 completed success') 'A retry hid the earlier failure.'
    Assert ((Get-LanePlanUrl) -match '/actions/runs/99/attempts/2$') 'Preview link did not bind the observed attempt.'
    $script:NextPlanRead=[datetime]::MinValue
    Start-LanePlanRead -Force -Worker { Start-Sleep -Milliseconds 250; @{Run=@{id=100;run_attempt=1;status='completed';conclusion='success';display_title='Wrong request'}} }
    $script:ProjectGeneration++; Drain-Plan
    Assert ($script:PlanState.Run.id -eq 99) 'An old-generation response overwrote the preview.'
    Assert (-not $script:PlanJob -and -not $script:UiErrors.Count) 'Worker leak or UI exception.'
    "PASS: advisory workflow payload, frozen revisions/UUID/attempt, uncertain writes, duplicate/read/write guards, release/nightly coverage and responsive preview ${Latency}ms."
} finally {
    foreach ($Job in @($script:PlanJob,$script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
