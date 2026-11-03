# Builds Fletchery.exe into shell\bin with the .NET Framework compiler that ships with Windows (no Visual Studio).
# The WebView2 SDK comes from nuget.org: the first build pins its version and SHA-256 in webview2.lock.json (commit
# that file); every later build downloads exactly that version and refuses a file whose hash differs.
#   powershell -NoProfile -ExecutionPolicy Bypass -File shell\Build-Fletchery.ps1
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$Bin = Join-Path $Here 'bin'
$Pkgs = Join-Path $Here 'packages'
$Lock = Join-Path $Here 'webview2.lock.json'
New-Item -ItemType Directory -Force $Bin, $Pkgs | Out-Null
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --- WebView2 SDK, pinned ---
if (Test-Path $Lock) {
    $Pin = Get-Content $Lock -Raw | ConvertFrom-Json
} else {
    $Index = Invoke-RestMethod 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/index.json'
    $Version = @($Index.versions | Where-Object { $_ -notmatch '-' })[-1]
    $Pin = [pscustomobject]@{ version = $Version; sha256 = '' }
}
$Nupkg = Join-Path $Pkgs "microsoft.web.webview2.$($Pin.version).nupkg"
if (-not (Test-Path $Nupkg)) {
    Write-Host "Downloading Microsoft.Web.WebView2 $($Pin.version) from nuget.org"
    Invoke-WebRequest -UseBasicParsing "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$($Pin.version)/microsoft.web.webview2.$($Pin.version).nupkg" -OutFile $Nupkg
}
$Hash = (Get-FileHash $Nupkg -Algorithm SHA256).Hash.ToLowerInvariant()
if ($Pin.sha256 -and $Pin.sha256 -ne $Hash) {
    Remove-Item $Nupkg
    throw "WebView2 package hash mismatch: expected $($Pin.sha256), got $Hash. Not building with it."
}
if (-not $Pin.sha256) {
    [pscustomobject]@{ version = $Pin.version; sha256 = $Hash; source = 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/' } |
        ConvertTo-Json | Set-Content $Lock -Encoding ASCII
    Write-Host "Pinned WebView2 $($Pin.version) ($Hash) in webview2.lock.json: commit it."
}
$Extract = Join-Path $Pkgs "webview2-$($Pin.version)"
if (-not (Test-Path $Extract)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($Nupkg, $Extract)
}
function Find-Lib([string]$Name) {
    foreach ($Tfm in 'net462', 'net45') {
        $P = Join-Path $Extract "lib\$Tfm\$Name"
        if (Test-Path $P) { return $P }
    }
    throw "$Name not found in the WebView2 package"
}
$Core = Find-Lib 'Microsoft.Web.WebView2.Core.dll'
$WinForms = Find-Lib 'Microsoft.Web.WebView2.WinForms.dll'
$Loader = Join-Path $Extract 'runtimes\win-x64\native\WebView2Loader.dll'
if (-not (Test-Path $Loader)) { throw 'WebView2Loader.dll (win-x64) not found in the WebView2 package' }

# --- compile ---
$Csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $Csc)) { throw "The .NET Framework 4 compiler was not found at $Csc" }
$Icon = Join-Path (Split-Path $Here -Parent) 'ui\Fletchery.ico'
$Out = Join-Path $Bin 'Fletchery.exe'
& $Csc /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 "/win32icon:$Icon" "/out:$Out" `
    /r:System.dll /r:System.Core.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll /r:System.Web.Extensions.dll `
    "/r:$Core" "/r:$WinForms" (Join-Path $Here 'Fletchery.cs')
if ($LASTEXITCODE -ne 0) { throw "Fletchery.exe did not compile (csc exit $LASTEXITCODE)" }
Copy-Item $Core, $WinForms, $Loader $Bin -Force
Write-Host "Built $Out (WebView2 SDK $($Pin.version))"
