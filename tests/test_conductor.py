"""Queue decisions with fake GitHub/launchers/clock and real process-held file locks."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import conductor as q


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / ('test queue ' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones')}))
        self.enterContext(patch.dict(os.environ, {'AGWORKBENCH_CONFIG': str(self.config),
                                                 'AGWINTERM_ENABLED': '1', 'AGWINTERM_SESSION_ID': str(uuid.uuid4())}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.requests = []
        self.enterContext(patch.object(q.agw, 'request', self.terminal))
        self.enterContext(patch.object(q.agw, 'notify'))
        self.enterContext(patch.object(q.agw, 'set_status'))
        # The in-hand lookups (#28) call gh and the terminal; queue mechanics tests assume none in hand.
        self.real_in_hand = q.in_hand
        self.in_hand = self.enterContext(patch.object(q, 'in_hand', return_value={}))
        self.now = 1000
        self.store = q.Store(self.root / 'queues/o/r.json')
        self.launches = []
        self.pr_state = 'OPEN'

    def terminal(self, command, **kwargs):
        self.requests.append((command, kwargs))
        if command == 'session.new':
            return str(uuid.uuid4())
        if command == 'session.restore':
            return dict(action='pinned', pane=kwargs['target'], command=kwargs['args']['command'])
        raise AssertionError(command)

    def start(self, spec='o/r#1,2,3', **kwargs):
        return q.start_queue(spec, root=self.root / 'queues', **kwargs)

    def gh(self, *args):
        self.assertEqual(('pr', 'view'), args[:2])
        return {'state': self.pr_state}

    def spawn(self, data, m):
        self.launches.append((m['number'], m['attempt'], m['token']))
        directory = Path(m['checkout']) / '.workbench/state'
        directory.mkdir(parents=True, exist_ok=True)
        identity = directory / 'claude.json'
        if not identity.exists():
            q.atomic_json(identity, {'sessionId': str(uuid.uuid4()), 'pane': str(uuid.uuid4())})
        q.atomic_json(directory / 'queue-member.json', dict(queue=str(self.store.path), repo=data['repo'], number=m['number']))
        # This fake launcher stands in for a completed clone as well as the terminal.
        with patch.object(q, 'usable_checkout', return_value=True):
            q.member_result(self.store.path, m['number'], m['attempt'], m['token'], dict(result='ok', sessionId='session'))
        output = self.root / f'output-{m["number"]}.log'
        output.write_text('')
        class Done:
            pid = 123
            def poll(self):
                return 0
        return dict(process=Done(), stream=io.BytesIO(), path=output, started=self.now, attempt=m['attempt'], token=m['token'])

    def worker(self):
        return q.Worker(self.store, self.store.load()['owner']['token'], gh=self.gh,
                        clock=lambda: self.now, spawn=self.spawn)

    def member(self, n=1):
        return q.find_member(self.store.load(), n)

    def report(self, n, state='pr-open', **kwargs):
        m = self.member(n)
        checkout = Path(m['checkout'])
        identity = q.read_json(checkout / '.workbench/state/claude.json')
        if state == 'pr-open':
            kwargs.setdefault('pr', f'https://github.com/o/r/pull/{n}')
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=identity['sessionId']):
            return q.write_loop_state(checkout, state, **kwargs)

    def test_three_issues_release_on_pr_open_without_merge(self):
        self.start()
        worker = self.worker()
        for n in (1, 2, 3):
            worker.tick()
            worker.tick()
            self.assertEqual('active', self.member(n)['state'])
            self.report(n)
            worker.tick()
            self.assertEqual('pr-open', self.member(n)['state'])
        self.assertEqual([1, 2, 3], [x[0] for x in self.launches])
        self.assertTrue(q.finished(self.store.load()))

    def test_parallel_blocked_resumed_and_failed_members_release_slots(self):
        self.start(parallel=2)
        worker = self.worker()
        worker.tick(); worker.tick()
        self.assertEqual([1, 2], [x[0] for x in self.launches])
        self.report(1, 'blocked', reason='human answer needed')
        worker.tick()
        self.assertEqual([1, 2, 3], [x[0] for x in self.launches])
        self.report(1, 'resumed')
        worker.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        self.assertEqual('active', self.member(1)['state'])
        self.report(1)
        worker.tick()
        self.assertEqual('pr-open', self.member(1)['state'])

    def test_incomplete_retry_repairs_and_reuses_existing_report(self):
        self.start('o/r#1')
        worker = self.worker()
        worker.tick()
        first = self.member()
        self.report(1)
        q.member_result(self.store.path, 1, first['attempt'], first['token'], dict(result='incomplete', detail='Codex missing'))
        worker.tick()
        self.assertEqual('failed', self.member()['state'])
        self.start('o/r#1', retry=True)
        worker.tick(); worker.tick()
        self.assertEqual([1, 1], [x[0] for x in self.launches])
        self.assertEqual('pr-open', self.member()['state'])
        self.assertFalse(q.member_result(self.store.path, 1, first['attempt'], first['token'], dict(result='failed')))
        self.assertEqual('pr-open', self.member()['state'])

    def test_failed_members_stay_failed_on_rerun(self):
        self.start('o/r#1')
        with self.store.transaction() as data:
            data['members'][0].update(state='failed', slotReleased=True)
        self.start('o/r#1')
        worker = self.worker(); worker.tick()
        self.assertEqual([], self.launches)
        self.assertTrue(q.finished(self.store.load()))

    def test_loop_revision_identity_and_validation(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        self.report(1, 'blocked', reason='one')
        worker.tick()
        old_revision = self.member()['consumedRev']
        worker.tick()
        self.assertEqual(old_revision, self.member()['consumedRev'])
        directory = Path(self.member()['checkout']) / '.workbench/state'
        q.atomic_json(directory / 'claude.json', {'sessionId': str(uuid.uuid4())})
        report = self.report(1)
        self.assertEqual(1, report['rev'])
        worker.tick()
        self.assertEqual('pr-open', self.member()['state'])
        for change in ({'queue': 'foreign'}, {'repo': 'other/repo'}, {'loopId': str(uuid.uuid4())}, {'rev': 'bad'}):
            q.atomic_json(directory / 'loop.json', dict(report, **change))
            worker.tick()
            self.assertEqual('pr-open', self.member()['state'])
        (directory / 'loop.json').write_text('{broken')
        worker.tick()
        self.assertEqual('pr-open', self.member()['state'])

    def test_closed_orderings_replacement_and_blocked_merge(self):
        for blocked_first in (False, True):
            with self.subTest(blocked_first=blocked_first):
                self.pr_state = 'OPEN'
                self.start('o/r#1')
                worker = self.worker(); worker.tick(); worker.tick()
                self.report(1)
                worker.tick()
                if blocked_first:
                    self.report(1, 'blocked', reason='PR closed')
                self.pr_state = 'CLOSED'; worker.next_pr = 0
                worker.tick()
                if not blocked_first:
                    self.report(1, 'blocked', reason='PR closed'); worker.tick()
                self.assertEqual('blocked', self.member()['state'])
                self.assertEqual('CLOSED', self.member()['prState'])
                self.report(1, 'resumed'); worker.tick()
                self.pr_state = 'OPEN'
                self.report(1, pr='https://github.com/o/r/pull/99'); worker.tick()
                self.assertEqual('OPEN', self.member()['prState'])
                self.report(1, 'blocked', reason='waiting'); worker.tick()
                self.pr_state = 'MERGED'
                resumed = self.worker(); resumed.tick()
                self.assertEqual('merged', self.member()['state'])
                # Separate fixture state for the other ordering.
                self.store.path.unlink()

    def test_delayed_bootstrap_and_live_worker_do_not_create_a_second_session(self):
        self.start('o/r#1')
        self.start('o/r#2')
        self.assertEqual(1, sum(c == 'session.new' for c, _ in self.requests))
        with self.store.transaction() as data:
            data['owner'].update(state='running', session='gone', pid=999999)
        with q.Lock(self.store.worker_lock):
            self.start('o/r#3')
        self.assertEqual(1, sum(c == 'session.new' for c, _ in self.requests))
        self.assertEqual([1, 2, 3], [m['number'] for m in self.store.load()['members']])

    def test_abandoned_bootstrap_fences_late_worker(self):
        self.start('o/r#1')
        old = self.worker()
        with self.store.transaction() as data:
            data['owner']['reservedAt'] -= 91
        self.start('o/r#2')
        self.assertEqual(0, old.run())
        self.assertEqual(2, sum(c == 'session.new' for c, _ in self.requests))
        self.assertEqual([], self.launches)

    def test_live_unpinned_conductor_retries_pin_before_reporting_running(self):
        real_terminal = self.terminal
        def fail_pin(command, **kwargs):
            if command == 'session.restore':
                return {'action': 'failed'}
            return real_terminal(command, **kwargs)
        with patch.object(q.agw, 'request', fail_pin), self.assertRaisesRegex(q.QueueError, 'pin failed'):
            self.start('o/r#1')
        owner = self.store.load()['owner']
        self.assertFalse(owner['pinned'])
        with self.store.transaction() as data:
            data['owner']['state'] = 'running'
        with q.Lock(self.store.worker_lock):
            self.start('o/r#2')
        self.assertTrue(self.store.load()['owner']['pinned'])
        self.assertEqual(owner['token'], self.store.load()['owner']['token'])
        self.assertEqual(1, sum(c == 'session.new' for c, _ in self.requests))
        self.assertEqual(owner['session'], self.requests[-1][1]['target'])

    def test_two_racing_bootstraps_create_one_conductor(self):
        gate, release = threading.Event(), threading.Event()
        original = self.terminal
        failures = []
        def terminal(command, **kwargs):
            if command == 'session.new':
                gate.set()
                self.assertTrue(release.wait(5))
            return original(command, **kwargs)
        def first():
            try:
                self.start('o/r#1')
            except Exception as err:
                failures.append(err)
        with patch.object(q.agw, 'request', terminal):
            thread = threading.Thread(target=first)
            thread.start()
            try:
                self.assertTrue(gate.wait(5))
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.start('o/r#2')
                self.assertIn('in session pending', output.getvalue())
            finally:
                release.set(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(1, sum(c == 'session.new' for c, _ in self.requests))
        self.assertEqual([1, 2], [m['number'] for m in self.store.load()['members']])

    def test_process_lock_is_exclusive_and_released_after_process_exit(self):
        path = self.root / 'real.lock'
        script = ('import sys; sys.path.insert(0, sys.argv[1]); from conductor import Lock; '
                  'lock=Lock(__import__("pathlib").Path(sys.argv[2]),0); lock.acquire(); print("held",flush=True); sys.stdin.readline()')
        child = subprocess.Popen([sys.executable, '-c', script, str(ROOT / 'lib'), str(path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual('held', child.stdout.readline().strip())
            with self.assertRaises(q.QueueError):
                with q.Lock(path, 0):
                    pass
        finally:
            child.communicate('\n', timeout=5)
        with q.Lock(path, 0):
            pass

    def test_append_during_finish_is_seen_and_completed_snapshot_is_written(self):
        self.start('o/r#1')
        worker = self.worker()
        seen = []
        def tick():
            with self.store.transaction() as data:
                for m in data['members']:
                    if m['state'] == 'pending':
                        seen.append(m['number'])
                        m.update(state='pr-open', slotReleased=True)
            if seen == [1]:
                self.start('o/r#2')  # appends while worker.lock is held, before final check
        worker.tick = tick
        with patch.object(q.time, 'sleep'):
            self.assertEqual(0, worker.run())
        self.assertEqual([1, 2], seen)
        self.assertEqual('finished', self.store.load()['owner']['state'])
        self.assertTrue(self.store.path.with_suffix('.md').exists())
        self.start('o/r#3')
        self.assertEqual(2, sum(c == 'session.new' for c, _ in self.requests))

    def test_config_drift_and_missing_saved_checkout(self):
        self.start('o/r#1', parallel=2, yes=True)
        worker = self.worker(); worker.tick(); worker.tick()
        original = self.member()['checkout']
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'moved')}))
        self.start('o/r#2')
        self.assertEqual(original, self.member()['checkout'])
        self.assertEqual(2, self.store.load()['parallel'])
        self.assertEqual(str(self.config), self.store.load()['config'])
        self.assertIn('moved', self.member(2)['checkout'])
        shutil.rmtree(original)
        with self.store.transaction() as data:
            data['members'][0]['state'] = 'failed'
        self.start('o/r#1', retry=True)
        worker.tick()
        self.assertEqual('failed', self.member()['state'])
        self.assertIn('moved or deleted', self.member()['reason'])

    def test_dry_run_does_not_create_any_files_and_corrupt_state_is_preserved(self):
        self.start('o/r#1', dry_run=True)
        self.assertFalse(self.store.path.parent.exists())
        self.start('o/r#1')
        self.store.path.write_text('{bad')
        with self.assertRaisesRegex(q.QueueError, 'repair this file'):
            self.start('o/r#2')
        self.assertEqual('{bad', self.store.path.read_text())

    def test_tick_errors_are_retried_with_throttled_notification(self):
        self.start('o/r#1')
        worker = self.worker()
        errors = [OSError('disk busy'), q.QueueError('lock busy: state'), ValueError('bad report'),
                  KeyError('missing'), TypeError('bad type'), subprocess.TimeoutExpired('git', 30)]
        attempts = []
        def tick():
            attempts.append(1)
            if errors:
                raise errors.pop(0)
            with self.store.transaction() as data:
                data['members'][0].update(state='blocked', slotReleased=True)
        worker.tick = tick
        with patch.object(q.time, 'sleep') as sleep:
            self.assertEqual(0, worker.run())
        self.assertEqual(7, len(attempts))
        self.assertEqual(6, sleep.call_count)
        q.agw.notify.assert_called_once()
        self.assertEqual('completed', q.agw.set_status.call_args.args[0])

    def test_corrupt_state_blocks_and_exits_without_overwriting_it(self):
        self.start('o/r#1')
        worker = self.worker()
        def tick():
            self.store.path.write_text('{broken')
            self.store.load()
        worker.tick = tick
        with patch.object(q.time, 'sleep') as sleep:
            self.assertEqual(1, worker.run())
        sleep.assert_not_called()
        q.agw.notify.assert_called_once()
        self.assertEqual('blocked', q.agw.set_status.call_args.args[0])
        self.assertEqual('{broken', self.store.path.read_text())
        self.assertFalse(self.store.running())

    def check_partial_clone(self, result):
        self.start('o/r#1')
        worker = self.worker()
        def partial(data, m):
            checkout = Path(m['checkout'])
            (checkout / '.git').mkdir(parents=True)
            if result:
                q.member_result(self.store.path, 1, m['attempt'], m['token'], dict(result=result))
            output = self.root / 'partial.log'
            output.write_text('clone interrupted')
            class Exited:
                pid = 123
                def poll(self):
                    return 1
            return dict(process=Exited(), stream=io.BytesIO(), path=output, started=self.now,
                        attempt=m['attempt'], token=m['token'])
        worker.spawn = partial
        worker.tick(); worker.tick()
        self.assertEqual('failed', self.member()['state'])
        self.assertFalse(self.member()['checkoutEstablished'])
        checkout = Path(self.member()['checkout'])
        self.assertTrue(checkout.resolve().is_relative_to(self.root))
        shutil.rmtree(checkout)
        self.start('o/r#1', retry=True)
        worker.spawn = self.spawn
        worker.tick(); worker.tick()
        self.assertEqual('active', self.member()['state'])
        self.assertTrue(self.member()['checkoutEstablished'])

    def test_failed_clone_is_not_established_and_deletion_does_not_block_retry(self):
        self.check_partial_clone('failed')

    def test_killed_clone_is_not_established_and_deletion_does_not_block_retry(self):
        self.check_partial_clone(None)

    def test_watch_survives_failed_and_empty_scans_then_admits_new_issue(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [], 'work')):
            self.start('label:work', watch=True)
        worker = self.worker()
        responses = [OSError('offline'), [], [[{'number': 8, 'created_at': 'a'}]]]
        def gh(*args):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        worker.gh = gh
        for _ in range(3):
            worker.tick()
            self.assertFalse(q.finished(self.store.load()))
            self.now += 300
        self.assertEqual([8], [x[0] for x in self.launches])

    def test_watched_label_is_stable_but_other_label_snapshots_can_append(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [1], 'work')):
            self.start('label:work', watch=True)
        with patch.object(q, 'resolve_spec', return_value=('o/r', [2], 'later')):
            self.start('label:later')
            with self.assertRaises(q.UsageError):
                self.start('label:later', watch=True)
        self.assertEqual('work', self.store.load()['label'])
        self.assertEqual([1, 2], [m['number'] for m in self.store.load()['members']])

    def test_restart_reconciles_intent_and_completed_result_without_duplicate_launch(self):
        self.start('o/r#1')
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', attempt=1, token=str(uuid.uuid4()), startedAt=self.now)
        worker = self.worker()
        worker.tick()  # crash after intent, before child launch
        self.assertEqual([1], [x[0] for x in self.launches])
        original = q.read_json(Path(self.member()['checkout']) / '.workbench/state/claude.json')['sessionId']
        restarted = self.worker()
        restarted.tick()  # result checkpoint exists even though owner lost its job handle
        self.assertEqual('active', self.member()['state'])
        self.assertEqual([1], [x[0] for x in self.launches])
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', result=None, startedAt=self.now)
        restarted.tick()  # session exists but launch result was not checkpointed
        self.assertEqual([1, 1], [x[0] for x in self.launches])
        self.assertEqual(original, q.read_json(Path(self.member()['checkout']) / '.workbench/state/claude.json')['sessionId'])

    def test_launcher_exit_and_timeout_do_not_hold_the_queue(self):
        self.start('o/r#1,2')
        worker = self.worker()
        worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['result'] = None
        worker.tick()  # exit 0 without the required result is failed, next issue starts
        self.assertEqual('failed', self.member()['state'])
        self.assertEqual([1, 2], [x[0] for x in self.launches])
        with self.store.transaction() as data:
            data['members'][1]['result'] = None
        process = worker.jobs[2]['process']
        process.poll = lambda: None
        process.wait = lambda timeout: 0
        process.kill = lambda: None
        self.now += 601
        with patch.object(q.subprocess, 'run') as run, patch.object(q.shutil, 'which', return_value='gh'):
            worker.tick()
        self.assertEqual('failed', self.member(2)['state'])
        self.assertEqual('timeout', self.member(2)['launchResult'])
        self.assertTrue(q.finished(self.store.load()))
        if os.name == 'nt':
            self.assertEqual(['taskkill', '/PID', '123', '/T', '/F'], run.call_args.args[0])

    def test_pr_lookup_failure_retains_state_and_retries(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        self.report(1)
        worker.gh = lambda *a: (_ for _ in ()).throw(OSError('offline'))
        worker.tick()
        self.assertEqual('pr-open', self.member()['state'])
        worker.gh = self.gh
        self.pr_state = 'MERGED'
        self.now += 301
        worker.tick()
        self.assertEqual('merged', self.member()['state'])

    def test_gh_uses_a_deadline_and_never_modifies_a_pr(self):
        with patch.object(q.subprocess, 'Popen') as spawn, patch.object(q.shutil, 'which', return_value='gh'):
            spawn.return_value.returncode = 0
            spawn.return_value.communicate.return_value = (b'{"state":"OPEN"}', b'')
            self.assertEqual({'state': 'OPEN'}, q.gh_json('pr', 'view', 'url', '--json', 'state'))
        self.assertEqual(60, spawn.return_value.communicate.call_args.kwargs['timeout'])
        self.assertEqual(['gh', 'pr', 'view', 'url', '--json', 'state'], spawn.call_args.args[0])

    def test_gh_timeout_stops_its_child_tree(self):
        with patch.object(q.subprocess, 'Popen') as spawn, patch.object(q.subprocess, 'run') as run:
            spawn.return_value.pid = 123
            spawn.return_value.communicate.side_effect = [subprocess.TimeoutExpired('gh', 60), (b'', b'')]
            with self.assertRaises(subprocess.TimeoutExpired):
                q.run_gh(['issue', 'view', '1'])
        if os.name == 'nt':
            self.assertEqual(['taskkill', '/PID', '123', '/T', '/F'], run.call_args.args[0])

    def test_clone_proxy_has_no_short_deadline(self):
        with patch.object(q.subprocess, 'Popen') as spawn, patch.object(q.shutil, 'which', return_value='gh'), \
                patch.object(q.sys, 'stdout'), patch.object(q.sys, 'stderr'):
            spawn.return_value.returncode = 0
            spawn.return_value.communicate.return_value = (b'', b'')
            self.assertEqual(0, q.main(['gh-proxy', '--', 'repo', 'clone', 'o/r', 'checkout', '--', '--quiet']))
        self.assertIsNone(spawn.return_value.communicate.call_args.kwargs['timeout'])
        self.assertEqual(['gh', 'repo', 'clone', 'o/r', 'checkout', '--', '--quiet'], spawn.call_args.args[0])

    # #20: -Implementer on a queue is saved with it and passed to every member launch.

    def launched_args(self):
        data = self.store.load()
        m = dict(self.member(1), token=str(uuid.uuid4()))
        worker = q.Worker(self.store, data['owner']['token'], gh=self.gh, clock=lambda: self.now)
        with patch.object(q.subprocess, 'Popen') as popen:
            job = worker.spawn_launcher(data, m)
        job['stream'].close()
        return popen.call_args.args[0]

    def test_default_queue_launches_members_without_the_switch(self):
        self.start('o/r#1')
        self.assertNotIn('implementer', self.store.load())
        self.assertNotIn('-Implementer', self.launched_args())

    def test_claude_is_saved_and_passed_to_members(self):
        self.start('o/r#1', implementer='claude')
        self.assertEqual('claude', self.store.load()['implementer'])
        args = self.launched_args()
        self.assertEqual(['-Implementer', 'claude'], args[args.index('-Implementer'):args.index('-Implementer') + 2])

    def test_append_without_the_switch_keeps_the_saved_choice(self):
        self.start('o/r#1', implementer='claude')
        self.start('o/r#2')
        self.assertEqual('claude', self.store.load()['implementer'])

    def test_invalid_values_are_refused(self):
        with self.assertRaises(q.UsageError):
            self.start('o/r#1', implementer='aider')
        self.start('o/r#1')
        data = json.loads(self.store.path.read_text())
        data['implementer'] = 'aider'
        self.store.path.write_text(json.dumps(data))
        with self.assertRaises(q.StateError):
            self.store.load()


    # #23: -AutoMerge / -NoAutoMerge on a queue are saved (false included) and passed to members.
    def test_default_queue_passes_no_auto_merge_switch(self):
        self.start('o/r#1')
        self.assertNotIn('autoMerge', self.store.load())
        args = self.launched_args()
        self.assertNotIn('-AutoMerge', args)
        self.assertNotIn('-NoAutoMerge', args)

    def test_auto_merge_on_and_off_are_saved_and_passed(self):
        self.start('o/r#1', auto_merge=True)
        self.assertIs(True, self.store.load()['autoMerge'])
        self.assertIn('-AutoMerge', self.launched_args())
        self.start('o/r#2')
        self.assertIs(True, self.store.load()['autoMerge'])
        self.start('o/r#3', auto_merge=False)
        self.assertIs(False, self.store.load()['autoMerge'])
        args = self.launched_args()
        self.assertIn('-NoAutoMerge', args)
        self.assertNotIn('-AutoMerge', args)

    def test_invalid_saved_auto_merge_is_refused(self):
        self.start('o/r#1')
        data = json.loads(self.store.path.read_text())
        for bad in ('yes', 0, 1, 0.0):      # r16 i1: 0 == False and 1 == True, but they are not booleans
            with self.subTest(value=bad):
                data['autoMerge'] = bad
                self.store.path.write_text(json.dumps(data))
                with self.assertRaises(q.StateError):
                    self.store.load()

    def test_cli_auto_merge_flags_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            q.main(['start', '--spec', 'o/r#1', '--auto-merge', '--no-auto-merge'])

    # #27: -Autonomous / -NoAutonomous on a queue are saved (false included) and passed to members.
    def test_autonomy_is_saved_and_passed_to_members(self):
        self.start('o/r#1')
        self.assertNotIn('-Autonomous', self.launched_args())
        self.assertNotIn('-NoAutonomous', self.launched_args())
        self.start('o/r#2', autonomous=True)
        self.assertIs(True, self.store.load()['autonomous'])
        self.assertIn('-Autonomous', self.launched_args())
        self.start('o/r#3', autonomous=False)
        args = self.launched_args()
        self.assertIn('-NoAutonomous', args)
        self.assertNotIn('-Autonomous', args)

    def test_an_autonomous_queue_never_passes_no_auto_merge(self):
        self.start('o/r#1', autonomous=True, auto_merge=False)
        args = self.launched_args()
        self.assertIn('-Autonomous', args)
        self.assertNotIn('-NoAutoMerge', args)

    def test_invalid_saved_autonomy_is_refused(self):
        self.start('o/r#1')
        data = json.loads(self.store.path.read_text())
        for bad in ('yes', 1):
            with self.subTest(value=bad):
                data['autonomous'] = bad
                self.store.path.write_text(json.dumps(data))
                with self.assertRaises(q.StateError):
                    self.store.load()

def hold_exclusively(path):
    """Open a file the way the launcher's Invoke-WithCheckoutLock does: no sharing at all."""
    import ctypes
    from ctypes import wintypes
    create = ctypes.windll.kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0xC0000000, 0, None, 4, 0x80, None)     # GENERIC_READ|WRITE, share none, OPEN_ALWAYS
    if handle == wintypes.HANDLE(-1).value:
        raise OSError('could not hold the lock file')
    return handle


class QueueBugs(unittest.TestCase):
    """#28: -Queue bugs queues every open bug nobody is handling, appending to the repo's queue."""
    # QueueCase's fixture and helpers, without re-running its tests.
    terminal, start, gh, spawn, worker, member = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                  QueueCase.spawn, QueueCase.worker, QueueCase.member)

    def setUp(self):
        QueueCase.setUp(self)
        self.in_hand.side_effect = self.fake_in_hand
        self.prs = {}
        self.tree = {'workspaces': []}
        self.pulls = []
        self.closing = {}

    def fake_in_hand(self, repo, numbers, root, queue_path, gh=None, tree=None):
        # The tree is passed through (None -> the live agw.tree(), which each test patches).
        return self.real_in_hand(repo, numbers, root, queue_path, gh or self.fake_gh, tree)

    def fake_gh(self, *args):
        if args[:2] == ('repo', 'view'):
            return {'nameWithOwner': 'o/r'}
        if args[0] == 'api' and args[1].startswith('repos/o/r/issues?labels='):
            self.label_seen = args[1]
            return [[{'number': n, 'created_at': f'2026-09-{n:02d}'} for n in (1, 2, 3, 4, 5)]]
        if args[0] == 'api' and args[1].startswith('repos/o/r/pulls'):
            return [self.pulls]
        if args[:2] == ('api', 'graphql'):
            data = {f'i{n}': {'closedByPullRequestsReferences': {'nodes': nodes}} for n, nodes in self.closing.items()}
            return {'data': {'repository': data}}
        raise AssertionError(args)

    def start_bugs(self, spec='bugs', repo='o/r', **kwargs):
        with patch.object(q, 'gh_json', self.fake_gh), patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            return q.start_queue(spec, repo=repo, root=self.root / 'queues', gh=self.fake_gh, **kwargs)

    def members(self):
        return [m['number'] for m in self.store.load()['members']]

    def output(self):
        return sys.stdout.getvalue()

    def test_bugs_is_the_configured_label(self):
        self.start_bugs('BUGS')
        self.assertIn('labels=bug&', self.label_seen)
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'bugLabel': 'kind: bug'}))
        self.start_bugs()
        self.assertIn('labels=kind%3A%20bug&', self.label_seen)
        for bad in ('', 'a,b', 3):
            with self.subTest(label=bad):
                self.config.write_text(json.dumps({'bugLabel': bad}))
                with self.assertRaises(q.UsageError):
                    self.start_bugs()

    def test_each_in_hand_reason_is_skipped_and_reported(self):
        clones = self.root / 'clones'
        self.pulls = [{'number': 40, 'head': {'ref': 'issue-1-fix', 'repo': {'full_name': 'o/r'}}},
                      {'number': 41, 'head': {'ref': 'issue-5-fork', 'repo': {'full_name': 'someone/r'}}}]
        self.closing = {2: [{'number': 42, 'state': 'OPEN'}], 5: [{'number': 43, 'state': 'CLOSED'}]}
        self.tree = {'workspaces': [{'name': 'r', 'sessions': [{'id': 's3', 'name': '#3 fix the thing'},
                                                               {'id': 'h5', 'name': '#5 revmux r1'}]},
                                    {'name': 'other', 'sessions': [{'id': 'x', 'name': '#5 elsewhere'}]}]}
        foreign = clones / 'r-issue-4' / '.workbench' / 'state'
        foreign.mkdir(parents=True)
        self.start_bugs()
        self.assertEqual([5], self.members())            # a fork's PR, a closed PR, a helper, another repo: not in hand
        out = self.output()
        self.assertIn('#1 skipped: pr: open PR #40 on issue-1-fix', out)
        self.assertIn('#2 skipped: pr: open PR #42 will close it', out)
        self.assertIn('#3 skipped: session: a live workbench session is open for it', out)
        self.assertIn('#4 skipped: checkout exists from an earlier loop', out)

    @unittest.skipUnless(os.name == 'nt', 'the launcher lock is a Windows share-mode lock')
    def test_a_held_launch_lock_is_skipped_but_a_stale_lock_file_is_not(self):
        import ctypes
        lock = self.root / 'clones' / 'r-issue-2' / '.workbench' / 'state' / 'launch.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text('')
        membership = lock.parent / 'queue-member.json'
        q.atomic_json(membership, dict(queue=str(self.store.path), repo='o/r', number=2))
        handle = hold_exclusively(lock)
        try:
            reasons = q.skip_reasons([2], 'o/r', self.root / 'clones', self.store.path, {}, set())
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        self.assertEqual({2: 'checkout-lock: a launcher holds its checkout'}, reasons)
        self.assertEqual({}, q.skip_reasons([2], 'o/r', self.root / 'clones', self.store.path, {}, set()))

    def test_append_to_a_running_queue_skips_members_and_reports_settings(self):
        self.start('o/r#1,2')
        self.start_bugs(autonomous=True)
        self.assertEqual([1, 2, 3, 4, 5], self.members())
        out = self.output()
        self.assertIn('#1 skipped: queued (pending)', out)
        self.assertIn('settings: autonomous null -> true (for every member launched from now on)', out)

    def test_a_pr_closed_unmerged_makes_the_issue_eligible_on_the_next_rescan(self):
        self.pulls = [{'number': 40, 'head': {'ref': 'issue-1-fix', 'repo': {'full_name': 'o/r'}}}]
        self.start_bugs(watch=True)
        self.assertEqual([2, 3, 4, 5], self.members())
        worker = self.worker()
        worker.gh = self.fake_gh
        self.pulls = []                                  # the PR was closed without merging
        with patch.object(q.agw, 'tree', return_value=self.tree):
            worker.refresh_remote()
        self.assertEqual([2, 3, 4, 5, 1], self.members())

    def test_rescan_logs_a_skip_only_when_its_reason_changes(self):
        self.pulls = [{'number': 40, 'head': {'ref': 'issue-1-fix', 'repo': {'full_name': 'o/r'}}}]
        self.start_bugs(watch=True)
        worker = self.worker()
        worker.gh = self.fake_gh
        before = self.output().count('#1 skipped')
        with patch.object(q.agw, 'tree', return_value=self.tree):
            for _ in range(3):
                worker.refresh_remote()
                self.now += 300
        self.assertEqual(before + 1, self.output().count('#1 skipped'))

    def test_a_failed_lookup_fails_the_start_and_writes_nothing(self):
        def broken(*args):
            if args[0] == 'api' and args[1].startswith('repos/o/r/pulls'):
                raise q.QueueError('HTTP 502')
            return self.fake_gh(*args)
        with self.assertRaises(q.QueueError):
            with patch.object(q, 'gh_json', broken), patch.object(q.agw, 'tree', return_value=self.tree):
                q.start_queue('bugs', repo='o/r', root=self.root / 'queues', gh=broken)
        self.assertFalse(self.store.path.exists())

    def test_a_failed_lookup_on_a_rescan_adds_nothing(self):
        self.start_bugs(watch=True)
        worker = self.worker()
        worker.gh = lambda *args: (_ for _ in ()).throw(q.QueueError('HTTP 502')) if args[0] == 'api' and \
            args[1].startswith('repos/o/r/pulls') else self.fake_gh(*args)
        before = self.members()
        with self.store.transaction() as data:
            data['members'] = [m for m in data['members'] if m['number'] != 5]
        with patch.object(q.agw, 'tree', return_value=self.tree):
            worker.refresh_remote()
        self.assertEqual([n for n in before if n != 5], self.members())
        self.assertIn('label scan', worker.errors)

    def test_an_explicit_list_is_not_filtered(self):
        self.pulls = [{'number': 40, 'head': {'ref': 'issue-1-fix', 'repo': {'full_name': 'o/r'}}}]
        self.start_bugs('o/r#1,2')
        self.assertEqual([1, 2], self.members())
        self.in_hand.assert_not_called()

    def test_watch_onto_an_unwatched_queue_turns_watching_on(self):
        self.start('o/r#1')
        self.start_bugs(watch=True)
        data = self.store.load()
        self.assertEqual((True, 'bug'), (data['watch'], data['label']))
        self.assertIn('settings: watch false -> true', self.output())
        with self.assertRaises(q.UsageError):
            self.start_bugs('label:other', watch=True)

    def test_dry_run_prints_the_plan_and_writes_nothing(self):
        self.start('o/r#1')
        before = self.store.path.read_text()
        self.pulls = [{'number': 40, 'head': {'ref': 'issue-2-fix', 'repo': {'full_name': 'o/r'}}}]
        self.start_bugs(dry_run=True, autonomous=True)
        result = json.loads(self.output().strip().splitlines()[-1])
        self.assertEqual([3, 4, 5], result['members'])
        self.assertEqual([{'number': 1, 'reason': 'queued (pending)'},
                          {'number': 2, 'reason': 'pr: open PR #40 on issue-2-fix'}], result['skipped'])
        self.assertEqual({'autonomous': [None, True]}, result['settings'])
        self.assertEqual('append to a stopped queue', result['mode'])     # no worker holds the lock here
        self.assertEqual(before, self.store.path.read_text())

    def test_dry_run_of_a_new_queue_is_a_start_with_no_setting_changes(self):
        # r19
        self.start_bugs(dry_run=True, autonomous=True)
        result = json.loads(self.output().strip().splitlines()[-1])
        self.assertEqual(('start', {}, [1, 2, 3, 4, 5]), (result['mode'], result['settings'], result['members']))
        self.assertFalse(self.store.path.exists())

    def test_repo_and_workspace_names_match_in_any_case(self):
        # r19 M1: repo_name() lowercases; GitHub and the launcher's workspace keep the real case.
        self.pulls = [{'number': 40, 'head': {'ref': 'issue-1-fix', 'repo': {'full_name': 'O/R'}}}]
        self.tree = {'workspaces': [{'name': 'R', 'sessions': [{'id': 's2', 'name': '#2 fix'}]}]}
        self.start_bugs(repo='O/R')
        self.assertEqual([3, 4, 5], self.members())
        out = self.output()
        self.assertIn('#1 skipped: pr: open PR #40 on issue-1-fix', out)
        self.assertIn('#2 skipped: session: a live workbench session is open for it', out)

    def test_an_unreachable_terminal_on_a_rescan_adds_nothing_and_keeps_running(self):
        # r19: CtlError is a RuntimeError, which Worker.run would not catch.
        self.start_bugs(watch=True)
        worker = self.worker()
        worker.gh = self.fake_gh
        with self.store.transaction() as data:
            data['members'] = [m for m in data['members'] if m['number'] != 5]
        with patch.object(q.agw, 'tree', side_effect=q.agw.CtlError('agwinterm is not running')):
            worker.refresh_remote()
        self.assertEqual([1, 2, 3, 4], self.members())
        self.assertIn('label scan', worker.errors)

class Specs(unittest.TestCase):
    def test_lists_and_repositories(self):
        self.assertEqual(('o/r', [3, 4], None), q.resolve_spec('o/r#3,#4,3'))
        self.assertEqual(('o/r', [3, 4], None), q.resolve_spec('3,4', 'o/r'))
        self.assertEqual(('o/r', [3], None), q.resolve_spec('3', gh=lambda *a: {'nameWithOwner': 'o/r'}))
        for spec in ('', '0', '-1', 'o/r#1,x/y#2', '1,', 'label:'):
            with self.subTest(spec=spec), self.assertRaises(q.QueueError):
                q.resolve_spec(spec, 'o/r')

    def test_paginated_label_order_excludes_prs_and_encodes_label(self):
        calls = []
        def gh(*args):
            calls.append(args)
            return [[{'number': 3, 'created_at': 'b'}, {'number': 99, 'pull_request': {}, 'created_at': 'a'}],
                    [{'number': 2, 'created_at': 'a'}, {'number': 1, 'created_at': 'a'}]]
        self.assertEqual(('o/r', [1, 2, 3], 'needs work'), q.resolve_spec('label:needs work', 'o/r', gh))
        self.assertIn('labels=needs%20work', calls[0][1])
        self.assertEqual(('--paginate', '--slurp'), calls[0][-2:])
        with self.assertRaises(OSError):
            q.resolve_spec('label:work', 'o/r', lambda *a: (_ for _ in ()).throw(OSError('API unavailable')))

    def test_terminal_cli_fallback_preserves_creation_and_pin_arguments(self):
        args = q.agw._cli_args(dict(cmd='session.new', args={'name': '#queue o/r', 'command': "& 'python' 'a b'", 'no-select': True}))
        self.assertEqual(['session', 'new', '--name', '#queue o/r', '--command', "& 'python' 'a b'", '--no-select'], args)
        self.assertEqual(['session', 'restore', 'command', '--target', 'pane'],
                         q.agw._cli_args(dict(cmd='session.restore', target='pane', args={'command': 'command'})))

