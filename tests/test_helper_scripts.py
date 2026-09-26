"""#45: revmux and revdiff helpers never end silently - a helper that fails still mails the planner."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))

import hub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PWSH = shutil.which('pwsh')


@unittest.skipIf(PWSH is None, 'pwsh not on PATH')
class HelperScripts(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ('test helper scripts ' + uuid.uuid4().hex)
        self.checkout = self.folder / 'checkout'
        (self.checkout / '.workbench').mkdir(parents=True)
        self.bin = self.folder / 'bin'
        self.bin.mkdir()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(hub.reload_paths)

    def run_script(self, script, *params):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''))
        for name in ('AGWINTERM_PANE_ID', 'AGWINTERM_SESSION_ID'):
            env.pop(name, None)                          # no pane: no marker, nothing else changes
        return subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'lib' / script),
                               '-Checkout', str(self.checkout), *params],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120, env=env)

    def mails(self):
        os.environ['AI_HUB'] = str(self.checkout / '.workbench')
        return [hub.parse_message(path) for path in sorted((self.checkout / '.workbench' / 'inbox' / 'claude').glob('*.md'))]

    def test_a_revmux_round_that_fails_mails_the_planner(self):
        (self.bin / 'revmux.cmd').write_text('@echo revmux is broken\r\n@exit /b 3\r\n', encoding='utf-8')
        scope = self.checkout / 'scope.md'
        scope.write_text('scope', encoding='utf-8')
        done = self.run_script('run-revmux.ps1', '-ScopeFile', str(scope), '-Round', '2')
        self.assertNotEqual(0, done.returncode)
        [mail] = self.mails()
        self.assertEqual(('helper', 'note'), (mail['from'], mail['kind']))
        self.assertEqual('revmux round 2: ended without a report (it did not finish)', mail['subject'])

    def test_a_review_that_fails_mails_the_planner(self):
        (self.bin / 'revdiff.ps1').write_text("throw 'revdiff crashed'\n", encoding='utf-8')
        done = self.run_script('human-review.ps1', '-Base', 'origin/main')
        self.assertNotEqual(0, done.returncode)
        [mail] = self.mails()
        self.assertEqual(('helper', 'note'), (mail['from'], mail['kind']))
        self.assertTrue(mail['subject'].startswith('human review (revdiff): ended without a result'), mail['subject'])
        self.assertIn('revdiff crashed', done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
