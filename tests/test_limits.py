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
    # #61: the chooser a fresh pane shows (the #68 frame without its heads-up rows), and the
    # regression case - the same chooser dumped in tool output, with the composer below it
    "codex-warning-chooser-no-heads-up": ("codex", "warning", False),
    "codex-warning-chooser-in-tool-output": ("codex", None, False),
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
    # r17 M1: the agent's own reply glyph never carries a limit
    "claude-reply-starts-with-phrase": ("claude", None, False),
    "claude-reply-second-line-phrase": ("claude", None, False),
    "codex-message-starts-with-phrase": ("codex", None, False),
    # #65: Kimi Code, synthesised around the captured status-error frame (see README.md)
    "kimi-limited-quota": ("kimi", "limited", False),
    "kimi-limited-balance": ("kimi", "limited", False),
    "kimi-limited-usage": ("kimi", "limited", False),
    "kimi-limited-5hour": ("kimi", "limited", False),     # #77: the owner's evaluation, 2026-09-29/30
    # #88: the real docxy #775 frame - the error glued under a collapsed `● Ran a command` - and the
    # same frame in a ~60-column pane, where the error wraps over 5 rows and the hint over 3
    "kimi-limited-5hour-tool": ("kimi", "limited", False),
    "kimi-limited-5hour-narrow": ("kimi", "limited", False),
    # #88 reopened: docxy #820 - the same error twice, under Kimi's todo panel docked over the composer
    "kimi-limited-5hour-todo": ("kimi", "limited", False),
    "kimi-rate-limit-transient": ("kimi", None, False),
    "kimi-retrying": ("kimi", None, False),
    "kimi-tool-output": ("kimi", None, False),
    # FIX r1 m6: the tool call is the last item above the composer, its output ending in error + hint
    "kimi-tool-output-last": ("kimi", None, False),
    # FIX r2 m3: Kimi exited to the shell with a tool's quoted quota error + hint above the prompt
    "kimi-exited-quoted": ("kimi", None, False),
    "kimi-history": ("kimi", None, False),
    "kimi-diff": ("kimi", None, False),
}
KIMI_CAPTURED = ROOT / "tests" / "fixtures" / "kimi"


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
            if kind != "limited" or tool == "kimi":
                continue                 # Kimi's rows are composed from templates: test_kimi_rows_...
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
        kimi = (FIXTURES / "strings-kimi.txt").read_text(encoding="utf-8")
        for needle in ("exceeded_current_quota_error", "exceeded your current (?:token )?quota", "insufficient balance",
                       "this.showStatus(`Error: ${message}`", "return `[${error.code}] ${error.message}`",
                       "If this persists, run `/export-debug-zip`", "Retrying (${retry.nextAttempt}",
                       'PROVIDER_API_ERROR_CODE = "provider.api_error"', '"provider.rate_limit"'):
            self.assertIn(needle, kimi)

    def test_kimi_rows_are_composed_from_the_binarys_templates(self):
        """Kimi draws a session error as showStatus(`Error: ${formatErrorPayload}`) - `[code] message` -
        and then its report hint. The code, the hint and the quota wording come from strings-kimi.txt;
        only the provider's message around the quota words is invented (and marked in README.md)."""
        kimi = (FIXTURES / "strings-kimi.txt").read_text(encoding="utf-8")
        hint = re.search(r'return "(If this persists, run [^"]+)";', kimi).group(1)
        for name, (tool, kind, _) in EXPECTED.items():
            if tool != "kimi":
                continue
            with self.subTest(frame=name):
                text = frame(name)
                error = next(row.strip() for row in text.splitlines() if "Error: [provider." in row)
                code = re.search(r"\[(provider\.[a-z_]+)\]", error).group(1)
                # The strings run may break inside a code: provider.auth_error ends one run as `.auth_error"`.
                self.assertTrue(f'"{code}"' in kimi or f'.{code.split(".", 1)[1]}":' in kimi, code)
                self.assertTrue(" ".join(text.split()).find(" ".join(hint.split())) >= 0 or name == "kimi-diff")
                if kind == "limited" and name not in ("kimi-limited-usage", "kimi-limited-5hour",
                                                         "kimi-limited-5hour-tool", "kimi-limited-5hour-narrow",
                                                         "kimi-limited-5hour-todo"):
                    self.assertTrue(any(phrase in " ".join(text.split()) for phrase in
                                        ("exceeded your current quota", "insufficient balance")), name)

    def test_kimi_captured_frames_are_never_limits(self):
        for path in sorted(KIMI_CAPTURED.glob("*.txt")):
            with self.subTest(frame=path.name):
                self.assertIsNone(limits.classify(path.read_text(encoding="utf-8"), "kimi"))


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

    def test_the_warning_chooser_counts_only_at_the_bottom_of_the_pane(self):
        chooser = frame("codex-warning-chooser")
        self.assertTrue(limits.classify(chooser, "codex").line.startswith("⚠ Heads up"))
        # Answered: the heads-up row stays in history, the chooser is gone, the composer is back.
        answered = chooser.split("\n\n  Approaching")[0] + "\n\n› Ask Codex to do anything\n"
        self.assertIsNone(limits.classify(answered, "codex"))
        # Options with no warning row above them (another picker) never count.
        picker = "• Ran ls\n  └ lib\n\n› 1. Switch to plan mode\n  2. Keep current model\n"
        self.assertIsNone(limits.classify(picker, "codex"))

    def test_kimi_needs_the_idle_composer_and_the_hint_right_below_the_error(self):
        quota = frame("kimi-limited-quota")
        self.assertEqual("limited", limits.classify(quota, "kimi").kind)
        # A dialog replaced the composer: no verdict from a pane nobody can read.
        self.assertIsNone(limits.classify(quota.split(" ╭")[0] + "\n   ▶ 1. Approve once\n", "kimi"))
        # Without the hint row the error row cannot be told from a tool's output.
        no_hint = "\n".join(row for row in quota.splitlines() if "If this persists" not in row)
        self.assertIsNone(limits.classify(no_hint, "kimi"))
        # The same rows classify as nothing for the other tools.
        self.assertIsNone(limits.classify(quota, "claude"))
        self.assertIsNone(limits.classify(quota, "codex"))

    def test_unknown_tool_is_refused(self):
        with self.assertRaises(ValueError):
            limits.classify("x", "aider")

    def test_tail_hash_follows_the_last_twenty_rows(self):
        base = "\n".join(f"row {i}" for i in range(30))
        self.assertEqual(limits.tail_hash(base), limits.tail_hash("different top\n" + base + "\n\n"))
        self.assertNotEqual(limits.tail_hash(base), limits.tail_hash(base + "\nrow 30"))


KIMI_BOX = (" ╭" + "─" * 84 + "╮\n │ >" + " " * 82 + "│\n ╰" + "─" * 84 + "╯\n"
            " Ask When Needed  K2.8 Preview thinking: max  C:\\repo\n")
ERROR_5H = "   Error: [provider.auth_error] 403 You've reached your 5-hour usage limit. Your quota"
ERROR_5H_WRAPPED = [ERROR_5H, " will reset when the current 5-hour window ends."]
HINT = "   If this persists, run `/export-debug-zip` and share the file with us for diagnosis."
HINT_WRAPPED = [HINT, " Please don't share it publicly."]


def kimi(*rows: str) -> str:
    return " ✨ continue\n\n ● Working on it.\n\n" + "\n".join(rows) + "\n\n" + KIMI_BOX


class KimiOwner(unittest.TestCase):
    """#88: a session error glued under a successful tool call is the session's, not the tool's, only
    when the tool's output provably cannot hold it (Kimi 2.1.1's outcomeRows: at most 3 rows, each cut
    to one row, or ONE row marked with `…`), or when a row of it wraps to column 1."""

    def test_the_issue_frame_is_limited_on_the_error_row(self):
        found = limits.classify(frame("kimi-limited-5hour-tool"), "kimi")
        self.assertEqual("limited", found.kind)
        self.assertTrue(found.line.startswith("Error: [provider.auth_error] 403 You've reached your 5-hour"), found.line)
        narrow = limits.classify(frame("kimi-limited-5hour-narrow"), "kimi")
        self.assertEqual("Error: [provider.auth_error] 403 You've reached your", narrow.line)   # its first row

    def test_one_marked_outcome_row_then_the_error(self):
        for outcome in ("   … the last line of a long output", "   the first line of a long output …"):
            with self.subTest(outcome=outcome):
                text = kimi(" ● Ran a command · $ make test", outcome, ERROR_5H, HINT)
                self.assertEqual("limited", limits.classify(text, "kimi").kind)

    def test_a_width_cut_is_not_a_collapse_marker(self):
        text = kimi(" ● Ran a command · $ cat notes.txt", "   every skipped/ignored test by na…", ERROR_5H, HINT)
        self.assertIsNone(limits.classify(text, "kimi"))

    def test_a_marked_row_with_more_output_after_it_is_not_a_collapse(self):
        text = kimi(" ● Ran a command · $ cat notes.txt", "   … earlier", "   more output", ERROR_5H, HINT)
        self.assertIsNone(limits.classify(text, "kimi"))

    def test_output_rows_at_column_3_never_place_the_error(self):
        # A `cat` of a 3-line file: error, hint, hint continuation - all tool output rows at column 3.
        text = kimi(" ● Ran a command · $ cat x.txt", ERROR_5H, HINT, "   Please don't share it publicly.")
        self.assertIsNone(limits.classify(text, "kimi"))

    def test_a_row_wrapped_to_column_1_is_a_status_row(self):
        text = kimi(" ● Ran a command · $ make test", "   ok", *ERROR_5H_WRAPPED, HINT)
        self.assertEqual("limited", limits.classify(text, "kimi").kind)
        text = kimi(" ● Ran a command · $ make test", "   ok", ERROR_5H, *HINT_WRAPPED)
        self.assertEqual("limited", limits.classify(text, "kimi").kind)

    def test_a_failed_call_still_owns_its_rows(self):
        text = kimi(" ✗ Ran a command · $ make test", "   … collapsed", *ERROR_5H_WRAPPED, *HINT_WRAPPED)
        self.assertIsNone(limits.classify(text, "kimi"))


class KimiTurnLimit(unittest.TestCase):
    """#88: the safety nets' rule - the frame ends in a Kimi limit session error, whatever item owns it."""

    def test_the_issue_frames_and_any_owner(self):
        for name in ("kimi-limited-5hour-tool", "kimi-limited-5hour-narrow", "kimi-limited-5hour", "kimi-tool-output-last"):
            with self.subTest(frame=name):
                self.assertEqual("limited", limits.kimi_turn_limit(frame(name)).kind)
        failed = kimi(" ✗ Ran a command · $ make test", "   ok", ERROR_5H, HINT)
        self.assertEqual("limited", limits.kimi_turn_limit(failed).kind)

    def test_only_at_the_bottom_of_an_idle_pane(self):
        for name in ("kimi-tool-output", "kimi-history", "kimi-retrying", "kimi-exited-quoted",
                     "kimi-rate-limit-transient", "kimi-diff"):
            with self.subTest(frame=name):
                self.assertIsNone(limits.kimi_turn_limit(frame(name)))
        for path in sorted(KIMI_CAPTURED.glob("*.txt")):
            with self.subTest(frame=path.name):
                self.assertIsNone(limits.kimi_turn_limit(path.read_text(encoding="utf-8")))
        answered = frame("kimi-limited-5hour-tool").replace(" Please don't share it publicly.\n",
                                                             " Please don't share it publicly.\n\n ● Done.\n")
        self.assertIsNone(limits.kimi_turn_limit(answered))


def todo_panel(*todos: str, more: str | None = None) -> list[str]:
    """Kimi's TodoPanelComponent rows (#88) in its 1-column gutter, as wide as KIMI_BOX."""
    return [" " + "─" * 86, "   Todo", *(f"   {todo}" for todo in todos), *([f"   {more}"] if more else [])]


# In progress `●` rows titled like a tool call (`● Read …`, `● Running …`) are still panel rows.
COLLAPSED = todo_panel("● Read the spec", "● Running tests", "○ Implement the fix", "○ Commit", "○ Reply",
                       more="… +2 more (2 done) · ctrl+t to expand")
EXPANDED = todo_panel(*[f"{'✓' if n < 4 else '●' if n == 4 else '○'} step {n}" for n in range(12)],
                      more="all 12 items · ctrl+t to collapse")
SPINNER = "  ⠹ Thinking… · Tip: /plugins: manage plugins"


def kimi_docked(rows: list[str], panel: list[str]) -> str:
    """`kimi()` with the todo panel docked between `rows` and the box: no blank row below the panel."""
    return kimi(*rows, "", *panel).replace("\n\n ╭", "\n ╭")


class KimiTodoPanel(unittest.TestCase):
    """#88 reopened (docxy #820): Kimi docks its todo panel between the transcript and the composer box,
    so the error and hint are no longer the last rows above the box."""

    def test_the_820_frame_with_and_without_blank_rows_around_the_panel(self):
        rows = frame("kimi-limited-5hour-todo").splitlines()
        rule = next(i for i, row in enumerate(rows) if row.startswith(" ─"))
        box = next(i for i, row in enumerate(rows) if row.startswith(" ╭"))
        self.assertEqual("", rows[rule - 1])
        variants = {
            "as built": rows,
            "no blank above": rows[:rule - 1] + rows[rule:],
            "blank below too": rows[:box] + [""] + rows[box:],
            "blank below only": rows[:rule - 1] + rows[rule:box] + [""] + rows[box:],
        }
        for name, variant in variants.items():
            with self.subTest(variant=name):
                text = "\n".join(variant)
                found = limits.classify(text, "kimi")
                self.assertEqual("limited", found.kind if found else None)
                self.assertTrue(found.line.startswith("Error: [provider.auth_error] 403 You've reached your 5-hour"))
                self.assertEqual("limited", limits.kimi_turn_limit(text).kind)

    def test_the_775_tool_call_layout_under_a_panel(self):
        docked = frame("kimi-limited-5hour-tool").replace(" ╭", "\n".join(COLLAPSED) + "\n ╭", 1)
        self.assertEqual("limited", limits.classify(docked, "kimi").kind)
        self.assertEqual("limited", limits.kimi_turn_limit(docked).kind)

    def test_an_expanded_panel_does_not_count_against_the_window(self):
        rows = frame("kimi-limited-5hour-tool").splitlines()
        start = next(i for i, row in enumerate(rows) if "Error: [provider." in row)
        error = [" ✨ go", *rows[start:start + 5]]                # wrapped over 3 rows, the hint over 2
        self.assertEqual("limited", limits.classify(kimi_docked(error, EXPANDED), "kimi").kind)
        longer = todo_panel(*[f"○ step {n}" for n in range(40)], more="all 40 items · ctrl+t to collapse")
        self.assertEqual("limited", limits.classify(kimi_docked(error, longer), "kimi").kind)

    def test_a_spinner_above_the_panel_is_a_running_turn(self):
        self.assertIsNone(limits.classify(kimi_docked([" ✨ go", ERROR_5H, HINT, "", SPINNER], COLLAPSED), "kimi"))
        self.assertIsNone(limits.kimi_turn_limit(kimi_docked([" ✨ go", ERROR_5H, HINT, "", SPINNER], EXPANDED)))
        # Without a panel, as before.
        self.assertIsNone(limits.classify(kimi(" ✨ go", ERROR_5H, HINT, "", SPINNER), "kimi"))
        self.assertEqual("limited", limits.classify(kimi(" ✨ go", ERROR_5H, HINT), "kimi").kind)

    def test_a_panel_with_no_error_above_it_is_not_a_limit(self):
        self.assertIsNone(limits.classify(kimi_docked([" ● Tests written; implementing now."], COLLAPSED), "kimi"))
        self.assertIsNone(limits.kimi_turn_limit(kimi_docked([" ● Tests written."], EXPANDED)))

    def test_an_item_after_the_error_makes_it_old(self):
        for item in (" ● Resumed: the window reset.", " ✗ Ran a command · $ make test"):
            with self.subTest(item=item):
                text = kimi_docked([" ✨ go", ERROR_5H, HINT, "", item], COLLAPSED)
                self.assertIsNone(limits.classify(text, "kimi"))
                self.assertIsNone(limits.kimi_turn_limit(text))

    def test_only_the_exact_panel_is_removed(self):
        above = [" ✨ go", ERROR_5H, HINT, ""]
        self.assertEqual(above[:-1], limits.kimi_without_todo(above + COLLAPSED))
        self.assertEqual(above[:-1], limits.kimi_without_todo(above + EXPANDED + [""]))
        self.assertEqual(above[:-1], limits.kimi_without_todo(above + todo_panel("✓ Done")))   # no overflow row
        lookalikes = {
            "a non-marker row": todo_panel("- Write tests"),
            "no todo row (clipped)": todo_panel(),
            "no rule": COLLAPSED[1:],
            "no Todo head": COLLAPSED[:1] + COLLAPSED[2:],
            "two overflow rows": COLLAPSED + [COLLAPSED[-1]],
            "a row after the overflow": COLLAPSED + ["   ○ one more"],
            "an item between the rows": COLLAPSED[:3] + [" ✨ go on"] + COLLAPSED[3:],
        }
        for name, panel in lookalikes.items():
            with self.subTest(lookalike=name):
                self.assertEqual(above + panel, limits.kimi_without_todo(above + panel))
                self.assertIsNone(limits.classify(kimi_docked(above[:3], panel), "kimi"))



class PsPromptLast(unittest.TestCase):
    """#98: the relay's exit watch reads only the last row: a crash leaves the agent's frame above it."""

    def test_table(self):
        prompt = "PS C:\\Users\\boris\\source\\workbench\\repo-issue-7> "
        kimi = (FIXTURES.parent / "kimi" / "idle-after-turn.txt").read_text(encoding="utf-8")
        for text, expected in ((prompt, True), (prompt.rstrip(), True), (prompt + "   \n\n", True),
                               ("To resume this session: kimi -r s\n" + prompt, True),
                               (kimi + "\n" + prompt, True),            # a crashed agent's frame above it
                               (prompt + "git status", False), ("PS C:\\x> pwsh -File pane-codex.ps1", False),
                               (prompt + "\nOverwrite? [y/N]", False), (kimi, False), ("❯ ", False),
                               ("PS /home/x> ", False), ("", False), (None, False)):
            with self.subTest(text=(text or "")[-40:]):
                self.assertIs(expected, limits.ps_prompt_last(text))


class Cli(unittest.TestCase):
    def test_classify_prints_json(self):
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "codex"],
                              input=frame("codex-limited-exited").encode("utf-8"), capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(("limited", True), (result["kind"], result["exited"]))
        self.assertEqual(limits.tail_hash(frame("codex-limited-exited")), result["tail"])
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "kimi"],
                              input=frame("kimi-limited-quota").encode("utf-8"), capture_output=True)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("limited", json.loads(done.stdout)["kind"])
        done = subprocess.run([sys.executable, str(ROOT / "lib" / "limits.py"), "classify", "--tool", "claude"],
                              input=frame("claude-grep").encode("utf-8"), capture_output=True)
        self.assertIsNone(json.loads(done.stdout)["kind"])


if __name__ == "__main__":
    unittest.main()
