"""Submission guards exercised with captured frames; no terminal or network."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import agw
import peerchat

# Verbatim captured frames supplied with plan v3 (Unicode preserved).

CLAUDE_IDLE = r"""✻ Brewed for 1m 0s · done 4:23 PM
─────────────────────────────────────────────────────
>
─────────────────────────────────────────────────────
  [Fable 5.1] C:\Users\boris\source\workbench\docxy-issue-50 on issue-50-suite-proje…
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"""

CLAUDE_RUNNING = r"""● Checking mcp/skill coupling, testing dirs, docs mentioning projcore and control verbs
· Hullaballooing… (2m 30s · ↓ 4.9k tokens · thinking some more with high effort)
  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude's current
     work
───────────────────────────────────────────────────────────────────────────────────────
>
───────────────────────────────────────────────────────────────────────────────────────
  [Fable 5.1] C:\Users\boris\source\workbench\docxy-issue-50 on issue-50-suite-proje…
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"""

CODEX_IDLE = r"""• No unread mail. Waiting for the next “Chat from Workbench:” notification; no files
  edited.
──────────────────────────────────────────────────
› Ask Codex to do anything
  gpt-6-astra high · ~\source\workbench\agworkbench-issue-1 · Wait for draft plan"""

CODEX_QUEUED = r"""• Explored
  └ Read SKILL.md (workbench-implementer skill)
───────────────────────────────────────────────────────────────────────────────────────
◦ Working (11s • esc to interrupt)
• Queued follow-up inputs
  ↳ Chat from Workbench: workbench mail from claude: plan v1 for #50 (step 1:
    projcore::editor) [id 20260922T193916Z-claude-1528] - read it with: python C:
    \Users\boris\source\agworkbench\lib\agmsg.py read 20260922T193916Z-claude-1528
    …
    alt + ↑ edit last queued message
› Ask Codex to do anything
  gpt-6-astra high · ~\source\workbench\docxy-issue-50 · Wait for implementation plan"""

CODEX_UNSUBMITTED = r"""──────────────────────────────────────────────────
› Chat from Workbench: workbench mail from claude: plan v1 for #50 step 2 (Project tab
  kind in the suite) [id 20260922T221152Z-claude-7be1] - read it with: python C:
  \Users\boris\source\agworkbench\lib\agmsg.py read 20260922T221152Z-claude-7be1
  (AI_HUB=C:\Users\boris\source\workbench\docxy-issue-50\.workbench)
  gpt-6-astra high · ~\source\workbench\docxy-issue-50 · Wait for implementation plan"""


TEXT = (r"Chat from Workbench: workbench mail from claude: plan v1 for #50 (step 1: "
        r"projcore::editor) [id 20260922T193916Z-claude-1528] - read it with: python C:\Users\boris\source\agworkbench\lib\agmsg.py "
        r"read 20260922T193916Z-claude-1528 (AI_HUB=C:\Users\boris\source\workbench\docxy-issue-50\.workbench)")


def codex(content, prefix=''):
    return prefix + '\n' + '─' * 50 + '\n› ' + content + '\n  gpt-6-astra high · fixture'


def claude(content):
    return CLAUDE_IDLE.replace('\n>\n', '\n> ' + content + '\n')


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def pause(self, seconds):
        self.t += seconds


class FakeAgw:
    def __init__(self, tool='codex', frames=None, after=None):
        self.tool = tool
        self.frames = list(frames) if frames else None
        self.after = after
        self.keys = []
        self.reads = 0
        self.read_errors = {}
        self.type_errors = {}

    def pane_text(self, pane):
        self.reads += 1
        if self.reads in self.read_errors:
            raise self.read_errors[self.reads]
        if self.frames:
            if len(self.frames) > 1:
                return self.frames.pop(0)
            return self.frames[0]
        if not self.keys:
            return CODEX_IDLE if self.tool == 'codex' else CLAUDE_IDLE
        if len(self.keys) == 1:
            return codex(self.keys[0]) if self.tool == 'codex' else claude(self.keys[0])
        if self.after:
            return self.after(self)
        return CODEX_IDLE if self.tool == 'codex' else CLAUDE_IDLE

    def type_into(self, pane, text):
        self.keys.append(text)
        if len(self.keys) in self.type_errors:
            raise self.type_errors[len(self.keys)]


class Submission(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    @contextlib.contextmanager
    def environment(self, fake):
        with patch.object(agw, 'pane_text', fake.pane_text), patch.object(agw, 'type_into', fake.type_into), \
                patch.object(peerchat, 'now', self.clock.now), patch.object(peerchat, 'pause', self.clock.pause):
            yield

    def send(self, fake, text=TEXT):
        with self.environment(fake):
            try:
                return peerchat.send('pane', peerchat.PROFILES[fake.tool], text, dry_run=False, retry=True)
            finally:
                self.assertEqual(1, fake.keys.count(text), 'a send must never retype its text')

    def test_submitted_first_time_for_both_profiles(self):
        for tool, key in [('codex', '\t'), ('claude', '\n')]:
            with self.subTest(tool=tool):
                fake = FakeAgw(tool)
                self.assertEqual('submitted', self.send(fake))
                self.assertEqual([TEXT, key], fake.keys)

    def test_captured_queue_identifies_this_message_only(self):
        for frame, expected in [(CODEX_QUEUED, 'queued'),
                                (CODEX_QUEUED.replace('193916Z', '193917Z'), 'submitted')]:
            with self.subTest(expected=expected):
                fake = FakeAgw(after=lambda f: frame)
                self.assertEqual(expected, self.send(fake))
                self.assertEqual([TEXT, '\t'], fake.keys)

    def test_queue_label_requires_matching_text_as_well_as_id(self):
        frame = CODEX_QUEUED.replace('step 1:', 'step 2:')
        self.assertEqual('submitted', self.send(FakeAgw(after=lambda f: frame)))

    def test_submit_and_queue_after_retries_report_the_path(self):
        for retry, completed, expected in [(1, CODEX_IDLE, 'submitted after retry 1'),
                                            (2, CODEX_QUEUED, 'queued after retry 2')]:
            with self.subTest(retry=retry):
                fake = FakeAgw(after=lambda f: completed if len(f.keys) >= retry + 2 else codex(TEXT))
                self.assertEqual(expected, self.send(fake))
                self.assertEqual([TEXT] + ['\t'] * (retry + 1), fake.keys)

    def test_idle_codex_uses_one_return_after_tabs_fail(self):
        fake = FakeAgw(after=lambda f: CODEX_IDLE if f.keys[-1] == '\n' else codex(TEXT))
        self.assertEqual('submitted after Return', self.send(fake))
        self.assertEqual([TEXT, '\t', '\t', '\t', '\n'], fake.keys)

    def test_exhausted_retries_quote_the_composer_without_retyping(self):
        for tool, keys in [('codex', ['\t', '\t', '\t', '\n']), ('claude', ['\n'] * 3)]:
            with self.subTest(tool=tool):
                fake = FakeAgw(tool, after=lambda f: codex(TEXT) if f.tool == 'codex' else claude(TEXT))
                with self.assertRaisesRegex(peerchat.Failed, 'pointer still unsent') as caught:
                    self.send(fake)
                self.assertIn(repr(TEXT), str(caught.exception))
                self.assertEqual([TEXT] + keys, fake.keys)

    def test_busy_or_queued_evidence_anywhere_vetoes_return(self):
        for marker, padding in [('Working (esc to interrupt)', 0), ('Working (esc to interrupt)', 20),
                                ('Working (esc to interrupt)', 50), ('Queued follow-up inputs', 50)]:
            with self.subTest(marker=marker, padding=padding):
                frame = codex(TEXT, marker + '\n' + 'old row\n' * padding)
                fake = FakeAgw(after=lambda f: frame)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertEqual([TEXT, '\t', '\t', '\t'], fake.keys)

    def test_old_same_id_queue_cannot_hide_unsent_or_changed_composer(self):
        for draft in [TEXT, 'check if codex replied']:
            with self.subTest(draft=draft):
                frame = CODEX_QUEUED.replace('› Ask Codex to do anything', '› ' + draft)
                fake = FakeAgw(after=lambda f: frame)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                expected = ['\t'] * (3 if draft == TEXT else 1)
                self.assertEqual([TEXT] + expected, fake.keys)

    def test_changed_drafts_fail_before_first_submit_and_during_polling(self):
        for draft in ['check if codex replied', TEXT + ' and more', TEXT.replace('step 1:', 'step 2:')]:
            for before_submit in [True, False]:
                with self.subTest(draft=draft, before_submit=before_submit):
                    fake = (FakeAgw(frames=[CODEX_IDLE, codex(draft)]) if before_submit
                            else FakeAgw(after=lambda f: codex(draft)))
                    with self.assertRaisesRegex(peerchat.Failed, 'other than the attempted pointer'):
                        self.send(fake)
                    self.assertEqual([TEXT] + ([] if before_submit else ['\t']), fake.keys)

    def test_short_wrapped_and_clipped_text_can_be_verified(self):
        cases = [('hello', 'hello'), (TEXT, TEXT[:70] + '\n  ' + TEXT[70:]),
                 (TEXT, TEXT[:90])]
        for text, rendered in cases:
            with self.subTest(rendered=rendered):
                fake = FakeAgw(frames=[CODEX_IDLE, codex(rendered), CODEX_IDLE])
                self.assertEqual('submitted', self.send(fake, text))
                self.assertEqual([text, '\t'], fake.keys)

    def test_captured_unsubmitted_frame_really_holds_a_pointer(self):
        content = peerchat.composer(peerchat.PROFILES['codex'], CODEX_UNSUBMITTED)
        self.assertIn('[id 20260922T221152Z-claude-7be1]', content)
        fake = FakeAgw(frames=[CODEX_IDLE, CODEX_UNSUBMITTED])
        with self.assertRaisesRegex(peerchat.Failed, 'pointer still unsent'):
            self.send(fake, content)
        self.assertEqual([content, '\t', '\t', '\t', '\n'], fake.keys)

    def test_dialog_and_disappearance_after_submit_withhold_further_keys(self):
        for frame, reason in [(codex(TEXT) + '\n› 1. Yes', 'dialog'), ('─' * 50, 'disappeared')]:
            with self.subTest(reason=reason):
                fake = FakeAgw(after=lambda f: frame)
                with self.assertRaisesRegex(peerchat.Failed, reason):
                    self.send(fake)
                self.assertEqual([TEXT, '\t'], fake.keys)

    def test_fresh_retry_boundary_rejects_dialog_disappearance_or_new_draft(self):
        for changed in [codex(TEXT) + '\n› 1. Yes', '─' * 50, codex('another draft')]:
            with self.subTest(changed=changed):
                deadline_seen = False

                def after(fake):
                    nonlocal deadline_seen
                    if deadline_seen:
                        return changed
                    if self.clock.t >= 2 * peerchat.SETTLE + peerchat.SUBMIT_TIMEOUT:
                        deadline_seen = True
                    return codex(TEXT)

                self.clock.t = 0
                fake = FakeAgw(after=after)
                with self.assertRaises(peerchat.Failed):
                    self.send(fake)
                self.assertTrue(deadline_seen)
                self.assertEqual([TEXT, '\t'], fake.keys)

    def test_post_write_control_errors_are_failed_with_the_phase(self):
        cases = [('read', 2, 'verifying typed text'), ('read', 3, 'verifying submit'),
                 ('type', 1, 'typing text'), ('type', 2, 'submitting'), ('type', 3, 'retry 1'),
                 ('type', 5, 'Return fallback')]
        for kind, index, phase in cases:
            for error_type in [agw.CtlError, OSError]:
                with self.subTest(kind=kind, index=index, error_type=error_type):
                    fake = FakeAgw(after=lambda f: codex(TEXT))
                    errors = fake.read_errors if kind == 'read' else fake.type_errors
                    errors[index] = error_type('lost terminal')
                    with self.assertRaisesRegex(peerchat.Failed, phase):
                        self.send(fake)

    def test_precheck_control_error_stays_prewrite(self):
        fake = FakeAgw()
        fake.read_errors[1] = agw.CtlError('not reachable')
        with self.environment(fake), self.assertRaises(agw.CtlError):
            peerchat.send_once('pane', peerchat.PROFILES['codex'], TEXT, dry_run=False)
        self.assertEqual([], fake.keys)

    def test_dry_run_only_prechecks(self):
        fake = FakeAgw()
        with self.environment(fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual('dry-run', peerchat.send_once('pane', peerchat.PROFILES['codex'], TEXT, dry_run=True))
        self.assertEqual([], fake.keys)
        self.assertEqual(1, fake.reads)

    def test_cli_prints_valid_json(self):
        fake = FakeAgw()
        output = io.StringIO()
        with self.environment(fake), patch.object(sys, 'argv', ['peerchat', '--to', 'codex', '--text', 'hello', '--label', '']), \
                patch.object(peerchat, 'resolve_target', return_value=('pane', peerchat.PROFILES['codex'], 'codex')), \
                patch.object(agw, 'my_pane', return_value='other'), contextlib.redirect_stdout(output):
            self.assertEqual(0, peerchat.main())
        self.assertEqual({'sent': 'submitted', 'to': 'codex', 'pane': 'pane'}, json.loads(output.getvalue()))


class CapturedBusyFrames(unittest.TestCase):
    def test_activity_matches_captured_frames(self):
        for frame, expected in [(CLAUDE_IDLE, False), (claude('check if codex replied'), False),
                                (CLAUDE_RUNNING, True), (CODEX_IDLE, False), (CODEX_QUEUED, True),
                                (CODEX_UNSUBMITTED, False)]:
            with self.subTest(frame=frame):
                self.assertEqual(expected, peerchat.is_busy(frame))
