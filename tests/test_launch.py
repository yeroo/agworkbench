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


if __name__ == "__main__":
    unittest.main()
