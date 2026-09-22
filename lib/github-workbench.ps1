<#
.SYNOPSIS
  github-workbench <issue> - open a two-pane Claude + Codex workbench for a GitHub issue.

.DESCRIPTION
  Claude Code on the left, Codex on the right, in one agwinterm session - the layout from
  umputun's agterm cookbook recipe `two-agent-chat`, on Windows. Claude starts by running
  /start-github-issue, which drives the loop: agree a plan with Codex, Codex implements, Claude
  reviews with revmux, Codex fixes, the human reviews with revdiff and merges the PR.

  Run it from PowerShell or cmd. Inside agwinterm or agliteterm it opens the session in that
  window. From any other terminal it starts agwinterm (installing it with scoop if needed) and opens
  the session there.

  Each issue gets its own full clone under ~/source/workbench, on branch issue-<n>-<slug>, and its
  own mailbox in .workbench/ inside that clone. Running it again for the same issue resumes.

.EXAMPLE
  github-workbench 42                     # issue 42 of the repo in the current directory
  github-workbench yeroo/agworkbench#7
  github-workbench https://github.com/yeroo/agworkbench/issues/7
  github-workbench 7 -DryRun              # print what would happen, touch nothing
  github-workbench -Version              # report the installed toolchain
#>
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Position = 0)] [string] $Issue,
    [string] $Repo,
    [switch] $DryRun,
    [switch] $Yes,
    [switch] $NoRelay,
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

if (-not $Issue) {
    Write-Host "usage: github-workbench <issue> [-Repo owner/name] [-DryRun] [-Yes]" -ForegroundColor Yellow
    Write-Host "       github-workbench -Version"
    Write-Host "  <issue> is 123, owner/repo#123, or https://github.com/owner/repo/issues/123"
    exit 2
}

function Quote([string] $Value) { return "'" + $Value.Replace("'", "''") + "'" }

function Get-PaneLaunch([string] $Script, [hashtable] $Arguments) {
    # A shell executable run explicitly with -ExecutionPolicy Bypass, so a machine whose policy is
    # Restricted still runs the pane script - `& 'x.ps1'` alone would be refused there.
    $shell = 'powershell.exe'
    if (Get-Command pwsh -ErrorAction SilentlyContinue) { $shell = 'pwsh' }
    $parts = @($shell, '-NoLogo', '-ExecutionPolicy', 'Bypass', '-File', (Quote (Join-Path $script:Lib $Script)))
    foreach ($key in $Arguments.Keys) { $parts += @("-$key", (Quote ([string]$Arguments[$key]))) }
    return ($parts -join ' ')
}

$config = Get-WorkbenchConfig
$ref = Resolve-IssueRef -Ref $Issue -RepoHint $Repo
$info = Get-IssueInfo $ref
$issueRef = "$($ref.Repo)#$($ref.Number)"
$repoName = ($ref.Repo -split '/')[1]
$slug = ConvertTo-Slug $info.title 24
Write-Host "workbench for $issueRef - $($info.title)" -ForegroundColor Cyan
if ($info.state -ne 'OPEN') { Write-Warning "issue is $($info.state)" }

# --- 1. the terminal --------------------------------------------------------------------------
if (Test-InsideAgwinterm) {
    Write-Step "inside agwinterm: opening the session in this window"
} elseif (Get-AgwintermCtl) {
    if ($DryRun) { Write-Step "would start agwinterm if it is not running" }
    elseif (-not (Test-AgwintermRunning)) { Start-AgwintermApp }
    else { Write-Step "agwinterm is running: opening the session there" }
} else {
    if ($DryRun) { Write-Step "would install agwinterm with scoop, then start it" }
    else { Install-Agwinterm -Yes:$Yes; Start-AgwintermApp }
}

# --- 2. the checkout --------------------------------------------------------------------------
if ($DryRun) {
    $dir = Join-Path $config.checkoutRoot "$repoName-issue-$($ref.Number)"
    $co = @{ Dir = $dir; Branch = "issue-$($ref.Number)-$(ConvertTo-Slug $info.title 32)" }
    Write-Step "would clone $($ref.Repo) into $($co.Dir) on branch $($co.Branch)"
} else {
    $co = New-IssueCheckout -Issue $ref -Title $info.title -Root $config.checkoutRoot
    Grant-CodexTrust -Dir $co.Dir
}
$hubDir = Join-Path $co.Dir '.workbench'

$claudeLaunch = Get-PaneLaunch 'pane-claude.ps1' @{ Checkout = $co.Dir; Issue = $issueRef }
$codexLaunch = Get-PaneLaunch 'pane-codex.ps1' @{ Checkout = $co.Dir; Issue = $issueRef }
if ($DryRun) {
    Write-Step "would open session '#$($ref.Number) $slug' in workspace '$repoName'"
    Write-Step "left pane:  $claudeLaunch"
    Write-Step "right pane: $codexLaunch"
    Write-Step "relay:      python lib\relay.py --hub $hubDir --repo $($ref.Repo) --branch $($co.Branch)"
    exit 0
}

# --- 3. the session: Claude on the left -------------------------------------------------------
$sessionId = Invoke-Ctl session new --name "#$($ref.Number) $slug" --cwd $co.Dir `
    --workspace-name $repoName --create-workspace --command $claudeLaunch
$sessionId = ($sessionId -split '\s+')[0]
Start-Sleep -Milliseconds 600
$session = Get-SessionById $sessionId
if (-not $session) { throw "session $sessionId did not appear in the tree" }
$left = (Get-PaneIds $session)[0]

# --- 4. the split: Codex on the right ---------------------------------------------------------
Invoke-Ctl session split on --target $sessionId | Out-Null
$right = $null
foreach ($attempt in 1..30) {
    Start-Sleep -Milliseconds 300
    $fresh = @(Get-PaneIds (Get-SessionById $sessionId)) | Where-Object { $_ -ne $left }
    if ($fresh) { $right = @($fresh)[0]; break }
}
if (-not $right) { throw "the split did not appear" }
$hubDir = Initialize-Mailbox -Checkout $co.Dir -ClaudePane $left -CodexPane $right
Write-Done "mailbox ready: $hubDir"

# Typed only into the pane this script just created, and only once it shows a shell prompt:
# the one case where typing a launch line is not a guess about what holds focus.
if (-not (Wait-ShellPrompt -Pane $right)) {
    Write-Warning "the right pane is not at a shell prompt; start Codex there yourself with:`n  $codexLaunch"
} else {
    Invoke-Ctl session type --select "$codexLaunch`n" --target $right | Out-Null
    Write-Done "Codex starting in the right pane"
}

# --- 5. the relay: its own small, visible session ---------------------------------------------
if (-not $NoRelay) {
    $relay = "python " + (Quote (Join-Path $script:Lib 'relay.py')) + " --hub " + (Quote $hubDir) +
        " --claude-pane $left --codex-pane $right --repo $($ref.Repo) --branch $($co.Branch)"
    Invoke-Ctl session new --name "#$($ref.Number) relay" --cwd $co.Dir --workspace-name $repoName `
        --no-select --command $relay | Out-Null
    Write-Done "relay watching the mailbox and the PR"
}

Invoke-Ctl session select $sessionId | Out-Null
Invoke-Ctl session focus left --target $sessionId | Out-Null
Write-Done "ready: Claude (left) is running /start-github-issue $issueRef"
