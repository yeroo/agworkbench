"""The PowerShell side, composed without a terminal: issue parsing and Codex's launch policy.

Every case runs PowerShell in a child process against a temp config (AGWORKBENCH_CONFIG), so the
suite never reads the user's own ~/.agworkbench.json and never starts an agent.
Skipped whole when pwsh is not on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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


class PaneIds(unittest.TestCase):
    def test_flat_scalar_panes_and_invalid_lists(self):
        for session, expected in [({"id": MAIN_ID}, [MAIN_ID]),
                                  ({"id": MAIN_ID, "paneIds": [MAIN_ID, RIGHT_ID]}, [MAIN_ID, RIGHT_ID])]:
            with self.subTest(session=session):
                result = ps(". ./lib/Workbench.ps1; $ids = @(Get-PaneIds " + ps_json(session) +
                            "); ConvertTo-Json -Compress -InputObject $ids")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, json.loads(result.stdout))
        for ids in ([], [MAIN_ID, RIGHT_ID, RELAY_ID], [MAIN_ID, MAIN_ID], ["1"], [None]):
            with self.subTest(ids=ids):
                result = ps("$ErrorActionPreference='Stop'; . ./lib/Workbench.ps1; Get-PaneIds " +
                            ps_json({"id": MAIN_ID, "paneIds": ids}))
                self.assertNotEqual(0, result.returncode)
                self.assertIn(MAIN_ID, result.stderr)


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
        self.registry_path = self.checkout / ".workbench/state/agents.json"
        self.config_path = self.temp / "config.json"
        self.config_path.write_text(json.dumps({"checkoutRoot": str(self.temp)}), encoding="utf-8")
        self.env = dict(os.environ, PATH=str(self.temp) + os.pathsep +
                        str(Path(os.environ["SystemRoot"]) / "System32"),
                        AGWINTERMCTL=str(self.temp / "agwintermctl.ps1"),
                        STUB_CTL_SCENARIO=str(self.scenario_path), STUB_CTL_CALLS=str(self.calls_path),
                        AGWORKBENCH_CONFIG=str(self.config_path), PYTHONIOENCODING="utf-8",
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
                         "tree": {"workspaces": []}, "text": {}}
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

    def flow(self, shell=PWSH, no_relay=False):
        script = (self.setup_ps() + "Connect-LaunchLog " + ps_quote(self.log_path) + "; "
                  "$codexLine = Get-PaneLaunch 'pane-codex.ps1' @{Checkout=" + ps_quote(self.checkout) +
                  "; Issue='o/repo#7'}; $builder = { param($Hub,$Left,$Right) "
                  "'python ' + (Quote 'relay.py') + ' --hub ' + (Quote $Hub) + "
                  "' --claude-pane ' + (Quote $Left) + ' --codex-pane ' + (Quote $Right) }; "
                  "$ok = Invoke-LaunchSafely { Start-WorkbenchSession -Checkout " + ps_quote(self.checkout) +
                  " -Number 7 -Slug 'fix-x' -RepoName 'repo' -ClaudeLaunch 'claude launch' "
                  "-CodexLaunch $codexLine -RelayCommand $builder " + ("-NoRelay " if no_relay else "") +
                  "}; if (-not $ok) {exit 1}; $script:Launch | ConvertTo-Json -Compress")
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                              env=self.env, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=40)

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
        registry = self.register(claude=RIGHT_ID, codex=MAIN_ID)
        for session, reg, expected in [({"id": MAIN_ID}, None, (MAIN_ID, None, True, "primary")),
                                      ({"id": MAIN_ID, "paneIds": [MAIN_ID, RIGHT_ID]}, registry,
                                       (RIGHT_ID, MAIN_ID, False, "split")),
                                      ({"id": OTHER_ID, "paneIds": [OTHER_ID, RELAY_ID]}, registry,
                                       (OTHER_ID, RELAY_ID, False, "primary"))]:
            result = ps(". ./lib/Workbench.ps1; Get-PanePlan " + ps_json(session) + " " + ps_json(reg) +
                        " | ConvertTo-Json -Compress", env=self.env)
            self.assertEqual(0, result.returncode, result.stderr)
            plan = json.loads(result.stdout)
            self.assertEqual(expected, tuple(plan[k] for k in ("Claude", "Codex", "NeedSplit", "ClaudeSlot")))


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
        commands = [c[:2] for c in self.calls()]
        self.assertEqual([["tree", "--json"], ["session", "new"], ["tree", "--json"],
                          ["session", "split"], ["tree", "--json"], ["session", "text"],
                          ["session", "type"], ["session", "new"], ["session", "select"],
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
        self.scenario["split_delay"] = 2
        self.save_scenario()
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(1, sum(c[:2] == ["session", "split"] for c in self.calls()))
        self.assertEqual(4, sum(c == ["tree", "--json"] for c in self.calls()))

    def test_existing_relay_restarts_in_its_own_shell(self):
        self.resumed()
        self.scenario["text"][RELAY_ID] = "PS C:\\relay> "
        self.save_scenario()
        result = self.flow()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        typed = [c for c in self.calls() if c[:2] == ["session", "type"]]
        self.assertEqual(1, len(typed))
        self.assertEqual(RELAY_ID, typed[0][-1])
        self.assertFalse(any(c[:2] == ["session", "new"] for c in self.calls()))

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
                for text in ("pane-codex.ps1", "python 'relay.py'", "--codex-pane '" + RIGHT_ID + "'", str(self.checkout)):
                    self.assertIn(text, result.stdout)

    @unittest.skipUnless(WINDOWS_PS, "Windows PowerShell not installed")
    def test_windows_powershell_split_failure(self):
        self.scenario["responses"] = [{"args": r"^session split", "stdout": "native stderr failure", "exit": 1, "stderr": True}]
        self.save_scenario()
        result = self.flow(shell=WINDOWS_PS)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("ctl exit 1: native stderr failure", self.log())
        self.assertIn("error ", self.log())
        self.assertEqual(1, result.stdout.count("Launcher failed:"))


class LauncherScript(LauncherFixtures):
    def run_entry(self, *args):
        return subprocess.run([PWSH, "-NoProfile", "-File", str(LIB / "github-workbench.ps1"), *args],
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

    def test_body_wires_launch_commands_and_repair_boundary(self):
        # Replace only network/checkout/trust boundaries; run the real launcher body,
        # session logic, CLI transport, mailbox writes, and shared error handler.
        result = ps(self.setup_ps() +
                    "function Get-IssueInfo { return @{title='fix-x'; state='OPEN'} }; "
                    "function New-IssueCheckout { Connect-LaunchLog " + ps_quote(self.log_path) +
                    "; return @{Dir=" + ps_quote(self.checkout) + "; Branch='issue-7-fix-x'} }; "
                    "function Grant-CodexTrust {}; function Grant-ClaudeTrust {}; "
                    "$ok=Invoke-LaunchSafely { Invoke-LauncherBody -Issue 'o/repo#7' }; "
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


if __name__ == "__main__":
    unittest.main()
