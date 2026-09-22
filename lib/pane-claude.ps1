<#
.SYNOPSIS
  The left pane: Claude Code, started on /start-github-issue for this workbench.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $Issue)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
$config = Get-WorkbenchConfig

$env:AGWORKBENCH = $script:Root
$env:AI_HUB = Join-Path $Checkout '.workbench'
$env:AI_BOX = 'claude'
Set-Location -LiteralPath $Checkout

# The launcher registers both panes right after the split. Wait for that, so the first mail Claude
# sends already knows where Codex is.
$registry = Join-Path $env:AI_HUB 'state\agents.json'
$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Path -LiteralPath $registry) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }

Write-Host "agworkbench: Claude for $Issue in $Checkout" -ForegroundColor DarkGray
$claudeArgs = @(@($config.claudeArgs) | Where-Object { $_ })
& claude @claudeArgs "/start-github-issue $Issue"
