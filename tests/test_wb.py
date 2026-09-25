"""Helper workspace selection and inbox waiting; no live terminal calls or real sleeps."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import agw
import hub
import wb


class QueueReports(unittest.TestCase):
    def setUp(self):
        import conductor
        self.q = conductor
        self.folder = Path(__file__).resolve().parent.parent / ('test wb queue ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.loop = str(uuid.uuid4())
        self.q.atomic_json(self.state / 'queue-member.json', dict(queue=str(self.folder / 'queue.json'), repo='o/r', number=1))
        self.q.atomic_json(self.state / 'claude.json', dict(sessionId=self.loop))
        self.enterContext(patch.dict(os.environ, AI_HUB=str(self.folder / '.workbench'), CLAUDE_CODE_SESSION_ID=self.loop))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('terminal access')))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def report(self, state, pr=None, reason=None):
        return wb.cmd_loop_state(wb.argparse.Namespace(state=state, pr=pr, reason=reason))

    def test_reports_keep_pr_increment_revision_and_need_no_terminal(self):
        self.assertEqual(0, self.report('pr-open', pr='https://github.com/o/r/pull/2'))
        self.assertEqual(0, self.report('blocked', reason='human answer'))
        self.assertEqual(0, self.report('resumed'))
        report = self.q.read_json(self.state / 'loop.json')
        self.assertEqual(3, report['rev'])
        self.assertEqual(self.loop, report['loopId'])
        self.assertEqual('https://github.com/o/r/pull/2', report['pr'])
        self.assertIsNone(report['reason'])
        self.assertEqual([], list(self.state.glob('*.tmp')))

    def test_invalid_state_url_reason_or_identity_does_not_publish(self):
        for state, pr, reason in [('bad', None, None), ('pr-open', None, None),
                                  ('pr-open', 'https://github.com/other/repo/pull/1', None), ('blocked', None, None)]:
            self.assertEqual(2, self.report(state, pr, reason))
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=str(uuid.uuid4())):
            self.assertEqual(2, self.report('blocked', reason='x'))
        self.assertFalse((self.state / 'loop.json').exists())

    def test_queue_instructions_are_at_each_decision_point(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text()
        for heading, next_heading, needle in [
            ('## Phase 2', '## Phase 3', 'loop-state blocked'),
            ('## Phase 4', '## Phase 5', 'loop-state blocked'),
            ('## Phase 6', '## Phase 7', 'loop-state pr-open --pr'),
            ('## Phase 7', '## Rules', 'loop-state blocked --reason "PR closed"'),
        ]:
            self.assertIn(needle, text.split(heading)[1].split(next_heading)[0])
        phase6 = text.split('## Phase 6')[1].split('## Phase 7')[0]
        self.assertIn('Do not open revdiff automatically', phase6)
        self.assertIn('Outside queue mode', phase6)
        self.assertIn('loop-state resumed', text)
        self.assertIn('loop-state blocked --reason "mail waiter configuration error"', text)


class HelperWorkspace(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.tree = self.enterContext(patch.object(agw, 'tree', return_value={
            'workspaces': [
                {'id': 'active-workspace', 'active': True,
                 'sessions': [{'id': 'active-session'}]},
                {'id': 'caller-workspace', 'sessions': [
                    {'id': 'caller-session', 'paneIds': ['caller-left', 'caller-right']},
                    {'id': 'unsplit-session'}]},
            ]}))
        self.request = self.enterContext(patch.object(agw, 'request', return_value='new-session'))
        self.stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.stderr))

    def test_both_helpers_use_callers_workspace_even_when_another_is_active(self):
        os.environ.update(AGWINTERM_PANE_ID='caller-right', AGWINTERM_SESSION_ID='active-session')
        for name, select in [('revmux', False), ('human-review', True)]:
            self.assertEqual('new-session', wb.open_session(name, Path('checkout'), 'command', select))
            args = self.request.call_args.kwargs['args']
            self.assertEqual('session.new', self.request.call_args.args[0])
            self.assertEqual('caller-workspace', args['workspace'])
            self.assertEqual(name, args['name'])
            self.assertEqual('checkout', args['cwd'])
            self.assertEqual('command', args['command'])
            self.assertEqual(not select, args.get('no-select', False))
        self.assertEqual('', self.stderr.getvalue())

    def test_session_id_is_used_when_pane_id_is_absent(self):
        os.environ['AGWINTERM_SESSION_ID'] = 'unsplit-session'
        wb.open_session('revmux', Path('checkout'), 'command', False)
        self.assertEqual('caller-workspace', self.request.call_args.kwargs['args']['workspace'])
        self.assertEqual('', self.stderr.getvalue())

    def test_unknown_or_missing_pane_omits_workspace_and_warns_once(self):
        for pane in ['', 'missing-pane']:
            os.environ['AGWINTERM_PANE_ID'] = pane
            self.stderr.seek(0)
            self.stderr.truncate()
            wb.open_session('revmux', Path('checkout'), 'command', False)
            self.assertNotIn('workspace', self.request.call_args.kwargs['args'])
            self.assertEqual(1, len(self.stderr.getvalue().splitlines()))
            self.assertIn('workspace', self.stderr.getvalue())


class WaitMail(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb ' + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder), 'AI_BOX': 'claude'}, clear=True))
        hub.reload_paths()
        self.t = 0.0
        self.sleeps = []
        self.on_sleep = lambda: None
        self.enterContext(patch.object(wb, 'now', lambda: self.t))
        self.enterContext(patch.object(wb, 'pause', self.advance))
        self.enterContext(patch.object(agw, 'request', side_effect=AssertionError('terminal request')))
        self.enterContext(patch.object(agw, 'tree', side_effect=AssertionError('terminal tree lookup')))
        self.enterContext(patch.object(wb.subprocess, 'run', side_effect=AssertionError('subprocess run')))
        self.enterContext(patch.object(wb.subprocess, 'Popen', side_effect=AssertionError('subprocess Popen')))

    def advance(self, seconds):
        self.assertGreater(seconds, 0)
        self.sleeps.append(seconds)
        self.t += seconds
        self.on_sleep()

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['wb', 'wait-mail', *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = wb.main()
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def post(self, box='claude', subject='review ready'):
        return hub.write_message(to=box, sender='codex', subject=subject, body='private message body')

    def files(self):
        return {path.relative_to(self.folder): path.read_bytes()
                for path in self.folder.rglob('*') if path.is_file()}

    def test_unread_at_start_returns_every_message_immediately_without_consuming_it(self):
        paths = [self.post(subject='first review'), self.post(subject='second review')]
        before = self.files()
        code, output, error = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual('', error)
        self.assertEqual(2, len(output.splitlines()))
        for path, subject in zip(paths, ['first review', 'second review']):
            self.assertIn(f'NEW MAIL: {path.stem} codex {subject}\n', output)
        self.assertNotIn('private message body', output)
        self.assertEqual([], self.sleeps)
        self.assertEqual(before, self.files())

    def test_cp1252_output_replaces_unicode_subject_without_losing_wakeup(self):
        subject = 'review ? \U0001f600 \u0436 \u2192'
        path = self.post(subject=subject)
        stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
        with io.TextIOWrapper(stdout_bytes, encoding='cp1252', errors='strict') as out, \
                io.TextIOWrapper(stderr_bytes, encoding='cp1252', errors='strict') as err, \
                patch.object(sys, 'argv', ['wb', 'wait-mail', '--timeout', '0']), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(0, wb.main())
            out.flush()
            err.flush()
            self.assertEqual([f'NEW MAIL: {path.stem} codex review ? ? ? ?'],
                             stdout_bytes.getvalue().decode('cp1252').splitlines())
            self.assertEqual(b'', stderr_bytes.getvalue())
        self.assertEqual(subject, hub.parse_message(path)['subject'])

    def test_cp1252_stderr_can_report_invalid_unicode_box(self):
        stderr_bytes = io.BytesIO()
        with io.TextIOWrapper(stderr_bytes, encoding='cp1252', errors='strict') as err, \
                patch.object(sys, 'argv', ['wb', 'wait-mail', '--box', '\u0436']), \
                contextlib.redirect_stderr(err):
            self.assertEqual(2, wb.main())
            err.flush()
            self.assertIn('bad box name', stderr_bytes.getvalue().decode('cp1252'))

    def test_interval_and_timeout_upper_bounds_are_inclusive(self):
        self.post()  # All accepted inputs return immediately, even before the bounds fix.
        self.assertEqual(0, self.invoke('--interval', '3600', '--timeout', '1440')[0])
        for option, values in [('interval', ['3600.001', '1e308']), ('timeout', ['1440.001', '1e308'])]:
            for value in values:
                with self.subTest(option=option, value=value):
                    code, output, error = self.invoke(f'--{option}={value}')
                    self.assertEqual(2, code)
                    self.assertEqual('', output)
                    self.assertIn(f'--{option}', error)
        self.assertEqual([], self.sleeps)

    def test_read_and_archived_messages_do_not_wake_but_later_unread_mail_does(self):
        read = self.post(subject='already read')
        archived = self.post(subject='archived')
        hub.mark_read(read)
        archived.rename(archived.parent / 'archive' / archived.name)
        posted = []
        self.on_sleep = lambda: posted.append(self.post(subject='new reply'))
        code, output, error = self.invoke('--timeout', '1', '--interval', '2')
        self.assertEqual(0, code)
        self.assertEqual('', error)
        self.assertEqual([2], self.sleeps)
        self.assertEqual(f'NEW MAIL: {posted[0].stem} codex new reply\n', output)
        self.assertTrue(posted[0].is_file())

    def test_timeout_clamps_the_last_sleep_and_does_not_create_a_missing_box(self):
        code, output, error = self.invoke('--timeout', '0.25', '--interval', '10')
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertEqual('no new mail in 0.25 minutes\n', output)
        self.assertEqual(2, len(self.sleeps))
        self.assertEqual(10, self.sleeps[0])
        self.assertAlmostEqual(5, self.sleeps[1])
        self.assertAlmostEqual(15, self.t)
        self.assertEqual([], list(self.folder.iterdir()))

    def test_default_timeout_is_55_minutes_and_default_interval_is_10_seconds(self):
        code, output, error = self.invoke()
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertEqual('no new mail in 55 minutes\n', output)
        self.assertAlmostEqual(3300, self.t)
        self.assertTrue(all(0 < seconds <= 10 for seconds in self.sleeps))
        self.assertEqual(10, self.sleeps[0])

    def test_zero_timeout_checks_exactly_once_with_and_without_mail(self):
        for present in [False, True]:
            if present:
                self.post()
            with patch.object(hub, 'unread', wraps=hub.unread) as unread:
                code, _, _ = self.invoke('--timeout', '0')
            self.assertEqual(0 if present else 3, code)
            unread.assert_called_once_with('claude')
            self.assertEqual([], self.sleeps)

    def test_reply_between_timed_out_waiter_and_replacement_is_not_lost(self):
        code, _, _ = self.invoke('--timeout', '0.1')
        self.assertEqual(3, code)
        path = self.post()
        sleeps = list(self.sleeps)
        code, output, _ = self.invoke('--timeout', '0')
        self.assertEqual(0, code)
        self.assertIn(path.stem, output)
        self.assertEqual(sleeps, self.sleeps)

    def test_box_precedence_and_registry_pane_fallback(self):
        paths = {box: self.post(box) for box in ['claude', 'codex', 'reviewer']}
        hub.save_registry({'agents': {'reviewer': {'box': 'reviewer', 'pane': 'registered-pane'},
                                      'codex': {'box': 'codex', 'pane': 'other-pane'}}})
        cases = [
            (['--box', 'codex'], {'AI_BOX': 'claude', 'AGWINTERM_PANE_ID': 'registered-pane'}, 'codex'),
            ([], {'AI_BOX': 'claude', 'AGWINTERM_PANE_ID': 'registered-pane'}, 'claude'),
            ([], {'AGWINTERM_PANE_ID': 'registered-pane', 'AGWINTERM_SESSION_ID': 'other-pane'}, 'reviewer'),
            ([], {'AGWINTERM_SESSION_ID': 'registered-pane'}, 'reviewer'),
            ([], {'AGWINTERM_PANE_ID': 'unknown-pane'}, 'claude'),
            ([], {}, 'claude'),
        ]
        for args, env, expected in cases:
            with self.subTest(env=env, args=args), \
                    patch.dict(os.environ, {'AI_HUB': str(self.folder), **env}, clear=True):
                code, output, error = self.invoke(*args, '--timeout', '0')
                self.assertEqual(0, code)
                self.assertEqual('', error)
                self.assertEqual(f'NEW MAIL: {paths[expected].stem} codex review ready\n', output)
        self.assertEqual([], self.sleeps)

    def test_environment_hub_is_reloaded_before_scanning(self):
        wrong = self.folder / 'old-hub'
        wrong.mkdir()
        with patch.dict(os.environ, {'AI_HUB': str(wrong)}):
            hub.reload_paths()
            self.post(subject='wrong hub')
        code, output, error = self.invoke('--timeout', '0')
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertNotIn('wrong hub', output)
        self.assertEqual(self.folder, hub.HUB)

    def test_missing_nonexistent_or_file_hub_is_an_error(self):
        regular_file = self.folder / 'not-a-directory'
        regular_file.write_text('file', encoding='utf-8')
        for root in ['', str(self.folder / 'missing'), str(regular_file)]:
            with self.subTest(root=root), patch.dict(os.environ, {'AI_HUB': root}):
                code, output, error = self.invoke('--timeout', '0')
                self.assertEqual(2, code)
                self.assertEqual('', output)
                self.assertIn('AI_HUB', error)
        self.assertEqual([], self.sleeps)

    def test_invalid_box_names_are_errors_instead_of_falling_back(self):
        for box in ['../outside', 'Bad Box', '']:
            with self.subTest(box=box):
                code, output, error = self.invoke('--box', box, '--timeout', '0')
                self.assertEqual(2, code)
                self.assertEqual('', output)
                self.assertIn('bad box name', error)
        with patch.dict(os.environ, {'AI_BOX': '../outside'}):
            self.assertEqual(2, self.invoke('--timeout', '0')[0])
        self.assertEqual([], self.sleeps)

    def test_invalid_intervals_and_timeouts_fail_without_sleeping(self):
        for option, values in [('interval', ['0', '-1', 'nan', 'inf', '-inf', 'invalid']),
                               ('timeout', ['-1', 'nan', 'inf', '-inf', 'invalid'])]:
            for value in values:
                with self.subTest(option=option, value=value):
                    code, output, error = self.invoke(f'--{option}={value}')
                    self.assertEqual(2, code)
                    self.assertEqual('', output)
                    self.assertIn(f'--{option}', error)
        self.assertEqual([], self.sleeps)

    def test_file_read_by_another_agent_between_listing_and_parse_is_skipped(self):
        path = self.post()
        parse = hub.parse_message

        def read_elsewhere(candidate):
            hub.mark_read(candidate)
            return parse(candidate)  # FileNotFoundError from the path that was listed.

        with patch.object(hub, 'parse_message', side_effect=read_elsewhere):
            code, output, error = self.invoke('--timeout', '0')
        self.assertEqual(3, code)
        self.assertEqual('', error)
        self.assertNotIn('NEW MAIL', output)
        self.assertTrue((path.parent / 'read' / path.name).is_file())

class RevmuxProfile(unittest.TestCase):
    """#20: the review round's profile follows the implementer the launcher saved for the checkout."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb revmux ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        (self.folder / 'scope.md').write_text('scope', encoding='utf-8')
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench')}))
        self.opened = self.enterContext(patch.object(wb, 'open_session', return_value='sid'))
        self.enterContext(patch.object(wb, 'issue_number', return_value='20'))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def run_round(self, *extra):
        with patch.object(sys, 'argv', ['wb.py', 'revmux', '--round', '1', '--scope', 'scope.md', *extra]):
            self.assertEqual(0, wb.main())
        return self.opened.call_args.args[2]

    def save(self, text):
        (self.folder / '.workbench/state/implementer.json').write_text(text, encoding='utf-8')

    def test_no_saved_implementer_keeps_comprehensive(self):
        self.assertIn("-Profile 'comprehensive'", self.run_round())

    def test_claude_implementer_uses_the_saved_claude_only_profile(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'claude-only'", self.run_round())

    def test_explicit_profile_wins(self):
        self.save('{"tool": "claude", "revmuxProfile": "claude-only"}')
        self.assertIn("-Profile 'codex-final'", self.run_round('--profile', 'codex-final'))

    def test_unreadable_or_unsafe_saved_profile_falls_back(self):
        for text in ('not json', '{"revmuxProfile": "x\' ; calc"}', '[]'):
            with self.subTest(text=text):
                self.save(text)
                self.assertIn("-Profile 'comprehensive'", self.run_round())


HEAD = 'a' * 40
MARK = wb.PLANNER_MARKER


def clean_pr(**changes):
    pr = dict(number=7, url='https://github.com/o/r/pull/7', state='OPEN', mergeable='MERGEABLE',
              mergeStateStatus='CLEAN', reviewDecision='', headRefOid=HEAD, reviews=[], comments=[])
    pr.update(changes)
    return pr


def comment(body, when, who='yeroo'):
    return {'body': body, 'createdAt': when, 'author': {'login': who}}


class MergeCheck(unittest.TestCase):
    """#23: the read-only auto-merge gate. Conditions are pure; one test covers the gh wiring."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb merge ' + uuid.uuid4().hex)
        self.state = self.folder / '.workbench/state'
        self.state.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'), 'AI_BOX': 'claude'}))
        hub.reload_paths()
        self.seen([7])
        self.out = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.out))

    def seen(self, numbers):
        (self.state / 'relay.json').write_text(json.dumps({'seen_open': numbers}), encoding='utf-8')

    def mail(self, sender, folder=''):
        box = self.folder / '.workbench/inbox/claude' / folder
        box.mkdir(parents=True, exist_ok=True)
        (box / f'm-{sender}.md').write_text(f'---\nid: m-{sender}\nfrom: {sender}\nto: claude\nsubject: note\n---\nbody\n',
                                            encoding='utf-8')

    def failures(self, pr=None, inline=(), head=HEAD):
        return wb.merge_failures(pr or clean_pr(), list(inline), head, self.folder)

    def run_check(self, *args, fetched=None):
        fetch = patch.object(wb, 'fetch_pr', return_value=fetched or (clean_pr(), []))
        with fetch, patch.object(sys, 'argv', ['wb.py', 'merge-check', *args]):
            return wb.main()

    # --- the verdict ------------------------------------------------------------------------------

    def test_every_condition_holding_prints_ok(self):
        self.assertEqual([], self.failures())
        self.assertEqual(0, self.run_check('--pr', '7', '--head', HEAD))
        self.assertEqual('ok', self.out.getvalue().strip())

    def test_failures_exit_1_one_prefixed_line_each(self):
        self.seen([])
        code = self.run_check('--pr', '7', '--head', HEAD,
                              fetched=(clean_pr(state='CLOSED', headRefOid='b' * 40), []))
        self.assertEqual(1, code)
        lines = self.out.getvalue().strip().splitlines()
        self.assertEqual(['state:', 'relay:', 'head:'], [line.split()[0] for line in lines])
        self.assertNotIn('ok', lines)

    def test_head_is_required_and_must_be_a_full_sha(self):
        for head in ([], ['--head', 'abc1234'], ['--head', 'g' * 40]):
            with self.subTest(head=head), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = self.run_check('--pr', '7', *head)
                except SystemExit as exit_:
                    code = exit_.code
                self.assertEqual(2, code)
        self.assertNotIn('ok', self.out.getvalue())

    # --- (a) GitHub's merge state --------------------------------------------------------------

    def test_only_open_mergeable_clean_passes(self):
        for status in ('UNSTABLE', 'BLOCKED', 'BEHIND', 'DIRTY', 'UNKNOWN', 'HAS_HOOKS'):
            with self.subTest(status=status):
                lines = self.failures(clean_pr(mergeStateStatus=status))
                self.assertEqual([f'mergeable: merge state is {status}, not CLEAN' +
                                  (' (retry in ~30s)' if status == 'UNKNOWN' else '')], lines)
        self.assertIn('mergeable: GitHub says CONFLICTING', self.failures(clean_pr(mergeable='CONFLICTING')))
        self.assertIn('mergeable: GitHub says UNKNOWN (retry in ~30s)', self.failures(clean_pr(mergeable='UNKNOWN')))
        self.assertIn('state: PR is MERGED, not OPEN', self.failures(clean_pr(state='MERGED')))

    # --- (b) reviews ------------------------------------------------------------------------------

    def test_changes_requested_blocks_until_that_reviewer_approves(self):
        request = {'state': 'CHANGES_REQUESTED', 'body': '', 'submittedAt': '2026-09-24T10:00:00Z', 'author': {'login': 'ann'}}
        approve = dict(request, state='APPROVED', submittedAt='2026-09-24T11:00:00Z')
        self.assertEqual(['review: ann requested changes (2026-09-24T10:00:00Z)'],
                         self.failures(clean_pr(reviews=[request])))
        self.assertEqual([], self.failures(clean_pr(reviews=[approve, request])))
        self.assertEqual(['review: the review decision is CHANGES_REQUESTED'],
                         self.failures(clean_pr(reviewDecision='CHANGES_REQUESTED')))

    # --- (c) holds --------------------------------------------------------------------------------

    def test_a_hold_from_the_author_login_blocks_unless_the_planner_marked_it(self):
        hold = comment('please hold, I want to look', '2026-09-24T10:00:00Z', who='yeroo')
        lines = self.failures(clean_pr(comments=[hold]))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith('hold: yeroo at 2026-09-24T10:00:00Z: "please hold'), lines)
        marked = comment('please hold, I want to look\n' + MARK, '2026-09-24T10:00:00Z')
        self.assertEqual([], self.failures(clean_pr(comments=[marked])))

    def test_holds_do_not_expire_and_only_a_later_unmarked_lift_releases_them(self):
        hold = comment("don't merge yet", '2026-09-01T00:00:00Z')      # long before any later commit
        self.assertTrue(self.failures(clean_pr(comments=[hold])))
        lift = comment('go ahead', '2026-09-24T12:00:00Z')
        self.assertEqual([], self.failures(clean_pr(comments=[lift, hold])))
        early_lift = comment('resume', '2026-08-01T00:00:00Z')
        self.assertTrue(self.failures(clean_pr(comments=[hold, early_lift])))
        planner_lift = comment('go ahead\n' + MARK, '2026-09-24T12:00:00Z')
        self.assertTrue(self.failures(clean_pr(comments=[hold, planner_lift])))

    def test_hold_word_matching(self):
        # r16 M1: bodies are normalised (apostrophes, emphasis, whitespace, case) before matching.
        for body, holds in [('hold', True), ('Please WAIT', True), ('do not merge', True), ('dont merge', True),
                            ("don't merge", True), ('Don\u2019t merge this yet', True), ('Don\u02bct merge', True),
                            ('do not\nmerge', True), ('do not  merge', True), ('Do **not** merge', True),
                            ('do-not-merge', True), ('DO NOT MERGE', True), ('waiting on legal', True),
                            ('`wip`', True), ('unhold', False), ('household threshold', False),
                            ('wipe the cache', False), ('hold, then go ahead', True),
                            # r16 M2: negated lifts are holds
                            ("don't go ahead", True), ('do not resume', True), ("don't unhold", True)]:
            with self.subTest(body=body):
                self.assertEqual(holds, bool(self.failures(clean_pr(comments=[comment(body, '2026-09-24T10:00:00Z')]))))

    def test_only_the_hold_author_lifts_it_with_a_bare_directive(self):
        # r16 M2
        hold = comment('hold', '2026-09-24T10:00:00Z', who='yeroo')
        for lift, released in [(comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo'), True),
                               (comment('@claude resume.', '2026-09-24T11:00:00Z', who='yeroo'), True),
                               (comment('Unhold please!', '2026-09-24T11:00:00Z', who='yeroo'), True),
                               (comment('go ahead', '2026-09-24T11:00:00Z', who='ann'), False),
                               (comment('I will resume reviewing tomorrow', '2026-09-24T11:00:00Z', who='yeroo'), False),
                               (comment('go ahead and rename X first', '2026-09-24T11:00:00Z', who='yeroo'), False),
                               (comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo[bot]'), False)]:
            with self.subTest(lift=lift['body'], who=lift['author']['login']):
                self.assertEqual(not released, bool(self.failures(clean_pr(comments=[hold, lift]))))
        both = [hold, comment('wait', '2026-09-24T10:30:00Z', who='ann'),
                comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo')]
        lines = self.failures(clean_pr(comments=both))
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith('hold: ann at'), lines)
        bot_hold = comment('hold', '2026-09-24T10:00:00Z', who='ci[bot]')
        self.assertTrue(self.failures(clean_pr(comments=[bot_hold, comment('go ahead', '2026-09-24T11:00:00Z', who='ci[bot]')])))

    def test_labels_title_and_description_can_hold(self):
        # r16 m3
        for name in ('do-not-merge', 'DO NOT MERGE', 'on hold', 'WIP'):
            with self.subTest(label=name):
                self.assertEqual([f"label: the PR is labelled '{name}'"], self.failures(clean_pr(labels=[{'name': name}])))
        self.assertEqual([], self.failures(clean_pr(labels=[{'name': 'enhancement'}])))
        self.assertTrue(self.failures(clean_pr(title='[WIP] auto-merge'))[0].startswith('hold: the PR title'))
        described = clean_pr(body='Do not merge until #24 lands', author={'login': 'yeroo'})
        self.assertTrue(self.failures(described)[0].startswith('hold: yeroo at PR description'))
        self.assertEqual([], self.failures(clean_pr(body='Do not merge until #24 lands\n' + MARK)))
        lifted = dict(described, comments=[comment('go ahead', '2026-09-24T11:00:00Z', who='yeroo')])
        self.assertEqual([], self.failures(lifted))

    def test_review_bodies_and_inline_comments_can_hold(self):
        review = {'state': 'COMMENTED', 'body': 'hold', 'submittedAt': '2026-09-24T10:00:00Z', 'author': {'login': 'yeroo'}}
        self.assertTrue(self.failures(clean_pr(reviews=[review])))
        inline = {'body': 'wait - this breaks X', 'created_at': '2026-09-24T10:00:00Z', 'user': {'login': 'yeroo'}}
        self.assertTrue(self.failures(inline=[inline]))

    # --- (d) relay, (e) mail, head ------------------------------------------------------------

    def test_unread_human_or_github_mail_blocks(self):
        self.mail('codex')
        self.assertEqual([], self.failures())
        self.mail('human', folder='read')
        self.assertEqual([], self.failures())
        for sender in ('human', 'github'):
            with self.subTest(sender=sender):
                self.mail(sender)
                self.assertTrue(any(line.startswith(f'mail: unread from {sender}') for line in self.failures()))

    def test_an_unreadable_unread_message_fails_closed(self):
        # r16 m1
        self.mail('human')
        with patch.object(hub, 'parse_message', side_effect=UnicodeDecodeError('utf-8', b'x', 0, 1, 'bad')):
            lines = self.failures()
        self.assertEqual(1, len(lines))
        self.assertTrue(lines[0].startswith('mail: cannot read unread message m-human.md'), lines)
        with patch.object(hub, 'parse_message', side_effect=FileNotFoundError('moved')):
            self.assertEqual([], self.failures())

    def test_the_relay_must_have_seen_the_pr_open(self):
        self.seen([6])
        self.assertEqual(["relay: the relay has not recorded PR #7 as seen open yet - wait for its 'PR is open' "
                          "mail, then check again"], self.failures())
        (self.state / 'relay.json').unlink()
        self.assertTrue(self.failures())

    def test_the_tested_head_must_be_the_pr_head(self):
        self.assertTrue(self.failures(head='b' * 40)[0].startswith('head: the PR head is ' + HEAD))
        self.assertEqual([], self.failures(head=HEAD.upper()))

    # --- the gh boundary ------------------------------------------------------------------------

    def test_one_view_and_the_inline_comments_are_fetched(self):
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            payload = clean_pr() if argv[1:3] == ['pr', 'view'] else [[{'body': 'a'}], [{'body': 'b'}]]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), '')
        with patch.object(wb.subprocess, 'run', side_effect=fake):
            pr, inline = wb.fetch_pr('7')
        self.assertEqual(['gh', 'pr', 'view', '7', '--json', wb.PR_FIELDS], calls[0])
        self.assertEqual(['gh', 'api', 'repos/o/r/pulls/7/comments', '--paginate', '--slurp'], calls[1])
        self.assertEqual(2, len(calls))
        self.assertEqual([{'body': 'a'}, {'body': 'b'}], inline)
        for field in ('headRefOid', 'mergeStateStatus', 'reviewDecision', 'reviews', 'comments',
                      'labels', 'title', 'body', 'author'):
            self.assertIn(field, wb.PR_FIELDS)

    def test_a_gh_failure_is_not_ok(self):
        failed = subprocess.CompletedProcess([], 1, '', 'HTTP 502')
        with patch.object(wb.subprocess, 'run', return_value=failed), \
                patch.object(sys, 'argv', ['wb.py', 'merge-check', '--pr', '7', '--head', HEAD]):
            self.assertEqual(1, wb.main())
        self.assertIn('gh: ', self.out.getvalue())
        self.assertNotIn('ok\n', self.out.getvalue())


class Settings(unittest.TestCase):
    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb settings ' + uuid.uuid4().hex)
        (self.folder / '.workbench/state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.config = self.folder / 'config.json'
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.folder / '.workbench'),
                                                 'AGWORKBENCH_CONFIG': str(self.config)}))

    def printed(self, record=None):
        if record is not None:
            (self.folder / '.workbench/state/implementer.json').write_text(record, encoding='utf-8')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(sys, 'argv', ['wb.py', 'settings']):
            self.assertEqual(0, wb.main())
        return out.getvalue().strip()

    def test_defaults_and_records(self):
        self.assertEqual('implementer=codex revmuxProfile=comprehensive autoMerge=false failover=true', self.printed())
        # a #20 record has no autoMerge key: off
        self.assertEqual('implementer=claude revmuxProfile=claude-only autoMerge=false failover=true',
                         self.printed('{"tool": "claude", "revmuxProfile": "claude-only"}'))
        self.assertEqual('implementer=claude revmuxProfile=claude-only autoMerge=true failover=true',
                         self.printed('{"tool": "claude", "revmuxProfile": "claude-only", "autoMerge": true}'))
        self.assertIn('autoMerge=false', self.printed('{"tool": "codex", "autoMerge": "true"}'))

    def test_failover_is_on_unless_the_config_says_false(self):
        # #24
        for text, shown in (('{"failover": false}', 'false'), ('{"failover": true}', 'true'), ('{}', 'true'),
                            ('not json', 'true'), ('{"failover": 0}', 'true')):
            with self.subTest(config=text):
                self.config.write_text(text, encoding='utf-8')
                self.assertTrue(self.printed().endswith('failover=' + shown))


class Handover(unittest.TestCase):
    """#24: the facts for a HANDOVER mail, computed from the mailbox instead of guessed."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parent.parent / ('test wb handover ' + uuid.uuid4().hex)
        self.hub = self.folder / '.workbench'
        (self.hub / 'state').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder)
        self.addCleanup(hub.reload_paths)
        self.enterContext(patch.dict(os.environ, {'AI_HUB': str(self.hub)}))
        hub.reload_paths()

    def mail(self, box, mid, sender, subject, folder=''):
        directory = self.hub / 'inbox' / box / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f'{mid}.md').write_text(f'---\nid: {mid}\nfrom: {sender}\nto: {box}\nsubject: {subject}\n---\nbody\n',
                                             encoding='utf-8')

    def test_the_newest_unanswered_request_is_open(self):
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'plan v1', folder='read')
        self.mail('claude', '20260925T081000Z-codex-0002', 'codex', 're: plan v1', folder='read')
        self.mail('codex', '20260925T082000Z-claude-0003', 'claude', 'IMPLEMENT plan v2', folder='archive')
        self.mail('codex', '20260925T083000Z-human-0004', 'human', 'not from the planner')
        pending, recent = wb.open_request()
        self.assertEqual('20260925T082000Z-claude-0003', pending['id'])
        self.assertEqual(['plan v1', 'IMPLEMENT plan v2'], [m['subject'] for m in recent])

    def test_an_answered_request_is_not_open(self):
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'FIX r1', folder='read')
        self.mail('claude', '20260925T081000Z-codex-0002', 'codex', 'FIXED abc')
        self.assertIsNone(wb.open_request()[0])

    def test_the_command_prints_branch_request_and_status(self):
        self.mail('codex', '20260925T080000Z-claude-0001', 'claude', 'FIX r1')
        runs = [subprocess.CompletedProcess([], 0, 'issue-24-x\n', ''), subprocess.CompletedProcess([], 0, ' M lib/wb.py\n', '')]
        out = io.StringIO()
        with patch.object(wb.subprocess, 'run', side_effect=runs), contextlib.redirect_stdout(out), \
                patch.object(sys, 'argv', ['wb.py', 'handover']):
            self.assertEqual(0, wb.main())
        text = out.getvalue()
        self.assertIn('branch: issue-24-x', text)
        self.assertIn('open request: 20260925T080000Z-claude-0001 "FIX r1" (no reply yet)', text)
        self.assertIn('   M lib/wb.py', text)


class AutoMergeProse(unittest.TestCase):
    """#23: the planner's conditions and the implementer's exclusion are in the prose."""

    def test_planner_phase_6_and_rules(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        phase6 = text.split('## Phase 6')[1].split('## Phase 7')[0]
        for needle in ['wb.py" settings', 'autoMerge=true', 'wb.py" merge-check --pr <N> --head <full sha>',
                       '--match-head-commit <full sha>', 'None is deferred', 'three-round cap',
                       'whole suite passed on the PR head', 'If any condition fails, do not merge',
                       'UNKNOWN', 'go ahead']:
            self.assertIn(needle, ' '.join(phase6.split()) if ' ' in needle else phase6)
        rules = text.split('## Rules')[1]
        self.assertIn('**Never merge** unless auto-merge is on for this checkout', rules)
        self.assertIn('Never approve your own PR', rules)
        self.assertIn(wb.PLANNER_MARKER, text)
        self.assertIn('ends with the planner marker line', text.split('## Phase 5')[1].split('## Phase 6')[0])

    def test_phase_6_waits_for_the_relay_and_rechecks(self):
        # r16 M3
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/start-github-issue.md').read_text(encoding='utf-8')
        auto = ' '.join(text.split('### Auto-merge')[1].split('### The human')[0].split())
        for needle in ['**Wait for the relay first.**', "`PR #N is open` mail from `github`",
                       '**Retryable failures:** `relay:`', '`mail:` (read and handle the mail)', '`UNKNOWN`',
                       'Every other failure is final for this head', '**Check again after any event',
                       'new head with the whole suite re-run on it']:
            self.assertIn(needle, auto)
        self.assertLess(auto.index('Wait for the relay first'), auto.index('merge-check --pr <N> --head <full sha>'))

    def test_implementer_never_merges(self):
        text = (Path(__file__).resolve().parent.parent / 'claude/commands/workbench-implementer.md').read_text(encoding='utf-8')
        self.assertIn("is the planner's act, never yours", text)


class UsageLimitProse(unittest.TestCase):
    """#24: what the planner does on a usage-limit mail, and what a new implementer does on HANDOVER."""
    ROOT = Path(__file__).resolve().parent.parent

    def text(self, path):
        return ' '.join((self.ROOT / path).read_text(encoding='utf-8').split())

    def test_planner_section(self):
        text = self.text('claude/commands/start-github-issue.md')
        section = text.split("## Usage limits")[1].split('## Adopted session')[0]
        for needle in ['usage limit: <box> (<tool>) <kind>', 'failover=true', "the agent's own limit message",
                       'github-workbench <owner/repo#N> -Failover', 'timeout: 600000', 'wb.py" handover',
                       'subject `HANDOVER`', 'uncommitted changes are the previous implementer',
                       'refused** (exit 2)', 'status blocked --sound', '-Implementer <tool>',
                       'never answer it', 'failover=false', 'only a record', 'never types into the limited agent']:
            self.assertIn(needle, section)

    def test_both_implementers_know_handover(self):
        for path in ('claude/commands/workbench-implementer.md', 'codex/skills/workbench-implementer/SKILL.md'):
            with self.subTest(path=path):
                text = self.text(path)
                self.assertIn('## HANDOVER - you replace another implementer mid-loop', text)
                self.assertIn('Run `git status`', text)
                self.assertIn('Continue the phase the mail names', text)


if __name__ == '__main__':
    unittest.main()
