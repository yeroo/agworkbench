"""#24: recognising a usage limit from a pane frame, with fixtures built from the binaries' own strings."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import limits  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "limits"

# frame -> (tool, expected kind, exited)
EXPECTED = {
    "codex-limited-live": ("codex", "limited", False),
    "codex-limited-reached": ("codex", "limited", False),
    "codex-limited-credits": ("codex", "limited", False),
    "codex-limited-exited": ("codex", "limited", True),
    "claude-limited-idle": ("claude", "limited", False),
    "claude-limited-team": ("claude", "limited", False),
    "claude-limited-credit": ("claude", "limited", False),
    "codex-warning-chooser": ("codex", "warning", False),
    "codex-auto-switched": ("codex", None, False),
    "codex-tool-output": ("codex", None, False),
    "codex-working": ("codex", None, False),
    "claude-tool-output-idle": ("claude", None, False),
    "claude-tool-output-bare": ("claude", None, False),
    "claude-tool-output-running": ("claude", None, False),
    "claude-cat-fixture": ("claude", None, False),
    "claude-diff": ("claude", None, False),
    "claude-unittest-failure": ("claude", None, False),
    "claude-grep": ("claude", None, False),
    "claude-prose": ("claude", None, False),
    "claude-quoted": ("claude", None, False),
    "claude-scrolled": ("claude", None, False),
    "codex-text-above-fresh-claude": ("claude", None, False),
}


def frame(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


class Fixtures(unittest.TestCase):
    def test_every_frame_file_has_an_expectation(self):
        names = {p.stem for p in FIXTURES.glob("*.txt") if not p.stem.startswith("strings-")}
        self.assertEqual(set(EXPECTED), names)

    def test_frames_classify_as_expected(self):
        for name, (tool, kind, exited) in EXPECTED.items():
            with self.subTest(frame=name):
                found = limits.classify(frame(name), tool)
                self.assertEqual(kind, found.kind if found else None, found)
                if found:
                    self.assertEqual(exited, found.exited)

    def test_limit_rows_are_quoted_from_the_binaries_not_retyped(self):
        strings = {tool: (FIXTURES / f"strings-{tool}.txt").read_text(encoding="utf-8") for tool in ("codex", "claude")}
        # The #68 warning rows came from the planner's captured frame; everything else from the binaries.
        captured = {"Heads up, you have less than 10% of your weekly limit left. Run /status for a",
                    "Switch to gpt-5.6-luna for lower credit usage?"}
        for name, (tool, kind, _) in EXPECTED.items():
            if kind != "limited":
                continue
            with self.subTest(frame=name):
                line = limits.classify(frame(name), tool).line
                phrase = line.split(" ", 1)[1].lstrip() if not line[0].isalnum() else line
                fragment = limits.APOSTROPHES.sub("'", phrase[:24])
                source = limits.APOSTROPHES.sub("'", strings[tool])
                template = re.match(r"You've hit your ((?:session|weekly|Opus|Sonnet|Fable) limit)", fragment + phrase[24:])
                if tool == "claude" and template:
                    # Claude composes `You've hit your ${name}` from the limit-name table.
                    self.assertIn("You've hit your", source)
                    self.assertIn(f'"{template.group(1)}"', source)
                    continue
                self.assertTrue(fragment in source, f"{fragment!r} is not in strings-{tool}.txt")
        warning = limits.classify(frame("codex-warning-chooser"), "codex").line
        self.assertTrue(any(c in frame("codex-warning-chooser") for c in captured), warning)

    def test_the_binary_extraction_found_the_documented_strings(self):
        codex = (FIXTURES / "strings-codex.txt").read_text(encoding="utf-8")
        claude = (FIXTURES / "strings-claude.txt").read_text(encoding="utf-8")
        for needle in ("hit your usage limit", "Usage limit reached", "out of credits", "Approaching rate limits",
                       "Heads up, you have less than", "due to usage limits"):
            self.assertIn(needle, codex)
        for needle in ("You've hit your", "Usage limit reached", "usage credit limit reached",
                       'five_hour:"session limit"'):
            self.assertIn(needle, claude)


class Position(unittest.TestCase):
    COMPOSER = "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n> \n" \
               "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n  status\n"

    def test_a_real_limit_below_an_old_tool_call_still_counts(self):
        text = ("\u25cf Bash(git status)\n  \u23bf  clean\n\n> continue with r16\n"
                "  \u23bf  You've hit your session limit \u00b7 resets 3pm\n" + self.COMPOSER)
        self.assertEqual("limited", limits.classify(text, "claude").kind)

    def test_typographic_apostrophes_and_case(self):
        text = "> go\n  \u23bf  You\u2019ve hit your weekly limit \u00b7 resets Mon\n" + self.COMPOSER
        self.assertEqual("limited", limits.classify(text, "claude").kind)
        self.assertEqual("limited", limits.classify(
            "\u25a0 you\u2019ve hit your usage limit.\n\n\u203a Ask Codex to do anything\n", "codex").kind)

    def test_forbidden_prefixes_never_count(self):
        for prefix in ('"', "`", "+ ", "- ", "> ", "# ", "| ", "lib/x.py:3: "):
            with self.subTest(prefix=prefix):
                text = f"{prefix}You\u2019ve hit your usage limit.\n\n\u203a Ask Codex to do anything\n"
                self.assertIsNone(limits.classify(text, "codex"))

    def test_exited_needs_the_phrase_in_the_last_commands_output(self):
        shell = "PS C:\\repo> "
        old = f"\u25a0 You\u2019ve hit your usage limit.\n{shell}codex\n{shell}git status\nclean\n{shell}\n"
        self.assertIsNone(limits.classify(old, "codex"))
        fresh = f"{shell}codex\n\u25a0 You\u2019ve hit your usage limit.\nTo continue this session, run codex resume x\n{shell}\n"
        self.assertTrue(limits.classify(fresh, "codex").exited)

    def test_unknown_tool_is_refused(self):
        with self.assertRaises(ValueError):
            limits.classify("x", "aider")

    def test_tail_hash_follows_the_last_twenty_rows(self):
        base = "\n".join(f"row {i}" for i in range(30))
        self.assertEqual(limits.tail_hash(base), limits.tail_hash("different top\n" + base + "\n\n"))
        self.assertNotEqual(limits.tail_hash(base), limits.tail_hash(base + "\nrow 30"))


class Cli(unittest.TestCase):
    def test_classify_prints_json(self):
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "codex"],
                              input=frame("codex-limited-exited").encode("utf-8"), capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(("limited", True), (result["kind"], result["exited"]))
        self.assertEqual(limits.tail_hash(frame("codex-limited-exited")), result["tail"])
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "claude"],
                              input=frame("claude-grep").encode("utf-8"), capture_output=True)
        self.assertIsNone(json.loads(done.stdout)["kind"])


if __name__ == "__main__":
    unittest.main()
