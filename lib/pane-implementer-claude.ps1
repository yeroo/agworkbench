<#
.SYNOPSIS
  The right pane with "implementer": "claude" - Claude Code as the implementer for this workbench.

.DESCRIPTION
  The sibling of pane-codex.ps1, for when Codex is out of quota. Launch policy, and why:

  Its own conversation record, state\implementer-claude.json
      The planner's record is state\claude.json; the two Claudes share this clone as their cwd, so
      each role keeps its own launcher-owned id (#5). A transcript for the id means resume.
  AI_BOX=codex
      The implementer's mailbox box is 'codex' whichever tool runs it; the registry's tool field
      tells the relay this pane takes Claude's keys.
  --disallowedTools, always
      Claude Code has no OS sandbox for its shell on Windows, and unlike Codex it inherits the
      network and an authenticated gh. The deny list keeps it off push, gh and (unless allowNetwork)
      the web tools; deny rules hold under --dangerously-skip-permissions too. It is a guardrail, not
      a boundary: a command can be spelled around a prefix rule, so the instructions still say never
      push. The list goes first, so the next option ends its variadic value list.

  Extra arguments from "claudeArgs" in ~/.agworkbench.json pass through, as for the planner - so
  the human's --dangerously-skip-permissions opt-in applies here too - except anything that would
  re-decide the conversation identity or widen this pane's tool policy.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $Issue,
      [switch] $WhatIfOnly)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
$config = Get-WorkbenchConfig

try { $identity = Read-ClaudeIdentity $Checkout $Issue 'implementer' }
catch {
    Write-Host "Cannot start the Claude implementer: $($_.Exception.Message)"
    Write-Host "Repair: github-workbench $(Quote $Issue)"
    exit 1
}
$claudeArgs = @(@($config.claudeArgs) | Where-Object { $_ })
foreach ($argument in $claudeArgs) {
    if ($argument -match '^--(session-id|resume|continue|fork-session)(=|$)' -or $argument -cmatch '^-[rc]') {
        throw "claudeArgs: '$argument' would override the conversation identity or launch mode"
    }
    if ($argument -match '^--(add-dir|permission-mode|allowedTools|allowed-tools|disallowedTools|disallowed-tools|settings)(=|$)') {
        throw "claudeArgs: '$argument' would re-decide the Claude implementer's tool policy"
    }
}

$denied = @('Bash(git push:*)', 'Bash(gh:*)')
if (-not $config.allowNetwork) { $denied += @('WebFetch', 'WebSearch') }
$policy = @('--disallowedTools') + $denied

$transcript = Get-ClaudeTranscript $identity.sessionId
if ($transcript) {
    $resumePrompt = @"
You were resumed after an agwinterm restart. You are still the IMPLEMENTER, in the RIGHT pane for $Issue.
Follow /workbench-implementer. Start one background wb.py wait-mail --box codex waiter, read any unread
workbench mail, and continue the step you were in (implementing, or fixing review findings).
"@
    $modeArgs = @('--resume', $identity.sessionId, $resumePrompt)
}
else { $modeArgs = @('--session-id', $identity.sessionId, "/workbench-implementer $Issue") }
if ($WhatIfOnly) {
    Write-Host "would cd: $($identity.cwd)"
    Write-Host ('would run: claude ' + (($policy + $claudeArgs + $modeArgs | ForEach-Object { Quote ([string]$_) }) -join ' '))
    return
}

$env:AGWORKBENCH = $script:Root
$env:AI_HUB = Join-Path $Checkout '.workbench'
$env:AI_BOX = 'codex'
Set-Location -LiteralPath $identity.cwd

$registry = Join-Path $env:AI_HUB 'state\agents.json'
$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Path -LiteralPath $registry) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }

Write-Host "agworkbench: Claude implementer for $Issue in $Checkout - denied: $($denied -join ', ')" -ForegroundColor DarkGray
& claude @policy @claudeArgs @modeArgs
