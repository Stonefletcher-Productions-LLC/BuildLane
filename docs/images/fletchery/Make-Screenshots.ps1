# Makes the README's Fletchery screenshots from the made-up "Harbor" demo project (python -m fletchery demo --serve
# --fixture harbor): real screens, illustrative data, nothing from a real project. Uses Microsoft Edge in headless mode,
# so the pictures are what the app's WebView2 window draws. Run on Windows from the repository root:
#   powershell -NoProfile -ExecutionPolicy Bypass -File docs\images\fletchery\Make-Screenshots.ps1
param([int]$Port = 8771, [string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Out = $PSScriptRoot
$Edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Edge) { throw 'Microsoft Edge was not found.' }

$Tmp = Join-Path $env:TEMP "fletchery-shots-$PID"
New-Item -ItemType Directory -Force $Tmp | Out-Null
$DemoProfile = Join-Path $Tmp 'harbor.profile.json'
@{ name = 'Harbor'; workers = @{ dashboardPort = $Port; stateDir = (Join-Path $Tmp 'state') } } | ConvertTo-Json | Set-Content $DemoProfile -Encoding ASCII
$Demo = Start-Process -FilePath $Python -ArgumentList @('-m', 'fletchery', 'demo', '--serve', '--fixture', 'harbor', '--profile', $DemoProfile) `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru
try {
    $Url = "http://127.0.0.1:$Port/"
    for ($i = 0; $i -lt 40; $i++) {
        try { $null = Invoke-WebRequest -UseBasicParsing "$($Url)api/status" -TimeoutSec 2; break } catch { Start-Sleep -Milliseconds 500 }
    }
    Start-Sleep 4  # the two demo workers get going, so the Workers strip has something to show
    $Shots = @(
        @{ Name = 'overview.png'; Path = ''; W = 1400; H = 1000 },
        @{ Name = 'queue.png'; Path = '?only=queue'; W = 1400; H = 640 },
        @{ Name = 'workers.png'; Path = '?only=workers'; W = 1400; H = 460 },
        @{ Name = 'build-lane.png'; Path = '?only=lane'; W = 1400; H = 520 },
        @{ Name = 'usage-week.png'; Path = '?open=week'; W = 1400; H = 900 },
        @{ Name = 'add-task.png'; Path = '?open=add'; W = 1400; H = 900 },
        @{ Name = 'agents.png'; Path = '?only=agents&scores=opus'; W = 1400; H = 1000 },
        @{ Name = 'accounts.png'; Path = '?open=accounts'; W = 1400; H = 1000 },
        @{ Name = 'settings.png'; Path = '?open=settings'; W = 1400; H = 1000 },
        @{ Name = 'projects.png'; Path = '?open=projects'; W = 1400; H = 1000 },
        @{ Name = 'phone.png'; Path = ''; W = 420; H = 1500 }
    )
    foreach ($S in $Shots) {
        $File = Join-Path $Out $S.Name
        Remove-Item $File -ErrorAction SilentlyContinue
        $EdgeArgs = @('--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1.5', "--user-data-dir=$Tmp\edge",
                  "--window-size=$($S.W),$($S.H)", '--virtual-time-budget=6000', "--screenshot=$File", "$Url$($S.Path)")
        $P = Start-Process -FilePath $Edge -ArgumentList $EdgeArgs -WindowStyle Hidden -PassThru -Wait
        if (-not (Test-Path $File)) { throw "No screenshot for $($S.Name)" }
        Write-Host "$($S.Name)  $([math]::Round((Get-Item $File).Length / 1KB)) KB"
    }
} finally {
    if ($Demo -and -not $Demo.HasExited) { taskkill /PID $Demo.Id /T /F | Out-Null }
    Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}
