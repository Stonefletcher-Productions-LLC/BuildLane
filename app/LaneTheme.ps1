$script:ThemeFile = Join-Path $ProfileDirectory 'appearance.txt'
function Set-LaneTheme([bool]$Dark, [switch]$Save) {
    $Old=@($Ink,$Paper,$Blue,$Orange,$Green,$Grey)
    $script:DarkMode=$Dark
    $Hex=if($Dark){@('#E5EAF2','#171C24','#75C7FA','#FFB477','#65D6AF','#ADB9CA')}else{@('#1F2A37','#F5F7FA','#0072B2','#D55E00','#009E73','#6E6E6E')}
    $New=@($Hex | ForEach-Object { [Drawing.ColorTranslator]::FromHtml($_) })
    $script:Ink,$script:Paper,$script:Blue,$script:Orange,$script:Green,$script:Grey=$New
    $Surface=if($Dark){[Drawing.ColorTranslator]::FromHtml('#242D3A')}else{[Drawing.Color]::White}
    function Paint-Control($C) {
        $C.BackColor=if($C -is [Windows.Forms.TextBoxBase] -or $C -is [Windows.Forms.ListView] -or $C -is [Windows.Forms.ComboBox] -or $C -is [Windows.Forms.Button]){$Surface}else{$Paper}
        $Color=$Ink
        for($i=0;$i -lt $Old.Count;$i++){if($C.ForeColor.ToArgb() -eq $Old[$i].ToArgb()){$Color=$New[$i];break}}
        $C.ForeColor=$Color
        if($C -is [Windows.Forms.Button]){$C.UseVisualStyleBackColor=$false}
        if($C -is [Windows.Forms.ListView]){foreach($Item in $C.Items){for($i=0;$i -lt $Old.Count;$i++){if($Item.ForeColor.ToArgb() -eq $Old[$i].ToArgb()){$Item.ForeColor=$New[$i];break}}}}
        foreach($Child in $C.Controls){Paint-Control $Child}
    }
    Paint-Control $Form
    $ThemeButton.Text=if($Dark){'Use light mode'}else{'Use dark mode'}
    if($Save){try{[IO.File]::WriteAllText($script:ThemeFile,$(if($Dark){'dark'}else{'light'}))}catch{Show-Error 'Could not save appearance preference.' $_}}
}
$ThemeButton=New-Button 'Use dark mode' { Set-LaneTheme (-not $script:DarkMode) -Save } $TopBar
Set-LaneTheme ((Test-Path $script:ThemeFile) -and ((Get-Content $script:ThemeFile -ErrorAction SilentlyContinue) -eq 'dark'))
