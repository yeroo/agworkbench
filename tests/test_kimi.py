"""#65: the Kimi implementer's trust record, web-tool guard, role file and shell guard rails.

The guard-rail tests run the shims the way Kimi Code runs every command on Windows: Git Bash's
launcher (`Git\\bin\\bash.exe -c "cd '<cwd>' && <cmd>"`) with the pane's environment. A test that
executed the shim file directly would pass while Git Bash put its own git first on PATH.
No test reads or writes the real ~/.kimi-code: KIMI_CODE_HOME and `home` point at temp dirs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import kimi  # noqa: E402

GIT = shutil.which("git")


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="agw-kimi-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.home = self.temp / "kimi-home"
        self.home.mkdir()

    def clone(self) -> Path:
        checkout = self.temp / "repo-issue-7"
        checkout.mkdir()
        git(checkout, "init", "-q")
        git(checkout, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        return checkout


class TrustRecord(Temp):
    def test_the_key_is_kimis_trust_key(self):
        # The existing record wd_docxy_d1ad5ceee03d was written by Kimi itself for this path.
        self.assertEqual("wd_docxy_d1ad5ceee03d", kimi.trust_key(r"C:\Users\boris\source\docxy"))
        for spelling in ("c:/users/boris/source/docxy", "C:\\Users\\Boris\\source\\docxy\\", "C:/Users/boris/source/docxy//"):
            with self.subTest(spelling=spelling):
                self.assertEqual("wd_docxy_d1ad5ceee03d", kimi.trust_key(spelling))
        self.assertTrue(kimi.trust_key(r"C:\w\My Repo!! (copy)").startswith("wd_my-repo-copy_"))
        self.assertTrue(kimi.trust_key("C:\\").startswith("wd_workspace_") or kimi.trust_key("C:\\").startswith("wd_c_"))

    def test_written_once_and_no_other_record_is_touched(self):
        other = self.home / "workspace-trust" / "wd_docxy_d1ad5ceee03d"
        other.parent.mkdir()
        other.write_text('{"root":"x","trustedAt":1}', encoding="utf-8")
        directory = str(self.temp / "repo-issue-7")
        self.assertEqual(f"trusted: {directory}", kimi.grant_trust(directory, self.home))
        record = self.home / "workspace-trust" / kimi.trust_key(directory)
        data = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(directory, data["root"])
        self.assertIsInstance(data["trustedAt"], int)
        self.assertEqual(f"already trusted: {directory}", kimi.grant_trust(directory, self.home))
        self.assertEqual(json.dumps(data), record.read_text(encoding="utf-8"))
        self.assertEqual('{"root":"x","trustedAt":1}', other.read_text(encoding="utf-8"))
        self.assertEqual(2, len(list((self.home / "workspace-trust").iterdir())))

    def test_kimi_code_home_is_honoured(self):
        self.assertEqual(self.home, kimi.kimi_home({"KIMI_CODE_HOME": str(self.home)}))
        self.assertEqual(Path.home() / ".kimi-code", kimi.kimi_home({}))


class WebGuard(Temp):
    def config(self, text):
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_allow_network_needs_no_config(self):
        self.assertIsNone(kimi.web_guard(True, self.home))

    def test_missing_config_or_tools_refuses_with_the_lines_to_add(self):
        problem = kimi.web_guard(False, self.home)
        self.assertIn("does not exist", problem)
        self.assertIn('disabled = ["FetchURL", "WebSearch"]', problem)
        self.assertIn('"allowNetwork": true', problem)
        self.config('default_model = "k"\n')
        self.assertIn("FetchURL, WebSearch are on", kimi.web_guard(False, self.home))
        self.config('[tools]\ndisabled = ["FetchURL"]\n')
        problem = kimi.web_guard(False, self.home)
        self.assertIn("WebSearch are on", problem)
        self.assertNotIn("FetchURL, WebSearch are on", problem)

    def test_both_disabled_passes(self):
        self.config('default_model = "k"\n[tools]\ndisabled = ["AskUserQuestion", "FetchURL", "WebSearch"]\n')
        self.assertIsNone(kimi.web_guard(False, self.home))

    def test_unreadable_config_fails_closed(self):
        self.config('[tools\ndisabled = ["FetchURL", "WebSearch"]\n')
        self.assertIn("cannot read", kimi.web_guard(False, self.home))
        self.config('[tools]\ndisabled = "FetchURL,WebSearch"\n')
        self.assertIn("not a list", kimi.web_guard(False, self.home))

    def test_the_cli_reads_kimi_code_home(self):
        self.config('[tools]\ndisabled = ["FetchURL", "WebSearch"]\n')
        env = dict(os.environ, KIMI_CODE_HOME=str(self.home))
        done = subprocess.run([sys.executable, str(ROOT / "lib/kimi.py"), "web-guard"], env=env, capture_output=True, text=True)
        self.assertEqual((0, "ok"), (done.returncode, done.stdout.strip()))
        (self.home / "config.toml").unlink()
        done = subprocess.run([sys.executable, str(ROOT / "lib/kimi.py"), "web-guard"], env=env, capture_output=True, text=True)
        self.assertEqual(1, done.returncode)
        self.assertIn(str(self.home), done.stdout)


@unittest.skipUnless(GIT, "git not on PATH")
class Prepare(Temp):
    def test_role_exclude_shims_and_trust(self):
        checkout = self.clone()
        result = kimi.prepare(str(checkout), "o/repo#7", allow_network=False, home=self.home)
        role = (checkout / ".kimi-code/AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(role.startswith(kimi.MARKER))
        self.assertIn("GitHub issue o/repo#7", role)
        self.assertIn("The web tools (FetchURL, WebSearch) are off", role)
        self.assertNotIn("@@", role)
        self.assertEqual("", git(checkout, "status", "--porcelain", "--", ".kimi-code"))   # excluded, not untracked
        # Idempotent: a second launch rewrites the role and adds no second exclude line.
        kimi.prepare(str(checkout), "o/repo#7", allow_network=True, home=self.home)
        exclude = (checkout / ".git/info/exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, exclude.count(kimi.EXCLUDE_LINE))
        self.assertIn("you may use FetchURL", (checkout / ".kimi-code/AGENTS.md").read_text(encoding="utf-8"))
        shims = checkout / ".workbench/state/kimi-bin"
        for name in ("git", "gh", "env.sh"):
            with self.subTest(shim=name):
                data = (shims / name).read_bytes()
                self.assertNotIn(b"\r\n", data)                  # bash scripts, whatever git's autocrlf says
                self.assertNotIn(b"@@", data)
        self.assertIn(kimi.msys_path(GIT).encode(), (shims / "git").read_bytes())
        self.assertTrue((self.home / "workspace-trust" / kimi.trust_key(str(checkout.resolve()))).exists())
        self.assertEqual(result["trustKey"], kimi.trust_key(str(checkout.resolve())))

    def test_environment(self):
        checkout = self.clone()
        env = kimi.prepare(str(checkout), "o/repo#7", allow_network=False, dry_run=True, home=self.home)["env"]
        self.assertEqual("1", env["KIMI_CODE_NO_AUTO_UPDATE"])
        self.assertTrue(env["KIMI_SHELL_PATH"].lower().endswith("\\bin\\bash.exe" if os.name == "nt" else "/bash"))
        self.assertTrue(env["BASH_ENV"].endswith("/.workbench/state/kimi-bin/env.sh"))
        self.assertEqual("agworkbench-refused", env["GH_TOKEN"])
        self.assertEqual("5", env["GIT_CONFIG_COUNT"])
        self.assertEqual({"https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/",
                          "ssh://github.com/"}, {env[f"GIT_CONFIG_VALUE_{i}"] for i in range(5)})
        self.assertEqual({"url.agworkbench-push-refused://.pushInsteadOf"}, {env[f"GIT_CONFIG_KEY_{i}"] for i in range(5)})
        self.assertFalse((checkout / ".kimi-code").exists())        # a dry run writes nothing
        self.assertFalse((self.home / "workspace-trust").exists())

    def test_a_tracked_or_foreign_role_file_is_never_overwritten(self):
        checkout = self.clone()
        role = checkout / ".kimi-code/AGENTS.md"
        role.parent.mkdir()
        role.write_text("the repository's own notes\n", encoding="utf-8")
        with self.assertRaises(kimi.Refused) as caught:
            kimi.prepare(str(checkout), "o/repo#7", allow_network=False, home=self.home)
        self.assertIn("not written by agworkbench", str(caught.exception))
        git(checkout, "add", ".kimi-code/AGENTS.md")
        with self.assertRaises(kimi.Refused) as caught:
            kimi.prepare(str(checkout), "o/repo#7", allow_network=False, home=self.home)
        self.assertIn("is tracked", str(caught.exception))
        self.assertEqual("the repository's own notes\n", role.read_text(encoding="utf-8"))

    def test_no_git_no_shell(self):
        with self.assertRaises(kimi.Refused):
            kimi.git_bash(None)


BASH = None
if GIT:
    try:
        BASH = kimi.git_bash(GIT)
    except kimi.Refused:
        BASH = None


# The git a push can reach past the shim with, and the git bash finds without BASH_ENV: Git Bash's
# own on Windows, the system git on Linux/macOS (#60).
if os.name == "nt":
    BYPASS_GITS, PLAIN_GIT = ("git.exe", "/mingw64/bin/git", "cmd //c git"), "/mingw64/bin/git"
else:
    BYPASS_GITS, PLAIN_GIT = (GIT or "git", "command -p git"), GIT


@unittest.skipUnless(BASH, "Git Bash not found")
class GuardRails(Temp):
    """The shims and the backstop, run through Git Bash's launcher with the pane's environment."""
    SUFFIX = ""

    def setUp(self):
        super().setUp()
        # Not under %TEMP%: MSYS spells that /tmp/..., a spelling the shim path never has, which once
        # hid a guard that skipped the prepend whenever the shim was anywhere on PATH (FIX r1 M1). A
        # real checkout is /c/..., like this one (inside the repository's ignored .workbench/).
        parent = ROOT / ".workbench" / f"test-kimi-{os.getpid()}-{id(self)}{self.SUFFIX}"
        parent.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, parent, ignore_errors=True)
        self.checkout = parent / "repo-issue-7"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q")
        git(self.checkout, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        self.assertTrue(kimi.msys_path(str(self.checkout)).startswith("/c/") or not str(self.checkout).lower().startswith("c:"))
        prepared = kimi.prepare(str(self.checkout), "o/repo#7", allow_network=False, home=self.home)
        # Exactly the pane's environment: its env map, and the shim directory first on the Windows PATH.
        self.env = dict(os.environ, **prepared["env"])
        self.env["PATH"] = prepared["shimDir"] + os.pathsep + os.environ["PATH"]
        # A github-shaped remote: the backstop rewrites its push URL to a scheme git cannot speak, so
        # nothing ever reaches the network. Fetch URLs are left alone.
        git(self.checkout, "remote", "add", "origin", "https://github.com/o/repo.git")

    def bash(self, command: str) -> subprocess.CompletedProcess:
        cwd = str(self.checkout).replace("\\", "/").replace("'", "'\\''")
        return subprocess.run([BASH, "-c", f"cd '{cwd}' && {command}"], env=self.env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)

    def test_git_resolves_to_the_shim_in_kimis_shell(self):
        done = self.bash("command -v git")
        self.assertTrue(done.stdout.strip().endswith("/.workbench/state/kimi-bin/git"), done.stdout + done.stderr)
        if os.name != "nt":
            return  # only Git Bash's launcher moves its own git ahead of an inherited PATH (#60)
        # Without BASH_ENV, Git Bash's own git wins: the reason BASH_ENV is there.
        plain = subprocess.run([BASH, "-c", "command -v git"], env=dict(os.environ, PATH=str(self.checkout / ".workbench/state/kimi-bin") + os.pathsep + os.environ["PATH"]),
                               capture_output=True, text=True)
        self.assertEqual(PLAIN_GIT, plain.stdout.strip())

    def test_push_in_any_spelling_the_shim_sees_is_refused(self):
        for command in ("git push origin HEAD", "git -C . push", "git -c a.b=c push origin HEAD",
                        "git --no-pager push", "git send-pack x", "command git push origin HEAD"):
            with self.subTest(command=command):
                done = self.bash(command)
                self.assertNotEqual(0, done.returncode)
                self.assertIn("refused in the Kimi implementer's pane", done.stderr)

    def test_the_backstop_stops_a_push_that_gets_past_the_shim(self):
        for command in (f"{bypass} push origin HEAD" for bypass in BYPASS_GITS):
            with self.subTest(command=command):
                done = self.bash(command)
                self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
                self.assertIn("agworkbench-push-refused", done.stderr)
        # Every GitHub spelling git writes is rewritten for push.
        for url in ("http://github.com/o/repo.git", "ssh://github.com/o/repo.git", "git@github.com:o/repo.git",
                    "ssh://git@github.com/o/repo.git"):
            with self.subTest(url=url):
                git(self.checkout, "remote", "set-url", "origin", url)
                done = self.bash(f"{BYPASS_GITS[0]} push origin HEAD")
                self.assertNotEqual(0, done.returncode)
                self.assertIn("agworkbench-push-refused", done.stderr)
        git(self.checkout, "remote", "set-url", "origin", "https://github.com/o/repo.git")
        # Fetch URLs are not rewritten.
        self.assertIn("https://github.com/o/repo.git (fetch)", self.bash(f"{BYPASS_GITS[0]} remote -v").stdout)
        self.assertIn("agworkbench-push-refused://o/repo.git (push)", self.bash(f"{BYPASS_GITS[0]} remote -v").stdout)

    def test_ordinary_git_passes_through(self):
        self.assertEqual(0, self.bash("git status --short").returncode)
        done = self.bash("git -c user.email=t@t -c user.name=t commit -q --allow-empty -m kimi && git log --oneline -1")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("kimi", done.stdout)

    def test_gh_is_refused_and_names_the_planner(self):
        done = self.bash("gh pr list")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("planner", done.stderr)
        self.assertEqual("agworkbench-refused", self.bash("echo $GH_TOKEN").stdout.strip())


class GuardRailsInAnOddPath(GuardRails):
    """FIX r2 m5: an apostrophe and a bracket in the checkout path (an O'Neil profile) must not break
    the single-quoted shims or env.sh's substitution - every guard-rail test again, in such a path."""
    SUFFIX = " o'neil [x]"


if __name__ == "__main__":
    unittest.main()
