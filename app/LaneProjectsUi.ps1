$ProjectTab = New-Object Windows.Forms.TabPage('Projects'); $ProjectTab.BackColor = $Paper
[void]$Tabs.TabPages.Add($ProjectTab)
$ProjectRoot = New-Object Windows.Forms.TableLayoutPanel
$ProjectRoot.Dock='Fill'; $ProjectRoot.ColumnCount=1; $ProjectRoot.AutoScroll=$true
$ProjectRoot.Padding=New-Object Windows.Forms.Padding(24)
$ProjectTab.Controls.Add($ProjectRoot)
[void](Add-Row (New-Label 'Project settings' $FontTitle) 0 $ProjectRoot)
[void](Add-Row (New-Label 'Save a project, then switch between its build queue and releases. Existing build services are managed separately.' $FontSmall $Grey) 0 $ProjectRoot)
$ProjectPicker = Add-Row (New-Object Windows.Forms.ComboBox) 12 $ProjectRoot
$ProjectPicker.DropDownStyle='DropDownList'; $ProjectPicker.DisplayMember='Label'
$ProjectActions = Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 4 $ProjectRoot
$ProjectActions.AutoSize=$true
$Editor = Add-Row (New-Object Windows.Forms.TableLayoutPanel) 8 $ProjectRoot
$Editor.ColumnCount=2; $Editor.AutoSize=$true
[void]$Editor.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Absolute',180)))
[void]$Editor.ColumnStyles.Add((New-Object Windows.Forms.ColumnStyle('Percent',100)))
$script:ProjectFields = [ordered]@{}
$FieldLabels = [ordered]@{name='Project name';repo='GitHub repository';branch='Base branch';workflow='Lane workflow';statusFile='Status file';pauseFile='Pause file';runnerPath='Runner folder';drive='Build drive';serviceFilter='Runner service filter';cachePort='Cache port (0 = off)';minimumFreeGB='Minimum free GB';cleanBelowGB='Cleanup below GB'}
foreach ($Key in $FieldLabels.Keys) {
    $Label=New-Label $FieldLabels[$Key] $FontSmall
    $Label.Margin=New-Object Windows.Forms.Padding(0,7,12,5)
    $Input=New-Object Windows.Forms.TextBox; $Input.Dock='Top'
    $Input.Margin=New-Object Windows.Forms.Padding(0,4,0,4)
    [void]$Editor.Controls.Add($Label); [void]$Editor.Controls.Add($Input)
    $script:ProjectFields[$Key]=$Input
}
$SaveActions=Add-Row (New-Object Windows.Forms.FlowLayoutPanel) 12 $ProjectRoot; $SaveActions.AutoSize=$true
$ProjectMessage=Add-Row (New-Label '' $FontSmall $Grey) 4 $ProjectRoot
function Edit-LaneProject([string]$Path) {
    $Value=Read-LaneProfile $Path
    $script:EditingPath=[IO.Path]::GetFullPath($Path)
    $script:EditingHash=(Get-FileHash -LiteralPath $Path).Hash
    foreach ($Key in $script:ProjectFields.Keys) { $script:ProjectFields[$Key].Text=[string]$Value.$Key }
    $ProjectMessage.Text="Editing $($Value.name). Save and use applies these settings."
    $ProjectMessage.ForeColor=$Grey
}
function Update-ProjectPicker {
    $ProjectPicker.Items.Clear()
    $Paths=@($script:Profile) + @(Get-ChildItem -LiteralPath $ProfileDirectory -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object FullName)
    foreach ($Path in @($Paths | Select-Object -Unique)) {
        try { $Value=Read-LaneProfile $Path; [void]$ProjectPicker.Items.Add([pscustomobject]@{Label=$Value.name;Path=$Path}) } catch {}
    }
    if ($ProjectPicker.Items.Count) { $ProjectPicker.SelectedIndex=0 }
}
function Use-LaneProject([string]$Path) {
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
    $Form.Text="Buildlane | $ProductName"; $Brand.Text="Buildlane / $ProductName"
    $ProjectInfo.Text="Project: $Repo`nStatus: $JsonFile`nRunner: $Runner"
    Update-View; Start-LaneRefresh 'auth'
}
[void](New-Button 'Edit selected' {
    if ($ProjectPicker.SelectedItem) { try { Edit-LaneProject $ProjectPicker.SelectedItem.Path } catch { Show-Error 'Could not open project.' $_ } }
} $ProjectActions)
[void](New-Button 'Use selected' {
    if ($ProjectPicker.SelectedItem) { try { Use-LaneProject $ProjectPicker.SelectedItem.Path; Edit-LaneProject $Profile } catch { Show-Error 'Could not switch project.' $_ } }
} $ProjectActions)
[void](New-Button 'New project' {
    $script:EditingPath=''; $script:EditingHash=''
    foreach ($Key in $script:ProjectFields.Keys) { $script:ProjectFields[$Key].Text='' }
    foreach ($Pair in @{branch='main';workflow='lane.yml';drive='C';serviceFilter='*';cachePort='0';minimumFreeGB='12';cleanBelowGB='25'}.GetEnumerator()) { $script:ProjectFields[$Pair.Key].Text=$Pair.Value }
    $ProjectMessage.Text='Enter your project details, then Save and use.'
} $ProjectActions)
[void](New-Button 'Import...' {
    $Dialog=New-Object Windows.Forms.OpenFileDialog; $Dialog.Filter='Project profiles (*.json)|*.json'; $Dialog.InitialDirectory=$ProfileDirectory
    try {
        if ($Dialog.ShowDialog($Form) -eq 'OK') {
            $Value=Read-LaneProfile $Dialog.FileName
            $Path=Join-Path $ProfileDirectory ([guid]::NewGuid().ToString()+'.json')
            Save-LaneProfile $Path $Value ''; Edit-LaneProject $Path; Update-ProjectPicker
        }
    } catch { Show-Error 'Could not import project.' $_ } finally { $Dialog.Dispose() }
} $ProjectActions)
[void](New-Button 'Save and use' {
    try {
        $Value=[ordered]@{}
        foreach ($Key in $script:ProjectFields.Keys) { $Value[$Key]=$script:ProjectFields[$Key].Text.Trim() }
        Test-LaneProfile $Value
        foreach ($Key in @('cachePort','minimumFreeGB','cleanBelowGB')) { $Value[$Key]=[int]$Value[$Key] }
        $Path=$script:EditingPath
        if (-not $Path) { $Path=Join-Path $ProfileDirectory ([guid]::NewGuid().ToString()+'.json') }
        Save-LaneProfile $Path $Value $script:EditingHash
        Use-LaneProject $Path; Edit-LaneProject $Path; Update-ProjectPicker
        $ProjectMessage.Text="Saved. $ProductName is now active."; $ProjectMessage.ForeColor=$Green
    } catch { $ProjectMessage.Text=$_.Exception.Message; $ProjectMessage.ForeColor=$Orange }
} $SaveActions)
[void](New-Button 'Reset edits' { try { Edit-LaneProject $Profile } catch { Show-Error 'Could not reload project.' $_ } } $SaveActions)
Update-ProjectPicker
Edit-LaneProject $Profile
