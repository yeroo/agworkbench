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
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import conductor as q
import closer
import cleanup

REAL_FREE_BYTES = q.free_bytes          # QueueCase patches it; the real one is tested on its own


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
        # #41: the disk guard reads this; a test machine's real free space must not pause the queue.
        self.free = 500 * q.GIB
        self.enterContext(patch.object(q, 'free_bytes', lambda path: (self.free, 'X:')))
        self.cleanups = []
        self.enterContext(patch.object(cleanup, 'start_after_close',
                                       side_effect=lambda *args: self.cleanups.append(args) or (1, 'wmi')))
        self.now = 1000
        self.store = q.Store(self.root / 'queues/o/r.json')
        self.launches = []
        self.pr_state = 'OPEN'
        self.issues = []

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
        if args[:2] == ('issue', 'list'):          # the priority labels (#34); none unless a test sets them
            return self.issues
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
            if args[:2] == ('issue', 'list'):
                return []
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

class CloseBackstop(unittest.TestCase):
    """#33: the conductor closes a merged member's sessions only when its relay is gone; while the
    relay lives it only flags the member. One closer at a time, never on a timeout alone."""
    terminal, start, gh, spawn, worker, member = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                  QueueCase.spawn, QueueCase.worker, QueueCase.member)
    PLANNER = '11111111-1111-4111-8111-111111111111'
    IMPLEMENTER = '22222222-2222-4222-8222-222222222222'

    def setUp(self):
        QueueCase.setUp(self)
        self.start('o/r#7')
        with self.store.transaction() as data:
            m = data['members'][0]
            # Issue 7's PR is 42: relay.json and loop-done.json hold the PR number, not the issue's.
            m.update(state='merged', slotReleased=True, pr='https://github.com/o/r/pull/42', prState='MERGED')
            self.checkout = Path(m['checkout'])
        self.state = self.checkout / '.workbench' / 'state'
        self.state.mkdir(parents=True)
        for name, value in (('relay.json', {'close_pending': 42}), ('implementer.json', {'autonomous': True}),
                            ('loop-done.json', {'pr': 42}),
                            ('agents.json', {'agents': {'claude': {'pane': self.PLANNER, 'tool': 'claude'},
                                                        'codex': {'pane': self.IMPLEMENTER, 'tool': 'claude'}}})):
            q.atomic_json(self.state / name, value)
        sys.path.insert(0, str(ROOT / 'tests'))
        from frames import CLAUDE_IDLE
        self.text = {self.PLANNER: CLAUDE_IDLE, self.IMPLEMENTER: CLAUDE_IDLE}
        self.tree = {'workspaces': [{'name': 'r', 'sessions': [
            {'id': self.PLANNER, 'name': '#7 fix', 'paneIds': [self.PLANNER, self.IMPLEMENTER]},
            {'id': 'relay-7', 'name': '#7 relay'}]}]}
        self.actions = []
        self.enterContext(patch.object(q.agw, 'tree', side_effect=lambda: self.tree))
        self.enterContext(patch.object(q.agw, 'pane_text', side_effect=lambda pane: self.text[pane]))
        self.enterContext(patch.object(q.agw, 'close_session', side_effect=lambda sid: self.actions.append(('close', sid))))
        self.enterContext(patch.object(q.agw, 'clear_restore', side_effect=lambda pane: self.actions.append(('unpin', pane))))
        self.w = self.worker()
        self.w.notify = Mock()

    def run_for(self, seconds, step=10):
        end = self.now + seconds
        while self.now < end:
            self.w.close_backstop()
            self.now += step

    def relay_gone(self):
        self.tree['workspaces'][0]['sessions'] = self.tree['workspaces'][0]['sessions'][:1]

    def test_nothing_happens_for_fifteen_minutes(self):
        self.relay_gone()
        self.run_for(890)
        self.assertEqual([], self.actions)
        self.assertTrue(self.member(7)['closePending'])
        self.assertFalse(q.finished(self.store.load()))          # the conductor stays up for it

    def test_a_live_relay_is_only_flagged(self):
        self.run_for(1200)
        self.assertEqual([], self.actions)
        self.assertIn('relay is alive', self.member(7)['closeStuck'])
        self.assertTrue(q.finished(self.store.load()))           # flagged: the human's, not a reason to stay up

    def test_a_gone_relay_gets_the_close(self):
        self.relay_gone()
        self.run_for(1000)
        self.assertEqual([('unpin', self.PLANNER), ('unpin', self.IMPLEMENTER), ('close', self.PLANNER)], self.actions)
        # #41: and then the checkout cleanup, for the PR (not the issue) number.
        self.assertEqual([(self.checkout.parent / self.checkout.name, 'o/r', '7', 42, 'merged')],
                         [(Path(a[0]), *a[1:]) for a in self.cleanups])
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))
        self.assertNotIn('closePending', self.member(7))
        self.assertIn('the conductor runs the close', (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_the_backstop_cleanup_follows_the_recorded_mode(self):
        for value, expected in (('build', 'build'), ('off', None)):
            with self.subTest(value=value):
                self.setUp()
                q.atomic_json(self.state / 'implementer.json', {'autonomous': True, 'cleanup': value})
                self.relay_gone()
                self.run_for(1000)
                self.assertEqual([] if expected is None else [expected], [a[4] for a in self.cleanups])

    def test_no_backstop_cleanup_without_the_close(self):
        self.relay_gone()
        box = self.checkout / '.workbench' / 'inbox' / 'claude'
        box.mkdir(parents=True)
        (box / 'h1.md').write_text('---\nid: h1\nfrom: human\nto: claude\nsubject: wait\n---\nx\n', encoding='utf-8')
        self.run_for(900 + closer.CLOSE_WAIT + 60)
        self.assertEqual([], self.cleanups)                     # refused on the timeout
        self.setUp()
        q.atomic_json(self.state / 'implementer.json', {'autonomous': False})
        self.relay_gone()
        self.run_for(1000)
        self.assertEqual([], self.cleanups)                     # autonomy off: nothing closed, nothing deleted

    def test_the_relay_finishing_its_own_close_clears_the_flags(self):
        self.run_for(1000)
        self.assertIn('closeStuck', self.member(7))
        q.atomic_json(self.state / 'relay.json', {})
        self.w.close_backstop()
        self.assertNotIn('closeStuck', self.member(7))
        self.assertNotIn('closePending', self.member(7))

    def test_a_close_that_cannot_complete_is_refused_and_flagged_never_forced(self):
        self.relay_gone()
        box = self.checkout / '.workbench' / 'inbox' / 'claude'
        box.mkdir(parents=True)
        (box / 'h1.md').write_text('---\nid: h1\nfrom: human\nto: claude\nsubject: wait\n---\nx\n', encoding='utf-8')
        self.run_for(900 + closer.CLOSE_WAIT + 60)
        self.assertEqual([], [a for a in self.actions if a[0] == 'close'])
        self.assertIn('the backstop close timed out', self.member(7)['closeStuck'])
        self.assertIn('the planner has unread mail h1', self.w.notify.call_args.args[0])
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))

    def test_close_pending_is_matched_against_the_pr_number_not_the_issue(self):
        self.relay_gone()
        q.atomic_json(self.state / 'relay.json', {'close_pending': 7})       # the issue's number: not this PR
        self.run_for(1000)
        self.assertEqual([], self.actions)
        self.assertNotIn('closePending', self.member(7))
        q.atomic_json(self.state / 'loop-done.json', {'pr': 7})              # the planner's done for another PR
        q.atomic_json(self.state / 'relay.json', {'close_pending': 42})
        self.run_for(900 + closer.CLOSE_WAIT + 60)
        self.assertEqual([], [a for a in self.actions if a[0] == 'close'])
        self.assertIn('loop-state done --pr 42', self.member(7)['closeStuck'])

    def test_a_relay_that_comes_back_takes_the_close_back(self):
        self.relay_gone()
        self.run_for(910)                                        # the attempt has started: panes settling
        self.assertIsNotNone(self.w.closes[7]['attempt'])
        self.tree['workspaces'][0]['sessions'].append({'id': 'relay-7', 'name': '#7 relay'})
        self.run_for(200)
        self.assertEqual([], self.actions)
        self.assertIsNone(self.w.closes[7]['attempt'])
        self.assertIn('relay is alive', self.member(7)['closeStuck'])
        self.assertIn('the relay is back', (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_end_close_leaves_a_close_pending_it_did_not_run(self):
        q.atomic_json(self.state / 'relay.json', {'close_pending': 43, 'other': 1})
        self.w.end_close(self.member(7), 42, self.checkout / '.workbench', stuck=None)
        self.assertEqual({'close_pending': 43, 'other': 1}, q.read_json(self.state / 'relay.json'))
        self.w.end_close(self.member(7), 43, self.checkout / '.workbench', stuck=None)
        self.assertEqual({'other': 1}, q.read_json(self.state / 'relay.json'))

    def test_unread_implementer_mail_alone_is_overridden_after_the_wait(self):
        # #44: the backstop applies the same rule as the relay.
        self.relay_gone()
        box = self.checkout / '.workbench' / 'inbox' / 'codex'
        box.mkdir(parents=True)
        (box / 'm1.md').write_text('---\nid: m1\nfrom: claude\nto: codex\nsubject: x\n---\nx\n', encoding='utf-8')
        self.run_for(900 + closer.CLOSE_WAIT - 60)
        self.assertEqual([], self.actions)                       # it blocks during the wait
        self.run_for(200)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertEqual(1, len(self.cleanups))
        self.assertNotIn('closeStuck', self.member(7))
        self.assertIn('despite unread implementer mail: m1', (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_a_relay_that_gave_up_hands_the_close_to_the_backstop(self):
        # #44 AC5, end to end: a real relay refuses (no loop-done record yet) in queue mode and hands
        # over; the conductor, reading that same relay.json, closes the issue session and cleans up.
        import hub
        import relay
        q.atomic_json(self.state / 'queue-member.json', {'queue': str(self.store.path), 'repo': 'o/r', 'number': 7})
        (self.state / 'loop-done.json').unlink()
        clock = {'t': 0.0}
        peers = [relay.Peer('claude', 'claude', self.PLANNER), relay.Peer('codex', 'claude', self.IMPLEMENTER)]
        with patch.dict(os.environ), patch.object(relay, 'now', lambda: clock['t']), \
                patch.object(relay, 'pause', lambda s: clock.update(t=clock['t'] + max(s, 1))), \
                patch.object(q.agw, 'my_pane', return_value='relay-7'):
            r = relay.Relay(self.checkout / '.workbench', peers, 'o/r', 'issue-7-fix', 5, 60)
            r.log = lambda text: None
            r.close_after_merge(42)
        hub.reload_paths()
        self.assertEqual([('unpin', 'relay-7'), ('close', 'relay-7')], self.actions)     # only its own session
        saved = q.read_json(self.state / 'relay.json')
        self.assertEqual(42, saved['close_pending'])
        self.assertEqual(42, saved['close_handoff']['pr'])
        self.actions.clear()
        self.relay_gone()                                        # its session closed
        q.atomic_json(self.state / 'loop-done.json', {'pr': 42})  # what held it up is resolved
        self.run_for(1000)
        self.assertEqual([('unpin', self.PLANNER), ('unpin', self.IMPLEMENTER), ('close', self.PLANNER)], self.actions)
        self.assertEqual([42], [a[3] for a in self.cleanups])
        saved = q.read_json(self.state / 'relay.json')
        for key in ('close_pending', 'close_merged_at', 'close_handoff'):
            self.assertNotIn(key, saved)
        self.assertNotIn('closePending', self.member(7))

    def test_an_unreadable_tree_closes_nothing(self):
        self.w.close_backstop()                                  # starts the 15-minute clock
        self.now += 1000
        with patch.object(q.agw, 'tree', side_effect=q.agw.CtlError('no pipe')):
            self.w.close_backstop()
        self.assertEqual([], self.actions)
        self.assertIn('close #7', self.w.errors)

class DiskGuard(unittest.TestCase):
    """#41: below minFreeGB on the checkout drive the conductor admits nothing and fails nothing;
    it resumes by itself when space returns."""
    terminal, start, gh, spawn, worker, member = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                  QueueCase.spawn, QueueCase.worker, QueueCase.member)

    def setUp(self):
        QueueCase.setUp(self)
        self.start('o/r#1,2,3', parallel=2)
        self.w = self.worker()
        self.w.notify = Mock()
        self.w.status = Mock()

    def states(self):
        return [m['state'] for m in self.store.load()['members']]

    def test_low_disk_pauses_admission_and_resumes(self):
        self.free = 5 * q.GIB
        for _ in range(3):
            self.w.tick()
        self.assertEqual([], self.launches)
        self.assertEqual(['pending'] * 3, self.states())
        paused = self.store.load()['diskPaused']
        self.assertEqual('low disk: 5.0 GB free < 20 GB on X:', paused)
        self.free = 4 * q.GIB                                                    # a shrinking disk is not news
        self.w.tick()
        self.assertEqual('low disk: 4.0 GB free < 20 GB on X:', self.store.load()['diskPaused'])
        self.w.notify.assert_called_once_with('queue paused: ' + paused)          # once per change, not per tick
        self.w.status.assert_called_once_with('blocked')
        self.assertFalse(q.finished(self.store.load()))
        self.free = 25 * q.GIB
        self.w.tick()
        self.assertEqual([1, 2], [n for n, _, _ in self.launches])
        self.assertNotIn('diskPaused', self.store.load())
        self.assertEqual('queue resumed: disk space is back', self.w.notify.call_args.args[0])
        self.w.status.assert_called_with('active')

    def test_an_orphaned_launch_is_not_respawned_while_paused(self):
        # r1 m1: a launching member whose launcher died is re-spawned only when there is space.
        with self.store.transaction() as data:
            m = data['members'][0]
            m.update(state='launching', attempt=1, token=str(uuid.uuid4()), result=None, startedAt=self.now,
                     slotReleased=False)
        self.free = 5 * q.GIB
        self.w.tick()
        self.assertEqual([], self.launches)
        self.assertEqual('launching', self.member(1)['state'])
        self.free = 25 * q.GIB
        self.w.tick()
        self.assertIn(1, [n for n, _, _ in self.launches])

    def test_low_disk_never_times_out_an_orphaned_launch(self):
        # r2 m1: the 600 s window of an orphaned launch restarts while paused, so it is re-spawned after.
        with self.store.transaction() as data:
            m = data['members'][0]
            m.update(state='launching', attempt=1, token=str(uuid.uuid4()), result=None, startedAt=self.now - 700,
                     slotReleased=False)
        self.free = 5 * q.GIB
        self.w.tick()
        self.assertEqual('launching', self.member(1)['state'])
        self.assertEqual([], self.launches)
        self.now += 700                                   # a long pause
        self.w.tick()
        self.assertEqual('launching', self.member(1)['state'])
        self.free = 25 * q.GIB
        with patch.object(q, 'checkout_locked', return_value=True):
            self.w.tick()                                 # a predecessor still holds it: left waiting
        self.assertEqual('launching', self.member(1)['state'])
        self.assertNotIn(1, [n for n, _, _ in self.launches])
        self.w.tick()
        self.assertIn(1, [n for n, _, _ in self.launches])
        self.assertNotEqual('failed', self.member(1)['state'])

    def test_a_restarted_conductor_announces_the_pause_it_finds(self):
        # r1 m2: run() sets the status active; the first tick of a new worker must say it is paused.
        self.free = 5 * q.GIB
        self.w.tick()
        fresh = self.worker()
        fresh.notify, fresh.status = Mock(), Mock()
        fresh.tick()
        fresh.notify.assert_called_once_with('queue paused: low disk: 5.0 GB free < 20 GB on X:')
        fresh.status.assert_called_once_with('blocked')
        fresh.tick()
        fresh.notify.assert_called_once()

    def test_the_threshold_comes_from_the_queue_config(self):
        self.free = 5 * q.GIB
        for value, admitted in ((0, True), (4.5, True), (6, False)):
            with self.subTest(value=value):
                self.launches.clear()
                with self.store.transaction() as data:
                    for m in data['members']:
                        m.update(state='pending', attempt=0, slotReleased=False)
                self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'minFreeGB': value}))
                self.w.tick()
                self.assertEqual(admitted, bool(self.launches))

    def test_an_invalid_threshold_pauses_rather_than_admits(self):
        for value in (-1, 'x', True):
            with self.subTest(value=value):
                self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'minFreeGB': value}))
                self.w.tick()
                self.assertEqual([], self.launches)
                self.assertIn('minFreeGB', self.store.load()['diskPaused'])
                self.assertEqual(['pending'] * 3, self.states())

    def test_diskpaused_must_be_a_string(self):
        with self.store.transaction() as data:
            data['diskPaused'] = 5
        with self.assertRaises(q.StateError):
            self.store.load()

    def test_free_bytes_reads_the_nearest_existing_ancestor(self):
        free, drive = REAL_FREE_BYTES(self.root / 'clones' / 'not' / 'yet')     # the root does not exist yet
        self.assertEqual(shutil.disk_usage(self.root).free // q.GIB, free // q.GIB)
        self.assertEqual(Path(self.root).anchor, drive)


class PriorityOrder(unittest.TestCase):
    """#34: pending members are admitted P0, P1, untriaged, P2, P3, oldest issue first; with -Triage
    an untriaged member is triaged (in the background, one at a time) before it may be admitted."""
    terminal, start, gh, spawn, worker, member, report = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                          QueueCase.spawn, QueueCase.worker, QueueCase.member,
                                                          QueueCase.report)

    def setUp(self):
        QueueCase.setUp(self)
        self.triage_runs = []
        self.exit_code = {}                 # number -> the triage run's exit code (None: still running)
        self.outcome = {}                   # number -> the priority the run wrote

    def label(self, number, created, priority=None):
        labels = [{'name': f'priority:{priority}'}] if priority else [{'name': 'bug'}]
        self.issues.append({'number': number, 'createdAt': created, 'labels': labels})

    def spawn_triage(self, data, m):
        number = m['number']
        self.triage_runs.append(number)
        result = self.root / f'triage-{number}.json'
        log = self.root / f'triage-{number}.log'
        log.write_text('triage output\n')
        case = self

        class Run:
            pid = 456
            killed = False

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return None

            def poll(self):
                code = case.exit_code.get(number)
                if code is not None and number in case.outcome:
                    result.write_text(json.dumps({str(number): {'priority': case.outcome[number], 'written': True}}))
                return code
        return dict(process=Run(), stream=io.BytesIO(), path=log, result=result, number=number, started=self.now)

    def admit_all(self, worker, count):
        for _ in range(count):
            worker.tick()
            self.now += 20
            launched = self.launches[-1][0]
            self.report(launched)
            worker.tick()
        return [x[0] for x in self.launches]

    def test_admission_follows_priority_then_age(self):
        self.start('o/r#1,2,3,4,5,6')
        self.label(1, '2026-01-01', 'P3')
        self.label(2, '2026-01-02')                 # untriaged: after P1, before P2
        self.label(3, '2026-03-01', 'P0')
        self.label(4, '2026-01-04', 'P1')
        self.label(5, '2026-02-01', 'P0')           # the older P0 goes first
        self.label(6, '2026-01-06', 'P2')
        order = self.admit_all(self.worker(), 6)
        self.assertEqual([5, 3, 4, 2, 6, 1], order)
        self.assertEqual('P0', self.member(5)['priority'])
        self.assertEqual('2026-02-01', self.member(5)['createdAt'])

    def test_active_members_are_left_alone(self):
        self.start('o/r#1,2')
        worker = self.worker()
        worker.tick(); worker.tick()                # 1 admitted before any label was read
        self.assertEqual('active', self.member(1)['state'])
        self.label(1, '2026-01-01', 'P3')
        self.label(2, '2026-01-02', 'P0')
        worker.next_labels = 0
        worker.tick()
        self.assertEqual('active', self.member(1)['state'])
        self.assertNotIn('priority', self.member(1))            # only pending members are re-read
        self.assertEqual('pending', self.member(2)['state'])     # parallel 1: it waits its turn
        self.assertEqual('P0', self.member(2)['priority'])

    def test_an_old_queue_file_loads_and_a_bad_priority_is_refused(self):
        self.start('o/r#1')
        data = q.read_json(self.store.path)
        self.assertNotIn('triage', data)
        self.assertNotIn('priority', data['members'][0])
        data['members'][0]['priority'] = 'P9'
        q.atomic_json(self.store.path, data)
        with self.assertRaises(q.StateError):
            self.store.load()

    def test_triage_saves_the_setting(self):
        self.start('o/r#1')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.start('o/r#1', triage_on=True)
        self.assertIs(True, self.store.load()['triage'])
        self.assertIn('settings: triage null -> true', out.getvalue())

    def triage_worker(self):
        self.start('o/r#1,2', triage_on=True)
        self.label(1, '2026-01-01')                 # untriaged, the oldest
        self.label(2, '2026-01-02', 'P2')
        return q.Worker(self.store, self.store.load()['owner']['token'], gh=self.gh, clock=lambda: self.now,
                        spawn=self.spawn, spawn_triage=self.spawn_triage)

    def test_an_untriaged_member_is_triaged_before_it_is_admitted(self):
        worker = self.triage_worker()
        self.exit_code[1] = None
        for _ in range(3):
            worker.tick()
            self.now += 20
        self.assertEqual([1], self.triage_runs)     # one run, in the background
        self.assertEqual([], self.launches)         # nothing jumps the member being triaged
        self.exit_code[1], self.outcome[1] = 0, 'P3'
        order = self.admit_all(worker, 2)
        self.assertEqual([2, 1], order)             # triaged P3: after the P2
        self.assertEqual(('P3', 'ok'), (self.member(1)['priority'], self.member(1)['triageResult']))

    def test_a_failed_triage_admits_the_member_untriaged(self):
        worker = self.triage_worker()
        self.exit_code[1] = 1
        worker.tick()                               # starts the run; it is polled from the next tick
        order = self.admit_all(worker, 2)
        self.assertEqual([1, 2], order)             # untriaged ranks before P2
        self.assertTrue(self.member(1)['triageResult'].startswith('failed: exited 1'))

    def test_a_usage_limit_pauses_triage(self):
        worker = self.triage_worker()
        self.label(3, '2026-01-03')
        with self.store.transaction() as data:
            data['members'].append(q.new_member(3, 'o/r', self.root / 'clones'))
        self.exit_code[1] = 3
        worker.tick()
        self.now += 20
        worker.tick()
        self.assertEqual([1], self.triage_runs)     # #3 is not triaged while paused
        self.assertEqual([1], [x[0] for x in self.launches])
        self.report(1)
        worker.tick()
        self.assertEqual([1, 3], [x[0] for x in self.launches])     # ...and does not hold the queue
        with self.store.transaction() as data:     # put #3 back to see triage resume after the pause
            m = q.find_member(data, 3)
            m.update(state='pending', attempt=0, slotReleased=False)
            m.pop('token'); m.pop('result', None); m.pop('startedAt', None)
        worker.jobs.clear()
        self.now += q.TRIAGE_PAUSE
        self.exit_code[3] = None
        worker.tick()
        self.assertEqual([1, 3], self.triage_runs)

    def test_a_triage_run_that_hangs_is_killed_and_fails(self):
        worker = self.triage_worker()
        self.exit_code[1] = None
        worker.tick()
        self.now += q.TRIAGE_JOB_TIMEOUT
        job = worker.triage_job
        with patch.object(q.subprocess, 'run') as kill:
            kill.return_value = subprocess.CompletedProcess([], 0)
            worker.tick()
        self.assertEqual('failed: timed out', self.member(1)['triageResult'])
        if os.name == 'nt':                          # r21: the whole tree, claude -p included
            self.assertEqual(['taskkill', '/PID', '456', '/T', '/F'], kill.call_args.args[0])
        else:
            self.assertTrue(job['process'].killed)

    def test_a_partial_write_keeps_the_priority_it_wrote(self):
        # r21 m1: the label was written but the comment failed (exit 1): the member ranks as labelled.
        worker = self.triage_worker()
        self.exit_code[1], self.outcome[1] = 1, 'P0'
        worker.tick()
        worker.tick()
        self.assertEqual('P0', self.member(1)['priority'])
        self.assertTrue(self.member(1)['triageResult'].startswith('failed'))
        self.assertEqual([1], [x[0] for x in self.launches])


def listed(number, created, *labels, pr=False):
    issue = {'number': number, 'created_at': created, 'labels': [{'name': name} for name in labels]}
    if pr:
        issue['pull_request'] = {}
    return issue


class LabelQuery(unittest.TestCase):
    """#38: `-Queue 'where: <query>'` selects open issues by a boolean label query; the queue
    machinery is unchanged, and a watched query is stored and compared in normalised form."""
    terminal, start, member = QueueCase.terminal, QueueCase.start, QueueCase.member

    def setUp(self):
        QueueCase.setUp(self)
        self.calls = []
        self.pages = [[listed(5, '2026-01-05', 'bug', 'priority:P1'), listed(3, '2026-01-03', 'Bug', 'priority:P0'),
                       listed(9, '2026-01-09', 'bug', 'priority:P0', pr=True)],
                      [listed(4, '2026-01-04', 'bug', 'priority:P3'), listed(2, '2026-01-02', 'bug', 'wontfix', 'priority:P0'),
                       listed(1, '2026-01-06', 'bug')]]

    def fake_gh(self, *args):
        self.calls.append(args)
        if args[:2] == ('repo', 'view'):
            return {'nameWithOwner': 'o/r'}
        if args[0] == 'api' and args[1].startswith('repos/o/r/issues?state=open'):
            return self.pages
        if args[0] == 'api' and args[1].startswith('repos/o/r/issues?labels='):
            return [[]]
        raise AssertionError(args)

    def query(self, spec, **kwargs):
        return self.start(spec, gh=self.fake_gh, **kwargs)

    def test_a_query_selects_open_issues_oldest_first_prs_excluded(self):
        repo, numbers, query = q.resolve_spec('where: bug AND priority IN [P0, P1] AND NOT wontfix', 'o/r', self.fake_gh)
        self.assertEqual(('o/r', [3, 5]), (repo, numbers))                     # 9 is a PR, 2 is wontfix
        self.assertEqual('(bug AND priority IN [P0, P1] AND NOT wontfix)', query.text)
        self.assertEqual([('api', 'repos/o/r/issues?state=open&per_page=100', '--paginate', '--slurp')], self.calls)
        _, numbers, _ = q.resolve_spec('WHERE:bug AND priority NOT IN [P2, P3]', 'o/r', self.fake_gh)
        self.assertEqual([2, 3, 5, 1], numbers)                                # untriaged #1 included

    def test_a_malformed_query_is_refused_before_any_call(self):
        with self.assertRaises(q.UsageError) as caught:
            q.resolve_spec('where: bug AND', None, self.fake_gh)
        self.assertIn('column 8', str(caught.exception))
        self.assertEqual([], self.calls)
        with self.assertRaises(q.UsageError):
            self.query('where: priority IN []')
        self.assertFalse(self.store.path.exists())                             # nothing written

    def test_a_listing_failure_is_an_error_never_no_issues(self):
        def failing(*args):
            raise q.QueueError('HTTP 502')
        with self.assertRaises(q.QueueError):
            q.resolve_spec('where: bug', 'o/r', failing)

    def test_dry_run_shows_the_canonical_query_the_matches_and_the_members(self):
        self.in_hand.return_value = {3: 'pr: open PR #40 will close it'}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, self.query('where: bug and priority in [P0,P1] and not wontfix', repo='o/r', dry_run=True))
        report = json.loads(out.getvalue())
        self.assertEqual('(bug AND priority IN [P0, P1] AND NOT wontfix)', report['query'])
        self.assertEqual(2, report['matches'])
        self.assertEqual([5], report['members'])
        self.assertEqual([{'number': 3, 'reason': 'pr: open PR #40 will close it'}], report['skipped'])
        self.assertFalse(self.store.path.exists())

    def test_members_go_through_the_usual_skip_rules(self):
        self.in_hand.return_value = {5: 'session: a live workbench session is open for it'}
        self.query('where: bug AND priority IN [P0, P1]', repo='o/r')
        self.assertEqual([2, 3], [m['number'] for m in self.store.load()['members']])
        self.in_hand.assert_called_once()

    def test_a_watched_query_is_saved_rescanned_and_compared_by_its_normalised_form(self):
        self.query('where: bug AND priority IN [P0]', repo='o/r', watch=True)
        data = self.store.load()
        self.assertEqual((True, None, '(bug AND priority IN [P0])'), (data['watch'], data['label'], data['query']))
        self.assertEqual('where: (bug AND priority IN [P0])', q.watched_spec(data))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.query('where: Bug and (PRIORITY in ["p0"])', repo='o/r', watch=True)   # the same query
        self.assertNotIn('settings:', out.getvalue())
        self.assertEqual('(bug AND priority IN [P0])', self.store.load()['query'])     # as first written
        for other in ('where: bug AND priority IN [P1]', 'label:bug'):
            with self.subTest(other=other), self.assertRaises(q.UsageError):
                self.query(other, repo='o/r', watch=True)

    def test_a_label_watch_refuses_a_query(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [], 'work')):
            self.start('label:work', watch=True)
        with self.assertRaises(q.UsageError):
            self.query('where: work', repo='o/r', watch=True)

    def test_the_rescan_uses_the_saved_query(self):
        self.query('where: bug AND priority IN [P0]', repo='o/r', watch=True)
        worker = q.Worker(self.store, self.store.load()['owner']['token'], clock=lambda: self.now,
                          gh=lambda *args: [] if args[:2] == ('issue', 'list') else self.fake_gh(*args),
                          spawn=QueueCase.spawn.__get__(self))
        self.launches = []
        self.pages[1].append(listed(12, '2026-02-01', 'bug', 'priority:P0'))
        self.now += 400
        with patch.dict(os.environ, {'AGWORKBENCH_QUEUE_SPEC': 'where: nonsense ((('}):   # never read by the rescan
            worker.refresh_remote()
        self.assertIn(12, [m['number'] for m in self.store.load()['members']])

    def test_queue_files_old_and_invalid(self):
        self.start('o/r#1')
        data = q.read_json(self.store.path)
        self.assertNotIn('query', data)                                         # an old file, as it was
        for change in ({'watch': True, 'label': 'bug', 'query': '(bug)'}, {'watch': True, 'label': None, 'query': None},
                       {'watch': False, 'query': 7}, {'watch': True, 'label': None, 'query': ''}):
            with self.subTest(change=change):
                q.atomic_json(self.store.path, dict(data, **change))
                with self.assertRaises(q.StateError):
                    self.store.load()
        q.atomic_json(self.store.path, dict(data, watch=True, label=None, query='(bug)'))
        self.assertEqual('(bug)', self.store.load()['query'])

    def test_the_spec_can_come_from_the_environment(self):
        with patch.object(q, 'start_queue', return_value=0) as start:
            with patch.dict(os.environ, {'AGWORKBENCH_QUEUE_SPEC': 'where: NOT "needs design" & x'}):
                self.assertEqual(0, q.main(['start', '--spec-env', '--repo', 'o/r']))
            self.assertEqual('where: NOT "needs design" & x', start.call_args.args[0])
            with patch.dict(os.environ, {'AGWORKBENCH_QUEUE_SPEC': '  '}), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, q.main(['start', '--spec-env']))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            q.main(['start', '--spec', 'bugs', '--spec-env'])


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

