# Runs in an isolated PowerShell runspace. Never touches Windows Forms controls.
param([string]$Repository, [string]$BaseBranch, [string]$CredentialFile, [string]$AccessToken, [string]$Kind = 'releases', [string]$Workflow, [datetime]$RequestedAfterUtc = [datetime]::MinValue, [string]$RunId)
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
    if ($Kind -eq 'pulls') {
        $Parts=$Repository.Split('/')
        $Query='query($owner:String!,$name:String!,$after:String,$base:String!){repository(owner:$owner,name:$name){open:pullRequests(states:OPEN,first:50,after:$after,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number title headRefOid baseRefName isDraft reviewDecision mergeable mergeStateStatus commits(last:1){nodes{commit{statusCheckRollup{state}}}}}pageInfo{hasNextPage endCursor}}merged:pullRequests(states:MERGED,baseRefName:$base,first:20,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number title baseRefName mergedAt}}}}'
        $Prs=@(); $Merged=@(); $Cursor=$null
        for ($Page=1;$Page -le 10;$Page++) {
            $Body=@{query=$Query;variables=@{owner=$Parts[0];name=$Parts[1];after=$Cursor;base=$BaseBranch}} | ConvertTo-Json -Depth 8 -Compress
            $Response=Invoke-WebRequest -UseBasicParsing -Uri 'https://api.github.com/graphql' -Method Post -Body $Body -ContentType 'application/json' -Headers $Headers -TimeoutSec 15
            $Data=$Response.Content | ConvertFrom-Json
            if ($Data.errors -or -not $Data.data.repository.open.pageInfo) { throw 'Pull request status could not be verified.' }
            $Connection=$Data.data.repository.open
            $Prs+=@($Connection.nodes)
            if ($Page -eq 1) { $Merged=@($Data.data.repository.merged.nodes | Sort-Object mergedAt -Descending) }
            if (-not $Connection.pageInfo.hasNextPage) { break }
            if ($Page -eq 10 -or -not $Connection.pageInfo.endCursor -or $Cursor -eq $Connection.pageInfo.endCursor) { throw 'Too many pull requests or incomplete pagination. Previous results retained.' }
            $Cursor=$Connection.pageInfo.endCursor
        }
        return @{Token=$AccessToken;Repo=$Repository;Branch=$BaseBranch;AtUtc=[datetime]::UtcNow;Pulls=@($Prs | Sort-Object number -Unique);Merged=$Merged}
    }
    if ($Kind -eq 'history') {
        if ($RunId -notmatch '^\d+$') { throw 'A numeric run ID is required.' }
        function Read-Json([string]$Path) { (Invoke-WebRequest -UseBasicParsing -Uri "https://api.github.com/repos/$Repository$Path" -Headers $Headers -TimeoutSec 15).Content | ConvertFrom-Json }
        function Seconds($Start,$End) {
            $A=[DateTimeOffset]::MinValue; $B=[DateTimeOffset]::MinValue
            if ([DateTimeOffset]::TryParse([string]$Start,[ref]$A) -and [DateTimeOffset]::TryParse([string]$End,[ref]$B) -and $B -ge $A -and $A.Year -gt 2000) { return [math]::Round(($B-$A).TotalSeconds,1) }
            return $null
        }
        $Run=Read-Json "/actions/runs/$RunId"
        if ([string]$Run.id -ne $RunId -or [int]$Run.run_attempt -lt 1) { throw 'Run identity did not match.' }
        $Jobs=@()
        for ($Page=1;$Page -le 10;$Page++) {
            $Batch=@((Read-Json "/actions/runs/$RunId/attempts/$($Run.run_attempt)/jobs?per_page=100&page=$Page").jobs)
            if (@($Batch | Where-Object { [string]$_.run_id -ne $RunId }).Count) { throw 'Job identity did not match.' }
            $Jobs+=@($Batch)
            if ($Batch.Count -lt 100) { break }
            if ($Page -eq 10) { throw 'Too many jobs to display safely.' }
        }
        $After=Read-Json "/actions/runs/$RunId"
        if ([string]$After.id -ne $RunId -or [int]$After.run_attempt -ne [int]$Run.run_attempt -or $After.head_sha -ne $Run.head_sha) { throw 'The run attempt changed while timings were loading. Refresh to load the new attempt.' }
        $First=@($Jobs | Where-Object started_at | Sort-Object started_at | Select-Object -First 1)
        $Last=@($Jobs | Where-Object completed_at | Sort-Object completed_at -Descending | Select-Object -First 1)
        $StoredJobs=@(); $StepCount=0
        foreach ($J in $Jobs) {
            $Steps=@()
            foreach ($S in $J.steps) {
                if (++$StepCount -gt 300) { throw 'Too many steps to retain safely.' }
                $Step=@{name=$S.name;result=$(if ($S.conclusion) { $S.conclusion } else { $S.status });seconds=$null}
                if ($S.conclusion -ne 'skipped') { $Duration=Seconds $S.started_at $S.completed_at; if ($null -ne $Duration) { $Step.seconds=[double]$Duration } }
                $Steps+=@($Step)
            }
            $StoredJob=@{name=$J.name;result=$(if ($J.conclusion) { $J.conclusion } else { $J.status });seconds=$null;steps=$Steps}
            if ($J.conclusion -ne 'skipped') { $Duration=Seconds $J.started_at $J.completed_at; if ($null -ne $Duration) { $StoredJob.seconds=[double]$Duration } }
            $StoredJobs+=@($StoredJob)
        }
        $History=@{id=[string]$Run.id;attempt=[int]$Run.run_attempt;repo=$Repository;sha=$Run.head_sha;status=$Run.status;conclusion=$Run.conclusion;what='';jobs=$StoredJobs;beforeJobsSeconds=$null;jobSpanSeconds=$null}
        # A rerun keeps the original created_at; do not count time between attempts as queue wait.
        $Origin=if ([int]$Run.run_attempt -eq 1) { $Run.created_at } else { $Run.run_started_at }
        $History.beforeJobsBasis=if ([int]$Run.run_attempt -eq 1) { 'run creation' } else { 'GitHub run start for this attempt' }
        if ($First.Count) { $Duration=Seconds $Origin $First[0].started_at; if ($null -ne $Duration) { $History.beforeJobsSeconds=[double]$Duration } }
        if ($Run.status -eq 'completed' -and $First.Count -and $Last.Count) { $Duration=Seconds $First[0].started_at $Last[0].completed_at; if ($null -ne $Duration) { $History.jobSpanSeconds=[double]$Duration } }
        return @{History=$History}
    }
    if ($Kind -eq 'controller') {
        $Uri="https://api.github.com/repos/$Repository/actions/workflows/$([uri]::EscapeDataString($Workflow))/runs?event=workflow_dispatch&branch=$([uri]::EscapeDataString($BaseBranch))&per_page=30"
        $Response=Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers $Headers -TimeoutSec 15
        $Runs=($Response.Content | ConvertFrom-Json).workflow_runs
        $Run=$Runs | Where-Object { [DateTimeOffset]::Parse($_.created_at).UtcDateTime -ge $RequestedAfterUtc.ToUniversalTime().AddSeconds(-1) } | Sort-Object created_at -Descending | Select-Object -First 1
        return @{Controller=$Run}
    }
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
