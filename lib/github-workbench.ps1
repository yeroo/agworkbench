<#
.SYNOPSIS
  github-workbench <issue> - open a two-pane Claude + Codex workbench for a GitHub issue.

.DESCRIPTION
  Claude Code on the left, Codex on the right, in one agwinterm session - the layout from
  umputun's agterm cookbook recipe `two-agent-chat`, on Windows. Claude starts by running
  /start-github-issue, which drives the loop: agree a plan with Codex, Codex implements, Claude
  reviews with revmux, Codex fixes, and the human reviews and merges the PR. Revdiff opens
  automatically only outside queue mode.

  Run it from PowerShell or cmd. Inside agwinterm or agliteterm it adopts the caller's session;
  -NewSession keeps the separate-session/resume behavior. From another terminal it starts agwinterm and opens
  the session there.

  Each issue gets its own full clone under ~/source/workbench, on branch issue-<n>-<slug>, and its
  own mailbox in .workbench/ inside that clone. Running it again for the same issue resumes.
  Inside agwinterm, -Queue runs an issue list or watched label in separate sessions.

.EXAMPLE
  github-workbench 42                     # issue 42 of the repo in the current directory
  github-workbench yeroo/agworkbench#7
  github-workbench https://github.com/yeroo/agworkbench/issues/7
  github-workbench 7 -DryRun              # print what would happen, touch nothing
  github-workbench -Version              # report the installed toolchain
.EXAMPLE
  github-workbench -Queue 'yeroo/agworkbench#7,10' -Parallel 2
.EXAMPLE
  github-workbench -Queue 'label:ready' -Repo yeroo/agworkbench -Watch
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)] [string] $Issue,
    [string] $Repo,
    [switch] $DryRun,
    [switch] $Yes,
    [switch] $NoRelay,
    [switch] $NewSession,
    [string] $Queue,
    [int] $Parallel,
    [switch] $Watch,
    [switch] $Retry,
    [string] $QueueMember,
    [int] $QueueAttempt,
    [string] $QueueToken,
    [switch] $Version
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')

if ($Version) {
    $others = @($PSBoundParameters.Keys | Where-Object { $_ -ne 'Version' } | Sort-Object)
    if ($others) {
        Write-Host "-Version takes no other arguments (got: $($others -join ', '))" -ForegroundColor Yellow
        exit 2
    }
    Get-ToolchainVersions | ForEach-Object { "{0,-12} {1}" -f $_.Name, $_.Version }
    exit 0
}

if ($PSBoundParameters.ContainsKey('Queue')) {
    if (-not $Queue -or $Issue -or $NewSession -or $NoRelay -or $QueueMember -or $QueueAttempt -or $QueueToken -or
        (-not (Test-InsideAgwinterm)) -or ($PSBoundParameters.ContainsKey('Parallel') -and ($Parallel -lt 1 -or $Parallel -gt 8))) {
        Write-Host 'Queue requires agwinterm, a spec and Parallel 1..8; Issue/NewSession/NoRelay/internal member options cannot be combined with it.'
        exit 2
    }
    $queueArgs = @((Join-Path $script:Lib 'conductor.py'), 'start', '--spec', $Queue)
    if ($Repo) { $queueArgs += @('--repo', $Repo) }
    if ($PSBoundParameters.ContainsKey('Parallel')) { $queueArgs += @('--parallel', "$Parallel") }
    if ($Watch) { $queueArgs += '--watch' }
    if ($Retry) { $queueArgs += '--retry' }
    if ($Yes) { $queueArgs += '--yes' }
    if ($DryRun) { $queueArgs += '--dry-run' }
    & python @queueArgs
    exit $LASTEXITCODE
}
if ($PSBoundParameters.ContainsKey('Parallel') -or $Watch -or $Retry -or
    ((-not $QueueMember) -and ($QueueAttempt -or $QueueToken))) {
    Write-Host 'Parallel/Watch/Retry require Queue; QueueAttempt/QueueToken require QueueMember.'
    exit 2
}

if (-not $Issue) {
    Write-Host "usage: github-workbench <issue> [-Repo owner/name] [-DryRun] [-Yes] [-NewSession]" -ForegroundColor Yellow
    Write-Host "       github-workbench -Version"
    Write-Host "       github-workbench -Queue <spec> [-Repo owner/name] [-Parallel 1..8] [-Watch] [-Retry] [-Yes] [-DryRun]"
    Write-Host "  <issue> is 123, owner/repo#123, or https://github.com/owner/repo/issues/123"
    exit 2
}

$script:Launch = @{ Stage = 'config'; IssueRef = $Issue; DryRun = [bool]$DryRun; NoRelay = [bool]$NoRelay }
if ($QueueMember) {
    if ($DryRun -or $NoRelay -or $QueueAttempt -lt 1 -or -not (Test-SessionGuid $QueueToken) -or
        $Issue -notmatch '^([^/#]+/[^/#]+)#([1-9][0-9]*)$') {
        Write-Host 'QueueMember requires a qualified issue, attempt/token and unattended setup with its relay.'
        exit 2
    }
    $memberNumber = [int]$Matches[2]
    $memberRepo = $Matches[1]
    $memberLockPath = Join-Path ([IO.Path]::ChangeExtension([IO.Path]::GetFullPath($QueueMember), [NullString]::Value)) "member-$memberNumber.lock"
    try { $memberLock = [IO.File]::Open($memberLockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { Write-Host "Queue member launch already in progress: $memberLockPath"; exit 75 }
    try {
        $contextArgs = @((Join-Path $script:Lib 'conductor.py'), 'member-context', '--file', $QueueMember,
            '--number', "$memberNumber", '--attempt', "$QueueAttempt", '--token', $QueueToken)
        $contextText = & python @contextArgs
        if ($LASTEXITCODE -ne 0) { exit 2 }
        $context = $contextText | ConvertFrom-Json
        if ($context.repo -ne $memberRepo) { Write-Host 'Queue repository mismatch'; exit 2 }
        $script:Launch.QueueContext = $context
        $env:AGWORKBENCH_CONFIG = $context.config
        # Reads have a short deadline; cloning uses the overall launcher deadline.
        function gh {
            & python (Join-Path $script:Lib 'conductor.py') gh-proxy -- @args
            $global:LASTEXITCODE = $LASTEXITCODE
        }
        Enable-LaunchLog
        $ok = Invoke-LaunchSafely {
            Invoke-LauncherBody -Issue $Issue -Repo $Repo -Yes:$Yes -NewSession
        }
        $outcome = 'ok'
        if (-not $ok) { $outcome = 'failed' }
        if ($script:Launch.QueueIncomplete) { $outcome = 'incomplete' }
        $result = @{ result = $outcome; checkout = $script:Launch.Checkout; sessionId = $script:Launch.SessionId;
            claudePane = $script:Launch.Claude; codexPane = $script:Launch.Codex; relaySession = $script:Launch.RelaySession;
            detail = $null }
        if ($outcome -ne 'ok') { $result.detail = "$($script:Launch.Failure)`n$(Format-RepairMessage $script:Launch)" }
        $resultPath = Join-Path (Split-Path -Parent $memberLockPath) ("result-$QueueToken.json")
        try {
            Write-AtomicJson $resultPath $result
            $resultArgs = @((Join-Path $script:Lib 'conductor.py'), 'member-result', '--file', $QueueMember,
                '--number', "$memberNumber", '--attempt', "$QueueAttempt", '--token', $QueueToken, '--result-file', $resultPath)
            & python @resultArgs
            if ($LASTEXITCODE -ne 0) { exit 1 }
        } finally { if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath } }
        if (-not $ok) { exit 1 }
        exit 0
    } finally { $memberLock.Dispose() }
}
if ($DryRun) { Disable-LaunchLog } else { Enable-LaunchLog }
if (-not (Invoke-LaunchSafely {
    Invoke-LauncherBody -Issue $Issue -Repo $Repo -DryRun:$DryRun -Yes:$Yes -NoRelay:$NoRelay -NewSession:$NewSession
})) { exit $script:Launch.ExitCode }
if ($script:Launch.ClaudeHerePending -and -not $DryRun) {
    Invoke-ClaudeHere -Checkout $script:Launch.Checkout -Issue $script:Launch.IssueRef
    exit $LASTEXITCODE
}
exit 0
