"""Claude folder trust, against a temp copy of the config - never the real ~/.claude.json."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import trust  # noqa: E402


class ClaudeTrust(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.config = self.dir / ".claude.json"
        self.config.write_text(json.dumps({
            "numStartups": 5,
            "projects": {"C:/other": {"hasTrustDialogAccepted": True, "allowedTools": ["a"],
                                      "deep": {"nested": {"kept": [1, 2, {"x": 1}]}}}},
        }), encoding="utf-8")

    def load(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_the_clone_becomes_trusted(self):
        clone = self.dir / "repo-issue-1"
        trust.grant_claude(str(clone), self.config)
        self.assertTrue(self.load()["projects"][trust.claude_key(str(clone))]["hasTrustDialogAccepted"])

    def test_the_key_uses_forward_slashes_like_claude_does(self):
        self.assertNotIn(chr(92), trust.claude_key(r"C:\Users\x\repo"))

    def test_everything_else_in_the_file_survives_intact(self):
        # the reason this is Python: PowerShell 5.1's ConvertTo-Json flattens past depth 2
        trust.grant_claude(str(self.dir / "c"), self.config)
        data = self.load()
        self.assertEqual(5, data["numStartups"])
        self.assertEqual([1, 2, {"x": 1}], data["projects"]["C:/other"]["deep"]["nested"]["kept"])
        self.assertEqual(["a"], data["projects"]["C:/other"]["allowedTools"])

    def test_granting_twice_is_a_no_op(self):
        clone = str(self.dir / "c")
        trust.grant_claude(clone, self.config)
        self.assertIn("already trusted", trust.grant_claude(clone, self.config))

    def test_no_temp_file_is_left_behind(self):
        trust.grant_claude(str(self.dir / "c"), self.config)
        self.assertEqual([], [p.name for p in self.dir.glob("*.tmp")])


if __name__ == "__main__":
    unittest.main()
