$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('buildlane-moves-'+[guid]::NewGuid())
try {
    . "$PSScriptRoot\LaneApp.ps1" -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
    # Exercise the actual request worker without making network calls.
    $script:Calls=New-Object Collections.ArrayList
    $script:FailureMode='partial'
    function Invoke-RestMethod {
        param($Method,$Uri,$Headers,$ContentType,$Body,$TimeoutSec)
        [void]$script:Calls.Add(@{Method=$Method;Uri=$Uri;Body=([Text.Encoding]::UTF8.GetString($Body) | ConvertFrom-Json)})
        if ($Uri -like '*/issues/2') {
            if ($script:FailureMode -eq 'rate') {
                $Exception=New-Object Exception 'Rate limited'
                $Exception | Add-Member NoteProperty Response ([pscustomobject]@{StatusCode=429})
                throw $Exception
            }
            throw 'Fixture timeout'
        }
        if ($Method -eq 'POST' -and $script:FailureMode -eq 'dispatch') { throw 'Fixture dispatch failure' }
    }
    $Request=@{Repo='example/project';Branch='develop';Workflow='lane.yml';Token='fixture-only';Numbers=@(1,2,3);Milestone=$null}
    $Progress=[hashtable]::Synchronized(@{Done=0;Message=''})
    $Result=& $script:MoveWorker $Request $Progress
    Assert (($Result.Succeeded -join ',') -eq '1,3' -and ($Result.Failed -join ',') -eq '2') 'Partial failure accounting is incorrect.'
    Assert (@($script:Calls | Where-Object Method -eq POST).Count -eq 1) 'Lane dispatch must occur once.'
    Assert ($null -eq $script:Calls[0].Body.milestone -and $script:Calls[-1].Body.ref -eq 'develop') 'Snapshot target or branch changed.'
    $script:Calls.Clear(); $script:FailureMode='rate'; $Progress.Done=0
    $Result=& $script:MoveWorker $Request $Progress
    Assert (($Result.Failed -join ',') -eq '2,3') 'Rate limiting did not retain the unattempted item.'
    Assert (-not @($script:Calls | Where-Object Uri -like '*/issues/3').Count) 'Rate-limited batch continued sending writes.'
    $script:Calls.Clear(); $script:FailureMode='dispatch'; $Progress.Done=0
    $Result=& $script:MoveWorker $Request $Progress
    Assert ($Result.DispatchError -and $Result.Succeeded.Count -eq 2) 'Dispatch failure lost confirmed moves.'
    $script:Calls.Clear(); $Request.Numbers=@(2); $Progress.Done=0
    $Result=& $script:MoveWorker $Request $Progress
    Assert (-not @($script:Calls | Where-Object Method -eq POST).Count) 'All-failed moves dispatched the lane.'
    Remove-Item Function:\Invoke-RestMethod

    $Config.setupRequired=$false
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); Update-View
    foreach ($Number in @(1,2,3)) {
        $Item=New-Object Windows.Forms.ListViewItem "#$Number"
        [void]$Item.SubItems.Add('Sample PR'); [void]$Item.SubItems.Add('Old release'); [void]$Item.SubItems.Add('ready')
        $Item.Tag=$Number; $Item.Checked=$true; [void]$PrList.Items.Add($Item)
    }
    $Watch=[Diagnostics.Stopwatch]::StartNew()
    Start-LaneMove @(1,2,3) 7 'Next release' -Worker {
        param($Request,$Progress)
        $Progress.Message='Moving fixture PRs...'; Start-Sleep -Milliseconds 700
        @{Succeeded=@(1,3);Failed=@(2);DispatchError=''}
    }
    Assert ($Watch.ElapsedMilliseconds -lt 500) 'Starting a move blocked the UI.'
    $First=$script:MoveJob
    Start-LaneMove @(4) 9 'Wrong release' -Worker { throw 'Duplicate must not run' }
    Assert ([object]::ReferenceEquals($First,$script:MoveJob)) 'Duplicate move was started.'
    Start-LaneRefresh -Worker { throw 'Refresh must not overlap a move' }
    Assert (-not $script:RefreshJob) 'Read refresh overlapped a move.'
    $Rejected=$false
    try { Use-LaneProject $Profile } catch { $Rejected=$true }
    Assert $Rejected 'Project switching was allowed during a move.'
    $Form.Close()
    Assert (-not $Form.IsDisposed) 'Closing abandoned an in-flight move.'
    $Pulses=0
    while ($script:MoveJob -and $Watch.ElapsedMilliseconds -lt 10000) {
        [Windows.Forms.Application]::DoEvents(); Complete-LaneMove; $Pulses++; Start-Sleep -Milliseconds 20
    }
    Assert ($Pulses -gt 2 -and -not $script:MoveJob) 'UI did not remain responsive until completion.'
    Assert (($PrList.CheckedItems | ForEach-Object Tag) -eq 2) 'Failed items were not retained for retry.'
    Assert ($PrList.Items[0].SubItems[2].Text -eq 'Next release') 'Confirmed move was not reflected in the list.'
    Assert ($MoveStatus.Text -match 'Moved 2 of 3' -and $MoveStatus.Text -match '#2') 'Partial result is not visible.'
    Assert ($ProjectRoot.Enabled -and $MoveBar.Enabled -and $PrList.Enabled) 'Controls did not recover.'
    Write-Output 'PASS: responsive bulk moves, partial failures, rate limits, dispatch errors, duplicate/read isolation, project and close guards.'
} finally {
    if ($script:MoveJob) { $script:MoveJob.Shell.Stop(); $script:MoveJob.Shell.Dispose(); $script:MoveJob=$null }
    $script:ProjectDirty=$false
    if ($Form) { $Form.Close(); $Form.Dispose() }
    $Resolved=[IO.Path]::GetFullPath($FixtureRoot)
    if ($Resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase) -and (Split-Path $Resolved -Leaf) -like 'buildlane-moves-*' -and (Test-Path -LiteralPath $Resolved)) { Remove-Item -LiteralPath $Resolved -Recurse -Force }
}
