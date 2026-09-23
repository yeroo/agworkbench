# Shared helpers for github-workbench. Dot-sourced; defines functions only.
# Written for Windows PowerShell 5.1 as well as PowerShell 7: no ternaries, no ?? operators.

$script:Lib = $PSScriptRoot
$script:Root = Split-Path -Parent $PSScriptRoot

class AdoptRefused : System.Exception {
    AdoptRefused([string] $Message) : base($Message) {}
}

function Write-Step([string] $Text) { Write-LaunchLog step $Text; Write-Host "  $Text" -ForegroundColor DarkGray }
function Write-Done([string] $Text) { Write-LaunchLog done $Text; Write-Host "  $Text" -ForegroundColor Green }

# Logging is opt-in: loading helpers, -Version and -DryRun never create a log.
function Enable-LaunchLog {
    $script:LaunchLog = @{ Enabled = $true; Path = $null; Warned = $false;
        Pending = (New-Object 'System.Collections.Generic.List[string]') }
}

function Disable-LaunchLog { $script:LaunchLog = $null }

function Flush-LaunchLog {
    if (-not $script:LaunchLog -or -not $script:LaunchLog.Path -or $script:LaunchLog.Warned) { return }
    try {
        $path = $script:LaunchLog.Path
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) -ErrorAction Stop | Out-Null
        if ($script:LaunchLog.Pending.Count) {
            Add-Content -LiteralPath $path -Value $script:LaunchLog.Pending.ToArray() -Encoding UTF8 -ErrorAction Stop
            $script:LaunchLog.Pending.Clear()
        }
    } catch {
        $script:LaunchLog.Warned = $true
        Write-Warning "Cannot write launch log '$path': $($_.Exception.Message). Keeping the log in memory." -WarningAction Continue
    }
}

function Write-LaunchLog([string] $Step, [string] $Text) {
    if (-not $script:LaunchLog -or -not $script:LaunchLog.Enabled) { return }
    foreach ($line in ($Text -split '\r\n|\r|\n')) {
        $script:LaunchLog.Pending.Add("$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') $Step $line")
    }
    Flush-LaunchLog
}

function Connect-LaunchLog([string] $Path) {
    if (-not $script:LaunchLog -or -not $script:LaunchLog.Enabled) { return }
    $script:LaunchLog.Path = $Path
    Flush-LaunchLog
}

function Set-LaunchStage([string] $Stage) {
    $script:Launch.Stage = $Stage
    Write-LaunchLog $Stage 'starting'
}

function Invoke-LaunchSafely([scriptblock] $Body) {
    # Both the entry point and the terminal-free flow tests use this failure boundary.
    try { & $Body | Out-Null; return $true } catch {
        $failure = $_
        $script:Launch.ClaudeHerePending = $false
        if ($failure.Exception -is [AdoptRefused]) {
            $script:Launch.ExitCode = 2
            Write-Host "Adoption refused: $($failure.Exception.Message)" -ForegroundColor Yellow
            return $false
        }
        $script:Launch.ExitCode = 1
        # Logging must not turn an empty failed-clone target into a non-empty one.
        if ($script:LaunchLog -and -not $script:LaunchLog.Path -and $script:Launch.Checkout -and
            (Test-Path -LiteralPath (Join-Path $script:Launch.Checkout '.git'))) {
            Connect-LaunchLog (Join-Path $script:Launch.Checkout '.workbench\state\launch.log')
        }
        $dump = @($failure.Exception.ToString(), $failure.InvocationInfo.PositionMessage,
            $failure.ScriptStackTrace) -join "`n"
        Write-LaunchLog error $dump
        Write-Host "Launcher failed: $dump" -ForegroundColor Red
        Write-Host (Format-RepairMessage $script:Launch)
        if ($script:LaunchLog -and $script:LaunchLog.Pending.Count) {
            Write-Host ($script:LaunchLog.Pending -join "`n")
        }
        return $false
    }
}

# --- configuration ---------------------------------------------------------------------------

function Get-WorkbenchConfig {
    <# ~/.agworkbench.json, all keys optional:
         claudeArgs     extra arguments for claude, e.g. ["--dangerously-skip-permissions"]
         codexArgs      extra arguments for codex (policy flags are refused - see pane-codex.ps1)
         checkoutRoot   where per-issue clones go (default ~/source/workbench)
         allowNetwork   let Codex's sandbox reach the network (default false) #>
    $path = Join-Path $HOME '.agworkbench.json'
    if ($env:AGWORKBENCH_CONFIG) { $path = $env:AGWORKBENCH_CONFIG }   # tests point this elsewhere
    $config = @{ claudeArgs = @(); codexArgs = @(); checkoutRoot = (Join-Path $HOME 'source\workbench'); allowNetwork = $false }
    if (Test-Path -LiteralPath $path) {
        $loaded = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
        foreach ($key in @('claudeArgs', 'codexArgs', 'checkoutRoot', 'allowNetwork')) {
            if ($null -ne $loaded.$key) { $config[$key] = $loaded.$key }
        }
    }
    return $config
}

# --- the terminal ----------------------------------------------------------------------------

function Get-AgwintermCtl {
    $candidates = @()
    if ($env:AGWINTERMCTL) { $candidates += $env:AGWINTERMCTL }
    $onPath = Get-Command agwintermctl -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\agwinterm\agwintermctl.exe')
    $candidates += (Join-Path $HOME 'scoop\apps\agwinterm\current\agwintermctl.exe')
    foreach ($candidate in $candidates) { if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate } }
    return $null
}

# --- installed versions ----------------------------------------------------------------------

function Find-Tool([string] $Name) {
    # npm installs adjacent .ps1/.cmd shims, and PATH can contain several installs.
    # Prefer one executable in PATH order; use a PowerShell-only install as a fallback.
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) {
        $command = Get-Command $Name -CommandType ExternalScript -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($command) { return $command.Source }
    return $null
}

function Get-VersionToken([string] $Line) {
    if ($Line -match '\d+\.\d+(\.\d+)?') { return $Matches[0] }
    return $null
}

function Get-GoModuleVersion([string] $Exe) {
    # go install stamps module metadata even when the CLI itself says "unknown".
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        $go = Find-Tool go
        if (-not $go) { return $null }
        $global:LASTEXITCODE = 0
        $output = & $go version -m $Exe 2>&1
        if ($LASTEXITCODE -ne 0) { return $null }
        foreach ($line in $output) {
            if ("$line" -match '^\s*mod\s+\S+\s+(\S+)') { return $Matches[1] }
        }
    } catch { return $null }
    return $null
}

function Get-ToolVersion([string] $Exe, [string[]] $Arguments) {
    # 5.1 wraps native stderr in ErrorRecords: capture it without aborting the report.
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    try {
        # Scripts without an explicit exit/native call leave this value untouched.
        # Reset the global value: a local variable would hide native exit-code updates.
        $global:LASTEXITCODE = 0
        $output = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
        $line = $output | ForEach-Object { "$_" -split '\r?\n' } |
            Where-Object { $_.Trim() } | Select-Object -First 1
        if ($code -ne 0) {
            if ($line) { return "error (exit ${code}): $line" }
            return "error (exit $code)"
        }
        if (-not $line) { return 'error: no output' }
        $token = Get-VersionToken $line
        if ($token) { return $token }
        $module = Get-GoModuleVersion $Exe
        if ($module) { return $module }
        return $line
    } catch {
        return "error: $(($_.Exception.Message -split '\r?\n')[0])"
    }
}

function Get-ToolchainVersions {
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    $version = 'unversioned'
    try {
        $git = Find-Tool git
        if ($git) {
            $global:LASTEXITCODE = 0
            $description = & $git -C $script:Root describe --always --dirty 2>$null
            if ($LASTEXITCODE -eq 0 -and $description) { $version = @($description)[0] }
        }
    } catch { $version = 'unversioned' }
    [pscustomobject] @{ Name = 'agworkbench'; Version = "$version ($script:Root)" }
    foreach ($name in @('agwinterm', 'claude', 'codex', 'revmux', 'revdiff', 'gh')) {
        try {
            $arguments = @('--version')
            if ($name -eq 'agwinterm') {
                $exe = Get-AgwintermCtl
                $arguments = @('version')
            } else { $exe = Find-Tool $name }
            $version = 'missing'
            if ($exe) { $version = Get-ToolVersion $exe $arguments }
        } catch {
            $version = "error: $(($_.Exception.Message -split '\r?\n')[0])"
        }
        [pscustomobject] @{ Name = $name; Version = $version }
    }
}

function Get-AgwintermApp {
    # The installer ships Agwinterm.Win32.exe; the scoop manifest exposes agwinterm.exe.
    foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\agwinterm\Agwinterm.Win32.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\agwinterm\agwinterm.exe'),
            (Join-Path $HOME 'scoop\apps\agwinterm\current\agwinterm.exe'),
            (Join-Path $HOME 'scoop\apps\agwinterm\current\Agwinterm.Win32.exe'))) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $shim = Get-Command agwinterm -ErrorAction SilentlyContinue
    if ($shim) { return $shim.Source }
    return $null
}

function Invoke-Ctl {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Arguments)
    Write-LaunchLog ctl ($Arguments -join ' ')
    $ctl = Get-AgwintermCtl
    if (-not $ctl) { throw "agwintermctl not found" }
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    # agwintermctl is .NET and writes stdout in the console code page. Under `pwsh -NoProfile` -
    # which is how github-workbench.cmd starts this - that is ibm437, and Codex's prompt glyph
    # U+276F arrives as three wrong characters, so a pane sitting on a prompt never looked like one.
    # UTF-8 for the call, then put back whatever the caller had.
    $previous = $null
    try { $previous = [Console]::OutputEncoding; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { $previous = $null }
    try {
        $global:LASTEXITCODE = 0
        $output = & $ctl @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        if ($null -ne $previous) { try { [Console]::OutputEncoding = $previous } catch { } }
    }
    $text = (($output | ForEach-Object { "$_" }) -join "`n").Trim()
    Write-LaunchLog ctl "exit ${code}: $(($text -split '\r?\n')[0])"
    if ($code -ne 0) { throw "agwintermctl $($Arguments -join ' ') (exit ${code}): $text" }
    return $text
}

function Test-AgwintermRunning {
    try { Invoke-Ctl ping | Out-Null; return $true } catch { return $false }
}

function Test-InsideAgwinterm {
    # agwinterm and agliteterm both set these, and share the control API.
    return ($env:AGWINTERM_ENABLED -eq '1' -and [bool]$env:AGWINTERM_SESSION_ID)
}

function Install-Agwinterm {
    param([switch] $Yes)
    Write-Host "agwinterm is not installed." -ForegroundColor Yellow
    if (-not $Yes) {
        $answer = Read-Host "Install it with scoop from github.com/yeroo/scoop-bucket? [y/N]"
        if ($answer -notmatch '^(y|yes)$') { throw "agwinterm is required; not installed." }
    }
    if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
        if (-not $Yes) {
            $answer = Read-Host "scoop is not installed either. Install scoop (https://scoop.sh) for this user? [y/N]"
            if ($answer -notmatch '^(y|yes)$') { throw "scoop is required to install agwinterm." }
        }
        Write-Step "installing scoop"
        Invoke-RestMethod -Uri 'https://get.scoop.sh' | Invoke-Expression
    }
    $buckets = (scoop bucket list 6>$null | Out-String)
    if ($buckets -notmatch 'yeroo/scoop-bucket') {
        Write-Step "adding the agwinterm bucket"
        scoop bucket add agwinterm https://github.com/yeroo/scoop-bucket | Out-Host
    }
    Write-Step "installing agwinterm"
    scoop install agwinterm/agwinterm | Out-Host
    if (-not (Get-AgwintermCtl)) { throw "scoop finished but agwintermctl is still not found" }
    Install-AgwintermIntegrations
}

function Install-AgwintermIntegrations {
    # The terminal's own agent skill and status hooks: sidebar dots, and agents that know the API.
    $ctl = Get-AgwintermCtl
    if (-not $ctl) { return }
    foreach ($what in @('skill', 'hooks')) {
        try { Invoke-Ctl install $what | Out-Null; Write-Done "agwinterm: installed $what" }
        catch { Write-Warning "agwinterm install $what failed: $_" }
    }
}

function Start-AgwintermApp {
    param([int] $TimeoutSeconds = 30)
    $app = Get-AgwintermApp
    if (-not $app) { throw "agwinterm is installed but its executable was not found" }
    Write-Step "starting agwinterm"
    Start-Process -FilePath $app | Out-Null
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-AgwintermRunning) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "agwinterm did not answer on its control pipe within $TimeoutSeconds s"
}

function Get-Tree {
    $reply = (Invoke-Ctl tree --json) | ConvertFrom-Json
    if ($reply.ok -eq $false -or $null -eq $reply.result -or
        $null -eq $reply.result.PSObject.Properties['workspaces']) { throw 'agwintermctl returned an invalid session tree' }
    return $reply.result
}

function Get-SessionById([string] $Id) {
    foreach ($ws in (Get-Tree).workspaces) { foreach ($s in $ws.sessions) { if ($s.id -eq $Id) { return $s } } }
    return $null
}

function Get-PaneIds($Session) {
    # Emit individual strings. Consumers use @() to preserve a single pane as an array.
    $hasIds = $null -ne $Session -and $null -ne $Session.PSObject.Properties['paneIds']
    if ($Session -is [System.Collections.IDictionary]) { $hasIds = $Session.Contains('paneIds') }
    $ids = @($Session.id)
    if ($hasIds) { $ids = @($Session.paneIds) }
    if ($ids.Count -lt 1 -or $ids.Count -gt 2) { throw "session '$($Session.id)' has unsupported panes: $($ids -join ', ')" }
    foreach ($id in $ids) {
        $guid = [guid]::Empty
        if ($id -isnot [string] -or -not [guid]::TryParse($id, [ref]$guid) -or $guid -eq [guid]::Empty) {
            throw "session '$($Session.id)' has invalid pane id '$id'"
        }
    }
    if (@($ids | Select-Object -Unique).Count -ne $ids.Count) { throw "session '$($Session.id)' has duplicate pane ids" }
    return $ids
}

function Test-ShellReady([string] $Text) {
    $rows = @($Text -split '\r?\n' | Where-Object { $_.Trim() })
    if (-not $rows.Count) { return $false }
    $frame = ($rows | Select-Object -Last 15) -join "`n"
    if ($frame -match 'esc to interrupt|bypass permissions|for shortcuts|Ask Codex|Chat from Workbench|Working|\[y/N\]|Do you trust|\(y/n\)') { return $false }
    # Rules around a composer are evidence of an agent even during a footer redraw.
    if ($frame -match ('(?m)^\s*[-' + [char]0x2500 + [char]0x2501 + [char]0x2014 + ']{10,}\s*$')) { return $false }
    $last = $rows[-1]
    if ($last -match '^PS [A-Za-z]:\\[^>]*> ?$') { return $true }
    if ($last -match ('^\s*' + [char]0x276F + '\s*$') -and $rows.Count -ge 2) {
        return ($rows[-2] -match '(\d+(\.\d+)?(ms|s)|\d\d:\d\d(:\d\d)?)\s*$')
    }
    return $false
}

function Wait-ShellPrompt {
    <# Newly created panes use the prompt-glyph rule. Adopted panes require Test-ShellReady's
       recognized empty shell frame; a lone glyph is refused. Empty text never permits typing. #>
    param([string] $Pane, [int] $TimeoutSeconds = 20, [switch] $Adopted)
    $started = Get-Date
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $prompt = '(>|' + [char]0x276F + '|\$|#)\s*$'
    $tail = ''
    while ((Get-Date) -lt $deadline) {
        $text = Invoke-Ctl session text --target $Pane
        $tail = ($text -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($Adopted) { $ready = Test-ShellReady $text }
        else { $ready = $tail -and $tail -match $prompt }
        if ($ready) {
            Write-LaunchLog prompt-decision "$Pane proven last-row=$tail waited=$([math]::Round(((Get-Date) - $started).TotalSeconds, 2))s"
            return $true
        }
        Start-Sleep -Milliseconds 300
    }
    Write-LaunchLog prompt-decision "$Pane not-proven timeout last-row=$tail waited=${TimeoutSeconds}s"
    return $false
}

# --- the issue -------------------------------------------------------------------------------

function Find-SessionByPane($Tree, [string] $Pane) {
    if (-not $Pane) { return $null }
    foreach ($workspace in $Tree.workspaces) {
        foreach ($session in $workspace.sessions) {
            if ($session.paneIds -contains $Pane -or (-not $session.paneIds -and $session.id -eq $Pane)) {
                return @{ Workspace = $workspace; Session = $session }
            }
        }
    }
    return $null
}

function Test-IssueSessionName([string] $Name, [int] $Number) {
    return ($Name.StartsWith("#$Number ") -and $Name -notmatch "^#$Number (relay|revmux r\d+|your review)$")
}

function Find-IssueSession($Tree, [string] $RepoName, [int] $Number, [string] $Slug, $Registry) {
    $registered = Find-SessionByPane $Tree $Registry.agents.claude.pane
    if ($registered -and $registered.Workspace.name -eq $RepoName -and
        (Test-IssueSessionName $registered.Session.name $Number)) { return $registered.Session }
    if ($Registry.agents.claude.pane) { Write-LaunchLog resume 'stale or mismatching registry pane ignored' }
    $candidates = @(foreach ($workspace in $Tree.workspaces) {
        if ($workspace.name -eq $RepoName) {
            foreach ($session in $workspace.sessions) {
                if (Test-IssueSessionName $session.name $Number) { $session }
            }
        }
    })
    $exact = @($candidates | Where-Object { $_.name -eq "#$Number $Slug" })
    if ($exact.Count) { $candidates = $exact }
    if ($candidates.Count -gt 1) { throw "ambiguous #$Number sessions in '$RepoName': $($candidates.id -join ', ')" }
    if ($candidates.Count) { return $candidates[0] }
    return $null
}

function Find-RelaySession($Tree, [string] $RepoName, [int] $Number) {
    $candidates = @(foreach ($workspace in $Tree.workspaces) {
        if ($workspace.name -eq $RepoName) {
            $workspace.sessions | Where-Object { $_.name -eq "#$Number relay" }
        }
    })
    if ($candidates.Count -gt 1) { throw "ambiguous #$Number relay sessions in '$RepoName': $($candidates.id -join ', ')" }
    if ($candidates.Count) { return $candidates[0] }
    return $null
}

function Get-PanePlan($Session, $Registry) {
    $panes = @(Get-PaneIds $Session)
    $claude = $null
    $codex = $null
    if ($Registry.agents.claude.pane -in $panes) { $claude = $Registry.agents.claude.pane }
    elseif ($Registry.agents.codex.pane -in $panes) { $codex = $Registry.agents.codex.pane }
    else { $claude = $panes[0] }
    if ($panes.Count -eq 2) {
        if ($claude) { $codex = @($panes | Where-Object { $_ -ne $claude })[0] }
        else { $claude = @($panes | Where-Object { $_ -ne $codex })[0] }
    }
    $newRole = 'Codex'
    if (-not $claude) { $newRole = 'Claude' }
    $slot = 'primary'
    if ($claude -ne $panes[0]) { $slot = 'split' }
    return @{ Claude = $claude; Codex = $codex; NeedSplit = $panes.Count -eq 1; ClaudeSlot = $slot; NewPaneRole = $newRole }
}

function Test-ClaudeCaller { return $env:CLAUDECODE -eq '1' }

function Normalize-WorkbenchPath([string] $Path) {
    if (-not $Path) { throw 'empty path' }
    return [IO.Path]::GetFullPath($Path.Replace('/', '\')).TrimEnd('\').ToLowerInvariant()
}

function Test-SessionGuid([string] $Value) {
    $id = [guid]::Empty
    return [guid]::TryParse($Value, [ref]$id) -and $id -ne [guid]::Empty
}

function Get-ClaudeTranscript([string] $SessionId) {
    if (-not (Test-SessionGuid $SessionId)) { throw "invalid Claude conversation id '$SessionId'" }
    $root = Join-Path $HOME '.claude'
    if ($env:CLAUDE_CONFIG_DIR) { $root = $env:CLAUDE_CONFIG_DIR }
    $projects = Join-Path $root 'projects'
    if (-not (Test-Path -LiteralPath $projects)) { return $null }
    foreach ($dir in @(Get-ChildItem -LiteralPath $projects -Directory | Sort-Object Name)) {
        $path = Join-Path $dir.FullName "$SessionId.jsonl"
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($line in [IO.File]::ReadLines($path)) {
            try { $entry = $line | ConvertFrom-Json } catch { continue }
            if ($entry.cwd -and [IO.Path]::IsPathRooted([string]$entry.cwd)) {
                return @{ Path = $path; Cwd = [string]$entry.cwd }
            }
        }
        return @{ Path = $path; Cwd = $null }
    }
    return $null
}

function Get-AdoptedClaudeIdentity {
    try {
        $id = $env:CLAUDE_CODE_SESSION_ID
        if (-not $id) { throw 'CLAUDE_CODE_SESSION_ID is missing' }
        $transcript = Get-ClaudeTranscript $id
        if (-not $transcript -or -not $transcript.Cwd) { throw "Claude transcript or cwd missing for '$id'" }
        return @{ sessionId = $id; cwd = $transcript.Cwd }
    } catch { throw [AdoptRefused]::new($_.Exception.Message) }
}

function Read-ClaudeIdentity([string] $Checkout, [string] $Issue) {
    $path = Join-Path $Checkout '.workbench\state\claude.json'
    $record = Get-Content -Raw -LiteralPath $path -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
    if (-not (Test-SessionGuid $record.sessionId) -or
        ($null -ne $record.pane -and -not (Test-SessionGuid $record.pane)) -or
        -not $record.cwd -or -not [IO.Path]::IsPathRooted([string]$record.cwd) -or
        $record.origin -notin @('fresh', 'adopted') -or $record.issue -ne $Issue -or
        (Normalize-WorkbenchPath $record.checkout) -ne (Normalize-WorkbenchPath $Checkout)) {
        throw "invalid Claude identity in '$path'"
    }
    if (-not $record.PSObject.Properties['pane']) { throw "missing pane binding in '$path'" }
    return $record
}

function Save-ClaudeIdentity([string] $Checkout, $Record) {
    $path = Join-Path $Checkout '.workbench\state\claude.json'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    $Record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$path.tmp" -Encoding UTF8
    Move-Item -LiteralPath "$path.tmp" -Destination $path -Force
}

function Reserve-ClaudeIdentity([string] $Checkout, [string] $Issue, [string] $Pane, $CallerIdentity) {
    # The caller has established pane ownership through #3/#4 discovery. Registry creation
    # comes later; it must not invalidate a reservation after a split/mailbox failure.
    $path = Join-Path $Checkout '.workbench\state\claude.json'
    $record = $null
    $changed = $false
    if (Test-Path -LiteralPath $path) {
        try { $record = Read-ClaudeIdentity $Checkout $Issue } catch { Write-LaunchLog identity "$_" }
        if (-not $record -or ($record.pane -and $record.pane -ne $Pane)) {
            $archive = Join-Path (Split-Path -Parent $path) ("claude.$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')).json")
            Write-LaunchLog identity "archiving Claude conversation '$($record.sessionId)' from pane '$($record.pane)' to '$archive'"
            Move-Item -LiteralPath $path -Destination $archive
            $record = $null
        }
    }
    if (-not $record) {
        $record = [pscustomobject]@{ pane = $null; sessionId = [guid]::NewGuid().ToString(); cwd = $Checkout;
            origin = 'fresh'; reservedAt = [DateTime]::UtcNow.ToString('o'); issue = $Issue; checkout = $Checkout }
        $changed = $true
    }
    if ($CallerIdentity -and ($record.sessionId -ne $CallerIdentity.sessionId -or
        $record.cwd -ne $CallerIdentity.cwd -or $record.origin -ne 'adopted')) {
        $record.sessionId = $CallerIdentity.sessionId
        $record.cwd = $CallerIdentity.cwd
        $record.origin = 'adopted'
        $changed = $true
    }
    if ($Pane -and $record.pane -ne $Pane) { $record.pane = $Pane; $changed = $true }
    if ($changed) { Save-ClaudeIdentity $Checkout $record }
    return $record
}

function Find-CodexSession([string] $Checkout) {
    $root = Join-Path $HOME '.codex'
    if ($env:CODEX_HOME) { $root = $env:CODEX_HOME }
    $wanted = Normalize-WorkbenchPath $Checkout
    $matches = @()
    foreach ($file in @(Get-ChildItem -Path (Join-Path $root 'sessions\*\*\*\rollout-*.jsonl') -File -ErrorAction SilentlyContinue)) {
        try {
            $entry = Get-Content -LiteralPath $file.FullName -TotalCount 1 -Encoding UTF8 | ConvertFrom-Json
            $meta = $entry.payload
            $stamp = [DateTimeOffset]::MinValue
            # PowerShell 7 deserializes ISO timestamps as DateTime; stringifying that
            # value loses its fractional seconds and timezone. PS5.1 leaves a string.
            $validStamp = $false
            if ($meta.timestamp -is [DateTime]) { $stamp = [DateTimeOffset]$meta.timestamp; $validStamp = $true }
            else { $validStamp = [DateTimeOffset]::TryParse([string]$meta.timestamp, [ref]$stamp) }
            if ($entry.type -ne 'session_meta' -or -not (Test-SessionGuid $meta.id) -or
                $meta.originator -ne 'codex-tui' -or (Normalize-WorkbenchPath $meta.cwd) -ne $wanted -or
                -not $validStamp) { continue }
            $matches += [pscustomobject]@{ Id = $meta.id; Timestamp = $stamp; Name = $file.FullName }
        } catch { continue }
    }
    $newest = $matches | Sort-Object Timestamp, Name -Descending | Select-Object -First 1
    if ($newest) { return $newest.Id }
    return $null
}

function Enable-RestoreReplay {
    Set-LaunchStage restore-config
    $script:Launch.RestoreRepair = 'agwintermctl config set restore-commands true'
    $reply = (Invoke-Ctl config get restore-commands --json) | ConvertFrom-Json
    if ($reply.ok -isnot [bool] -or -not $reply.ok -or $reply.result -isnot [string] -or $reply.result -notin @('true', 'false')) {
        throw 'cannot read restore-commands as true or false'
    }
    if ($reply.result -eq 'false') {
        Invoke-Ctl config set restore-commands true | Out-Null
        $reply = (Invoke-Ctl config get restore-commands --json) | ConvertFrom-Json
        if ($reply.ok -isnot [bool] -or -not $reply.ok -or $reply.result -isnot [string] -or $reply.result -ne 'true') {
            throw 'restore-commands was not enabled'
        }
        Write-Step 'enabled agwinterm restore-commands'
    }
    $script:Launch.RestoreConfirmed = $true
    $script:Launch.Remove('RestoreRepair')
}

function Set-PaneRestore([string] $Pane, [string] $Command) {
    if (-not (Test-SessionGuid $Pane)) { throw "invalid restore pane '$Pane'" }
    Set-LaunchStage restore-pin
    $script:Launch.RestoreRepair = "agwintermctl session restore $(Quote $Command) --target $(Quote $Pane)"
    $reply = (Invoke-Ctl session restore $Command --target $Pane) | ConvertFrom-Json
    if ($reply.action -ne 'pinned' -or $reply.pane -ne $Pane -or $reply.command -ne $Command) {
        throw "restore pin was not confirmed for '$Pane'"
    }
    $script:Launch.Remove('RestoreRepair')
}

function Get-CallerSession($Tree) {
    $pane = $env:AGWINTERM_PANE_ID
    if (-not $pane) { $pane = $env:AGWINTERM_SESSION_ID }
    $found = Find-SessionByPane $Tree $pane
    if (-not $found) { throw [AdoptRefused]::new("caller pane '$pane' is not in the terminal tree") }
    $found.Pane = $pane
    return $found
}

function Get-AdoptionPlan($Tree, [string] $Checkout, [string] $RepoName, [int] $Number) {
    if (-not (Test-ClaudeCaller) -and ($env:CODEX_THREAD_ID -or $env:CODEX_SANDBOX)) {
        throw [AdoptRefused]::new('a Codex process cannot adopt its pane as Claude; use -NewSession')
    }
    $caller = Get-CallerSession $Tree
    $session = $caller.Session
    $panes = @(Get-PaneIds $session)
    $workspaces = @($Tree.workspaces | Where-Object { $_.name -eq $RepoName })
    if ($workspaces.Count -gt 1) {
        throw [AdoptRefused]::new("multiple workspaces named '$RepoName': $($workspaces.id -join ', ')")
    }
    $workspaceId = $null
    if ($workspaces.Count) {
        $guid = [guid]::Empty
        if (-not [guid]::TryParse([string]$workspaces[0].id, [ref]$guid) -or $guid -eq [guid]::Empty) {
            throw [AdoptRefused]::new("workspace '$RepoName' has no valid id")
        }
        $workspaceId = $workspaces[0].id
    }
    $registry = $null
    $adoption = $null
    foreach ($file in @('agents', 'adoption')) {
        $path = Join-Path $Checkout ".workbench\state\$file.json"
        if (Test-Path -LiteralPath $path) {
            try { $data = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json }
            catch { throw [AdoptRefused]::new("cannot validate '$path': $_") }
            if ($file -eq 'agents') { $registry = $data } else { $adoption = $data }
        }
    }
    if ($registry.agents.codex.pane -eq $caller.Pane) {
        throw [AdoptRefused]::new('the caller is registered as Codex, not Claude')
    }
    if ($registry.agents.claude.pane -and -not (Find-SessionByPane $Tree $registry.agents.claude.pane)) {
        Write-LaunchLog adoption 'stale registry Claude pane ignored'
    }
    foreach ($workspace in $workspaces) {
        foreach ($other in $workspace.sessions) {
            if ((Test-IssueSessionName $other.name $Number) -and $other.id -ne $session.id) {
                throw [AdoptRefused]::new("issue session '$($other.id)' already exists; run from there or use -NewSession to resume it")
            }
        }
    }
    $ownRegistry = ($caller.Workspace.name -eq $RepoName -and
        (Test-IssueSessionName $session.name $Number) -and $registry.agents.claude.pane -eq $caller.Pane)
    if ($ownRegistry -and $panes.Count -eq 2) {
        $ownRegistry = $registry.agents.codex.pane -in $panes -and $registry.agents.codex.pane -ne $caller.Pane
    }
    $ownRecord = $adoption.session -eq $session.id -and $adoption.claudePane -eq $caller.Pane
    if ($ownRecord) {
        if ($adoption.codexPane) {
            $ownRecord = $adoption.codexPane -in $panes -and $adoption.codexPane -ne $caller.Pane
        } elseif ($panes.Count -eq 2) {
            # A durable intent written before split permits recovery when its reply was
            # received but the new pane id could not be checkpointed.
            $ownRecord = $adoption.stage -eq 'splitting'
        }
    }
    $helper = $session.name -match '^#\d+ (relay|revmux r\d+|your review)$|^(relay|revmux|your review)$'
    $foreignIssue = $session.name -match '^#\d+ ' -and -not (Test-IssueSessionName $session.name $Number)
    if ($helper -or $foreignIssue) { throw [AdoptRefused]::new("caller session '$($session.name)' belongs to another issue or a helper") }
    $owned = $ownRegistry -or $ownRecord
    if (-not $owned -and ($panes.Count -ne 1 -or $session.name -match '^#\d+ ')) {
        throw [AdoptRefused]::new("caller session '$($session.id)' is not owned by this issue; its panes will not be adopted")
    }
    $codex = $null
    if ($panes.Count -eq 2) { $codex = @($panes | Where-Object { $_ -ne $caller.Pane })[0] }
    $mode = 'adopt-fresh'
    if ($owned) { $mode = 'resume-own' }
    $identity = $null
    if (Test-ClaudeCaller) { $identity = Get-AdoptedClaudeIdentity }
    return @{ Mode = $mode; Session = $session; CallerPane = $caller.Pane; CodexPane = $codex; ClaudeIdentity = $identity;
              Workspace = $caller.Workspace; TargetWorkspace = $workspaceId }
}

function Save-AdoptionState([string] $Checkout, $State) {
    $path = Join-Path $Checkout '.workbench\state\adoption.json'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath "$path.tmp" -Encoding UTF8
    Move-Item -LiteralPath "$path.tmp" -Destination $path -Force
}

function Initialize-AdoptedSession($Plan, [string] $Checkout, [string] $RepoName, [int] $Number, [string] $Slug) {
    $script:Launch.SessionId = $Plan.Session.id
    $script:Launch.Claude = $Plan.CallerPane
    $script:Launch.Codex = $Plan.CodexPane
    $state = @{ session = $Plan.Session.id; claudePane = $Plan.CallerPane; stage = 'claimed' }
    if ($Plan.CodexPane) { $state.codexPane = $Plan.CodexPane }
    Save-AdoptionState $Checkout $state
    Set-LaunchStage adopt-rename
    if ($Plan.Session.name -ne "#$Number $Slug") {
        Invoke-Ctl session rename "#$Number $Slug" --target $Plan.Session.id | Out-Null
    }
    $state.stage = 'renamed'
    Save-AdoptionState $Checkout $state
    Set-LaunchStage adopt-workspace
    $workspaceId = $Plan.TargetWorkspace
    if (-not $workspaceId) {
        $workspaceId = (Invoke-Ctl workspace new $RepoName).Trim()
        $guid = [guid]::Empty
        if (-not [guid]::TryParse($workspaceId, [ref]$guid) -or $guid -eq [guid]::Empty) {
            throw "workspace new returned an invalid id: '$workspaceId'"
        }
    }
    if ($Plan.Workspace.id -ne $workspaceId) {
        Set-LaunchStage adopt-move
        Invoke-Ctl session move $workspaceId --target $Plan.Session.id | Out-Null
    }
    $state.stage = 'moved'
    Save-AdoptionState $Checkout $state
}

function Start-WorkbenchSession {
    param([string] $Checkout, [int] $Number, [string] $Slug, [string] $RepoName,
          [string] $ClaudeLaunch, [string] $CodexLaunch, [scriptblock] $RelayCommand, [switch] $NoRelay,
          [string] $AdoptSession, [string] $CallerPane)
    $ErrorActionPreference = 'Stop'
    if (-not $script:Launch) { $script:Launch = @{} }
    $adoptedCodex = $script:Launch.Codex
    foreach ($key in @('SessionId', 'Claude', 'Codex', 'RelaySession', 'MailboxReady', 'ClaudeTyped', 'CodexTyped',
            'RelayStarted', 'RelayCommand', 'RelayStopFile', 'ClaudeLaunchRequired')) {
        $script:Launch.Remove($key)
    }
    if ($AdoptSession) {
        # These ids were already validated before rename/move. Keep them in repair output
        # if rediscovery fails before the new tree can be read.
        $script:Launch.SessionId = $AdoptSession
        $script:Launch.Claude = $CallerPane
        $script:Launch.Codex = $adoptedCodex
    }
    $script:Launch.Checkout = $Checkout
    $script:Launch.ClaudeLaunch = $ClaudeLaunch
    $script:Launch.CodexLaunch = $CodexLaunch
    $script:Launch.NoRelay = [bool]$NoRelay
    $hub = Join-Path $Checkout '.workbench'
    $registryPath = Join-Path $hub 'state\agents.json'
    $registry = $null
    Set-LaunchStage discovery
    if (Test-Path -LiteralPath $registryPath) {
        try { $registry = Get-Content -Raw -LiteralPath $registryPath | ConvertFrom-Json }
        catch { Write-LaunchLog resume "cannot read registry; using session names: $_" }
    }
    $tree = Get-Tree
    if ($AdoptSession) {
        try { $livePlan = Get-AdoptionPlan $tree $Checkout $RepoName $Number }
        catch [AdoptRefused] { throw "adoption changed after setup: $($_.Exception.Message)" }
        if ($livePlan.Session.id -ne $AdoptSession -or $livePlan.CallerPane -ne $CallerPane) {
            throw 'adoption caller session changed'
        }
        $session = $livePlan.Session
    } else { $session = Find-IssueSession $tree $RepoName $Number $Slug $registry }
    if (-not $script:Launch.RestoreConfirmed) { Enable-RestoreReplay }
    $codexRestore = Get-PaneLaunch 'pane-codex.ps1' @{ Checkout = $Checkout; Issue = $script:Launch.IssueRef } -Switches @('Resume')
    $relaySession = $null
    if (-not $NoRelay) {
        $relaySession = Find-RelaySession $tree $RepoName $Number
        if ($relaySession) { $script:Launch.RelaySession = $relaySession.id }
    }
    $script:Launch.Adopted = $null -ne $session
    if (-not $session) {
        Set-LaunchStage session
        $null = Reserve-ClaudeIdentity $Checkout $script:Launch.IssueRef
        $id = Invoke-Ctl session new --name "#$Number $Slug" --cwd $Checkout `
            --workspace-name $RepoName --create-workspace --command $ClaudeLaunch
        $script:Launch.SessionId = ($id -split '\s+')[0]
        if (-not (Test-SessionGuid $script:Launch.SessionId)) { throw 'session new returned an invalid pane id' }
        $script:Launch.Claude = $script:Launch.SessionId
        $null = Reserve-ClaudeIdentity $Checkout $script:Launch.IssueRef $script:Launch.Claude
        Set-PaneRestore $script:Launch.Claude $ClaudeLaunch
        Start-Sleep -Milliseconds 600
        $session = Get-SessionById $script:Launch.SessionId
        if (-not $session) { throw "session $($script:Launch.SessionId) did not appear in the tree" }
    } else {
        $script:Launch.SessionId = $session.id
        Write-LaunchLog resume "adopting session $($session.id)"
    }
    if ($AdoptSession) {
        $panes = @(Get-PaneIds $session)
        $codex = $null
        if ($panes.Count -eq 2) { $codex = @($panes | Where-Object { $_ -ne $CallerPane })[0] }
        $slot = 'primary'
        if ($panes[0] -ne $CallerPane) { $slot = 'split' }
        $plan = @{ Claude = $CallerPane; Codex = $codex; NeedSplit = $panes.Count -eq 1;
                   ClaudeSlot = $slot; NewPaneRole = 'Codex' }
    } else { $plan = Get-PanePlan $session $registry }
    $claudeSide = 'left'
    $codexSide = 'right'
    if ($plan.ClaudeSlot -eq 'split') { $claudeSide = 'right'; $codexSide = 'left' }
    $script:Launch.Claude = $plan.Claude
    $script:Launch.Codex = $plan.Codex
    $callerIdentity = $null
    if ($AdoptSession) { $callerIdentity = $livePlan.ClaudeIdentity }
    if ($plan.Claude) {
        $null = Reserve-ClaudeIdentity $Checkout $script:Launch.IssueRef $plan.Claude $callerIdentity
        if ($script:Launch.Adopted) { Set-PaneRestore $plan.Claude $ClaudeLaunch }
    } else { $null = Reserve-ClaudeIdentity $Checkout $script:Launch.IssueRef }
    if ($plan.Codex) { Set-PaneRestore $plan.Codex $codexRestore }
    # A new Claude pane needs a launch even if its shell never becomes ready.
    # Existing Claude panes become launch candidates only after a shell is proven below.
    $script:Launch.ClaudeLaunchRequired = $plan.NeedSplit -and $plan.NewPaneRole -eq 'Claude'
    if ($plan.NeedSplit) {
        Set-LaunchStage split
        if ($AdoptSession) {
            Save-AdoptionState $Checkout @{ session = $session.id; claudePane = $CallerPane; stage = 'splitting' }
        }
        $reply = Invoke-Ctl session split on --target $session.id
        $script:Launch[$plan.NewPaneRole] = ($reply -split '\s+')[0]
        $null = @(Get-PaneIds ([pscustomobject]@{ id = $session.id;
            paneIds = @($script:Launch.Claude, $script:Launch.Codex) }))
        if ($AdoptSession) {
            $null = @(Get-PaneIds ([pscustomobject]@{ id = $session.id;
                paneIds = @($CallerPane, $script:Launch.Codex) }))
            Save-AdoptionState $Checkout @{ session = $session.id; claudePane = $CallerPane;
                codexPane = $script:Launch.Codex; stage = 'split' }
        }
        if ($plan.NewPaneRole -eq 'Claude') {
            $null = Reserve-ClaudeIdentity $Checkout $script:Launch.IssueRef $script:Launch.Claude
            Set-PaneRestore $script:Launch.Claude $ClaudeLaunch
        } else { Set-PaneRestore $script:Launch.Codex $codexRestore }
        Set-LaunchStage split
        $confirmed = $false
        foreach ($attempt in 1..30) {
            Start-Sleep -Milliseconds 300
            $updated = Get-SessionById $session.id
            if ($updated) {
                $panes = @(Get-PaneIds $updated)
                if ($panes.Count -eq 2 -and $panes -contains $script:Launch.Claude -and
                    $panes -contains $script:Launch.Codex -and $script:Launch.Claude -ne $script:Launch.Codex) {
                    $confirmed = $true
                    break
                }
            }
        }
        if (-not $confirmed) { throw "the split did not appear for session $($session.id) (reply: $reply)" }
    }
    $relayLine = & $RelayCommand $hub $script:Launch.Claude $script:Launch.Codex
    if ($relaySession) {
        $relayPanes = @(Get-PaneIds $relaySession)
        if ($relayPanes.Count -ne 1) { throw "relay session '$($relaySession.id)' has multiple panes; restart it manually" }
        Set-PaneRestore $relayPanes[0] $relayLine
    }
    $stopFile = Join-Path $hub 'state\relay.stop'
    $restartRelay = $relaySession -and ($plan.NeedSplit -or -not $script:Launch.Adopted -or
        ($registry.agents.claude.pane -and $registry.agents.claude.pane -ne $script:Launch.Claude) -or
        ($registry.agents.codex.pane -and $registry.agents.codex.pane -ne $script:Launch.Codex) -or
        (Test-Path -LiteralPath $stopFile))
    if ($restartRelay) {
        # Persist the restart before overwriting registry bindings. A later failure must not
        # make the next run mistake the old relay's argv for the newly registered pane ids.
        Set-LaunchStage relay-stop
        $script:Launch.RelayStopFile = $stopFile
        Write-LaunchLog relay "stopping relay $($relaySession.id); old panes=$($registry.agents.claude.pane),$($registry.agents.codex.pane); new panes=$($script:Launch.Claude),$($script:Launch.Codex)"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stopFile) | Out-Null
        Set-Content -LiteralPath $stopFile -Value 'launcher: restart with current pane ids' -Encoding UTF8
    }
    Set-LaunchStage mailbox
    $hub = Initialize-Mailbox -Checkout $Checkout -ClaudePane $script:Launch.Claude -CodexPane $script:Launch.Codex
    $script:Launch.MailboxReady = $true
    $script:Launch.RelayCommand = $relayLine
    Write-Done "mailbox ready: $hub"
    $roles = @('Codex')
    # A previous run may have split or registered an empty Claude pane before failing.
    # Fresh sessions already start Claude through --command; never probe/type that pane.
    if ($script:Launch.Adopted -and -not $AdoptSession) { $roles = @('Claude', 'Codex') }
    foreach ($role in $roles) {
        Set-LaunchStage $role.ToLowerInvariant()
        $line = $script:Launch["${role}Launch"]
        $side = $codexSide
        if ($role -eq 'Claude') { $side = $claudeSide }
        Write-Step "waiting for the $side pane's shell prompt"
        $freshPane = $plan.NeedSplit -and $plan.NewPaneRole -eq $role
        $timeout = 3
        if ($freshPane) { $timeout = 90 }
        if (Wait-ShellPrompt -Pane $script:Launch[$role] -TimeoutSeconds $timeout -Adopted:(-not $freshPane)) {
            if ($role -eq 'Claude') { $script:Launch.ClaudeLaunchRequired = $true }
            Invoke-Ctl session type --select "$line`n" --target $script:Launch[$role] | Out-Null
            $script:Launch["${role}Typed"] = $true
            Write-Done "$role starting in the $side pane"
        } else {
            Write-LaunchLog $role.ToLowerInvariant() 'pane is not a proven shell; no launch text sent'
            if ($AdoptSession -and $freshPane) { throw 'new Codex pane did not reach a shell prompt; rerun to complete adoption' }
            Write-Warning "the $side pane is not at a proven shell prompt; start $role there yourself with:`n  $line"
        }
    }
    if (-not $NoRelay) {
        Set-LaunchStage relay
        $relay = $script:Launch.RelayCommand
        if ($relaySession) {
            $script:Launch.RelaySession = $relaySession.id
            $relayPanes = @(Get-PaneIds $relaySession)
            if ($relayPanes.Count -ne 1) { throw "relay session '$($relaySession.id)' has multiple panes; restart it manually" }
            $timeout = 3
            if ($restartRelay) { $timeout = 15 }
            if (Wait-ShellPrompt -Pane $relayPanes[0] -TimeoutSeconds $timeout -Adopted) {
                # relay.py leaves its stop file in place. Remove it only once a shell is proven.
                if ($restartRelay) {
                    Remove-Item -LiteralPath $stopFile -ErrorAction Stop
                    $script:Launch.Remove('RelayStopFile')
                }
                Invoke-Ctl session type --select "$relay`n" --target $relayPanes[0] | Out-Null
                $script:Launch.RelayStarted = $true
            } elseif ($restartRelay) {
                throw "relay session '$($relaySession.id)' did not reach a proven shell within 15 s; stop request remains at '$stopFile'. Wait for it to exit before following the repair commands."
            } else {
                Write-LaunchLog relay 'existing relay is not a proven shell; no launch text sent'
                Write-Warning "relay session '$($relaySession.id)' is not at a proven shell prompt; if it has stopped, run there:`n  $relay"
            }
        } else {
            if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile -ErrorAction Stop }
            $reply = Invoke-Ctl session new --name "#$Number relay" --cwd $Checkout --workspace-name $RepoName `
                --no-select --command $relay
            $script:Launch.RelaySession = ($reply -split '\s+')[0]
            Set-PaneRestore $script:Launch.RelaySession $relay
            $script:Launch.RelayStarted = $true
        }
        if ($script:Launch.RelayStarted) { Write-Done 'relay watching the mailbox and the PR' }
    }
    Set-LaunchStage focus
    Invoke-Ctl session select $script:Launch.SessionId | Out-Null
    Invoke-Ctl session focus $plan.ClaudeSlot --target $script:Launch.SessionId | Out-Null
    Set-LaunchStage ready
    if ($AdoptSession) {
        Save-AdoptionState $Checkout @{ session = $session.id; claudePane = $CallerPane;
            codexPane = $script:Launch.Codex; stage = 'ready' }
        Write-Done 'ready: caller pane preserved; adoption context follows'
    } elseif ($script:Launch.ClaudeLaunchRequired -and -not $script:Launch.ClaudeTyped) {
        Write-Done "ready: Claude ($claudeSide) still needs starting by hand:`n  $ClaudeLaunch"
    } else {
        Write-Done "ready: existing non-shell panes left untouched; new agent commands launched where needed"
    }
    return $script:Launch
}

function Format-RepairMessage($Launch) {
    $lines = @("Launcher stopped at stage '$($Launch.Stage)'.")
    if ($Launch.RestoreRepair) { $lines += "Repair restart configuration: $($Launch.RestoreRepair)" }
    foreach ($key in @('Checkout', 'IssueRef', 'SessionId', 'Claude', 'Codex', 'RelaySession')) {
        if ($Launch[$key]) { $lines += "${key}: $($Launch[$key])" }
    }
    if ($Launch.DryRun) { return $lines -join "`n" }
    if ($Launch.MailboxReady) {
        if ($Launch.ClaudeLaunchRequired -and -not $Launch.ClaudeTyped) {
            $lines += "In the Claude pane, once it is at an empty shell prompt: $($Launch.ClaudeLaunch)"
        }
        if (-not $Launch.CodexTyped) {
            $lines += "In the Codex pane, once it is at an empty shell prompt: $($Launch.CodexLaunch)"
        }
        if (-not $Launch.NoRelay -and $Launch.RelayCommand) {
            if ($Launch.RelayStopFile) {
                $lines += "Only after the old relay has exited, clear its stop request: Remove-Item -LiteralPath $(Quote $Launch.RelayStopFile)"
            }
            $lines += "In the relay's shell (only if the relay is not already running): $($Launch.RelayCommand)"
        }
    } else { $lines += 'Session setup or mailbox registration is incomplete; rerun to complete the missing steps.' }
    if ($Launch.IssueRef) {
        $resume = "Resume and complete missing steps: github-workbench $(Quote $Launch.IssueRef)"
        if ($Launch.NoRelay) { $resume += ' -NoRelay' }
        if ($Launch.NewSession) { $resume += ' -NewSession' }
        $lines += $resume
    }
    return $lines -join "`n"
}

function Resolve-IssueRef {
    <# Accepts 123 | #123 | owner/repo#123 | owner/repo 123 | https://github.com/owner/repo/issues/123
       A bare number takes the repository from the current directory's git remote. #>
    param([string] $Ref, [string] $RepoHint)
    $Ref = $Ref.Trim()
    if ($Ref -match '^https?://github\.com/([^/\s]+/[^/\s]+)/(issues|pull)/(\d+)') {
        return @{ Repo = $Matches[1]; Number = [int]$Matches[3] }
    }
    if ($Ref -match '^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)$') {
        return @{ Repo = $Matches[1]; Number = [int]$Matches[2] }
    }
    if ($Ref -match '^#?(\d+)$') {
        $repo = $RepoHint
        if (-not $repo) {
            $repo = (& gh repo view --json nameWithOwner --jq .nameWithOwner 2>$null)
            if ($LASTEXITCODE -ne 0 -or -not $repo) {
                throw "'$Ref' is a bare issue number, and this directory is not a GitHub checkout. Use owner/repo#$($Matches[1]) or the issue URL."
            }
        }
        return @{ Repo = "$repo".Trim(); Number = [int]$Matches[1] }
    }
    throw "cannot read '$Ref' as an issue. Use 123, owner/repo#123, or https://github.com/owner/repo/issues/123"
}

function ConvertTo-Slug([string] $Text, [int] $Max = 40) {
    $slug = ($Text.ToLowerInvariant() -replace '[^a-z0-9]+', '-').Trim('-')
    if ($slug.Length -gt $Max) { $slug = $slug.Substring(0, $Max).Trim('-') }
    if (-not $slug) { $slug = 'issue' }
    return $slug
}

function Get-IssueInfo([hashtable] $Issue) {
    $json = & gh issue view $Issue.Number --repo $Issue.Repo --json number,title,url,state 2>&1
    if ($LASTEXITCODE -ne 0) { throw "gh could not read $($Issue.Repo)#$($Issue.Number): $json" }
    return ($json | ConvertFrom-Json)
}

# --- the checkout ----------------------------------------------------------------------------

function New-IssueCheckout {
    <# A FULL clone per issue, not a worktree: a worktree's .git lives outside the checkout, and
       Codex's workspace-write sandbox would then be unable to commit. Reused if it already exists,
       so running github-workbench again on the same issue resumes rather than starts over. #>
    param([hashtable] $Issue, [string] $Title, [string] $Root)
    $name = ($Issue.Repo -split '/')[1]
    $dir = Join-Path $Root "$name-issue-$($Issue.Number)"
    if ($script:Launch) { $script:Launch.Checkout = $dir }
    $branch = "issue-$($Issue.Number)-$(ConvertTo-Slug $Title 32)"
    if (Test-Path -LiteralPath (Join-Path $dir '.git')) {
        Write-Step "reusing $dir"
    } else {
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        Write-Step "cloning $($Issue.Repo) into $dir"
        & gh repo clone $Issue.Repo $dir -- --quiet | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "gh repo clone failed" }
    }
    Connect-LaunchLog (Join-Path $dir '.workbench\state\launch.log')
    Push-Location $dir
    try {
        $existing = & git rev-parse --abbrev-ref HEAD
        if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed in $dir (exit $LASTEXITCODE)" }
        if ($existing -notlike "issue-$($Issue.Number)-*") {
            $default = (& gh repo view $Issue.Repo --json defaultBranchRef --jq .defaultBranchRef.name).Trim()
            if ($LASTEXITCODE -ne 0 -or -not $default) { throw 'could not read the default branch' }
            & git fetch --quiet origin $default
            if ($LASTEXITCODE -ne 0) { throw 'git fetch failed' }
            $known = & git branch --list "issue-$($Issue.Number)-*"
            if ($LASTEXITCODE -ne 0) { throw 'git branch lookup failed' }
            if ($known) { $branch = ("$known" -replace '^\*?\s*', '').Trim(); & git checkout --quiet $branch }
            else { & git checkout --quiet -b $branch "origin/$default" }
            if ($LASTEXITCODE -ne 0) { throw 'git checkout failed' }
        } else { $branch = $existing }
        # keep the workbench's own files out of the project without touching its .gitignore
        $exclude = Join-Path $dir '.git\info\exclude'
        $lines = @('.workbench/', '.revmux/')
        $current = ''
        if (Test-Path -LiteralPath $exclude) { $current = Get-Content -Raw -LiteralPath $exclude }
        foreach ($line in $lines) { if ($current -notmatch [regex]::Escape($line)) { Add-Content -LiteralPath $exclude -Value $line } }
    } finally { Pop-Location }
    return @{ Dir = $dir; Branch = $branch }
}

function Initialize-Mailbox {
    param([string] $Checkout, [string] $ClaudePane, [string] $CodexPane)
    $hub = Join-Path $Checkout '.workbench'
    New-Item -ItemType Directory -Force -Path $hub | Out-Null
    $env:AI_HUB = $hub
    $py = @"
import sys; sys.path.insert(0, sys.argv[1])
import hub; hub.reload_paths()
hub.register('claude', tool='claude', pane=sys.argv[3], role='planner and reviewer', cwd=sys.argv[2])
hub.register('codex', tool='codex', pane=sys.argv[4], role='implementer', cwd=sys.argv[2])
"@
    $py | & python - $script:Lib $Checkout $ClaudePane $CodexPane | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not initialise the workbench mailbox" }
    return $hub
}

function Grant-CodexTrust {
    <# Codex stops on a "Do you trust the contents of this directory?" chooser the first time it runs
       in a new directory, and the relay - correctly - will not type into a dialog. So the loop would
       sit on that chooser for every new issue. The human authorised this clone by running
       github-workbench on it, so the workbench records that trust the way Codex does itself when
       you answer "Yes": a lowercase [projects.'<path>'] entry in ~/.codex/config.toml. Only for
       clones this tool created under checkoutRoot; never for anything else. #>
    param([string] $Dir)
    $config = Join-Path $HOME '.codex\config.toml'
    $key = $Dir.ToLowerInvariant()
    $existing = ''
    if (Test-Path -LiteralPath $config) { $existing = Get-Content -Raw -LiteralPath $config }
    if ($existing -match [regex]::Escape("[projects.'$key']")) { return }
    $entry = "`n[projects.'$key']`ntrust_level = `"trusted`"`n"
    Add-Content -LiteralPath $config -Value $entry -Encoding utf8
    Write-Step "codex: trusted $key (this clone only)"
}

function Grant-ClaudeTrust {
    <# Claude Code's folder-trust dialog is not skipped by --dangerously-skip-permissions, and its
       default is "No, exit". See lib/trust.py for why and how the entry is written. #>
    param([string] $Dir)
    $result = & python (Join-Path $script:Lib 'trust.py') --claude $Dir 2>&1
    if ($LASTEXITCODE -eq 0) { Write-Step "claude: $result (this clone only)" }
    else { Write-Warning "claude trust not recorded ($result) - answer its trust prompt in the left pane yourself" }
}

# --- launcher entry --------------------------------------------------------------------------

function Quote([string] $Value) { return "'" + $Value.Replace("'", "''") + "'" }

function Get-PaneLaunchArgs([string] $Script, [Alias('Parameters')] [hashtable] $Arguments, [string[]] $Switches = @()) {
    # A shell executable run explicitly with -ExecutionPolicy Bypass, so a machine whose policy is
    # Restricted still runs the pane script - `& 'x.ps1'` alone would be refused there.
    $shell = 'powershell.exe'
    if (Get-Command pwsh -ErrorAction SilentlyContinue) { $shell = 'pwsh' }
    $prefix = @('-NoLogo', '-ExecutionPolicy', 'Bypass', '-File')
    $scriptPath = Join-Path $script:Lib $Script
    $parameters = @()
    $parts = $prefix + @($scriptPath)
    foreach ($key in $Arguments.Keys) {
        $parameters += @{ Name = "-$key"; Value = [string]$Arguments[$key] }
        $parts += @("-$key", [string]$Arguments[$key])
    }
    foreach ($name in $Switches) {
        if ($name -notmatch '^[A-Za-z][A-Za-z0-9]*$') { throw "invalid launch switch '$name'" }
        $parts += "-$name"
    }
    return @{ Exe = $shell; Prefix = $prefix; ScriptPath = $scriptPath; Parameters = $parameters; Switches = $Switches; Args = $parts }
}

function Get-PaneLaunch([string] $Script, [Alias('Parameters')] [hashtable] $Arguments, [string[]] $Switches = @()) {
    $launch = Get-PaneLaunchArgs $Script $Arguments -Switches $Switches
    $parts = @($launch.Exe) + $launch.Prefix + @((Quote $launch.ScriptPath))
    foreach ($parameter in $launch.Parameters) {
        $parts += @($parameter.Name, (Quote $parameter.Value))
    }
    foreach ($name in $launch.Switches) { $parts += "-$name" }
    return ($parts -join ' ')
}

function Invoke-ClaudeHere([string] $Checkout, [string] $Issue) {
    $launch = Get-PaneLaunchArgs 'pane-claude.ps1' @{ Checkout = $Checkout; Issue = $Issue }
    $launchArgs = $launch.Args
    & $launch.Exe @launchArgs
}

function Quote-Bash([string] $Value) { return "'" + $Value.Replace("'", "'\''") + "'" }

function Format-AdoptedBlock([string] $Checkout, [string] $Issue) {
    $path = Join-Path $Checkout '.workbench\state\adopted.sh'
    $lines = @("cd -- $(Quote-Bash ($Checkout -replace '\\', '/')) || return 1 2>/dev/null || exit 1",
        "export AGWORKBENCH=$(Quote-Bash $script:Root) AI_HUB=$(Quote-Bash (Join-Path $Checkout '.workbench')) AI_BOX=claude")
    [IO.File]::WriteAllText($path, (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding $false))
    Write-Host "WORKBENCH ADOPTED`nissue:    $Issue`ncheckout: $Checkout"
    Write-Host "context:  . $(Quote-Bash ($path -replace '\\', '/'))"
    Write-Host "next:     run the start-github-issue skill for $Issue; prefix EVERY shell command (including wait-mail) with the context line and ' && '; use absolute paths under checkout for EVERY loop file read/write and source review, including file tools"
}

function Invoke-LauncherBody {
    param([string] $Issue, [string] $Repo, [switch] $DryRun, [switch] $Yes, [switch] $NoRelay, [switch] $NewSession)
    $script:Launch.ClaudeHerePending = $false
    $script:Launch.RestoreConfirmed = $false
    $script:Launch.ExitCode = 0
    $script:Launch.NewSession = [bool]$NewSession
    $script:Launch.DryRun = [bool]$DryRun
    $script:Launch.NoRelay = [bool]$NoRelay
    Set-LaunchStage config
    $config = Get-WorkbenchConfig
    Set-LaunchStage resolve
    $ref = Resolve-IssueRef -Ref $Issue -RepoHint $Repo
    $issueRef = "$($ref.Repo)#$($ref.Number)"
    $script:Launch.IssueRef = $issueRef
    $info = Get-IssueInfo $ref
    $repoName = ($ref.Repo -split '/')[1]
    $slug = ConvertTo-Slug $info.title 24
    Write-Host "workbench for $issueRef - $($info.title)" -ForegroundColor Cyan
    if ($info.state -ne 'OPEN') { Write-Warning "issue is $($info.state)" }
    $adoptionPlan = $null
    if ((Test-InsideAgwinterm) -and -not $NewSession) {
        Set-LaunchStage adoption-preflight
        $expectedCheckout = Join-Path $config.checkoutRoot "$repoName-issue-$($ref.Number)"
        $adoptionPlan = Get-AdoptionPlan (Get-Tree) $expectedCheckout $repoName $ref.Number
    }

    # --- 1. the terminal --------------------------------------------------------------------------
    Set-LaunchStage terminal
    if (Test-InsideAgwinterm) {
        if ($adoptionPlan) { Write-Step "inside agwinterm: adopting session $($adoptionPlan.Session.id)" }
        else { Write-Step "inside agwinterm: opening the session in this window" }
    } elseif (Get-AgwintermCtl) {
        if ($DryRun) { Write-Step "would start agwinterm if it is not running" }
        elseif (-not (Test-AgwintermRunning)) { Start-AgwintermApp }
        else { Write-Step "agwinterm is running: opening the session there" }
    } else {
        if ($DryRun) { Write-Step "would install agwinterm with scoop, then start it" }
        else { Install-Agwinterm -Yes:$Yes; Start-AgwintermApp }
    }

    Set-LaunchStage checkout
    # --- 2. the checkout --------------------------------------------------------------------------
    if ($DryRun) {
        $dir = Join-Path $config.checkoutRoot "$repoName-issue-$($ref.Number)"
        $co = @{ Dir = $dir; Branch = "issue-$($ref.Number)-$(ConvertTo-Slug $info.title 32)" }
        Write-Step "would clone $($ref.Repo) into $($co.Dir) on branch $($co.Branch)"
    } else {
        $co = New-IssueCheckout -Issue $ref -Title $info.title -Root $config.checkoutRoot
        Set-LaunchStage trust
        Grant-CodexTrust -Dir $co.Dir
        Grant-ClaudeTrust -Dir $co.Dir
    }
    $hubDir = Join-Path $co.Dir '.workbench'
    $script:Launch.Checkout = $co.Dir

    $claudeLaunch = Get-PaneLaunch 'pane-claude.ps1' @{ Checkout = $co.Dir; Issue = $issueRef }
    $codexLaunch = Get-PaneLaunch 'pane-codex.ps1' @{ Checkout = $co.Dir; Issue = $issueRef }
    if ($DryRun) {
        if ($adoptionPlan) {
            Write-Step "would $($adoptionPlan.Mode) session '$($adoptionPlan.Session.id)' as '#$($ref.Number) $slug' in workspace '$repoName'"
            Write-Step "caller pane '$($adoptionPlan.CallerPane)' preserved; would write adoption state and Bash context"
            if (-not (Test-ClaudeCaller)) { Write-Step "would start Claude here after successful setup: $claudeLaunch" }
        } else {
            Write-Step "would open session '#$($ref.Number) $slug' in workspace '$repoName'"
            Write-Step "left pane:  $claudeLaunch"
        }
        Write-Step "right pane: $codexLaunch"
        Write-Step "relay:      python lib\relay.py --hub $hubDir --repo $($ref.Repo) --branch $($co.Branch)"
        return
    }

    $relayBuilder = {
        param($Hub, $Left, $Right)
        'python ' + (Quote (Join-Path $script:Lib 'relay.py')) + ' --hub ' + (Quote $Hub) +
            ' --claude-pane ' + (Quote $Left) + ' --codex-pane ' + (Quote $Right) +
            ' --repo ' + (Quote $ref.Repo) + ' --branch ' + (Quote $co.Branch)
    }
    $adoptArgs = @{}
    if ($adoptionPlan) {
        Set-LaunchStage adoption-recheck
        $currentPlan = Get-AdoptionPlan (Get-Tree) $co.Dir $repoName $ref.Number
        $previousPanes = (@(Get-PaneIds $adoptionPlan.Session) | Sort-Object) -join ','
        $currentPanes = (@(Get-PaneIds $currentPlan.Session) | Sort-Object) -join ','
        if ($currentPlan.Session.id -ne $adoptionPlan.Session.id -or
            $currentPlan.CallerPane -ne $adoptionPlan.CallerPane -or $currentPanes -ne $previousPanes -or
            $currentPlan.Workspace.id -ne $adoptionPlan.Workspace.id -or
            $currentPlan.Workspace.name -ne $adoptionPlan.Workspace.name -or
            $currentPlan.TargetWorkspace -ne $adoptionPlan.TargetWorkspace -or
            $currentPlan.Mode -ne $adoptionPlan.Mode) {
            throw [AdoptRefused]::new('caller panes or workspace eligibility changed during setup; rerun to reassess adoption')
        }
        $adoptionPlan = $currentPlan
        Enable-RestoreReplay
        $script:Launch.SessionId = $adoptionPlan.Session.id
        $script:Launch.Claude = $adoptionPlan.CallerPane
        $script:Launch.Codex = $adoptionPlan.CodexPane
        $null = Reserve-ClaudeIdentity $co.Dir $issueRef $adoptionPlan.CallerPane $adoptionPlan.ClaudeIdentity
        Set-PaneRestore $adoptionPlan.CallerPane $claudeLaunch
        if ($adoptionPlan.CodexPane) {
            $codexRestore = Get-PaneLaunch 'pane-codex.ps1' @{ Checkout = $co.Dir; Issue = $issueRef } -Switches @('Resume')
            Set-PaneRestore $adoptionPlan.CodexPane $codexRestore
        }
        Initialize-AdoptedSession $adoptionPlan $co.Dir $repoName $ref.Number $slug
        $adoptArgs = @{ AdoptSession = $adoptionPlan.Session.id; CallerPane = $adoptionPlan.CallerPane }
    }
    Start-WorkbenchSession -Checkout $co.Dir -Number $ref.Number -Slug $slug -RepoName $repoName `
        -ClaudeLaunch $claudeLaunch -CodexLaunch $codexLaunch -RelayCommand $relayBuilder -NoRelay:$NoRelay @adoptArgs
    if ($adoptionPlan) {
        Format-AdoptedBlock $co.Dir $issueRef
        if (-not (Test-ClaudeCaller)) { $script:Launch.ClaudeHerePending = $true }
    }
}
