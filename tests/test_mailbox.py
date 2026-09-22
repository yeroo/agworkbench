"""The mailbox the agents and the review helpers share, in a temp directory.

post.py is how revmux and revdiff report (they are not agents and get no box of their own), and
agmsg is how the agents read. This pins that the two meet.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"


class Mailbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=Path(__file__).resolve().parent.parent)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = dict(os.environ, AI_HUB=self.tmp, AI_BOX="claude", PYTHONIOENCODING="utf-8")

    def run_py(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=self.env,
                              encoding="utf-8", errors="replace")

    def test_a_helper_posts_and_the_agent_reads_it(self):
        report = Path(self.tmp) / "report.md"
        report.write_text("## finding\nsomething is wrong", encoding="utf-8")
        posted = self.run_py(str(LIB / "post.py"), "--hub", self.tmp, "--to", "claude",
                             "--sender", "revmux", "--kind", "review", "--subject", "revmux round 1: findings reported",
                             "--body-file", str(report))
        self.assertEqual(0, posted.returncode, posted.stderr)
        listed = self.run_py(str(LIB / "agmsg.py"), "list", "--box", "claude")
        self.assertIn("revmux round 1", listed.stdout)
        read = self.run_py(str(LIB / "agmsg.py"), "read", "--box", "claude")
        self.assertIn("something is wrong", read.stdout)
        self.assertIn("from:    revmux", read.stdout)

    def test_an_empty_report_is_still_delivered_as_a_message(self):
        # a revmux run that wrote nothing must not vanish silently
        posted = self.run_py(str(LIB / "post.py"), "--hub", self.tmp, "--to", "claude", "--sender", "revmux",
                             "--subject", "revmux round 2: tool error (exit 2)",
                             "--body-file", str(Path(self.tmp) / "missing.md"))
        self.assertEqual(0, posted.returncode, posted.stderr)
        read = self.run_py(str(LIB / "agmsg.py"), "read", "--box", "claude")
        self.assertIn("(empty)", read.stdout)

    def test_a_box_name_cannot_escape_the_mailbox(self):
        posted = self.run_py(str(LIB / "post.py"), "--hub", self.tmp, "--to", "../outside",
                             "--sender", "x", "--subject", "s", "--text", "t")
        self.assertNotEqual(0, posted.returncode)
        self.assertFalse((Path(self.tmp).parent / "outside").exists())


if __name__ == "__main__":
    unittest.main()
