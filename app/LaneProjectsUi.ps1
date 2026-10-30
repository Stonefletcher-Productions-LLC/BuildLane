$ProjectTab = New-Object Windows.Forms.TabPage('Projects'); $ProjectTab.BackColor = $Paper
[void]$Tabs.TabPages.Add($ProjectTab)
$ProjectRoot = New-Object Windows.Forms.TableLayoutPanel
$ProjectRoot.Dock='Fill'; $ProjectRoot.ColumnCount=1; $ProjectRoot.AutoScroll=$true
$ProjectRoot.Padding=New-Object Windows.Forms.Padding(24)
$ProjectTab.Controls.Add($ProjectRoot)
[void](Add-Row (New-Label 'Make BuildLane yours' $FontTitle) 0 $ProjectRoot)
[void](Add-Row (New-Label "1. Choose a repository   >   2. Point to your runner   >   3. Save and connect GitHub.`nYour build workflow and runner must already be set up." $FontSmall $Grey) 0 $ProjectRoot)
$ProjectPicker = Add-Row (New-Object Windows.Forms.ComboBox) 12 $ProjectRoot
$ProjectPicker.DropDownStyle='DropDownList'; $ProjectPicker.DisplayMember='Label'
$ProjectActions = Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 4 $ProjectRoot
$ProjectActions.AutoSize=$true
$Editor = Add-Row (New-Object Windows.Forms.TableLayoutPanel) 8 $ProjectRoot
$Editor.ColumnCount=2; $Editor.AutoSize=$true
[void]$Editor.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute',180)))
[void]$Editor.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
$AdvancedToggle=New-Object Windows.Forms.CheckBox
$AdvancedToggle.Text='Advanced settings (workflow, services and disk limits)'; $AdvancedToggle.AutoSize=$true
[void](Add-Row $AdvancedToggle 12 $ProjectRoot)
$AdvancedEditor=Add-Row (New-Object Windows.Forms.TableLayoutPanel) 4 $ProjectRoot
$AdvancedEditor.ColumnCount=2; $AdvancedEditor.AutoSize=$true; $AdvancedEditor.Visible=$false
[void]$AdvancedEditor.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute',180)))
[void]$AdvancedEditor.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
$AdvancedToggle.Add_CheckedChanged({ $AdvancedEditor.Visible=$AdvancedToggle.Checked })
$script:ProjectFields = [ordered]@{}
$ProjectTips=New-Object Windows.Forms.ToolTip
$ProjectTips.AutoPopDelay=15000
$Hints=@{name='A friendly name shown in the app.';repo='Paste a GitHub repository URL or enter owner/repository.';branch='The branch your lane builds, usually main.';workflow='The workflow filename accepting task=lane.';integrationWorkflow='Optional server-owned integration controller on trusted main. Mode is stored by the backend, never in this profile.';activeJobs='Optional exact workflow/job allowlist, e.g. ci.yml|Build and test;compile-preview.yml|Compile only (not release validation). Read-only live job observation.';statusFile='JSON status file written by your build runner.';pauseFile='A marker file your runner checks before starting work.';runnerPath='Existing runner folder, used to find recent step logs.';drive='Drive letter to monitor for free space.';serviceFilter='Only show runner services matching this pattern.';cachePort='Optional local cache port. Leave 0 to disable.';minimumFreeGB='Display a warning below this amount. Your backend enforces build limits.';cleanBelowGB='Display the cleanup threshold. Your backend performs cleanup.'}
$FieldLabels = [ordered]@{name='Project name';repo='GitHub repository';branch='Base branch';workflow='Lane workflow';integrationWorkflow='Integration controller (optional)';activeJobs='Active build jobs (optional)';statusFile='Status file';pauseFile='Pause file';runnerPath='Runner folder';drive='Build drive';serviceFilter='Runner service filter';cachePort='Cache port (0 = off)';minimumFreeGB='Minimum free GB';cleanBelowGB='Cleanup below GB'}
foreach ($Key in $FieldLabels.Keys) {
    $Label=New-Label $FieldLabels[$Key] $FontSmall
    $Label.Margin=New-Object Windows.Forms.Padding(0,7,12,5)
    $Input=New-Object Windows.Forms.TextBox; $Input.Dock='Top'
    $Input.Margin=New-Object Windows.Forms.Padding(0,4,0,4)
    $Panel=if ($Key -in @('name','repo','runnerPath','statusFile','pauseFile')) { $Editor } else { $AdvancedEditor }
    $ProjectTips.SetToolTip($Input,$Hints[$Key])
    $Input.AccessibleName=$FieldLabels[$Key]; $Input.AccessibleDescription=$Hints[$Key]
    [void]$Panel.Controls.Add($Label)
    if ($Key -in @('runnerPath','statusFile','pauseFile')) {
        $PathRow=New-Object Windows.Forms.TableLayoutPanel; $PathRow.ColumnCount=2; $PathRow.AutoSize=$true; $PathRow.Dock='Top'
        [void]$PathRow.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
        [void]$PathRow.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute',84)))
        [void]$PathRow.Controls.Add($Input)
        $Browse=New-Object Windows.Forms.Button; $Browse.Text='Browse...'; $Browse.Tag=$Key; $Browse.AutoSize=$true
        $Browse.Add_Click({ Select-LanePath ([string]$this.Tag) })
        [void]$PathRow.Controls.Add($Browse); [void]$Panel.Controls.Add($PathRow)
    } else { [void]$Panel.Controls.Add($Input) }
    $Input.Add_TextChanged({ if (-not $script:LoadingProject) { $script:ProjectDirty=$true; $ProjectMessage.Text='Unsaved changes. Save and use when you are ready.'; $ProjectMessage.ForeColor=$Orange } })
    $script:ProjectFields[$Key]=$Input
}
$SaveActions=Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 12 $ProjectRoot; $SaveActions.AutoSize=$true
$ProjectMessage=Add-Row (New-Label '' $FontSmall $Grey) 4 $ProjectRoot
function Confirm-LaneDiscard {
    if (-not $script:ProjectDirty) { return $true }
    ([Windows.Forms.MessageBox]::Show('Discard your unsaved project changes?', 'Unsaved changes', 'YesNo', 'Question') -eq 'Yes')
}
function Select-LanePath([string]$Key) {
    $Dialog=if ($Key -eq 'runnerPath') { New-Object Windows.Forms.FolderBrowserDialog } else { New-Object Windows.Forms.SaveFileDialog }
    try {
        if ($Key -eq 'runnerPath') { $Dialog.Description='Choose the folder used by your build runner'; $Dialog.SelectedPath=$script:ProjectFields[$Key].Text }
        else { $Dialog.Title='Choose the path your runner uses'; $Dialog.FileName=$script:ProjectFields[$Key].Text; $Dialog.OverwritePrompt=$false; $Dialog.CheckFileExists=$false }
        if ($Dialog.ShowDialog($Form) -eq 'OK') {
            $script:ProjectFields[$Key].Text=if ($Key -eq 'runnerPath') { $Dialog.SelectedPath } else { $Dialog.FileName }
        }
    } finally { $Dialog.Dispose() }
}
function New-LaneProject {
    $script:LoadingProject=$true
    $script:EditingPath=''; $script:EditingHash=''
    $Defaults=New-LaneProjectDefaults
    foreach ($Key in $script:ProjectFields.Keys) { $script:ProjectFields[$Key].Text=[string]$Defaults.$Key }
    $script:LoadingProject=$false; $script:ProjectDirty=$false
    $ProjectMessage.Text='Give your project a name and repository. Check that the paths match your runner.'
    $ProjectMessage.ForeColor=$Grey
}
function Edit-LaneProject([string]$Path) {
    $Value=Read-LaneProfile $Path
    $script:EditingPath=[IO.Path]::GetFullPath($Path)
    $script:EditingHash=(Get-FileHash -LiteralPath $Path).Hash
    $script:LoadingProject=$true
    foreach ($Key in $script:ProjectFields.Keys) { $script:ProjectFields[$Key].Text=[string]$Value.$Key }
    $script:LoadingProject=$false; $script:ProjectDirty=$false
    $ProjectMessage.Text="Editing $($Value.name). Save and use applies these settings."
    $ProjectMessage.ForeColor=$Grey
}
function Update-ProjectPicker {
    $ProjectPicker.Items.Clear()
    $Paths=@($script:Profile) + @(Get-ChildItem -LiteralPath $ProfileDirectory -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object FullName)
    foreach ($Path in @($Paths | Select-Object -Unique)) {
        try { $Value=Read-LaneProfile $Path; if (-not $Value.setupRequired) { [void]$ProjectPicker.Items.Add([pscustomobject]@{Label=$Value.name;Path=$Path}) } } catch {}
    }
    if ($ProjectPicker.Items.Count) { $ProjectPicker.SelectedIndex=0 }
    $ProjectPicker.Visible=($ProjectPicker.Items.Count -gt 0)
    foreach ($Button in $ProjectActions.Controls) { if ($Button.Text -in @('Edit selected','Use selected')) { $Button.Enabled=($ProjectPicker.Items.Count -gt 0) } }
}
function Use-LaneProject([string]$Path) {
    if ($script:ActionJob) { throw 'Wait for the release or connection request to finish before switching projects.' }
    if (($script:IntegrationJob -and $script:IntegrationJob.Kind -eq 'request') -or $script:IntegrationPending) { throw 'Wait for the integration request to be confirmed before switching projects.' }
    if ($script:CommandJob) { throw 'Wait for the lane request to finish before switching projects.' }
    if ($script:MoveJob) { throw 'Wait for the current move to finish before switching projects.' }
    $Value=Read-LaneProfile $Path
    # Persist first, so a failed write cannot partially switch the UI.
    [void][IO.Directory]::CreateDirectory($ProfileDirectory)
    [IO.File]::WriteAllText((Join-Path $ProfileDirectory 'current.txt'), [IO.Path]::GetFullPath($Path))
    Stop-LaneRefresh
    $script:Profile=[IO.Path]::GetFullPath($Path); $script:Config=$Value
    $script:Repo=$Value.repo; $script:ProductName=$Value.name
    $script:Branch=[uri]::EscapeDataString($Value.branch); $script:Workflow=[uri]::EscapeDataString($Value.workflow)
    $script:JsonFile=$Value.statusFile; $script:PauseFile=$Value.pauseFile; $script:Runner=$Value.runnerPath; $script:Drive=$Value.drive
    Reset-LaneCredential
    $script:State=$null; $script:Milestones=@(); $script:NextRefresh=[datetime]::MinValue
    $RelList.Items.Clear(); $PrList.Items.Clear(); $MoveTo.Items.Clear()
    $RelButtons.Enabled=$true; $MoveBar.Enabled=$true
    $RelMsg.Text='Open Releases to refresh this project.'
    if ($MoveStatus) { $MoveStatus.Text='' }
    $Form.Text="Buildlane | $ProductName"; $Brand.Text="Buildlane / $ProductName"
    $ProjectInfo.Text="Project: $Repo`nStatus: $JsonFile`nRunner: $Runner"
    Update-View; Start-LaneRefresh 'auth'
}
[void](New-Button 'Edit selected' {
    if (-not (Confirm-LaneDiscard)) { return }
    if ($ProjectPicker.SelectedItem) { try { Edit-LaneProject $ProjectPicker.SelectedItem.Path } catch { Show-Error 'Could not open project.' $_ } }
} $ProjectActions)
[void](New-Button 'Use selected' {
    if (-not (Confirm-LaneDiscard)) { return }
    if ($ProjectPicker.SelectedItem) { try { Use-LaneProject $ProjectPicker.SelectedItem.Path; Edit-LaneProject $Profile } catch { Show-Error 'Could not switch project.' $_ } }
} $ProjectActions)
[void](New-Button 'New project' {
    if (Confirm-LaneDiscard) { New-LaneProject }
} $ProjectActions)
[void](New-Button 'Import...' {
    if (-not (Confirm-LaneDiscard)) { return }
    $Dialog=New-Object Windows.Forms.OpenFileDialog; $Dialog.Filter='Project profiles (*.json)|*.json'; $Dialog.InitialDirectory=$ProfileDirectory
    try {
        if ($Dialog.ShowDialog($Form) -eq 'OK') {
            $Value=Read-LaneProfile $Dialog.FileName
            $Path=Join-Path $ProfileDirectory ([guid]::NewGuid().ToString()+'.json')
            Save-LaneProfile $Path $Value ''; Edit-LaneProject $Path; Update-ProjectPicker
        }
    } catch { Show-Error 'Could not import project.' $_ } finally { $Dialog.Dispose() }
} $ProjectActions)
$SaveProjectButton=New-Button 'Save and use' {
    try {
        $Value=[ordered]@{}
        foreach ($Key in $script:ProjectFields.Keys) { $Value[$Key]=$script:ProjectFields[$Key].Text.Trim() }
        $Value.repo=ConvertTo-LaneRepository $Value.repo
        Test-LaneProfile $Value
        foreach ($Key in @('cachePort','minimumFreeGB','cleanBelowGB')) { $Value[$Key]=[int]$Value[$Key] }
        $Path=$script:EditingPath
        if (-not $Path) { $Path=Join-Path $ProfileDirectory ([guid]::NewGuid().ToString()+'.json') }
        Save-LaneProfile $Path $Value $script:EditingHash
        Use-LaneProject $Path; Edit-LaneProject $Path; Update-ProjectPicker
        $ProjectMessage.Text="Saved. $ProductName is now active."; $ProjectMessage.ForeColor=$Green
    } catch { $ProjectMessage.Text=$_.Exception.Message; $ProjectMessage.ForeColor=$Orange }
} $SaveActions
$SaveProjectButton.BackColor=$Blue; $SaveProjectButton.ForeColor=[Drawing.Color]::White
[void](New-Button 'Reset edits' { if (-not (Confirm-LaneDiscard)) { return }; try { Edit-LaneProject $Profile } catch { Show-Error 'Could not reload project.' $_ } } $SaveActions)
[void](New-Button 'Export saved profile...' {
    if ($script:ProjectDirty) { $ProjectMessage.Text='Save your edits before exporting.'; $ProjectMessage.ForeColor=$Orange; return }
    if (-not $script:EditingPath) { $ProjectMessage.Text='Save a project before exporting.'; return }
    $Dialog=New-Object Windows.Forms.SaveFileDialog; $Dialog.Filter='Project profile (*.json)|*.json'; $Dialog.FileName='project.profile.json'
    try {
        if ($Dialog.ShowDialog($Form) -eq 'OK') {
            $Value=Read-LaneProfile $script:EditingPath
            $Hash=if (Test-Path -LiteralPath $Dialog.FileName) { (Get-FileHash -LiteralPath $Dialog.FileName).Hash } else { '' }
            Save-LaneProfile $Dialog.FileName $Value $Hash
            $ProjectMessage.Text='Profile exported. Tokens are stored separately and are not included.'
        }
    } catch { Show-Error 'Could not export profile.' $_ } finally { $Dialog.Dispose() }
} $SaveActions)
[void](New-Button 'Setup guide' { Start-Process 'https://github.com/Stonefletcher-Productions-LLC/BuildLane/blob/main/docs/GETTING-STARTED.md' } $SaveActions)
$Form.Add_FormClosing({ if (-not (Confirm-LaneDiscard)) { $_.Cancel=$true } })
$Form.Add_FormClosed({ $ProjectTips.Dispose() })
Update-ProjectPicker
if ($Config.setupRequired) { New-LaneProject; $Tabs.SelectedTab=$ProjectTab } else { Edit-LaneProject $Profile }
