<#
.SYNOPSIS
  The human's review: revdiff over the branch, annotations posted to Claude when you quit.

.DESCRIPTION
  Claude opens this in its own session and selects it, so it is in front of you:

    agwintermctl session new --name "#N your review" --cwd <checkout> `
      --command "pwsh -NoLogo -ExecutionPolicy Bypass -File <lib>\human-review.ps1 -Checkout <checkout> -Base origin/main"

  revdiff is umputun's TUI for annotating a diff. Annotate what you want changed, press q, and the
  annotations are posted to Claude's mailbox; the relay rings Claude, which turns them into a review
  round for Codex. Quit without annotating and Claude is told you had nothing to add.

  It reviews the local branch - before or after the PR is raised, the diff is the same one.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $Base)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
Set-Location -LiteralPath $Checkout
$hubDir = Join-Path $Checkout '.workbench'
$reviewDir = Join-Path $hubDir 'review'
New-Item -ItemType Directory -Force -Path $reviewDir | Out-Null
$out = Join-Path $reviewDir ("human-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + ".md")

$posted = $false
$code = $null
try {
if (-not (Get-Command revdiff -ErrorAction SilentlyContinue)) {
    throw "revdiff is not installed - run install.ps1 from the agworkbench checkout"
}
Write-Host "Your review of $Base..HEAD. Annotate, then press q; the notes go to Claude." -ForegroundColor Cyan
& revdiff $Base --output $out
$code = $LASTEXITCODE
$annotated = (Test-Path -LiteralPath $out) -and ((Get-Content -Raw -LiteralPath $out).Trim().Length -gt 0)

if ($annotated) {
    & python (Join-Path $script:Lib 'post.py') --hub $hubDir --to claude --sender human --kind review `
        --subject "human review (revdiff): annotations to address" --body-file $out | Out-Host
} else {
    & python (Join-Path $script:Lib 'post.py') --hub $hubDir --to claude --sender human --kind note `
        --subject "human review (revdiff): no annotations" `
        --text "The human reviewed $Base..HEAD in revdiff and left no annotations." | Out-Host
}
$posted = $LASTEXITCODE -eq 0
Write-Host "Posted to Claude. You can close this session." -ForegroundColor Yellow
} catch {
    Write-Host "your review failed: $_" -ForegroundColor Red
} finally {
    # Never end silently (#45): the planner waits on this mail, not on a watcher of its own.
    if (-not $posted) {
        $why = if ($null -ne $code) { "exit $code" } else { 'it did not finish' }
        & python (Join-Path $script:Lib 'post.py') --hub $hubDir --to claude --sender helper --kind note `
            --subject "human review (revdiff): ended without a result ($why)" `
            --text "human-review.ps1 ended without posting the human's review ($why). Ask the human, or look at the '#N your review' session." | Out-Host
    }
    # The last act (#33): mark this helper done for the autonomous close (see run-revmux.ps1).
    & python (Join-Path $script:Lib 'helper_done.py') --hub $hubDir --kind review
}
if (-not $posted) { exit 1 }
