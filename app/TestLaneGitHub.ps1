# Default: isolated reader regression test. -Live: read-only GitHub refresh through the actual UI renderer.
param([switch]$Live, [string]$Profile)
$ErrorActionPreference='Stop'
if ($Live) {
    if (-not $Profile) { throw 'Pass -Profile with a configured project JSON file for the live test.' }
    . "$PSScriptRoot\LaneApp.ps1" -Profile $Profile -NoShow
    if ($Config.setupRequired) { throw 'Configure the project before running the live test.' }
    try {
        Start-LaneRefresh
        $Deadline=[datetime]::UtcNow.AddSeconds(60)
        while ($script:RefreshJob -and [datetime]::UtcNow -lt $Deadline) { Complete-LaneRefresh; Start-Sleep -Milliseconds 100 }
        if ($script:RefreshJob -or $RelMsg.Text -notmatch '^Updated') { throw "Live refresh failed: $($RelMsg.Text)" }
        "PASS: live Windows PowerShell release rendering ($($RelList.Items.Count) releases, $($PrList.Items.Count) PRs)."
    } finally { Stop-LaneRefresh; $Timer.Dispose(); $RefreshTimer.Dispose(); $Form.Dispose() }
    return
}
$global:LaneGitHubTestPages=New-Object Collections.ArrayList
function Invoke-WebRequest {
    param($Uri,$Headers,$TimeoutSec,[switch]$UseBasicParsing)
    [void]$global:LaneGitHubTestPages.Add($Uri)
    $Items=if ($Uri -match '/pulls\?') {
        @(@{number=21;title='First PR'},@{number=22;title='Second PR'})
    } elseif ($Uri -match 'page=2$') {
        @(@{number=101;title='v101';due_on='2026-12-01T00:00:00Z'})
    } elseif ($Uri -match 'state=open') {
        @(1..100 | ForEach-Object { @{number=$_;title="v$_";due_on='2026-11-01T00:00:00Z'} })
    } else {
        @(@{number=102;title='v102';closed_at='2026-10-25T00:00:00Z'},@{number=103;title='v103';closed_at='2026-10-26T00:00:00Z'})
    }
    @{Content=ConvertTo-Json -InputObject $Items -Depth 5}
}
$Result=& "$PSScriptRoot\LaneGitHub.ps1" -Repository 'example/test' -BaseBranch 'main' -AccessToken 'test-only'
if ($Result.Error) { throw $Result.Error }
if ($Result.Open.Count -ne 101) { throw "Expected 101 flat milestones across two pages; got $($Result.Open.Count)." }
if ($Result.Closed.Count -ne 2 -or $Result.Prs.Count -ne 2) { throw 'Closed milestones or PRs were returned as nested arrays.' }
foreach ($Milestone in $Result.Open) { [void][datetime]::Parse($Milestone.due_on) }
foreach ($Milestone in $Result.Closed) { [void][datetime]::Parse($Milestone.closed_at) }
if ($global:LaneGitHubTestPages.Count -ne 4) { throw 'Pagination did not request exactly the required pages.' }
'PASS: paginated GitHub arrays contain individual milestones and PRs with scalar dates.'

# Workflow dispatch returns no run ID. Observe only recent controllers in this workflow/branch;
# never treat an older successful controller as evidence for the new request.
function Invoke-WebRequest {
    param($Uri,$Headers,$TimeoutSec,[switch]$UseBasicParsing)
    if ($Uri -notmatch '/workflows/lane.yml/runs\?event=workflow_dispatch&branch=develop&per_page=30$') { throw 'Controller query lost workflow/branch filtering.' }
    @{Content=(@{workflow_runs=@(
        @{id=1;created_at='2026-10-29T12:00:00Z';status='completed';conclusion='success'},
        @{id=2;created_at='2026-10-29T12:01:02Z';status='queued'}
    )} | ConvertTo-Json -Depth 5)}
}
$Result=& "$PSScriptRoot\LaneGitHub.ps1" -Repository 'example/test' -BaseBranch 'develop' -AccessToken 'test-only' -Kind controller -Workflow lane.yml -RequestedAfterUtc ([datetime]'2026-10-29T12:01:00Z')
if ($Result.Error -or $Result.Controller.id -ne 2) { throw 'Controller observation selected an old run.' }
$Result=& "$PSScriptRoot\LaneGitHub.ps1" -Repository 'example/test' -BaseBranch 'develop' -AccessToken 'test-only' -Kind controller -Workflow lane.yml -RequestedAfterUtc ([datetime]'2026-10-29T12:02:00Z')
if ($Result.Error -or $Result.Controller) { throw 'Missing recent controller was replaced by old success.' }
'PASS: controller observation filters workflow, branch and request time without claiming build completion.'
