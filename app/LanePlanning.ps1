# Advisory planning only. Never consumes an artifact as instructions or starts native validation.
$script:PlanJob=$null; $script:PlanState=$null; $script:PlanIdentity=''
$script:NextPlanRead=[datetime]::MinValue
$script:PlanWorker={
    param($Request,$Progress)
    $ErrorActionPreference='Stop'
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $Headers=@{Authorization="Bearer $($Request.Token)";Accept='application/vnd.github+json';'User-Agent'='Buildlane-Desktop'}
    function Call([string]$Method,[string]$Path,$Body=$null) {
        $Args=@{Method=$Method;Uri="https://api.github.com/repos/$($Request.Repo)$Path";Headers=$Headers;TimeoutSec=20}
        if ($null -ne $Body) { $Args.ContentType='application/json; charset=utf-8'; $Args.Body=[Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 5)) }
        Invoke-RestMethod @Args
    }
    $Stage='lookup'; $Source=$null; $Base=$null
    try {
        if ($Request.Kind -eq 'observe') {
            $Response=Call GET "/actions/workflows/$([uri]::EscapeDataString($Request.Workflow))/runs?event=workflow_dispatch&branch=$([uri]::EscapeDataString($Request.Branch))&per_page=100"
            $Runs=@($Response.workflow_runs | Where-Object { $_.event -eq 'workflow_dispatch' -and $_.head_branch -eq $Request.Branch -and [string]$_.id -match '^\d+$' -and ([string]$_.display_title).StartsWith("Plan $($Request.RequestId) ") })
            if ($Runs.Count -gt 1) { return @{Error='More than one preview matched this request. Check GitHub.';RetrySeconds=60} }
            if ($Runs.Count -and (([string]$Runs[0].display_title).IndexOf(" source $($Request.Source) base $($Request.Base) ") -lt 0 -or [string]$Runs[0].run_attempt -notmatch '^[1-9]\d*$')) { return @{Error='The matching request has different source/base or attempt identity. Check GitHub.';RetrySeconds=60} }
            return @{Run=$(if ($Runs.Count) { $Runs[0] })}
        }
        function Resolve([string]$Ref) {
            if ($Ref -match '^[a-fA-F0-9]{40}$') { $Commit=Call GET "/commits/$Ref"; $Value=[string]$Commit.sha }
            elseif ($Ref -match '^#?(\d+)$') { $Pr=Call GET "/pulls/$($Matches[1])"; $Value=[string]$Pr.head.sha }
            else { $Commit=Call GET "/commits/$([uri]::EscapeDataString($Ref))"; $Value=[string]$Commit.sha }
            if ($Value -notmatch '^[a-fA-F0-9]{40}$') { throw 'A revision could not be resolved.' }
            $Value.ToLowerInvariant()
        }
        $Progress.Message='Resolving the change and comparison revision...'
        $Source=Resolve $Request.SourceRef; $Base=Resolve $Request.BaseRef
        $Inputs=@{source=$Source;base=$Base;tier=$Request.Tier;test_mode=$Request.TestMode;tests=$Request.Tests;compile=$Request.Compile;request_id=$Request.RequestId}
        $Stage='dispatch'; $Progress.Message='Sending the advisory plan request...'
        Call POST "/actions/workflows/$([uri]::EscapeDataString($Request.Workflow))/dispatches" @{ref=$Request.Branch;inputs=$Inputs} | Out-Null
        @{Source=$Source;Base=$Base;Accepted=$true}
    } catch {
        $Delay=60
        if ($_.Exception.Response) { $Retry=0; if ([int]::TryParse([string]$_.Exception.Response.Headers['Retry-After'],[ref]$Retry)) { $Delay=[math]::Max(60,$Retry) } }
        @{Error=$(if ($Stage -eq 'dispatch') { 'The preview request could not be confirmed. Check GitHub before retrying; it will not be resent automatically.' } else { 'GitHub could not resolve the revisions or read preview status. Check access and try refreshing.' });RetrySeconds=$Delay;Source=$Source;Base=$Base}
    }
}
$PlanTab=New-Object Windows.Forms.TabPage('Build choices'); $PlanTab.BackColor=$Paper
[void]$Tabs.TabPages.Add($PlanTab)
$PlanRoot=New-Object Windows.Forms.TableLayoutPanel
$PlanRoot.Dock='Fill'; $PlanRoot.AutoScroll=$true; $PlanRoot.ColumnCount=1; $PlanRoot.Padding=New-Object Windows.Forms.Padding(24)
$PlanTab.Controls.Add($PlanRoot)
[void](Add-Row (New-Label 'Choose what a change needs' $FontTitle) 0 $PlanRoot)
[void](Add-Row (New-Label 'Preview compilation, imports and tests. This sends an advisory plan request, not a build or package.' $FontBody) 4 $PlanRoot)
$PlanSetup=Add-Row (New-Label '' $FontSmall $Grey) 8 $PlanRoot
$PlanFields=Add-Row (New-Object Windows.Forms.TableLayoutPanel) 8 $PlanRoot
$PlanFields.AutoSize=$true; $PlanFields.ColumnCount=2
[void]$PlanFields.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute',230)))
[void]$PlanFields.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
function Add-PlanField([string]$Caption,$Control) {
    $L=New-Label $Caption $FontSmall; $L.Margin=New-Object Windows.Forms.Padding(0,8,12,8)
    $Control.Dock='Top'; $Control.Margin=New-Object Windows.Forms.Padding(0,4,0,8); $Control.AccessibleName=$Caption
    [void]$PlanFields.Controls.Add($L); [void]$PlanFields.Controls.Add($Control)
    $Control
}
$PlanSource=Add-PlanField 'Change (PR #, commit or branch)' (New-Object Windows.Forms.TextBox)
$PlanBase=Add-PlanField 'Compare with (commit or branch)' (New-Object Windows.Forms.TextBox)
function New-PlanCombo($Names) {
    $C=New-Object Windows.Forms.ComboBox; $C.DropDownStyle='DropDownList'; $C.Items.AddRange([object[]]$Names); $C.SelectedIndex=0; $C
}
$PlanTier=Add-PlanField 'Validation context' (New-PlanCombo @('Routine change','Release','Nightly'))
$PlanTests=Add-PlanField 'Test scope' (New-PlanCombo @('Auto - affected checks','Focused - diagnostic only','Full suite'))
$PlanNames=Add-PlanField 'Focused test names (comma-separated)' (New-Object Windows.Forms.TextBox)
$PlanCompile=Add-PlanField 'Compilation' (New-PlanCombo @('Auto','Incremental','Clean rebuild','None - static checks only'))
$PlanExplanation=Add-Row (New-Label '' $FontSmall $Grey) 4 $PlanRoot
$PlanControls=Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 12 $PlanRoot; $PlanControls.AutoSize=$true
$PlanPreview=New-Button 'Preview plan' { try { Start-LanePlan } catch { $PlanStatus.Text=$_.Exception.Message; $PlanStatus.ForeColor=$Orange } } $PlanControls
$PlanOpen=New-Button 'Open preview run' {
    $Url=Get-LanePlanUrl
    if ($Url) { Start-Process $Url }
} $PlanControls
$PlanRefresh=New-Button 'Refresh preview status' { Start-LanePlanRead -Force } $PlanControls
$PlanStatus=Add-Row (New-Label '' $FontBody $Blue) 12 $PlanRoot
$PlanSnapshot=Add-Row (New-Label '' $FontSmall $Grey) 8 $PlanRoot
[void](Add-Row (New-Label 'Preview results are untrusted advice and require review. They never replace full release validation or authorize artifact reuse.' $FontSmall $Grey) 16 $PlanRoot)
function Update-LanePlanOptions {
    $FullContext=$PlanTier.SelectedIndex -gt 0
    if ($FullContext) { $PlanTests.SelectedIndex=2 }
    $PlanTests.Enabled=-not $FullContext
    $PlanNames.Enabled=(-not $FullContext -and $PlanTests.SelectedIndex -eq 1)
    $PlanExplanation.Text=if ($FullContext) { 'Release and nightly contexts always retain the complete suite. Full tests do not require a clean rebuild.' }
        elseif ($PlanTests.SelectedIndex -eq 1) { 'Enter known test names, for example RunAnimTest. Baseline checks are added by the planner. Focused checks cannot certify a merge or release.' }
        else { 'Auto proposes dependency groups; shared or unknown changes retain full coverage. No compilation is allowed only for static checks until matching binary receipts are supported.' }
}
$PlanTier.Add_SelectedIndexChanged({ Update-LanePlanOptions })
$PlanTests.Add_SelectedIndexChanged({ Update-LanePlanOptions })
function Initialize-LanePlanning {
    $Identity="$Repo|$Profile"
    if ($Identity -ne $script:PlanIdentity) {
        $script:PlanIdentity=$Identity; $script:PlanState=$null; $script:NextPlanRead=[datetime]::MinValue
        $PlanSource.Text=$Config.branch; $PlanBase.Text=$Config.branch; $PlanNames.Text=''
        $PlanTier.SelectedIndex=0; $PlanTests.SelectedIndex=0; $PlanCompile.SelectedIndex=0; $PlanSnapshot.Text=''; $PlanStatus.Text=''
    }
    $Enabled=-not $Config.setupRequired -and -not [string]::IsNullOrWhiteSpace([string]$Config.planWorkflow)
    $PlanSetup.Text=if ($Enabled) { "Planning workflow: $($Config.planWorkflow). Revisions are frozen before sending." } else { 'Set an advisory planning workflow in Projects > Advanced settings to enable previews. This does not provision a backend.' }
    $Busy=$script:PlanJob -or $script:CommandJob -or $script:MoveJob -or $script:ActionJob
    $PlanFields.Enabled=($Enabled -and -not $Busy); $PlanPreview.Enabled=($Enabled -and -not $Busy)
    $PlanOpen.Enabled=($Enabled -and $null -ne $script:PlanState)
    $PlanOpen.Text=if ($script:PlanState -and $script:PlanState.Run) { 'Open preview attempt' } else { 'Open preview runs' }
    $PlanRefresh.Enabled=($Enabled -and -not $Busy -and $script:PlanState -and $script:PlanState.Phase -eq 'accepted')
    Update-LanePlanOptions
}
function Get-LanePlanUrl {
    $State=$script:PlanState
    if (-not $State -or $State.Request.Repo -ne $Repo) { return $null }
    if ($State.Run -and [string]$State.Run.id -match '^\d+$' -and [string]$State.Run.run_attempt -match '^[1-9]\d*$') { return "https://github.com/$Repo/actions/runs/$($State.Run.id)/attempts/$($State.Run.run_attempt)" }
    "https://github.com/$Repo/actions/workflows/$([uri]::EscapeDataString($State.Request.Workflow))"
}
function Get-LanePlanRequest {
    if (-not $Config.planWorkflow) { throw 'Configure the advisory planning workflow first.' }
    $SourceRef=$PlanSource.Text.Trim(); $BaseRef=$PlanBase.Text.Trim()
    foreach ($Value in @($SourceRef,$BaseRef)) { if (-not $Value -or $Value -notmatch '^(?:#?\d+|[A-Za-z0-9][A-Za-z0-9._/-]*)$' -or $Value.Contains('..')) { throw 'Enter a PR number, full commit or branch for both revisions.' } }
    $Tier=@('auto','release','nightly')[$PlanTier.SelectedIndex]
    $Mode=@('auto','focused','full')[$PlanTests.SelectedIndex]
    if ($Tier -ne 'auto' -and $Mode -ne 'full') { throw 'Release and nightly previews require the full suite.' }
    $Compile=@('auto','incremental','clean','none')[$PlanCompile.SelectedIndex]
    if ($Tier -ne 'auto' -and $Compile -eq 'none') { throw 'Full release/nightly validation requires compilation unless verified binary receipts are supported.' }
    $Names=if ($Mode -eq 'focused') { $PlanNames.Text.Trim() } else { '' }
    if ($Mode -eq 'focused' -and ($Names -notmatch '^[A-Za-z][A-Za-z0-9]*(?:\s*,\s*[A-Za-z][A-Za-z0-9]*)*$')) { throw 'Enter comma-separated known test names for focused diagnostics.' }
    @{Kind='dispatch';Repo=$Repo;Branch=$Config.branch;Workflow=$Config.planWorkflow;Token=$script:Token;SourceRef=$SourceRef;BaseRef=$BaseRef;Tier=$Tier;TestMode=$Mode;Tests=($Names -replace '\s','');Compile=$Compile;RequestId=[guid]::NewGuid().ToString();RequestedAtUtc=[datetime]::UtcNow}
}
function Start-LanePlan([scriptblock]$Worker) {
    if ($script:PlanJob -or $script:CommandJob -or $script:MoveJob -or $script:ActionJob) { return }
    if (-not $Worker -and ($SmokeTest -or -not $script:Token)) { throw 'Connect GitHub before requesting a preview.' }
    $Request=Get-LanePlanRequest
    Stop-LaneRefresh
    $script:PlanState=@{Request=$Request.Clone();Phase='sending';Run=$null}; $script:PlanState.Request.Remove('Token')
    Start-LanePlanWorker $Request $Worker
    $PlanStatus.Text='Preparing an advisory preview...'; $PlanStatus.ForeColor=$Blue
    Update-View
}
function Start-LanePlanWorker($Request,[scriptblock]$Worker) {
    $Progress=[hashtable]::Synchronized(@{Message='Contacting GitHub...'})
    $Shell=[PowerShell]::Create()
    try {
        if (-not $Worker) { $Worker=$script:PlanWorker }
        [void]$Shell.AddScript($Worker.ToString()).AddArgument($Request).AddArgument($Progress)
        $script:PlanJob=@{Shell=$Shell;Handle=$Shell.BeginInvoke();Kind=$Request.Kind;Generation=$script:ProjectGeneration;RequestId=$Request.RequestId;Progress=$Progress}
    } catch { $Shell.Dispose(); $script:PlanState.Phase='error'; throw }
}
function Complete-LanePlan {
    $Job=$script:PlanJob
    if (-not $Job) { return }
    if (-not $Job.Handle.IsCompleted) { if ($Job.Kind -eq 'dispatch') { $PlanStatus.Text=$Job.Progress.Message }; return }
    try { $Results=@($Job.Shell.EndInvoke($Job.Handle)); if ($Job.Shell.HadErrors -or -not $Results.Count) { throw 'Unconfirmed response' }; $Result=$Results[-1] }
    catch { $Result=@{Error='The preview response could not be confirmed. Check GitHub before retrying.';RetrySeconds=60} }
    finally { $Job.Shell.Dispose(); $script:PlanJob=$null }
    $State=$script:PlanState
    if ($Job.Generation -ne $script:ProjectGeneration -or -not $State -or $Job.RequestId -ne $State.Request.RequestId) { return }
    if ($Result.Error) {
        if ($Job.Kind -eq 'dispatch') { $State.Phase='error' }
        if ($Result.Source -match '^[a-f0-9]{40}$' -and $Result.Base -match '^[a-f0-9]{40}$') {
            $State.Source=$Result.Source; $State.Base=$Result.Base
            $PlanSnapshot.Text="Source: $($State.Source)`nCompared with: $($State.Base)`nRequest: $($State.Request.RequestId)`nThe request outcome is uncertain; inspect GitHub before retrying."
        }
        $PlanStatus.Text=$Result.Error; $PlanStatus.ForeColor=$Orange
        $script:NextPlanRead=[datetime]::UtcNow.AddSeconds([math]::Max(10,[int]$Result.RetrySeconds))
    } elseif ($Job.Kind -eq 'dispatch') {
        if (-not $Result.Accepted -or $Result.Source -notmatch '^[a-f0-9]{40}$' -or $Result.Base -notmatch '^[a-f0-9]{40}$') { $State.Phase='error'; $PlanStatus.Text='The preview request identity could not be confirmed. Check GitHub before retrying.' }
        else {
            $State.Phase='accepted'; $State.Source=$Result.Source; $State.Base=$Result.Base
            $PlanStatus.Text='Preview request accepted. Waiting for its matching planning run; no build was requested.'
            $PlanSnapshot.Text="Source: $($State.Source)`nCompared with: $($State.Base)`nContext: $($State.Request.Tier); tests: $($State.Request.TestMode); compile: $($State.Request.Compile)`nRequest: $($State.Request.RequestId)"
            $script:NextPlanRead=[datetime]::MinValue
        }
    } elseif ($Result.Run) {
        if ($Result.Run.event -eq 'workflow_dispatch' -and $Result.Run.head_branch -eq $State.Request.Branch -and ([string]$Result.Run.display_title).StartsWith("Plan $($State.Request.RequestId) ") -and ([string]$Result.Run.display_title).Contains(" source $($State.Source) base $($State.Base) ") -and [string]$Result.Run.id -match '^\d+$' -and [string]$Result.Run.run_attempt -match '^[1-9]\d*$') {
            $State.Run=$Result.Run
            $State.Attempts=@(@{id=$Result.Run.id;attempt=$Result.Run.run_attempt;status=$Result.Run.status;conclusion=$Result.Run.conclusion})+@($State.Attempts | Where-Object { $_.id -ne $Result.Run.id -or $_.attempt -ne $Result.Run.run_attempt } | Select-Object -First 9)
            $PlanStatus.Text=if ($Result.Run.status -eq 'completed') { "Advisory preview attempt $($Result.Run.run_attempt) ended: $($Result.Run.conclusion). Open the run to review the plan; this does not certify a build or release." } else { "Advisory preview attempt $($Result.Run.run_attempt): $($Result.Run.status). No build or package was requested." }
            $PlanSnapshot.Text="Source: $($State.Source)`nCompared with: $($State.Base)`nContext: $($State.Request.Tier); tests: $($State.Request.TestMode); compile: $($State.Request.Compile)`nRequest: $($State.Request.RequestId)`nWorkflow revision: $($Result.Run.head_sha)`nObserved attempts: $(($State.Attempts | ForEach-Object { "#$($_.id)/$($_.attempt) $($_.status) $($_.conclusion)" }) -join '; ')"
            $PlanStatus.ForeColor=if ($Result.Run.conclusion -in @('failure','cancelled','timed_out')) { $Orange } else { $Blue }
        }
    }
    Update-View
}
function Start-LanePlanRead([scriptblock]$Worker,[switch]$Force) {
    $State=$script:PlanState
    if (-not $State -or $State.Phase -ne 'accepted' -or (-not $Force -and $State.Run -and $State.Run.status -eq 'completed') -or $script:PlanJob -or $script:RefreshJob -or $script:CommandJob -or $script:MoveJob -or $script:ActionJob -or [datetime]::UtcNow -lt $script:NextPlanRead) { return }
    if (-not $Worker -and ($SmokeTest -or -not $script:Token)) { return }
    $Request=$State.Request.Clone(); $Request.Kind='observe'; $Request.Token=$script:Token; $Request.Source=$State.Source; $Request.Base=$State.Base
    Start-LanePlanWorker $Request $Worker
    $script:NextPlanRead=[datetime]::UtcNow.AddSeconds($(if (([datetime]::UtcNow-$State.Request.RequestedAtUtc).TotalMinutes -lt 1) { 10 } else { 30 }))
}
function Stop-LanePlanning {
    if ($script:PlanJob) {
        [void]$script:PlanJob.Shell.BeginStop($null,$null)
        [void]$script:RetiredJobs.Add($script:PlanJob); $script:PlanJob=$null
    }
}
