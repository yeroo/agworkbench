<#
.SYNOPSIS
  The left pane: Claude Code, started on /start-github-issue for this workbench.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $Issue,
      [switch] $WhatIfOnly)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
$config = Get-WorkbenchConfig

try { $identity = Read-ClaudeIdentity $Checkout $Issue }
catch {
    Write-Host "Cannot start Claude: $($_.Exception.Message)"
    Write-Host "Repair: github-workbench $(Quote $Issue)"
    exit 1
}
$claudeArgs = @(@($config.claudeArgs) | Where-Object { $_ })
foreach ($argument in $claudeArgs) {
    if ($argument -match '^--(session-id|resume|continue|fork-session)(=|$)' -or $argument -cmatch '^-[rc]') {
        throw "claudeArgs: '$argument' would override the conversation identity or launch mode"
    }
}
$transcript = Get-ClaudeTranscript $identity.sessionId
if ($transcript) { $modeArgs = @('--resume', $identity.sessionId) }
else { $modeArgs = @('--session-id', $identity.sessionId, "/start-github-issue $Issue") }
if ($WhatIfOnly) {
    Write-Host "would cd: $($identity.cwd)"
    Write-Host ('would run: claude ' + (($claudeArgs + $modeArgs | ForEach-Object { Quote ([string]$_) }) -join ' '))
    return
}

$env:AGWORKBENCH = $script:Root
$env:AI_HUB = Join-Path $Checkout '.workbench'
$env:AI_BOX = 'claude'
Set-Location -LiteralPath $identity.cwd

# The launcher registers both panes right after the split. Wait for that, so the first mail Claude
# sends already knows where Codex is.
$registry = Join-Path $env:AI_HUB 'state\agents.json'
$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Path -LiteralPath $registry) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }

Write-Host "agworkbench: Claude for $Issue in $Checkout" -ForegroundColor DarkGray
& claude @claudeArgs @modeArgs
