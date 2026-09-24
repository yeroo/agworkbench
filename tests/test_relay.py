"""The relay's decisions, with no terminal and no network.

The relay's effects (typing into panes, calling gh) are thin; what matters is WHEN it acts. These
tests pin that: which PR changes produce mail, that a re-read of the same PR produces none, and that
Claude is never rung while it is mid-turn.
"""

from __future__ import annotations

import sys
import os
import copy
import errno
import json
import shutil
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import relay  # noqa: E402
import agw
import hub
import peerchat
from frames import CLAUDE_IDLE, CLAUDE_RUNNING, CLAUDE_SUGGESTION, CODEX_IDLE, Clock, FakeAgw, claude, codex, stable_frames

BOUNDARY = '2026-09-24T16:00:00+00:00'
OLD_CREATED = '2026-09-24T15:00:00Z'
OPEN = {"number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN", "reviewDecision": "",
        "createdAt": OLD_CREATED,
        "reviews": [], "comments": [], "inline": [], "headRefName": "issue-6", "isCrossRepository": False}


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

    def test_a_close_without_merge_also_ends_it_and_says_so(self):
        closed = with_(OPEN, state="CLOSED")
        events = relay.pr_events(OPEN, closed)
        self.assertTrue(any("CLOSED without merging" in e["subject"] for e in events))

    def test_preexisting_or_retired_terminal_pr_does_not_finish(self):
        for state in ['MERGED', 'CLOSED']:
            terminal = with_(OPEN, state=state)
            self.assertEqual([], relay.pr_events(None, terminal))

    def test_switching_pr_numbers_starts_a_new_event_history(self):
        review = {'id': 'R1', 'author': {'login': 'a'}, 'state': 'APPROVED', 'body': 'new'}
        old = with_(OPEN, number=51, reviews=[review])
        new = with_(OPEN, number=53, reviews=[review])
        events = relay.pr_events(old, new)
        self.assertEqual('PR #53 is open', events[0]['subject'])
        self.assertEqual('new', events[1]['body'])
        self.assertEqual([], relay.pr_events(old, with_(new, state='MERGED')))

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


class PaneIds(unittest.TestCase):
    GOOD = "461a2dd0-4f22-49c3-a724-90e3b2cde3db"

    def test_a_single_character_is_not_a_pane(self):
        # the exact value the first live run produced
        with self.assertRaises(SystemExit):
            relay.check_panes("4", self.GOOD)

    def test_the_two_agents_cannot_share_a_pane(self):
        with self.assertRaises(SystemExit):
            relay.check_panes(self.GOOD, self.GOOD)

    def test_two_distinct_pane_ids_pass(self):
        relay.check_panes(self.GOOD, "d387360b-a120-4e4b-b7a4-4db3171780ab")


class DeliveryFixture(unittest.TestCase):
    def setUp(self):
        self.peer = relay.Peer('codex', 'codex', 'codex-pane')
        # Run the constructor, replacing only mailbox storage boundaries. No real hub is touched.
        with patch.dict(os.environ), patch.object(hub, 'reload_paths'), \
                patch.object(relay.Relay, '_load', return_value={'announced': [], 'pr': None,
                                                               'branch': 'issue-6', 'watch_since': BOUNDARY}):
            self.r = relay.Relay(Path('fixture-hub'), [self.peer], 'o/r', 'issue-6', 5, 60)
        self.messages = {'m1': {'id': 'm1', 'from': 'claude', 'subject': 'review'}}
        self.unread = {'codex': ['m1']}
        self.r.hub = SimpleNamespace(unread=lambda box: [Path(mid + '.md') for mid in self.unread.get(box, [])],
                                     parse_message=lambda path: self.messages[path.stem])
        self.r._save = Mock()
        self.logs = []
        self.r.log = self.logs.append
        self.t = 0
        self.enterContext(patch.object(relay, 'now', lambda: self.t))
        # An accidental real terminal request is a hard test failure.
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('real terminal request')))
        self.status = self.enterContext(patch.object(agw, 'set_status'))
        self.notify = self.enterContext(patch.object(agw, 'notify'))
        self.pane = self.enterContext(patch.object(agw, 'pane_text', return_value=CLAUDE_IDLE))
        self.real_send = peerchat.send
        self.send = self.enterContext(patch.object(peerchat, 'send', return_value='submitted'))
        self.gh = self.enterContext(patch.object(relay.subprocess, 'run',
                                                side_effect=AssertionError('real GitHub request')))

    def tick(self, instant):
        self.t = instant
        self.r.deliver_mail()

    def use_disk_state(self):
        folder = Path(__file__).resolve().parent.parent / ('test relay ' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        self.r.hub_dir = folder
        self.r.state_file = folder / 'state' / 'relay.json'
        self.r._save = relay.Relay._save.__get__(self.r)

    def restart_from_disk(self, *, dry_run=False):
        previous = self.r
        with patch.dict(os.environ), patch.object(hub, 'reload_paths'), \
                patch.object(hub, 'unread', side_effect=previous.hub.unread), \
                patch.object(relay.Relay, 'log', side_effect=self.logs.append):
            self.r = relay.Relay(previous.hub_dir, previous.peers, previous.repo, previous.branch,
                                 previous.mail_interval, previous.pr_interval, dry_run=dry_run)
        self.r.hub = previous.hub
        self.r.log = self.logs.append
        self.r.fetch_pr = Mock(return_value=None)  # GitHub unavailable after the restart.

    def assert_unannounced(self):
        self.assertEqual([], self.r.state['announced'])
        self.r._save.assert_not_called()
        self.assertFalse(any(line.startswith('rang ') for line in self.logs))


class Delivery(DeliveryFixture):
    def test_saved_pr_from_another_branch_is_discarded_before_startup(self):
        self.use_disk_state()
        self.r.state = {'pr': with_(OPEN, number=8, state='MERGED', headRefName='issue-3-launcher-log-resume'),
                        'terminal_mail': [['codex', 'old-final']], 'announced': ['m1'],
                        'reset_pending': ['codex']}
        self.r._save()
        self.restart_from_disk()
        kept = {'announced': ['m1'], 'reset_pending': ['codex'], 'branch': 'issue-6'}
        self.assertEqual(kept, self.r.state)
        self.assertEqual(kept, json.loads(self.r.state_file.read_text(encoding='utf-8')))
        self.assertTrue(self.r.holds[('codex', '')].clear_pending)
        self.assertIn('discarding saved PR state for branch issue-3-launcher-log-resume '
                      '(this relay watches issue-6)', self.logs)
        self.r.fetch_pr.return_value = relay.PrFetch(copy.deepcopy(OPEN))
        self.r.hub.write_message = Mock(return_value=Path('new-pr.md'))
        self.r.stop_file = SimpleNamespace(exists=lambda: self.r.fetch_pr.call_count > 0)
        with patch.object(relay, 'pause'):
            self.assertEqual(0, self.r.run())
        self.r.fetch_pr.assert_called_once()
        self.r.hub.write_message.assert_called_once()
        self.assertEqual('PR #7 is open', self.r.hub.write_message.call_args.kwargs['subject'])
        self.assertFalse(any('resuming final notice drain' in line for line in self.logs))

    def test_saved_pr_without_branch_is_also_discarded(self):
        self.use_disk_state()
        self.r.state['pr'] = {'number': 8, 'state': 'MERGED'}
        self.r.state['terminal_mail'] = [['codex', 'old-final']]
        self.r._save()
        self.restart_from_disk()
        self.assertIsNone(self.r.state.get('pr'))
        self.assertNotIn('terminal_mail', self.r.state)

    def test_dry_run_discards_stale_snapshot_only_in_memory(self):
        self.use_disk_state()
        self.r.state['pr'] = with_(OPEN, state='MERGED', headRefName='old-branch')
        self.r.state['terminal_mail'] = [['codex', 'old-final']]
        self.r._save()
        saved = self.r.state_file.read_bytes()
        self.restart_from_disk(dry_run=True)
        self.assertIsNone(self.r.state.get('pr'))
        self.assertNotIn('terminal_mail', self.r.state)
        self.assertEqual(saved, self.r.state_file.read_bytes())

    def test_mail_moved_between_glob_and_read_is_skipped(self):
        self.messages['m2'] = {'id': 'm2', 'subject': 'second'}
        self.unread['codex'].append('m2')
        self.r.hub.parse_message = Mock(side_effect=[FileNotFoundError('read elsewhere'), self.messages['m2']])
        self.tick(0)
        self.assertEqual(['m2'], self.r.state['announced'])
        self.send.assert_called_once()

    def test_terminal_unread_check_never_opens_message_files(self):
        self.r.state['terminal_mail'] = [['codex', 'm1']]
        self.r.hub.parse_message = Mock(side_effect=FileNotFoundError('moved after glob'))
        self.assertEqual({('codex', 'm1')}, self.r.pending_terminal_mail())
        self.unread['codex'] = []
        self.assertEqual(set(), self.r.pending_terminal_mail())
        self.r.hub.parse_message.assert_not_called()

    def test_failed_ring_alerts_throttles_and_recovers(self):
        self.send.side_effect = peerchat.Failed("pointer still unsent in composer: 'the pointer'")
        self.tick(0)
        self.assert_unannounced()
        self.status.assert_called_once_with('blocked', sound=True, blink=True, pane_id=self.peer.pane)
        self.assertEqual(1, self.notify.call_count)
        notice = self.notify.call_args.args[1]
        for text in ['codex', 'm1', 'the pointer']:
            self.assertIn(text, notice)
        self.assertTrue(any(line.startswith('FAILED ringing codex for m1:') for line in self.logs))
        self.tick(299)
        self.assertEqual(1, self.notify.call_count)
        self.tick(300)
        self.assertEqual(2, self.notify.call_count)
        self.send.side_effect = None
        self.tick(310)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.r._save.assert_called_once()
        self.assertIn('rang codex for m1 (review) [submitted] after holding 310s', self.logs)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual({}, self.r.holds)

    def test_draft_hold_alerts_after_a_minute_and_clears_on_success(self):
        self.send.side_effect = peerchat.Refused("composer holds a draft: 'check if codex replied'")
        self.tick(0)
        self.tick(30)
        self.notify.assert_not_called()
        self.status.assert_not_called()
        self.tick(61)
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('check if codex replied', self.notify.call_args.args[1])
        self.send.side_effect = None
        self.tick(90)
        self.assertIn('rang codex for m1 (review) [submitted] after holding 90s', self.logs)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def claude_peer(self):
        self.peer = relay.Peer('claude', 'claude', 'claude-pane')
        self.r.peers = [self.peer]
        self.unread = {'claude': ['m1']}

    def test_real_ambiguous_composer_never_types_and_alerts_after_ten_minutes(self):
        self.claude_peer()
        fake = FakeAgw('claude', frames=[CLAUDE_SUGGESTION], cursors=[2])
        self.send.side_effect = self.real_send
        with patch.object(agw, 'pane_text', fake.pane_text), patch.object(agw, 'type_into', fake.type_into), \
                patch.object(agw, 'cursor_column', fake.cursor_column):
            self.tick(0)
            self.tick(540)
            self.notify.assert_not_called()
            self.status.assert_not_called()
            self.tick(600)
        self.assertEqual([], fake.keys)
        self.assertEqual(1, self.notify.call_count)
        self.status.assert_called_once_with('blocked', sound=True, blink=True, pane_id=self.peer.pane)
        self.assertIn('possibly a suggestion', self.notify.call_args.args[1])
        self.assert_unannounced()

    def test_full_ambiguous_text_controls_timer_not_truncated_reason(self):
        self.claude_peer()
        first = peerchat.AmbiguousComposer('x' * 60 + 'first')
        second = peerchat.AmbiguousComposer('x' * 60 + 'second')
        self.assertEqual(str(first), str(second))
        self.send.side_effect = first
        self.tick(0)
        self.send.side_effect = second
        self.tick(300)
        self.tick(600)
        self.tick(899)
        self.notify.assert_not_called()
        entry = self.r.holds[('claude', 'm1')]
        self.assertEqual(0, entry.first_at)
        self.assertEqual(300, entry.condition_since)
        self.assertEqual(second.content, entry.ambiguous_text)
        self.tick(900)
        self.assertEqual(1, self.notify.call_count)

    def test_changing_ambiguity_keeps_prewrite_alert_quiet(self):
        self.claude_peer()
        for t in range(0, 1801, 200):
            self.send.side_effect = peerchat.AmbiguousComposer(f'new text at {t}')
            self.tick(t)
        self.notify.assert_not_called()
        self.status.assert_not_called()

    def test_leaving_ambiguity_starts_normal_threshold_without_resetting_total_hold(self):
        self.claude_peer()
        self.send.side_effect = peerchat.AmbiguousComposer('text')
        self.tick(0)
        self.tick(500)
        self.send.side_effect = peerchat.Refused('composer holds a draft')
        self.tick(550)
        self.tick(609)
        self.notify.assert_not_called()
        self.tick(610)
        self.assertEqual(1, self.notify.call_count)
        self.assertEqual(0, self.r.holds[('claude', 'm1')].first_at)
        self.assertEqual(550, self.r.holds[('claude', 'm1')].condition_since)

    def test_alerted_draft_becoming_ambiguous_retains_idle_reset_when_read(self):
        self.claude_peer()
        self.send.side_effect = peerchat.Refused('composer holds a draft')
        self.tick(0)
        self.tick(60)
        entry = self.r.holds[('claude', 'm1')]
        self.send.side_effect = peerchat.AmbiguousComposer('text')
        self.tick(65)
        self.assertIs(entry, self.r.holds[('claude', 'm1')])
        self.assertEqual(60, entry.last_alert_at)
        self.assertTrue(entry.alerted)
        self.unread['claude'] = []
        self.tick(70)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual({}, self.r.holds)
        self.assertEqual(1, self.notify.call_count)

    def test_ambiguity_transitions_preserve_existing_alert_throttle(self):
        self.claude_peer()
        self.send.side_effect = peerchat.AmbiguousComposer('text')
        self.tick(0)
        self.tick(600)
        self.send.side_effect = peerchat.Refused('composer holds a draft')
        self.tick(610)
        self.tick(670)
        self.assertEqual(1, self.notify.call_count)
        self.tick(900)
        self.assertEqual(2, self.notify.call_count)

    def test_post_submit_ambiguity_still_alerts_failure_immediately(self):
        self.claude_peer()
        pointer = peerchat.compose_text('Chat from Workbench: ',
                                        relay.pointer_text(self.messages['m1'], self.r.agmsg, self.r.hub_dir))
        # The relay reads the first frame for busy state before peerchat's own precheck.
        fake = FakeAgw('claude', frames=[CLAUDE_IDLE] + stable_frames(CLAUDE_IDLE, claude(pointer), CLAUDE_SUGGESTION), cursors=[2])
        clock = Clock()
        self.send.side_effect = self.real_send
        with patch.object(agw, 'pane_text', fake.pane_text), patch.object(agw, 'type_into', fake.type_into), \
                patch.object(agw, 'cursor_column', fake.cursor_column), \
                patch.object(peerchat, 'now', clock.now), patch.object(peerchat, 'pause', clock.pause):
            self.tick(0)
        self.assertEqual([pointer, '\n'], fake.keys)
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('submit not proven', self.notify.call_args.args[1])
        self.assert_unannounced()

    def test_mid_turn_and_changed_reason_share_the_original_hold_and_throttle(self):
        self.peer = relay.Peer('claude', 'claude', 'claude-pane')
        self.r.peers = [self.peer]
        self.unread = {'claude': ['m1']}
        self.pane.return_value = CLAUDE_RUNNING
        self.tick(0)
        self.tick(70)
        self.send.assert_not_called()
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('mid-turn', self.notify.call_args.args[1])
        self.pane.return_value = CLAUDE_IDLE
        self.send.side_effect = peerchat.Refused('composer holds a draft')
        self.tick(80)
        self.tick(369)
        self.assertEqual(1, self.notify.call_count)
        self.tick(370)
        self.assertEqual(2, self.notify.call_count)
        self.assertIn('composer holds a draft', self.notify.call_args.args[1])
        self.assertEqual(0, self.r.holds[('claude', 'm1')].first_at)

    def test_prewrite_terminal_failures_also_alert_after_a_minute(self):
        self.send.side_effect = agw.CtlError('unreachable')
        self.tick(0)
        self.tick(70)
        self.assert_unannounced()
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('terminal not reachable', self.notify.call_args.args[1])

    def test_failed_then_refused_has_one_alert_throttle(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = peerchat.Refused('composer holds the pointer')
        self.tick(5)
        self.tick(299)
        self.assertEqual(1, self.notify.call_count)
        self.tick(300)
        self.assertEqual(2, self.notify.call_count)
        self.assert_unannounced()

    def test_independently_read_mail_clears_its_alert(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.unread['codex'] = []
        self.tick(5)
        self.assertEqual({}, self.r.holds)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assert_unannounced()

    def test_failed_idle_reset_is_retried_without_another_ring(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = None
        self.status.side_effect = [agw.CtlError('reset unavailable'), None]
        self.tick(5)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.assertTrue(self.r.holds[('codex', 'm1')].clear_pending)
        sends = self.send.call_count
        self.tick(10)
        self.assertEqual(sends, self.send.call_count)
        self.assertEqual({}, self.r.holds)
        self.assertEqual([call('idle', pane_id=self.peer.pane)] * 2, self.status.call_args_list[-2:])
        self.assertIn('cleared hold codex for m1; last reason: submit failed', self.logs)

    def test_independently_read_mail_keeps_retrying_failed_status_reset(self):
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.unread['codex'] = []
        self.status.side_effect = [OSError('reset unavailable'), None]
        self.tick(5)
        self.assertTrue(self.r.holds[('codex', 'm1')].clear_pending)
        self.tick(10)
        self.assertEqual({}, self.r.holds)
        self.assertEqual(1, self.send.call_count)

    def test_pending_status_reset_survives_a_real_restart(self):
        self.use_disk_state()
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = None
        self.status.side_effect = [agw.CtlError('reset unavailable'), agw.CtlError('still unavailable'), None]
        self.tick(5)
        stored = json.loads(self.r.state_file.read_text(encoding='utf-8'))
        self.assertEqual(['codex'], stored['reset_pending'])
        self.assertEqual(['m1'], stored['announced'])
        self.restart_from_disk()
        self.send.reset_mock()
        self.tick(10)
        self.assertEqual(['codex'], json.loads(self.r.state_file.read_text(encoding='utf-8'))['reset_pending'])
        self.tick(15)
        self.assertEqual([], json.loads(self.r.state_file.read_text(encoding='utf-8'))['reset_pending'])
        self.assertEqual({}, self.r.holds)
        self.send.assert_not_called()
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def test_closed_filename_in_line_comment_is_not_a_terminal_event(self):
        self.r.peers.append(relay.Peer('claude', 'claude', 'claude-pane'))
        self.r.state['pr'] = copy.deepcopy(OPEN)
        inline = {'id': 55, 'user': {'login': 'a'}, 'path': '0004-CLOSED.md', 'line': 12, 'body': 'fix'}
        self.r.fetch_pr = Mock(return_value=relay.PrFetch(with_(OPEN, inline=[inline])))
        self.r.hub.write_message = Mock(return_value=Path('comment.md'))
        self.assertFalse(self.r.watch_pr())
        self.r.hub.write_message.assert_called_once()
        self.assertEqual('claude', self.r.hub.write_message.call_args.kwargs['to'])
        self.assertEqual([], self.r.state['terminal_mail'])

    def test_other_alerted_message_prevents_early_status_clear(self):
        self.messages['m2'] = {'id': 'm2', 'from': 'claude', 'subject': 'second'}
        self.unread['codex'].append('m2')
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.send.side_effect = ['submitted', peerchat.Refused('holds a draft')]
        self.tick(5)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.assertNotIn(call('idle', pane_id=self.peer.pane), self.status.call_args_list)
        self.send.side_effect = None
        self.tick(10)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual(['m1', 'm2'], self.r.state['announced'])

    def test_unalerted_hold_does_not_change_agent_status_on_success(self):
        self.send.side_effect = peerchat.Refused('a draft')
        self.tick(0)
        self.send.side_effect = None
        self.tick(5)
        self.status.assert_not_called()
        self.assertEqual({}, self.r.holds)

    def test_alert_errors_are_independent_and_do_not_end_delivery(self):
        self.messages['m2'] = {'id': 'm2', 'subject': 'second'}
        self.unread['codex'].append('m2')
        self.send.side_effect = [peerchat.Failed('submit failed'), 'submitted']
        self.status.side_effect = agw.CtlError('status broken')
        self.notify.side_effect = agw.CtlError('notify broken')
        self.tick(0)
        self.notify.assert_called_once()
        self.assertEqual(['m2'], self.r.state['announced'])
        self.assertTrue(any('could not set blocked status' in line for line in self.logs))
        self.assertTrue(any('could not notify' in line for line in self.logs))
        self.assertIn(('codex', 'm1'), self.r.holds)

    def test_holds_are_keyed_by_recipient_and_message(self):
        self.r.peers.append(relay.Peer('claude', 'claude', 'claude-pane'))
        self.unread['claude'] = ['m1']
        self.send.side_effect = peerchat.Failed('submit failed')
        self.tick(0)
        self.assertEqual({('claude', 'm1'), ('codex', 'm1')}, set(self.r.holds))
        self.assertEqual(2, self.notify.call_count)

    def test_real_send_refuses_stuck_text_then_rerings_after_composer_empties(self):
        pointer = peerchat.compose_text('Chat from Workbench: ',
                                        relay.pointer_text(self.messages['m1'], self.r.agmsg, self.r.hub_dir))
        fake = FakeAgw(after=lambda f: codex(pointer, 'Working (esc to interrupt)'))
        clock = Clock()
        self.send.side_effect = self.real_send
        with patch.object(agw, 'pane_text', fake.pane_text), patch.object(agw, 'type_into', fake.type_into), \
                patch.object(agw, 'cursor_column', fake.cursor_column), \
                patch.object(peerchat, 'now', clock.now), patch.object(peerchat, 'pause', clock.pause):
            self.tick(0)
            self.assert_unannounced()
            self.assertEqual([pointer, '\t', '\t', '\t'], fake.keys)
            self.tick(5)
            self.assertEqual([pointer, '\t', '\t', '\t'], fake.keys)
            self.assertEqual(1, self.notify.call_count)
            fake.frames = stable_frames(CODEX_IDLE, codex(pointer), CODEX_IDLE)
            self.tick(10)
        self.assertEqual([pointer, '\t', '\t', '\t', pointer, '\t'], fake.keys)
        self.assertEqual(['m1'], self.r.state['announced'])
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def test_dry_run_does_not_send_or_raise_terminal_alerts(self):
        self.r.dry_run = True
        self.r.peers = [relay.Peer('claude', 'claude', 'claude-pane')]
        self.unread = {'claude': ['m1']}
        self.pane.return_value = CLAUDE_RUNNING
        self.tick(0)
        self.tick(70)
        self.send.assert_not_called()
        self.status.assert_not_called()
        self.notify.assert_not_called()

    def test_dry_run_leaves_disk_state_and_mail_untouched(self):
        folder = Path(__file__).resolve().parent.parent / ('test relay ' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        self.r.state_file = folder / 'relay.json'
        self.r.state['pr'] = copy.deepcopy(OPEN)
        self.r.state['seen_open'] = [7]
        before = copy.deepcopy(self.r.state)
        self.r.state_file.write_text(json.dumps(before), encoding='utf-8')
        original_bytes = self.r.state_file.read_bytes()
        self.r._save = relay.Relay._save.__get__(self.r)
        self.r.hub.write_message = Mock(side_effect=AssertionError('dry run filed mail'))
        self.r.fetch_pr = Mock(return_value=relay.PrFetch(with_(OPEN, state='MERGED', mergedAt='now')))
        self.r.dry_run = True
        self.tick(0)
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(before, self.r.state)
        self.assertEqual(original_bytes, self.r.state_file.read_bytes())
        self.r.hub.write_message.assert_not_called()
        self.send.assert_not_called()


def rest_pr(number, *, state='open', repo='o/r', merged=False, created=OLD_CREATED):
    return {'number': number, 'state': state, 'head': {'repo': {'full_name': repo}, 'ref': 'issue-6'},
            'merged_at': 'now' if merged else None, 'created_at': created}


class GithubLookup(DeliveryFixture):
    def setUp(self):
        super().setUp()
        self.open_pages = [[]]
        self.history_pages = [[]]
        self.inline_pages = [[]]
        self.views = {}
        self.date_response = SimpleNamespace(returncode=0, stdout=
            'HTTP/2.0 200 OK\r\nDate: Thu, 24 Sep 2026 16:00:00 GMT\r\n\r\n{}')
        self.r.hub.write_message = Mock(return_value=Path('event.md'))
        self.gh.side_effect = self.command

    def command(self, argv, **kwargs):
        self.assertEqual('gh', argv[0])
        if argv == ['gh', 'api', '-i', 'rate_limit']:
            return self.date_response
        if argv[1] == 'api':
            self.assertEqual(['--paginate', '--slurp'], argv[3:])
            url = urlsplit(argv[2])
            if url.path.endswith('/comments'):
                data = self.inline_pages
            else:
                self.assertEqual('repos/o/r/pulls', url.path)
                query = parse_qs(url.query)
                self.assertEqual(['o:' + self.r.branch], query['head'])
                self.assertEqual(['100'], query['per_page'])
                data = self.open_pages if query['state'] == ['open'] else self.history_pages
        else:
            self.assertEqual(['pr', 'view'], argv[1:3])
            self.assertEqual(['--repo', 'o/r', '--json', relay.PR_FIELDS], argv[4:])
            data = self.views[int(argv[3])]
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))

    def test_pages_are_decoded_before_selecting_highest_same_repository_pr(self):
        self.open_pages = [[rest_pr(51)], [rest_pr(53, repo='O/R'), rest_pr(99, repo='o/fork')]]
        self.views[53] = with_(OPEN, number=53)
        self.inline_pages = [[{'id': 1}], [{'id': 2}]]
        snapshot = self.r.fetch_pr().snapshot
        self.assertEqual(53, snapshot['number'])
        self.assertEqual([{'id': 1}, {'id': 2}], snapshot['inline'])
        commands = [c.args[0] for c in self.gh.call_args_list]
        self.assertEqual(3, len(commands))
        self.assertEqual('53', commands[1][3])
        self.assertEqual('repos/o/r/pulls/53/comments', commands[2][2])

    def test_selection_is_empty_without_an_eligible_open_pr(self):
        for candidates in [[], [rest_pr(51, state='closed')], [rest_pr(99, repo='other/r')],
                           [{'number': 99, 'state': 'open', 'head': {'repo': None}}]]:
            self.assertIsNone(relay.select_open(candidates, 'o/r'))

    def test_same_repository_pr_does_not_require_owner_as_author(self):
        candidate = rest_pr(53)
        candidate['user'] = {'login': 'another-user'}
        self.open_pages = [[candidate]]
        self.views[53] = with_(OPEN, number=53)
        self.assertEqual(53, self.r.fetch_pr().snapshot['number'])

    def test_empty_open_list_looks_up_the_watched_number_directly(self):
        self.r.state.update(pr=with_(OPEN, number=53), seen_open=[53])
        self.views[53] = with_(OPEN, number=53, state='MERGED')
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(3, self.gh.call_count)
        self.assertEqual('PR #53 MERGED - the loop is complete',
                         self.r.hub.write_message.call_args.kwargs['subject'])

    def test_direct_lookup_can_still_return_open(self):
        self.r.state.update(pr=with_(OPEN, number=53), seen_open=[53])
        self.views[53] = with_(OPEN, number=53)
        self.assertFalse(self.r.watch_pr())
        self.r.hub.write_message.assert_not_called()

    def test_preexisting_finished_pr_stays_silent_across_polls_and_restart(self):
        self.use_disk_state()
        self.history_pages = [[rest_pr(51, state='closed', merged=True),
                               rest_pr(52, state='closed'), rest_pr(99, state='closed', repo='o/fork')]]
        self.assertFalse(self.r.watch_pr())
        self.assertEqual([51, 52], self.r.state['ignored_prs'])
        self.history_pages[0][0]['comments'] = [{'id': 1, 'body': 'late comment'}]
        self.assertFalse(self.r.watch_pr())
        self.restart_from_disk()
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.assertFalse(self.r.watch_pr())
        ignored_logs = [line for line in self.logs if line.startswith('ignoring finished')]
        self.assertEqual(2, len(ignored_logs))
        self.r.hub.write_message.assert_not_called()
        self.assertIsNone(self.r.state.get('pr'))

    def test_open_pr_arriving_in_history_pass_is_not_recorded_as_finished(self):
        self.history_pages = [[rest_pr(53)]]
        self.assertFalse(self.r.watch_pr())
        self.assertNotIn('ignored_prs', self.r.state)
        self.open_pages = [[rest_pr(53)]]
        self.views[53] = with_(OPEN, number=53)
        self.assertFalse(self.r.watch_pr())
        self.assertEqual([53], self.r.state['seen_open'])
        self.assertEqual('PR #53 is open', self.r.hub.write_message.call_args.kwargs['subject'])

    def test_branch_with_no_prs_waits(self):
        self.assertFalse(self.r.watch_pr())
        self.r.hub.write_message.assert_not_called()
        self.r._save.assert_not_called()

    def test_completed_pr_is_not_looked_up_or_finished_again(self):
        self.r.state.update(pr=with_(OPEN, number=51, state='MERGED'), seen_open=[51], completed_prs=[51])
        self.history_pages = [[rest_pr(51, state='closed', merged=True)]]
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(2, self.gh.call_count)
        self.assertFalse(self.logs)
        self.r.hub.write_message.assert_not_called()

    def test_view_rejects_a_fork_or_different_branch(self):
        self.open_pages = [[rest_pr(53)]]
        for changes in [{'isCrossRepository': True}, {'headRefName': 'different-branch'}]:
            self.views[53] = with_(OPEN, number=53, **changes)
            self.assertFalse(self.r.watch_pr())
        self.r.hub.write_message.assert_not_called()
        self.assertEqual(4, self.gh.call_count)  # no inline fetch

    def test_preboundary_pr_finishing_between_list_and_view_never_files_events(self):
        self.open_pages = [[rest_pr(53)]]
        self.views[53] = with_(OPEN, number=53, state='MERGED')
        self.assertFalse(self.r.watch_pr())
        self.views[53]['comments'] = [{'id': 'late', 'body': 'comment after merge'}]
        self.assertFalse(self.r.watch_pr())
        self.assertEqual([53], self.r.state['ignored_prs'])
        self.assertEqual(1, sum(line.startswith('ignoring finished') for line in self.logs))
        self.r.hub.write_message.assert_not_called()

    def test_failed_open_lookup_cannot_finish_the_tracked_pr(self):
        self.r.state.update(pr=copy.deepcopy(OPEN), seen_open=[7])
        for result in [SimpleNamespace(returncode=1, stdout=''),
                       SimpleNamespace(returncode=0, stdout='broken JSON')]:
            self.gh.reset_mock()
            self.gh.side_effect = None
            self.gh.return_value = result
            self.assertFalse(self.r.watch_pr())
            self.gh.assert_called_once()
        self.r.hub.write_message.assert_not_called()
        self.assertEqual('OPEN', self.r.state['pr']['state'])

    def test_failed_inline_lookup_preserves_snapshot_and_does_not_repeat_comments(self):
        self.open_pages = [[rest_pr(53)]]
        self.views[53] = with_(OPEN, number=53)
        self.inline_pages = [[{'id': 1, 'path': 'a.py', 'line': 4, 'body': 'fix this',
                               'user': {'login': 'reviewer'}}]]
        self.assertFalse(self.r.watch_pr())
        before = copy.deepcopy(self.r.state)
        self.r.hub.write_message.reset_mock()
        self.r._save.reset_mock()

        def transient_failure(argv, **kwargs):
            if argv[1] == 'api' and argv[2].endswith('/comments'):
                return SimpleNamespace(returncode=1, stdout='')
            return self.command(argv, **kwargs)

        self.gh.side_effect = transient_failure
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(before, self.r.state)
        self.r._save.assert_not_called()
        self.gh.side_effect = self.command
        self.assertFalse(self.r.watch_pr())
        self.r.hub.write_message.assert_not_called()

    def test_retired_closed_pr_can_reopen_and_merge_after_restart(self):
        self.use_disk_state()
        self.r.peers.append(relay.Peer('claude', 'claude', 'claude-pane'))
        self.unread = {'codex': [], 'claude': []}
        filed = []

        def write(**message):
            mid = message['message_id']
            filed.append(message)
            self.messages[mid] = dict(message, id=mid)
            self.unread[message['to']].append(mid)
            return Path(mid + '.md')

        self.r.hub.write_message = Mock(side_effect=write)
        self.history_pages = [[rest_pr(51, state='closed'), rest_pr(53, state='closed')]]
        self.assertFalse(self.r.watch_pr())
        self.assertEqual([51, 53], self.r.state['ignored_prs'])

        for ending in ['CLOSED', 'MERGED']:
            self.restart_from_disk()
            self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
            self.open_pages = [[rest_pr(53)]]
            self.views[53] = with_(OPEN, number=53)
            self.assertFalse(self.r.watch_pr())
            stored = json.loads(self.r.state_file.read_text())
            self.assertEqual([51], stored['ignored_prs'])
            self.assertNotIn(53, stored.get('completed_prs', []))
            self.open_pages = [[]]
            self.views[53] = with_(OPEN, number=53, state=ending)
            self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 60)

            def advance(seconds):
                self.t += seconds

            with patch.object(relay, 'pause', advance):
                self.assertEqual(0, self.r.run())
            self.assertEqual([53], json.loads(self.r.state_file.read_text())['completed_prs'])
            self.assertIsNone(self.r.state.get('pr'))
            self.assertEqual({'claude', 'codex'}, {m['to'] for m in filed if ending in m['subject']})
        self.assertEqual(2, sum(m['subject'] == 'PR #53 is open' for m in filed))

    def test_query_encodes_branch_name(self):
        self.r.branch = 'feature/a&b#c'
        self.assertIsNone(self.r.fetch_pr())
        self.assertIn('head=o%3Afeature%2Fa%26b%23c', self.gh.call_args_list[0].args[0][2])

    def test_dry_run_does_not_persist_ignored_prs(self):
        self.use_disk_state()
        self.r._save()
        before = self.r.state_file.read_bytes()
        self.r.dry_run = True
        self.history_pages = [[rest_pr(51, state='closed', merged=True)]]
        self.assertFalse(self.r.watch_pr())
        self.assertNotIn('ignored_prs', self.r.state)
        self.assertEqual(before, self.r.state_file.read_bytes())
        self.r.hub.write_message.assert_not_called()

    def test_full_lifecycle_restarts_on_same_branch_and_follows_next_pr(self):
        self.use_disk_state()
        self.r.peers.append(relay.Peer('claude', 'claude', 'claude-pane'))
        self.unread['claude'] = []
        filed = []

        def write(**message):
            mid = message['message_id']
            filed.append(message)
            self.messages[mid] = dict(message, id=mid)
            self.unread[message['to']].append(mid)
            return Path(mid + '.md')

        self.r.hub.write_message = Mock(side_effect=write)
        self.history_pages = [[rest_pr(51, state='closed', merged=True)]]
        self.assertFalse(self.r.watch_pr())
        self.open_pages = [[rest_pr(53)]]
        self.views[53] = with_(OPEN, number=53)
        self.r.pr_interval = 5
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 50)

        def merge_first(seconds):
            self.t += seconds
            self.open_pages = [[]]
            self.views[53] = with_(OPEN, number=53, state='MERGED')

        with patch.object(relay, 'pause', merge_first):
            self.assertEqual(0, self.r.run())
        self.assertEqual([53], json.loads(self.r.state_file.read_text())['completed_prs'])
        self.assertIsNone(self.r.state.get('pr'))
        self.assertNotIn('terminal_mail', self.r.state)
        self.assertEqual({'claude', 'codex'}, {m['to'] for m in filed if 'MERGED' in m['subject']})

        self.history_pages[0].append(rest_pr(53, state='closed', merged=True))
        self.restart_from_disk()
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 50)
        self.messages['m2'] = {'id': 'm2', 'from': 'claude', 'subject': 'next task'}
        self.unread['codex'].append('m2')
        self.logs.clear()
        restart_at = self.t

        def next_pr_then_merge(seconds):
            self.t += seconds
            if self.t < restart_at + 10:
                # First restart poll has no OPEN: normal mail delivery must continue.
                self.open_pages = [[rest_pr(58)]]
                self.views[58] = with_(OPEN, number=58)
            else:
                self.open_pages = [[]]
                self.views[58] = with_(OPEN, number=58, state='MERGED')

        with patch.object(relay, 'pause', next_pr_then_merge):
            self.assertEqual(0, self.r.run())
        self.assertEqual(restart_at + 10, self.t)
        self.assertIn('m2', self.r.state['announced'])
        self.assertEqual([53, 58], json.loads(self.r.state_file.read_text())['completed_prs'])
        self.assertEqual([51], self.r.state['ignored_prs'])
        self.assertFalse(any('resuming final notice drain' in line for line in self.logs))
        self.assertEqual(1, sum(m['subject'] == 'PR #58 is open' for m in filed))


    def fast_candidate(self, *, state='MERGED', created=BOUNDARY, path='history', number=53):
        candidate = rest_pr(number, state='closed', merged=state == 'MERGED', created=created)
        self.open_pages = [[with_(candidate, state='open')]] if path == 'open' else [[]]
        self.history_pages = [[candidate]]
        self.views[number] = with_(OPEN, number=number, state=state, createdAt=created)

    def record_mail(self):
        self.r.peers = [self.peer, relay.Peer('claude', 'claude', 'claude-pane')]
        self.unread = {'codex': [], 'claude': []}
        filed = []

        def write(**message):
            mid = message['message_id']
            filed.append(message)
            self.messages[mid] = dict(message, id=mid)
            self.unread[message['to']].append(mid)
            return Path(mid + '.md')

        self.r.hub.write_message = Mock(side_effect=write)
        return filed

    def test_fast_terminal_boundaries_in_both_discovery_paths(self):
        initial = copy.deepcopy(self.r.state)
        for path in ['history', 'open']:
            for state in ['MERGED', 'CLOSED']:
                for created, eligible in [(BOUNDARY, True), ('2026-09-24T19:00:01+03:00', True),
                                          ('2026-09-24T15:59:59Z', False), (OLD_CREATED, False)]:
                    with self.subTest(path=path, state=state, created=created):
                        self.r.state = copy.deepcopy(initial)
                        self.fast_candidate(state=state, created=created, path=path)
                        filed = self.record_mail()
                        self.assertEqual(eligible, self.r.watch_pr())
                        if eligible:
                            self.assertEqual([53], self.r.state['seen_open'])
                            self.assertEqual(state, self.r.state['pr']['state'])
                            self.assertNotIn('_observed_after_watch', self.r.state['pr'])
                            self.assertEqual(3, len(filed))
                            self.assertEqual('PR #53 is open', filed[0]['subject'])
                            self.assertEqual({'codex', 'claude'}, {m['to'] for m in filed[1:]})
                        else:
                            self.assertEqual([], filed)
                            self.assertEqual([53], self.r.state['ignored_prs'])

    def test_unknown_creation_time_retries_without_ignoring(self):
        initial = copy.deepcopy(self.r.state)
        for field in ['created_at', 'createdAt']:
            for bad in [None, '', 'nonsense', '2026-09-24T16:00:00']:
                with self.subTest(field=field, bad=bad):
                    self.r.state = copy.deepcopy(initial)
                    self.fast_candidate()
                    target = self.history_pages[0][0] if field == 'created_at' else self.views[53]
                    target[field] = bad
                    self.assertFalse(self.r.watch_pr())
                    self.assertEqual(initial, self.r.state)
                    self.r.hub.write_message.assert_not_called()
        self.fast_candidate()
        self.assertTrue(self.r.watch_pr())

    def test_fast_fetch_failures_do_not_commit_observation_or_ignores(self):
        self.fast_candidate()
        self.history_pages[0].append(rest_pr(51, state='closed'))
        before = copy.deepcopy(self.r.state)
        for failed in ['history', 'view', 'inline']:
            for response in [SimpleNamespace(returncode=1, stdout=''),
                             SimpleNamespace(returncode=0, stdout='bad json')]:
                with self.subTest(failed=failed, response=response):
                    def command(argv, **kwargs):
                        matches = (failed == 'history' and 'state=all' in argv[2] or
                                   failed == 'view' and argv[1] == 'pr' or
                                   failed == 'inline' and argv[2].endswith('/comments'))
                        return response if matches else self.command(argv, **kwargs)
                    self.gh.side_effect = command
                    self.assertFalse(self.r.watch_pr())
                    self.assertEqual(before, self.r.state)
                    self.r._save.assert_not_called()
                    self.r.hub.write_message.assert_not_called()
        self.gh.side_effect = self.command
        self.assertTrue(self.r.watch_pr())
        self.assertEqual([51], self.r.state['ignored_prs'])

    def test_fast_view_must_match_repository_branch_and_number(self):
        self.fast_candidate()
        before = copy.deepcopy(self.r.state)
        for changes in [{'isCrossRepository': True}, {'headRefName': 'other'}, {'number': 99}]:
            self.views[53] = with_(OPEN, number=53, state='MERGED', createdAt=BOUNDARY)
            self.views[53].update(changes)
            self.assertFalse(self.r.watch_pr())
            self.assertEqual(before, self.r.state)
            self.assertIn('view does not match the requested number, head repository or branch', self.logs[-1])
        self.r.hub.write_message.assert_not_called()

    def test_fast_events_commit_terminal_snapshot_and_do_not_repeat_after_restart(self):
        self.use_disk_state()
        self.fast_candidate()
        filed = self.record_mail()
        self.views[53].update(reviews=[{'id': 'R1', 'state': 'APPROVED', 'body': 'review'}],
                              comments=[{'id': 'C1', 'body': 'conversation'}])
        self.inline_pages = [[{'id': 1, 'body': 'inline', 'path': 'x.py', 'line': 2}]]
        saved = []
        real_save = self.r._save

        def save():
            saved.append(copy.deepcopy(self.r.state))
            real_save()

        self.r._save = save
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(2, len(saved))  # planned outbox, then publication checkpoint
        self.assertEqual(6, len(saved[0]['outbox']))
        self.assertNotIn('outbox', saved[1])
        self.assertEqual('MERGED', saved[0]['pr']['state'])
        self.assertEqual([53], saved[0]['seen_open'])
        self.assertEqual(2, len(saved[0]['terminal_mail']))
        self.assertEqual(6, len(filed))  # discovery, three discussions, two final recipients
        self.restart_from_disk()
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(6, len(filed))

    def test_server_boundary_initializes_before_polling_and_survives_restart(self):
        self.use_disk_state()
        self.r.state.pop('watch_since')  # legacy state migrates at first successful server Date
        self.r._save()

        def command(argv, **kwargs):
            if argv != ['gh', 'api', '-i', 'rate_limit']:
                self.assertEqual(BOUNDARY, json.loads(self.r.state_file.read_text())['watch_since'])
            return self.command(argv, **kwargs)

        self.gh.side_effect = command
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(BOUNDARY, self.r.state['watch_since'])
        self.restart_from_disk()
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.date_response.stdout = 'HTTP/2 200\nDate: Fri, 25 Sep 2026 12:00:00 GMT\n\n{}'
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(1, sum(c.args[0] == ['gh', 'api', '-i', 'rate_limit']
                                for c in self.gh.call_args_list))

    def test_branch_change_discards_and_reinitializes_boundary(self):
        self.use_disk_state()
        self.r.state['watch_since'] = OLD_CREATED
        self.r._save()
        self.r.branch = 'issue-next'
        self.restart_from_disk()
        self.assertNotIn('watch_since', self.r.state)
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(BOUNDARY, self.r.state['watch_since'])

    def test_missing_or_invalid_server_date_never_uses_local_time(self):
        self.r.state.pop('watch_since')
        before = copy.deepcopy(self.r.state)
        responses = [SimpleNamespace(returncode=1, stdout=''),
                     SimpleNamespace(returncode=0, stdout='HTTP/2 200\n\nDate: Thu, 24 Sep 2026 16:00:00 GMT'),
                     SimpleNamespace(returncode=0, stdout='HTTP/2 200\nDate: broken\n\n{}'),
                     OSError('offline'), relay.subprocess.TimeoutExpired('gh', 10)]
        for response in responses:
            self.gh.reset_mock()
            self.gh.side_effect = [response]
            self.assertFalse(self.r.watch_pr())
            self.gh.assert_called_once()
            self.assertEqual(before, self.r.state)
            self.r._save.assert_not_called()
        self.gh.side_effect = self.command
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(BOUNDARY, self.r.state['watch_since'])

    def test_bootstrap_failure_still_delivers_mail_then_recovers(self):
        self.r.state.pop('watch_since')
        self.date_response = SimpleNamespace(returncode=1, stdout='')
        self.r.pr_interval = 5
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 10)

        def advance(seconds):
            self.assertIn('m1', self.r.state['announced'])
            self.t += seconds
            self.date_response = SimpleNamespace(returncode=0, stdout=
                'HTTP/2 200\nDate: Thu, 24 Sep 2026 16:00:00 GMT\n\n{}')

        with patch.object(relay, 'pause', advance):
            self.assertEqual(0, self.r.run())
        self.assertEqual(BOUNDARY, self.r.state['watch_since'])
        self.send.assert_called_once()

    def test_dry_run_fast_path_does_not_change_mail_or_state(self):
        self.use_disk_state()
        self.r.state.pop('watch_since')
        self.r._save()
        before = copy.deepcopy(self.r.state)
        disk = self.r.state_file.read_bytes()
        self.r.dry_run = True
        self.fast_candidate()
        self.assertTrue(self.r.watch_pr())
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(before, self.r.state)
        self.assertEqual(disk, self.r.state_file.read_bytes())
        self.r.hub.write_message.assert_not_called()
        self.assertEqual(1, sum(c.args[0] == ['gh', 'api', '-i', 'rate_limit']
                                for c in self.gh.call_args_list))

    def test_two_fast_prs_retire_one_per_run_and_choose_newest_creation_first(self):
        self.use_disk_state()
        self.fast_candidate(number=53, state='CLOSED')
        self.history_pages[0].append(rest_pr(51, state='closed', merged=True,
                                             created='2026-09-24T16:00:01Z'))
        self.views[51] = with_(OPEN, number=51, state='MERGED', createdAt='2026-09-24T16:00:01Z')
        filed = self.record_mail()
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 100)

        def advance(seconds):
            self.t += seconds

        for number, completed in [(51, [51]), (53, [51, 53])]:
            with patch.object(relay, 'pause', advance):
                self.assertEqual(0, self.r.run())
            self.assertEqual(completed, json.loads(self.r.state_file.read_text())['completed_prs'])
            self.assertNotIn('pr', self.r.state)
            self.assertNotIn('ignored_prs', self.r.state)
            self.assertEqual(1, sum(m['subject'] == f'PR #{number} is open' for m in filed))
            self.restart_from_disk()
            self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
            self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 100)
        self.assertEqual(6, len(filed))

    def test_equal_creation_times_choose_highest_number(self):
        self.fast_candidate(number=53)
        self.history_pages[0].append(rest_pr(54, state='closed', created=BOUNDARY))
        self.views[54] = with_(OPEN, number=54, state='CLOSED', createdAt=BOUNDARY)
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(54, self.r.state['pr']['number'])
        self.assertNotIn('ignored_prs', self.r.state)

    def test_finished_predecessor_ends_run_and_successor_waits_for_restart(self):
        self.use_disk_state()
        filed = self.record_mail()
        self.open_pages = [[rest_pr(51)]]
        self.views[51] = with_(OPEN, number=51)
        self.assertFalse(self.r.watch_pr())
        self.open_pages = [[rest_pr(53)]]
        self.views[51]['state'] = 'MERGED'
        self.views[53] = with_(OPEN, number=53)
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 30)

        def advance(seconds):
            self.t += seconds

        with patch.object(relay, 'pause', advance):
            self.assertEqual(0, self.r.run())
        self.assertEqual([51], self.r.state['completed_prs'])
        self.assertNotIn(53, self.r.state['seen_open'])
        self.assertFalse(any('PR #53' in m['subject'] for m in filed))
        for box in ['claude', 'codex']:
            self.assertEqual(1, sum('PR #51 MERGED' in m['subject'] and m['to'] == box for m in filed))
        self.restart_from_disk()
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 30)
        with patch.object(relay, 'pause', advance):
            self.assertEqual(0, self.r.run())
        self.assertEqual(30, self.t)
        self.assertEqual(53, self.r.state['pr']['number'])
        self.assertEqual(1, sum(m['subject'] == 'PR #53 is open' for m in filed))
        self.assertEqual('stop file found; exiting', self.logs[-1])

    def test_switch_marks_unresolved_previous_pr_completed_with_reason(self):
        self.r.state.update(pr=with_(OPEN, number=51), seen_open=[51])
        self.open_pages = [[rest_pr(53)]]
        self.views.update({51: with_(OPEN, number=51), 53: with_(OPEN, number=53)})
        self.assertFalse(self.r.watch_pr())
        self.assertEqual([51], self.r.state['completed_prs'])
        self.assertTrue(any('previous watch (OPEN); switching to PR #53' in line for line in self.logs))

    def test_failed_previous_view_defers_switch_without_committing(self):
        self.r.state.update(pr=with_(OPEN, number=51), seen_open=[51])
        self.open_pages = [[rest_pr(53)]]
        self.views.update({51: None, 53: with_(OPEN, number=53)})
        before = copy.deepcopy(self.r.state)
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(before, self.r.state)
        self.r.hub.write_message.assert_not_called()

    def test_legacy_orphan_seen_open_gets_terminal_events_even_before_boundary(self):
        self.r.state['seen_open'] = [53]
        self.fast_candidate(created=OLD_CREATED, state='CLOSED')
        filed = self.record_mail()
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(3, len(filed))
        self.assertEqual('PR #53 was CLOSED without merging', filed[-1]['subject'])
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(3, len(filed))

    def use_disk_mail(self):
        directory = self.r.hub_dir
        for name, path in [('INBOX', directory / 'inbox'), ('STATE', directory / 'state'),
                           ('LOG', directory / 'state' / 'log.jsonl')]:
            self.enterContext(patch.object(hub, name, path))
        self.r.peers = [self.peer, relay.Peer('claude', 'claude', 'claude-pane')]
        self.r.hub = SimpleNamespace(unread=hub.unread, parse_message=hub.parse_message,
                                     write_message=hub.write_message)

    def test_outbox_recovers_crashes_before_during_and_after_publication(self):
        initial = copy.deepcopy(self.r.state)
        for crash_after in range(7):
            with self.subTest(crash_after=crash_after):
                self.r.state = copy.deepcopy(initial)
                self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
                self.use_disk_state()
                self.use_disk_mail()
                self.fast_candidate()
                self.views[53].update(reviews=[{'id': 'R1', 'body': 'review'}],
                                      comments=[{'id': 'C1', 'body': 'comment'}])
                self.inline_pages = [[{'id': 1, 'body': 'inline'}]]
                publications = []

                def write(**message):
                    if len(publications) == crash_after:
                        raise RuntimeError('crash during publication')
                    path = hub.write_message(**message)
                    publications.append(path)
                    return path

                self.r.hub.write_message = write
                real_save = self.r._save

                def save():
                    if crash_after == 6 and 'outbox' not in self.r.state:
                        raise RuntimeError('crash before publication checkpoint')
                    real_save()

                self.r._save = save
                with self.assertRaises(RuntimeError):
                    self.r.watch_pr()
                saved = json.loads(self.r.state_file.read_text())
                self.assertEqual('MERGED', saved['pr']['state'])
                self.assertEqual([53], saved['seen_open'])
                self.assertEqual(6, len(saved['outbox']))
                self.assertEqual(2, len(saved['terminal_mail']))
                self.assertEqual(crash_after, len(publications))
                # An agent may have read a partially-published batch before the crash.
                if crash_after == 6:
                    publications = [hub.mark_read(path) for path in publications]
                elif publications:
                    publications[0] = hub.mark_read(publications[0])
                original = {path: path.read_bytes() for path in publications}
                self.r.hub.write_message = hub.write_message
                self.restart_from_disk()
                self.assertNotIn(53, self.r.state.get('completed_prs', []))
                self.r.stop_file = SimpleNamespace(exists=lambda: False)
                with patch.object(relay, 'pause', side_effect=AssertionError('unexpected wait')):
                    self.assertEqual(0, self.r.run())
                self.r.fetch_pr.assert_not_called()
                self.assertEqual([53], self.r.state['completed_prs'])
                self.assertNotIn('outbox', self.r.state)
                paths = list((self.r.hub_dir / 'inbox').rglob('*.md'))
                self.assertEqual(6, len(paths))
                self.assertEqual(6, len({p.stem for p in paths}))
                self.assertEqual(6, len(hub.LOG.read_text().splitlines()))
                for path, contents in original.items():
                    self.assertEqual(contents, path.read_bytes())

    def test_explicit_mail_id_is_idempotent_in_inbox_read_and_archive(self):
        self.use_disk_state()
        self.use_disk_mail()
        message = dict(to='claude', sender='github', subject='original', body='first',
                       message_id='github-pr53-note-1-claude')
        path = hub.write_message(**message)
        contents = path.read_bytes()
        for folder in ['', 'read', 'archive']:
            target = hub.INBOX / 'claude' / folder / path.name
            if path != target:
                path.rename(target)
                path = target
            duplicate = hub.write_message(**dict(message, body='different'))
            self.assertEqual(path, duplicate)
            self.assertEqual(contents, path.read_bytes())
        self.assertEqual(1, len(hub.LOG.read_text().splitlines()))
        for invalid in ['../escape', 'x/y', 'x\\y', '.']:
            with self.assertRaises(ValueError):
                hub.write_message(**dict(message, message_id=invalid))

    def test_hard_link_failure_falls_back_to_exclusive_publication(self):
        self.use_disk_state()
        self.use_disk_mail()
        message = dict(to='claude', sender='github', subject='fallback', body='complete body',
                       message_id='github-fallback')
        with patch.object(hub.os, 'link', side_effect=OSError(errno.EPERM, 'links unavailable')):
            path = hub.write_message(**message)
            original = path.read_bytes()
            self.assertEqual(path, hub.write_message(**message))
        self.assertEqual(original, path.read_bytes())
        self.assertEqual('complete body', hub.parse_message(path)['body'])
        self.assertEqual(1, len(hub.LOG.read_text().splitlines()))
        self.assertEqual([], list(hub.INBOX.rglob('*.tmp')))

    def test_concurrent_replay_keeps_original_and_removes_temp_without_logging(self):
        self.use_disk_state()
        self.use_disk_mail()
        original = '---\nsubject: concurrent\n---\noriginal\n'

        def concurrent_link(source, target):
            target.write_text(original, encoding='utf-8')
            raise FileExistsError('another replay won')

        with patch.object(hub.os, 'link', side_effect=concurrent_link):
            path = hub.write_message(to='claude', sender='github', subject='concurrent',
                                     body='replacement', message_id='github-concurrent')
        self.assertEqual(original, path.read_text(encoding='utf-8'))
        self.assertEqual([], list(hub.INBOX.rglob('*.tmp')))
        self.assertFalse(hub.LOG.exists())

    def test_temp_write_failure_cleans_up(self):
        self.use_disk_state()
        self.use_disk_mail()
        real_temp = hub.tempfile.NamedTemporaryFile

        def failing_temp(**kwargs):
            handle = real_temp(**kwargs)
            handle.write = Mock(side_effect=OSError('write failed'))
            return handle

        with patch.object(hub.tempfile, 'NamedTemporaryFile', side_effect=failing_temp):
            with self.assertRaises(OSError):
                hub.write_message(to='claude', sender='github', subject='failure', body='body')
        self.assertEqual([], list(hub.INBOX.rglob('*.tmp')))
        self.assertEqual([], list(hub.INBOX.rglob('*.md')))

    def test_id_collision_logs_different_subject_without_overwriting(self):
        self.use_disk_state()
        self.use_disk_mail()
        message = dict(to='claude', sender='github', subject='original', body='body',
                       message_id='github-collision')
        path = hub.write_message(**message)
        original = path.read_bytes()
        hub.write_message(**dict(message, subject='different'))
        self.assertEqual(original, path.read_bytes())
        collision = json.loads(hub.LOG.read_text().splitlines()[-1])
        self.assertEqual('id-collision', collision['event'])
        self.assertEqual('original', collision['stored_subject'])
        self.assertEqual('different', collision['subject'])

    def test_outbox_oserror_logs_and_retries_on_later_tick_before_retirement(self):
        self.fast_candidate()
        filed = self.record_mail()
        write = self.r.hub.write_message.side_effect
        attempts = []

        def unavailable_once(**message):
            attempts.append(self.t)
            if len(attempts) == 1:
                raise OSError('mailbox unavailable')
            return write(**message)

        self.r.hub.write_message.side_effect = unavailable_once
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t > 30)

        def advance(seconds):
            self.assertNotIn(53, self.r.state.get('completed_prs', []))
            self.assertTrue(self.r.state.get('outbox'))
            self.t += seconds

        with patch.object(relay, 'pause', advance):
            self.assertEqual(0, self.r.run())
        self.assertEqual([0, 5, 5, 5], attempts)
        self.assertEqual(3, len(filed))
        self.assertEqual([53], self.r.state['completed_prs'])
        self.assertTrue(any('outbox publication failed; will retry' in line for line in self.logs))

    def test_branch_switch_discards_unpublished_outbox_and_counter(self):
        self.use_disk_state()
        self.r.state.update(outbox=[dict(to='claude', sender='github', subject='stale', body='old',
                                        message_id='github-old-branch')], event_sequence=5)
        self.r._save()
        self.r.branch = 'new-branch'
        self.restart_from_disk()
        self.assertNotIn('outbox', self.r.state)
        self.assertNotIn('event_sequence', self.r.state)
        self.assertTrue(self.r.flush_outbox())
        self.r.hub.write_message.assert_not_called()

    def test_state_loss_rediscovery_keeps_old_ids_and_files_new_review(self):
        self.use_disk_state()
        self.use_disk_mail()
        self.open_pages = [[rest_pr(53)]]
        self.views[53] = with_(OPEN, number=53, reviews=[{'id': 'R1', 'body': 'old review'}])
        self.assertFalse(self.r.watch_pr())
        original = {hub.mark_read(path): path.stem for path in hub.unread('claude')}
        self.assertEqual(2, len(original))
        contents = {path: path.read_bytes() for path in original}
        self.r.state_file.unlink()
        self.restart_from_disk()
        self.r.fetch_pr = relay.Relay.fetch_pr.__get__(self.r)
        self.views[53]['reviews'].append({'id': 'R2', 'body': 'new review'})
        self.assertFalse(self.r.watch_pr())
        self.assertFalse(self.r.watch_pr())
        self.assertEqual(['new review'], [hub.parse_message(path)['body'] for path in hub.unread('claude')])
        self.assertEqual(3, len(list(hub.INBOX.rglob('*.md'))))
        self.assertEqual(3, len(hub.LOG.read_text().splitlines()))
        for path, original_bytes in contents.items():
            self.assertEqual(original_bytes, path.read_bytes())

    def test_closed_predecessor_also_ends_run_without_observing_successor(self):
        self.r.state.update(pr=with_(OPEN, number=51), seen_open=[51])
        self.views.update({51: with_(OPEN, number=51, state='CLOSED'), 53: with_(OPEN, number=53)})
        self.open_pages = [[rest_pr(53)]]
        filed = self.record_mail()
        self.assertTrue(self.r.watch_pr())
        self.assertEqual(51, self.r.state['pr']['number'])
        self.assertEqual([51], self.r.state['seen_open'])
        self.assertEqual(2, len(filed))
        self.assertTrue(all(m['subject'] == 'PR #51 was CLOSED without merging' for m in filed))

    def test_preboundary_unseen_terminal_does_not_end_run(self):
        self.fast_candidate(created=OLD_CREATED)
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 10)

        def advance(seconds):
            self.t += seconds

        with patch.object(relay, 'pause', advance):
            self.assertEqual(0, self.r.run())
        self.assertEqual(10, self.t)
        self.assertEqual([53], self.r.state['ignored_prs'])
        self.r.hub.write_message.assert_not_called()

    def test_dry_run_does_not_publish_or_clear_a_saved_outbox(self):
        self.use_disk_state()
        self.fast_candidate()
        self.r.flush_outbox = Mock(side_effect=RuntimeError('crash'))
        # The first flush precedes fetching; allow that one and fail after the checkpoint.
        self.r.flush_outbox.side_effect = [True, RuntimeError('crash')]
        with self.assertRaises(RuntimeError):
            self.r.watch_pr()
        disk = self.r.state_file.read_bytes()
        self.restart_from_disk(dry_run=True)
        before = copy.deepcopy(self.r.state)
        self.r.hub.write_message.reset_mock()
        self.assertEqual(0, self.r.run())
        self.r.hub.write_message.assert_not_called()
        self.assertEqual(before, self.r.state)
        self.assertEqual(disk, self.r.state_file.read_bytes())

    def test_fast_terminal_drain_waits_for_hold_and_failed_status_reset(self):
        self.fast_candidate()
        filed = self.record_mail()
        self.r.stop_file = SimpleNamespace(exists=lambda: self.t >= 100)
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 70 else CLAUDE_IDLE
        failed = False

        def status(value, **kwargs):
            nonlocal failed
            if value == 'idle' and not failed:
                failed = True
                raise agw.CtlError('reset unavailable')

        self.status.side_effect = status

        def advance(seconds):
            self.t += seconds

        with patch.object(relay, 'pause', advance):
            self.assertEqual(0, self.r.run())
        self.assertEqual(75, self.t)
        self.assertEqual(3, len(filed))
        self.assertEqual(3, self.send.call_count)
        self.assertEqual([53], self.r.state['completed_prs'])
        self.assertEqual([], self.r.state['reset_pending'])


class BranchState(DeliveryFixture):
    def save_legacy(self, state):
        self.r.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.r.state_file.write_text(json.dumps(state), encoding='utf-8')

    def test_fresh_state_is_scoped_without_a_discard_log(self):
        self.use_disk_state()
        self.save_legacy({'announced': [], 'pr': None})
        self.logs.clear()
        self.restart_from_disk()
        self.assertEqual('issue-6', json.loads(self.r.state_file.read_text())['branch'])
        self.assertFalse(any('discarding saved PR state' in line for line in self.logs))

    def test_unscoped_saved_branch_state_still_logs_its_discard(self):
        self.use_disk_state()
        for state in [{'announced': [], 'ignored_prs': [51]},
                      {'announced': [], 'branch': 'old-branch'}]:
            self.save_legacy(state)
            self.logs.clear()
            self.restart_from_disk()
            self.assertEqual(1, sum('discarding saved PR state' in line for line in self.logs))
            self.assertEqual('issue-6', json.loads(self.r.state_file.read_text())['branch'])

    def test_legacy_open_observation_survives_an_offline_merge(self):
        self.use_disk_state()
        self.save_legacy({'pr': copy.deepcopy(OPEN), 'announced': []})
        self.restart_from_disk()
        self.assertEqual([7], self.r.state['seen_open'])
        self.r.fetch_pr.return_value = relay.PrFetch(with_(OPEN, state='MERGED'))
        self.r.hub.write_message = Mock(return_value=Path('terminal.md'))
        self.assertTrue(self.r.watch_pr())
        self.assertEqual('PR #7 MERGED - the loop is complete',
                         self.r.hub.write_message.call_args.kwargs['subject'])

    def test_legacy_terminal_without_pending_notices_is_retired_not_observed(self):
        self.use_disk_state()
        for terminal_mail in [[], [['codex', 'old-final']]]:
            self.save_legacy({'pr': with_(OPEN, state='MERGED'), 'announced': [],
                              'terminal_mail': terminal_mail})
            self.restart_from_disk()
            self.assertNotIn('seen_open', self.r.state)
            self.assertIsNone(self.r.state.get('pr'))
            self.assertEqual([7], self.r.state['completed_prs'])
            self.assertEqual(self.r.state, json.loads(self.r.state_file.read_text()))

    def test_branch_scope_clears_observation_without_a_snapshot(self):
        self.use_disk_state()
        for branch in ['old-branch', None]:
            state = {'announced': ['m1'], 'reset_pending': ['codex'], 'ignored_prs': [51],
                     'seen_open': [53], 'completed_prs': [53], 'terminal_mail': [['codex', 'old-final']]}
            if branch:
                state['branch'] = branch
            self.save_legacy(state)
            self.restart_from_disk()
            self.assertEqual({'announced': ['m1'], 'reset_pending': ['codex'], 'branch': 'issue-6'},
                             self.r.state)
            self.assertEqual(self.r.state, json.loads(self.r.state_file.read_text()))

    def test_reset_only_restart_continues_polling_without_a_terminal_pr(self):
        self.use_disk_state()
        for snapshot in [None, copy.deepcopy(OPEN)]:
            self.save_legacy({'pr': snapshot, 'branch': 'issue-6', 'reset_pending': ['codex'],
                              'announced': []})
            self.restart_from_disk()
            self.r.stop_file = SimpleNamespace(exists=lambda: self.r.fetch_pr.call_count >= 2)
            self.logs.clear()

            def advance(seconds):
                self.t += seconds

            with patch.object(relay, 'pause', advance):
                self.assertEqual(0, self.r.run())
            self.assertEqual(2, self.r.fetch_pr.call_count)
            self.assertEqual([], self.r.state['reset_pending'])
            self.assertIn('m1', self.r.state['announced'])
            self.assertFalse(any('resuming final notice drain' in line for line in self.logs))
            self.assertNotIn('completed_prs', self.r.state)

    def test_dry_run_migrates_and_retires_only_in_memory(self):
        self.use_disk_state()
        for snapshot in [copy.deepcopy(OPEN), with_(OPEN, state='MERGED')]:
            self.save_legacy({'pr': snapshot, 'announced': []})
            before = self.r.state_file.read_bytes()
            self.restart_from_disk(dry_run=True)
            self.assertEqual(before, self.r.state_file.read_bytes())
            if snapshot['state'] == 'OPEN':
                self.assertEqual([7], self.r.state['seen_open'])
            else:
                self.assertEqual([7], self.r.state['completed_prs'])
                self.assertIsNone(self.r.state.get('pr'))


class FinalNotices(DeliveryFixture):
    def prepare_final(self, state='MERGED'):
        self.peer = relay.Peer('claude', 'claude', 'claude-pane')
        self.r.peers = [self.peer]
        self.unread = {'claude': []}
        self.messages = {}
        self.r.state = {'pr': copy.deepcopy(OPEN), 'announced': [], 'seen_open': [7], 'branch': 'issue-6'}
        self.r.holds.clear()
        self.r.fetch_pr = Mock(return_value=relay.PrFetch(with_(OPEN, state=state, mergedAt='now')))
        event = relay.pr_events(OPEN, with_(OPEN, state=state, mergedAt='now'))[-1]
        self.final_ids = {box: relay.event_message_id(7, event, box) for box in ['claude', 'codex']}
        self.r.stop_file = SimpleNamespace(exists=lambda: False)

        def write(**message):
            mid = message['message_id']
            self.messages[mid] = dict(message, id=mid)
            self.unread[message['to']].append(mid)
            return Path(mid + '.md')

        self.r.hub.write_message = Mock(side_effect=write)
        self.enterContext(patch.object(relay, 'pause', self.advance))

    def advance(self, seconds):
        self.t += seconds

    def test_merged_and_closed_notices_wait_until_the_third_tick(self):
        for state in ['MERGED', 'CLOSED']:
            with self.subTest(state=state):
                self.prepare_final(state)
                self.r.peers.append(relay.Peer('codex', 'codex', 'codex-pane'))
                self.unread['codex'] = []
                self.pane.side_effect = [CLAUDE_RUNNING, CLAUDE_RUNNING, CLAUDE_IDLE]
                before = self.send.call_count
                self.assertEqual(0, self.r.run())
                self.assertEqual(before + 2, self.send.call_count)
                self.assertEqual(sorted(self.final_ids.values()), self.r.state['announced'])
                self.r.fetch_pr.assert_called_once()
                self.assertFalse(self.r.pending_terminal_mail())

    def test_final_notice_alerts_if_held_over_a_minute(self):
        self.prepare_final()
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 70 else CLAUDE_IDLE
        self.assertEqual(0, self.r.run())
        self.assertEqual(1, self.notify.call_count)
        self.assertIn('mid-turn', self.notify.call_args.args[1])
        self.status.assert_called_with('idle', pane_id=self.peer.pane)
        self.assertEqual([self.final_ids['claude']], self.r.state['announced'])

    def test_drain_waits_for_a_failed_idle_reset_without_ringing_again(self):
        self.prepare_final()
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 70 else CLAUDE_IDLE
        self.status.side_effect = [None, agw.CtlError('reset unavailable'), None]
        self.assertEqual(0, self.r.run())
        self.assertEqual(75, self.t)
        self.send.assert_called_once()
        self.assertEqual({}, self.r.holds)
        self.status.assert_called_with('idle', pane_id=self.peer.pane)

    def test_drain_ceiling_logs_the_remaining_recipient_id_and_reason(self):
        self.prepare_final()
        self.pane.return_value = CLAUDE_RUNNING
        self.assertEqual(0, self.r.run())
        self.assertEqual(relay.TERMINAL_DRAIN_TIMEOUT, self.t)
        self.assertEqual([], self.r.state['announced'])
        self.assertIn('drain deadline reached', self.logs[-1])
        self.assertIn('claude/' + self.final_ids['claude'], self.logs[-1])
        self.assertIn('mid-turn', self.logs[-1])
        self.r.fetch_pr.assert_called_once()
        self.assertNotIn(7, self.r.state.get('completed_prs', []))
        self.assertEqual('MERGED', self.r.state['pr']['state'])

    def test_independently_read_final_notice_allows_exit(self):
        self.prepare_final()
        self.pane.return_value = CLAUDE_RUNNING

        def read_and_advance(seconds):
            self.advance(seconds)
            self.unread['claude'] = []

        with patch.object(relay, 'pause', read_and_advance):
            self.assertEqual(0, self.r.run())
        self.send.assert_not_called()
        self.assertEqual(5, self.t)
        self.assertFalse(self.r.holds)

    def test_final_notice_state_can_resume_a_drain_after_restart(self):
        self.prepare_final()
        self.use_disk_state()
        self.assertTrue(self.r.watch_pr())
        saved = json.loads(self.r.state_file.read_text(encoding='utf-8'))
        self.assertEqual([['claude', self.final_ids['claude']]], saved['terminal_mail'])
        self.r.hub.write_message.reset_mock()
        self.restart_from_disk()
        self.assertEqual(saved, self.r.state)
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 15 else CLAUDE_IDLE
        self.assertEqual(0, self.r.run())
        self.assertEqual(15, self.t)
        self.assertEqual(4, self.pane.call_count)
        self.r.fetch_pr.assert_not_called()
        self.r.hub.write_message.assert_not_called()
        self.assertEqual([self.final_ids['claude']], self.r.state['announced'])

    def test_restarted_drain_keeps_its_ceiling_without_github(self):
        self.prepare_final('CLOSED')
        self.use_disk_state()
        self.assertTrue(self.r.watch_pr())
        self.restart_from_disk()
        self.pane.return_value = CLAUDE_RUNNING
        self.assertEqual(0, self.r.run())
        self.assertEqual(relay.TERMINAL_DRAIN_TIMEOUT, self.t)
        self.assertIn('drain deadline reached', self.logs[-1])
        self.assertIn('claude/' + self.final_ids['claude'], self.logs[-1])
        self.r.fetch_pr.assert_not_called()

    def test_legacy_pending_terminal_notices_drain_without_seeding_observation(self):
        self.prepare_final()
        self.use_disk_state()
        self.assertTrue(self.r.watch_pr())
        self.r.state.pop('seen_open')
        self.r._save()
        self.restart_from_disk()
        self.assertNotIn('seen_open', self.r.state)
        self.pane.side_effect = lambda pane: CLAUDE_RUNNING if self.t < 15 else CLAUDE_IDLE
        self.assertEqual(0, self.r.run())
        self.assertEqual(15, self.t)
        self.r.fetch_pr.assert_not_called()
        self.assertEqual([7], self.r.state['completed_prs'])
        self.assertNotIn('seen_open', self.r.state)
        self.assertEqual(self.r.state, json.loads(self.r.state_file.read_text()))

    def test_resolved_terminal_restart_retries_status_reset_before_retiring(self):
        self.prepare_final()
        self.use_disk_state()
        self.assertTrue(self.r.watch_pr())
        self.r.state['announced'] = [self.final_ids['claude']]
        self.r.state['reset_pending'] = ['claude']
        self.r._save()
        self.restart_from_disk()
        self.assertEqual('MERGED', self.r.state['pr']['state'])
        self.status.side_effect = [agw.CtlError('reset unavailable'), None]
        self.assertEqual(0, self.r.run())
        self.assertEqual(5, self.t)
        self.send.assert_not_called()
        self.r.fetch_pr.assert_not_called()
        self.assertEqual([7], self.r.state['completed_prs'])

    def test_restart_after_delivery_before_retirement_resumes_normal_watching(self):
        self.prepare_final()
        self.use_disk_state()
        self.assertTrue(self.r.watch_pr())
        self.r.state['announced'] = [self.final_ids['claude']]
        self.r._save()  # Simulate a stop after delivery was saved but before drain retirement.
        self.restart_from_disk()
        self.assertEqual([7], self.r.state['completed_prs'])
        self.assertIsNone(self.r.state.get('pr'))
        self.r.stop_file = SimpleNamespace(exists=lambda: self.r.fetch_pr.call_count >= 2)
        self.logs.clear()
        self.assertEqual(0, self.r.run())
        self.assertEqual(2, self.r.fetch_pr.call_count)
        self.assertFalse(any('resuming final notice drain' in line for line in self.logs))
        self.assertEqual(self.r.state, json.loads(self.r.state_file.read_text()))

    def test_persisted_status_reset_blocks_retirement_even_if_its_hold_is_dropped(self):
        self.prepare_final()
        self.use_disk_state()
        self.assertTrue(self.r.watch_pr())
        self.r.state['announced'] = [self.final_ids['claude']]
        self.r.state['reset_pending'] = ['claude']
        self.r._save()
        self.restart_from_disk()
        self.r.holds.clear()  # The mailbox-owned reset must outlive a lost in-memory hold.
        self.assertEqual(0, self.r.run())
        self.assertEqual(relay.TERMINAL_DRAIN_TIMEOUT, self.t)
        self.assertNotIn(7, self.r.state.get('completed_prs', []))
        self.assertEqual(['claude'], self.r.state['reset_pending'])
        self.assertIn('claude/', self.logs[-1])
        self.assertIn('status reset pending', self.logs[-1])
        self.assertEqual(self.r.state, json.loads(self.r.state_file.read_text()))
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
