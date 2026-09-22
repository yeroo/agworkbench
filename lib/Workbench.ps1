# Shared helpers for github-workbench. Dot-sourced; defines functions only.
# Written for Windows PowerShell 5.1 as well as PowerShell 7: no ternaries, no ?? operators.

$script:Lib = $PSScriptRoot
$script:Root = Split-Path -Parent $PSScriptRoot

function Write-Step([string] $Text) { Write-Host "  $Text" -ForegroundColor DarkGray }
function Write-Done([string] $Text) { Write-Host "  $Text" -ForegroundColor Green }

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
    $command = Get-Command $Name -CommandType Application, ExternalScript -ErrorAction SilentlyContinue
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
    $ctl = Get-AgwintermCtl
    if (-not $ctl) { throw "agwintermctl not found" }
    $output = & $ctl @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "agwintermctl $($Arguments -join ' '): $output" }
    return ($output | Out-String).Trim()
}

function Test-AgwintermRunning {
    $ctl = Get-AgwintermCtl
    if (-not $ctl) { return $false }
    & $ctl ping *> $null
    return ($LASTEXITCODE -eq 0)
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
        & $ctl install $what | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Done "agwinterm: installed $what" } else { Write-Warning "agwinterm install $what failed" }
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

function Get-Tree { return ((Invoke-Ctl tree --json) | ConvertFrom-Json).result }

function Get-SessionById([string] $Id) {
    foreach ($ws in (Get-Tree).workspaces) { foreach ($s in $ws.sessions) { if ($s.id -eq $Id) { return $s } } }
    return $null
}

function Get-PaneIds($Session) {
    if ($Session.paneIds) { return @($Session.paneIds) }
    return @($Session.id)
}

function Wait-ShellPrompt {
    <# True once the pane's last non-blank row ends in a prompt glyph. An EMPTY pane is not a prompt:
       a pane that has drawn nothing yet must not have a launch line typed into it. #>
    param([string] $Pane, [int] $TimeoutSeconds = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $prompt = '(>|' + [char]0x276F + '|\$|#)\s*$'
    while ((Get-Date) -lt $deadline) {
        $text = Invoke-Ctl session text --target $Pane
        $tail = ($text -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($tail -and $tail -match $prompt) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

# --- the issue -------------------------------------------------------------------------------

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
    $branch = "issue-$($Issue.Number)-$(ConvertTo-Slug $Title 32)"
    if (Test-Path -LiteralPath (Join-Path $dir '.git')) {
        Write-Step "reusing $dir"
    } else {
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        Write-Step "cloning $($Issue.Repo) into $dir"
        & gh repo clone $Issue.Repo $dir -- --quiet | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "gh repo clone failed" }
    }
    Push-Location $dir
    try {
        $existing = & git rev-parse --abbrev-ref HEAD
        if ($existing -notlike "issue-$($Issue.Number)-*") {
            $default = (& gh repo view $Issue.Repo --json defaultBranchRef --jq .defaultBranchRef.name).Trim()
            & git fetch --quiet origin $default
            $known = & git branch --list "issue-$($Issue.Number)-*"
            if ($known) { $branch = ("$known" -replace '^\*?\s*', '').Trim(); & git checkout --quiet $branch }
            else { & git checkout --quiet -b $branch "origin/$default" }
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
