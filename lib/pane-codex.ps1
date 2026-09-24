<#
.SYNOPSIS
  The right pane: Codex, sandboxed and unprompted, as the implementer for this workbench.

.DESCRIPTION
  Launch policy, and why each part is there:

  --sandbox workspace-write --ask-for-approval never
      Codex runs commands on its own; the OS sandbox is what bounds them. A pane sitting on an
      approval prompt is a pane doing nothing while the human reads the other one.
  --cd <checkout>
      The blast radius of workspace-write IS the workspace root: the issue's own clone, nothing else.
  -c sandbox_workspace_write.network_access=false and .writable_roots=[]
      Selecting workspace-write does not reset the nested settings that tune it, and user or project
      config is merged in automatically. Pinned here so the guarantee does not depend on a file.
      Set "allowNetwork": true in ~/.agworkbench.json to open the network (e.g. package installs).
  -c shell_environment_policy.set.*
      Codex strips the environment from tool subprocesses. AI_HUB and AI_BOX are copied in so its
      mail lands in this workbench's mailbox, under its own name. Values are TOML LITERAL strings
      (single quotes): a Windows path in a basic string is a run of invalid escapes.

  Codex never touches the terminal from here. Its sandbox denies the agwinterm control pipe; the
  relay reads its mail and rings Claude instead.

  Extra arguments from "codexArgs" in ~/.agworkbench.json pass through, except anything that would
  re-decide the sandbox, the approvals, or the workspace root - in any spelling codex accepts.
#>
param([Parameter(Mandatory = $true)] [string] $Checkout,
      [Parameter(Mandatory = $true)] [string] $Issue,
      [switch] $Resume,
      [switch] $WhatIfOnly)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Workbench.ps1')
$config = Get-WorkbenchConfig

function TomlLiteral([string] $Value) {
    if ($Value.Contains("'")) { throw "cannot pass a value containing a single quote as a TOML literal: $Value" }
    return "'" + $Value + "'"
}

$hubDir = Join-Path $Checkout '.workbench'
$network = 'false'
if ($config.allowNetwork) { $network = 'true' }

$policy = @(
    '--sandbox', 'workspace-write',
    '--ask-for-approval', 'never',
    '--cd', $Checkout,
    '-c', "sandbox_workspace_write.network_access=$network",
    '-c', 'sandbox_workspace_write.writable_roots=[]'
)
$inject = @(
    '-c', ('shell_environment_policy.set.AI_HUB=' + (TomlLiteral $hubDir)),
    '-c', "shell_environment_policy.set.AI_BOX='codex'",
    '-c', ('shell_environment_policy.set.AGWORKBENCH=' + (TomlLiteral $script:Root))
)

# The passthrough may not re-decide the policy (the lessons of seven bypasses, ported from the
# hub's start-codex.ps1): long forms case-insensitively, short forms case-sensitively so -c
# survives and -C does not, and policy keys inside any -c / --config spelling.
$forbiddenLong = '^--(sandbox|ask-for-approval|dangerously-bypass-approvals-and-sandbox|full-auto|approve-for-me|add-dir|cd|profile|yolo)(=|$)'
$forbiddenShort = '^-[saCp]'
$policyKey = '^(sandbox|sandbox_mode|sandbox_workspace_write|approval_policy|windows|trust_level|projects|shell_environment_policy)([._]|$|=)'
# @( ... ) around the WHOLE pipeline: a one-item result is unrolled to a bare string, and then
# $extra[0] is its first character - a single forbidden flag would sail through as '-'.
$extra = @(@($config.codexArgs) | Where-Object { $null -ne $_ })
for ($i = 0; $i -lt $extra.Count; $i++) {
    $text = [string]$extra[$i]
    if ($text -match $forbiddenLong -or $text -cmatch $forbiddenShort) { throw "codexArgs: '$text' would re-decide the sandbox policy" }
    $setting = $null
    if ($text -ceq '-c' -or $text -eq '--config') { if ($i + 1 -lt $extra.Count) { $setting = [string]$extra[$i + 1] } }
    elseif ($text -cmatch '^-c=?(.+)$') { $setting = $Matches[1] }
    elseif ($text -match '^--config=(.+)$') { $setting = $Matches[1] }
    if ($setting) { $setting = $setting.Trim().Trim([char]34, [char]39).Trim() }
    if ($setting -and $setting -match $policyKey) { throw "codexArgs: the setting '$setting' decides sandbox policy" }
}

$prompt = @"
You are CODEX, the IMPLEMENTER, in the RIGHT pane of an agworkbench session for GitHub issue $Issue.
Claude Code is in the left pane: it plans with you, reviews your work, and handles GitHub.
Use the workbench-implementer skill; it defines the loop, the mailbox commands and the rules.
Right now: wait. Claude will send you a draft plan through the workbench mailbox, and a line starting
"Chat from Workbench:" will appear here when it does. Until then do not edit anything. You have no
network access and no gh: the issue text will be in .workbench/issue.md once Claude has written it.
"@

$resumeArgs = @()
if ($Resume) {
    $sessionId = Find-CodexSession $Checkout
    if ($sessionId) {
        $resumeArgs = @('resume', $sessionId)
        $prompt = @"
You were resumed after an agwinterm restart. You are still CODEX, the IMPLEMENTER, in the RIGHT pane for $Issue.
Use the workbench-implementer skill. If you were implementing or fixing, continue that step and report
as usual. Otherwise run python "$script:Lib\agmsg.py" list to read unread workbench mail, then wait
for the next "Chat from Workbench:" line from the relay.
"@
    }
}

if ($WhatIfOnly) {
    Write-Host ("would run: codex " + (($inject + $resumeArgs + $policy + $extra + @($prompt)) -join ' '))
    return
}

$env:AGWORKBENCH = $script:Root
$env:AI_HUB = $hubDir
$env:AI_BOX = 'codex'
Set-Location -LiteralPath $Checkout
Write-Host "agworkbench: Codex for $Issue - sandbox workspace-write, network $network, root $Checkout" -ForegroundColor DarkGray
& codex @inject @resumeArgs @policy @extra $prompt
