# Shared helpers for github-workbench. Dot-sourced; defines functions only.
# Written for Windows PowerShell 5.1 as well as PowerShell 7: no ternaries, no ?? operators.

$script:Lib = $PSScriptRoot
$script:Root = Split-Path -Parent $PSScriptRoot

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

function Start-WorkbenchSession {
    param([string] $Checkout, [int] $Number, [string] $Slug, [string] $RepoName,
          [string] $ClaudeLaunch, [string] $CodexLaunch, [scriptblock] $RelayCommand, [switch] $NoRelay)
    $ErrorActionPreference = 'Stop'
    if (-not $script:Launch) { $script:Launch = @{} }
    foreach ($key in @('SessionId', 'Claude', 'Codex', 'RelaySession', 'MailboxReady', 'ClaudeTyped', 'CodexTyped',
            'RelayStarted', 'RelayCommand', 'RelayStopFile', 'ClaudeLaunchRequired')) {
        $script:Launch.Remove($key)
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
    $session = Find-IssueSession $tree $RepoName $Number $Slug $registry
    $relaySession = $null
    if (-not $NoRelay) {
        $relaySession = Find-RelaySession $tree $RepoName $Number
        if ($relaySession) { $script:Launch.RelaySession = $relaySession.id }
    }
    $script:Launch.Adopted = $null -ne $session
    if (-not $session) {
        Set-LaunchStage session
        $id = Invoke-Ctl session new --name "#$Number $Slug" --cwd $Checkout `
            --workspace-name $RepoName --create-workspace --command $ClaudeLaunch
        $script:Launch.SessionId = ($id -split '\s+')[0]
        Start-Sleep -Milliseconds 600
        $session = Get-SessionById $script:Launch.SessionId
        if (-not $session) { throw "session $($script:Launch.SessionId) did not appear in the tree" }
    } else {
        $script:Launch.SessionId = $session.id
        Write-LaunchLog resume "adopting session $($session.id)"
    }
    $plan = Get-PanePlan $session $registry
    $claudeSide = 'left'
    $codexSide = 'right'
    if ($plan.ClaudeSlot -eq 'split') { $claudeSide = 'right'; $codexSide = 'left' }
    $script:Launch.Claude = $plan.Claude
    $script:Launch.Codex = $plan.Codex
    # A new Claude pane needs a launch even if its shell never becomes ready.
    # Existing Claude panes become launch candidates only after a shell is proven below.
    $script:Launch.ClaudeLaunchRequired = $plan.NeedSplit -and $plan.NewPaneRole -eq 'Claude'
    if ($plan.NeedSplit) {
        Set-LaunchStage split
        $reply = Invoke-Ctl session split on --target $session.id
        $script:Launch[$plan.NewPaneRole] = ($reply -split '\s+')[0]
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
    $script:Launch.RelayCommand = & $RelayCommand $hub $script:Launch.Claude $script:Launch.Codex
    Write-Done "mailbox ready: $hub"
    $roles = @('Codex')
    # A previous run may have split or registered an empty Claude pane before failing.
    # Fresh sessions already start Claude through --command; never probe/type that pane.
    if ($script:Launch.Adopted) { $roles = @('Claude', 'Codex') }
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
            $script:Launch.RelayStarted = $true
        }
        if ($script:Launch.RelayStarted) { Write-Done 'relay watching the mailbox and the PR' }
    }
    Set-LaunchStage focus
    Invoke-Ctl session select $script:Launch.SessionId | Out-Null
    Invoke-Ctl session focus $plan.ClaudeSlot --target $script:Launch.SessionId | Out-Null
    Set-LaunchStage ready
    if ($script:Launch.ClaudeLaunchRequired -and -not $script:Launch.ClaudeTyped) {
        Write-Done "ready: Claude ($claudeSide) still needs starting by hand:`n  $ClaudeLaunch"
    } else {
        Write-Done "ready: Claude ($claudeSide) is running /start-github-issue $($script:Launch.IssueRef)"
    }
    return $script:Launch
}

function Format-RepairMessage($Launch) {
    $lines = @("Launcher stopped at stage '$($Launch.Stage)'.")
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
import sys; sys.path.insert(0, r'$script:Lib')
import hub; hub.reload_paths()
hub.register('claude', tool='claude', pane=r'$ClaudePane', role='planner and reviewer', cwd=r'$Checkout')
hub.register('codex', tool='codex', pane=r'$CodexPane', role='implementer', cwd=r'$Checkout')
"@
    $py | & python - | Out-Null
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

function Get-PaneLaunch([string] $Script, [hashtable] $Arguments) {
    # A shell executable run explicitly with -ExecutionPolicy Bypass, so a machine whose policy is
    # Restricted still runs the pane script - `& 'x.ps1'` alone would be refused there.
    $shell = 'powershell.exe'
    if (Get-Command pwsh -ErrorAction SilentlyContinue) { $shell = 'pwsh' }
    $parts = @($shell, '-NoLogo', '-ExecutionPolicy', 'Bypass', '-File', (Quote (Join-Path $script:Lib $Script)))
    foreach ($key in $Arguments.Keys) { $parts += @("-$key", (Quote ([string]$Arguments[$key]))) }
    return ($parts -join ' ')
}

function Invoke-LauncherBody {
    param([string] $Issue, [string] $Repo, [switch] $DryRun, [switch] $Yes, [switch] $NoRelay)
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

    # --- 1. the terminal --------------------------------------------------------------------------
    Set-LaunchStage terminal
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

    $claudeLaunch = Get-PaneLaunch 'pane-claude.ps1' @{ Checkout = $co.Dir; Issue = $issueRef }
    $codexLaunch = Get-PaneLaunch 'pane-codex.ps1' @{ Checkout = $co.Dir; Issue = $issueRef }
    if ($DryRun) {
        Write-Step "would open session '#$($ref.Number) $slug' in workspace '$repoName'"
        Write-Step "left pane:  $claudeLaunch"
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
    Start-WorkbenchSession -Checkout $co.Dir -Number $ref.Number -Slug $slug -RepoName $repoName `
        -ClaudeLaunch $claudeLaunch -CodexLaunch $codexLaunch -RelayCommand $relayBuilder -NoRelay:$NoRelay
}
