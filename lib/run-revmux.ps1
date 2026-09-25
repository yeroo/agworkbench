<#
.SYNOPSIS
  One revmux review round for this workbench, run in its own visible session, report posted to
  Claude's mailbox when it finishes.

.DESCRIPTION
  Claude writes the scope (it knows the plan and what changed) and launches this:

    agwintermctl session new --name "#N revmux r1" --cwd <checkout> --no-select `
      --command "pwsh -NoLogo -ExecutionPolicy Bypass -File <lib>\run-revmux.ps1 -Checkout <checkout> -ScopeFile <file> -Round 1"

  The revmux TUI stays on screen in that session. stdout is the report and stderr is progress,
  so only stdout goes to the file - merging them makes the report unreadable. When revmux exits the
  report is posted to Claude and the relay rings its pane: Claude never waits on it.

  Exit 1 from revmux means findings were reported. It is a success.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $ScopeFile,
      [int] $Round = 1,
      [string] $Profile = 'comprehensive')

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
Set-Location -LiteralPath $Checkout
$hubDir = Join-Path $Checkout '.workbench'
$reviewDir = Join-Path $hubDir 'review'
New-Item -ItemType Directory -Force -Path $reviewDir | Out-Null
$report = Join-Path $reviewDir "revmux-r$Round.md"

$run = "r$Round"
$code = $null
try {
$created = & revmux new --task workbench --run $run | Out-String
if ($LASTEXITCODE -ne 0) { throw "revmux new failed: $created" }
$paths = $created | ConvertFrom-Json
Copy-Item -LiteralPath $ScopeFile -Destination $paths.scope -Force

Write-Host "revmux round $Round, profile $Profile -> $report" -ForegroundColor Cyan
& revmux --task workbench --run $run --profile $Profile --markdown | Out-File -FilePath $report -Encoding utf8
$code = $LASTEXITCODE
$verdict = switch ($code) { 0 { 'clean' } 1 { 'findings reported' } default { "tool error (exit $code)" } }

& python (Join-Path $script:Lib 'post.py') --hub $hubDir --to claude --sender revmux --kind review `
    --subject "revmux round ${Round}: $verdict" --body-file $report | Out-Host
Write-Host "revmux exit $code ($verdict). Report posted to Claude." -ForegroundColor Yellow
} finally {
    # The last act (#33): mark this helper done, with what its pane shows now, so an autonomous close
    # can prove nobody touched the pane since. A killed script writes no marker and stays open.
    $doneArgs = @((Join-Path $script:Lib 'helper_done.py'), '--hub', $hubDir, '--kind', 'revmux', '--round', "$Round")
    if ($null -ne $code) { $doneArgs += @('--exit', "$code") }
    & python @doneArgs
}
