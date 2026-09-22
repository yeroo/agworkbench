"""The PowerShell side, composed without a terminal: issue parsing and Codex's launch policy.

Every case runs PowerShell in a child process against a temp config (AGWORKBENCH_CONFIG), so the
suite never reads the user's own ~/.agworkbench.json and never starts an agent.
Skipped whole when pwsh is not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
PWSH = shutil.which("pwsh")

if PWSH is None:
    raise unittest.SkipTest("pwsh not on PATH")


def ps(script: str, config: dict | None = None) -> subprocess.CompletedProcess:
    env = None
    tmp = None
    if config is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, tmp)
        tmp.close()
        import os
        env = dict(os.environ, AGWORKBENCH_CONFIG=tmp.name)
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
        # PowerShell unrolls a one-element array returned from a function; indexing the result then
        # takes a character. The first live run registered Claude's pane as "4" this way.
        result = ps(". ./lib/Workbench.ps1; $s = [pscustomobject]@{ id = 'abc-123' }; "
                    "(Get-PaneIds $s)[0]; @(Get-PaneIds $s)[0]")
        self.assertEqual(["abc-123", "abc-123"], result.stdout.split())

    def test_a_split_session_yields_both_panes_in_order(self):
        result = ps(". ./lib/Workbench.ps1; $s = [pscustomobject]@{ id = 'a'; paneIds = @('p1','p2') }; "
                    "(Get-PaneIds $s) -join ','")
        self.assertEqual("p1,p2", result.stdout.strip())


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


if __name__ == "__main__":
    unittest.main()
