<#
.SYNOPSIS
  One revmux review round for this workbench, run in its own visible session, report posted to
  Claude's mailbox when it finishes.

.DESCRIPTION
  Claude writes the scope (it knows the plan and what changed) and launches this:

    agwintermctl session new --name "#N revmux r1" --cwd <checkout> --no-select `
      --command "pwsh -NoLogo -ExecutionPolicy Bypass -File <lib>\run-revmux.ps1 -Checkout <checkout> -ScopeFile <file> -Round 1"

  wb.py revmux does this through a launch file (.workbench\state\helpers\launch-revmux-r<K>.ps1, or
  launch-revmux-r<K>-<n>.ps1 for rerun attempt n) that calls this script with its parameters, so the
  session's command line stays inside agwinterm's limits however long they are (#86).

  The revmux TUI stays on screen in that session. stdout is the report and stderr is progress,
  so only stdout goes to the file - merging them makes the report unreadable. When revmux exits the
  report is posted to Claude and the relay rings its pane: Claude never waits on it.

  Exit 1 from revmux means findings were reported. It is a success.

  The run it used is recorded in .workbench/review/revmux-r<Round>.json ({run, dir, profile, attempt,
  scope}): wb.py review-round reads the run's events.jsonl from `dir` to tell a reviewer's usage limit
  from any other degraded source (#77). A rerun of a round (wb.py revmux --rerun) passes its own -Run
  (r<Round>-<n>: revmux refuses a round that has already run) and -Attempt, and -After M first waits
  M minutes on screen, redrawing its line every minute, for a reviewer's limit to reset.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $ScopeFile,
      [int] $Round = 1,
      [string] $Profile = 'comprehensive',
      [string] $Run,
      [int] $Attempt = 0,
      [int] $After = 0)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
Set-Location -LiteralPath $Checkout
$hubDir = Join-Path $Checkout '.workbench'
$reviewDir = Join-Path $hubDir 'review'
New-Item -ItemType Directory -Force -Path $reviewDir | Out-Null
$report = Join-Path $reviewDir "revmux-r$Round.md"

function Get-PostedId($lines) {
    # post.py prints `posted <id>.md -> <box>`; the report's id names the result mail in the marker (#84).
    foreach ($line in @($lines)) { if ("$line" -match '^posted (\S+)\.md -> ') { return $Matches[1] } }
    return $null
}

$run = if ($Run) { $Run } else { "r$Round" }
$code = $null
$mail = $null
$posted = $false
$why = $null
try {
if ($After -gt 0) {
    # Visible and stoppable (#77): the human sees what it waits for, and the relay's stall watch sees
    # the pane change every minute.
    $start = (Get-Date).AddMinutes($After)
    while ((Get-Date) -lt $start) {
        $left = [math]::Ceiling(($start - (Get-Date)).TotalMinutes)
        Write-Host ("waiting for the reviewer's usage limit: starts at {0:HH:mm}, {1} min left" -f $start, $left) -ForegroundColor Cyan
        Start-Sleep -Seconds ([math]::Min(60, [math]::Max(1, ($start - (Get-Date)).TotalSeconds)))
    }
}
$created = & revmux new --task workbench --run $run | Out-String
if ($LASTEXITCODE -ne 0) { throw "revmux new failed: $created" }
$paths = $created | ConvertFrom-Json
Copy-Item -LiteralPath $ScopeFile -Destination $paths.scope -Force
$record = [pscustomobject]@{ run = $run; dir = [string]$paths.round_dir; profile = $Profile; attempt = $Attempt;
                             scope = [string](Resolve-Path -LiteralPath $ScopeFile) }
Write-AtomicJson (Join-Path $reviewDir "revmux-r$Round.json") $record

Write-Host "revmux round $Round (run $run), profile $Profile -> $report" -ForegroundColor Cyan
& revmux --task workbench --run $run --profile $Profile --markdown | Out-File -FilePath $report -Encoding utf8
$code = $LASTEXITCODE
$verdict = switch ($code) { 0 { 'clean' } 1 { 'findings reported' } default { "tool error (exit $code)" } }

$out = & $script:Python (Join-Path $script:Lib 'post.py') --hub $hubDir --to claude --sender revmux --kind review `
    --subject "revmux round ${Round}: $verdict" --body-file $report
$postExit = $LASTEXITCODE
$out | Out-Host
$mail = Get-PostedId $out
if ($postExit -eq 0) {
    $posted = $true
    Write-Host "revmux exit $code ($verdict). Report posted to Claude." -ForegroundColor Yellow
} else {
    $why = "post.py exit $postExit"
}
} catch {
    Write-Host "revmux round $Round failed: $_" -ForegroundColor Red
} finally {
    # Never end silently (#45): the planner waits on this mail, not on a watcher of its own.
    if (-not $posted) {
        if ($null -eq $why) { $why = if ($null -ne $code) { "revmux exit $code" } else { 'it did not finish' } }
        $out = & $script:Python (Join-Path $script:Lib 'post.py') --hub $hubDir --to claude --sender helper --kind note `
            --subject "revmux round ${Round}: ended without a report ($why)" `
            --text "run-revmux.ps1 for round $Round ended without posting its report ($why). Look at the '#N revmux r$Round' session and $report."
        # Not a result (#84 r1): the note sends Claude to this session, so its marker names no mail
        # and the relay leaves the session open.
        $out | Out-Host
    }
    # The last act (#33): mark this helper done, with what its pane shows now, so an autonomous close
    # can prove nobody touched the pane since. A killed script writes no marker and stays open.
    $doneArgs = @((Join-Path $script:Lib 'helper_done.py'), '--hub', $hubDir, '--kind', 'revmux', '--round', "$Round")
    if ($null -ne $code) { $doneArgs += @('--exit', "$code") }
    # The mail that carries the result (#84): the relay closes this session once Claude has read it.
    # Only a posted report of a review that ran (exit 0 clean, 1 findings) is a result. A round that
    # failed, or whose review run was a tool error, keeps its session: its pane holds revmux's stderr,
    # the only record of the error. The tool-error mail is still posted; it is just not a result.
    if ($posted -and $mail -and ($code -eq 0 -or $code -eq 1)) { $doneArgs += @('--mail', $mail, '--to', 'claude') }
    & $script:Python @doneArgs
}
if (-not $posted) { exit 1 }
