# github-workbench - the PowerShell entry point (#38). install.ps1 copies this file next to
# github-workbench.cmd as github-workbench.ps1, when PowerShell may run local scripts. PowerShell
# then finds it before the .cmd, whose command line would split an argument at its inner double
# quotes (-Queue 'where: bug AND NOT "needs design"'). cmd.exe and Git Bash keep using the .cmd.
#
# The arguments go to the launcher as a JSON array in a one-off environment variable, in a child
# PowerShell like the .cmd starts: this session's location, variables and environment stay as they
# were, prompts still reach this console, and the exit code comes back unchanged.
$shell = 'powershell.exe'
if (Get-Command pwsh -CommandType Application -ErrorAction SilentlyContinue) { $shell = 'pwsh' }
$name = 'AGWORKBENCH_ARGS_' + [guid]::NewGuid().ToString('N')
$list = @($args | ForEach-Object { if ($null -eq $_) { '' } else { [string]$_ } })
[Environment]::SetEnvironmentVariable($name, (ConvertTo-Json -InputObject $list -Compress), 'Process')
try {
    & $shell -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'lib\github-workbench.ps1') -ArgsEnv $name
    $code = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath "Env:\$name" -ErrorAction SilentlyContinue
}
exit $code
