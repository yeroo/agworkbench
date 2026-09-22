"""The relay's decisions, with no terminal and no network.

The relay's effects (typing into panes, calling gh) are thin; what matters is WHEN it acts. These
tests pin that: which PR changes produce mail, that a re-read of the same PR produces none, and that
Claude is never rung while it is mid-turn.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import relay  # noqa: E402

OPEN = {"number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN", "reviewDecision": "",
        "reviews": [], "comments": [], "inline": []}


def with_(base: dict, **changes) -> dict:
    out = dict(base)
    out.update(changes)
    return out


class Busy(unittest.TestCase):
    def test_a_running_turn_is_busy(self):
        self.assertTrue(relay.is_busy("output\n\n✢ Working… (12s · esc to interrupt)\n\n>\n"))

    def test_an_idle_composer_is_not_busy(self):
        self.assertFalse(relay.is_busy("done.\n\n────────\n>\n────────\n  footer\n"))

    def test_only_the_bottom_of_the_pane_counts(self):
        # "esc to interrupt" scrolled far up in history is an old turn, not the current one.
        old = "Working (esc to interrupt)\n" + "line\n" * 40 + ">\n"
        self.assertFalse(relay.is_busy(old))


class PrEvents(unittest.TestCase):
    def test_nothing_before_a_pr_exists(self):
        self.assertEqual([], relay.pr_events(None, None))

    def test_a_new_pr_is_announced_once(self):
        events = relay.pr_events(None, OPEN)
        self.assertEqual(1, len(events))
        self.assertIn("PR #7 is open", events[0]["subject"])
        self.assertEqual([], relay.pr_events(OPEN, OPEN), "re-reading the same PR must stay silent")

    def test_a_new_review_is_reported_with_its_state_and_author(self):
        review = {"id": "R1", "state": "CHANGES_REQUESTED", "author": {"login": "yeroo"}, "body": "fix x"}
        events = relay.pr_events(OPEN, with_(OPEN, reviews=[review]))
        self.assertEqual(1, len(events))
        self.assertIn("yeroo", events[0]["subject"])
        self.assertIn("CHANGES_REQUESTED", events[0]["subject"])
        self.assertEqual("fix x", events[0]["body"])

    def test_a_line_comment_names_the_file_and_line(self):
        inline = {"id": 55, "user": {"login": "yeroo"}, "path": "app/x.py", "line": 12, "body": "why?"}
        events = relay.pr_events(OPEN, with_(OPEN, inline=[inline]))
        self.assertIn("app/x.py:12", events[0]["subject"])

    def test_a_conversation_comment_is_reported(self):
        comment = {"id": "C1", "author": {"login": "yeroo"}, "body": "looks good"}
        events = relay.pr_events(OPEN, with_(OPEN, comments=[comment]))
        self.assertEqual("looks good", events[0]["body"])

    def test_an_approval_decision_is_reported(self):
        events = relay.pr_events(OPEN, with_(OPEN, reviewDecision="APPROVED"))
        self.assertTrue(any("APPROVED" in e["subject"] for e in events))

    def test_merge_ends_the_loop(self):
        merged = with_(OPEN, state="MERGED", mergedAt="2026-09-22T10:00:00Z")
        events = relay.pr_events(OPEN, merged)
        self.assertTrue(any("MERGED" in e["subject"] for e in events))
        self.assertTrue(relay.finished(merged))

    def test_a_close_without_merge_also_ends_it_and_says_so(self):
        closed = with_(OPEN, state="CLOSED")
        events = relay.pr_events(OPEN, closed)
        self.assertTrue(any("CLOSED without merging" in e["subject"] for e in events))
        self.assertTrue(relay.finished(closed))

    def test_an_open_pr_is_not_finished(self):
        self.assertFalse(relay.finished(OPEN))
        self.assertFalse(relay.finished(None))

    def test_old_reviews_are_not_re_announced_when_a_new_one_lands(self):
        first = {"id": "R1", "state": "COMMENTED", "author": {"login": "a"}, "body": "one"}
        second = {"id": "R2", "state": "APPROVED", "author": {"login": "a"}, "body": "two"}
        events = relay.pr_events(with_(OPEN, reviews=[first]), with_(OPEN, reviews=[first, second]))
        self.assertEqual(["two"], [e["body"] for e in events if e["kind"] == "review"])


class Pointer(unittest.TestCase):
    def test_the_pointer_carries_the_id_and_the_read_command_but_never_the_body(self):
        message = {"id": "20260922T1-codex-ab12", "from": "codex", "subject": "IMPLEMENTED abc123",
                   "body": "a long body that must stay in the file"}
        text = relay.pointer_text(message, Path("C:/wb/lib/agmsg.py"), Path("C:/clone/.workbench"))
        self.assertIn("20260922T1-codex-ab12", text)
        self.assertIn("read 20260922T1-codex-ab12", text)
        self.assertNotIn("long body", text)

    def test_the_pointer_is_one_line(self):
        message = {"id": "x", "from": "github", "subject": "PR #7 is open"}
        self.assertNotIn(chr(10), relay.pointer_text(message, Path("a"), Path("b")))


if __name__ == "__main__":
    unittest.main()
