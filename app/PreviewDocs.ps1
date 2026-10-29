# Renders the documentation images from the real Windows Forms interface, with synthetic data only: a temporary
# profile, a temporary status file and fixture releases. It never reads your saved projects, never opens the network
# and never calls GitHub, so it is safe to run on any machine.
# Usage: powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File .\app\PreviewDocs.ps1 [-OutDirectory .\docs\images]
# setup.png and projects.png come from TestLaneOnboarding.ps1, which renders the first-run screen it tests.
param([string]$OutDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) 'docs\images'))

$ErrorActionPreference = 'Stop'
$PreviewRoot = Join-Path ([IO.Path]::GetTempPath()) ('buildlane-preview-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory $PreviewRoot | Out-Null
try {
    $ProfilePath = Join-Path $PreviewRoot 'acme-desktop.json'
    $StatusPath = Join-Path $PreviewRoot 'status.json'
    @{
        name = 'Acme Desktop'; repo = 'acme/desktop'; branch = 'main'; workflow = 'build.yml'
        statusFile = $StatusPath; pauseFile = "$PreviewRoot\PAUSED"; runnerPath = 'C:\BuildRunner\Acme'
        drive = 'C'; serviceFilter = '*buildlane-preview-none*'; cachePort = 0; minimumFreeGB = 1; cleanBelowGB = 2
    } | ConvertTo-Json | Set-Content -LiteralPath $ProfilePath

    . "$PSScriptRoot\LaneApp.ps1" -Profile $ProfilePath -ProfileDirectory $PreviewRoot -SmokeTest -NoShow

    [void][IO.Directory]::CreateDirectory($OutDirectory)
    function Save-Shot([string]$Name) {
        Resize-Content; Set-PaneHeights
        [Windows.Forms.Application]::DoEvents()
        $Bitmap = New-Object Drawing.Bitmap($Form.Width, $Form.Height)
        try {
            $Form.DrawToBitmap($Bitmap, (New-Object Drawing.Rectangle(0, 0, $Form.Width, $Form.Height)))
            $Bitmap.Save((Join-Path $OutDirectory $Name))
        } finally { $Bitmap.Dispose() }
        Write-Output "wrote $Name"
    }

    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop()

    # A batch mid-test: the phase cursor, a real step line, the queue with each of the everyday states.
    @{
        updated = (Get-Date).ToString('o'); started = (Get-Date).AddMinutes(-27).ToString('o')
        what = 'batch #473, #478'; phase = 4; done = ''; runId = '40931784541'
        phases = @('Wait for the PC', 'Quick checks', 'Compile', 'Content imports', 'Tests', 'Package')
        step = 'Tests (Game set): RunM3bTest and RunM7bTest running'; progress = @{ done = 7; total = 18 }
        notes = @()
        queue = @(
            @{ number = 473; title = 'Rebuild the town maps through the lane'; state = 'in the batch, building now' },
            @{ number = 478; title = 'Keep the day summary shift line on data careers'; state = 'in the batch, building now' },
            @{ number = 331; title = 'Cut the road corridor into the City landscape'; state = 'held: v0.2.6 is being packaged first' },
            @{ number = 400; title = 'Rebuild and import HomeKit with receipts'; state = 'held: v0.2.6 is being packaged first; in v0.3.0' },
            @{ number = 411; title = 'Turn the boutique floor into a shop'; state = 'waits for the next batch: it conflicts with #408' },
            @{ number = 438; title = 'Hold the lane for local editor runs'; state = 'held: main is red' },
            @{ number = 472; title = 'Reuse successful content imports across lane splits'; state = 'held: after the v0.2.6 freeze' }
        )
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatusPath
    $Tabs.SelectedTab = $NowTab
    Update-View
    Save-Shot 'overview.png'

    # Releases: one queued release with its cut time, one open, one packaged, and the PR list below.
    $Future = (Get-Date).ToUniversalTime().AddHours(2).ToString('yyyy-MM-ddTHH:mm:ssZ')
    $Soon = (Get-Date).ToUniversalTime().AddDays(-3).ToString('yyyy-MM-ddTHH:mm:ssZ')
    Set-ReleaseView @{
        Open = @(
            @{ number = 12; title = 'v0.2.6'; description = "lane: queued`nlane-deadline: $Future" },
            @{ number = 13; title = 'v0.3.0'; description = 'lane: open' }
        )
        Closed = @(
            @{ number = 11; title = 'v0.2.5'; state = 'closed'; closed_at = $Soon; description = 'lane: packaged' }
        )
        Prs = @(
            @{ number = 473; title = 'Rebuild the town maps'; milestone = @{ number = 13; title = 'v0.3.0' }; labels = @(@{ name = 'ready' }) },
            @{ number = 478; title = 'Day summary shift line'; milestone = @{ number = 12; title = 'v0.2.6' }; labels = @(@{ name = 'ready' }, @{ name = 'fix-main' }) },
            @{ number = 471; title = 'Shorten the test path'; milestone = @{ number = 12; title = 'v0.2.6' }; labels = @(@{ name = 'ready' }) },
            @{ number = 400; title = 'Rebuild HomeKit imports'; milestone = @{ number = 13; title = 'v0.3.0' }; labels = @(@{ name = 'ready' }) },
            @{ number = 479; title = 'Fit the new braids'; labels = @() }
        )
    }
    $Tabs.SelectedTab = $RelTab
    Save-Shot 'releases.png'

    # Waiting and history use reported reasons and exact attempt records, never live GitHub data.
    $OriginalStatus=Get-Content -LiteralPath $StatusPath -Raw
    $WaitingStatus=$OriginalStatus | ConvertFrom-Json
    $WaitingStatus.phase=0; $WaitingStatus.step='Waiting for the runner'
    $WaitingStatus | Add-Member NoteProperty waitingReason 'A release package owns the runner' -Force
    $WaitingStatus | Add-Member NoteProperty blockingRunId '40931784000' -Force
    $WaitingStatus | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatusPath
    $Tabs.SelectedTab=$NowTab; Update-View; Save-Shot 'waiting.png'
    Set-Content -LiteralPath $StatusPath -Value $OriginalStatus; Update-View
    $HistoryFixture=@{id='40931784541';attempt=2;repo=$Repo;sha=('a'*40);what='';status='completed';conclusion='cancelled';beforeJobsSeconds=12;beforeJobsBasis='GitHub run start for this attempt';jobSpanSeconds=132;jobs=@(
        @{name='Windows build';result='cancelled';seconds=132;steps=@(
            @{name='Compile';result='success';seconds=62},
            @{name='Animation tests';result='cancelled';seconds=70},
            @{name='Package';result='skipped';seconds=$null}
        )}
    )}
    Set-LaneHistoryResult @{RunId='40931784541';What='batch #473, #478';Report='fixture'} @{History=$HistoryFixture}
    $Tabs.SelectedTab=$HistoryTab; Save-Shot 'timings.png'

    $HandoffOriginal=Get-Content -LiteralPath $StatusPath -Raw
    $HandoffStatus=$HandoffOriginal | ConvertFrom-Json
    $HandoffStatus.runId='10'; $HandoffStatus.what='Previous compile preview'; $HandoffStatus.done='failure'; $HandoffStatus.updated=[datetime]::UtcNow.AddMinutes(-5).ToString('o')
    $HandoffStatus | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StatusPath
    $Config | Add-Member NoteProperty activeJobs 'ci.yml|Build and test' -Force
    Initialize-LaneActivity
    $script:ActivitySnapshot=@{AtUtc=[datetime]::UtcNow;Repo=$Repo;Count=1;Active=@{Run=@{id=30;run_attempt=1;display_title='Integration batch #331, #400';head_sha=('a'*40)};Job=@{name='Build and test';status='in_progress';started_at=[datetime]::UtcNow.AddSeconds(-4).ToString('o');steps=@(@{name='Run actions/checkout@v4';status='in_progress'})}}}
    $Tabs.SelectedTab=$NowTab; Update-View; Save-Shot 'handoff.png'
    $Config.activeJobs=''; $script:ActivitySnapshot=$null
    Set-Content -LiteralPath $StatusPath -Value $HandoffOriginal

    # The same overview in dark mode, for the appearance setting. Light is the app's default, so the guide's images
    # are light; this one shows what the toggle does.
    Set-LaneTheme $true
    $Tabs.SelectedTab = $NowTab
    Update-View
    Save-Shot 'overview-dark.png'
    Set-LaneTheme $false

    $Form.Close()
} finally {
    if ($Form) { $Form.Dispose() }
    if (Get-Command Stop-LaneRefresh -ErrorAction SilentlyContinue) { Stop-LaneRefresh }
    $Resolved = [IO.Path]::GetFullPath($PreviewRoot)
    if ($Resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path $Resolved -Leaf) -like 'buildlane-preview-*' -and (Test-Path -LiteralPath $Resolved)) {
        Remove-Item -LiteralPath $Resolved -Recurse -Force
    }
}

