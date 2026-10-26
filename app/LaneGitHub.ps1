# Runs in an isolated PowerShell runspace. Never touches Windows Forms controls.
param([string]$Repository, [string]$BaseBranch, [string]$CredentialFile, [string]$AccessToken, [string]$Kind = 'releases')
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try {
    if (-not $AccessToken -and (Test-Path -LiteralPath $CredentialFile)) {
        $Secure = (Get-Content -LiteralPath $CredentialFile -Raw).Trim() | ConvertTo-SecureString
        $AccessToken = [Net.NetworkCredential]::new('', $Secure).Password
    }
    if (-not $AccessToken) {
        $Cli = Get-Command gh -ErrorAction SilentlyContinue
        if ($Cli) {
            $Start = New-Object Diagnostics.ProcessStartInfo
            $Start.FileName = $Cli.Source; $Start.Arguments = 'auth token'
            $Start.UseShellExecute = $false; $Start.CreateNoWindow = $true
            $Start.RedirectStandardOutput = $true; $Start.RedirectStandardError = $true
            $Child = [Diagnostics.Process]::Start($Start)
            try {
                $Output = $Child.StandardOutput.ReadToEndAsync(); $Errors = $Child.StandardError.ReadToEndAsync()
                if (-not $Child.WaitForExit(8000)) { $Child.Kill(); throw 'GitHub sign-in lookup timed out.' }
                if ($Child.ExitCode -eq 0) { $AccessToken = $Output.Result.Trim() }
            } finally { $Child.Dispose() }
        }
    }
    if (-not $AccessToken) { throw 'Connect GitHub to view releases.' }
    if ($Kind -eq 'auth') { return @{ Token = $AccessToken } }
    $Headers = @{ Authorization="Bearer $AccessToken"; Accept='application/vnd.github+json'; 'User-Agent'='Buildlane-Desktop' }
    function Read-Pages([string]$Path) {
        $Items = New-Object 'Collections.Generic.List[object]'
        for ($Page = 1; $Page -le 100; $Page++) {
            $Response = Invoke-WebRequest -UseBasicParsing -Uri "https://api.github.com/repos/$Repository$Path&per_page=100&page=$Page" -Headers $Headers -TimeoutSec 15
            # Windows PowerShell 5.1 emits the JSON array as a single pipeline object.
            # Enumerate it before counting pages or collecting individual records.
            $Batch = @($Response.Content | ConvertFrom-Json | ForEach-Object { $_ })
            foreach ($Item in $Batch) { $Items.Add($Item) }
            if ($Batch.Count -lt 100) { return ,$Items.ToArray() }
        }
        throw 'Too many results to show safely. Narrow this project before managing releases.'
    }
    $Open = Read-Pages '/milestones?state=open'
    $Closed = Read-Pages '/milestones?state=closed'
    $Prs = Read-Pages ("/pulls?state=open&base=" + [uri]::EscapeDataString($BaseBranch))
    @{ Token=$AccessToken; Open=$Open; Closed=$Closed; Prs=$Prs }
} catch {
    $Delay = 60
    $Response = $_.Exception.Response
    if ($Response) {
        $Retry = 0
        if ([int]::TryParse([string]$Response.Headers['Retry-After'], [ref]$Retry)) { $Delay = [math]::Max(60, $Retry) }
        $Reset = 0L
        if ($Response.Headers['X-RateLimit-Remaining'] -eq '0' -and [long]::TryParse([string]$Response.Headers['X-RateLimit-Reset'], [ref]$Reset)) {
            $Delay = [math]::Max($Delay, [int]([DateTimeOffset]::FromUnixTimeSeconds($Reset).UtcDateTime - [datetime]::UtcNow).TotalSeconds + 1)
        }
    }
    @{ Error=$_.Exception.Message; RetrySeconds=$Delay }
}
