@echo off
rem github-workbench <issue> - two-pane Claude + Codex workbench for a GitHub issue.
rem Works from cmd and from PowerShell. Prefers PowerShell 7, falls back to Windows PowerShell.
setlocal
set "WB_PS=powershell.exe"
where pwsh >nul 2>nul && set "WB_PS=pwsh"
"%WB_PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0lib\github-workbench.ps1" %*
exit /b %ERRORLEVEL%
