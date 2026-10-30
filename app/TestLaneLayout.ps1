param([string]$OutDirectory)
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('lane-layout-'+[guid]::NewGuid())
[void][IO.Directory]::CreateDirectory($FixtureRoot)
try {
    $FixtureProfile=Join-Path $FixtureRoot 'project.json'; $StatusPath=Join-Path $FixtureRoot 'status.json'
    @{name='LifeShare layout fixture';repo='example/layout';branch='main';workflow='lane.yml';activeJobs='ci.yml|Build and test';statusFile=$StatusPath;pauseFile="$FixtureRoot/PAUSED";runnerPath=$FixtureRoot;drive='C';serviceFilter='*fixture*';cachePort=0;minimumFreeGB=1;cleanBelowGB=2} | ConvertTo-Json | Set-Content $FixtureProfile
    . "$PSScriptRoot/LaneApp.ps1" -Profile $FixtureProfile -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    $Status=@{runId='10';what='batch compile preview (no acceptance tests)';done='failure';phase=2;phases=@('Wait for the PC','Quick checks','Compile','Content imports','Tests','Package');step='Report';started=[datetime]::UtcNow.AddMinutes(-4).ToString('o');updated=[datetime]::UtcNow.ToString('o');queue=@();notes=@()}
    $Status | ConvertTo-Json -Depth 6 | Set-Content $StatusPath
    Set-Content $PauseFile 'Paused by release owner while Test-Staging migration is verified. Current integration was allowed to finish.'
    $Form.Show(); [Windows.Forms.Application]::DoEvents(); $Timer.Stop(); $RefreshTimer.Stop(); $Tabs.SelectedTab=$NowTab
    $Form.MaximumSize=New-Object Drawing.Size(2000,2000)
    $Form.Size=New-Object Drawing.Size(980,1100); $NowSplit.SplitterDistance=500
    $Cells=@{}; foreach ($Control in $Root.Controls) { $Cells[$Control]=$Root.GetPositionFromControl($Control) }
    foreach ($Dark in @($false,$true)) {
        Set-LaneTheme $Dark
        foreach ($N in 1..12) {
            $script:ActivitySnapshot=@{AtUtc=[datetime]::UtcNow;Active=$null;Count=0;Repo=$Repo}
            if ($N % 2 -eq 0) { $script:ActivitySnapshot.Active=@{Run=@{id=30;run_attempt=1;display_title='Integration batch #331, #400';head_sha=('a'*40)};Job=@{name='Build and test';status='in_progress';started_at=[datetime]::UtcNow.AddSeconds(-4).ToString('o');steps=@(@{name='Run actions/checkout@v4';status='in_progress'})}} }
            Update-View; [Windows.Forms.Application]::DoEvents()
        }
        $script:ActivitySnapshot=@{AtUtc=[datetime]::UtcNow;Active=$null;Count=0;Repo=$Repo}
        Update-View; [Windows.Forms.Application]::DoEvents()
        if ($OutDirectory) {
            [void][IO.Directory]::CreateDirectory($OutDirectory)
            $Bitmap=New-Object Drawing.Bitmap($Form.Width,$Form.Height)
            try { $Form.DrawToBitmap($Bitmap,(New-Object Drawing.Rectangle(0,0,$Form.Width,$Form.Height))); $Bitmap.Save((Join-Path $OutDirectory "layout-$(if ($Dark) { 'dark' } else { 'light' })-980x1100.png")) } finally { $Bitmap.Dispose() }
        }
        if ($Form.Width -ne 980 -or $Form.Height -ne 1100) { throw "Viewport was clamped to $($Form.Size); required 980x1100." }
        foreach ($Control in $Root.Controls) {
            $Cell=$Root.GetPositionFromControl($Control)
            if ($Cell.Column -ne 0 -or $Cell.Row -ne $Cells[$Control].Row) { throw "Control moved out of its stable row: $($Control.Text)" }
        }
        if ($Root.GetColumnWidths().Count -ne 1) { throw 'Overview unexpectedly grew extra columns.' }
        $Previous=$null
        foreach ($Control in $Root.Controls) {
            if (-not $Control.Visible) { continue }
            $Row=$Root.GetPositionFromControl($Control).Row; $Heights=$Root.GetRowHeights()
            if ($Previous -and $Control.Bounds.Top -lt $Previous.Bounds.Bottom) { throw "Overlap: $($Previous.GetType().Name) '$($Previous.Text)' bottom$($Previous.Bottom) overlaps '$($Control.Text)' top$($Control.Top); row$Row height$($Heights[$Row]), bounds$($Control.Bounds)" }
            if ($Control -is [Windows.Forms.Label] -and $Control.Height -lt $Control.GetPreferredSize((New-Object Drawing.Size($Control.Width,0))).Height) { throw "Clipped label: $($Control.Text) bounds$($Control.Bounds)" }
            $Previous=$Control
        }
        if ($JobBar.Width -lt 700 -or $StepBar.Width -lt 700) { throw "Progress bars collapsed: $($JobBar.Bounds) / $($StepBar.Bounds); columns $($Root.GetColumnWidths() -join ',')" }
        # An unchanged completed report and unchanged checkout handoff must not repaint
        # intermediate local values or toggle rows on each one-second status tick.
        foreach ($Handoff in @($false,$true)) {
            $script:ActivitySnapshot=@{AtUtc=[datetime]::UtcNow;Active=$null;Count=0;Repo=$Repo}
            if ($Handoff) { $script:ActivitySnapshot.Active=@{Run=@{id=30;run_attempt=1;display_title='Integration batch #331, #400';head_sha=('a'*40)};Job=@{name='Build and test';status='in_progress';started_at=[datetime]::UtcNow.AddSeconds(-4).ToString('o');steps=@(@{name='Run actions/checkout@v4';status='in_progress'})}} }
            Update-View; [Windows.Forms.Application]::DoEvents()
            $script:LayoutEvents=0; $script:VisibleEvents=0; $script:TextEvents=0
            $OnLayout={ $script:LayoutEvents++ }; $OnVisible={ $script:VisibleEvents++ }; $OnText={ $script:TextEvents++ }
            $Root.Add_Layout($OnLayout)
            $Watched=@($Root.Controls)+@($Queue,$QueueHead,$QueueHint)
            foreach ($Control in $Watched) { $Control.Add_VisibleChanged($OnVisible); $Control.Add_TextChanged($OnText) }
            try {
                foreach ($Tick in 1..20) { Update-View; [Windows.Forms.Application]::DoEvents() }
                if ($script:VisibleEvents -or $script:TextEvents -or $script:LayoutEvents) { throw "Unchanged status repainted (handoff=$Handoff): visible=$script:VisibleEvents text=$script:TextEvents layout=$script:LayoutEvents" }
            } finally {
                $Root.Remove_Layout($OnLayout)
                foreach ($Control in $Watched) { $Control.Remove_VisibleChanged($OnVisible); $Control.Remove_TextChanged($OnText) }
            }
        }
        if ($script:UiErrors.Count) { throw 'UI handler failed during repeated refreshes.' }
    }
    "PASS: tall 980x1100 layout, DPI $($Form.DeviceDpi), light/dark, repeated handoff/completed transitions, no text overlap, full-width bars, and zero text/visibility/layout events across unchanged idle/handoff ticks."
} finally {
    foreach ($Job in @($script:ActivityJob,$script:RefreshJob,$script:HealthJob)+@($script:RetiredJobs)) { if ($Job -and $Job.Shell) { try { $Job.Shell.Stop() } finally { $Job.Shell.Dispose() } } }
    if ($Form) { $Form.Dispose() }; if ($Timer) { $Timer.Dispose() }; if ($RefreshTimer) { $RefreshTimer.Dispose() }
    Remove-Item -LiteralPath $FixtureRoot -Recurse -Force
}
