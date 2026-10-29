# Buildlane desktop client. Project configuration is supplied by -Profile.
# InstallLaneApp.ps1 builds the console-free GUI launcher and a desktop shortcut.
# Compatible status producers and release workflow contract: ../README.md.
param(
    [string]$Profile,
    [string]$ProfileDirectory = (Join-Path $env:APPDATA "Buildlane\Projects"),
    [string]$JsonFile, [string]$PauseFile, [string]$Runner, [string]$Repo,
    [switch]$SmokeTest,
    [switch]$NoShow,
    [string]$PreviewFile
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'LaneProfiles.ps1')
if (-not $Profile) {
    $CurrentFile=Join-Path $ProfileDirectory 'current.txt'
    if (Test-Path $CurrentFile) { $Profile=([IO.File]::ReadAllText($CurrentFile)).Trim() }
    if (-not $Profile -or -not (Test-Path -LiteralPath $Profile)) {
        $Profile=Join-Path $ProfileDirectory 'default.json'
        if (-not (Test-Path -LiteralPath $Profile)) { Save-LaneProfile $Profile (Read-LaneProfile (Join-Path $PSScriptRoot 'BuildLane.profile.json')) '' }
    }
}
$Config = Read-LaneProfile $Profile
if (-not $JsonFile) { $JsonFile = $Config.statusFile }
if (-not $PauseFile) { $PauseFile = $Config.pauseFile }
if (-not $Runner) { $Runner = $Config.runnerPath }
if (-not $Repo) { $Repo = $Config.repo }
$ProductName = [string]$Config.name
$Branch = [uri]::EscapeDataString([string]$Config.branch)
$Workflow = [uri]::EscapeDataString([string]$Config.workflow)
$Drive = [string]$Config.drive
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, Microsoft.VisualBasic
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not ('LaneTaskbar' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'LaneTaskbar.cs') }
[LaneTaskbar]::InitializeProcess()
[System.Windows.Forms.Application]::EnableVisualStyles()

# Colour-blind-safe palette (blue / orange / grey), and every state also has a glyph and words.
$Blue = [Drawing.Color]::FromArgb(0, 114, 178); $Orange = [Drawing.Color]::FromArgb(213, 94, 0)
$Green = [Drawing.Color]::FromArgb(0, 158, 115); $Grey = [Drawing.Color]::FromArgb(110, 110, 110)
$Ink = [Drawing.Color]::FromArgb(31, 42, 55); $Paper = [Drawing.Color]::FromArgb(245, 247, 250)
$FontBody = New-Object Drawing.Font("Segoe UI", 10)
$FontSmall = New-Object Drawing.Font("Segoe UI", 9)
$FontTitle = New-Object Drawing.Font("Segoe UI Semibold", 18)
$FontHead = New-Object Drawing.Font("Segoe UI Semibold", 11)
$FontMono = New-Object Drawing.Font("Consolas", 9)

$Form = New-Object Windows.Forms.Form
$Form.Text = "Buildlane | $ProductName"
$Form.Size = New-Object Drawing.Size(980, 800)
$Form.MinimumSize = New-Object Drawing.Size(780, 640)
$Form.BackColor = $Paper
$Form.Font = $FontBody
$Form.AutoScaleMode = "Dpi"
$Form.StartPosition = "CenterScreen"
# The BuildLane icon lives beside this script.
$IcoFile = Join-Path $PSScriptRoot "BuildLane.ico"
$Form.Icon = New-Object Drawing.Icon $IcoFile
$Form.ShowIcon = $true
$Form.ShowInTaskbar = $true
$Launcher = Join-Path $PSScriptRoot 'Buildlane.exe'
$RelaunchCommand = if (Test-Path -LiteralPath $Launcher) { '"' + $Launcher + '"' } else {
    '"' + (Join-Path $PSHOME 'powershell.exe') + '" -NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"'
}
$Form.Add_HandleCreated({ [LaneTaskbar]::ConfigureWindow($Form.Handle, $RelaunchCommand, $IcoFile) })
# Without a handler, an exception raised inside a WinForms event (a timer tick, a paint, a watcher) puts up the .NET
# unhandled-error dialog and waits for a click. In the app nobody sees which handler failed; in a test or on a headless
# CI runner that dialog is a hang with no output. Record it instead, so the screen checks can assert there were none.
$script:UiErrors = New-Object System.Collections.ArrayList
[Windows.Forms.Application]::add_ThreadException({
    param($Sender, $EventArgs)
    [void]$script:UiErrors.Add($EventArgs.Exception)
    if ($SmokeTest) { Write-Warning "UI error: $($EventArgs.Exception.Message)" }
    else { [void][Windows.Forms.MessageBox]::Show("The window hit a problem and kept going:`n`n$($EventArgs.Exception.Message)", "Buildlane", "OK", "Warning") }
})

# GitHub, for the buttons that change things: a token kept for this Windows user (DPAPI), or the gh CLI's.
function Reset-LaneCredential {
    $script:TokenFile = Join-Path $env:APPDATA "Buildlane\$($Repo -replace '/', '_')\github.token"
    if ($Repo -in @('ProjektPayne/LifeShare','Stonefletcher-Productions-LLC/LifeShare') -and -not (Test-Path $TokenFile)) {
        $LegacyToken=Join-Path $env:APPDATA 'LifeShareLane\github.token'
        if (Test-Path $LegacyToken) { $script:TokenFile=$LegacyToken }
    }
    $script:Token=$null; $script:TokenChecked=$false
}
Reset-LaneCredential
function Get-Token { if (-not $SmokeTest) { $script:Token } }
function Invoke-Gh([string]$Method, [string]$Path, $Body = $null) {
    if ($Config.setupRequired) { throw "Save your project settings first." }
    if ($Method -ne "GET") { $script:DataRevision++ }
    $T = Get-Token
    if (-not $T) { throw "Connect GitHub first (the button at the top right)." }
    $Req = @{ Method = $Method; Uri = "https://api.github.com/repos/$Repo$Path"; TimeoutSec = 20
              Headers = @{ Authorization = "Bearer $T"; Accept = "application/vnd.github+json"; "User-Agent" = "Buildlane-Desktop" } }
    if ($null -ne $Body) { $Req.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 5)); $Req.ContentType = "application/json; charset=utf-8" }
    Invoke-RestMethod @Req
}
# Windows PowerShell hands a JSON array back as one object; this lists its items.
function Get-GhList([string]$Path) { $R = Invoke-Gh GET $Path; @($R | ForEach-Object { $_ }) }
function Start-Lane([string]$Kind="lane") { try { Start-LaneCommand -Kind $Kind -Path "/actions/workflows/$Workflow/dispatches" -Body @{ ref = $Config.branch; inputs = @{ task = "lane" } } } catch { Show-Error "The lane could not be started. The local pause state is shown above." $_ } }
function Show-Error([string]$What, $Err) { [void][Windows.Forms.MessageBox]::Show("$What`n`n$($Err.Exception.Message)", "Buildlane", "OK", "Warning") }

# Top bar (every tab): pause everything, and the GitHub connection.
$Outer = New-Object Windows.Forms.TableLayoutPanel
$Outer.Dock = "Fill"; $Outer.ColumnCount = 1; $Outer.RowCount = 2
[void]$Outer.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$Outer.RowStyles.Add((New-Object Windows.Forms.RowStyle("Percent", 100)))
$Form.Controls.Add($Outer)
$TopBar = New-Object Windows.Forms.FlowLayoutPanel
$TopBar.AutoSize = $true; $TopBar.Dock = "Top"; $TopBar.Padding = New-Object Windows.Forms.Padding(20, 12, 20, 12)
[void]$Outer.Controls.Add($TopBar, 0, 0)
$Tabs = New-Object Windows.Forms.TabControl
$Tabs.Dock = "Fill"; $Tabs.Font = $FontBody; $Tabs.Padding = New-Object Drawing.Point(14, 5)
[void]$Outer.Controls.Add($Tabs, 0, 1)
$NowTab = New-Object Windows.Forms.TabPage("Overview"); $NowTab.BackColor = $Paper
$RelTab = New-Object Windows.Forms.TabPage("Releases"); $RelTab.BackColor = $Paper
$SystemTab = New-Object Windows.Forms.TabPage("System"); $SystemTab.BackColor = $Paper
$Tabs.TabPages.AddRange(@($NowTab, $RelTab, $SystemTab))
$SystemRoot = New-Object Windows.Forms.TableLayoutPanel
$SystemRoot.Dock = 'Fill'; $SystemRoot.ColumnCount = 1; $SystemRoot.AutoScroll = $true
$SystemRoot.Padding = New-Object Windows.Forms.Padding(24)
$SystemTab.Controls.Add($SystemRoot)

$Root = New-Object Windows.Forms.TableLayoutPanel
$Root.Dock = "Fill"; $Root.Padding = New-Object Windows.Forms.Padding(24); $Root.ColumnCount = 1; $Root.AutoScroll = $true
# The queue is the one part of the overview that grows with the project, so it gets its own resizable pane instead of a
# fixed 180 px box: drag the divider, and the height is remembered for this Windows user.
$QueuePanel = New-Object Windows.Forms.TableLayoutPanel
$QueuePanel.Dock = "Fill"; $QueuePanel.ColumnCount = 1; $QueuePanel.RowCount = 3
$QueuePanel.Padding = New-Object Windows.Forms.Padding(24, 4, 24, 8)
[void]$QueuePanel.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$QueuePanel.RowStyles.Add((New-Object Windows.Forms.RowStyle("Percent", 100)))
[void]$QueuePanel.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
$NowSplit = New-Object Windows.Forms.SplitContainer
$NowSplit.Dock = "Fill"; $NowSplit.Orientation = "Horizontal"; $NowSplit.SplitterWidth = 12
$NowSplit.Size = New-Object Drawing.Size(900, 620)   # a real size before the minimums, which are checked against it
$NowSplit.Panel1MinSize = 300; $NowSplit.Panel2MinSize = 120
$LogSplit = New-Object Windows.Forms.SplitContainer
$LogSplit.Dock = 'Fill'; $LogSplit.Orientation = 'Horizontal'; $LogSplit.SplitterWidth = 12
$LogSplit.Size = New-Object Drawing.Size(900, 500)
$LogSplit.Panel1MinSize = 200; $LogSplit.Panel2MinSize = 90
$LogSplit.Panel1.Controls.Add($Root); $LogSplit.Panel2Collapsed = $true
$NowSplit.Panel1.Controls.Add($LogSplit)
$NowSplit.Panel2.Controls.Add($QueuePanel)
$NowTab.Controls.Add($NowSplit)
$NowSplit.AccessibleName = 'Overview and queue divider'; $NowSplit.AccessibleDescription = 'Drag up or down to resize the queue.'
$NowSplit.Panel1.AccessibleName = 'Build overview'; $NowSplit.Panel2.AccessibleName = 'Queue'
$LogSplit.AccessibleName = 'Step log divider'; $LogSplit.AccessibleDescription = 'Drag up or down to resize the step log.'
$LogSplit.Panel1.AccessibleName = 'Build overview'; $LogSplit.Panel2.AccessibleName = 'Step log'
function Add-Row($Control, [int]$Top = 4, $Panel = $Root) {
    $Control.Margin = New-Object Windows.Forms.Padding(0, $Top, 0, 4)
    $Control.Dock = "Top"
    if ($Panel -eq $Root) {
        [void]$Panel.RowStyles.Add((New-Object Windows.Forms.RowStyle('AutoSize')))
    }
    [void]$Panel.Controls.Add($Control)
    $Control
}
function New-Label([string]$Text = "", $Font = $FontBody, $Color = $Ink) {
    $L = New-Object Windows.Forms.Label
    $L.Text = $Text; $L.Font = $Font; $L.ForeColor = $Color; $L.AutoSize = $true; $L.MaximumSize = New-Object Drawing.Size(880, 0)
    $L
}
function New-Bar {
    $B = New-Object Windows.Forms.ProgressBar
    $B.Height = 8; $B.Minimum = 0; $B.Maximum = 1000; $B.Style = "Continuous"
    $B
}

# What is building, and the result of the last build.
$Title = Add-Row (New-Label "Reading the lane's status..." $FontTitle) 0
$Sub = Add-Row (New-Label "" $FontSmall $Grey) 0

# The whole job, then the step it is on.
$JobLabel = Add-Row (New-Label "" $FontHead) 12
$JobBar = Add-Row (New-Bar)
$StepLabel = Add-Row (New-Label "")
$StepBar = Add-Row (New-Bar)
$Time = Add-Row (New-Label "" $FontSmall $Grey)

# The steps, ticked off.
$StepsHead = Add-Row (New-Label "Steps" $FontHead) 12
$StepsBox = Add-Row (New-Object Windows.Forms.FlowLayoutPanel)
$StepsBox.AutoSize = $true; $StepsBox.FlowDirection = "LeftToRight"; $StepsBox.WrapContents = $true

$Notes = Add-Row (New-Label "" $FontSmall $Orange)

# The queue. Its head and hint sit outside the scroller so the list itself can fill whichever height the divider gives it.
$QueueHead = New-Label "Queue" $FontHead
$QueueHead.Margin = New-Object Windows.Forms.Padding(0, 8, 0, 4)
[void]$QueuePanel.Controls.Add($QueueHead, 0, 0)
$Queue = New-Object Windows.Forms.ListView
$Queue.Dock = "Fill"; $Queue.Margin = New-Object Windows.Forms.Padding(0)
$Queue.View = "Details"; $Queue.FullRowSelect = $true; $Queue.HeaderStyle = "Nonclickable"; $Queue.Font = $FontSmall
[void]$Queue.Columns.Add("PR", 60); [void]$Queue.Columns.Add("Title", 380); [void]$Queue.Columns.Add("State", 260)
[void]$QueuePanel.Controls.Add($Queue, 0, 1)
$Queue.Add_DoubleClick({
    if ($Queue.SelectedItems.Count -and $Queue.SelectedItems[0].Tag) {
        $N = $Queue.SelectedItems[0].Tag
        $Base = "https://github.com/$Repo"
        if ($Base) { Start-Process "$Base/pull/$N" }
    }
})
$QueueHint = New-Label "Drag the divider above to resize the queue. Double-click a PR to open it." $FontSmall $Grey
$QueueHint.Margin = New-Object Windows.Forms.Padding(0, 4, 0, 0)
[void]$QueuePanel.Controls.Add($QueueHint, 0, 2)

# The PC.
$PcHead = Add-Row (New-Label "Machine health" $FontTitle) 0 $SystemRoot
$DiskLabel = Add-Row (New-Label "") 16 $SystemRoot
$DiskBar = Add-Row (New-Bar) 4 $SystemRoot
$Health = Add-Row (New-Label "") 16 $SystemRoot
$ProjectInfo=Add-Row (New-Label "Project: $Repo`nStatus: $JsonFile`nRunner: $Runner" $FontSmall $Grey) 24 $SystemRoot

# Buttons.
$Buttons = Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 12
$Buttons.AutoSize = $true
function New-Button([string]$Text, [scriptblock]$OnClick, $Panel = $Buttons) {
    $B = New-Object Windows.Forms.Button
    $B.FlatStyle = 'Flat'; $B.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(214, 221, 230)
    $B.BackColor = [Drawing.Color]::White; $B.ForeColor = $Ink; $B.Cursor = 'Hand'
    $B.Margin = New-Object Windows.Forms.Padding(0, 0, 8, 8)
    $B.Text = $Text; $B.AutoSize = $true; $B.Padding = New-Object Windows.Forms.Padding(8, 4, 8, 4); $B.Font = $FontBody
    $B.Add_Click($OnClick)
    [void]$Panel.Controls.Add($B)
    $B
}

$Brand = New-Label "Buildlane  /  $ProductName" $FontHead
$Brand.Margin = New-Object Windows.Forms.Padding(0, 10, 24, 0)
[void]$TopBar.Controls.Add($Brand)
# Pause everything: no new build starts (batches, the release build, the nightly, a PR's own build), and the lane
# starts nothing. The build running now can be stopped too (it asks). Resume lets the lane go on at once.
$PauseButton = New-Button "Pause builds" {
    if ($Config.setupRequired) { return }
    if (Test-Path $PauseFile) {
        try { Remove-Item $PauseFile -ErrorAction Stop }
        catch { Show-Error "Could not resume (removing $PauseFile)." $_; return }
        Update-View
        Start-Lane -Kind resume
    }
    else {
        try { Set-Content $PauseFile "Paused $(Get-Date -Format s) by $env:USERNAME from the Build Lane app" -ErrorAction Stop }
        catch { Show-Error "Could not pause (writing $PauseFile)." $_; return }
        $script:CommandState=$null
        Update-View
        $S = $script:State
        if ($S -and -not $S.done -and $S.runId) {
            $A = [Windows.Forms.MessageBox]::Show("Paused: nothing new will start.`n`nA build is running now ($($S.what)). Stop it too?`n`nYes stops it now. No lets it finish.", "Pause everything", "YesNo", "Question")
            if ($A -eq "Yes") {
                try { Start-LaneCommand -Kind cancel -Path "/actions/runs/$($S.runId)/cancel" } catch { Show-Error "Paused, but the running build could not be stopped." $_ }
            }
        }
    }
    Update-View
} $TopBar
$PauseState = New-Object Windows.Forms.Label
$PauseState.AutoSize = $true; $PauseState.Font = $FontBody; $PauseState.Margin = New-Object Windows.Forms.Padding(8, 10, 24, 0)
[void]$TopBar.Controls.Add($PauseState)
$GhButton = New-Button "Connect GitHub" { Show-Connect } $TopBar

# Connect GitHub: a fine-grained token for the selected repository, kept for this Windows user only.
function Show-Connect {
    $D = New-Object Windows.Forms.Form
    $D.Text = "Connect GitHub"; $D.Size = New-Object Drawing.Size(560, 330); $D.StartPosition = "CenterParent"; $D.Font = $FontBody; $D.BackColor = $Paper
    $D.FormBorderStyle = "FixedDialog"; $D.MaximizeBox = $false; $D.MinimizeBox = $false
    $L = New-Object Windows.Forms.Label
    $L.Location = New-Object Drawing.Point(16, 14); $L.Size = New-Object Drawing.Size(510, 150)
    $L.Text = "The Pause, Stop and Releases buttons change things on GitHub, so the app needs a token once.`n`n1. Open the link below and sign in.`n2. Repository access: Only select repositories, $Repo.`n3. Permissions: Actions, Issues and Pull requests, all Read and write.`n4. Generate, copy the token, paste it here.`n`nIt is kept for your Windows user only."
    $Link = New-Object Windows.Forms.LinkLabel
    $Link.Text = "Make a token on GitHub"; $Link.Location = New-Object Drawing.Point(16, 170); $Link.AutoSize = $true
    $Link.Add_LinkClicked({ Start-Process "https://github.com/settings/personal-access-tokens/new" })
    $Box = New-Object Windows.Forms.TextBox
    $Box.Location = New-Object Drawing.Point(16, 200); $Box.Size = New-Object Drawing.Size(510, 26); $Box.UseSystemPasswordChar = $true
    $Ok = New-Object Windows.Forms.Button
    $Ok.Text = "Connect"; $Ok.Location = New-Object Drawing.Point(330, 240); $Ok.AutoSize = $true
    $Forget = New-Object Windows.Forms.Button
    $Forget.Text = "Disconnect"; $Forget.Location = New-Object Drawing.Point(430, 240); $Forget.AutoSize = $true
    $Ok.Add_Click({
        $T = $Box.Text.Trim()
        if (-not $T) { return }
        $Old = $script:Token; Stop-LaneRefresh; $script:NextRefresh=[datetime]::MinValue; $script:Token = $T
        try {
            Invoke-Gh GET "" | Out-Null
            New-Item -ItemType Directory -Force (Split-Path $TokenFile) | Out-Null
            ConvertTo-SecureString $T -AsPlainText -Force | ConvertFrom-SecureString | Set-Content $TokenFile
            $D.Close()
        }
        catch { $script:Token = $Old; Show-Error "That token does not open $Repo." $_ }
    })
    $Forget.Add_Click({ Stop-LaneRefresh; Remove-Item $TokenFile -ErrorAction SilentlyContinue; $script:Token = $null; $script:NextRefresh=[datetime]::MinValue; $D.Close() })
    $D.Controls.AddRange(@($L, $Link, $Box, $Ok, $Forget))
    $D.AcceptButton = $Ok
    [void]$D.ShowDialog($Form)
    Update-View; Update-Releases
}
[void](New-Button "Open live log" { if ($script:State.runUrl) { Start-Process $script:State.runUrl } })
[void](New-Button "Open status page" { if ($script:State.issueUrl) { Start-Process $script:State.issueUrl } })
$LogButton = New-Button "Show step log" {
    if (-not $LogSplit.Panel2Collapsed) {
        $script:LaneLayout['log'] = $LogSplit.Height - $LogSplit.SplitterWidth - $LogSplit.SplitterDistance
    }
    $LogSplit.Panel2Collapsed = -not $LogSplit.Panel2Collapsed
    $LogButton.Text = if ($LogSplit.Panel2Collapsed) { "Show step log" } else { "Hide step log" }
    if (-not $LogSplit.Panel2Collapsed) { Set-PaneHeights 'log' }
    Update-View
}
$LogBox = New-Object Windows.Forms.TextBox
$LogBox.Dock = 'Fill'; $LogBox.Multiline = $true; $LogBox.ReadOnly = $true
$LogBox.ScrollBars = 'Both'; $LogBox.Font = $FontMono; $LogBox.WordWrap = $false
$LogSplit.Panel2.Controls.Add($LogBox)

function Format-Span([TimeSpan]$T) {
    if ($T.TotalMinutes -lt 1) { return "under a minute" }
    if ($T.TotalHours -lt 1) { return "$([int][math]::Floor($T.TotalMinutes)) min" }
    "$([int][math]::Floor($T.TotalHours)) h $($T.Minutes) min"
}
function Set-Bar($Bar, [double]$Fraction) {
    if ($Fraction -lt 0) { $Bar.Style = "Marquee"; $Bar.MarqueeAnimationSpeed = 40; return }
    $Bar.Style = "Continuous"; $Bar.Value = [int]([math]::Max(0.0, [math]::Min(1.0, $Fraction)) * 1000)
}

$script:State = $null
function Update-View {
    $Ready = -not $Config.setupRequired
    $PauseButton.Enabled=($Ready -and -not $script:CommandJob -and -not $script:MoveJob); $GhButton.Enabled=($Ready -and -not $script:MoveJob -and -not $script:CommandJob)
    $NowTab.Enabled=$Ready; $RelTab.Enabled=$Ready; $SystemTab.Enabled=$Ready
    if (-not $Ready) { $PauseState.Text="Set up your first project"; $Tabs.SelectedTab=$ProjectTab; return }
    # A read that lands mid-write keeps the last good state instead of flashing "no build".
    $ReadWarning = ''
    if (Test-Path $JsonFile) {
        try {
            $Candidate = Get-Content $JsonFile -Raw -ErrorAction Stop | ConvertFrom-Json
            if (-not $Candidate.updated -or -not $Candidate.started) { throw 'Status timestamps are missing.' }
            [void][datetime]::Parse($Candidate.updated); [void][datetime]::Parse($Candidate.started)
            $script:State = $Candidate
        } catch { $ReadWarning = 'Status could not be read. Showing the last valid update.' }
    }
    else { $script:State = $null }
    $S = $script:State
    $Notes.Text = ''; $Sub.ForeColor = $Grey
    $Paused = Test-Path $PauseFile
    $PauseButton.Text = if ($Paused) { "Resume builds" } else { "Pause builds" }
    $PauseState.Text = if ($Paused) { "⏸ PAUSED: nothing new starts" } else { "Builds allowed" }
    $PauseState.ForeColor = if ($Paused) { $Orange } else { $Grey }
    Update-LaneCommandFeedback
    $GhButton.Text = if (Get-Token) { "✓ GitHub connected" } else { "Connect GitHub" }
    # A status written by something other than the lane (a PR's quick checks share the same runner and file) can carry a
    # one-entry phase list. That is not a job shape: showing "Step 1 of 1" for a running batch is worse than falling
    # back to the standard phases, so require a real multi-phase list.
    $Phases = if ($S -and $S.phases -and @($S.phases).Count -ge 2) { @($S.phases) } else { @("Wait for the PC", "Quick checks", "Compile", "Content imports", "Tests", "Package") }

    if (-not $S) {
        $Title.Text = "No build has reported yet"; $Title.ForeColor = $Grey
        $Sub.Text = "Waiting for a status file. Check the runner paths in Projects, or open the setup guide."
        $JobLabel.Text = ""; $StepLabel.Text = ""; $Time.Text = ""; Set-Bar $JobBar 0; Set-Bar $StepBar 0
    }
    else {
        $Updated = [datetime]::Parse($S.updated).ToLocalTime()
        $Started = [datetime]::Parse($S.started).ToLocalTime()
        $Phase = [int]$S.phase
        if ($S.done) {
            $Ok = $S.done -eq "success"
            $Title.Text = if ($Ok) { "✓ $($S.what): passed" } elseif ($S.done -eq "cancelled") { "○ $($S.what): cancelled" } elseif ($S.done -eq 'failure') { "✗ $($S.what): FAILED" } else { "○ $($S.what): $($S.done)" }
            $Title.ForeColor = if ($Ok) { $Green } elseif ($S.done -eq "cancelled") { $Grey } else { $Orange }
            $Sub.Text = "$($S.what), finished $($Updated.ToString('HH:mm')) after $(Format-Span ($Updated - $Started)). $(if ($Ok) { 'Check the queue below for upcoming work.' } else { 'Open the live log for details.' })"
            $JobLabel.Text = "Run ended: $($S.done)"; Set-Bar $JobBar ($(if ($Ok) { 1 } else { 0 }))
            $StepLabel.Text = ""; Set-Bar $StepBar ($(if ($Ok) { 1 } else { 0 }))
            $Time.Text = ""
        }
        else {
            $Title.Text = if ($Phase -eq 0) { "Waiting to build $($S.what)" } else { "▶ Building $($S.what -replace '^batch ', 'batch ')" }; $Title.ForeColor = $Blue
            $Quiet = (Get-Date) - $Updated
            $Sub.Text = if ($Quiet.TotalMinutes -ge 10) { "⚠ No update from the build for $(Format-Span $Quiet). Check the live log for activity." } else { "Updated $($Updated.ToString('HH:mm:ss'))" }
            $Sub.ForeColor = if ($Quiet.TotalMinutes -ge 10) { $Orange } else { $Grey }
            $Frac = 0.0
            if ($S.progress -and [int]$S.progress.total -gt 0) { $Frac = [double]$S.progress.done / [double]$S.progress.total }
            $N = [math]::Max(0, [math]::Min($Phase, $Phases.Count - 1))
            $JobLabel.Text = "Step $($N + 1) of $($Phases.Count): $($Phases[$N])"
            Set-Bar $JobBar (($N + $Frac) / $Phases.Count)
            $StepLabel.Text = "$($S.step)$(if ($S.detail) { "  ·  $($S.detail)" })"
            if ($Paused -and $N -eq 0) {
                $JobLabel.Text = "Step 1 of $($Phases.Count): ⏸ Paused, waiting for Resume"
                $Sub.Text = "This build waits at the start while the lane is paused. Press Resume (top left) to let it go on."; $Sub.ForeColor = $Orange
            }
            if ($S.progress -and [int]$S.progress.total -gt 0) { Set-Bar $StepBar $Frac } else { Set-Bar $StepBar -1 }
            $Time.Text = "Running for $(Format-Span ((Get-Date) - $Started)) (started $($Started.ToString('HH:mm')), your time)"
        }
        $Identity = if ($S.runId) { "Run #$($S.runId)" } else { 'Run ID not reported' }
        $Sub.Text += "`nLatest local report: $Identity. This result applies only to $($S.what), not other builds or releases."
        $Notes.Text = (@($S.notes) | Select-Object -Last 3 | ForEach-Object { "⚠ Watchdog, $_" }) -join "`n"
    }

    if ($ReadWarning) { $Sub.Text = $ReadWarning; $Sub.ForeColor = $Orange }

    # Steps, ticked off. Only rebuilt when the phase cursor or its result actually moves: this used to dispose and
    # recreate every label on each 3-second tick, which flickered and burned CPU while nothing changed.
    $StepsKey = "$(if ($S) { "$($S.phase)|$($S.done)" } else { 'none' })|$($Phases -join '|')"
    if ($StepsKey -ne $script:StepsKey) {
        $script:StepsKey = $StepsKey
        $StepsBox.SuspendLayout(); while ($StepsBox.Controls.Count) { $StepsBox.Controls[0].Dispose() }
        for ($i = 0; $i -lt $Phases.Count; $i++) {
            $Cur = if ($S) { [int]$S.phase } else { -1 }
            # A phase index is a cursor, not a receipt: earlier steps may have been skipped.
            # Legacy producers report no per-phase conclusions. Never turn them all green.
            $Glyph, $Color = if ($i -eq $Cur -and $S.done -eq "success") { "✓", $Green }
                             elseif ($i -eq $Cur -and $S.done -eq "failure") { "✗", $Orange }
                             elseif ($i -eq $Cur -and -not $S.done) { "▶", $Blue }
                             else { "○", $Grey }
            $PhaseNote = if ($i -eq $Cur -and $S.done -eq 'cancelled') { ' (cancelled)' }
                         elseif ($S.done -and $i -gt $Cur) { ' (not reported)' }
                         elseif ($i -lt $Cur) { ' (completed or skipped)' }
                         else { '' }
            $L = New-Label "$Glyph $($Phases[$i])$PhaseNote   " $FontBody $Color
            [void]$StepsBox.Controls.Add($L)
        }
        $StepsBox.ResumeLayout()
    }

    # Queue. Same rule: rebuild only when the list itself changes, so hovering or scrolling is not interrupted.
    $QueueKey = (@($(if ($S) { $S.queue })) | ForEach-Object { "$($_.number)|$($_.state)|$($_.title)" }) -join "`n"
    if ($QueueKey -ne $script:QueueKey) {
        $script:QueueKey = $QueueKey
        $SelectedPr = if ($Queue.SelectedItems.Count) { $Queue.SelectedItems[0].Tag } else { $null }
        $Queue.BeginUpdate(); $Queue.Items.Clear()
        foreach ($Q in @(if ($S) { $S.queue })) {
            $It = New-Object Windows.Forms.ListViewItem("#$($Q.number)")
            $QueueState = [string]$Q.state
            $ReleaseWait = $false
            if ($QueueState -match '^held: (.+?) is being packaged first(?<suffix>;.*)?$') {
                $QueueState = "Waiting for $($Matches[1]) to finish$($Matches['suffix'])"
                $ReleaseWait = $true
            } elseif ($QueueState -match '^held: after the (.+) freeze$') {
                $QueueState = "Waiting for the $($Matches[1]) release freeze to end"
                $ReleaseWait = $true
            }
            [void]$It.SubItems.Add([string]$Q.title); [void]$It.SubItems.Add($QueueState); $It.Tag = $Q.number
            if ($ReleaseWait) { $It.ForeColor = $Grey; $It.Text = "○ #$($Q.number)" }
            elseif ($Q.state -match 'building now|in the batch') { $It.ForeColor = $Blue; $It.Text = "▶ #$($Q.number)" }
            elseif ($Q.state -match 'held|conflict') { $It.ForeColor = $Orange }
            elseif ($Q.state -match '^waiting') { $It.ForeColor = $Grey }
            [void]$Queue.Items.Add($It)
            if ($SelectedPr -eq $Q.number) { $It.Selected = $true }
        }
        if (-not $Queue.Items.Count) { [void]$Queue.Items.Add((New-Object Windows.Forms.ListViewItem("Nothing waiting."))) }
        $Queue.EndUpdate()
    }
    $QueueHead.Text = "Queue ($(@(if ($S) { $S.queue }).Count))"

    # Render the last machine snapshot; system queries run outside the Forms thread.
    Start-LaneHealth
    $Machine=$script:CachedMachine
    if ($Machine -and -not $Machine.DiskError) {
        Set-Bar $DiskBar $Machine.UsedFraction
        $DiskLabel.Text="Drive ${Drive}: $($Machine.FreeGB) GB free of $($Machine.TotalGB) GB"
        if ($Machine.FreeGB -lt $Config.minimumFreeGB) { $DiskLabel.Text+=". Below the configured $($Config.minimumFreeGB) GB minimum." }
        elseif ($Machine.FreeGB -lt $Config.cleanBelowGB) { $DiskLabel.Text+=' (below the configured cleanup threshold)' }
        $DiskLabel.ForeColor=if ($Machine.FreeGB -lt $Config.minimumFreeGB) { $Orange } else { $Ink }
    } else { $DiskLabel.Text=if ($Machine) { $Machine.DiskError } else { 'Checking machine health...' } }
    $P = if ($Paused) { "⏸ PAUSED: nothing new starts until you press Resume" } else { "✓ Builds allowed" }
    $Health.Text=(@(if ($Machine) { $Machine.Health } else { 'Checking machine health...' })+@($P)) -join "`n"
    $Health.ForeColor=if ($Paused) { $Orange } else { $Ink }

    # The running step's log, from the runner's live page.
    if (-not $LogSplit.Panel2Collapsed) {
        $Page = Get-ChildItem (Join-Path $Runner "_diag\pages") -Filter *.log -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        $LogBox.Text = if ($Page -and $Page.LastWriteTime -gt (Get-Date).AddMinutes(-30)) {
            (Get-Content $Page.FullName -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { $_ -replace '^\S+Z ', '' }) -join "`r`n"
        } else { "No step is running right now." }
        $LogBox.SelectionStart = $LogBox.Text.Length; $LogBox.ScrollToCaret()
    }
}

# Releases: GitHub milestones. The line "lane: <state>" in a milestone's description is what the lane reads (open,
# queued, packaging, packaged, failed); the rest of the description is left alone.
# Two panes, because both lists grow: drag the divider to give the PRs or the releases more room.
$RelRoot = New-Object Windows.Forms.TableLayoutPanel
$RelRoot.Dock = "Fill"; $RelRoot.Padding = New-Object Windows.Forms.Padding(16); $RelRoot.ColumnCount = 1; $RelRoot.RowCount = 5
[void]$RelRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$RelRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$RelRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("Percent", 100)))
[void]$RelRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$RelRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
$PrRoot = New-Object Windows.Forms.TableLayoutPanel
$PrRoot.Dock = "Fill"; $PrRoot.Padding = New-Object Windows.Forms.Padding(16, 4, 16, 16); $PrRoot.ColumnCount = 1; $PrRoot.RowCount = 4
[void]$PrRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$PrRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
[void]$PrRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("Percent", 100)))
[void]$PrRoot.RowStyles.Add((New-Object Windows.Forms.RowStyle("AutoSize")))
$RelSplit = New-Object Windows.Forms.SplitContainer
$RelSplit.Dock = "Fill"; $RelSplit.Orientation = "Horizontal"; $RelSplit.SplitterWidth = 12
$RelSplit.Size = New-Object Drawing.Size(900, 620)   # a real size before the minimums, which are checked against it
$RelSplit.Panel1MinSize = 230; $RelSplit.Panel2MinSize = 150
$RelSplit.Panel1.Controls.Add($RelRoot)
$RelSplit.Panel2.Controls.Add($PrRoot)
$RelTab.Controls.Add($RelSplit)
$RelSplit.AccessibleName = 'Releases and pull requests divider'; $RelSplit.AccessibleDescription = 'Drag up or down to resize the lists.'
$RelSplit.Panel1.AccessibleName = 'Releases'; $RelSplit.Panel2.AccessibleName = 'Pull requests'
$RelTitle = New-Label "Releases" $FontHead
$RelTitle.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 4)
[void]$RelRoot.Controls.Add($RelTitle, 0, 0)
[void]$RelRoot.Controls.Add((New-Label "Group changes into a release, then queue its build. Other releases wait while it packages." $FontSmall $Grey), 0, 1)
$RelList = New-Object Windows.Forms.ListView
$RelList.Dock = "Fill"; $RelList.Margin = New-Object Windows.Forms.Padding(0, 4, 0, 4)
$RelList.View = "Details"; $RelList.FullRowSelect = $true; $RelList.MultiSelect = $false; $RelList.HideSelection = $false; $RelList.HeaderStyle = "Nonclickable"
[void]$RelList.Columns.Add("Release", 110); [void]$RelList.Columns.Add("State", 230); [void]$RelList.Columns.Add("PRs", 140); [void]$RelList.Columns.Add("Notes", 200)
[void]$RelRoot.Controls.Add($RelList, 0, 2)
$RelButtons = New-Object Windows.Forms.FlowLayoutPanel
$RelButtons.AutoSize = $true; $RelButtons.Dock = "Top"
[void]$RelRoot.Controls.Add($RelButtons, 0, 3)
$RelMsg = New-Label "" $FontSmall $Grey
$RelMsg.Margin = New-Object Windows.Forms.Padding(0, 4, 0, 0)
[void]$RelRoot.Controls.Add($RelMsg, 0, 4)
$PrHead = New-Label "Pull requests" $FontHead
$PrHead.Margin = New-Object Windows.Forms.Padding(0, 0, 0, 2)
[void]$PrRoot.Controls.Add($PrHead, 0, 0)
[void]$PrRoot.Controls.Add((New-Label "Tick PRs, pick a release, press Move. Double-click a PR to open it." $FontSmall $Grey), 0, 1)
$PrList = New-Object Windows.Forms.ListView
$PrList.Dock = "Fill"; $PrList.Margin = New-Object Windows.Forms.Padding(0, 4, 0, 4)
$PrList.View = "Details"; $PrList.FullRowSelect = $true; $PrList.CheckBoxes = $true; $PrList.HeaderStyle = "Nonclickable"; $PrList.Font = $FontSmall
[void]$PrList.Columns.Add("PR", 70); [void]$PrList.Columns.Add("Title", 380); [void]$PrList.Columns.Add("Release", 90); [void]$PrList.Columns.Add("Lane", 120)
[void]$PrRoot.Controls.Add($PrList, 0, 2)
$PrList.Add_DoubleClick({ if ($PrList.SelectedItems.Count) { Start-Process "https://github.com/$Repo/pull/$($PrList.SelectedItems[0].Tag)" } })
$MoveBar = New-Object Windows.Forms.FlowLayoutPanel
$MoveBar.AutoSize = $true; $MoveBar.Dock = "Top"
[void]$PrRoot.Controls.Add($MoveBar, 0, 3)
$MoveTo = New-Object Windows.Forms.ComboBox
$MoveTo.DropDownStyle = "DropDownList"; $MoveTo.Width = 160; $MoveTo.Margin = New-Object Windows.Forms.Padding(0, 6, 8, 0)

$script:Milestones = @()
function Get-RelState($M) { if ("$($M.description)" -match '(?m)^lane:\s*(\w+)') { $Matches[1].ToLower() } else { "open" } }
# A queued release is a cut with an end: the lane writes "lane-deadline: <ISO 8601>" into the milestone description and
# packages whatever landed by then. Read it so the only visible symptom is not a release that never finishes.
function Get-CutDeadline($M) {
    if ("$($M.description)" -match '(?im)^lane-deadline:\s*(\S+)\s*$') {
        $Parsed = [datetimeoffset]::MinValue
        if ([datetimeoffset]::TryParse($Matches[1], [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$Parsed)) {
            return $Parsed.LocalDateTime
        }
    }
    return $null
}
function Format-RelState($M, [string]$St) {
    if ($St -eq "closed") { return "✓ closed" }
    if ($St -eq "queued") {
        $Cut = Get-CutDeadline $M
        if ($Cut) {
            return $(if ($Cut -le (Get-Date)) { "▶ queued: cut reached, packaging next" } else { "▶ queued: ships by $($Cut.ToString('HH:mm'))" })
        }
    }
    if ($StateWords[$St]) { return $StateWords[$St] }
    return $St
}
function Get-VerKey($M) { $N = @([regex]::Matches("$($M.title)", '\d+') | ForEach-Object { [int]$_.Value }) + @(0, 0, 0); ($N[0] * 1000000) + ($N[1] * 1000) + $N[2] }
function Set-RelState($M, [string]$St) {
    $Rest = ("$($M.description)" -replace '(?m)^lane:.*$', '').Trim()
    $Desc = "lane: $St$(if ($Rest) { "`n`n$Rest" })"
    Invoke-Gh PATCH "/milestones/$($M.number)" @{ description = $Desc } | Out-Null
}
function Get-SelectedRelease {
    if (-not $RelList.SelectedItems.Count) { [void][Windows.Forms.MessageBox]::Show("Pick a release in the list first.", "Releases"); return $null }
    $N = $RelList.SelectedItems[0].Tag
    $script:Milestones | Where-Object { $_.number -eq $N } | Select-Object -First 1
}
$StateWords = @{ open = "○ open"; queued = "▶ queued: landing its PRs"; packaging = "▶ packaging now"; packaged = "✓ packaged"; failed = "✗ package build failed" }

function Update-Releases { Start-LaneRefresh }
function Set-ReleaseView($Snapshot) {
    $Open=@($Snapshot.Open | Sort-Object { Get-VerKey $_ })
    $Closed=@($Snapshot.Closed | Sort-Object closed_at -Descending | Select-Object -First 3)
    $Prs=@($Snapshot.Prs | Sort-Object number)
    $script:Milestones = $Open
    $Sel = if ($RelList.SelectedItems.Count) { $RelList.SelectedItems[0].Tag } else { $null }
    $RelList.BeginUpdate(); $RelList.Items.Clear()
    foreach ($M in @($Open) + @($Closed)) {
        $St = if ($M.state -eq "closed") { "closed" } else { Get-RelState $M }
        $Mine = @($Prs | Where-Object { $_.milestone -and $_.milestone.number -eq $M.number })
        $Ready = @($Mine | Where-Object { -not $_.draft -and @($_.labels | Where-Object { $_.name -in @("ready", "fix-main") }).Count })
        $It = New-Object Windows.Forms.ListViewItem([string]$M.title)
        [void]$It.SubItems.Add((Format-RelState $M $St))
        [void]$It.SubItems.Add($(if ($M.state -eq "closed") { "" } else { "$($Ready.Count) ready of $($Mine.Count)" }))
        [void]$It.SubItems.Add($(if ($M.due_on) { "due $(([datetime]$M.due_on).ToString('yyyy-MM-dd'))" } elseif ($M.state -eq "closed") { "closed $(([datetime]$M.closed_at).ToString('yyyy-MM-dd'))" } else { "" }))
        $It.Tag = $M.number
        $It.ForeColor = switch ($St) { "queued" { $Blue } "packaging" { $Blue } "failed" { $Orange } "closed" { $Grey } "packaged" { $Green } default { $Ink } }
        if ($M.number -eq $Sel) { $It.Selected = $true }
        [void]$RelList.Items.Add($It)
    }
    if (-not $RelList.Items.Count) { [void]$RelList.Items.Add((New-Object Windows.Forms.ListViewItem("None yet: press New release."))) }
    $RelList.EndUpdate()

    $Was = $MoveTo.SelectedItem
    $MoveTo.Items.Clear()
    foreach ($M in $Open) { [void]$MoveTo.Items.Add([string]$M.title) }
    [void]$MoveTo.Items.Add("No release")
    $MoveTo.SelectedItem = if ($Was -and $MoveTo.Items.Contains($Was)) { $Was } else { $MoveTo.Items[0] }

    $Ticked = @($PrList.CheckedItems | ForEach-Object { $_.Tag })
    $PrList.BeginUpdate(); $PrList.Items.Clear()
    foreach ($P in $Prs) {
        $Labels = @($P.labels | ForEach-Object { $_.name })
        $It = New-Object Windows.Forms.ListViewItem("#$($P.number)")
        [void]$It.SubItems.Add([string]$P.title)
        [void]$It.SubItems.Add($(if ($P.milestone) { [string]$P.milestone.title } else { "next" }))
        [void]$It.SubItems.Add($(if ($P.draft) { "draft" } elseif ("fix-main" -in $Labels) { "✓ ready (fix-main)" } elseif ("ready" -in $Labels) { "✓ ready" } else { "not ready" }))
        $It.Tag = $P.number; $It.Checked = $P.number -in $Ticked
        if ($P.draft -or -not ($Labels -match '^(ready|fix-main)$')) { $It.ForeColor = $Grey }
        [void]$PrList.Items.Add($It)
    }
    $PrList.EndUpdate()
    $PrHead.Text = "Pull requests ($($Prs.Count) open)"
    $RelMsg.Text = "Updated $((Get-Date).ToString('HH:mm:ss'))"; $RelMsg.ForeColor = $Grey
}

[void](New-Button "New release..." {
    $Next = if ($script:Milestones.Count) { $K = Get-VerKey $script:Milestones[-1]; "v$([math]::Floor($K / 1000000)).$([math]::Floor($K / 1000) % 1000 + 1).0" } else { "v0.2.0" }
    $T = [Microsoft.VisualBasic.Interaction]::InputBox("Name of the new release, e.g. v0.3.0", "New release", $Next)
    if (-not $T.Trim()) { return }
    try { Invoke-Gh POST "/milestones" @{ title = $T.Trim(); description = "lane: open" } | Out-Null; Update-Releases }
    catch { Show-Error "Could not make the release $T." $_ }
} $RelButtons)
[void](New-Button "▶ Queue release build" {
    $M = Get-SelectedRelease; if (-not $M) { return }
    # Ready PRs with no release would wait behind this one and go in the next release: offer to add them first.
    try { $Loose = @(Get-GhList "/pulls?state=open&base=$Branch&per_page=100" | Where-Object { -not $_.milestone -and -not $_.draft -and @($_.labels | Where-Object { $_.name -in @("ready", "fix-main") }).Count }) }
    catch { Show-Error "Could not read the pull requests." $_; return }
    if ($Loose.Count) {
        $L = ($Loose | ForEach-Object { "#$($_.number) $($_.title)" }) -join "`n"
        $A = [Windows.Forms.MessageBox]::Show("These ready PRs are in no release yet:`n`n$L`n`nPut them in $($M.title) too?`n`nYes puts them in $($M.title). No leaves them out: they wait until $($M.title) is packaged and go in the next release.", "Queue release build", "YesNoCancel", "Question")
        if ($A -eq "Cancel") { return }
        if ($A -eq "Yes") {
            foreach ($P in $Loose) { try { Invoke-Gh PATCH "/issues/$($P.number)" @{ milestone = $M.number } | Out-Null } catch { Show-Error "Could not put #$($P.number) in $($M.title)." $_; return } }
        }
    }
    $Q = [Windows.Forms.MessageBox]::Show("Queue the $($M.title) release build?`n`n1. The lane lands $($M.title)'s ready PRs first. Everything else waits, and a batch building other PRs is stopped.`n2. Then it runs the package build on main: full tests, then the package, with nothing else on the PC.`n3. When it passes, the release closes and the next release's PRs go in.`n`nPRs in $($M.title) that are not ready are left out.", "Queue release build", "OKCancel", "Question")
    if ($Q -ne "OK") { return }
    try { Set-RelState $M "queued"; Start-Lane; Update-Releases } catch { Show-Error "Could not queue $($M.title)." $_ }
} $RelButtons)
[void](New-Button "Unqueue" {
    $M = Get-SelectedRelease; if (-not $M) { return }
    if ((Get-RelState $M) -eq "packaging") { [void][Windows.Forms.MessageBox]::Show("$($M.title)'s package build is running. Stop it with Pause everything (Yes to stop the running build), then Unqueue.", "Unqueue"); return }
    try { Set-RelState $M "open"; Start-Lane; Update-Releases } catch { Show-Error "Could not unqueue $($M.title)." $_ }
} $RelButtons)
[void](New-Button "Close release" {
    $M = Get-SelectedRelease; if (-not $M) { return }
    $Q = [Windows.Forms.MessageBox]::Show("Close $($M.title)? Its open PRs keep their release name; move them to another release first if they should go in later.", "Close release", "OKCancel", "Question")
    if ($Q -ne "OK") { return }
    try { Invoke-Gh PATCH "/milestones/$($M.number)" @{ state = "closed" } | Out-Null; Start-Lane; Update-Releases } catch { Show-Error "Could not close $($M.title)." $_ }
} $RelButtons)
[void](New-Button "Refresh" { Update-Releases } $RelButtons)

[void]$MoveBar.Controls.Add((New-Label "Move ticked PRs to"))
$MoveBar.Controls[0].Margin = New-Object Windows.Forms.Padding(0, 9, 6, 0)
[void]$MoveBar.Controls.Add($MoveTo)
[void](New-Button "Move" {
    $Ns = @($PrList.CheckedItems | ForEach-Object { $_.Tag })
    if (-not $Ns.Count) { [void][Windows.Forms.MessageBox]::Show("Tick the PRs to move first.", "Move"); return }
    $To = [string]$MoveTo.SelectedItem
    $M = $script:Milestones | Where-Object { $_.title -eq $To } | Select-Object -First 1
    $Num = if ($M) { $M.number } else { $null }
    try {
        if (-not $M -and $To -ne 'No release') { throw 'Choose a destination release first.' }
        Start-LaneMove -Numbers $Ns -Milestone $Num -Target $To
    }
    catch { Show-Error 'Could not start the move.' $_ }
} $MoveBar)
. (Join-Path $PSScriptRoot 'LaneRefresh.ps1')
. (Join-Path $PSScriptRoot 'LaneMoves.ps1')
. (Join-Path $PSScriptRoot 'LaneFeedback.ps1')
. (Join-Path $PSScriptRoot 'LaneProjectsUi.ps1')
$MoveStatus=Add-Row (New-Label '' $FontSmall $Grey) 4 $RelRoot
$Form.Add_FormClosing({ if ($script:CommandJob) { $_.Cancel=$true; $PauseState.Text="A lane request is still being sent. Wait for its result before closing." }; if ($script:MoveJob) { $_.Cancel=$true; $MoveStatus.Text='A move is still running. Wait for its result before closing BuildLane.' } })
$Tabs.Add_SelectedIndexChanged({ if ($Tabs.SelectedTab -eq $RelTab) { Update-Releases } })
$RefreshTimer=New-Object Windows.Forms.Timer
$RefreshTimer.Interval=150
$RefreshTimer.Add_Tick({ Complete-LaneMove; Complete-LaneRefresh; Complete-LaneCommand; Complete-LaneHealth })

$script:Ticks = 0
$Timer = New-Object Windows.Forms.Timer
$Timer.Interval = 1000
$Timer.Add_Tick({
    Update-View
    Start-LaneControllerRead
    $script:Ticks++
    if ($Tabs.SelectedTab -eq $RelTab -and $script:Ticks % 15 -eq 0) { Update-Releases }
})
$Form.Add_Shown({ Update-View; Start-LaneRefresh "auth"; $Timer.Start(); $RefreshTimer.Start() })
# Draw a visible grip in each divider; WinForms supplies mouse and keyboard resizing.
foreach ($Split in @($NowSplit, $LogSplit, $RelSplit)) {
    $Split.Add_Paint({
        param($Sender, $EventArgs)
        $R = $Sender.SplitterRectangle
        if ($R.Width -le 0 -or $R.Height -le 0) { return }
        $Pen = New-Object Drawing.Pen($script:Grey, 1)
        try {
            $MidX = [int]($R.Left + $R.Width / 2)
            $MidY = [int]($R.Top + $R.Height / 2)
            $EventArgs.Graphics.DrawLine($Pen, $MidX - 18, $MidY - 2, $MidX + 18, $MidY - 2)
            $EventArgs.Graphics.DrawLine($Pen, $MidX - 18, $MidY + 2, $MidX + 18, $MidY + 2)
        } finally { $Pen.Dispose() }
    })
}
# Pane sizes are screen space, not project data: remembered per Windows user, next to the profiles.
$LayoutFile = Join-Path $ProfileDirectory 'layout.txt'
$script:LaneLayout = @{}
if (Test-Path -LiteralPath $LayoutFile) {
    Get-Content -LiteralPath $LayoutFile -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_ -match '^(queue|prs|log)=(\d+)$') { $script:LaneLayout[$Matches[1]] = [int]$Matches[2] }
    }
}
# Dividers store the size of the *lower* pane, so the remembered size survives a different window height.
$script:Panes = @(
    [pscustomobject]@{ Split = $NowSplit; Key = 'queue'; Share = 0.36 }
    [pscustomobject]@{ Split = $RelSplit; Key = 'prs'; Share = 0.52 }
    [pscustomobject]@{ Split = $LogSplit; Key = 'log'; Share = 0.40 }
)
function Set-PaneHeights([string]$Only = '') {
    foreach ($Pane in $script:Panes) {
        if ($Only -and $Pane.Key -ne $Only) { continue }
        $Split = $Pane.Split
        if ($Split.Panel2Collapsed) { continue }
        # Pane sizes are cosmetic: a splitter that refuses a distance on a small screen (or before it is laid out) must
        # not throw inside the Shown handler, where a throw means an error dialog or, in a test, a hang.
        try {
            $Room = $Split.Height - $Split.SplitterWidth
            if ($Room -lt ($Split.Panel1MinSize + $Split.Panel2MinSize)) { continue }
            $Fallback = [int]($Room * [double]$Pane.Share)
            if ($Pane.Key -eq 'queue') {
                # Start the queue immediately below the overview content, while preserving room to scroll on small screens.
                $Preferred = $Root.GetPreferredSize((New-Object Drawing.Size($Root.ClientSize.Width, 0))).Height
                $Fallback = $Room - $Preferred
            }
            $Lower = if ($script:LaneLayout.ContainsKey($Pane.Key)) { [int]$script:LaneLayout[$Pane.Key] } else { $Fallback }
            $Lower = [math]::Max($Split.Panel2MinSize, [math]::Min($Lower, $Room - $Split.Panel1MinSize))
            if ($Lower -gt 0) { $Split.SplitterDistance = $Room - $Lower }
        } catch { Write-Warning "Could not size the $($Pane.Key) pane: $($_.Exception.Message)" }
    }
}
function Save-PaneHeights {
    try {
        foreach ($Pane in $script:Panes) {
            if ($Pane.Split.Panel2Collapsed) { continue }
            $script:LaneLayout[$Pane.Key] = $Pane.Split.Height - $Pane.Split.SplitterWidth - $Pane.Split.SplitterDistance
        }
        New-Item -ItemType Directory -Force (Split-Path $LayoutFile) | Out-Null
        ($script:LaneLayout.GetEnumerator() | Sort-Object Key | ForEach-Object { "$($_.Key)=$($_.Value)" }) |
            Set-Content -LiteralPath $LayoutFile -Encoding ASCII
    } catch {}
}
# Keep columns and text aligned with the available width, including DPI scaling.
function Resize-Content {
    foreach ($List in @($Queue, $RelList, $PrList)) {
        $List.BorderStyle = 'None'; $List.HideSelection = $false
        $Width = [math]::Max(300, $List.ClientSize.Width - 24)
        if ($List -eq $Queue) { $List.Columns[0].Width = 80; $List.Columns[2].Width = [int]($Width * .30); $List.Columns[1].Width = [math]::Max(120, $Width - 80 - $List.Columns[2].Width) }
        else { foreach ($Column in $List.Columns) { $Column.Width = [int]($Width / $List.Columns.Count) } }
    }
    foreach ($Panel in @($Root, $RelRoot, $PrRoot, $SystemRoot, $ProjectRoot)) {
        foreach ($Control in $Panel.Controls) {
            if ($Control -is [Windows.Forms.Label]) { $Control.MaximumSize = New-Object Drawing.Size([math]::Max(240, $Panel.ClientSize.Width - 64), 0) }
        }
    }
}
$Form.Add_Resize({ Resize-Content })
# The tab content now nests two splitters, which can talk the form into opening at its minimum width. Give it a
# deliberate starting size (and keep it inside the monitor's working area) before the panes are measured.
$Form.Add_Shown({
    try {
        # A headless session (a CI runner, a Windows service) can report no usable screen; fall back to the metrics
        # Windows provides instead of throwing here.
        $Screen = [Windows.Forms.Screen]::FromControl($Form)
        $Work = if ($Screen) { $Screen.WorkingArea } else { [Windows.Forms.SystemInformation]::WorkingArea }
        if ($Work.Width -lt 640 -or $Work.Height -lt 480) { $Work = [Windows.Forms.SystemInformation]::WorkingArea }
        $WantedW = [math]::Max($Form.MinimumSize.Width, [math]::Min(1040, $Work.Width - 40))
        $WantedH = [math]::Max($Form.MinimumSize.Height, [math]::Min(840, $Work.Height - 40))
        if ($Form.Width -lt 940) { $Form.Width = $WantedW }
        if ($Form.Height -lt 700 -or $Form.Height -gt $Work.Height - 20) { $Form.Height = $WantedH }
        if ($Form.WindowState -eq 'Normal') {
            # Built as two values first: `New-Object Drawing.Point(a + x, b + y)` over several lines binds the
            # additions inside the argument array and throws "[System.Object[]] does not contain op_Addition".
            $X = [int](($Work.Width - $Form.Width) / 2) + $Work.X
            $Y = [int](($Work.Height - $Form.Height) / 2) + $Work.Y
            $Form.Location = New-Object Drawing.Point($X, $Y)
        }
    } catch { Write-Warning "Could not size the window: $($_.Exception.Message) [$($_.InvocationInfo.PositionMessage -replace "`r?`n", ' ')]" }
})
$Form.Add_Shown({ try { Resize-Content; Set-PaneHeights } catch { Write-Warning "Could not lay out the window: $($_.Exception.Message)" } })
$Form.Add_FormClosed({ Save-PaneHeights; $Timer.Stop(); $Timer.Dispose(); $RefreshTimer.Stop(); $RefreshTimer.Dispose(); Stop-LaneRefresh })
. (Join-Path $PSScriptRoot 'LaneTheme.ps1')
if ($NoShow) { return }
if ($SmokeTest) {
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); Update-View; Resize-Content
    [Windows.Forms.Application]::DoEvents()
    if ($PreviewFile) {
        $Bitmap = New-Object Drawing.Bitmap($Form.Width, $Form.Height)
        $Form.DrawToBitmap($Bitmap, (New-Object Drawing.Rectangle(0, 0, $Form.Width, $Form.Height)))
        $Bitmap.Save($PreviewFile); $Bitmap.Dispose()
    }
    $Form.Close(); $Form.Dispose()
    Write-Output 'Buildlane UI smoke test passed.'
} else { [void]$Form.ShowDialog() }
