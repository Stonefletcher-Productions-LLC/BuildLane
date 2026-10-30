# Window-free transport and classification checks; no network or credentials.
$ErrorActionPreference='Stop'
function Assert($Ok,$Message) { if (-not $Ok) { throw $Message } }
$global:PullReaderPage=0
function Invoke-WebRequest {
    param($Uri,$Method,$Body,$ContentType,$Headers,$TimeoutSec,[switch]$UseBasicParsing)
    $Request=$Body | ConvertFrom-Json
    Assert ($Uri -eq 'https://api.github.com/graphql' -and $Request.variables.base -eq 'Test-Staging') 'Lost GraphQL or staging target.'
    $global:PullReaderPage++
    Assert (($global:PullReaderPage -eq 1 -and -not $Request.variables.after) -or ($global:PullReaderPage -eq 2 -and $Request.variables.after -eq 'page2')) 'Cursor was not followed.'
    $Pr=@{number=$global:PullReaderPage;title='Fixture';headRefOid=('a'*40);baseRefName='Test-Staging';isDraft=$false;reviewDecision='APPROVED';mergeable='MERGEABLE';mergeStateStatus='CLEAN';commits=@{nodes=@(@{commit=@{statusCheckRollup=@{state='SUCCESS'}}})}}
    @{Content=(@{data=@{repository=@{open=@{nodes=@($Pr);pageInfo=@{hasNextPage=($global:PullReaderPage -eq 1);endCursor='page2'}};merged=@{nodes=@(@{number=3;title='Merged fixture';baseRefName='Test-Staging';mergedAt='2026-10-30T00:00:00Z'})}}}} | ConvertTo-Json -Depth 15)}
}
$Result=& "$PSScriptRoot/LaneGitHub.ps1" -Repository example/test -BaseBranch Test-Staging -AccessToken fixture -Kind pulls
Assert (-not $Result.Error -and $Result.Pulls.Count -eq 2 -and $Result.Merged.Count -eq 1 -and $global:PullReaderPage -eq 2) 'Pagination failed or fanned out per PR.'
. "$PSScriptRoot/LanePulls.ps1"
$Pr=$Result.Pulls[0]
Assert ((Get-LanePullState $Pr) -match '^Ready for operator merge') 'Approved clean successful PR was not recognized.'
$Pr.reviewDecision='REVIEW_REQUIRED'; Assert ((Get-LanePullState $Pr) -match '^Review required') 'Checks were mistaken for approval.'
$Pr.reviewDecision='APPROVED'; $Pr.commits.nodes[0].commit.statusCheckRollup.state='FAILURE'; Assert ((Get-LanePullState $Pr) -match '^Fix checks') 'Failed checks were hidden.'
$Pr.mergeable='CONFLICTING'; Assert ((Get-LanePullState $Pr) -match '^Resolve conflicts') 'Conflicts were hidden.'
$Pr.isDraft=$true; Assert ((Get-LanePullState $Pr) -match '^Draft') 'Draft was shown ready.'
function Invoke-WebRequest { @{Content='{"errors":[{"message":"denied"}]}'} }
$ErrorResult=& "$PSScriptRoot/LaneGitHub.ps1" -Repository example/test -BaseBranch Test-Staging -AccessToken fixture -Kind pulls
Assert ($ErrorResult.Error -and -not $ErrorResult.Pulls) 'GraphQL error became an empty successful PR list.'
'PASS: paginated bounded PR read, no per-PR fanout, actual merged records, draft/review/check/conflict distinctions, API failure.'
