"""The PowerShell side, composed without a terminal: issue parsing and Codex's launch policy.

Every case runs PowerShell in a child process against a temp config (AGWORKBENCH_CONFIG), so the
suite never reads the user's own ~/.agworkbench.json and never starts an agent.
Skipped whole when pwsh is not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
PWSH = shutil.which("pwsh")

if PWSH is None:
    raise unittest.SkipTest("pwsh not on PATH")


def ps(script: str, config: dict | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ if env is None else env)
    tmp = None
    if config is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, tmp)
        tmp.close()
        env["AGWORKBENCH_CONFIG"] = tmp.name
    try:
        return subprocess.run([PWSH, "-NoProfile", "-Command", script], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=str(ROOT), env=env)
    finally:
        if tmp is not None:
            Path(tmp.name).unlink(missing_ok=True)


def resolve(ref: str, hint: str = "o/r") -> str:
    result = ps(f". ./lib/Workbench.ps1; $r = Resolve-IssueRef -Ref '{ref}' -RepoHint '{hint}'; "
                f"\"$($r.Repo)#$($r.Number)\"")
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class IssueRefs(unittest.TestCase):
    def test_every_accepted_spelling(self):
        cases = {
            "https://github.com/yeroo/agworkbench/issues/7": "yeroo/agworkbench#7",
            "https://github.com/yeroo/agworkbench/pull/9": "yeroo/agworkbench#9",
            "yeroo/agworkbench#7": "yeroo/agworkbench#7",
            "7": "o/r#7",
            "#7": "o/r#7",
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                self.assertEqual(expected, resolve(ref))

    def test_garbage_is_refused_rather_than_guessed(self):
        result = ps(". ./lib/Workbench.ps1; Resolve-IssueRef -Ref 'not an issue' -RepoHint 'o/r'")
        self.assertNotEqual(0, result.returncode)

    def test_slugs_are_short_lowercase_and_safe_for_a_branch_name(self):
        result = ps(". ./lib/Workbench.ps1; ConvertTo-Slug 'Fix: the Retry loop -- ignores Backoff!!' 20")
        slug = result.stdout.strip()
        self.assertRegex(slug, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(slug), 20)


class PaneIdsOfASession(unittest.TestCase):
    def test_an_unsplit_session_yields_its_whole_id_not_its_first_character(self):
        # Consumers capture the flat pipeline with @(), even for a single pane.
        result = ps(". ./lib/Workbench.ps1; $s = [pscustomobject]@{ id = '" + MAIN_ID + "' }; "
                    "$ids = @(Get-PaneIds $s); $ids.Count; $ids[0]; $ids[0].GetType().Name")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["1", MAIN_ID, "String"], result.stdout.split())

    def test_a_split_session_yields_both_panes_in_order(self):
        result = ps(". ./lib/Workbench.ps1; $s = " + ps_json({"id": MAIN_ID, "paneIds": [MAIN_ID, RIGHT_ID]}) +
                    "; $ids = @(Get-PaneIds $s); $ids.Count; $ids -join ','; $ids[0].GetType().Name")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["2", MAIN_ID + "," + RIGHT_ID, "String"], result.stdout.split())

    def test_invalid_pane_lists_are_refused(self):
        for ids in ([], [MAIN_ID, RIGHT_ID, RELAY_ID], [MAIN_ID, MAIN_ID], ["1"], [None]):
            with self.subTest(ids=ids):
                result = ps("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; Get-PaneIds " +
                            ps_json({"id": MAIN_ID, "paneIds": ids}))
                self.assertNotEqual(0, result.returncode)
                self.assertIn(MAIN_ID, result.stderr)


class CodexLaunch(unittest.TestCase):
    CHECKOUT = str(ROOT)

    def composed(self, config: dict | None = None) -> str:
        result = ps(f"& ./lib/pane-codex.ps1 -Checkout '{self.CHECKOUT}' -Issue 'o/r#1' -WhatIfOnly",
                    config if config is not None else {})
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout

    def refused(self, config: dict) -> None:
        result = ps(f"& ./lib/pane-codex.ps1 -Checkout '{self.CHECKOUT}' -Issue 'o/r#1' -WhatIfOnly", config)
        self.assertNotEqual(0, result.returncode, f"was not refused: {result.stdout}")
        self.assertNotIn("would run:", result.stdout)

    def test_sandboxed_unprompted_and_rooted_at_the_clone(self):
        line = self.composed()
        self.assertIn("--sandbox workspace-write", line)
        self.assertIn("--ask-for-approval never", line)
        self.assertIn(f"--cd {self.CHECKOUT}", line)

    def test_the_nested_settings_are_pinned(self):
        line = self.composed()
        self.assertIn("sandbox_workspace_write.network_access=false", line)
        self.assertIn("sandbox_workspace_write.writable_roots=[]", line)

    def test_network_opens_only_through_the_config_switch(self):
        self.assertIn("network_access=true", self.composed({"allowNetwork": True}))

    def test_paths_are_injected_as_toml_literal_strings(self):
        # a Windows path in a TOML basic string is a run of invalid escapes
        line = self.composed()
        self.assertIn(f"shell_environment_policy.set.AI_HUB='{self.CHECKOUT}", line)
        self.assertIn("shell_environment_policy.set.AI_BOX='codex'", line)

    def test_config_cannot_redecide_the_policy(self):
        for extra in (["--dangerously-bypass-approvals-and-sandbox"], ["-sdanger-full-access"],
                      ["--add-dir", "C:/"], ["--cd", "C:/"], ["-C", "C:/"], ["--profile", "x"],
                      ["-c", "sandbox_workspace_write.network_access=true"],
                      ["-c", " approval_policy=never"], ["exec", "--yolo"]):
            with self.subTest(extra=extra):
                self.refused({"codexArgs": extra})

    def test_ordinary_extra_arguments_pass(self):
        self.assertIn("-c model=o3", self.composed({"codexArgs": ["-c", "model=o3"]}))


COMPONENTS = ("agworkbench", "agwinterm", "claude", "codex", "revmux", "revdiff", "gh")
MODULE_VERSION = "v0.0.0-20260820161812-4b87635251dc"
WINDOWS_PS = shutil.which("powershell.exe")


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt", "toolchain fixtures use Windows cmd scripts")
class VersionFixtures(unittest.TestCase):
    def setUp(self):
        # Python 3.13's Windows mode-0700 temp directories exclude restricted sandbox
        # tokens. Inherit the checkout's ACL instead, and remove only our unique fixture.
        self.tools = ROOT / ("test versions " + uuid.uuid4().hex)
        self.tools.mkdir()
        self.addCleanup(shutil.rmtree, self.tools)
        self.env = dict(os.environ, PATH=str(self.tools) + os.pathsep +
                        str(Path(os.environ["SystemRoot"]) / "System32"),
                        LOCALAPPDATA=str(self.tools),
                        AGWINTERMCTL=str(self.tools / "agwintermctl.cmd"))
        self.stub("git", ["0408866-dirty"])
        self.stub("agwintermctl", [r"cli 0.20.9 C:\tools\agwintermctl.exe", "app unavailable"],
                  arguments="version")
        self.stub("claude", ["2.1.278 (Claude Code)"], arguments="--version")
        self.stub("codex", ["codex-cli 0.154.0"], arguments="--version")
        self.stub("revmux", ["revmux unknown"], arguments="--version")
        self.stub("revdiff", ["version: unknown"], arguments="--version")
        self.stub("gh", ["gh version 2.94.0 (2026-06-10)", "https://example.invalid/releases"],
                  arguments="--version")
        self.stub("go", ["tool.exe: go1.26.0", f"\tmod\tgithub.com/umputun/revmux\t{MODULE_VERSION}"])

    def stub(self, name, lines=(), code=0, stderr=False, arguments=None, marker=None):
        body = ["@echo off"]
        if arguments is not None:
            body.append(f'if not "%*"=="{arguments}" exit /b 91')
        if marker is not None:
            body.append(f'>"{marker}" echo called')
        for line in lines:
            # Fixture text is data, including parentheses and shell metacharacters.
            escaped = line.replace("^", "^^").replace("%", "%%")
            for char in "&|<>()":
                escaped = escaped.replace(char, "^" + char)
            body.append(("1>&2 " if stderr else "") + "echo(" + escaped)
        body.append(f"exit /b {code}")
        path = self.tools / f"{name}.cmd"
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def report(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(list(COMPONENTS), [line.split()[0] for line in lines])
        values = {}
        for name, line in zip(COMPONENTS, lines):
            self.assertTrue(line.startswith(f"{name:<12} "), line)
            values[name] = line[13:]
            self.assertTrue(values[name], line)
        self.assertTrue(values["agworkbench"].endswith(f" ({ROOT})"))
        return values

    def probe_report(self, setup=""):
        return self.report(ps("$ErrorActionPreference = 'Stop'; . ./lib/Workbench.ps1; " + setup +
                              "Get-ToolchainVersions | ForEach-Object { "
                              "'{0,-12} {1}' -f $_.Name, $_.Version }", env=self.env))

    def run_script(self, args=("-Version",), shell=PWSH):
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(LIB / "github-workbench.ps1"), *args], env=self.env,
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


class VersionProbe(VersionFixtures):
    def test_token_extraction(self):
        cases = [("gh version 2.94.0 (2026-06-10)", "2.94.0"),
                 ("codex-cli 0.154.0", "0.154.0"), ("2.1.278 (Claude Code)", "2.1.278"),
                 (r"cli 0.20.9 C:\x\agwintermctl.exe", "0.20.9"),
                 ("revmux unknown", None), ("", None), ("tool 2.1", "2.1")]
        literals = ", ".join(ps_quote(line) for line, _ in cases)
        result = ps(". ./lib/Workbench.ps1; @( " + literals + " ) | ForEach-Object { "
                    "ConvertTo-Json -Compress -InputObject (Get-VersionToken $_) }", env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([expected for _, expected in cases],
                         [json.loads(line) for line in result.stdout.splitlines()])

    def test_all_seven_from_stubs(self):
        self.assertEqual(dict(zip(COMPONENTS, [f"0408866-dirty ({ROOT})", "0.20.9", "2.1.278",
                                              "0.154.0", MODULE_VERSION, MODULE_VERSION, "2.94.0"])),
                         self.probe_report())

    def test_duplicate_path_matches_use_the_first_application(self):
        later = self.tools / "later"
        later.mkdir()
        for name in ("git", "codex", "go"):
            (later / f"{name}.cmd").write_text("@echo off\necho wrong 9.9.9\nexit /b 0\n",
                                              encoding="utf-8")
        self.env["PATH"] += os.pathsep + str(later)
        values = self.probe_report()
        self.assertEqual(f"0408866-dirty ({ROOT})", values["agworkbench"])
        self.assertEqual("0.154.0", values["codex"])
        self.assertEqual(MODULE_VERSION, values["revmux"])

    def test_application_is_preferred_over_adjacent_powershell_shim(self):
        (self.tools / "codex.ps1").write_text("throw 'PowerShell shim should not run'\n", encoding="utf-8")
        self.assertEqual("0.154.0", self.probe_report()["codex"])

    def test_powershell_probe_does_not_inherit_a_previous_exit_code(self):
        pure = self.tools / "pure.ps1"
        pure.write_text("'pure 1.2.3'\n", encoding="utf-8")
        result = ps(". ./lib/Workbench.ps1; & $env:COMSPEC /d /c 'exit 5'; "
                    "Get-ToolVersion " + ps_quote(pure) + " @()", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("1.2.3", result.stdout.strip())
        self.assertEqual("", result.stderr)

    def test_powershell_git_and_go_fallbacks_reset_previous_exit_codes(self):
        for name, output in (("git", "0408866-dirty"),
                             ("go", f"mod example.invalid/tool {MODULE_VERSION}")):
            (self.tools / f"{name}.cmd").unlink()
            (self.tools / f"{name}.ps1").write_text(ps_quote(output) + "\n", encoding="utf-8")
        result = ps(". ./lib/Workbench.ps1; & $env:COMSPEC /d /c 'exit 5'; "
                    "Get-GoModuleVersion 'unused.exe'", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(MODULE_VERSION, result.stdout.strip())
        values = self.probe_report("& $env:COMSPEC /d /c 'exit 5'; ")
        self.assertEqual(f"0408866-dirty ({ROOT})", values["agworkbench"])

    def test_everything_missing(self):
        self.env["PATH"] = str(self.tools / "empty")
        values = self.probe_report("function Get-AgwintermCtl { return $null }; ")
        self.assertEqual(f"unversioned ({ROOT})", values.pop("agworkbench"))
        self.assertEqual({"missing"}, set(values.values()))

    def test_go_fallback_keeps_the_raw_line_without_metadata(self):
        for state in ("absent", "failed", "no module"):
            with self.subTest(state=state):
                if state == "absent":
                    (self.tools / "go.cmd").unlink()
                elif state == "failed":
                    self.stub("go", [f"mod example.invalid/tool {MODULE_VERSION}"], code=1)
                else:
                    self.stub("go", ["tool.exe: go1.26.0"])
                self.assertEqual("revmux unknown", self.probe_report()["revmux"])

    def test_misbehaving_tools_get_error_labels_not_versions(self):
        self.stub("claude", ["", "2.1.278 (Claude Code)"])
        self.stub("codex")
        self.stub("gh", ["error: gh 2.0.0 is broken"], code=7, stderr=True)
        self.stub("revdiff", code=9)
        self.stub("git", ["fatal: no checkout"], code=128, stderr=True)
        values = self.probe_report()
        self.assertEqual(f"unversioned ({ROOT})", values["agworkbench"])
        self.assertEqual("2.1.278", values["claude"])
        self.assertEqual("error: no output", values["codex"])
        self.assertEqual("error (exit 7): error: gh 2.0.0 is broken", values["gh"])
        self.assertEqual("error (exit 9)", values["revdiff"])

    def test_unstartable_tool_is_an_error(self):
        result = ps("$ErrorActionPreference = 'Stop'; . ./lib/Workbench.ps1; "
                    "Get-ToolVersion " + ps_quote(self.tools / "absent.exe") + " @('--version')",
                    env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertTrue(result.stdout.startswith("error: "), result.stdout)

    def test_lookup_exception_is_isolated(self):
        values = self.probe_report("function Get-AgwintermCtl { throw \"broken lookup`nsecond line\" }; ")
        self.assertEqual("error: broken lookup", values["agwinterm"])
        self.assertEqual("2.94.0", values["gh"])


class VersionReport(VersionFixtures):
    def test_failed_component_does_not_fail_the_report(self):
        (self.tools / "codex.cmd").unlink()
        self.stub("gh", ["broken install"], code=7, stderr=True)
        values = self.report(self.run_script())
        self.assertEqual("missing", values["codex"])
        self.assertEqual("error (exit 7): broken install", values["gh"])

    def test_version_with_anything_else_is_refused_before_anything_runs(self):
        markers = [self.tools / (name + ".called") for name in ("gh", "agwintermctl", "git")]
        for name, marker in zip(("gh", "agwintermctl", "git"), markers):
            self.stub(name, marker=marker)
        for extra, parameter in [(["42"], "Issue"), (["-Repo", "o/r"], "Repo"),
                                 (["-DryRun"], "DryRun"), (["-Yes"], "Yes"), (["-NoRelay"], "NoRelay")]:
            with self.subTest(extra=extra):
                result = self.run_script(["-Version", *extra])
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertEqual(f"-Version takes no other arguments (got: {parameter})",
                                 result.stdout.strip())
                self.assertEqual("", result.stderr)
                self.assertFalse(any(marker.exists() for marker in markers))

    def test_pwsh_smoke(self):
        self.assertEqual("0.20.9", self.report(self.run_script())["agwinterm"])

    @unittest.skipUnless(WINDOWS_PS, "Windows PowerShell not installed")
    def test_windows_powershell_smoke(self):
        self.stub("gh", ["error: gh 2.0.0 is broken"], code=7, stderr=True)
        values = self.report(self.run_script(shell=WINDOWS_PS))
        self.assertEqual("0.20.9", values["agwinterm"])
        self.assertEqual(MODULE_VERSION, values["revmux"])
        self.assertEqual("error (exit 7): error: gh 2.0.0 is broken", values["gh"])

    def test_cmd_wrapper_smoke(self):
        self.env["PATH"] += os.pathsep + str(Path(PWSH).parent)
        for no_current_dir in (False, True):
            with self.subTest(no_current_dir=no_current_dir):
                if no_current_dir:
                    self.env["NoDefaultCurrentDirectoryInExePath"] = "1"
                else:
                    self.env.pop("NoDefaultCurrentDirectoryInExePath", None)
                result = subprocess.run([os.environ["COMSPEC"], "/d", "/c",
                                         str(ROOT / "github-workbench.cmd"), "-Version"],
                                        env=self.env, cwd=ROOT, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace")
                self.assertEqual("0.20.9", self.report(result)["agwinterm"])


MAIN_ID = "11111111-1111-4111-8111-111111111111"
RIGHT_ID = "22222222-2222-4222-8222-222222222222"
RELAY_ID = "33333333-3333-4333-8333-333333333333"
OTHER_ID = "44444444-4444-4444-8444-444444444444"


def ps_json(value):
    return "(ConvertFrom-Json " + ps_quote(json.dumps(value)) + ")"


class ShellReady(unittest.TestCase):
    def test_positive_shell_shapes_and_agent_draft_refusals(self):
        frames = [("PS C:\\x> ", True), ("PS C:\\x>", True),
                  ("project 0.282s\n12:00:56\n\u276f", True),
                  ("PS C:\\x> echo $", False), ("PS C:\\x> Write-Output #", False),
                  ("\u276f", False), (">", False), ("$", False), ("", False),
                  ("----------\n>\n----------\n[Fable 5.1] bypass permissions on", False),
                  ("----------\n\u276f\n----------", False),
                  ("\u203a Ask Codex to do anything\ngpt-test", False),
                  ("esc to interrupt\nPS C:\\x>", False),
                  ("Do you trust this directory? [y/N]", False),
                  ("PS C:\\x> Chat from Workbench:", False)]
        script = ". ./lib/Workbench.ps1; " + "; ".join(
            "ConvertTo-Json (Test-ShellReady " + ps_quote(frame) + ")" for frame, _ in frames)
        result = ps(script)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([expected for _, expected in frames],
                         [json.loads(line) for line in result.stdout.splitlines()])


@unittest.skipUnless(os.name == "nt", "launcher fixtures use Windows cmd scripts")
class LauncherFixtures(unittest.TestCase):
    def setUp(self):
        self.temp = ROOT / ("test launch " + uuid.uuid4().hex)
        self.temp.mkdir()
        self.addCleanup(shutil.rmtree, self.temp)
        self.checkout = self.temp / "repo-issue-7"
        self.checkout.mkdir()
        self.scenario_path = self.temp / "scenario.json"
        self.calls_path = self.temp / "calls.jsonl"
        self.log_path = self.checkout / ".workbench/state/launch.log"
        self.stop_path = self.checkout / ".workbench/state/relay.stop"
        self.registry_path = self.checkout / ".workbench/state/agents.json"
        self.config_path = self.temp / "config.json"
        self.config_path.write_text(json.dumps({"checkoutRoot": str(self.temp)}), encoding="utf-8")
        self.env = dict(os.environ, PATH=str(self.temp) + os.pathsep +
                        str(Path(os.environ["SystemRoot"]) / "System32"),
                        AGWINTERMCTL=str(self.temp / "agwintermctl.ps1"),
                        STUB_CTL_SCENARIO=str(self.scenario_path), STUB_CTL_CALLS=str(self.calls_path),
                        AGWORKBENCH_CONFIG=str(self.config_path), PYTHONIOENCODING="utf-8",
                        CLAUDE_CONFIG_DIR=str(self.temp / 'claude-home'), CODEX_HOME=str(self.temp / 'codex-home'),
                        AI_HUB=str(self.checkout / ".workbench"), AGWINTERM_ENABLED="1",
                        AGWINTERM_SESSION_ID=OTHER_ID)
        # cmd.exe truncates session-type arguments at the embedded submit newline.
        # Forward through PowerShell to preserve the argv the real native ctl receives.
        (self.temp / "agwintermctl.ps1").write_text(
            "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CtlArgs)\n& " +
            ps_quote(sys.executable) + " " + ps_quote(ROOT / "tests/stub_ctl.py") +
            " @CtlArgs\nexit $LASTEXITCODE\n", encoding="utf-8")
        self.cmd("python", f'"{sys.executable}" %*')
        self.scenario = {"main_id": MAIN_ID, "right_id": RIGHT_ID, "relay_id": RELAY_ID,
                         "tree": {"workspaces": []}, "text": {}, "stop_file": str(self.stop_path)}
        self.save_scenario()

    def cmd(self, name, body):
        (self.temp / (name + ".cmd")).write_text("@echo off\n" + body + "\n", encoding="utf-8")

    def save_scenario(self):
        self.scenario_path.write_text(json.dumps(self.scenario), encoding="utf-8")

    def calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]

    def log(self):
        return self.log_path.read_text(encoding="utf-8-sig")

    def setup_ps(self):
        return ("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; "
                "$script:Launch = @{Stage='config'; IssueRef='o/repo#7'; Checkout=" +
                ps_quote(self.checkout) + "}; Enable-LaunchLog; ")

    def flow(self, shell=PWSH, no_relay=False, timeout=40, implementer=None):
        if implementer == 'claude':
            right = ("$codexLine = Get-PaneLaunch 'pane-implementer-claude.ps1' @{Checkout=" + ps_quote(self.checkout) +
                     "; Issue='o/repo#7'}; $codexRestore = $codexLine; ")
        else:
            right = ("$codexLine = Get-PaneLaunch 'pane-codex.ps1' @{Checkout=" + ps_quote(self.checkout) +
                     "; Issue='o/repo#7'}; $codexRestore = Get-PaneLaunch 'pane-codex.ps1' @{Checkout=" + ps_quote(self.checkout) +
                     "; Issue='o/repo#7'} -Switches @('Resume'); ")
        script = (self.setup_ps() + "Connect-LaunchLog " + ps_quote(self.log_path) + "; " + right + "$claudeLine = Get-PaneLaunch 'pane-claude.ps1' @{Checkout=" +
                  ps_quote(self.checkout) + "; Issue='o/repo#7'}; $builder = { param($Hub,$Left,$Right) "
                  "'python ' + (Quote 'relay.py') + ' --hub ' + (Quote $Hub) + "
                  "' --claude-pane ' + (Quote $Left) + ' --codex-pane ' + (Quote $Right) }; "
                  "$ok = Invoke-LaunchSafely { Start-WorkbenchSession -Checkout " + ps_quote(self.checkout) +
                  " -Number 7 -Slug 'fix-x' -RepoName 'repo' -ClaudeLaunch $claudeLine "
                  "-CodexLaunch $codexLine -CodexRestore $codexRestore -RelayCommand $builder " + ("-NoRelay " if no_relay else "") +
                  ("-ImplementerTool " + implementer + " " if implementer else "") +
                  "}; if (-not $ok) {exit 1}; $script:Launch | ConvertTo-Json -Compress")
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                              env=self.env, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    def resumed(self, right_text="Ask Codex to do anything\ngpt-test", relay=True, panes=2):
        main = {"id": MAIN_ID, "name": "#7 fix-x"}
        if panes == 2:
            main["paneIds"] = [MAIN_ID, RIGHT_ID]
        sessions = [main]
        if relay:
            sessions.append({"id": RELAY_ID, "name": "#7 relay"})
        self.scenario["tree"] = {"workspaces": [{"name": "repo", "sessions": sessions}]}
        self.scenario["text"] = {RIGHT_ID: right_text, RELAY_ID: "relay up:"}
        self.save_scenario()

    def register(self, claude=MAIN_ID, codex=RIGHT_ID):
        result = ps(". ./lib/Workbench.ps1; Initialize-Mailbox -Checkout " + ps_quote(self.checkout) +
                    " -ClaudePane " + ps_quote(claude) + " -CodexPane " + ps_quote(codex), env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def surviving_codex(self):
        self.register(claude=MAIN_ID, codex=RIGHT_ID)
        self.scenario.update(main_id=RIGHT_ID, right_id=OTHER_ID,
                             tree={"workspaces": [{"name": "repo", "sessions": [{"id": RIGHT_ID, "name": "#7 fix-x"}]}]},
                             text={RIGHT_ID: "Ask Codex to do anything\ngpt-test"})
        self.save_scenario()


class IssueSessions(LauncherFixtures):
    def test_scoped_names_registry_disambiguation_and_helpers(self):
        registry = self.register()
        self.assertIsNone(registry["agents"]["claude"]["session"])
        main = {"id": MAIN_ID, "name": "#7 fix-x", "paneIds": [MAIN_ID, RIGHT_ID]}
        other = {"id": OTHER_ID, "name": "#7 fix-x"}
        helpers = [{"id": RELAY_ID, "name": name} for name in
                   ("#7 relay", "#7 revmux r1", "#7 your review", "#70 other")]
        tree = {"workspaces": [{"name": "repo", "sessions": [main, *helpers]},
                               {"name": "other-repo", "sessions": [other]}]}
        cases = [(tree, registry, "fix-x", MAIN_ID), (tree, None, "new-title", MAIN_ID),
                 ({"workspaces": [{"name": "repo", "sessions": helpers}]}, None, "fix-x", None)]
        duplicate = {"workspaces": [{"name": "repo", "sessions": [main, other]}]}
        cases.append((duplicate, registry, "fix-x", MAIN_ID))
        stale = json.loads(json.dumps(registry))
        stale["agents"]["claude"]["pane"] = OTHER_ID
        cases.append((tree, stale, "fix-x", MAIN_ID))
        for snapshot, reg, slug, expected in cases:
            result = ps(". ./lib/Workbench.ps1; $s = Find-IssueSession " + ps_json(snapshot) +
                        " 'repo' 7 " + ps_quote(slug) + " " + ps_json(reg) +
                        "; ConvertTo-Json -InputObject $s.id", env=self.env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(expected, json.loads(result.stdout))
        result = ps("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; Find-IssueSession " +
                    ps_json(duplicate) + " 'repo' 7 'fix-x' $null", env=self.env)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(MAIN_ID, result.stderr)
        self.assertIn(OTHER_ID, result.stderr)

    def test_registry_cannot_adopt_another_issue_or_helper(self):
        registry = self.register()
        for name in ("#3 old", "#7 relay", "#7 revmux r1", "#7 your review"):
            tree = {"workspaces": [{"name": "repo", "sessions": [{"id": MAIN_ID, "name": name}]}]}
            result = ps(". ./lib/Workbench.ps1; $s=Find-IssueSession " + ps_json(tree) +
                        " 'repo' 7 'fix-x' " + ps_json(registry) + "; ConvertTo-Json -InputObject $s", env=self.env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIsNone(json.loads(result.stdout))

    def test_relay_scope_and_ambiguity(self):
        tree = {"workspaces": [{"name": "other-repo", "sessions": [{"id": OTHER_ID, "name": "#7 relay"}]},
                               {"name": "repo", "sessions": [{"id": RELAY_ID, "name": "#7 relay"}]}]}
        result = ps(". ./lib/Workbench.ps1; (Find-RelaySession " + ps_json(tree) + " 'repo' 7).id", env=self.env)
        self.assertEqual(RELAY_ID, result.stdout.strip())
        tree["workspaces"][1]["sessions"].append({"id": MAIN_ID, "name": "#7 relay"})
        result = ps("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; Find-RelaySession " +
                    ps_json(tree) + " 'repo' 7", env=self.env)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(RELAY_ID, result.stderr)
        self.assertIn(MAIN_ID, result.stderr)

    def test_pane_roles_use_registry_or_first_pane(self):
        claude_known = self.register(claude=MAIN_ID, codex=OTHER_ID)
        codex_known = self.register(claude=OTHER_ID, codex=MAIN_ID)
        neither_known = self.register(claude=OTHER_ID, codex=RELAY_ID)
        one = {"id": MAIN_ID}
        two = {"id": MAIN_ID, "paneIds": [MAIN_ID, RIGHT_ID]}
        for session, reg, expected in [(one, claude_known, (MAIN_ID, None, True, "primary", "Codex")),
                                      (one, codex_known, (None, MAIN_ID, True, "split", "Claude")),
                                      (one, neither_known, (MAIN_ID, None, True, "primary", "Codex")),
                                      (two, claude_known, (MAIN_ID, RIGHT_ID, False, "primary", "Codex")),
                                      (two, codex_known, (RIGHT_ID, MAIN_ID, False, "split", "Codex")),
                                      (two, neither_known, (MAIN_ID, RIGHT_ID, False, "primary", "Codex"))]:
            result = ps(". ./lib/Workbench.ps1; Get-PanePlan " + ps_json(session) + " " + ps_json(reg) +
                        " | ConvertTo-Json -Compress", env=self.env)
            self.assertEqual(0, result.returncode, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(expected, tuple(plan[k] for k in ("Claude", "Codex", "NeedSplit", "ClaudeSlot", "NewPaneRole")))


class LaunchLog(LauncherFixtures):
    def test_buffer_flush_multiline_append_and_disable(self):
        result = ps(". ./lib/Workbench.ps1; Write-LaunchLog off 'ignored'; Enable-LaunchLog; "
                    "Write-LaunchLog config 'first'; Write-LaunchLog error \"one`ntwo\"; "
                    "Connect-LaunchLog " + ps_quote(self.log_path) + "; Write-LaunchLog done 'last'; "
                    "Disable-LaunchLog; Write-LaunchLog off 'ignored'; Connect-LaunchLog " +
                    ps_quote(self.temp / "disabled.log"), env=self.env)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = self.log().splitlines()
        self.assertEqual(["config first", "error one", "error two", "done last"], [s[20:] for s in lines])
        for line in lines:
            self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d ")
        self.assertFalse((self.temp / "disabled.log").exists())

    def test_log_failure_warns_once_and_preserves_original_error_and_buffer(self):
        blocked = self.temp / "blocked"
        blocked.write_text("file", encoding="utf-8")
        result = ps(self.setup_ps() + "Connect-LaunchLog " + ps_quote(blocked / "launch.log") +
                    "; Write-LaunchLog config 'buffered'; $ok=Invoke-LaunchSafely { throw 'original failure' }; "
                    "if (-not $ok) {exit 1}", env=self.env)
        self.assertEqual(1, result.returncode)
        self.assertEqual(1, result.stdout.count("Cannot write launch log"))
        self.assertIn("original failure", result.stdout)
        self.assertIn("config buffered", result.stdout)
        self.assertIn("github-workbench 'o/repo#7'", result.stdout)


class LauncherFlow(LauncherFixtures):
    def test_invalid_tree_refuses_to_create_a_session(self):
        self.scenario["responses"] = [{"args": r"^tree --json$", "stdout": '{"ok":false,"error":"unavailable"}'}]
        self.save_scenario()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("invalid session tree", self.log())
        self.assertEqual([["tree", "--json"]], self.calls())

    def test_fresh_then_resume_launches_each_component_once(self):
        first = self.flow()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        launch = json.loads(first.stdout.splitlines()[-1])
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        self.assertEqual([["session", "type", "--select", launch["CodexLaunch"] + "\n", "--target", RIGHT_ID]], typed)
        self.assertIn("pane-codex.ps1'", launch["CodexLaunch"])
        self.assertFalse(any(c[:2] == ["session", "text"] and c[-1] == MAIN_ID for c in self.calls()))
        commands = [c[:2] for c in self.calls()]
        self.assertEqual([["tree", "--json"], ["session", "new"],
                          ["session", "restore"], ["tree", "--json"],
                          ["session", "split"], ["session", "restore"], ["tree", "--json"], ["session", "text"],
                          ["session", "type"], ["session", "new"], ["session", "restore"], ["session", "select"],
                          ["session", "focus"]], commands)
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(MAIN_ID, registry["agents"]["claude"]["pane"])
        self.assertEqual(RIGHT_ID, registry["agents"]["codex"]["pane"])
        old_log = self.log()
        second = self.flow()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertTrue(json.loads(second.stdout.splitlines()[-1])["Adopted"])
        self.assertEqual(2, sum(c[:2] == ["session", "new"] for c in self.calls()))
        self.assertEqual(1, sum(c[:2] == ["session", "type"] for c in self.calls()))
        self.assertTrue(self.log().startswith(old_log))
        self.assertIn("prompt-decision", self.log())
        self.assertIn("not a proven shell", self.log())
        state = json.loads(self.scenario_path.read_text(encoding="utf-8"))
        self.assertEqual(["#7 fix-x", "#7 relay"], [s["name"] for s in state["tree"]["workspaces"][0]["sessions"]])
        self.assertFalse(state.get("stop_seen"))
        self.assertFalse(self.stop_path.exists())
        self.assertNotIn("relay watching", second.stdout)
        self.assertIn(launch["RelayCommand"], second.stdout)

    def test_half_built_session_is_completed(self):
        self.resumed("project 0.282s\n12:00:56\n\u276f", relay=False)
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual(1, sum(c[:2] == ["session", "new"] for c in calls))
        self.assertEqual(1, sum(c[:2] == ["session", "type"] for c in calls))
        self.assertFalse(any(c[:2] == ["session", "split"] for c in calls))

    def test_lone_chevron_gets_manual_launch_without_typing(self):
        self.resumed("\u276f")
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("pane-codex.ps1", result.stdout)
        self.assertFalse(any(c[:2] == ["session", "type"] for c in self.calls()))

    def test_one_pane_resume_and_delayed_split_confirmation(self):
        self.resumed(relay=False, panes=1)
        self.scenario["split_delay"] = 6
        self.save_scenario()
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(1, sum(c[:2] == ["session", "split"] for c in self.calls()))
        self.assertEqual(8, sum(c == ["tree", "--json"] for c in self.calls()))

    def test_existing_relay_restarts_in_its_own_shell(self):
        self.resumed()
        self.scenario["text"][RELAY_ID] = "PS C:\\relay> "
        self.save_scenario()
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        relay = ("python 'relay.py' --hub " + ps_quote(self.checkout / ".workbench") +
                 " --claude-pane '" + MAIN_ID + "' --codex-pane '" + RIGHT_ID + "'")
        self.assertEqual([["session", "type", "--select", relay + "\n", "--target", RELAY_ID]], typed)
        self.assertFalse(any(c[:2] == ["session", "new"] for c in self.calls()))

    def test_surviving_codex_gets_a_new_claude_pane(self):
        self.surviving_codex()
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        launch = json.loads(result.stdout.splitlines()[-1])
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        self.assertEqual([["session", "type", "--select", launch["ClaudeLaunch"] + "\n", "--target", OTHER_ID]], typed)
        self.assertIn("pane-claude.ps1'", launch["ClaudeLaunch"])
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(OTHER_ID, registry["agents"]["claude"]["pane"])
        self.assertEqual(RIGHT_ID, registry["agents"]["codex"]["pane"])
        self.assertEqual(["session", "focus", "split", "--target", RIGHT_ID], self.calls()[-1])

    def test_new_claude_is_launched_on_retry_after_mailbox_failure(self):
        self.surviving_codex()
        (self.temp / "python.cmd").unlink()
        failed = self.flow(no_relay=True)
        self.assertEqual(1, failed.returncode, failed.stdout + failed.stderr)
        self.assertIn("stage 'mailbox'", failed.stdout)
        self.assertFalse(any(c[:2] == ["session", "type"] for c in self.calls()))
        self.cmd("python", f'"{sys.executable}" %*')
        retry = self.flow(no_relay=True)
        self.assertEqual(0, retry.returncode, retry.stdout + retry.stderr)
        launch = json.loads(retry.stdout.splitlines()[-1])
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        self.assertEqual([["session", "type", "--select", launch["ClaudeLaunch"] + "\n", "--target", OTHER_ID]], typed)
        self.assertEqual(1, sum(c[:2] == ["session", "split"] for c in self.calls()))
        self.assertTrue(launch["ClaudeLaunchRequired"])
        self.assertTrue(launch["ClaudeTyped"])

    def test_adopted_claude_shell_starts_without_disturbing_codex(self):
        self.resumed(relay=False)
        self.scenario["text"][MAIN_ID] = "PS C:\\checkout> "
        self.save_scenario()
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        launch = json.loads(result.stdout.splitlines()[-1])
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        self.assertEqual([["session", "type", "--select", launch["ClaudeLaunch"] + "\n", "--target", MAIN_ID]], typed)
        self.assertFalse(any(c[:2] in (["session", "new"], ["session", "split"]) for c in self.calls()))

    def test_new_claude_shell_timeout_does_not_claim_claude_is_running(self):
        self.surviving_codex()
        self.scenario["responses"] = [{"args": "^session text --target " + OTHER_ID + "$", "stdout": ""}]
        self.save_scenario()
        result = self.flow(no_relay=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("not at a proven shell prompt; start Claude", result.stdout)
        self.assertIn("ready: Claude (right) still needs starting by hand:", result.stdout)
        self.assertNotIn("is running /start-github-issue", result.stdout)
        launch = json.loads(result.stdout.splitlines()[-1])
        self.assertIn(launch["ClaudeLaunch"], result.stdout)
        self.assertIn(OTHER_ID + " not-proven timeout last-row= waited=90s", self.log())
        self.assertFalse(any(c[:2] == ["session", "type"] for c in self.calls()))

    def test_relay_failure_after_both_agents_start_omits_their_repair_lines(self):
        self.resumed("PS C:\\checkout> ", relay=False)
        self.scenario["text"][MAIN_ID] = "PS C:\\checkout> "
        self.scenario["responses"] = [{"args": r"^session new --name #7 relay", "stdout": "forced failure", "exit": 1}]
        self.save_scenario()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        self.assertEqual([MAIN_ID, RIGHT_ID], [c[-1] for c in typed])
        repair = result.stdout.split("Launcher stopped at stage 'relay'.", 1)[1]
        self.assertNotIn("pane-claude.ps1", repair)
        self.assertNotIn("pane-codex.ps1", repair)
        self.assertIn("In the relay's shell", repair)
        self.assertIn("python 'relay.py'", repair)

    def test_changed_panes_stop_running_relay_before_restart(self):
        self.resumed(panes=1)
        self.register(claude=MAIN_ID, codex=OTHER_ID)
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        state = json.loads(self.scenario_path.read_text(encoding="utf-8"))
        self.assertTrue(state["stop_seen"])
        self.assertFalse(self.stop_path.exists())
        launch = json.loads(result.stdout.splitlines()[-1])
        relay_types = [c for c in self.calls() if c[:2] == ["session", "type"] and c[-1] == RELAY_ID]
        self.assertEqual([["session", "type", "--select", launch["RelayCommand"] + "\n", "--target", RELAY_ID]], relay_types)
        self.assertIn("--codex-pane '" + RIGHT_ID + "'", launch["RelayCommand"])
        self.assertNotIn(OTHER_ID, launch["RelayCommand"])
        self.assertFalse(any(c[:2] == ["session", "new"] for c in self.calls()))

    def test_relay_stop_timeout_never_types_and_retry_consumes_stop_request(self):
        self.resumed(panes=1)
        self.register(claude=MAIN_ID, codex=OTHER_ID)
        self.scenario["ignore_stop"] = True
        self.save_scenario()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("within 15 s", result.stdout)
        self.assertIn("Remove-Item -LiteralPath " + ps_quote(self.stop_path), result.stdout)
        self.assertNotIn("relay watching", result.stdout)
        self.assertTrue(self.stop_path.exists())
        self.assertFalse(any(c[:2] == ["session", "type"] and c[-1] == RELAY_ID for c in self.calls()))
        self.scenario = json.loads(self.scenario_path.read_text(encoding="utf-8"))
        self.scenario["ignore_stop"] = False
        self.save_scenario()
        retry = self.flow()
        self.assertEqual(0, retry.returncode, retry.stdout + retry.stderr)
        self.assertFalse(self.stop_path.exists())
        self.assertEqual(1, sum(c[:2] == ["session", "type"] and c[-1] == RELAY_ID for c in self.calls()))

    def test_changed_panes_request_relay_stop_before_mailbox_failure(self):
        self.resumed(panes=1)
        self.register(claude=MAIN_ID, codex=OTHER_ID)
        (self.temp / "python.cmd").unlink()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("stage 'mailbox'", result.stdout)
        self.assertTrue(self.stop_path.exists())
        self.assertFalse(any(c[:2] == ["session", "type"] for c in self.calls()))

    def test_new_relay_clears_a_stop_file_from_an_old_session(self):
        self.resumed(relay=False)
        self.stop_path.parent.mkdir(parents=True)
        self.stop_path.write_text("old relay stop request", encoding="utf-8")
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(self.stop_path.exists())
        self.assertEqual(1, sum(c[:2] == ["session", "new"] for c in self.calls()))

    def test_registry_second_pane_is_focused(self):
        self.resumed()
        self.scenario["text"][MAIN_ID] = "Ask Codex to do anything\ngpt-test"
        self.save_scenario()
        self.register(claude=RIGHT_ID, codex=MAIN_ID)
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["session", "focus", "split", "--target", MAIN_ID], self.calls()[-1])

    def test_other_issue_number_does_not_prevent_creation(self):
        self.scenario["tree"] = {"workspaces": [{"name": "repo", "sessions": [{"id": OTHER_ID, "name": "#70 other"}]}]}
        self.save_scenario()
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(any(c[:2] == ["session", "new"] for c in self.calls()))

    def test_split_failure_logs_exception_and_only_known_repair_steps(self):
        self.scenario["responses"] = [{"args": r"^session split", "stdout": "split exploded", "exit": 1, "stderr": True}]
        self.save_scenario()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        for text in ("ctl session split", "ctl exit 1: split exploded", "error ", "Start-WorkbenchSession", "Invoke-LaunchSafely"):
            self.assertIn(text, self.log())
        for text in (MAIN_ID, "github-workbench 'o/repo#7'", "incomplete"):
            self.assertIn(text, result.stdout)
        self.assertNotIn("pane-codex.ps1", result.stdout)
        self.assertNotIn("--codex-pane", result.stdout)
        self.assertFalse(any(c[:2] == ["session", "type"] for c in self.calls()))

    def test_mailbox_failure_has_panes_but_no_relay_command(self):
        (self.temp / "python.cmd").unlink()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(RIGHT_ID, result.stdout)
        self.assertIn("stage 'mailbox'", result.stdout)
        self.assertNotIn("--codex-pane", result.stdout)

    def test_typing_and_relay_failures_print_exact_repair_commands(self):
        for pattern in (r"^session type", r"^session new --name #7 relay"):
            with self.subTest(pattern=pattern):
                self.scenario["responses"] = [{"args": pattern, "stdout": "forced failure", "exit": 1}]
                self.save_scenario()
                result = self.flow()
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                for text in ("python 'relay.py'", "--codex-pane '" + RIGHT_ID + "'", str(self.checkout)):
                    self.assertIn(text, result.stdout)
                repair = result.stdout.split("Launcher stopped at stage ", 1)[1]
                self.assertEqual(pattern == r"^session type", "pane-codex.ps1" in repair)

    @unittest.skipUnless(WINDOWS_PS, "Windows PowerShell not installed")
    def test_windows_powershell_split_failure(self):
        self.scenario["responses"] = [{"args": r"^session split", "stdout": "native stderr failure", "exit": 1, "stderr": True}]
        self.save_scenario()
        result = self.flow(shell=WINDOWS_PS)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("ctl exit 1: native stderr failure", self.log())
        self.assertIn("error ", self.log())
        self.assertEqual(1, result.stdout.count("Launcher failed:"))


class RestartPanes(LauncherFixtures):
    def test_transcript_and_rollout_readers_share_and_release_files(self):
        self.transcript()
        transcript = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/project' / (OTHER_ID + '.jsonl')
        rollout = self.rollout('23', MAIN_ID, '2026-09-23T00:00:00Z')
        for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
            with self.subTest(shell=shell):
                command = ("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; "
                           '$paths=@(' + ps_quote(transcript) + ',' + ps_quote(rollout) + '); '
                           "$writers=@($paths | ForEach-Object { [IO.File]::Open($_,'Open','ReadWrite','ReadWrite') }); "
                           'try { $claude=Get-ClaudeTranscript ' + ps_quote(OTHER_ID) + '; '
                           '$codex=Find-CodexSession ' + ps_quote(self.checkout) + '; '
                           '@($claude.Cwd,$codex) | ConvertTo-Json -Compress '
                           '} finally { $writers | ForEach-Object { $_.Dispose() } }; '
                           "$paths | ForEach-Object { $writer=[IO.File]::Open($_,'Open','ReadWrite','None'); $writer.Dispose() }")
                result = subprocess.run([shell, '-NoProfile', '-Command', command], env=self.env, cwd=ROOT,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual([str(self.checkout), MAIN_ID], json.loads(result.stdout))

    def test_transcript_search_checks_all_copies_and_refuses_conflicting_cwds(self):
        projects = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects'
        paths = []
        for name, content in [('a', '{}'), ('b', json.dumps({'cwd': str(self.checkout)}))]:
            path = projects / name / (OTHER_ID + '.jsonl')
            path.parent.mkdir(parents=True)
            path.write_text(content, encoding='utf-8')
            paths.append(path)
        result = ps('. ./lib/Workbench.ps1; Get-ClaudeTranscript ' + ps_quote(OTHER_ID) +
                    ' | ConvertTo-Json -Compress', env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(str(self.checkout), json.loads(result.stdout)['Cwd'])
        paths[0].write_text(json.dumps({'cwd': str(self.temp)}), encoding='utf-8')
        command = ("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; try { Get-ClaudeTranscript " +
                   ps_quote(OTHER_ID) + " } catch { $_.Exception.Message }; " +
                   '@(' + ','.join(ps_quote(p) for p in paths) + ') | ForEach-Object { '
                   "$writer=[IO.File]::Open($_,'Open','ReadWrite','None'); $writer.Dispose() }")
        result = ps(command, env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('transcripts disagree on cwd', result.stdout)
        for path in paths:
            self.assertIn(str(path), result.stdout)

    def identity(self, **changes):
        record = dict(pane=MAIN_ID, sessionId=OTHER_ID, cwd=str(self.checkout), origin='fresh',
                      reservedAt='2026-09-23T00:00:00Z', issue='o/repo#7', checkout=str(self.checkout))
        record.update(changes)
        path = self.checkout / '.workbench/state/claude.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def transcript(self, session_id=OTHER_ID, cwd=None):
        path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/project' / (session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'cwd': str(cwd or self.checkout)}), encoding='utf-8')

    def rollout(self, day, session_id, timestamp, **changes):
        meta = dict(id=session_id, timestamp=timestamp, cwd=str(self.checkout), originator='codex-tui', source='cli')
        meta.update(changes)
        path = Path(self.env['CODEX_HOME']) / 'sessions/2026/09' / day / ('rollout-' + session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload': meta}) + '\nnot parsed: later lines', encoding='utf-8')
        return path

    def pane(self, role, resume=False, shell=PWSH):
        command = '. ./lib/pane-' + role + '.ps1 -Checkout ' + ps_quote(self.checkout) + " -Issue 'o/repo#7' -WhatIfOnly"
        if resume:
            command += ' -Resume'
        return subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                              cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20)

    def test_claude_reservation_and_transcript_choose_fresh_then_same_resume_id(self):
        original = self.temp / "it's the original project"
        original.mkdir()
        path = self.identity(pane=None, cwd=str(original))
        before = path.read_bytes()
        for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
            with self.subTest(shell=shell):
                fresh = self.pane('claude', shell=shell)
                self.assertEqual(0, fresh.returncode, fresh.stdout + fresh.stderr)
                self.assertIn("'--session-id' '" + OTHER_ID + "'", fresh.stdout)
                self.assertIn(str(original), fresh.stdout)
        self.transcript(cwd=original)
        resumed = self.pane('claude')
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertIn("'--resume' '" + OTHER_ID + "'", resumed.stdout)
        self.assertNotIn('/start-github-issue', resumed.stdout)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.registry_path.exists())

    def test_claude_missing_or_malformed_identity_fails_with_repair(self):
        missing = self.pane('claude')
        self.assertEqual(1, missing.returncode)
        self.assertIn("Repair: github-workbench 'o/repo#7'", missing.stdout)
        path = self.identity()
        path.write_text('{broken', encoding='utf-8')
        malformed = self.pane('claude')
        self.assertEqual(1, malformed.returncode)
        self.assertNotIn('would run:', malformed.stdout)

    def test_actual_pane_invocations_preserve_resume_argv_and_workbench_context(self):
        original = self.temp / "it's the original cwd"
        original.mkdir()
        self.identity(cwd=str(original), origin='adopted')
        self.transcript(cwd=original)
        self.registry_path.write_text('{}', encoding='utf-8')
        self.rollout('23', MAIN_ID, '2026-09-23T00:00:00Z')
        for role in ['claude', 'codex']:
            with self.subTest(role=role):
                command = ('function ' + role + ' { param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AgentArgs); '
                           '[pscustomobject]@{Args=$AgentArgs; Cwd=(Get-Location).Path; Hub=$env:AI_HUB; '
                           'Box=$env:AI_BOX; Root=$env:AGWORKBENCH} | ConvertTo-Json -Compress }; '
                           '& ./lib/pane-' + role + '.ps1 -Checkout ' + ps_quote(self.checkout) + " -Issue 'o/repo#7'" +
                           (' -Resume' if role == 'codex' else ''))
                result = ps(command, env=self.env)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                capture = json.loads(result.stdout.splitlines()[-1])
                self.assertEqual(str(self.checkout / '.workbench'), capture['Hub'])
                self.assertEqual(role, capture['Box'])
                self.assertEqual(str(ROOT), capture['Root'])
                if role == 'claude':
                    self.assertEqual(str(original), capture['Cwd'])
                    settings = str(self.checkout / '.workbench/state/claude-settings.json')
                    self.assertEqual(['--settings', settings, '--resume', OTHER_ID], capture['Args'][:4])   # #33
                    self.assertEqual(5, len(capture['Args']))
                    self.assertIn('resumed after an agwinterm restart', capture['Args'][4])
                    self.assertIn('one background wb.py wait-mail waiter', capture['Args'][4])
                    self.assertIn('continue the phase you were in', capture['Args'][4])
                else:
                    args = capture['Args']
                    self.assertEqual(str(self.checkout), capture['Cwd'])
                    self.assertEqual(MAIN_ID, args[args.index('resume') + 1])
                    self.assertEqual('workspace-write', args[args.index('--sandbox') + 1])
                    self.assertEqual('never', args[args.index('--ask-for-approval') + 1])
                    self.assertIn('resumed after an agwinterm restart', args[-1])
                    self.assertIn('implementing or fixing, continue that step and report', args[-1])
                    self.assertIn('Otherwise run python', args[-1])
                    self.assertNotIn('Do not edit anything now', args[-1])

    def test_what_if_preserves_environment_cwd_and_record(self):
        path = self.identity(cwd=str(self.temp / 'missing original directory'))
        before = path.read_bytes()
        command = ("$env:AI_HUB='old-hub'; $env:AI_BOX='old-box'; $before=(Get-Location).Path; "
                   '& ./lib/pane-claude.ps1 -Checkout ' + ps_quote(self.checkout) +
                   " -Issue 'o/repo#7' -WhatIfOnly; "
                   '@($env:AI_HUB,$env:AI_BOX,((Get-Location).Path -eq $before)) | ConvertTo-Json -Compress')
        result = ps(command, env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['old-hub', 'old-box', True], json.loads(result.stdout.splitlines()[-1]))
        self.assertEqual(before, path.read_bytes())

    def test_claude_identity_arguments_are_refused_in_both_modes(self):
        self.identity()
        for resumed in [False, True]:
            if resumed:
                self.transcript()
            for flag in ['--session-id', '--session-id=x', '--resume', '--resume=x', '-r', '-rx', '-r=x',
                         '--continue', '--continue=true', '-c', '-c=true', '--fork-session', '--fork-session=true']:
                with self.subTest(resumed=resumed, flag=flag):
                    self.config_path.write_text(json.dumps({'claudeArgs': [flag]}), encoding='utf-8')
                    result = self.pane('claude')
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn('override the conversation identity', result.stderr)

    def test_codex_newest_interactive_metadata_wins_across_days(self):
        self.rollout('01', MAIN_ID, '2026-09-24T00:00:00.2000000Z', cwd=str(self.checkout).upper().replace('\\', '/') + '/')
        self.rollout('23', RIGHT_ID, '2026-09-24T00:00:00.1000000Z')
        self.rollout('24', OTHER_ID, '2026-09-25T00:00:00Z', originator='codex_exec', source='exec')
        self.rollout('24', RELAY_ID, '2026-09-26T00:00:00Z', cwd=str(self.temp / 'elsewhere'))
        bad = Path(self.env['CODEX_HOME']) / 'sessions/2026/09/24/rollout-bad.jsonl'
        bad.write_text('{broken', encoding='utf-8')
        (bad.parent / 'rollout-empty.jsonl').touch()
        for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
            with self.subTest(shell=shell):
                result = self.pane('codex', resume=True, shell=shell)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn('resume ' + MAIN_ID, result.stdout)
                self.assertIn('--sandbox workspace-write --ask-for-approval never', result.stdout)
                self.assertIn('resumed after an agwinterm restart', result.stdout)
                self.assertIn('agmsg.py" list', result.stdout)
                self.assertNotIn('resume ' + OTHER_ID, result.stdout)

    def test_codex_missing_session_falls_back_to_fresh_prompt(self):
        result = self.pane('codex', resume=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn(' resume ', result.stdout)
        self.assertIn('Right now: wait.', result.stdout)

    def test_codex_resume_rejects_policy_overrides(self):
        self.rollout('23', MAIN_ID, '2026-09-23T00:00:00Z')
        for extra in [['--sandbox=danger-full-access'], ['-sdanger-full-access'], ['--ask-for-approval=on-request'],
                      ['--add-dir', 'C:/'], ['--cd', 'C:/'], ['-C', 'C:/'], ['--profile', 'x'],
                      ['-c', 'sandbox_workspace_write.network_access=true'], ['-c', 'approval_policy=never'],
                      ['exec', '--yolo']]:
            with self.subTest(extra=extra):
                self.config_path.write_text(json.dumps({'codexArgs': extra}), encoding='utf-8')
                result = self.pane('codex', resume=True)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('would run:', result.stdout)

    def test_generated_switch_command_binds_under_both_powershells(self):
        stub = self.temp / "it's a pane.ps1"
        stub.write_text('param($Checkout,$Issue,[switch]$Resume)\n@($Checkout,$Issue,[bool]$Resume) | ConvertTo-Json -Compress', encoding='utf-8')
        for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
            with self.subTest(shell=shell):
                env = dict(self.env, PATH=str(Path(shell).parent) + os.pathsep + self.env['PATH'])
                command = ('. ./lib/Workbench.ps1; $real = ${function:Get-PaneLaunchArgs}; '
                           'function Get-PaneLaunchArgs($Script,$Arguments,$Switches) { '
                           '$l = & $real $Script $Arguments -Switches $Switches; '
                           "$l.Prefix = @('-NoProfile') + $l.Prefix; $l.Exe = " + ps_quote(Path(shell).name) + '; $l }; '
                           '$script:Lib = ' + ps_quote(self.temp) + '; $line = Get-PaneLaunch ' + ps_quote(stub.name) +
                           ' -Arguments @{Checkout=' + ps_quote(self.temp / "it's a checkout") +
                           ";Issue='o/repo#7'} -Switches @('Resume'); & ([scriptblock]::Create($line))")
                result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual([str(self.temp / "it's a checkout"), 'o/repo#7', True], json.loads(result.stdout))


class RestoreFlow(LauncherFixtures):
    def legacy_transcript(self, session_id, mtime, **changes):
        metadata = dict(entrypoint='cli', cwd=str(self.checkout))
        metadata.update(changes)
        encoded = re.sub('[^a-zA-Z0-9]', '-', str(self.checkout))
        path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects' / encoded / (session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n' + json.dumps(metadata) + '\n', encoding='utf-8')
        os.utime(path, (mtime, mtime))
        return path

    def test_running_legacy_claude_recovers_newest_interactive_identity(self):
        self.resumed(relay=False)
        self.scenario['text'][MAIN_ID] = 'Claude is working'
        self.save_scenario()
        self.legacy_transcript(MAIN_ID, 1000)
        selected = self.legacy_transcript(OTHER_ID, 2000)
        self.legacy_transcript(RIGHT_ID, 3000, entrypoint='sdk')
        self.legacy_transcript(RELAY_ID, 4000, cwd=str(self.temp))
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(OTHER_ID, self.record()['sessionId'])
        self.assertEqual('recovered', self.record()['origin'])
        self.assertEqual(MAIN_ID, self.record()['pane'])
        self.assertIn(MAIN_ID, self.pins())
        self.assertFalse(any(c[:2] == ['session', 'type'] and c[-1] == MAIN_ID for c in self.calls()))
        # Recovery also disposes its reader before returning to the same process.
        check = ps("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; $null=Find-LegacyClaudeIdentity " +
                   ps_quote(self.checkout) + '; $stream=[IO.File]::Open(' + ps_quote(selected) +
                   ",'Open','ReadWrite','None'); $stream.Dispose()", env=self.env)
        self.assertEqual(0, check.returncode, check.stdout + check.stderr)

    def test_unknown_legacy_claude_is_not_assigned_an_unstarted_identity(self):
        self.resumed(relay=False)
        self.scenario['text'][MAIN_ID] = 'unknown pane content'
        self.save_scenario()
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.checkout / '.workbench/state/claude.json').exists())
        self.assertEqual({RIGHT_ID}, set(self.pins()))
        self.assertIn('leaving it unpinned', result.stdout)
        self.assertIn("github-workbench 'o/repo#7'", result.stdout)
        self.assertNotIn('start Claude there yourself', result.stdout)
        self.assertFalse(any(c[:2] == ['session', 'type'] and c[-1] == MAIN_ID for c in self.calls()))

    def test_tied_legacy_transcripts_leave_claude_unpinned(self):
        self.resumed(relay=False)
        self.scenario['text'][MAIN_ID] = 'Claude is working'
        self.save_scenario()
        self.legacy_transcript(MAIN_ID, 2000)
        self.legacy_transcript(OTHER_ID, 2000)
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.checkout / '.workbench/state/claude.json').exists())
        self.assertNotIn(MAIN_ID, self.pins())

    def test_existing_codex_shell_uses_resume_but_new_split_uses_fresh(self):
        first = self.flow(no_relay=True)
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        self.scenario = json.loads(self.scenario_path.read_text(encoding='utf-8'))
        self.scenario['text'][RIGHT_ID] = 'PS C:\\checkout> '
        self.save_scenario()
        second = self.flow(no_relay=True)
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        typed = [c[3] for c in self.calls() if c[:2] == ['session', 'type'] and c[-1] == RIGHT_ID]
        self.assertEqual(2, len(typed))
        self.assertNotIn('-Resume', typed[0])
        self.assertEqual(self.pins()[RIGHT_ID] + '\n', typed[1])
        self.assertIn('-Resume', typed[1])

    def test_failure_after_pin_preserves_callers_stage(self):
        self.resumed(relay=False)
        result = ps(self.setup_ps() + "$ok=Invoke-LaunchSafely { Set-LaunchStage known-pane; "
                    'Set-PaneRestore ' + ps_quote(MAIN_ID) + " 'test command'; throw 'after pin' }; "
                    'if (-not $ok) { exit 1 }', env=self.env)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("stage 'known-pane'", result.stdout)
        self.assertNotIn('Repair restart configuration', result.stdout)

    def test_checkout_lock_blocks_overlapping_launch_and_releases_after_failure(self):
        marker = self.temp / 'lock-held'
        release = self.temp / 'release-lock'
        command = (self.setup_ps() + 'Invoke-WithCheckoutLock ' + ps_quote(self.checkout) + ' { '
                   'Set-Content -LiteralPath ' + ps_quote(marker) + " -Value 'held'; "
                   '$deadline=(Get-Date).AddSeconds(20); while (-not (Test-Path -LiteralPath ' + ps_quote(release) +
                   ") -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 50 }; throw 'injected failure' }")
        holder = subprocess.Popen([PWSH, '-NoProfile', '-Command', command], env=self.env, cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and holder.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), 'lock holder failed to start')
            blocked = self.flow(no_relay=True)
            self.assertEqual(1, blocked.returncode, blocked.stdout + blocked.stderr)
            self.assertIn('Cannot acquire checkout launch lock', blocked.stdout)
            self.assertFalse((self.checkout / '.workbench/state/claude.json').exists())
            self.assertEqual([], self.calls())
        finally:
            release.touch()
            stdout, stderr = holder.communicate(timeout=10)
        self.assertNotEqual(0, holder.returncode, stdout + stderr)
        self.assertIn('injected failure', stderr)
        retry = self.flow(no_relay=True)
        self.assertEqual(0, retry.returncode, retry.stdout + retry.stderr)
        self.assertEqual(MAIN_ID, self.record()['pane'])
        self.assertEqual([], list((self.checkout / '.workbench/state').glob('*.tmp')))

    def record(self):
        return json.loads((self.checkout / '.workbench/state/claude.json').read_text(encoding='utf-8-sig'))

    def pins(self):
        return {c[-1]: c[2] for c in self.calls() if c[:2] == ['session', 'restore']}

    def test_reservation_precedes_create_and_all_pins_are_refreshed(self):
        first = self.flow()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        state = json.loads(self.scenario_path.read_text(encoding='utf-8'))
        reserved = state['created_claude_identities'][0]
        self.assertIsNone(reserved['pane'])
        record = self.record()
        self.assertEqual(reserved['sessionId'], record['sessionId'])
        self.assertEqual(MAIN_ID, record['pane'])
        self.assertEqual({MAIN_ID, RIGHT_ID, RELAY_ID}, set(self.pins()))
        self.assertIn('-Resume', self.pins()[RIGHT_ID])
        self.assertNotIn('-Resume', self.pins()[MAIN_ID])
        self.assertIn(RIGHT_ID, self.pins()[RELAY_ID])
        before_types = [c for c in self.calls() if c[:2] == ['session', 'type']]
        before_pins = sum(c[:2] == ['session', 'restore'] for c in self.calls())
        second = self.flow()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual(record, self.record())
        self.assertEqual(before_types, [c for c in self.calls() if c[:2] == ['session', 'type']])
        self.assertEqual(before_pins + 3, sum(c[:2] == ['session', 'restore'] for c in self.calls()))

    def retry_after_failure(self, pattern):
        self.scenario['responses'] = [{'args': pattern, 'exit': 1, 'stdout': 'interrupted', 'once': True}]
        self.save_scenario()
        first = self.flow()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        reserved = self.record()
        self.assertFalse(self.registry_path.exists())
        second = self.flow()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual(reserved['sessionId'], self.record()['sessionId'])
        self.assertEqual(MAIN_ID, self.record()['pane'])
        self.assertFalse(any(c[:2] == ['session', 'type'] and c[-1] == MAIN_ID for c in self.calls()))

    def test_failed_session_create_reuses_unbound_reservation(self):
        self.retry_after_failure(r'^session new --name #7 fix-x')

    def test_split_failure_preserves_bound_identity_before_registration(self):
        self.retry_after_failure(r'^session split on')

    def test_mailbox_failure_leaves_agent_pins(self):
        (self.temp / 'python.cmd').unlink()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("stage 'mailbox'", result.stdout)
        self.assertEqual({MAIN_ID, RIGHT_ID}, set(self.pins()))

    def test_pins_do_not_read_or_change_global_configuration(self):
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({MAIN_ID, RIGHT_ID}, set(self.pins()))
        self.assertFalse(any(c[0] == 'config' for c in self.calls()))

    def test_pin_failure_has_exact_repair_command_and_bound_identity(self):
        self.scenario['responses'] = [{'args': '^session restore', 'exit': 1, 'stdout': 'pin failed'}]
        self.save_scenario()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('Repair restart configuration: agwintermctl session restore', result.stdout)
        self.assertIn("--target '" + MAIN_ID + "'", result.stdout)
        self.assertEqual(MAIN_ID, self.record()['pane'])
        self.assertFalse(any(c[:2] == ['session', 'split'] for c in self.calls()))

    def test_replacement_claude_archives_identity_and_gets_a_new_one(self):
        self.surviving_codex()
        path = self.checkout / '.workbench/state/claude.json'
        path.write_text(json.dumps(dict(pane=MAIN_ID, sessionId=RELAY_ID, cwd=str(self.checkout), origin='fresh',
                                        reservedAt='2026-09-23', issue='o/repo#7', checkout=str(self.checkout))), encoding='utf-8')
        result = self.flow(no_relay=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(OTHER_ID, self.record()['pane'])
        self.assertNotEqual(RELAY_ID, self.record()['sessionId'])
        self.assertEqual(1, len(list(path.parent.glob('claude.*.json'))))
        self.assertIn(RELAY_ID, self.log())

    def test_existing_relay_pin_is_updated_before_failed_restart(self):
        self.resumed(panes=1)
        self.register(claude=MAIN_ID, codex=OTHER_ID)
        self.scenario['responses'] = [{'args': '(?s)^session type .*--target ' + RELAY_ID + '$', 'exit': 1}]
        self.save_scenario()
        result = self.flow()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(RIGHT_ID, self.pins()[RELAY_ID])
        self.assertNotIn(OTHER_ID, self.pins()[RELAY_ID])
        pin_index = next(i for i, c in enumerate(self.calls()) if c[:2] == ['session', 'restore'] and c[-1] == RELAY_ID)
        type_index = next(i for i, c in enumerate(self.calls()) if c[:2] == ['session', 'type'] and c[-1] == RELAY_ID)
        self.assertLess(pin_index, type_index)

    def test_bare_shell_repairs_keep_the_started_claude_identity(self):
        first = self.flow(no_relay=True)
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        record = self.record()
        transcript = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/project' / (record['sessionId'] + '.jsonl')
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'cwd': record['cwd']}), encoding='utf-8')
        self.scenario = json.loads(self.scenario_path.read_text(encoding='utf-8'))
        self.scenario['text'][MAIN_ID] = 'PS C:\\checkout> '
        self.save_scenario()
        second = self.flow(no_relay=True)
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual(record, self.record())
        typed = [c for c in self.calls() if c[:2] == ['session', 'type'] and c[-1] == MAIN_ID]
        self.assertEqual(1, len(typed))
        self.assertEqual(self.pins()[MAIN_ID] + '\n', typed[0][3])
        composed = ps('& ./lib/pane-claude.ps1 -Checkout ' + ps_quote(self.checkout) +
                      " -Issue 'o/repo#7' -WhatIfOnly", env=self.env)
        self.assertEqual(0, composed.returncode, composed.stdout + composed.stderr)
        self.assertIn("'--resume' '" + record['sessionId'] + "'", composed.stdout)


class QueueEntry(LauncherFixtures):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(LIB))
        import conductor
        self.queue_module = conductor
        self.queue_path = self.temp / 'queues/o/repo.json'
        self.store = conductor.Store(self.queue_path)
        self.store.directory.mkdir(parents=True)
        self.token = str(uuid.uuid4())
        member = conductor.new_member(7, 'o/repo', self.temp)
        member.update(state='launching', attempt=1, token=self.token)
        conductor.atomic_json(self.queue_path, dict(version=1, repo='o/repo', config=str(self.config_path),
                              parallel=1, watch=False, yes=False, label=None, owner=None, members=[member]))
        self.entry_lib = self.temp / 'queue entry'
        self.entry_lib.mkdir()
        for name in ['github-workbench.ps1', 'conductor.py', 'agw.py', 'hub.py', 'closer.py', 'limits.py']:
            shutil.copyfile(LIB / name, self.entry_lib / name)
        self.overrides = (
            "\nfunction Get-IssueInfo { return @{title='fix-x';state='OPEN'} }\n"
            "function New-IssueCheckout { param($Issue,$Title,$Root,$Directory); "
            "if (-not $Directory) {throw 'queue did not pass saved checkout'}; "
            "Connect-LaunchLog " + ps_quote(self.log_path) + "; return @{Dir=$Directory;Branch='issue-7-fix-x'} }\n"
            "function Grant-CodexTrust {}\nfunction Grant-ClaudeTrust {}\n")
        self.write_helpers()

    def write_helpers(self, extra=''):
        (self.entry_lib / 'Workbench.ps1').write_text((LIB / 'Workbench.ps1').read_text(encoding='utf-8-sig') +
                                                    self.overrides + extra, encoding='utf-8')

    def entry(self, *extra, shell=PWSH):
        return subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                               str(self.entry_lib / 'github-workbench.ps1'), 'o/repo#7',
                               '-QueueMember', str(self.queue_path), '-QueueAttempt', '1', '-QueueToken', self.token, *extra],
                              env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=45)

    def retry(self):
        self.token = str(uuid.uuid4())
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', token=self.token, result=None)

    def test_queue_bugs_reaches_the_conductor_unchanged(self):
        # #28: `bugs` is resolved by the conductor, so the launcher passes it through as a spec.
        args_file = self.temp / 'conductor-args.txt'
        self.cmd('python', f'echo %* > "{args_file}"')
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(self.entry_lib / 'github-workbench.ps1'),
                                 '-Queue', 'bugs', '-Repo', 'o/repo', '-Watch', '-Autonomous'],
                                env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=45)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        args = args_file.read_text(encoding='utf-8', errors='replace')
        self.assertIn('start --spec bugs --repo o/repo --watch', args)
        self.assertIn('--autonomous', args)

    def test_member_autonomous_switch_reaches_its_checkout(self):
        # #27: the conductor passes a queue's saved autonomy as -Autonomous / -NoAutonomous.
        result = self.entry('-Autonomous')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        checkout = Path(self.store.load()['members'][0]['checkout'])
        record = json.loads((checkout / '.workbench/state/implementer.json').read_text(encoding='utf-8-sig'))
        self.assertEqual((True, True), (record['autonomous'], record['autoMerge']))

    def test_member_auto_merge_switch_reaches_its_checkout(self):
        # #23: the conductor passes a queue's saved choice as -AutoMerge / -NoAutoMerge.
        result = self.entry('-AutoMerge')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        checkout = Path(self.store.load()['members'][0]['checkout'])
        record = json.loads((checkout / '.workbench/state/implementer.json').read_text(encoding='utf-8-sig'))
        self.assertIs(True, record['autoMerge'])

    def test_fresh_and_resume_preserve_focus_and_publish_result(self):
        first = self.entry()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        data = self.store.load()['members'][0]
        self.assertEqual('ok', data['result']['result'])
        self.assertEqual(MAIN_ID, data['result']['claudePane'])
        state = json.loads(self.scenario_path.read_text())
        self.assertEqual(str(self.queue_path), state['created_queue_memberships'][0]['queue'])
        before = (self.checkout / '.workbench/state/claude.json').read_bytes()
        self.retry()
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual(before, (self.checkout / '.workbench/state/claude.json').read_bytes())
        for call in self.calls():
            self.assertNotIn(call[:2], [['session', 'select'], ['session', 'focus']])
            if call[:2] == ['session', 'new']:
                self.assertIn('--no-select', call)
            if call[:2] == ['session', 'type']:
                self.assertNotIn('--select', call)
        self.assertEqual(2, sum(c[:2] == ['session', 'new'] for c in self.calls()))

    def test_incomplete_codex_retry_repairs_without_restarting_claude(self):
        self.write_helpers("\nfunction Wait-ShellPrompt { return $false }\n")
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertEqual('incomplete', self.store.load()['members'][0]['result']['result'])
        original = (self.checkout / '.workbench/state/claude.json').read_bytes()
        self.write_helpers()
        self.retry()
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual('ok', self.store.load()['members'][0]['result']['result'])
        self.assertEqual(original, (self.checkout / '.workbench/state/claude.json').read_bytes())
        typed = [c for c in self.calls() if c[:2] == ['session', 'type']]
        self.assertEqual([RIGHT_ID], [c[-1] for c in typed])
        self.assertIn('-Resume', typed[0][2])

    def test_failed_relay_retry_restores_it_with_same_conversation(self):
        self.scenario['responses'] = [{'args': '^session new --name #7 relay', 'exit': 1, 'once': True}]
        self.save_scenario()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertEqual('failed', self.store.load()['members'][0]['result']['result'])
        original = (self.checkout / '.workbench/state/claude.json').read_bytes()
        self.retry()
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual(original, (self.checkout / '.workbench/state/claude.json').read_bytes())
        self.assertEqual(1, sum(c[:2] == ['session', 'type'] for c in self.calls()))
        self.assertEqual(RELAY_ID, self.store.load()['members'][0]['result']['relaySession'])

    def test_non_queue_loop_is_refused_before_pin_or_typing(self):
        self.resumed()
        result = self.entry()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('outside the queue', result.stdout)
        self.assertFalse(any(c[:2] in [['session', 'restore'], ['session', 'type']] for c in self.calls()))
        self.assertFalse((self.checkout / '.workbench/state/queue-member.json').exists())

    def test_member_github_proxy_preserves_arguments_without_network(self):
        capture = self.temp / 'gh-args.json'
        stub = self.temp / 'fake-gh.py'
        stub.write_text('import json,sys\nfrom pathlib import Path\nPath(' + repr(str(capture)) +
                        ').write_text(json.dumps(sys.argv[1:]))\nprint(json.dumps(dict(title="fix-x",state="OPEN")))\n', encoding='utf-8')
        self.cmd('gh', '"' + sys.executable + '" "' + str(stub) + '" %*')
        self.overrides = self.overrides.replace("function Get-IssueInfo { return @{title='fix-x';state='OPEN'} }", '')
        self.write_helpers()
        result = self.entry()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(['issue', 'view', '7', '--repo', 'o/repo', '--json', 'number,title,url,state'],
                         json.loads(capture.read_text()))

    def configure_real_checkout(self):
        # Fake gh copies a local seed; the real launcher and Git HEAD checks run offline.
        def make_objects_writable():
            self.assertTrue(self.temp.resolve().is_relative_to(ROOT))
            for path in self.temp.rglob('*'):
                if path.is_file():
                    path.chmod(0o600)
        self.addCleanup(make_objects_writable)
        git = shutil.which('git')
        self.assertIsNotNone(git)
        seed = self.temp / 'seed'
        for args in (['init', '-b', 'issue-7-fix-x', str(seed)],
                     ['-C', str(seed), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', 'seed']):
            done = subprocess.run([git, *args], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.cmd('git', '"' + git + '" %*')
        capture, failure = self.temp / 'clone-args.jsonl', self.temp / 'fail-clone'
        stub = self.temp / 'fake-gh.py'
        stub.write_text(
            'import json,sys,shutil\nfrom pathlib import Path\n'
            'args=sys.argv[1:]\n'
            'with Path(' + repr(str(capture)) + ').open("a") as f: f.write(json.dumps(args)+"\\n")\n'
            'if args[:2] == ["issue", "view"]:\n'
            ' print(json.dumps(dict(title="fix-x",state="OPEN")))\n'
            'elif args[:2] == ["repo", "clone"]:\n'
            ' target=Path(args[3])\n'
            ' if Path(' + repr(str(failure)) + ').exists():\n'
            '  (target/".git/info").mkdir(parents=True,exist_ok=True)\n'
            '  (target/"partial").write_text("incomplete clone")\n'
            '  sys.exit(1)\n'
            ' assert args[4:] == ["--", "--quiet"], args\n'
            ' shutil.copytree(' + repr(str(seed)) + ',target,dirs_exist_ok=True)\n'
            'else: raise AssertionError(args)\n', encoding='utf-8')
        self.cmd('gh', '"' + sys.executable + '" "' + str(stub) + '" %*')
        self.overrides = '\nfunction Grant-CodexTrust {}\nfunction Grant-ClaudeTrust {}\n'
        self.write_helpers()
        return capture, failure

    def test_queue_real_clone_preserves_separator_and_reuses_usable_checkout(self):
        capture, _ = self.configure_real_checkout()
        self.checkout.rmdir()
        result = self.entry()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertIn(['repo', 'clone', 'o/repo', str(self.checkout), '--', '--quiet'], calls)
        self.assertTrue(self.store.load()['members'][0]['checkoutEstablished'])
        # A completed clone without a result checkpoint must survive retry as well.
        with self.store.transaction() as data:
            data['members'][0]['checkoutEstablished'] = False
        sentinel = self.checkout / 'keep-local-work'
        sentinel.write_text('keep')
        self.retry()
        result = self.entry()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('keep', sentinel.read_text())
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(1, sum(call[:2] == ['repo', 'clone'] for call in calls))

    def check_partial_clone_retry(self, shell):
        capture, failure = self.configure_real_checkout()
        failure.touch()
        first = self.entry(shell=shell)
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertTrue((self.checkout / 'partial').exists())
        self.assertFalse(self.store.load()['members'][0]['checkoutEstablished'])
        failure.unlink()
        self.retry()
        result = self.entry(shell=shell)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.checkout / 'partial').exists())
        self.assertTrue(self.store.load()['members'][0]['checkoutEstablished'])
        calls = [json.loads(line) for line in capture.read_text().splitlines()]
        self.assertEqual(2, sum(call[:2] == ['repo', 'clone'] for call in calls))

    def test_queue_retry_replaces_partial_clone(self):
        self.check_partial_clone_retry(PWSH)

    @unittest.skipUnless(WINDOWS_PS, 'Windows PowerShell not installed')
    def test_windows_powershell_queue_retry_replaces_partial_clone(self):
        self.check_partial_clone_retry(WINDOWS_PS)

    def test_unrecoverable_claude_identity_is_incomplete_not_active(self):
        first = self.entry()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        (self.checkout / '.workbench/state/claude.json').unlink()
        self.retry()
        result = self.entry()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        report = self.store.load()['members'][0]['result']
        self.assertEqual('incomplete', report['result'])
        self.assertIn('Claude identity is unavailable', report['detail'])

    def test_internal_membership_and_options_refused_before_mutation(self):
        self.token = str(uuid.uuid4())
        result = self.entry()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], self.calls())
        for args in (['-Queue', 'o/repo#1', '-Parallel', '0'], ['-Queue', 'o/repo#1', '-NoRelay'],
                     ['-Queue', 'o/repo#1', '-NewSession'], ['-Parallel', '2'], ['-Retry'],
                     ['-Queue', 'o/repo#1', '-Version'], ['o/repo#1', '-Queue', 'o/repo#2']):
            with self.subTest(args=args):
                run = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), *args],
                                     env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertEqual(2, run.returncode, run.stdout + run.stderr)
                self.assertEqual([], self.calls())

    @unittest.skipUnless(WINDOWS_PS, 'Windows PowerShell not installed')
    def test_windows_powershell_queue_entry(self):
        result = self.entry(shell=WINDOWS_PS)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('ok', self.store.load()['members'][0]['result']['result'])


class AdoptionEntry(LauncherFixtures):
    CALLER_WS = '55555555-5555-4555-8555-555555555555'
    REPO_WS = '66666666-6666-4666-8666-666666666666'

    def setUp(self):
        super().setUp()
        for key in list(self.env):
            if key.startswith('CODEX_') or key.startswith('CLAUDE'):
                self.env.pop(key)
        self.env.update(CLAUDECODE='1', AGWINTERM_PANE_ID=MAIN_ID, AGWINTERM_SESSION_ID=MAIN_ID)
        self.env['CLAUDE_CONFIG_DIR'] = str(self.temp / 'claude-home')
        self.env['CODEX_HOME'] = str(self.temp / 'codex-home')
        self.env['CLAUDE_CODE_SESSION_ID'] = '77777777-7777-4777-8777-777777777777'
        self.original_cwd = self.temp / 'original Claude project'
        self.original_cwd.mkdir()
        self.transcript_dir = self.temp / 'claude-home/projects/original'
        self.transcript_dir.mkdir(parents=True)
        (self.transcript_dir / (self.env['CLAUDE_CODE_SESSION_ID'] + '.jsonl')).write_text(
            json.dumps({'cwd': str(self.original_cwd)}), encoding='utf-8')
        self.scenario['workspace_id'] = self.REPO_WS
        self.scenario['tree'] = {'workspaces': [{'id': self.CALLER_WS, 'name': 'prepared',
                                                'sessions': [{'id': MAIN_ID, 'name': 'prepared Claude'}]}]}
        self.scenario['text'] = {MAIN_ID: 'PS C:\\looks-like-a-shell> '}
        self.save_scenario()
        self.effects = self.temp / 'effects.txt'
        self.fail_registration = self.temp / 'fail-registration'
        # Real entry point and setup logic; replace only checkout/trust and the interactive
        # Claude boundary. No network, user trust files, or actual agent can be reached.
        self.entry_lib = self.temp / 'entry lib'
        self.entry_lib.mkdir()
        for name in ['github-workbench.ps1', 'hub.py']:
            shutil.copyfile(LIB / name, self.entry_lib / name)
        overrides = (
            "\nfunction Get-IssueInfo { return @{ title='fix-x'; state='OPEN' } }\n"
            "function New-IssueCheckout { Add-Content -LiteralPath " + ps_quote(self.effects) + " -Value checkout; "
            "Connect-LaunchLog " + ps_quote(self.log_path) + "; return @{Dir=" + ps_quote(self.checkout) +
            "; Branch='issue-7-fix-x'} }\n"
            "function Grant-CodexTrust { Add-Content -LiteralPath " + ps_quote(self.effects) + " -Value codex-trust }\n"
            "function Grant-ClaudeTrust { Add-Content -LiteralPath " + ps_quote(self.effects) + " -Value claude-trust }\n"
            "$script:RealMailbox = ${function:Initialize-Mailbox}\n"
            "function Initialize-Mailbox { param($Checkout,$ClaudePane,$CodexPane,$CodexTool='codex'); "
            "if (Test-Path -LiteralPath " + ps_quote(self.fail_registration) + ") {throw 'registration failed'}; "
            "& $script:RealMailbox -Checkout $Checkout -ClaudePane $ClaudePane -CodexPane $CodexPane -CodexTool $CodexTool }\n"
            "function Invoke-ClaudeHere { param($Checkout,$Issue); "
            "Add-Content -LiteralPath " + ps_quote(self.effects) + " -Value claude-here; "
            "Write-Output \"CLAUDE-HERE: $Checkout $Issue\"; $global:LASTEXITCODE=0 }\n")
        (self.entry_lib / 'Workbench.ps1').write_text(
            (LIB / 'Workbench.ps1').read_text(encoding='utf-8-sig') + overrides, encoding='utf-8')

    def entry(self, *args, shell=PWSH):
        return subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                               str(self.entry_lib / 'github-workbench.ps1'), 'o/repo#7', *args],
                              env=self.env, cwd=ROOT, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=40)

    def current(self):
        return json.loads(self.scenario_path.read_text(encoding='utf-8'))

    def assert_caller_untouched(self):
        self.assertFalse(any(c[:2] in [['session', 'type'], ['session', 'text']] and c[-1] == MAIN_ID
                             for c in self.calls()), self.calls())

    def assert_refused_without_mutation(self, *args):
        before = {p: p.read_bytes() for p in self.checkout.rglob('*') if p.is_file()}
        result = self.entry(*args)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn('Adoption refused:', result.stdout)
        self.assertNotIn('Launcher stopped', result.stdout)
        self.assertFalse(self.effects.exists())
        self.assertTrue(all(c == ['tree', '--json'] for c in self.calls()), self.calls())
        self.assertEqual(before, {p: p.read_bytes() for p in self.checkout.rglob('*') if p.is_file()})
        return result

    def test_claude_adopts_once_and_rerun_never_probes_or_types_into_caller(self):
        first = self.entry()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        self.assertIn('WORKBENCH ADOPTED', first.stdout)
        self.assertIn(str(self.checkout), first.stdout)
        self.assertNotIn('CLAUDE-HERE:', first.stdout)
        self.assert_caller_untouched()
        creates = [c for c in self.calls() if c[:2] == ['session', 'new']]
        self.assertEqual(1, len(creates))
        self.assertEqual('#7 relay', creates[0][creates[0].index('--name') + 1])
        self.assertEqual('repo', creates[0][creates[0].index('--workspace-name') + 1])
        state = self.current()
        workspace = next(w for w in state['tree']['workspaces'] if w['id'] == self.REPO_WS)
        self.assertEqual(['#7 fix-x', '#7 relay'], [s['name'] for s in workspace['sessions']])
        registry = json.loads(self.registry_path.read_text(encoding='utf-8-sig'))
        self.assertEqual(MAIN_ID, registry['agents']['claude']['pane'])
        self.assertEqual(RIGHT_ID, registry['agents']['codex']['pane'])
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])
        self.assertEqual(1, sum(c[:3] == ['session', 'split', 'on'] for c in self.calls()))
        for pane in [MAIN_ID, RIGHT_ID, RELAY_ID]:
            self.assertEqual(2, sum(c[:2] == ['session', 'restore'] and c[-1] == pane for c in self.calls()))
        self.assert_caller_untouched()

    def test_shell_starts_claude_after_setup_with_stdout_intact(self):
        self.env.pop('CLAUDECODE')
        self.env['CODEX_HOME'] = 'configuration-only'
        result = self.entry()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('CLAUDE-HERE: ' + str(self.checkout) + ' o/repo#7', result.stdout)
        self.assertLess(result.stdout.index('WORKBENCH ADOPTED'), result.stdout.index('CLAUDE-HERE:'))
        self.assertEqual('claude-here', self.effects.read_text(encoding='utf-8-sig').splitlines()[-1])
        self.assert_caller_untouched()
        record = json.loads((self.checkout / '.workbench/state/claude.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(str(self.checkout), record['cwd'])
        self.assertEqual('fresh', record['origin'])

    def test_adopted_identity_preserves_original_cwd_then_tracks_new_runtime_id(self):
        first = self.entry('-NoRelay')
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        path = self.checkout / '.workbench/state/claude.json'
        original = json.loads(path.read_text(encoding='utf-8-sig'))
        # Re-serializing a PowerShell 7 DateTime can trim fractional zeros. A valid
        # unchanged identity should not be rewritten at all during a rerun.
        original['reservedAt'] = '2026-09-23T00:00:00.1234560Z'
        path.write_text(json.dumps(original), encoding='utf-8')
        unchanged_bytes = path.read_bytes()
        self.assertEqual(str(self.original_cwd), original['cwd'])
        self.assertEqual(self.env['CLAUDE_CODE_SESSION_ID'], original['sessionId'])
        result = subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                 str(self.entry_lib / 'github-workbench.ps1'), 'o/repo#7', '-NewSession', '-NoRelay'],
                                cwd=self.checkout, env=self.env, capture_output=True, text=True, timeout=40)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(original, json.loads(path.read_text(encoding='utf-8-sig')))
        self.assertEqual(unchanged_bytes, path.read_bytes())
        self.env['CLAUDE_CODE_SESSION_ID'] = OTHER_ID
        (self.transcript_dir / (OTHER_ID + '.jsonl')).write_text(json.dumps({'cwd': str(self.temp)}), encoding='utf-8')
        changed = self.entry('-NoRelay')
        self.assertEqual(0, changed.returncode, changed.stdout + changed.stderr)
        updated = json.loads(path.read_text(encoding='utf-8-sig'))
        self.assertEqual(OTHER_ID, updated['sessionId'])
        self.assertEqual(str(self.temp), updated['cwd'])

    def test_missing_adopted_transcript_refuses_before_any_mutation(self):
        self.env['CLAUDE_CODE_SESSION_ID'] = OTHER_ID
        result = self.assert_refused_without_mutation()
        self.assertIn('transcript or cwd missing', result.stdout)

    def test_shell_dry_run_has_no_mutations_or_claude_invocation(self):
        self.env.pop('CLAUDECODE')
        result = self.entry('-DryRun')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('would adopt-fresh', result.stdout)
        self.assertNotIn('CLAUDE-HERE:', result.stdout)
        self.assertEqual([['tree', '--json']], self.calls())
        self.assertFalse(self.effects.exists())
        self.assertEqual([], list(self.checkout.iterdir()))

    def test_foreign_split_is_refused_before_checkout_or_trust(self):
        self.scenario['tree']['workspaces'][0]['sessions'][0]['paneIds'] = [MAIN_ID, RIGHT_ID]
        self.save_scenario()
        self.assert_refused_without_mutation()

    def change_tree_during_checkout(self, tree):
        with (self.entry_lib / 'Workbench.ps1').open('a', encoding='utf-8') as stream:
            stream.write('\nfunction Grant-ClaudeTrust { $state = Get-Content -Raw -LiteralPath ' +
                         ps_quote(self.scenario_path) + ' | ConvertFrom-Json; $state.tree = ' + ps_json(tree) +
                         '; $state | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 -LiteralPath ' +
                         ps_quote(self.scenario_path) + ' }\n')

    def assert_recheck_refuses_before_terminal_mutation(self):
        result = self.entry()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn('Adoption refused:', result.stdout)
        self.assertTrue(all(c == ['tree', '--json'] for c in self.calls()), self.calls())
        self.assertFalse((self.checkout / '.workbench/state/adoption.json').exists())
        self.assertFalse(self.registry_path.exists())

    def test_pane_added_after_preflight_is_refused_before_rename_or_move(self):
        tree = self.scenario['tree']
        tree['workspaces'][0]['sessions'][0]['paneIds'] = [MAIN_ID, RIGHT_ID]
        self.change_tree_during_checkout(tree)
        self.assert_recheck_refuses_before_terminal_mutation()

    def test_changed_workspace_eligibility_is_refused_before_rename_or_move(self):
        tree = self.scenario['tree']
        tree['workspaces'].append({'name': 'repo', 'id': self.REPO_WS, 'sessions': []})
        self.change_tree_during_checkout(tree)
        self.assert_recheck_refuses_before_terminal_mutation()

    def test_other_issue_and_helper_callers_are_refused(self):
        for name in ['#8 another issue', '#7 relay', '#7 revmux r2', '#7 your review', '#7 unknown owner']:
            with self.subTest(name=name):
                self.scenario['tree']['workspaces'][0]['sessions'][0]['name'] = name
                self.save_scenario()
                self.assert_refused_without_mutation()

    def test_missing_caller_is_refused(self):
        self.env['AGWINTERM_PANE_ID'] = OTHER_ID
        self.assert_refused_without_mutation()

    def test_registered_codex_is_refused(self):
        self.register(claude=OTHER_ID, codex=MAIN_ID)
        self.assert_refused_without_mutation()

    def test_duplicate_repo_workspaces_are_refused_with_ids(self):
        self.scenario['tree']['workspaces'].extend([
            {'name': 'repo', 'id': self.REPO_WS, 'sessions': []},
            {'name': 'repo', 'id': OTHER_ID, 'sessions': []}])
        self.save_scenario()
        result = self.assert_refused_without_mutation()
        self.assertIn(self.REPO_WS, result.stdout)
        self.assertIn(OTHER_ID, result.stdout)

    def test_runtime_codex_markers_are_refused(self):
        self.env.pop('CLAUDECODE')
        for marker in ['CODEX_SANDBOX', 'CODEX_THREAD_ID']:
            with self.subTest(marker=marker):
                self.env[marker] = 'runtime'
                self.assert_refused_without_mutation()
                self.env.pop(marker)

    def test_claude_marker_wins_and_another_repo_same_number_does_not_conflict(self):
        self.env.update(CODEX_HOME='configuration', CODEX_SANDBOX='inherited')
        self.scenario['tree']['workspaces'].append(
            {'name': 'another-repo', 'id': OTHER_ID, 'sessions': [{'id': RIGHT_ID, 'name': '#7 elsewhere'}]})
        # Only the other repo owns RIGHT_ID; give our new split a different id.
        self.scenario['right_id'] = RELAY_ID
        self.save_scenario()
        result = self.entry('-NoRelay')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('CLAUDE-HERE:', result.stdout)
        self.assert_caller_untouched()

    def test_stale_registry_is_ignored(self):
        self.register(claude=OTHER_ID, codex=RIGHT_ID)
        result = self.entry()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('stale', self.log())
        self.assert_caller_untouched()

    def test_registered_own_session_resumes_without_adoption_record(self):
        self.scenario['tree'] = {'workspaces': [{'name': 'repo', 'id': self.REPO_WS, 'sessions': [
            {'id': MAIN_ID, 'name': '#7 fix-x', 'paneIds': [MAIN_ID, RIGHT_ID]}]}]}
        self.scenario['text'][RIGHT_ID] = 'Ask Codex to do anything\ngpt-test'
        self.save_scenario()
        self.register(claude=MAIN_ID, codex=RIGHT_ID)
        result = self.entry('-NoRelay')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(any(c[:2] in [['workspace', 'new'], ['session', 'move'], ['session', 'type']]
                             or c[:3] == ['session', 'split', 'on'] for c in self.calls()))
        self.assert_caller_untouched()

    def test_claude_implementer_pane_turning_shell_is_reserved_before_typing(self):
        # r15 m3: adoption withholds the pin from a running pane with no record; when that pane is a
        # shell by the time it is probed for launch, it gets a fresh identity before anything is typed.
        self.scenario['tree'] = {'workspaces': [{'name': 'repo', 'id': self.REPO_WS, 'sessions': [
            {'id': MAIN_ID, 'name': '#7 fix-x', 'paneIds': [MAIN_ID, RIGHT_ID]}]}]}
        self.scenario['text'][RIGHT_ID] = 'PS C:\\checkout> '
        self.scenario['responses'] = [{'args': '^session text --target ' + RIGHT_ID, 'stdout': 'esc to interrupt',
                                       'once': True}]
        self.save_scenario()
        self.register(claude=MAIN_ID, codex=RIGHT_ID)
        (self.checkout / '.workbench/state/implementer.json').write_text('{"tool": "claude"}', encoding='utf-8')
        result = self.entry('-NoRelay')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('has no recorded conversation', result.stdout)
        record = json.loads((self.checkout / '.workbench/state/implementer-claude.json').read_text(encoding='utf-8-sig'))
        self.assertEqual((RIGHT_ID, 'fresh'), (record['pane'], record['origin']))
        calls = self.calls()
        pin = next(i for i, c in enumerate(calls) if c[:2] == ['session', 'restore'] and c[-1] == RIGHT_ID)
        typed = next(i for i, c in enumerate(calls) if c[:2] == ['session', 'type'] and c[-1] == RIGHT_ID)
        self.assertLess(pin, typed)
        self.assertIn('pane-implementer-claude.ps1', calls[typed][-3])
        self.assert_caller_untouched()

    def recover_ctl_failure(self, pattern, stage):
        self.env.pop('CLAUDECODE')
        self.scenario['responses'] = [{'args': pattern, 'stdout': 'injected failure', 'exit': 1, 'once': True}]
        self.save_scenario()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertIn("stage '" + stage + "'", first.stdout)
        self.assertIn(MAIN_ID, first.stdout)
        self.assertNotIn('CLAUDE-HERE:', first.stdout)
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])
        self.assertEqual(1, self.effects.read_text(encoding='utf-8-sig').splitlines().count('claude-here'))
        self.assert_caller_untouched()

    def test_recovers_rename_failure(self):
        self.recover_ctl_failure(r'^session rename', 'adopt-rename')
        calls = self.calls()
        pin = next(i for i, c in enumerate(calls) if c[:2] == ['session', 'restore'] and c[-1] == MAIN_ID)
        rename = next(i for i, c in enumerate(calls) if c[:2] == ['session', 'rename'])
        self.assertLess(pin, rename)

    def test_recovers_workspace_creation_failure(self):
        self.recover_ctl_failure(r'^workspace new', 'adopt-workspace')

    def test_recovers_move_failure(self):
        self.recover_ctl_failure(r'^session move', 'adopt-move')

    def test_recovers_split_failure(self):
        self.recover_ctl_failure(r'^session split on', 'split')

    def test_recovers_codex_launch_failure(self):
        self.recover_ctl_failure(r'^session type', 'codex')

    def test_recovers_relay_failure_without_second_codex(self):
        self.recover_ctl_failure(r'^session new --name #7 relay', 'relay')

    def test_split_is_saved_before_mailbox_registration(self):
        self.fail_registration.touch()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        saved = json.loads((self.checkout / '.workbench/state/adoption.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(RIGHT_ID, saved['codexPane'])
        self.assertEqual('split', saved['stage'])
        self.fail_registration.unlink()
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])
        self.assertEqual(1, sum(c[:3] == ['session', 'split', 'on'] for c in self.calls()))
        self.assert_caller_untouched()

    def test_split_reply_is_saved_before_confirmation(self):
        self.scenario['fail_split_confirmation'] = True
        self.save_scenario()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertFalse(self.registry_path.exists())
        saved = json.loads((self.checkout / '.workbench/state/adoption.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(RIGHT_ID, saved['codexPane'])
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])
        self.assertEqual(1, sum(c[:3] == ['session', 'split', 'on'] for c in self.calls()))
        self.assert_caller_untouched()

    def test_split_intent_recovers_failure_after_reply_before_pane_save(self):
        failure = self.temp / 'fail-split-save'
        failure.touch()
        with (self.entry_lib / 'Workbench.ps1').open('a', encoding='utf-8') as stream:
            stream.write('\n$script:RealSaveAdoption = ${function:Save-AdoptionState}\n'
                         'function Save-AdoptionState($Checkout,$State) { '
                         "if ($State.stage -eq 'split' -and (Test-Path -LiteralPath " + ps_quote(failure) +
                         ")) {throw 'split reply received but save failed'}; "
                         '& $script:RealSaveAdoption $Checkout $State }\n')
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertIn('split reply received but save failed', first.stdout)
        path = self.checkout / '.workbench/state/adoption.json'
        saved = json.loads(path.read_text(encoding='utf-8-sig'))
        self.assertEqual({'session': MAIN_ID, 'claudePane': MAIN_ID, 'stage': 'splitting'}, saved)
        self.assertFalse(self.registry_path.exists())
        failure.unlink()
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        saved = json.loads(path.read_text(encoding='utf-8-sig'))
        self.assertEqual(RIGHT_ID, saved['codexPane'])
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])
        self.assertEqual(1, sum(c[:3] == ['session', 'split', 'on'] for c in self.calls()))
        self.assert_caller_untouched()

    def test_discovery_failure_retains_known_caller_for_repair(self):
        self.scenario['fail_move_discovery'] = True
        self.save_scenario()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertIn("stage 'discovery'", first.stdout)
        self.assertIn('SessionId: ' + MAIN_ID, first.stdout)
        self.assertIn('Claude: ' + MAIN_ID, first.stdout)
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])
        self.assert_caller_untouched()

    def test_new_codex_prompt_timeout_does_not_start_shell_claude(self):
        self.env.pop('CLAUDECODE')
        with (self.entry_lib / 'Workbench.ps1').open('a', encoding='utf-8') as stream:
            stream.write('\nfunction Wait-ShellPrompt { param($Pane,$TimeoutSeconds,[switch]$Adopted); '
                         "if ($TimeoutSeconds -ne 90 -or $Adopted) {throw 'incorrect fresh pane wait'}; return $false }\n")
        result = self.entry('-NoRelay')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('new Codex pane did not reach a shell prompt', result.stdout)
        self.assertNotIn('CLAUDE-HERE:', result.stdout)
        self.assertFalse(any(c[:2] == ['session', 'type'] for c in self.calls()))
        self.assert_caller_untouched()
        self.assertEqual({MAIN_ID, RIGHT_ID}, {c[-1] for c in self.calls() if c[:2] == ['session', 'restore']})

    def test_invalid_workspace_reply_does_not_invent_destination(self):
        self.scenario['responses'] = [{'args': r'^workspace new', 'stdout': 'created', 'once': True}]
        self.save_scenario()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        self.assertIn('invalid id', first.stdout)
        self.assertFalse(any(c[:2] == ['session', 'move'] for c in self.calls()))
        second = self.entry()
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertEqual([RIGHT_ID], self.current()['successful_types'])

    def test_saved_record_does_not_authorize_a_different_second_pane(self):
        self.fail_registration.touch()
        first = self.entry()
        self.assertEqual(1, first.returncode, first.stdout + first.stderr)
        state = self.current()
        next(s for w in state['tree']['workspaces'] for s in w['sessions'] if s['id'] == MAIN_ID)['paneIds'] = [MAIN_ID, OTHER_ID]
        self.scenario = state
        self.save_scenario()
        self.effects.unlink()
        self.calls_path.unlink()
        self.assert_refused_without_mutation()

    def test_new_session_uses_existing_creation_path(self):
        result = self.entry('-NewSession', '-NoRelay')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('WORKBENCH ADOPTED', result.stdout)
        self.assertNotIn('CLAUDE-HERE:', result.stdout)
        self.assertTrue(any(c[:2] == ['session', 'new'] for c in self.calls()))
        self.assertFalse(any(c[:2] in [['session', 'rename'], ['session', 'move'], ['workspace', 'new']]
                             for c in self.calls()))

    def test_outside_terminal_uses_existing_creation_path(self):
        self.env['AGWINTERM_ENABLED'] = '0'
        with (self.entry_lib / 'Workbench.ps1').open('a', encoding='utf-8') as stream:
            stream.write('\nfunction Test-AgwintermRunning { return $true }\n')
        result = self.entry('-NoRelay')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('WORKBENCH ADOPTED', result.stdout)
        self.assertTrue(any(c[:2] == ['session', 'new'] for c in self.calls()))
        self.assertFalse(any(c[:2] in [['session', 'rename'], ['session', 'move']] for c in self.calls()))

    @unittest.skipUnless(WINDOWS_PS, 'Windows PowerShell not installed')
    def test_windows_powershell_adoption(self):
        result = self.entry('-NoRelay', shell=WINDOWS_PS)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('WORKBENCH ADOPTED', result.stdout)
        self.assert_caller_untouched()

    def test_existing_issue_elsewhere_is_refused(self):
        self.scenario['tree']['workspaces'].append({'name': 'repo', 'id': self.REPO_WS,
                                                  'sessions': [{'id': OTHER_ID, 'name': '#7 fix-x'}]})
        self.save_scenario()
        result = self.assert_refused_without_mutation()
        self.assertIn(OTHER_ID, result.stdout)
        self.assertIn('-NewSession', result.stdout)


class AdoptionContext(LauncherFixtures):
    BASH = shutil.which('bash') or next((str(p) for p in [
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe',
        Path.home() / 'scoop/apps/git/current/bin/bash.exe'] if p.is_file()), None)

    @staticmethod
    def bash_quote(value):
        return "'" + str(value).replace("'", "'\\''") + "'"

    def test_mailbox_argv_preserves_apostrophe_paths_and_pane_ids(self):
        for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
            with self.subTest(shell=shell):
                checkout = self.temp / "it's a checkout" / Path(shell).stem
                script = ("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; Initialize-Mailbox -Checkout " +
                          ps_quote(checkout) + ' -ClaudePane ' + ps_quote(MAIN_ID) +
                          ' -CodexPane ' + ps_quote(RIGHT_ID))
                result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
                                        env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=20)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                registry = json.loads((checkout / '.workbench/state/agents.json').read_text(encoding='utf-8-sig'))
                for role, pane in [('claude', MAIN_ID), ('codex', RIGHT_ID)]:
                    self.assertEqual(str(checkout), registry['agents'][role]['cwd'])
                    self.assertEqual(pane, registry['agents'][role]['pane'])

    def test_launch_string_matches_original_format_with_explicit_fields(self):
        arguments = {'Checkout': str(self.temp / "it's a checkout"), 'Issue': 'o/repo#7'}
        script = ('. ./lib/Workbench.ps1; $a = @{}; $inputArgs = ' + ps_json(arguments) +
                  '; foreach ($p in $inputArgs.PSObject.Properties) {$a[$p.Name]=$p.Value}; '
                  "$l = Get-PaneLaunchArgs 'pane-codex.ps1' $a; "
                  "$expected = @($l.Exe,'-NoLogo','-ExecutionPolicy','Bypass','-File',"
                  "(Quote (Join-Path $script:Lib 'pane-codex.ps1'))); "
                  'foreach ($key in $a.Keys) {$expected += @("-$key",(Quote ([string]$a[$key])))}; '
                  "@(($expected -join ' '),(Get-PaneLaunch 'pane-codex.ps1' $a)) | ConvertTo-Json -Compress")
        result = ps(script, env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        expected, actual = json.loads(result.stdout)
        self.assertEqual(expected, actual)

    def test_context_quotes_and_encoding(self):
        checkout = self.temp / "it's a checkout"
        (checkout / '.workbench/state').mkdir(parents=True)
        workbench = self.temp / "it's the launcher"
        result = ps('. ./lib/Workbench.ps1; $script:Root = ' + ps_quote(workbench) +
                    '; Format-AdoptedBlock ' + ps_quote(checkout) + " 'o/repo#7'", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        path = checkout / '.workbench/state/adopted.sh'
        self.assertFalse(path.read_bytes().startswith(b'\xef\xbb\xbf'))
        lines = path.read_text(encoding='utf-8').splitlines()
        self.assertEqual(['cd', '--', checkout.as_posix()], shlex.split(lines[0])[:3])
        self.assertIn('|| return 1 2>/dev/null || exit 1', lines[0])
        self.assertEqual(['export', 'AGWORKBENCH=' + str(workbench),
                          'AI_HUB=' + str(checkout / '.workbench'), 'AI_BOX=claude'], shlex.split(lines[1]))
        context = next(line.removeprefix('context:  ') for line in result.stdout.splitlines()
                       if line.startswith('context:  '))
        self.assertEqual(['.', path.as_posix()], shlex.split(context))

    @unittest.skipUnless(BASH, 'Bash not installed')
    def test_context_round_trips_quotes_overrides_stale_env_and_fails_closed(self):
        checkout = self.temp / "it's a checkout"
        state = checkout / '.workbench/state'
        state.mkdir(parents=True)
        workbench = self.temp / "it's the launcher"
        result = ps('. ./lib/Workbench.ps1; $script:Root = ' + ps_quote(workbench) +
                    '; Format-AdoptedBlock ' + ps_quote(checkout) + " 'o/repo#7'", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        context = next(line.removeprefix('context:  ') for line in result.stdout.splitlines()
                       if line.startswith('context:  '))
        context_file = state / 'adopted.sh'
        self.assertFalse(context_file.read_bytes().startswith(b'\xef\xbb\xbf'))
        code = 'import json,os; print(json.dumps([os.getcwd(),os.environ["AGWORKBENCH"],os.environ["AI_HUB"],os.environ["AI_BOX"]]))'
        command = context + ' && ' + self.bash_quote(Path(sys.executable).as_posix()) + ' -c ' + self.bash_quote(code)
        env = dict(os.environ, AI_HUB='stale hub', AGWORKBENCH='old launcher', AI_BOX='codex')
        sourced = subprocess.run([self.BASH, '--noprofile', '--norc', '-c', command], env=env,
                                 capture_output=True, text=True, timeout=20)
        if sourced.returncode and 'CreateFileMapping' in sourced.stderr and 'Win32 error 5' in sourced.stderr:
            self.skipTest('sandbox denies Git Bash shared-memory initialization; run outside sandbox')
        self.assertEqual(0, sourced.returncode, sourced.stdout + sourced.stderr)
        cwd, root, hub, box = json.loads(sourced.stdout)
        self.assertEqual(checkout, Path(cwd))
        self.assertEqual([str(workbench), str(checkout / '.workbench'), 'claude'], [root, hub, box])
        # Keep the script accessible but make its guarded cd target unavailable.
        saved = self.temp / 'saved context.sh'
        shutil.copyfile(context_file, saved)
        moved = self.temp / 'moved checkout'
        self.assertTrue(checkout.resolve().is_relative_to(ROOT))
        self.assertTrue(moved.resolve().is_relative_to(ROOT))
        checkout.rename(moved)
        failed = subprocess.run([self.BASH, '--noprofile', '--norc', '-c',
                                 '. ' + self.bash_quote(saved.as_posix()) + ' && echo RAN'],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(0, failed.returncode)
        self.assertNotIn('RAN', failed.stdout)

    def test_structured_claude_invocation_preserves_arguments_and_stdout(self):
        lib = self.temp / "it's a script directory"
        lib.mkdir()
        (lib / 'pane-claude.ps1').write_text(
            'param($Checkout,$Issue)\n@($Checkout,$Issue) | ConvertTo-Json -Compress\n', encoding='utf-8')
        checkout = self.temp / "it's the checkout"
        # Suppress the user's profile in this subprocess while retaining the real argument builder.
        result = ps('. ./lib/Workbench.ps1; $realArgs = ${function:Get-PaneLaunchArgs}; '
                    'function Get-PaneLaunchArgs($Script,$Arguments) { $l = & $realArgs $Script $Arguments; '
                    "$l.Args = @('-NoProfile') + $l.Args; return $l }; $script:Lib = " + ps_quote(lib) +
                    '; Invoke-ClaudeHere -Checkout ' + ps_quote(checkout) + " -Issue 'o/repo#7'")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(result.stdout.lstrip().startswith('['), result.stdout + result.stderr)
        self.assertEqual([str(checkout), 'o/repo#7'], json.loads(result.stdout))


class LauncherScript(LauncherFixtures):
    def run_entry(self, *args):
        legacy = [] if '-Version' in args else ['-NewSession']
        return subprocess.run([PWSH, "-NoProfile", "-File", str(LIB / "github-workbench.ps1"), *args, *legacy],
                              env=self.env, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20)

    def test_early_failure_prints_buffer_without_creating_checkout_log(self):
        self.cmd("gh", "echo issue lookup failed\nexit /b 6")
        result = self.run_entry("o/repo#7")
        self.assertEqual(1, result.returncode)
        self.assertIn("issue lookup failed", result.stdout)
        self.assertIn("config starting", result.stdout)
        self.assertIn("resolve starting", result.stdout)
        self.assertFalse(self.log_path.exists())
        self.assertFalse(self.calls())

    def test_failed_dry_run_never_suggests_a_real_launch(self):
        self.cmd("gh", "echo lookup failed\nexit /b 6")
        result = self.run_entry("o/repo#7", "-DryRun")
        self.assertEqual(1, result.returncode)
        self.assertNotIn("incomplete", result.stdout)
        self.assertNotIn("github-workbench 'o/repo#7'", result.stdout)
        self.assertNotIn("Resume and complete", result.stdout)

    def test_repair_preserves_no_relay_before_session_setup(self):
        self.cmd("gh", "echo lookup failed\nexit /b 6")
        result = self.run_entry("o/repo#7", "-NoRelay")
        self.assertEqual(1, result.returncode)
        self.assertIn("github-workbench 'o/repo#7' -NoRelay", result.stdout)

    def test_failed_clone_does_not_poison_the_empty_checkout(self):
        self.cmd("gh", 'if "%1"=="issue" (\necho {"title":"fix-x","state":"OPEN"}\nexit /b 0\n)\necho clone failed\nexit /b 7')
        for _ in range(2):
            result = self.run_entry("o/repo#7")
            self.assertEqual(1, result.returncode)
            self.assertIn("gh repo clone failed", result.stdout)
            self.assertIn("config starting", result.stdout)
            self.assertEqual([], list(self.checkout.iterdir()))

    def test_body_wires_launch_commands_and_repair_boundary(self):
        # Replace only network/checkout/trust boundaries; run the real launcher body,
        # session logic, CLI transport, mailbox writes, and shared error handler.
        result = ps(self.setup_ps() +
                    "function Get-IssueInfo { return @{title='fix-x'; state='OPEN'} }; "
                    "function New-IssueCheckout { Connect-LaunchLog " + ps_quote(self.log_path) +
                    "; return @{Dir=" + ps_quote(self.checkout) + "; Branch='issue-7-fix-x'} }; "
                    "function Grant-CodexTrust {}; function Grant-ClaudeTrust {}; "
                    "$ok=Invoke-LaunchSafely { Invoke-LauncherBody -Issue 'o/repo#7' -NewSession }; "
                    "if (-not $ok) {exit 1}", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        creates = [c for c in self.calls() if c[:2] == ["session", "new"]]
        self.assertEqual(2, len(creates))
        relay = creates[1][creates[1].index("--command") + 1]
        self.assertIn("--repo 'o/repo' --branch 'issue-7-fix-x'", relay)
        self.assertIn("--hub '" + str(self.checkout / ".workbench") + "'", relay)
        self.assertIn("--claude-pane '" + MAIN_ID + "'", relay)
        self.assertIn("config starting", self.log())
        self.assertIn("trust starting", self.log())
        self.assertIn("ready starting", self.log())

    def test_failure_after_checkout_exists_writes_durable_log(self):
        self.cmd("gh", 'echo {"title":"fix-x","state":"OPEN"}\nexit /b 0')
        (self.checkout / ".git").mkdir()
        self.cmd("git", "echo git failed\nexit /b 5")
        result = self.run_entry("o/repo#7")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("git rev-parse failed", self.log())
        self.assertIn("config starting", self.log())
        self.assertIn("error ", self.log())
        self.assertFalse(self.calls())

    def test_dry_run_and_version_create_no_log(self):
        self.cmd("gh", 'echo {"title":"fix-x","state":"OPEN"}\nexit /b 0')
        result = self.run_entry("o/repo#7", "-DryRun")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("would open session", result.stdout)
        self.assertFalse(self.log_path.exists())
        self.assertFalse(self.calls())
        # Prevent the version smoke from querying the real terminal: its ctl is still a stub.
        result = self.run_entry("-Version")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(self.log_path.exists())


class ClaudeImplementer(LauncherFixtures):
    """#20: the right pane can run Claude Code as the implementer; the checkout keeps that choice."""

    def identity(self, role='implementer', **changes):
        record = dict(pane=RIGHT_ID, sessionId=OTHER_ID, cwd=str(self.checkout), origin='fresh',
                      reservedAt='2026-09-23T00:00:00Z', issue='o/repo#7', checkout=str(self.checkout))
        record.update(changes)
        name = 'implementer-claude.json' if role == 'implementer' else 'claude.json'
        path = self.checkout / '.workbench/state' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def state(self, name):
        return json.loads((self.checkout / '.workbench/state' / name).read_text(encoding='utf-8-sig'))

    def pins(self):
        return {c[-1]: c[2] for c in self.calls() if c[:2] == ['session', 'restore']}

    def body(self, implementer=None, config=None, extra=''):
        if config is not None:
            self.config_path.write_text(json.dumps(dict(config, checkoutRoot=str(self.temp))), encoding='utf-8')
        switch = f" -Implementer {implementer}" if implementer else ''
        return ps(self.setup_ps() +
                  "function Get-IssueInfo { return @{title='fix-x'; state='OPEN'} }; "
                  "function New-IssueCheckout { Connect-LaunchLog " + ps_quote(self.log_path) +
                  "; return @{Dir=" + ps_quote(self.checkout) + "; Branch='issue-7-fix-x'} }; "
                  "function Grant-CodexTrust {}; function Grant-ClaudeTrust {}; "
                  "$ok=Invoke-LaunchSafely { Invoke-LauncherBody -Issue 'o/repo#7' -NewSession" + switch + extra + " }; "
                  "if (-not $ok) { Write-Output \"EXIT=$($script:Launch.ExitCode)\"; exit 1 }", env=self.env)

    def relay_line(self):
        creates = [c for c in self.calls() if c[:2] == ['session', 'new']]
        return creates[1][creates[1].index('--command') + 1]

    def typed_right(self):
        return [c[3] for c in self.calls() if c[:2] == ['session', 'type'] and c[-1] == RIGHT_ID]

    def reuse_scenario(self, right_text):
        self.scenario = json.loads(self.scenario_path.read_text(encoding='utf-8'))
        self.scenario['text'][RIGHT_ID] = right_text
        self.scenario['text'][RELAY_ID] = 'PS C:\\relay> '
        self.save_scenario()

    # --- AC1/AC2: composition -------------------------------------------------------------------

    def test_default_launch_is_codex_with_an_unchanged_relay_line(self):
        result = self.body()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('--implementer-tool', self.relay_line())
        self.assertTrue(self.relay_line().endswith("--branch 'issue-7-fix-x'"))
        self.assertIn("pane-codex.ps1'", self.typed_right()[0])
        self.assertEqual('codex', self.state('agents.json')['agents']['codex']['tool'])
        self.assertEqual({'tool': 'codex', 'revmuxProfile': 'comprehensive', 'autoMerge': False, 'autonomous': False}, self.state('implementer.json'))
        self.assertFalse((self.checkout / '.workbench/state/implementer-claude.json').exists())

    def test_claude_composes_right_pane_relay_mailbox_identity_and_pin(self):
        result = self.body('claude')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        line = self.typed_right()[0].rstrip('\n')
        self.assertIn("pane-implementer-claude.ps1'", line)
        self.assertNotIn('pane-codex.ps1', line)
        self.assertEqual(line, self.pins()[RIGHT_ID])
        self.assertTrue(self.relay_line().endswith("--implementer-tool 'claude'"))
        agents = self.state('agents.json')['agents']
        self.assertEqual(('claude', RIGHT_ID), (agents['codex']['tool'], agents['codex']['pane']))
        self.assertEqual('claude', agents['claude']['tool'])
        self.assertEqual({'tool': 'claude', 'revmuxProfile': 'claude-only', 'autoMerge': False, 'autonomous': False}, self.state('implementer.json'))
        implementer = self.state('implementer-claude.json')
        planner = self.state('claude.json')
        self.assertEqual((RIGHT_ID, 'fresh', 'o/repo#7'), (implementer['pane'], implementer['origin'], implementer['issue']))
        self.assertEqual(MAIN_ID, planner['pane'])
        self.assertNotEqual(planner['sessionId'], implementer['sessionId'])
        self.assertIn('Claude implementer starting in the right pane', result.stdout)
        # The identity is bound before the pin can replay it, and the pin precedes typing.
        calls = self.calls()
        restore = next(i for i, c in enumerate(calls) if c[:2] == ['session', 'restore'] and c[-1] == RIGHT_ID)
        self.assertLess(restore, next(i for i, c in enumerate(calls) if c[:2] == ['session', 'type']))

    def test_config_selects_claude_and_revmux_profile_is_configurable(self):
        result = self.body(config={'implementer': 'claude', 'revmuxProfile': 'codex-final'})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({'tool': 'claude', 'revmuxProfile': 'codex-final', 'autoMerge': False, 'autonomous': False}, self.state('implementer.json'))
        self.assertTrue(self.relay_line().endswith("--implementer-tool 'claude'"))

    def test_invalid_config_and_switch_values_are_refused(self):
        bad = self.body(config={'implementer': 'aider'})
        self.assertEqual(1, bad.returncode)
        self.assertIn('implementer', bad.stdout)
        self.config_path.write_text(json.dumps({'checkoutRoot': str(self.temp)}), encoding='utf-8')
        switch = self.body('Claude')   # case matters: the relay and the registry use lowercase names
        self.assertEqual(1, switch.returncode)
        self.assertIn('EXIT=2', switch.stdout)
        self.assertFalse(any(c[:2] in (['session', 'new'], ['session', 'restore']) for c in self.calls()))

    # --- B1: the choice sticks to the checkout ------------------------------------------------------

    def test_relaunch_without_the_switch_keeps_claude(self):
        first = self.body('claude')
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        session = self.state('implementer-claude.json')['sessionId']
        self.reuse_scenario('esc to interrupt')   # Claude is mid-turn in the right pane
        second = self.body(config={})             # the config default is codex
        self.assertEqual(0, second.returncode, second.stdout + second.stderr)
        self.assertIn('pane-implementer-claude.ps1', self.pins()[RIGHT_ID])
        self.assertEqual('claude', self.state('agents.json')['agents']['codex']['tool'])
        self.assertEqual('claude', self.state('implementer.json')['tool'])
        self.assertEqual(session, self.state('implementer-claude.json')['sessionId'])
        self.assertEqual(1, len(self.typed_right()))

    def test_conflicting_switch_on_a_live_pane_is_refused_before_any_mutation(self):
        first = self.body('claude')
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        self.reuse_scenario('esc to interrupt')
        state = self.checkout / '.workbench/state'
        before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file() and p.suffix == '.json'}
        calls = len(self.calls())
        result = self.body('codex')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('EXIT=2', result.stdout)
        self.assertIn('Implementer switch refused', result.stdout)
        self.assertIn('runs claude', result.stdout)
        self.assertEqual(before, {p.name: p.read_bytes() for p in state.iterdir() if p.is_file() and p.suffix == '.json'})
        later = self.calls()[calls:]
        self.assertTrue(all(c[:2] in (['tree', '--json'], ['session', 'text']) for c in later), later)

    def test_pre_20_codex_checkout_is_not_switched_by_a_new_config_default(self):
        self.register(claude=MAIN_ID, codex=RIGHT_ID)   # an older loop: registry only, no implementer.json
        self.resumed(right_text='Ask Codex to do anything\ngpt-test', relay=False)
        result = self.body(config={'implementer': 'claude'})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('codex', self.state('implementer.json')['tool'])
        self.assertIn('pane-codex.ps1', self.pins()[RIGHT_ID])

    def test_switch_is_allowed_when_the_right_pane_is_a_shell(self):
        first = self.body('claude')
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        self.reuse_scenario('PS C:\\checkout> ')
        result = self.body('codex')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('-Resume', self.typed_right()[-1])
        self.assertIn('pane-codex.ps1', self.pins()[RIGHT_ID])
        self.assertEqual('codex', self.state('agents.json')['agents']['codex']['tool'])
        self.assertEqual({'tool': 'codex', 'revmuxProfile': 'comprehensive', 'autoMerge': False, 'autonomous': False}, self.state('implementer.json'))
        # B2: the relay was asked to stop and was restarted without the Claude profile.
        self.assertTrue(json.loads(self.scenario_path.read_text(encoding='utf-8'))['stop_seen'])
        relay = [c[3] for c in self.calls() if c[:2] == ['session', 'type'] and c[-1] == RELAY_ID]
        self.assertEqual(1, len(relay))
        self.assertNotIn('--implementer-tool', relay[0])

    def test_dry_run_reports_the_tool_and_writes_nothing(self):
        self.cmd('gh', 'echo {"title":"fix-x","state":"OPEN"}\nexit /b 0')
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7',
                                 '-NewSession', '-DryRun', '-Implementer', 'claude'],
                                env=self.env, cwd=ROOT, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('implementer: claude (revmux profile claude-only)', result.stdout)
        self.assertIn('pane-implementer-claude.ps1', result.stdout)
        self.assertIn('--implementer-tool claude', result.stdout)
        self.assertFalse((self.checkout / '.workbench').exists())
        self.assertFalse(self.calls())

    def test_entry_refuses_an_unknown_implementer(self):
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7',
                                 '-Implementer', 'aider'], env=self.env, cwd=ROOT, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn('-Implementer must be codex or claude', result.stdout)
        self.assertFalse(self.calls())

    # --- B2: relay restart on a tool change, same panes ---------------------------------------------

    def test_tool_change_on_the_same_panes_restarts_the_relay(self):
        self.resumed(right_text='PS C:\\checkout> ')
        self.scenario['text'][RELAY_ID] = 'PS C:\\relay> '
        self.save_scenario()
        self.register(claude=MAIN_ID, codex=RIGHT_ID)
        result = self.flow(implementer='claude')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(json.loads(self.scenario_path.read_text(encoding='utf-8'))['stop_seen'])
        self.assertEqual('claude', self.state('agents.json')['agents']['codex']['tool'])
        self.assertEqual(RIGHT_ID, self.state('implementer-claude.json')['pane'])

    # --- B3 / AC3: identities of two Claudes in one checkout ----------------------------------------

    def legacy_transcript(self, session_id, mtime):
        encoded = re.sub('[^a-zA-Z0-9]', '-', str(self.checkout))
        path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects' / encoded / (session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n' + json.dumps(dict(entrypoint='cli', cwd=str(self.checkout))) + '\n', encoding='utf-8')
        os.utime(path, (mtime, mtime))

    def running_planner_without_record(self):
        self.resumed(right_text='esc to interrupt', relay=False)
        self.scenario['text'][MAIN_ID] = 'Claude is working'
        self.save_scenario()
        self.register(claude=MAIN_ID, codex=RIGHT_ID)
        (self.checkout / '.workbench/state/implementer.json').write_text('{"tool": "claude"}', encoding='utf-8')

    def test_planner_recovery_never_takes_the_implementers_conversation(self):
        self.running_planner_without_record()
        self.identity(sessionId=RELAY_ID)                  # the implementer's own, newest transcript
        self.legacy_transcript(OTHER_ID, 1000)
        self.legacy_transcript(RELAY_ID, 5000)
        result = self.flow(no_relay=True, implementer='claude')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(OTHER_ID, self.state('claude.json')['sessionId'])
        self.assertEqual('recovered', self.state('claude.json')['origin'])
        self.assertEqual(RELAY_ID, self.state('implementer-claude.json')['sessionId'])

    def test_planner_recovery_is_refused_when_the_implementer_record_is_missing(self):
        self.running_planner_without_record()
        self.legacy_transcript(OTHER_ID, 1000)
        result = self.flow(no_relay=True, implementer='claude')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.checkout / '.workbench/state/claude.json').exists())
        self.assertIn('cannot be told apart', result.stdout)
        self.assertNotIn(MAIN_ID, self.pins())

    def test_running_implementer_without_record_is_left_unpinned_not_recovered(self):
        self.running_planner_without_record()
        self.identity(role='planner', pane=MAIN_ID)
        self.legacy_transcript(RELAY_ID, 5000)
        result = self.flow(no_relay=True, implementer='claude')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse((self.checkout / '.workbench/state/implementer-claude.json').exists())
        self.assertIn('Claude implementer pane', result.stdout)
        self.assertNotIn('start Claude implementer there yourself', result.stdout)   # r15 m3
        self.assertNotIn(RIGHT_ID, self.pins())
        self.assertFalse(any(c[:2] == ['session', 'type'] and c[-1] == RIGHT_ID for c in self.calls()))

    def test_implementer_record_moved_to_another_pane_is_archived(self):
        self.identity(pane=OTHER_ID)
        result = ps(self.setup_ps() + "$r = Reserve-ClaudeIdentity " + ps_quote(self.checkout) +
                    " 'o/repo#7' " + ps_quote(RIGHT_ID) + " -Role implementer; $r.pane", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(RIGHT_ID, result.stdout.strip().splitlines()[-1])
        archived = list((self.checkout / '.workbench/state').glob('implementer-claude.*.json'))
        self.assertEqual(1, len(archived))
        self.assertFalse((self.checkout / '.workbench/state/claude.json').exists())

    # --- AC3 / B5: the pane script --------------------------------------------------------------

    def pane(self, config=None):
        if config is not None:
            self.config_path.write_text(json.dumps(config), encoding='utf-8')
        return ps('& ./lib/pane-implementer-claude.ps1 -Checkout ' + ps_quote(self.checkout) +
                  " -Issue 'o/repo#7' -WhatIfOnly", env=self.env)

    def transcript(self, session_id=OTHER_ID):
        path = Path(self.env['CLAUDE_CONFIG_DIR']) / 'projects/project' / (session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'cwd': str(self.checkout)}), encoding='utf-8')

    def test_pane_fresh_then_resume_with_its_own_identity(self):
        path = self.identity()
        before = path.read_bytes()
        fresh = self.pane()
        self.assertEqual(0, fresh.returncode, fresh.stdout + fresh.stderr)
        self.assertIn("'--session-id' '" + OTHER_ID + "' '/workbench-implementer o/repo#7'", fresh.stdout)
        self.transcript()
        resumed = self.pane()
        self.assertEqual(0, resumed.returncode, resumed.stdout + resumed.stderr)
        self.assertIn("'--resume' '" + OTHER_ID + "'", resumed.stdout)
        self.assertIn('IMPLEMENTER', resumed.stdout)
        self.assertIn('wait-mail --box codex', resumed.stdout)
        self.assertNotIn('/workbench-implementer o/repo#7', resumed.stdout)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.registry_path.exists())

    def test_pane_uses_the_implementer_record_not_the_planners(self):
        self.identity(role='planner', sessionId=MAIN_ID, pane=MAIN_ID)
        missing = self.pane()
        self.assertEqual(1, missing.returncode)
        self.assertIn('Cannot start the Claude implementer', missing.stdout)
        self.assertNotIn('would run:', missing.stdout)

    def test_pane_always_denies_push_gh_and_web_first(self):
        self.identity()
        line = self.pane({'claudeArgs': ['--dangerously-skip-permissions']}).stdout
        run = line[line.index('would run: claude '):]
        self.assertTrue(run.startswith("would run: claude '--disallowedTools' 'Bash(git push:*)' 'Bash(gh:*)' "
                                       "'PowerShell(git push:*)' 'PowerShell(gh:*)' "
                                       "'WebFetch' 'WebSearch' '--dangerously-skip-permissions' '--settings'"), run)
        opened = self.pane({'allowNetwork': True}).stdout
        self.assertIn("'PowerShell(gh:*)' '--settings'", opened)
        self.assertNotIn('WebFetch', opened)

    def test_pane_refuses_policy_and_identity_arguments(self):
        self.identity()
        for flag in ['--add-dir', '--add-dir=C:/', '--permission-mode', '--permission-mode=bypassPermissions',
                     '--allowedTools', '--allowed-tools=Bash', '--disallowedTools', '--disallowed-tools=x',
                     '--settings', '--settings=x.json', '--session-id', '--resume', '-r', '-c', '--continue',
                     '--fork-session']:
            with self.subTest(flag=flag):
                result = self.pane({'claudeArgs': [flag]})
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('would run:', result.stdout)
                self.assertRegex(result.stderr, 'tool policy|conversation identity')

    def test_pane_invocation_sets_the_codex_box_and_the_clone(self):
        self.identity()
        self.registry_path.write_text('{}', encoding='utf-8')
        command = ('function claude { param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AgentArgs); '
                   '[pscustomobject]@{Args=$AgentArgs; Cwd=(Get-Location).Path; Hub=$env:AI_HUB; '
                   'Box=$env:AI_BOX; Root=$env:AGWORKBENCH} | ConvertTo-Json -Compress }; '
                   '& ./lib/pane-implementer-claude.ps1 -Checkout ' + ps_quote(self.checkout) + " -Issue 'o/repo#7'")
        result = ps(command, env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        capture = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(('codex', str(self.checkout / '.workbench'), str(self.checkout), str(ROOT)),
                         (capture['Box'], capture['Hub'], capture['Cwd'], capture['Root']))
        self.assertEqual(['--disallowedTools', 'Bash(git push:*)', 'Bash(gh:*)', 'PowerShell(git push:*)',
                          'PowerShell(gh:*)', 'WebFetch', 'WebSearch',
                          '--settings', str(self.checkout / '.workbench/state/claude-settings.json'),
                          '--session-id', OTHER_ID, '/workbench-implementer o/repo#7'], capture['Args'])

    def test_repair_text_offers_no_launch_for_an_unidentified_implementer(self):
        base = ("$l=@{Stage='relay'; MailboxReady=$true; CodexLaunch='THE-LINE'; ImplementerTool='claude'; "
                "IssueRef='o/repo#7'; ImplementerIdentityReady=")
        for ready, offered in (('$false', False), ('$true', True)):
            with self.subTest(ready=ready):
                result = ps('. ./lib/Workbench.ps1; ' + base + ready + '}; Format-RepairMessage $l', env=self.env)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(offered, 'In the Claude implementer pane, once it is at an empty shell prompt: THE-LINE'
                                 in result.stdout)

    def test_launched_claude_has_prompt_suggestions_off_through_a_native_argv(self):
        # #33 B1: a PowerShell function never goes through native quoting; a .cmd running python does.
        # The settings are a FILE path, so 5.1 cannot strip quotes out of an inline JSON string.
        self.identity()
        self.registry_path.write_text('{}', encoding='utf-8')
        dump = self.temp / 'claude-argv.json'
        (self.temp / 'claude_dump.py').write_text(
            'import json, os, sys\n'
            f'json.dump({{"args": sys.argv[1:], "env": os.environ.get("CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION")}}, '
            f'open(r"{dump}", "w", encoding="utf-8"))\n', encoding='utf-8')
        self.cmd('claude', f'"{sys.executable}" "{self.temp / "claude_dump.py"}" %*')
        settings = self.checkout / '.workbench/state/claude-settings.json'
        for script in ('pane-implementer-claude.ps1', 'pane-claude.ps1'):
            for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
                with self.subTest(script=script, shell=shell):
                    if script == 'pane-claude.ps1':
                        self.identity(role='planner', pane=MAIN_ID, sessionId=RELAY_ID)
                    dump.unlink(missing_ok=True)
                    result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(LIB / script),
                                             '-Checkout', str(self.checkout), '-Issue', 'o/repo#7'],
                                            env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=60)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    seen = json.loads(dump.read_text(encoding='utf-8'))
                    args = seen['args']
                    self.assertEqual(str(settings), args[args.index('--settings') + 1])
                    self.assertEqual('false', seen['env'])
                    self.assertEqual({'promptSuggestionEnabled': False}, json.loads(settings.read_text(encoding='utf-8')))

    def test_settings_in_claude_args_are_refused_for_the_planner_too(self):
        self.identity(role='planner', pane=MAIN_ID)
        for flag in ('--settings', '--settings=x.json'):
            with self.subTest(flag=flag):
                self.config_path.write_text(json.dumps({'claudeArgs': [flag]}), encoding='utf-8')
                result = ps('& ./lib/pane-claude.ps1 -Checkout ' + ps_quote(self.checkout) + " -Issue 'o/repo#7' -WhatIfOnly",
                            env=self.env)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("replace the workbench's own --settings", result.stderr)

    def test_codex_is_not_required(self):
        self.assertIsNone(shutil.which('codex', path=self.env['PATH']))
        result = self.body('claude')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    # --- adoption and install -------------------------------------------------------------------

    def test_the_claude_implementer_cannot_adopt_its_own_pane(self):
        self.register(claude=OTHER_ID, codex=MAIN_ID)
        registry = json.loads(self.registry_path.read_text(encoding='utf-8'))
        registry['agents']['codex']['tool'] = 'claude'
        self.registry_path.write_text(json.dumps(registry), encoding='utf-8')
        self.scenario['tree'] = {'workspaces': [{'id': '55555555-5555-4555-8555-555555555555', 'name': 'repo',
                                                'sessions': [{'id': OTHER_ID, 'name': '#7 fix-x',
                                                              'paneIds': [OTHER_ID, MAIN_ID]}]}]}
        self.save_scenario()
        env = dict(self.env, CLAUDECODE='1', AGWINTERM_PANE_ID=MAIN_ID, AGWINTERM_SESSION_ID=OTHER_ID)
        result = ps(". ./lib/Workbench.ps1; try { $null = Get-AdoptionPlan (Get-Tree) " + ps_quote(self.checkout) +
                    " 'repo' 7; 'adopted' } catch [AdoptRefused] { $_.Exception.Message }", env=env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('registered as Codex', result.stdout)

    def test_installer_ships_the_implementer_command(self):
        command = (ROOT / 'claude/commands/workbench-implementer.md').read_text(encoding='utf-8')
        for needle in ['AGREED: plan vK', 'IMPLEMENT plan vK', 'IMPLEMENTED <short sha>', 'FIXED <short sha>',
                       'wait-mail --box codex', 'Never push', 'Commit your own work', '--body-file']:
            self.assertIn(needle, command)
        self.assertIn('claude\\commands\\*.md', (ROOT / 'install.ps1').read_text(encoding='utf-8'))
        planner = (ROOT / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        self.assertIn("Never commit the implementer's uncommitted work yourself", planner)


class AutoMergeLaunch(LauncherFixtures):
    """#23: auto-merge is stored in the checkout's settings record; it is policy, not a process."""
    body = ClaudeImplementer.body
    state = ClaudeImplementer.state
    relay_line = ClaudeImplementer.relay_line
    typed_right = ClaudeImplementer.typed_right
    reuse_scenario = ClaudeImplementer.reuse_scenario

    def test_default_is_off_and_launch_strings_are_unchanged(self):
        result = self.body()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIs(False, self.state('implementer.json')['autoMerge'])
        self.assertTrue(self.relay_line().endswith("--branch 'issue-7-fix-x'"))
        self.assertNotIn('AutoMerge', self.relay_line() + self.typed_right()[0])
        self.assertIn('auto-merge off', result.stdout)

    def test_switch_turns_it_on_and_a_rerun_without_it_keeps_it(self):
        on = self.body(extra=' -AutoMerge $true')
        self.assertEqual(0, on.returncode, on.stdout + on.stderr)
        self.assertIs(True, self.state('implementer.json')['autoMerge'])
        self.assertIn('auto-merge on', on.stdout)
        self.reuse_scenario('esc to interrupt')
        again = self.body()
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertIs(True, self.state('implementer.json')['autoMerge'])

    def test_off_applies_even_while_the_implementer_is_running(self):
        self.assertEqual(0, self.body(extra=' -AutoMerge $true').returncode)
        self.reuse_scenario('esc to interrupt')      # a live agent does not block a policy change
        off = self.body(extra=' -AutoMerge $false')
        self.assertEqual(0, off.returncode, off.stdout + off.stderr)
        self.assertIs(False, self.state('implementer.json')['autoMerge'])
        self.assertEqual('codex', self.state('implementer.json')['tool'])

    def test_config_default_applies_to_new_checkouts_only(self):
        on = self.body(config={'autoMerge': True})
        self.assertEqual(0, on.returncode, on.stdout + on.stderr)
        self.assertIs(True, self.state('implementer.json')['autoMerge'])
        self.reuse_scenario('esc to interrupt')
        kept = self.body(config={'autoMerge': False})
        self.assertEqual(0, kept.returncode, kept.stdout + kept.stderr)
        self.assertIs(True, self.state('implementer.json')['autoMerge'])

    def test_a_pre_23_record_reads_as_the_config_default(self):
        state = self.checkout / '.workbench/state'
        state.mkdir(parents=True)
        (state / 'implementer.json').write_text('{"tool": "codex", "revmuxProfile": "comprehensive"}', encoding='utf-8')
        result = ps(". ./lib/Workbench.ps1; $r = Resolve-Implementer -Checkout " + ps_quote(self.checkout) +
                    " -Config (Get-WorkbenchConfig) -NoProbe; $r.AutoMerge", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('False', result.stdout.strip())

    def test_non_boolean_config_is_refused(self):
        for value in ('true', 1, None):
            with self.subTest(value=value):
                self.config_path.write_text(json.dumps({'checkoutRoot': str(self.temp), 'autoMerge': value}),
                                            encoding='utf-8')
                result = ps('. ./lib/Workbench.ps1; Get-WorkbenchConfig | Out-Null; "loaded"', env=self.env)
                if value is None:     # null is "not set": the default applies
                    self.assertIn('loaded', result.stdout, result.stderr)
                else:
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn('autoMerge', result.stderr + result.stdout)

    def entry(self, *args):
        return subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7', *args],
                              env=self.env, cwd=ROOT, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=20)

    def test_entry_refuses_both_switches(self):
        result = self.entry('-AutoMerge', '-NoAutoMerge')
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn('-AutoMerge and -NoAutoMerge cannot be combined', result.stdout)
        self.assertFalse(self.calls())

    def test_dry_run_prints_auto_merge_and_writes_nothing(self):
        self.cmd('gh', 'echo {"title":"fix-x","state":"OPEN"}\nexit /b 0')
        result = self.entry('-NewSession', '-DryRun', '-AutoMerge')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('auto-merge on', result.stdout)
        self.assertFalse((self.checkout / '.workbench').exists())
        self.assertFalse(self.calls())


LIMIT_FRAMES = ROOT / 'tests' / 'fixtures' / 'limits'


class FailoverLaunch(LauncherFixtures):
    """#24: -Failover stops a limited implementer only when provably idle at its limit (or takes an
    exited one), records the limit, clears the pane and switches through the #20 path. No test stops
    a real process: the process table and the stop are replaced at their boundary."""
    body = ClaudeImplementer.body
    state = ClaudeImplementer.state
    relay_line = ClaudeImplementer.relay_line

    CODEX_PID, NODE_PID, PANE_PID = 4242, 4241, 4240

    def setUp(self):
        super().setUp()
        self.stopped = self.temp / 'stopped.txt'
        first = self.body()                      # a running Codex loop: left Claude, right Codex, relay
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)

    def frame(self, name):
        return (LIMIT_FRAMES / f'{name}.txt').read_text(encoding='utf-8')

    def right(self, text):
        self.scenario = json.loads(self.scenario_path.read_text(encoding='utf-8'))
        self.scenario['text'][RIGHT_ID] = text
        self.scenario['text'][RELAY_ID] = 'PS C:\\relay> '
        self.save_scenario()

    def relay_record(self, text, age=120):
        sys.path.insert(0, str(LIB))
        import limits
        path = self.checkout / '.workbench/state/relay.json'
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        data['limits'] = {'codex': {'kind': 'limited', 'tool': 'codex', 'tail': limits.tail_hash(text),
                                    'since': time.time() - age, 'announced': True}}
        path.write_text(json.dumps(data), encoding='utf-8')

    def processes(self, *extra):
        hub = str(self.checkout / '.workbench')
        table = [dict(ProcessId=self.PANE_PID, ParentProcessId=1, Name='pwsh.exe',
                      CommandLine=f"pwsh -File pane-codex.ps1 -Checkout '{self.checkout}'"),
                 dict(ProcessId=self.NODE_PID, ParentProcessId=self.PANE_PID, Name='node.exe',
                      CommandLine=f"node codex.js -c shell_environment_policy.set.AI_HUB='{hub}'"),
                 dict(ProcessId=self.CODEX_PID, ParentProcessId=self.NODE_PID, Name='codex.exe',
                      CommandLine=f"codex.exe -c shell_environment_policy.set.AI_HUB='{hub}' --sandbox workspace-write")]
        return table + list(extra)

    def failover(self, processes=None, stop_to_shell=True, timing="Stable=90; Confirm=0; Sample=0.3; Step=0.1; ShellWait=2",
                 lock_after_stop=False, config=None, after_stop='PS C:\\checkout> ', survives=False):
        table = json.dumps(self.processes() if processes is None else processes)
        shell_write = ''
        if stop_to_shell:
            (self.temp / 'after-stop.txt').write_text(after_stop, encoding='utf-8')
            shell_write = ("$s = Get-Content -Raw " + ps_quote(self.scenario_path) + " | ConvertFrom-Json; "
                           "$s.text.'" + RIGHT_ID + "' = [IO.File]::ReadAllText(" + ps_quote(self.temp / 'after-stop.txt') +
                           ", [Text.Encoding]::UTF8); "
                           "$s | ConvertTo-Json -Depth 20 | Set-Content -Encoding UTF8 " + ps_quote(self.scenario_path) + "; ")
        lock = ''
        if lock_after_stop:
            lock = "New-Item -ItemType File -Force " + ps_quote(self.checkout / '.git/index.lock') + " | Out-Null; "
        # A stopped process leaves the table, unless the test says it survives the stop.
        gone = '' if survives else (" | Where-Object { $stopped = @(); if (Test-Path " + ps_quote(self.stopped) +
                                    ") { $stopped = @(Get-Content " + ps_quote(self.stopped) + ") }; "
                                    "$stopped -notcontains [string]$_.ProcessId }")
        overrides = ("function Get-AgentProcesses { " + ps_quote(table) + " | ConvertFrom-Json" + gone + " }; "
                     "function Stop-AgentTree([int] $ProcessId) { Add-Content " + ps_quote(self.stopped) +
                     " $ProcessId; " + shell_write + lock + "}; "
                     "$script:FailoverTiming = @{ " + timing + " }; ")
        if config is not None:
            self.config_path.write_text(json.dumps(dict(config, checkoutRoot=str(self.temp))), encoding='utf-8')
        return ps(self.setup_ps() + overrides +
                  "function Get-IssueInfo { return @{title='fix-x'; state='OPEN'} }; "
                  "function New-IssueCheckout { Connect-LaunchLog " + ps_quote(self.log_path) +
                  "; return @{Dir=" + ps_quote(self.checkout) + "; Branch='issue-7-fix-x'} }; "
                  "function Grant-CodexTrust {}; function Grant-ClaudeTrust {}; "
                  "$ok=Invoke-LaunchSafely { Invoke-LauncherBody -Issue 'o/repo#7' -NewSession -Failover }; "
                  "if (-not $ok) { Write-Output \"EXIT=$($script:Launch.ExitCode)\"; exit 1 }", env=self.env)

    def typed_right(self):
        # The launch lines are typed with --select; the failover's Clear-Host is not.
        return [c[3] if c[2] == '--select' else c[2] for c in self.calls()
                if c[:2] == ['session', 'type'] and c[-1] == RIGHT_ID]

    def stopped_pids(self):
        return [int(x) for x in self.stopped.read_text(encoding='utf-8-sig').split()] if self.stopped.exists() else []

    def assert_switched_to_claude(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        record = self.state('implementer.json')
        self.assertEqual('claude', record['tool'])
        self.assertIn('codex', record['limits'])
        self.assertIn('hit your usage limit', record['limits']['codex']['line'])
        typed = self.typed_right()
        self.assertEqual('Clear-Host\n', typed[-2])
        self.assertIn('pane-implementer-claude.ps1', typed[-1])
        self.assertEqual(RIGHT_ID, self.state('implementer-claude.json')['pane'])
        self.assertEqual('claude', self.state('agents.json')['agents']['codex']['tool'])
        relay = [c[3] for c in self.calls() if c[:2] == ['session', 'type'] and c[-1] == RELAY_ID]
        self.assertTrue(relay and relay[-1].rstrip().endswith("--implementer-tool 'claude'"), relay)
        self.assertFalse(any(c[:2] == ['session', 'type'] and c[-1] == MAIN_ID for c in self.calls()))

    def assert_refused(self, result, reason):
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('EXIT=2', result.stdout)
        self.assertIn('failover refused: ' + reason, result.stdout)
        self.assertEqual('codex', self.state('implementer.json')['tool'])
        self.assertNotIn('limits', self.state('implementer.json'))

    # --- the paths that switch --------------------------------------------------------------

    def test_an_exited_codex_is_switched_without_stopping_anything(self):
        self.right(self.frame('codex-limited-exited'))
        result = self.failover(processes=[])
        self.assert_switched_to_claude(result)
        self.assertEqual([], self.stopped_pids())

    def test_a_limited_codex_is_stopped_with_relay_history_and_one_confirming_read(self):
        text = self.frame('codex-limited-live')
        self.right(text)
        self.relay_record(text)
        result = self.failover(timing="Stable=90; Confirm=0; Sample=60; Step=30; ShellWait=2")
        self.assert_switched_to_claude(result)
        self.assertEqual([self.CODEX_PID], self.stopped_pids())    # the agent binary, not node or the pane's pwsh
        self.assertIn('relay saw this frame unchanged', self.log())

    def test_without_relay_history_the_pane_is_sampled(self):
        self.right(self.frame('codex-limited-live'))
        result = self.failover()
        self.assert_switched_to_claude(result)
        self.assertIn('sampling the pane', self.log())

    def test_young_relay_history_falls_back_to_sampling(self):
        text = self.frame('codex-limited-live')
        self.right(text)
        self.relay_record(text, age=10)
        result = self.failover()
        self.assert_switched_to_claude(result)
        self.assertIn('sampling the pane', self.log())

    # --- refusals: nothing stopped, nothing recorded -------------------------------------------

    def test_a_working_pane_is_refused(self):
        self.right(self.frame('codex-working'))
        self.assert_refused(self.failover(), 'the codex pane is neither showing')
        self.assertEqual([], self.stopped_pids())

    def test_a_pane_that_changes_while_checked_is_refused(self):
        self.right(self.frame('codex-limited-reached'))
        self.scenario['responses'] = [{'args': '^session text --target ' + RIGHT_ID,
                                       'stdout': self.frame('codex-limited-live'), 'once': True}]
        self.save_scenario()
        self.assert_refused(self.failover(), 'the codex pane changed while it was being checked')
        self.assertEqual([], self.stopped_pids())

    def test_a_git_lock_before_the_stop_is_refused(self):
        self.right(self.frame('codex-limited-live'))
        (self.checkout / '.git').mkdir(exist_ok=True)
        (self.checkout / '.git/index.lock').write_text('', encoding='utf-8')
        result = self.failover()
        self.assert_refused(result, "'")
        self.assertIn("index.lock' exists, so a git command may be running; nothing was stopped", result.stdout)
        self.assertEqual([], self.stopped_pids())

    def test_a_git_lock_that_appears_is_reported_and_kept(self):
        self.right(self.frame('codex-limited-live'))
        (self.checkout / '.git').mkdir(exist_ok=True)
        result = self.failover(lock_after_stop=True)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn('EXIT=3', result.stdout)
        self.assertIn('Failover incomplete: failover stopped codex but could not switch', result.stdout)
        self.assertIn('appeared while it was being stopped', result.stdout)
        self.assertTrue((self.checkout / '.git/index.lock').exists())
        self.assertEqual('codex', self.state('implementer.json')['tool'])

    def test_not_exactly_one_agent_process_is_refused(self):
        hub = str(self.checkout / '.workbench')
        second = dict(ProcessId=5000, ParentProcessId=1, Name='codex.exe',
                      CommandLine=f"codex.exe -c shell_environment_policy.set.AI_HUB='{hub}'")
        other_checkout = dict(ProcessId=5001, ParentProcessId=1, Name='codex.exe',
                              CommandLine="codex.exe -c shell_environment_policy.set.AI_HUB='C:\\elsewhere\\.workbench'")
        for table, found in (([other_checkout], 0), (self.processes(second), 2)):
            with self.subTest(found=found):
                self.right(self.frame('codex-limited-live'))
                self.assert_refused(self.failover(processes=table), f'expected exactly one codex process for this checkout, found {found}')
                self.assertEqual([], self.stopped_pids())

    def test_a_pane_that_stays_up_after_the_stop_is_refused(self):
        self.right(self.frame('codex-limited-live'))
        result = self.failover(stop_to_shell=False)
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn('EXIT=3', result.stdout)
        self.assertIn('failover stopped codex but could not switch: the pane showed no shell prompt', result.stdout)
        self.assertEqual('codex', self.state('implementer.json')['tool'])

    def test_a_process_that_survives_the_stop_is_reported(self):
        # r17 M2 (1): the root must be gone before anything else happens.
        self.right(self.frame('codex-limited-live'))
        result = self.failover(survives=True)
        self.assertIn('EXIT=3', result.stdout)
        self.assertIn(f'process {self.CODEX_PID} is still running', result.stdout)
        self.assertEqual('codex', self.state('implementer.json')['tool'])

    def test_a_force_stopped_frame_left_above_the_prompt_still_switches(self):
        # r17 M2: taskkill /F runs no cleanup, so the dead TUI's rules and footer stay on screen
        # above the new prompt; Test-ShellReady refuses that until Clear-Host.
        text = self.frame('codex-limited-live')
        self.right(text)
        leftover = text.rstrip('\n') + '\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\nPS C:\\checkout> '
        result = self.failover(after_stop=leftover)
        self.assert_switched_to_claude(result)

    def test_failover_off_is_refused(self):
        self.right(self.frame('codex-limited-exited'))
        self.assert_refused(self.failover(config={'failover': False}), '"failover" is false')

    # --- B4: no bounce ---------------------------------------------------------------------

    def test_a_target_with_a_recorded_limit_is_refused_until_the_human_clears_it(self):
        self.right(self.frame('codex-limited-exited'))
        self.assert_switched_to_claude(self.failover(processes=[]))
        # Claude now hits its limit too: switching back to the still-limited Codex is refused.
        self.right('PS C:\\checkout> ')
        result = self.failover(processes=[])
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn('codex was recorded limited', result.stdout)
        self.assertEqual('claude', self.state('implementer.json')['tool'])
        # A rerun without any switch keeps the record (the settings record owns it, not the launch).
        self.assertEqual(0, self.body().returncode)
        self.assertIn('codex', self.state('implementer.json')['limits'])
        self.right('PS C:\\checkout> ')             # that rerun started Claude; the human closes it
        # The human says Codex has reset: an explicit -Implementer codex clears its record.
        cleared = self.body('codex')
        self.assertEqual(0, cleared.returncode, cleared.stdout + cleared.stderr)
        self.assertEqual('codex', self.state('implementer.json')['tool'])
        self.assertNotIn('limits', self.state('implementer.json'))

    # --- entry and dry run ------------------------------------------------------------------

    def test_entry_refuses_failover_with_a_chosen_tool_or_queue(self):
        for extra in (['-Implementer', 'claude'], ['-NewSession'], ['-Queue', 'o/repo#7']):
            with self.subTest(extra=extra):
                result = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7',
                                         '-Failover', *extra], env=self.env, cwd=ROOT, capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=20)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn('-Failover picks the other tool itself', result.stdout)

    def test_dry_run_describes_the_failover_and_acts_on_nothing(self):
        self.cmd('gh', 'echo {"title":"fix-x","state":"OPEN"}\nexit /b 0')
        before = self.state('implementer.json')
        calls = len(self.calls())
        # Run as from a plain terminal: no adoption of this test's (real) caller pane.
        env = {k: v for k, v in self.env.items() if not k.startswith('AGWINTERM_')}
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7',
                                 '-DryRun', '-Failover'], env=env, cwd=ROOT, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('then switch to claude', result.stdout)
        self.assertEqual(before, self.state('implementer.json'))
        self.assertEqual(calls, len(self.calls()))


class AgentRoots(LauncherFixtures):
    """#24: which process is the limited agent - the binary itself, found by this checkout's marks."""

    def test_the_pane_frame_reaches_the_classifier_as_utf8_in_both_shells(self):
        # r17 M3: Windows PowerShell pipes to native programs as US-ASCII by default.
        frame = LIMIT_FRAMES / 'claude-limited-idle.txt'
        for shell in [PWSH] + ([WINDOWS_PS] if WINDOWS_PS else []):
            with self.subTest(shell=shell):
                command = (". ./lib/Workbench.ps1; $before = $OutputEncoding.WebName; "
                           "$t = [IO.File]::ReadAllText(" + ps_quote(frame) + ", [Text.Encoding]::UTF8); "
                           "$r = Get-PaneLimit $t claude; \"$($r.kind)|$($before -eq $OutputEncoding.WebName)\"")
                result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
                                        env=self.env, cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual('limited|True', result.stdout.strip())

    def test_failover_config_is_strictly_boolean(self):
        for value, ok in ((False, True), (True, True), ('false', False), (0, False)):
            with self.subTest(value=value):
                self.config_path.write_text(json.dumps({'failover': value}), encoding='utf-8')
                result = ps('. ./lib/Workbench.ps1; (Get-WorkbenchConfig).failover', env=self.env)
                self.assertEqual(ok, result.returncode == 0, result.stdout + result.stderr)
                if ok:
                    self.assertEqual(str(value), result.stdout.strip())

    def roots(self, tool, table, identity=None):
        if identity:
            path = self.checkout / '.workbench/state/implementer-claude.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'sessionId': identity}), encoding='utf-8')
        result = ps(". ./lib/Workbench.ps1; function Get-AgentProcesses { " + ps_quote(json.dumps(table)) +
                    " | ConvertFrom-Json }; @(Find-AgentRoot " + ps_quote(self.checkout) + " '" + tool +
                    "') | ForEach-Object { $_.ProcessId }", env=self.env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return [int(x) for x in result.stdout.split()]

    def test_codex_root_skips_wrappers_and_children(self):
        hub = str(self.checkout / '.workbench')
        mark = f"shell_environment_policy.set.AI_HUB='{hub}'"
        table = [dict(ProcessId=10, ParentProcessId=1, Name='pwsh.exe', CommandLine=f'pwsh -File pane-codex.ps1 {mark}'),
                 dict(ProcessId=11, ParentProcessId=10, Name='cmd.exe', CommandLine=f'cmd /c codex.cmd {mark}'),
                 dict(ProcessId=12, ParentProcessId=11, Name='node.exe', CommandLine=f'node codex.js {mark}'),
                 dict(ProcessId=13, ParentProcessId=12, Name='codex.exe', CommandLine=f'codex.exe {mark}'),
                 dict(ProcessId=14, ParentProcessId=13, Name='codex.exe', CommandLine=f'codex.exe helper {mark}'),
                 dict(ProcessId=15, ParentProcessId=13, Name='codex-command-runner.exe', CommandLine=mark)]
        self.assertEqual([13], self.roots('codex', table))

    def test_claude_root_is_found_by_its_recorded_conversation(self):
        sid = '77777777-7777-4777-8777-777777777777'
        table = [dict(ProcessId=20, ParentProcessId=1, Name='pwsh.exe', CommandLine=f'pwsh -File pane-implementer-claude.ps1 --session-id {sid}'),
                 dict(ProcessId=21, ParentProcessId=20, Name='claude.exe', CommandLine=f'claude.exe --disallowedTools x --resume {sid} "go"'),
                 dict(ProcessId=22, ParentProcessId=1, Name='claude.exe', CommandLine='claude.exe --session-id 88888888-8888-4888-8888-888888888888')]
        self.assertEqual([21], self.roots('claude', table, identity=sid))
        self.assertEqual([], self.roots('claude', table[:1] + table[2:], identity=sid))


class AutonomyLaunch(LauncherFixtures):
    """#27: autonomy is per-checkout policy like auto-merge, and it implies auto-merge."""
    body = ClaudeImplementer.body
    state = ClaudeImplementer.state
    reuse_scenario = ClaudeImplementer.reuse_scenario

    def record(self):
        data = self.state('implementer.json')
        return data['autonomous'], data['autoMerge']

    def test_default_is_off(self):
        result = self.body()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual((False, False), self.record())
        self.assertIn('autonomous off', result.stdout)

    def test_on_implies_auto_merge_and_sticks(self):
        on = self.body(extra=' -Autonomous $true')
        self.assertEqual(0, on.returncode, on.stdout + on.stderr)
        self.assertEqual((True, True), self.record())
        self.reuse_scenario('esc to interrupt')
        again = self.body()
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertEqual((True, True), self.record())

    def test_no_auto_merge_on_an_autonomous_checkout_is_refused(self):
        self.assertEqual(0, self.body(extra=' -Autonomous $true').returncode)
        self.reuse_scenario('esc to interrupt')
        refused = self.body(extra=' -AutoMerge $false')
        self.assertEqual(1, refused.returncode)
        self.assertIn('EXIT=2', refused.stdout)
        self.assertIn('autonomy implies auto-merge', refused.stdout)
        self.assertEqual((True, True), self.record())

    def test_no_autonomous_alone_leaves_auto_merge_as_saved(self):
        self.assertEqual(0, self.body(extra=' -Autonomous $true').returncode)
        self.reuse_scenario('esc to interrupt')
        off = self.body(extra=' -Autonomous $false')
        self.assertEqual(0, off.returncode, off.stdout + off.stderr)
        self.assertEqual((False, True), self.record())

    def test_config_default_and_strict_boolean(self):
        on = self.body(config={'autonomous': True})
        self.assertEqual(0, on.returncode, on.stdout + on.stderr)
        self.assertEqual((True, True), self.record())
        self.config_path.write_text(json.dumps({'autonomous': 'yes'}), encoding='utf-8')
        bad = ps('. ./lib/Workbench.ps1; Get-WorkbenchConfig | Out-Null', env=self.env)
        self.assertNotEqual(0, bad.returncode)
        self.assertIn('autonomous', bad.stderr + bad.stdout)

    def test_entry_refuses_contradictory_switches(self):
        for extra, message in ((['-Autonomous', '-NoAutonomous'], '-Autonomous and -NoAutonomous cannot be combined'),
                               (['-Autonomous', '-NoAutoMerge'], '-Autonomous implies auto-merge')):
            with self.subTest(extra=extra):
                result = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7', *extra],
                                        env=self.env, cwd=ROOT, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=20)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn(message, result.stdout)
                self.assertFalse(self.calls())

    def test_dry_run_prints_autonomy(self):
        self.cmd('gh', 'echo {"title":"fix-x","state":"OPEN"}\nexit /b 0')
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(LIB / 'github-workbench.ps1'), 'o/repo#7',
                                 '-NewSession', '-DryRun', '-Autonomous'], env=self.env, cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('auto-merge on; autonomous on', result.stdout)
        self.assertFalse((self.checkout / '.workbench').exists())


if __name__ == "__main__":
    unittest.main()
