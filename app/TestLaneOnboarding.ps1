# Isolated first-run test. Optional screenshots use sample data and no network calls.
param([string]$PreviewDirectory)
$ErrorActionPreference='Stop'
$FixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('buildlane-onboarding-'+[guid]::NewGuid())
try {
    . "$PSScriptRoot\LaneApp.ps1" -ProfileDirectory $FixtureRoot -SmokeTest -NoShow
    function Assert($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
    function Save-Preview([string]$Name) {
        if (-not $PreviewDirectory) { return }
        [void][IO.Directory]::CreateDirectory($PreviewDirectory)
        Resize-Content; [Windows.Forms.Application]::DoEvents()
        $Bitmap=New-Object Drawing.Bitmap($Form.Width,$Form.Height)
        try { $Form.DrawToBitmap($Bitmap,(New-Object Drawing.Rectangle(0,0,$Form.Width,$Form.Height))); $Bitmap.Save((Join-Path $PreviewDirectory $Name)) }
        finally { $Bitmap.Dispose() }
    }
    $Form.Show(); $Timer.Stop(); $RefreshTimer.Stop(); Update-View
    Assert ($Config.setupRequired -and $Tabs.SelectedTab -eq $ProjectTab) 'First run did not open setup.'
    Assert (-not $PauseButton.Enabled -and -not $GhButton.Enabled -and -not $RelTab.Enabled) 'Unconfigured project exposes actions.'
    Start-LaneRefresh
    Assert (-not $script:RefreshJob) 'First run attempted GitHub access.'
    Assert ($ProjectPicker.Items.Count -eq 0) 'Setup placeholder is listed as a saved project.'
    Assert ($script:ProjectFields.repo.Text -eq '' -and -not $script:ProjectDirty) 'First run did not start with an empty repository.'
    Assert ((ConvertTo-LaneRepository ' https://github.com/acme/desktop.git/ ') -eq 'acme/desktop') 'Repository link was not normalized.'
    $Rejected=$false
    try { ConvertTo-LaneRepository 'https://untrusted.example/acme/desktop' } catch { $Rejected=$true }
    Assert $Rejected 'A non-GitHub URL was accepted.'
    if ($PreviewDirectory) {
        # A complete example rather than an empty form: the guide shows what a filled first run looks like.
        $script:ProjectFields.name.Text='Acme Desktop'
        $script:ProjectFields.repo.Text='https://github.com/acme/desktop'
        $script:ProjectFields.runnerPath.Text='C:\BuildRunner\Acme'
        $script:ProjectFields.statusFile.Text='C:\BuildRunner\Acme\status.json'
        $script:ProjectFields.pauseFile.Text='C:\BuildRunner\Acme\PAUSED'
        $script:ProjectDirty=$false
        $ProjectMessage.Text='Looks good. Save and use, then connect GitHub.'
        $ProjectMessage.ForeColor=$Grey
        Save-Preview 'setup.png'
        # Back to an unsaved form so the assertions below still exercise typing.
        $script:ProjectFields.name.Text=''
        $script:ProjectFields.repo.Text=''
        $script:ProjectDirty=$false
    }
    $script:ProjectFields.name.Text='Acme Desktop'
    $script:ProjectFields.repo.Text='https://github.com/acme/desktop'
    $script:ProjectFields.runnerPath.Text=$FixtureRoot
    $script:ProjectFields.statusFile.Text=(Join-Path $FixtureRoot 'status.json')
    $script:ProjectFields.pauseFile.Text=(Join-Path $FixtureRoot 'PAUSED')
    Assert $script:ProjectDirty 'Typing did not mark unsaved changes.'
    $SaveButton=$SaveActions.Controls | Where-Object Text -eq 'Save and use'
    $SaveButton.PerformClick()
    Assert ($Repo -eq 'acme/desktop' -and -not $Config.setupRequired -and -not $script:ProjectDirty) "Save failed: $($ProjectMessage.Text)"
    Assert ($PauseButton.Enabled -and $RelTab.Enabled) 'Saved project did not unlock the app.'
    Assert ((Read-LaneProfile $Profile).repo -eq 'acme/desktop') 'Normalized repository was not saved.'
    $UnicodeProfile=Read-LaneProfile $Profile
    $UnicodeProfile.name='Studio '+[char]0x00e9
    $UnicodePath=Join-Path $FixtureRoot 'unicode.json'
    Save-LaneProfile $UnicodePath $UnicodeProfile ''
    Assert ((Read-LaneProfile $UnicodePath).name -eq $UnicodeProfile.name) 'Non-ASCII project names were corrupted.'
    $AdvancedToggle.Checked=$true
    Assert $AdvancedEditor.Visible 'Advanced settings did not expand.'
    $AdvancedToggle.Checked=$false
    $Fixture=@{updated=(Get-Date).ToString('o');started=(Get-Date).AddMinutes(-3).ToString('o');phase=1;phases=@('Prepare','Build','Test','Package');what='Acme Desktop 1.2';step='Building desktop app';progress=@{done=3;total=5};queue=@(@{number=42;title='Improve the welcome screen';state='building now'});notes=@();done=''}
    $Fixture | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $JsonFile
    $Tabs.SelectedTab=$NowTab; Update-View
    # The dashboard picture in the guide comes from app\PreviewDocs.ps1, which renders a busier, more representative
    # state; this test only renders the two screens it is responsible for testing.
    $Tabs.SelectedTab=$ProjectTab
    Edit-LaneProject $Profile
    if ($PreviewDirectory) {
        $script:LoadingProject=$true
        $script:ProjectFields.runnerPath.Text='C:\BuildRunner\Acme'
        $script:ProjectFields.statusFile.Text='C:\BuildRunner\Acme\status.json'
        $script:ProjectFields.pauseFile.Text='C:\BuildRunner\Acme\PAUSED'
        $script:LoadingProject=$false
    }
    Save-Preview 'projects.png'
    Write-Output 'PASS: first-run isolation, setup gating, repository URL validation, dirty tracking, save-and-use, persistence, advanced settings.'
} finally {
    $script:ProjectDirty=$false
    if ($Form) { $Form.Close(); $Form.Dispose() }
    if (Get-Command Stop-LaneRefresh -ErrorAction SilentlyContinue) { Stop-LaneRefresh }
    $Resolved=[IO.Path]::GetFullPath($FixtureRoot)
    if ($Resolved.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase) -and (Split-Path $Resolved -Leaf) -like 'buildlane-onboarding-*' -and (Test-Path -LiteralPath $Resolved)) { Remove-Item -LiteralPath $Resolved -Recurse -Force }
}
