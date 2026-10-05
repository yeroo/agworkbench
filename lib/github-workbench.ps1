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
  Inside agwinterm, -Queue runs an issue list, or a watched label or query, in separate sessions.

.EXAMPLE
  github-workbench 42                     # issue 42 of the repo in the current directory
  github-workbench yeroo/agworkbench#7
  github-workbench https://github.com/yeroo/agworkbench/issues/7
  github-workbench 7 -DryRun              # print what would happen, touch nothing
  github-workbench -Version              # report the installed toolchain
  github-workbench 7 -Implementer claude  # Claude, not Codex, in the right pane (e.g. Codex out of quota)
  github-workbench 7 -Implementer kimi    # Kimi Code in the right pane (#65)
  github-workbench 7 -Implementer auto    # the router picks the tool and model from implementerRoster (#109)
  github-workbench 7 -Implementer claude -ImplementerModel claude-opus-5-5   # one model for this checkout (#109)
  github-workbench 7 -AutoMerge           # the planner merges its own PR when every condition holds
  github-workbench 7 -Failover            # the planner, on a usage-limit mail: switch the implementer tool
  github-workbench 7 -Autonomous          # merge, file follow-ups and close the sessions without the human
  github-workbench 7 -BigReview           # a big issue: up to review.maxRoundsBig (10) revmux rounds (#75)
  github-workbench 7 -WaitOnLimit         # a usage limit is waited out, never failed over (#77)
.EXAMPLE
  github-workbench -Queue 'yeroo/agworkbench#7,10' -Parallel 2
.EXAMPLE
  github-workbench -Queue 'label:ready' -Repo yeroo/agworkbench -Watch
  github-workbench -Queue 'label:ready' -Repo yeroo/agworkbench -ClearLimit codex   # codex's limit has reset (#61)
.EXAMPLE
  github-workbench -Queue bugs -Repo yeroo/docxy -Autonomous   # every open bug nobody is handling
.EXAMPLE
  github-workbench -Queue bugs -Repo yeroo/docxy -Autonomous -Triage   # P0 first; untriaged triaged first
.EXAMPLE
  # A second, named queue of the same repo (#66): its own workspace (docxy-kimi), checkouts
  # (docxy-kimi-issue-N) and settings; an issue one queue holds is skipped by the other.
  github-workbench -Queue 'where: priority IN [P2]' -Repo yeroo/docxy -QueueName kimi -Implementer kimi -NoAutoMerge -Watch
.EXAMPLE
  github-workbench -Queue 'where: bug AND priority IN [P0, P1] AND NOT wontfix' -Repo yeroo/docxy
  github-workbench -Queue "where: label IN [bug, regression] AND NOT 'needs design'" -Repo yeroo/docxy
.EXAMPLE
  github-workbench -Triage -Repo yeroo/docxy            # label every untriaged open issue priority:P0..P3
  github-workbench -Triage -Repo yeroo/docxy -Watch     # and keep doing it for new ones, in its own session
  github-workbench -Retriage -Repo yeroo/docxy -DryRun  # re-judge the labelled ones too; print, write nothing
  github-workbench -Triage -Retriage -FollowUps -Repo yeroo/docxy -Limit 200
  github-workbench -Triage -Retriage -KimiOnly -Repo yeroo/docxy   # only the kimi label, on open P2/P3 (#77)
.EXAMPLE
  github-workbench -Cleanup -DryRun                     # list finished checkouts and their sizes; delete nothing
  github-workbench -Cleanup -Repo yeroo/docxy           # delete docxy's finished checkouts that are safe to delete
  github-workbench -Cleanup -BuildOnly                  # only their target/, node_modules/, bin/, obj/
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
    [switch] $Prune,
    [switch] $Retry,
    [string] $QueueMember,
    [int] $QueueAttempt,
    [string] $QueueToken,
    [string] $Implementer,
    [string] $ImplementerModel,
    [string] $RosterId,
    [string] $QueueName,
    [string] $Workspace,
    [string] $RevmuxProfile,
    [string] $ClearLimit,
    [switch] $AutoMerge,
    [switch] $NoAutoMerge,
    [switch] $Failover,
    [switch] $Autonomous,
    [switch] $NoAutonomous,
    [switch] $BigReview,
    [switch] $NoBigReview,
    [switch] $WaitOnLimit,
    [switch] $NoWaitOnLimit,
    [switch] $Triage,
    [switch] $Retriage,
    [switch] $FollowUps,
    [switch] $KimiOnly,
    [int] $Limit,
    [switch] $Cleanup,
    [switch] $BuildOnly,
    [switch] $Version,
    [string] $ArgsEnv
)

$ErrorActionPreference = 'Stop'

if ($ArgsEnv) {
    # From the PowerShell entry point (github-workbench.ps1 next to the .cmd, #38): the caller's
    # arguments as a JSON array in a one-off environment variable, bound here by name, exactly as
    # typed - no command line in between that could split them or strip their quotes.
    $raw = [Environment]::GetEnvironmentVariable($ArgsEnv)
    Remove-Item -LiteralPath "Env:\$ArgsEnv" -ErrorAction SilentlyContinue
    if ($PSBoundParameters.Count -ne 1 -or $null -eq $raw) {
        Write-Host '-ArgsEnv is internal to the PowerShell entry point and takes no other arguments.' -ForegroundColor Yellow
        exit 2
    }
    # A foreach statement, not the pipeline: 5.1's ConvertFrom-Json emits the array as one object.
    $tokens = @()
    foreach ($token in (ConvertFrom-Json $raw)) { $tokens += [string]$token }
    $parameters = (Get-Command $PSCommandPath).Parameters
    $named = @{}
    $positional = @()
    for ($i = 0; $i -lt $tokens.Count; $i++) {
        $token = $tokens[$i]
        if ($token -notmatch '^-([A-Za-z][A-Za-z0-9]*)(:(.*))?$') {
            $positional += $token
            continue
        }
        $given, $hasValue, $value = $Matches[1], [bool]$Matches[2], $Matches[3]
        # PowerShell's own rule: a unique prefix of a parameter name (or an alias) names it.
        $found = @($parameters.Values | Where-Object {
                $_.Name -ne 'ArgsEnv' -and ($_.Name -like "$given*" -or @($_.Aliases | Where-Object { $_ -like "$given*" }))
            })
        $exact = @($found | Where-Object { $_.Name -eq $given -or $_.Aliases -contains $given })
        if ($exact.Count -eq 1) { $found = $exact }
        if ($found.Count -ne 1) {
            $why = if ($found.Count) { 'is ambiguous' } else { 'is not a parameter' }
            Write-Host "github-workbench: -$given $why" -ForegroundColor Yellow
            exit 2
        }
        $parameter = $found[0]
        if ($parameter.SwitchParameter) {
            if ($hasValue -and $value -eq '' -and $i + 1 -lt $tokens.Count) { $i++; $value = $tokens[$i] }
            $named[$parameter.Name] = if ($hasValue) { $value -notin @('False', '$false', '0') } else { $true }
            continue
        }
        if (-not $hasValue -or $value -eq '') {
            if ($i + 1 -ge $tokens.Count) {
                Write-Host "github-workbench: -$($parameter.Name) needs a value" -ForegroundColor Yellow
                exit 2
            }
            $i++
            $value = $tokens[$i]
        }
        $named[$parameter.Name] = $value
    }
    & $PSCommandPath @named @positional
    exit $LASTEXITCODE
}
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

if ($Cleanup -or $BuildOnly) {
    # The checkout sweep (#41): lib/cleanup.py, over the configured checkoutRoot.
    $others = @($PSBoundParameters.Keys | Where-Object { $_ -notin @('Cleanup', 'BuildOnly', 'Repo', 'DryRun') })
    if (-not $Cleanup -or $others) {
        Write-Host 'usage: github-workbench -Cleanup [-Repo owner/name] [-DryRun] [-BuildOnly]' -ForegroundColor Yellow
        exit 2
    }
    try { $config = Get-WorkbenchConfig } catch { Write-Host $_ -ForegroundColor Yellow; exit 2 }
    $cleanupArgs = @((Join-Path $script:Lib 'cleanup.py'), 'sweep', '--root', [string]$config.checkoutRoot)
    if ($Repo) { $cleanupArgs += @('--repo', $Repo) }
    if ($DryRun) { $cleanupArgs += '--dry-run' }
    if ($BuildOnly) { $cleanupArgs += '--build-only' }
    & $script:Python @cleanupArgs
    exit $LASTEXITCODE
}

if ($PSBoundParameters.ContainsKey('Implementer') -and $Implementer -cnotin @('codex', 'claude', 'kimi', 'auto')) {
    Write-Host "-Implementer must be codex, claude or kimi, or auto for the router (got '$Implementer')" -ForegroundColor Yellow
    exit 2
}
if ($Implementer -ceq 'auto' -and ($PSBoundParameters.ContainsKey('ImplementerModel') -or $Failover)) {
    Write-Host '-Implementer auto lets the router choose the tool and the model: it cannot be combined with -ImplementerModel or -Failover.' -ForegroundColor Yellow
    exit 2
}
if ($PSBoundParameters.ContainsKey('RosterId') -and (-not $QueueMember -or $RosterId -cnotmatch $script:RosterIdPattern)) {
    Write-Host '-RosterId is the conductor''s: the roster entry a queue member was routed to.' -ForegroundColor Yellow
    exit 2
}

if ($PSBoundParameters.ContainsKey('ImplementerModel')) {
    $modelProblem = Get-ModelProblem $ImplementerModel
    if ($modelProblem) {
        Write-Host "-ImplementerModel: $modelProblem" -ForegroundColor Yellow
        exit 2
    }
    if ($Failover -or $PSBoundParameters.ContainsKey('Queue')) {
        Write-Host '-ImplementerModel belongs to one launch: it cannot be combined with -Failover or -Queue (a queue routes its own models).' -ForegroundColor Yellow
        exit 2
    }
}

if ($PSBoundParameters.ContainsKey('ClearLimit') -and
    ($ClearLimit -cnotin @('codex', 'claude', 'kimi') -or -not $PSBoundParameters.ContainsKey('Queue'))) {
    # A queue's recorded usage limit (#61); a checkout's is cleared by -Implementer <tool> on it.
    Write-Host "-ClearLimit takes codex, claude or kimi and belongs to -Queue (got '$ClearLimit')" -ForegroundColor Yellow
    exit 2
}

if (($QueueName -or $Workspace) -and -not $PSBoundParameters.ContainsKey('Queue')) {
    Write-Host '-QueueName and -Workspace belong to -Queue (a named queue of the repo, #66).' -ForegroundColor Yellow
    exit 2
}
if ($Workspace -and -not $QueueName) {
    Write-Host '-Workspace requires -QueueName: the main queue uses the workspace named after the repo.' -ForegroundColor Yellow
    exit 2
}
if ($QueueName -and $Triage) {
    Write-Host "a named queue does not triage; triage is the repo-wide github-workbench -Triage -Repo <owner/name> -Watch" -ForegroundColor Yellow
    exit 2
}
if ($PSBoundParameters.ContainsKey('RevmuxProfile') -and
    ($RevmuxProfile -notmatch '^[A-Za-z0-9._-]+$' -or -not ($PSBoundParameters.ContainsKey('Queue') -or $QueueMember))) {
    Write-Host "-RevmuxProfile takes a revmux profile name and belongs to -Queue (got '$RevmuxProfile')" -ForegroundColor Yellow
    exit 2
}

if ($Failover -and ($Implementer -or $PSBoundParameters.ContainsKey('Queue') -or $NewSession -or $QueueMember)) {
    Write-Host '-Failover picks the next tool in failoverOrder itself; it cannot be combined with -Implementer, -Queue, -NewSession or a queue member.' -ForegroundColor Yellow
    exit 2
}
if ($Autonomous -and $NoAutonomous) {
    Write-Host '-Autonomous and -NoAutonomous cannot be combined.' -ForegroundColor Yellow
    exit 2
}
if ($Autonomous -and $NoAutoMerge) {
    Write-Host '-Autonomous implies auto-merge; it cannot be combined with -NoAutoMerge.' -ForegroundColor Yellow
    exit 2
}
# $null leaves the checkout's saved autonomy (or the config default) alone.
$autonomousChoice = $null
if ($Autonomous) { $autonomousChoice = $true }
if ($NoAutonomous) { $autonomousChoice = $false }
if ($AutoMerge -and $NoAutoMerge) {
    Write-Host '-AutoMerge and -NoAutoMerge cannot be combined.' -ForegroundColor Yellow
    exit 2
}
# $null leaves the checkout's saved choice (or the config default) alone.
$autoMergeChoice = $null
if ($AutoMerge) { $autoMergeChoice = $true }
if ($NoAutoMerge) { $autoMergeChoice = $false }
if ($BigReview -and $NoBigReview) {
    Write-Host '-BigReview and -NoBigReview cannot be combined.' -ForegroundColor Yellow
    exit 2
}
# $null leaves the checkout's saved choice alone (#75).
$bigReviewChoice = $null
if ($BigReview) { $bigReviewChoice = $true }
if ($NoBigReview) { $bigReviewChoice = $false }
if ($WaitOnLimit -and $NoWaitOnLimit) {
    Write-Host '-WaitOnLimit and -NoWaitOnLimit cannot be combined.' -ForegroundColor Yellow
    exit 2
}
# '' leaves the checkout's saved choice alone (#77).
$onLimitChoice = ''
if ($WaitOnLimit) { $onLimitChoice = 'wait' }
if ($NoWaitOnLimit) { $onLimitChoice = 'failover' }

if ($PSBoundParameters.ContainsKey('Queue')) {
    if ($FollowUps -or ($Prune -and -not $Watch)) {
        Write-Host '-FollowUps belongs to triage; -Prune requires -Queue with -Watch.' -ForegroundColor Yellow
        exit 2
    }
    if (-not $Queue -or $Issue -or $NewSession -or $NoRelay -or $QueueMember -or $QueueAttempt -or $QueueToken -or
        (-not (Test-InsideAgwinterm)) -or ($PSBoundParameters.ContainsKey('Parallel') -and ($Parallel -lt 1 -or $Parallel -gt 8))) {
        Write-Host 'Queue requires agwinterm, a spec and Parallel 1..8; Issue/NewSession/NoRelay/internal member options cannot be combined with it.'
        exit 2
    }
    # The spec travels in the environment, not argv: Windows PowerShell 5.1 strips the double quotes
    # out of a native argument, and a query quotes labels (#38).
    $queueArgs = @((Join-Path $script:Lib 'conductor.py'), 'start', '--spec-env')
    if ($Repo) { $queueArgs += @('--repo', $Repo) }
    if ($PSBoundParameters.ContainsKey('Parallel')) { $queueArgs += @('--parallel', "$Parallel") }
    if ($Watch) { $queueArgs += '--watch' }
    if ($Prune) { $queueArgs += '--prune' }
    if ($Retry) { $queueArgs += '--retry' }
    if ($Yes) { $queueArgs += '--yes' }
    if ($DryRun) { $queueArgs += '--dry-run' }
    if ($Implementer) { $queueArgs += @('--implementer', $Implementer) }
    if ($QueueName) { $queueArgs += @('--name', $QueueName) }
    if ($Workspace) { $queueArgs += @('--workspace', $Workspace) }
    if ($RevmuxProfile) { $queueArgs += @('--revmux-profile', $RevmuxProfile) }
    if ($ClearLimit) { $queueArgs += @('--clear-limit', $ClearLimit) }
    if ($AutoMerge) { $queueArgs += '--auto-merge' }
    if ($NoAutoMerge) { $queueArgs += '--no-auto-merge' }
    if ($Autonomous) { $queueArgs += '--autonomous' }
    if ($NoAutonomous) { $queueArgs += '--no-autonomous' }
    if ($BigReview) { $queueArgs += '--big-review' }
    if ($NoBigReview) { $queueArgs += '--no-big-review' }
    if ($WaitOnLimit) { $queueArgs += '--wait-on-limit' }
    if ($NoWaitOnLimit) { $queueArgs += '--no-wait-on-limit' }
    if ($Triage) { $queueArgs += '--triage' }
    if ($Retriage -or $KimiOnly -or $PSBoundParameters.ContainsKey('Limit')) {
        Write-Host '-Retriage, -KimiOnly and -Limit belong to -Triage without -Queue; a queue triages each untriaged member once.' -ForegroundColor Yellow
        exit 2
    }
    $env:AGWORKBENCH_QUEUE_SPEC = $Queue
    try {
        & $script:Python @queueArgs
        $code = $LASTEXITCODE
    } finally {
        Remove-Item Env:\AGWORKBENCH_QUEUE_SPEC -ErrorAction SilentlyContinue
    }
    exit $code
}
if ($Triage -or $Retriage) {
    # Issue triage (#34): lib/triage.py labels the repo's open issues priority:P0..P3.
    if (-not $Repo -or $Issue -or $NewSession -or $NoRelay -or $QueueMember -or $Retry -or $Implementer -or
        $PSBoundParameters.ContainsKey('Parallel') -or $AutoMerge -or $NoAutoMerge -or $Autonomous -or $NoAutonomous -or $BigReview -or $NoBigReview -or
        $WaitOnLimit -or $NoWaitOnLimit -or $Failover -or $Prune -or ($FollowUps -and $Watch) -or
        ($PSBoundParameters.ContainsKey('Limit') -and $Limit -lt 1) -or ($Watch -and ($Retriage -or $DryRun -or $KimiOnly))) {
        Write-Host 'usage: github-workbench -Triage|-Retriage -Repo owner/name [-Limit N] [-DryRun] [-FollowUps] [-KimiOnly] | -Triage -Repo owner/name -Watch' -ForegroundColor Yellow
        exit 2
    }
    $triageArgs = @((Join-Path $script:Lib 'triage.py'))
    if ($Watch) {
        if (-not (Test-InsideAgwinterm)) { Write-Host '-Triage -Watch requires agwinterm.' -ForegroundColor Yellow; exit 2 }
        $triageArgs += @('start-watch', '--repo', $Repo)
    } else {
        $triageArgs += @('run', '--repo', $Repo)
        if ($Retriage) { $triageArgs += '--retriage' }
        if ($FollowUps) { $triageArgs += '--follow-ups' }
        if ($KimiOnly) { $triageArgs += '--kimi-only' }      # #77: implies --retriage in triage.py
        if ($DryRun) { $triageArgs += '--dry-run' }
    }
    if ($PSBoundParameters.ContainsKey('Limit')) { $triageArgs += @('--limit', "$Limit") }
    & $script:Python @triageArgs
    exit $LASTEXITCODE
}
if ($PSBoundParameters.ContainsKey('Parallel') -or $Watch -or $Retry -or $Prune -or $FollowUps -or $KimiOnly -or $PSBoundParameters.ContainsKey('Limit') -or
    ((-not $QueueMember) -and ($QueueAttempt -or $QueueToken))) {
    Write-Host 'Parallel/Retry require -Queue; -Prune requires -Queue -Watch; -FollowUps and -KimiOnly require -Triage or -Retriage; Watch/Limit also go with Triage; QueueAttempt/QueueToken require QueueMember.'
    exit 2
}

if (-not $Issue) {
    Write-Host "usage: github-workbench <issue> [-Repo owner/name] [-DryRun] [-Yes] [-NewSession] [-Implementer codex|claude|kimi|auto [-ImplementerModel <model>]] [-AutoMerge|-NoAutoMerge] [-Autonomous|-NoAutonomous] [-BigReview|-NoBigReview] [-WaitOnLimit|-NoWaitOnLimit] [-Failover]" -ForegroundColor Yellow
    Write-Host "       github-workbench -Version"
    Write-Host "       (<spec> is a list like 3,4,5, label:<name>, bugs = label:<bugLabel>, or where: <label query>)"
    Write-Host "       github-workbench -Queue <spec> [-Repo owner/name] [-Parallel 1..8] [-Watch] [-Prune] [-Retry] [-Yes] [-DryRun] [-Implementer codex|claude|kimi|auto] [-ClearLimit codex|claude|kimi] [-AutoMerge|-NoAutoMerge] [-Autonomous|-NoAutonomous] [-BigReview|-NoBigReview] [-WaitOnLimit|-NoWaitOnLimit] [-Triage] [-RevmuxProfile <profile>] [-QueueName <name> [-Workspace <ws>]]"
    Write-Host "       github-workbench -Triage|-Retriage -Repo owner/name [-Limit N] [-DryRun] [-FollowUps] [-KimiOnly] [-Watch]"
    Write-Host "       github-workbench -Cleanup [-Repo owner/name] [-DryRun] [-BuildOnly]"
    Write-Host "  <issue> is 123, owner/repo#123, or https://github.com/owner/repo/issues/123"
    exit 2
}

$script:Launch = @{ Stage = 'config'; IssueRef = $Issue; DryRun = [bool]$DryRun; NoRelay = [bool]$NoRelay;
    UnrecordedOpenSessions = @() }
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
        $contextText = & $script:Python @contextArgs
        if ($LASTEXITCODE -ne 0) { exit 2 }
        $context = $contextText | ConvertFrom-Json
        if ($context.repo -ne $memberRepo) { Write-Host 'Queue repository mismatch'; exit 2 }
        $script:Launch.QueueContext = $context
        $env:AGWORKBENCH_CONFIG = $context.config
        # Reads have a short deadline; cloning uses the overall launcher deadline.
        function gh {
            & $script:Python (Join-Path $script:Lib 'conductor.py') gh-proxy -- @args
            $global:LASTEXITCODE = $LASTEXITCODE
        }
        Enable-LaunchLog
        $ok = Invoke-LaunchSafely {
            Invoke-LauncherBody -Issue $Issue -Repo $Repo -Yes:$Yes -NewSession -Implementer $Implementer -AutoMerge $autoMergeChoice `
                -Autonomous $autonomousChoice -RevmuxProfile $RevmuxProfile -BigReview $bigReviewChoice -OnLimit $onLimitChoice -ImplementerModel $ImplementerModel -RosterId $RosterId
        }
        $outcome = 'ok'
        if (-not $ok) { $outcome = 'failed' }
        if ($script:Launch.QueueIncomplete) { $outcome = 'incomplete' }
        $failureStage = $script:Launch.Stage
        $remaining = @()
        if ($script:Launch.Checkout) {
            if ($outcome -ne 'ok') {
                try { $remaining = @(Close-QueueSessions $script:Launch.Checkout $QueueToken) }
                catch {
                    $remaining = @(@($script:Launch.SessionId, $script:Launch.RelaySession) | Where-Object { $_ })
                    $script:Launch.Failure += "`ncleanup failed: $_"
                }
            }
        }
        foreach ($id in @($script:Launch.UnrecordedOpenSessions | Where-Object { $_ })) {
            $stillOpen = $true
            try { $stillOpen = $null -ne (Get-SessionById $id) } catch { $script:Launch.Failure += "`ncannot verify close of ${id}: $_" }
            if ($stillOpen) { $remaining += $id }
        }
        $result = @{ result = $outcome; checkout = $script:Launch.Checkout; sessionId = $script:Launch.SessionId;
            claudePane = $script:Launch.Claude; codexPane = $script:Launch.Codex; relaySession = $script:Launch.RelaySession;
            stage = $failureStage; infra = ($failureStage -in @('terminal', 'window', 'cleanup', 'session', 'split', 'codex', 'claude', 'kimi', 'relay', 'relay-stop', 'focus'));
            detail = $null }
        if ($outcome -ne 'ok') { $result.detail = "$($script:Launch.Failure)`n$(Format-RepairMessage $script:Launch)" }
        if ($outcome -ne 'ok' -and $remaining.Count) { $result.detail += "`nSessions still open: $($remaining -join ', ')" }
        if ($outcome -ne 'ok' -and $script:Launch.SessionId -and $remaining -notcontains $script:Launch.SessionId) {
            $result.sessionId = $null; $result.claudePane = $null; $result.codexPane = $null
        }
        if ($outcome -ne 'ok' -and $script:Launch.RelaySession -and $remaining -notcontains $script:Launch.RelaySession) {
            $result.relaySession = $null
        }
        $resultPath = Join-Path (Split-Path -Parent $memberLockPath) ("result-$QueueToken.json")
        try {
            Write-AtomicJson $resultPath $result
            $resultArgs = @((Join-Path $script:Lib 'conductor.py'), 'member-result', '--file', $QueueMember,
                '--number', "$memberNumber", '--attempt', "$QueueAttempt", '--token', $QueueToken, '--result-file', $resultPath)
            & $script:Python @resultArgs
            if ($LASTEXITCODE -ne 0) {
                if ($script:Launch.Checkout) {
                    try { Close-QueueSessions $script:Launch.Checkout $QueueToken | Out-Null }
                    catch { Write-LaunchLog cleanup "late result cleanup failed: $_" }
                }
                exit 1
            }
            if ($ok -and $script:Launch.Checkout) {
                $recordPath = Get-QueueLaunchPath $script:Launch.Checkout
                if (Test-Path -LiteralPath $recordPath) { Remove-Item -LiteralPath $recordPath }
            }
        } finally { if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath } }
        if (-not $ok) { exit 1 }
        exit 0
    } finally { $memberLock.Dispose() }
}
if ($DryRun) { Disable-LaunchLog } else { Enable-LaunchLog }
if (-not (Invoke-LaunchSafely {
    Invoke-LauncherBody -Issue $Issue -Repo $Repo -DryRun:$DryRun -Yes:$Yes -NoRelay:$NoRelay -NewSession:$NewSession `
        -Implementer $Implementer -AutoMerge $autoMergeChoice -Failover:$Failover -Autonomous $autonomousChoice `
        -BigReview $bigReviewChoice -OnLimit $onLimitChoice -ImplementerModel $ImplementerModel -RosterId $RosterId
})) { exit $script:Launch.ExitCode }
if ($script:Launch.ClaudeHerePending -and -not $DryRun) {
    Invoke-ClaudeHere -Checkout $script:Launch.Checkout -Issue $script:Launch.IssueRef
    exit $LASTEXITCODE
}
exit 0
