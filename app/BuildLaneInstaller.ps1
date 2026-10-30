# Build a per-user MSI using only Windows Installer, makecab and the .NET compiler.
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$Version='1.1.0',
    [string]$OutputDirectory="$env:LOCALAPPDATA\Buildlane-Packages",
    [string]$CertificateThumbprint,
    [string]$TimestampServer='http://timestamp.digicert.com'
)
$ErrorActionPreference='Stop'
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
[void][IO.Directory]::CreateDirectory($OutputDirectory)
$Stage=Join-Path $OutputDirectory 'payload'
& (Join-Path $PSScriptRoot 'InstallLaneApp.ps1') -Destination $Stage -NoShortcut | Out-Null
function Sign-Artifact([string]$Path) {
    if (-not $CertificateThumbprint) { return }
    $Certificate=Get-Item "Cert:\CurrentUser\My\$CertificateThumbprint" -ErrorAction Stop
    if (-not $Certificate.HasPrivateKey -or $Certificate.NotAfter -le (Get-Date)) { throw 'Signing certificate needs a valid private key.' }
    $Signature=Set-AuthenticodeSignature -FilePath $Path -Certificate $Certificate -HashAlgorithm SHA256 -TimestampServer $TimestampServer
    if ($Signature.Status -ne 'Valid' -or -not $Signature.TimeStamperCertificate) { throw "Signing or timestamp verification failed: $Path ($($Signature.Status))." }
}
$Files=@('Buildlane.exe','LaneApp.ps1','LaneProfiles.ps1','LaneGitHub.ps1','LaneRefresh.ps1','LaneMoves.ps1','LaneFeedback.ps1','LaneActions.ps1','LaneHistory.ps1','LaneActivity.ps1','LaneIntegration.ps1','LaneReleaseRequests.ps1','LanePulls.ps1','LaneProjectsUi.ps1', 'LaneTheme.ps1','BuildLane.profile.json','BuildLane.ico','LaneTaskbar.cs','Invoke-LaneResource.ps1','LaneJob.cs')
foreach ($Name in $Files) { if ($Name -match '\.(exe|ps1)$') { Sign-Artifact (Join-Path $Stage $Name) } }
$Cabinet=Join-Path $OutputDirectory 'Buildlane.cab'
$Ddf=Join-Path $OutputDirectory 'Buildlane.ddf'
$Lines=@('.OPTION EXPLICIT','.Set Cabinet=on','.Set Compress=on','.Set CompressionType=MSZIP', '.Set CabinetNameTemplate=Buildlane.cab', ('.Set DiskDirectoryTemplate="'+$OutputDirectory+'"'), ('.Set InfFileName="'+$OutputDirectory+'\Buildlane.inf"'), ('.Set RptFileName="'+$OutputDirectory+'\Buildlane.rpt"'))
for ($Index=0;$Index -lt $Files.Count;$Index++) { $Lines += '"'+(Join-Path $Stage $Files[$Index])+'" F'+$Index }
[IO.File]::WriteAllLines($Ddf,$Lines,[Text.Encoding]::Default)
& makecab.exe /F $Ddf | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cabinet creation failed.' }
$Suffix=if ($CertificateThumbprint) { '' } else { '-unsigned' }
$Msi=Join-Path $OutputDirectory "Buildlane-$Version$Suffix.msi"
if (Test-Path -LiteralPath $Msi) { Remove-Item -LiteralPath $Msi -Force }
$Installer=New-Object -ComObject WindowsInstaller.Installer
$Database=$Installer.OpenDatabase($Msi,3)
function Sql([string]$Statement) {
    $View=$Database.OpenView($Statement)
    try { $View.Execute() } finally { $View.Close(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($View) }
}
function Row([string]$Table,[string[]]$Columns,[object[]]$Values) {
    $Fields=($Columns | ForEach-Object { '`'+$_+'`' }) -join ','
    $Data=($Values | ForEach-Object { if ($null -eq $_) { 'NULL' } elseif ($_ -is [int]) { [string]$_ } else { "'"+([string]$_).Replace("'","''")+"'" } }) -join ','
    Sql ('INSERT INTO `'+$Table+'` ('+$Fields+') VALUES ('+$Data+')')
}
function Stable-Guid([string]$Name) {
    $Hash=[Security.Cryptography.SHA256]::Create()
    try { $Bytes=$Hash.ComputeHash([Text.Encoding]::UTF8.GetBytes('Buildlane.Windows.'+$Name)); ([guid]::new([byte[]]$Bytes[0..15])).ToString('B').ToUpperInvariant() }
    finally { $Hash.Dispose() }
}
try {
    Sql 'CREATE TABLE `Property` (`Property` CHAR(72) NOT NULL, `Value` CHAR(0) LOCALIZABLE PRIMARY KEY `Property`)'
    Sql 'CREATE TABLE `Directory` (`Directory` CHAR(72) NOT NULL, `Directory_Parent` CHAR(72), `DefaultDir` CHAR(255) NOT NULL LOCALIZABLE PRIMARY KEY `Directory`)'
    Sql 'CREATE TABLE `Component` (`Component` CHAR(72) NOT NULL, `ComponentId` CHAR(38), `Directory_` CHAR(72) NOT NULL, `Attributes` SHORT NOT NULL, `Condition` CHAR(255), `KeyPath` CHAR(72) PRIMARY KEY `Component`)'
    Sql 'CREATE TABLE `Feature` (`Feature` CHAR(38) NOT NULL, `Feature_Parent` CHAR(38), `Title` CHAR(64) LOCALIZABLE, `Description` CHAR(255) LOCALIZABLE, `Display` SHORT, `Level` SHORT NOT NULL, `Directory_` CHAR(72), `Attributes` SHORT NOT NULL PRIMARY KEY `Feature`)'
    Sql 'CREATE TABLE `FeatureComponents` (`Feature_` CHAR(38) NOT NULL, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Feature_`, `Component_`)'
    Sql 'CREATE TABLE `File` (`File` CHAR(72) NOT NULL, `Component_` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL LOCALIZABLE, `FileSize` LONG NOT NULL, `Version` CHAR(72), `Language` CHAR(20), `Attributes` SHORT, `Sequence` SHORT NOT NULL PRIMARY KEY `File`)'
    Sql 'CREATE TABLE `Media` (`DiskId` SHORT NOT NULL, `LastSequence` SHORT NOT NULL, `DiskPrompt` CHAR(64) LOCALIZABLE, `Cabinet` CHAR(255), `VolumeLabel` CHAR(32), `Source` CHAR(72) PRIMARY KEY `DiskId`)'
    Sql 'CREATE TABLE `Signature` (`Signature` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL, `MinVersion` CHAR(20), `MaxVersion` CHAR(20), `MinSize` LONG, `MaxSize` LONG, `MinDate` LONG, `MaxDate` LONG, `Languages` CHAR(255) PRIMARY KEY `Signature`)'
    Sql 'CREATE TABLE `AppSearch` (`Property` CHAR(72) NOT NULL, `Signature_` CHAR(72) NOT NULL PRIMARY KEY `Property`, `Signature_`)'
    Sql 'CREATE TABLE `RegLocator` (`Signature_` CHAR(72) NOT NULL, `Root` SHORT NOT NULL, `Key` CHAR(255) NOT NULL, `Name` CHAR(255), `Type` SHORT PRIMARY KEY `Signature_`)'
    Sql 'CREATE TABLE `Registry` (`Registry` CHAR(72) NOT NULL, `Root` SHORT NOT NULL, `Key` CHAR(255) NOT NULL LOCALIZABLE, `Name` CHAR(255) LOCALIZABLE, `Value` CHAR(0) LOCALIZABLE, `Component_` CHAR(72) NOT NULL PRIMARY KEY `Registry`)'
    Sql 'CREATE TABLE `Shortcut` (`Shortcut` CHAR(72) NOT NULL, `Directory_` CHAR(72) NOT NULL, `Name` CHAR(128) NOT NULL LOCALIZABLE, `Component_` CHAR(72) NOT NULL, `Target` CHAR(255) NOT NULL LOCALIZABLE, `Arguments` CHAR(255) LOCALIZABLE, `Description` CHAR(255) LOCALIZABLE, `Hotkey` SHORT, `Icon_` CHAR(72), `IconIndex` SHORT, `ShowCmd` SHORT, `WkDir` CHAR(72) PRIMARY KEY `Shortcut`)'
    Sql 'CREATE TABLE `MsiShortcutProperty` (`MsiShortcutProperty` CHAR(72) NOT NULL, `Shortcut_` CHAR(72) NOT NULL, `PropertyKey` CHAR(0) NOT NULL, `PropVariantValue` CHAR(0) NOT NULL PRIMARY KEY `MsiShortcutProperty`)'
    Sql 'CREATE TABLE `RemoveFile` (`FileKey` CHAR(72) NOT NULL, `Component_` CHAR(72) NOT NULL, `FileName` CHAR(255) LOCALIZABLE, `DirProperty` CHAR(72) NOT NULL, `InstallMode` SHORT NOT NULL PRIMARY KEY `FileKey`)'
    Sql 'CREATE TABLE `InstallExecuteSequence` (`Action` CHAR(72) NOT NULL, `Condition` CHAR(255), `Sequence` SHORT PRIMARY KEY `Action`)'
    Sql 'CREATE TABLE `Upgrade` (`UpgradeCode` CHAR(38) NOT NULL, `VersionMin` CHAR(20), `VersionMax` CHAR(20), `Language` CHAR(255), `Attributes` LONG NOT NULL, `Remove` CHAR(255), `ActionProperty` CHAR(72) NOT NULL PRIMARY KEY `UpgradeCode`, `VersionMin`, `VersionMax`, `Language`, `Attributes`)'
    Sql 'CREATE TABLE `LaunchCondition` (`Condition` CHAR(255) NOT NULL, `Description` CHAR(255) NOT NULL LOCALIZABLE PRIMARY KEY `Condition`)'
    $UpgradeCode=Stable-Guid 'upgrade'
    $ProductCode=Stable-Guid "product.$Version"
    $Properties=@{ProductCode=$ProductCode;ProductName='Buildlane';ProductVersion=$Version;ProductLanguage='1033';Manufacturer='Stonefletcher Productions LLC';UpgradeCode=$UpgradeCode;INSTALLLEVEL='1';ARPNOMODIFY='1';SecureCustomProperties='OLDERPRODUCTS;NEWERPRODUCTFOUND';MSIFASTINSTALL='7';REINSTALLMODE='amus'}
    foreach ($Pair in $Properties.GetEnumerator()) { Row 'Property' @('Property','Value') @($Pair.Key,$Pair.Value) }
    foreach ($Dir in @(@('TARGETDIR',$null,'SourceDir'),@('LocalAppDataFolder','TARGETDIR','.'),@('ProgramsRoot','LocalAppDataFolder','Programs'),@('APPDIR','ProgramsRoot','Buildlane'),@('DesktopFolder','TARGETDIR','.'),@('DESKTOPROOT','DesktopFolder','.'),@('ProgramMenuFolder','TARGETDIR','.'),@('STARTMENUROOT','ProgramMenuFolder','Buildlane'))) { Row 'Directory' @('Directory','Directory_Parent','DefaultDir') $Dir }
    foreach ($DirectoryProperty in @('APPDIR','DESKTOPROOT','STARTMENUROOT')) {
        Row 'AppSearch' @('Property','Signature_') @($DirectoryProperty,$DirectoryProperty)
        Row 'RegLocator' @('Signature_','Root','Key','Name','Type') @($DirectoryProperty,1,'Software\Buildlane\InstallPaths',$DirectoryProperty,2)
        Row 'Registry' @('Registry','Root','Key','Name','Value','Component_') @($DirectoryProperty,1,'Software\Buildlane\InstallPaths',$DirectoryProperty,('['+$DirectoryProperty+']'),'C0')
    }
    Row 'Feature' @('Feature','Title','Display','Level','Directory_','Attributes') @('DesktopClient','Buildlane',1,1,'APPDIR',0)
    for ($Index=0;$Index -lt $Files.Count;$Index++) {
        $FileId='F'+$Index; $Component='C'+$Index; $RegistryId='R'+$Index
        Row 'Component' @('Component','ComponentId','Directory_','Attributes','KeyPath') @($Component,(Stable-Guid $Files[$Index]),'APPDIR',4,$RegistryId)
        Row 'Registry' @('Registry','Root','Key','Name','Value','Component_') @($RegistryId,1,'Software\Buildlane\Installer',$Files[$Index],$Version,$Component)
        Row 'FeatureComponents' @('Feature_','Component_') @('DesktopClient',$Component)
        $FileVersion=if ($Index -eq 0) { [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $Stage $Files[0])).FileVersion } else { $null }
        Row 'File' @('File','Component_','FileName','FileSize','Version','Attributes','Sequence') @($FileId,$Component,$Files[$Index],[int](Get-Item (Join-Path $Stage $Files[$Index])).Length,$FileVersion,512,($Index+1))
    }
    foreach ($Target in @('DESKTOPROOT','STARTMENUROOT')) { Row 'Shortcut' @('Shortcut','Directory_','Name','Component_','Target','Description','ShowCmd','WkDir') @($Target,$Target,'Buildlane','C0','[APPDIR]Buildlane.exe','Buildlane projects, builds and releases',1,'APPDIR') }
    foreach ($Target in @('DESKTOPROOT','STARTMENUROOT')) {
        Row 'MsiShortcutProperty' @('MsiShortcutProperty','Shortcut_','PropertyKey','PropVariantValue') @(($Target+'AppId'),$Target,'System.AppUserModel.ID','Stonefletcher.BuildLane.Desktop')
    }
    Row 'RemoveFile' @('FileKey','Component_','DirProperty','InstallMode') @('StartMenuFolder','C0','STARTMENUROOT',2)
    Row 'Media' @('DiskId','LastSequence','Cabinet') @(1,$Files.Count,'#Buildlane.cab')
    Row 'Upgrade' @('UpgradeCode','VersionMax','Attributes','ActionProperty') @($UpgradeCode,$Version,1,'OLDERPRODUCTS')
    Row 'Upgrade' @('UpgradeCode','VersionMin','Attributes','ActionProperty') @($UpgradeCode,$Version,2,'NEWERPRODUCTFOUND')
    Row 'LaunchCondition' @('Condition','Description') @('NOT NEWERPRODUCTFOUND','A newer Buildlane version is already installed.')
    Row 'LaunchCondition' @('Condition','Description') @('VersionNT >= 601','Buildlane requires a supported Windows desktop.')
    $Sequence=[ordered]@{FindRelatedProducts=25;AppSearch=50;LaunchConditions=100;ValidateProductID=700;CostInitialize=800;FileCost=900;CostFinalize=1000;MigrateFeatureStates=1200;InstallValidate=1400;InstallInitialize=1500;RemoveExistingProducts=1550;ProcessComponents=1600;UnpublishFeatures=1800;RemoveShortcuts=3200;RemoveRegistryValues=3300;RemoveFiles=3500;InstallFiles=4000;CreateShortcuts=4500;WriteRegistryValues=5000;RegisterUser=6000;RegisterProduct=6100;PublishFeatures=6300;PublishProduct=6400;InstallFinalize=6600}
    foreach ($Pair in $Sequence.GetEnumerator()) { Row 'InstallExecuteSequence' @('Action','Sequence') @($Pair.Key,[int]$Pair.Value) }
    $Stream=$Database.OpenView('INSERT INTO `_Streams` (`Name`, `Data`) VALUES (?, ?)')
    $Record=$Installer.CreateRecord(2); $Record.StringData(1)='Buildlane.cab'; $Record.SetStream(2,$Cabinet)
    $Stream.Execute($Record); $Stream.Close()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Record)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Stream)
    $Summary=$Database.SummaryInformation(20)
    $Summary.Property(2)='Buildlane'; $Summary.Property(3)='Desktop build dashboard'
    $Summary.Property(7)='Intel;1033'; $Summary.Property(9)=[guid]::NewGuid().ToString('B').ToUpperInvariant()
    $Summary.Property(14)=200; $Summary.Property(15)=10; $Summary.Persist()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Summary)
    $Database.Commit()
} finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Database)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($Installer)
}
Sign-Artifact $Msi
@{Version=$Version;ProductCode=$ProductCode;Signed=[bool]$CertificateThumbprint;SHA256=(Get-FileHash $Msi).Hash;Installer=$Msi} | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory 'build.json')
Write-Output $Msi
