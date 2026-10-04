<#
.SYNOPSIS
  The right pane with "implementer": "kimi" - Kimi Code as the implementer for this workbench (#65).

.DESCRIPTION
  The sibling of pane-codex.ps1 and pane-implementer-claude.ps1. Launch policy, and why:

  --yolo ("Ask When Needed") by default; --auto only with "kimiApproval": "never"
      Routine edits and commands run on their own; Kimi still stops for commands it rates dangerous,
      sensitive files and .git control paths. Our own guards cover only push, gh and the web, so
      --auto ("Never Ask") removes the only stop left. A pane on an approval prompt shows up as a
      dialog to the relay (mail is held) and in the stall watch - and waits for a human. A machine
      that runs its loops unattended, where the Claude agents already run with
      --dangerously-skip-permissions, can choose "kimiApproval": "never" instead.
  No --agent-file
      Kimi Code 2.1.1 ignores --agent-file and --agent in interactive mode: only -p binds the profile.
      The role is <checkout>\.kimi-code\AGENTS.md instead, which Kimi loads next to the repository's
      own AGENTS.md (lib/kimi.py prepare writes it and excludes it from git).
  UTF-8 console
      Kimi writes UTF-8; under the console's default code page its box, spinner and item glyphs reach
      the relay as mojibake, and nothing that reads the pane could recognise them.
  Guard rails in Kimi's shell (Git Bash)
      KIMI_SHELL_PATH pins the Git Bash, BASH_ENV puts the git/gh shims first on its PATH, and
      GIT_CONFIG_* pushInsteadOf plus an unusable GH_TOKEN stop the spellings a shim cannot catch.
      The web tools are off only through the human's own [tools] disabled, which the launcher checks.
      Guardrails, not a sandbox: the role says never push, never gh.
  -c on -Resume
      Kimi's last session for this directory, if there is one; the relay rings each mail only once,
      so a note from 'relay' tells the resumed Kimi to read what arrived while it was down.
  KIMI_CODE_NO_AUTO_UPDATE
      An auto-update re-executes kimi.exe as a child of the running one: an extra process under the
      pane that the failover's stop (a tree kill of the root kimi.exe) has to take down with it.

  Extra arguments from "kimiArgs" in ~/.agworkbench.json pass through, except anything that would
  re-decide the approval mode, the session, the agent or its directories - in any spelling kimi takes.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $Issue,
      [switch] $Resume,
      [switch] $WhatIfOnly)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
$config = Get-WorkbenchConfig

$kimi = Find-KimiExe $config
if (-not $kimi) {
    Write-Host "Cannot start the Kimi implementer: $(Get-KimiProblem $config)"
    exit 1
}

$argumentProblem = Get-KimiArgsProblem $config
if ($argumentProblem) { throw $argumentProblem }
$extra = @(@($config.kimiArgs) | Where-Object { $null -ne $_ })

if (-not $WhatIfOnly) {
    $problem = Get-KimiProblem $config
    if ($problem) {
        Write-Host "Cannot start the Kimi implementer: $problem"
        Write-Host "Repair, then: github-workbench $(Quote $Issue)"
        exit 1
    }
}

$prepareArgs = @((Join-Path $script:Lib 'kimi.py'), 'prepare', '--checkout', $Checkout, '--issue', $Issue)
if ($config.allowNetwork) { $prepareArgs += '--allow-network' }
if ($WhatIfOnly) { $prepareArgs += '--dry-run' }
$said = & python @prepareArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Cannot start the Kimi implementer: $(($said | ForEach-Object { "$_" }) -join ' ')"
    exit 1
}
$prepared = ($said -join "`n") | ConvertFrom-Json

$modeArgs = @()
$resumed = $false
if ($Resume) {
    $ErrorActionPreference = 'Continue'
    $listed = & $kimi session list --cwd $Checkout --json --limit 1 2>$null
    $ErrorActionPreference = 'Stop'
    try { $sessions = @(($listed -join "`n") | ConvertFrom-Json) } catch { $sessions = @() }
    if ($LASTEXITCODE -eq 0 -and $sessions.Count -and $sessions[0].id) {
        $modeArgs = @('-c')
        $resumed = $true
    }
}
$approval = '--yolo'
if ($config.kimiApproval -eq 'never') { $approval = '--auto' }
$kimiArgs = @($approval) + $modeArgs + $extra

$shimDir = [string]$prepared.shimDir
$environment = [ordered]@{ AGWORKBENCH = $script:Root; AI_HUB = (Join-Path $Checkout '.workbench'); AI_BOX = 'codex' }
foreach ($entry in $prepared.env.PSObject.Properties) { $environment[$entry.Name] = [string]$entry.Value }
$environment['PATH'] = $shimDir + [IO.Path]::PathSeparator + $env:PATH

if ($WhatIfOnly) {
    Write-Host "would cd: $Checkout"
    Write-Host 'would set: console output encoding UTF-8'
    foreach ($name in $environment.Keys) {
        $value = $environment[$name]
        if ($name -eq 'PATH') { $value = $shimDir + [IO.Path]::PathSeparator + '...' }
        Write-Host "would set: $name=$value"
    }
    Write-Host "would write: $($prepared.agentsMd); shims in $shimDir; Kimi trust record $($prepared.trustKey)"
    if ($resumed) { Write-Host "would mail box codex from relay: resumed after restart" }
    Write-Host ('would run: ' + (Quote $kimi) + ' ' + (($kimiArgs | ForEach-Object { Quote ([string]$_) }) -join ' '))
    return
}

foreach ($name in $environment.Keys) { Set-Item -LiteralPath "env:$name" -Value $environment[$name] }
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
Set-Location -LiteralPath $Checkout

$registry = Join-Path $env:AI_HUB 'state\agents.json'
$deadline = (Get-Date).AddSeconds(60)
while (-not (Test-Path -LiteralPath $registry) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }

if ($resumed) {
    # Kimi takes no first prompt in interactive mode, and the relay rings each mail once: this note is
    # what it rings, so the resumed Kimi reads whatever arrived while it was down.
    $note = @"
You were resumed after an agwinterm restart. You are still the IMPLEMENTER, in the RIGHT pane for $Issue.
Read your unread workbench mail (python "$script:Lib\agmsg.py" list), then continue the step you were in
(critiquing the plan, implementing, or fixing review findings) and reply as your role says.
"@
    $py = "import sys; sys.path.insert(0, sys.argv[1]); import hub; hub.reload_paths(); " +
          "hub.write_message(to='codex', sender='relay', kind='note', subject='resumed after restart', body=sys.stdin.read())"
    $note | & python -c $py $script:Lib | Out-Null
}

Write-Host "agworkbench: Kimi implementer for $Issue in $Checkout - $($prepared.trust); git push and gh refused in its shell" -ForegroundColor DarkGray
& $kimi @kimiArgs
