"""Queue decisions with fake GitHub/launchers/clock and real process-held file locks."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import conductor as q
import closer
import cleanup
import tslog
import wb

def tool_route(data):
    """(the tool a launch is routed to, pause reason): conductor.route_entry's entry, tool only."""
    entry, reason = q.route_entry(data)
    return (entry['tool'] if entry else None), reason


REAL_FREE_BYTES = q.free_bytes          # QueueCase patches it; the real one is tested on its own
REAL_FREE_RAM = q.free_ram


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
        # #109: a merged or closed member's outcome goes to the router's statistics: not to the real file, no gh.
        self.outcomes = self.enterContext(patch.object(q.router, 'record_outcome', return_value=None))
        # The in-hand lookups (#28) call gh and the terminal; queue mechanics tests assume none in hand.
        self.real_in_hand = q.in_hand
        self.in_hand = self.enterContext(patch.object(q, 'in_hand', return_value={}))
        # #41: the disk guard reads this; a test machine's real free space must not pause the queue.
        self.free = 500 * q.GIB
        self.enterContext(patch.object(q, 'free_bytes', lambda path: (self.free, 'X:')))
        # #61: and the memory guard this; the test machine's real free memory must not either.
        self.ram = 32 * q.GIB
        self.enterContext(patch.object(q, 'free_ram', lambda: self.ram))
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
        if command == 'tree':
            # #61: the ceiling reads the terminal; these members' sessions are gone unless a test says so.
            return {'workspaces': [{'name': 'r', 'sessions': [{'id': str(uuid.uuid4()), 'name': f'#{n} issue'}
                                                              for n in getattr(self, 'sessions', ())]}]}
        raise AssertionError(command)

    def start(self, spec='o/r#1,2,3', **kwargs):
        return q.start_queue(spec, root=self.root / 'queues', **kwargs)

    def gh(self, *args):
        if args[:2] == ('issue', 'list'):          # the priority labels (#34); none unless a test sets them
            return self.issues
        if args[:2] == ('pr', 'list'):
            return []
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

    def test_closed_report_releases_slot_and_is_counted_separately(self):
        self.start(parallel=1)
        worker = self.worker()
        worker.tick(); worker.tick()
        self.report(1, 'blocked', reason='checking duplicate')
        worker.tick()
        self.report(1, 'closed', reason='duplicate of #9')
        worker.tick()
        member = self.member(1)
        self.assertEqual(('closed', 'closed', 'duplicate of #9', True),
                         (member['state'], member['phase'], member['reason'], member['slotReleased']))
        self.assertIn('closed 1', q.summary(self.store.load()))
        self.assertIn('merged 0', q.summary(self.store.load()))
        self.assertIn(2, [item[0] for item in self.launches])
        self.assertTrue(q.finished(dict(self.store.load(), members=[member])))

    def test_closed_member_accepts_a_resumed_report_after_reopen(self):
        self.start('o/r#1')
        worker = self.worker()
        worker.tick(); worker.tick()
        self.report(1, 'closed', reason='duplicate')
        worker.tick()
        self.assertEqual('closed', self.member(1)['state'])
        with self.store.transaction() as data:
            data['members'][0].update(closePending=True, closeStuck=q.RELAY_ALIVE)
        worker.closes[1] = {'since': self.now, 'attempt': None}
        self.report(1, 'resumed')
        worker.tick()
        self.assertEqual('active', self.member(1)['state'])
        self.assertNotIn('closePending', self.member(1))
        self.assertNotIn('closeStuck', self.member(1))
        self.assertNotIn(1, worker.closes)
        # #61 M5: a resumed loop is live work again and holds a slot, so the queue is not finished.
        self.assertFalse(self.member(1)['slotReleased'])
        self.assertFalse(q.finished(self.store.load()))

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
        self.assertFalse(self.member(1)['slotReleased'])       # #61 M5: resumed work takes a slot again
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
        self.assertEqual('failed' if result else 'pending', self.member()['state'])
        self.assertFalse(self.member()['checkoutEstablished'])
        checkout = Path(self.member()['checkout'])
        self.assertTrue(checkout.resolve().is_relative_to(self.root))
        shutil.rmtree(checkout)
        if result:
            self.start('o/r#1', retry=True)
        else:
            self.now += 60  # missing launcher result automatically retries after infrastructure back-off
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

    def test_watched_label_can_change_and_other_snapshots_can_append(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [1], 'work')):
            self.start('label:work', watch=True)
        with patch.object(q, 'resolve_spec', return_value=('o/r', [2], 'later')):
            self.start('label:later')
            self.start('label:later', watch=True)
        self.assertEqual('later', self.store.load()['label'])
        self.assertEqual([1, 2], [m['number'] for m in self.store.load()['members']])

    def test_prune_removes_only_nonmatching_pending_and_clears_backoff(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [1, 2, 3], 'old')):
            self.start('label:old', watch=True)
        with self.store.transaction() as data:
            data['members'][2]['state'] = 'blocked'
            data['launchBackoff'] = dict(member=1, failures=2, until=9999, reason='launcher failed')
            data['launchPaused'] = 'launches failing: launcher failed'
        with patch.object(q, 'resolve_spec', return_value=('o/r', [2], 'new')):
            self.start('label:new', watch=True, prune=True)
        data = self.store.load()
        self.assertEqual([2, 3], [m['number'] for m in data['members']])
        self.assertNotIn('launchBackoff', data)
        self.assertNotIn('launchPaused', data)
        self.assertIn('#1 pruned: no longer matches the watched spec', sys.stdout.getvalue())
        worker = self.worker()
        worker.next_scan = self.now + 300
        worker.tick()
        self.assertEqual([2], [n for n, *_ in self.launches])

    def test_prune_dry_run_reports_changes_without_writing(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [1, 2], 'old')):
            self.start('label:old', watch=True)
        before = self.store.path.read_bytes()
        with patch.object(q, 'resolve_spec', return_value=('o/r', [2], 'new')):
            self.start('label:new', watch=True, prune=True, dry_run=True)
        result = json.loads(sys.stdout.getvalue().splitlines()[-1])
        self.assertEqual([1], result['pruned'])
        self.assertEqual(['old', 'new'], result['settings']['label'])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_prune_requires_a_watched_label_or_query(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [1], 'work')):
            with self.assertRaises(q.UsageError):
                self.start('label:work', prune=True)
        with self.assertRaises(q.UsageError):
            self.start('o/r#1', watch=True, prune=True)
        self.assertFalse(self.store.path.exists())

    def test_mark_ignores_a_pruned_member(self):
        self.start('o/r#1')
        with self.store.transaction() as data:
            data['members'].clear()
        self.worker().mark(1, triageResult='failed')
        self.assertEqual([], self.store.load()['members'])

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

    def test_launcher_exit_and_timeout_back_off_the_same_member(self):
        self.start('o/r#1,2')
        worker = self.worker()
        worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['result'] = None
        worker.tick()  # exit 0 without a result is infrastructure; the same member backs off
        self.assertEqual('pending', self.member()['state'])
        self.assertEqual([1], [x[0] for x in self.launches])
        self.assertEqual('launcher', self.member()['reason'].split(':')[1].strip())
        self.now += 60
        worker.tick()  # retry starts
        with self.store.transaction() as data:
            data['parallel'] = 2
        worker.tick()  # retry succeeds, then issue 2 may start
        self.assertEqual([1, 1, 2], [x[0] for x in self.launches])
        with self.store.transaction() as data:
            data['members'][1]['result'] = None
        process = worker.jobs[2]['process']
        process.poll = lambda: None
        process.wait = lambda timeout: 0
        process.kill = lambda: None
        self.now += 601
        with patch.object(q.subprocess, 'run') as run, patch.object(q.shutil, 'which', return_value='gh'):
            worker.tick()
        self.assertEqual('pending', self.member(2)['state'])
        self.assertEqual('timeout', self.member(2)['launchResult'])
        self.assertEqual(1, self.store.load()['launchBackoff']['failures'])
        self.assertFalse(q.finished(self.store.load()))
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

    def test_done_without_pr_open_is_adopted_and_merged(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        state = Path(self.member()['checkout']) / '.workbench/state'
        q.atomic_json(state / 'loop-done.json', {'pr': 457, 'at': self.now})
        self.pr_state = 'MERGED'
        worker.tick()
        self.assertEqual(('merged', 'MERGED', True),
                         (self.member()['state'], self.member()['prState'], self.member()['slotReleased']))
        self.assertEqual('https://github.com/o/r/pull/457', self.member()['pr'])

    def test_adopt_done_ignores_old_no_pr_and_malformed_records(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        state = Path(self.member()['checkout']) / '.workbench/state'
        for record in ({'pr': 2, 'at': self.now - 1}, {'pr': 2, 'at': True},
                       {'pr': True, 'at': self.now}, {'pr': 0, 'at': self.now},
                       {'pr': 2, 'at': float('nan')}, {'pr': 2, 'at': self.now, 'noPr': True},
                       {'pr': '2', 'at': self.now}, []):
            with self.subTest(record=record):
                q.atomic_json(state / 'loop-done.json', record)
                worker.tick()
                self.assertEqual(('active', None), (self.member()['state'], self.member()['pr']))
        with self.store.transaction() as data:
            data['members'][0].pop('startedAt')
        q.atomic_json(state / 'loop-done.json', {'pr': 2, 'at': self.now})
        worker.tick()
        self.assertEqual('active', self.member()['state'])

    def test_late_pr_open_report_cannot_demote_merged_member(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        self.report(1)
        worker.tick()
        with self.store.transaction() as data:
            data['members'][0].update(state='merged', prState='MERGED', slotReleased=True)
        report = self.report(1)
        with self.store.transaction() as data:
            self.assertTrue(q.apply_loop(data, data['members'][0], self.store.path))
        m = self.member()
        self.assertEqual(('merged', 'MERGED', True, report['rev']),
                         (m['state'], m['prState'], m['slotReleased'], m['consumedRev']))

    def test_done_report_after_merge_stays_merged_through_tick(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['members'][0].update(state='merged', pr='https://github.com/O/R/pull/5',
                                      prState='MERGED', slotReleased=True)
        checkout = Path(self.member()['checkout'])
        identity = q.read_json(checkout / '.workbench/state/claude.json')
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=identity['sessionId']):
            self.assertEqual(0, wb.loop_done(checkout, '5', 'sha'))
        worker.tick()
        self.assertEqual(('merged', 'MERGED', True),
                         (self.member()['state'], self.member()['prState'], self.member()['slotReleased']))
        with self.store.transaction() as data:
            self.assertTrue(q.apply_loop(data, data['members'][0], self.store.path))
        self.assertEqual('merged', self.member()['state'])

    def test_stale_active_merged_pr_wins_and_releases_slot(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        self.now += 1801
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}):
            worker.refresh_remote()
        self.assertEqual(self.now, self.member()['goneSince'])
        self.now += 1801
        worker.gh = Mock(side_effect=lambda *args: ([{'number': 457, 'state': 'MERGED', 'isCrossRepository': False}]
                              if args[:2] == ('pr', 'list') else AssertionError(args)))
        branch = Mock(returncode=0, stdout='issue-1-fix\n')
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=branch):
            worker.refresh_remote()
        self.assertEqual(('merged', 'MERGED', True),
                         (self.member()['state'], self.member()['prState'], self.member()['slotReleased']))
        self.assertEqual('https://github.com/o/r/pull/457', self.member()['pr'])
        self.assertFalse(any(c.args[:2] == ('issue', 'view') for c in worker.gh.call_args_list))

    def test_stale_active_checks_sessions_branch_and_closed_issue(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        self.now += 1801
        live = {'workspaces': [{'name': 'r', 'sessions': [{'name': '#1 helper'}]}]}
        with patch.object(q.agw, 'tree', return_value=live):
            worker.refresh_remote()
        self.assertNotIn('goneSince', self.member())
        self.now += 301
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}):
            worker.refresh_remote()
        self.assertIn('goneSince', self.member())
        self.now += 1801
        branch = Mock(returncode=1, stdout='')
        gh = Mock(side_effect=AssertionError('empty branch must not query GitHub'))
        worker.gh = gh
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=branch):
            worker.refresh_remote()
        self.assertEqual('active', self.member()['state'])
        self.assertIn('goneSince', self.member())
        gh.assert_not_called()
        worker.gh = Mock(return_value=[{'number': 5, 'state': 'MERGED'}])
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='issue-1-fix')):
            worker.next_pr = 0
            worker.refresh_remote()
        self.assertEqual('active', self.member()['state'])
        self.assertIn('goneSince', self.member())
        worker.gh = Mock(side_effect=lambda *args: [] if args[:2] == ('pr', 'list') else {'state': 'CLOSED'})
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='issue-1-fix')):
            worker.next_pr = 0
            worker.refresh_remote()
        self.assertEqual(('closed', True), (self.member()['state'], self.member()['slotReleased']))
        self.assertTrue(self.member()['reason'].startswith('stale:'))

    def test_stale_session_reappearing_clears_gone_since(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        self.now += 1801
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}):
            worker.refresh_remote()
        self.assertIn('goneSince', self.member())
        self.now += 301
        with patch.object(q.agw, 'tree', return_value={'workspaces': [
                {'name': 'r', 'sessions': [{'name': '#1 review'}]}]}):
            worker.refresh_remote()
        self.assertNotIn('goneSince', self.member())

    def test_stale_open_pr_keeps_active_even_if_issue_closed(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['goneSince'] = self.now - 1801
        self.now += 1801
        worker.gh = Mock(side_effect=lambda *args: [{'number': 12, 'state': 'OPEN', 'isCrossRepository': False}]
                         if args[:2] == ('pr', 'list') else {'state': 'CLOSED'})
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='issue-1-fix')):
            worker.refresh_remote()
        self.assertEqual(('active', None, False),
                         (self.member()['state'], self.member()['pr'], self.member()['slotReleased']))
        self.assertFalse(any(c.args[:2] == ('issue', 'view') for c in worker.gh.call_args_list))

    def test_stale_open_pr_wins_over_older_merged_pr(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['goneSince'] = self.now - 1801
        self.now += 1801
        prs = [{'number': 1, 'state': 'MERGED', 'isCrossRepository': False},
               {'number': 2, 'state': 'OPEN', 'isCrossRepository': False}]
        worker.gh = Mock(return_value=prs)
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='issue-1-fix')):
            worker.refresh_remote()
        self.assertEqual(('active', None, False),
                         (self.member()['state'], self.member()['pr'], self.member()['slotReleased']))

    def test_stale_ignores_fork_prs_on_same_branch(self):
        self.start('o/r#1,2')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['parallel'] = 2
        worker.tick(); worker.tick()
        with self.store.transaction() as data:
            for member in data['members']:
                member['goneSince'] = self.now - 1801
        self.now += 1801
        def gh(*args):
            if args[:2] == ('pr', 'list'):
                return [{'number': 12, 'state': 'OPEN', 'isCrossRepository': True}] if 'issue-1-fix' in args else [
                    {'number': 22, 'state': 'MERGED', 'isCrossRepository': True}]
            return {'state': 'CLOSED' if args[2] == '1' else 'OPEN'}
        worker.gh = Mock(side_effect=gh)
        def branch(args, **kwargs):
            return Mock(returncode=0, stdout='issue-1-fix' if 'r-issue-1' in args[2] else 'issue-2-fix')
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', side_effect=branch):
            worker.refresh_remote()
        self.assertEqual(('closed', None), (self.member(1)['state'], self.member(1)['pr']))
        self.assertEqual(('active', None), (self.member(2)['state'], self.member(2)['pr']))

    def test_stale_uses_relay_issue_branch_after_checkout_switches_to_main(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['goneSince'] = self.now - 1801
        state = Path(self.member()['checkout']) / '.workbench/state'
        q.atomic_json(state / 'relay.json', {'branch': 'issue-1-fix'})
        self.now += 1801
        worker.gh = Mock(return_value=[{'number': 5, 'state': 'MERGED', 'isCrossRepository': False}])
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='main')) as git:
            worker.refresh_remote()
        git.assert_not_called()
        self.assertEqual('merged', self.member()['state'])
        self.assertIn('issue-1-fix', worker.gh.call_args.args)

    def test_stale_refuses_non_issue_branch(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['goneSince'] = self.now - 1801
        self.now += 1801
        worker.gh = Mock(side_effect=AssertionError('wrong branch queried'))
        with patch.object(q.agw, 'tree', return_value={'workspaces': []}), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='main')):
            worker.refresh_remote()
        worker.gh.assert_not_called()
        self.assertEqual('active', self.member()['state'])
        self.assertIn('goneSince', self.member())

    def test_stale_rechecks_sessions_before_resolving(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        with self.store.transaction() as data:
            data['members'][0]['goneSince'] = self.now - 1801
        self.now += 1801
        worker.gh = Mock(return_value=[{'number': 5, 'state': 'MERGED', 'isCrossRepository': False}])
        empty = {'workspaces': []}
        live = {'workspaces': [{'name': 'r', 'sessions': [{'name': '#1 review'}]}]}
        with patch.object(q.agw, 'tree', side_effect=[empty, live]), \
                patch.object(q.subprocess, 'run', return_value=Mock(returncode=0, stdout='issue-1-fix')):
            worker.refresh_remote()
        self.assertEqual('active', self.member()['state'])
        self.assertNotIn('goneSince', self.member())

    def test_mark_cli_records_audit_and_refuses_invalid_targets(self):
        self.start('o/r#1,2')
        worker = self.worker(); worker.tick(); worker.tick()
        url = 'https://github.com/o/r/pull/457'
        with patch.dict(os.environ):
            os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
            self.assertEqual(0, q.main(['mark', '--file', str(self.store.path), '--number', '1',
                                        '--pr', url, '--reason', 'planner gone']))
        member = self.member()
        audit = member['operatorMark']
        self.assertEqual((url, 'planner gone', 'pr-open', True),
                         (audit['pr'], audit['reason'], member['state'], member['slotReleased']))
        line = json.loads((self.store.directory / 'operator.log').read_text().splitlines()[0])
        self.assertEqual(dict(number=1, **audit), line)
        for number, pr in [(1, 'https://github.com/other/repo/pull/1'), (99, url), (2, url)]:
            with self.subTest(number=number, pr=pr):
                self.assertEqual(2, q.main(['mark', '--file', str(self.store.path),
                                            '--number', str(number), '--pr', pr]))
        self.assertEqual(1, len((self.store.directory / 'operator.log').read_text().splitlines()))

    def test_failed_mark_commit_does_not_write_operator_log(self):
        self.start('o/r#1')
        worker = self.worker(); worker.tick(); worker.tick()
        original = q.atomic_json
        def fail_queue(path, data):
            if Path(path) == self.store.path:
                raise OSError('queue commit failed')
            return original(path, data)
        with patch.object(q, 'atomic_json', side_effect=fail_queue):
            self.assertEqual(1, q.main(['mark', '--file', str(self.store.path), '--number', '1',
                                        '--pr', 'https://github.com/o/r/pull/5']))
        self.assertFalse((self.store.directory / 'operator.log').exists())
        self.assertEqual('active', self.member()['state'])

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
        # The arguments are the subject, not the machine: a PowerShell is found whether or not one
        # is installed (macOS has none by default, #60).
        real_which = shutil.which
        fake_which = lambda name, *a, **k: '/usr/bin/pwsh' if name in ('pwsh', 'powershell.exe') else real_which(name, *a, **k)
        with patch.object(q.subprocess, 'Popen') as popen, patch.object(q.shutil, 'which', side_effect=fake_which):
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

    # #75: -BigReview / -NoBigReview on a queue are saved (false included) and passed to members.
    def test_big_review_is_saved_and_passed_to_members(self):
        self.start('o/r#1')
        self.assertNotIn('bigReview', self.store.load())
        self.assertNotIn('-BigReview', self.launched_args())
        self.assertNotIn('-NoBigReview', self.launched_args())
        self.start('o/r#2', big_review=True)
        self.assertIs(True, self.store.load()['bigReview'])
        self.assertIn('-BigReview', self.launched_args())
        self.start('o/r#3')
        self.assertIs(True, self.store.load()['bigReview'])
        self.start('o/r#4', big_review=False)
        self.assertIs(False, self.store.load()['bigReview'])
        args = self.launched_args()
        self.assertIn('-NoBigReview', args)
        self.assertNotIn('-BigReview', args)

    def test_invalid_saved_big_review_is_refused(self):
        self.start('o/r#1')
        data = json.loads(self.store.path.read_text())
        for bad in ('yes', 1):
            with self.subTest(value=bad):
                data['bigReview'] = bad
                self.store.path.write_text(json.dumps(data))
                with self.assertRaises(q.StateError):
                    self.store.load()

    def test_cli_big_review_flags_are_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            q.main(['start', '--spec', 'o/r#1', '--big-review', '--no-big-review'])

    # #77: -WaitOnLimit / -NoWaitOnLimit on a queue are saved ('failover' included) and passed to members.
    def test_on_limit_is_saved_and_passed_to_members(self):
        self.start('o/r#1')
        self.assertNotIn('onLimit', self.store.load())
        self.assertNotIn('-WaitOnLimit', self.launched_args())
        self.assertNotIn('-NoWaitOnLimit', self.launched_args())
        self.start('o/r#2', on_limit='wait')
        self.assertEqual('wait', self.store.load()['onLimit'])
        self.assertIn('-WaitOnLimit', self.launched_args())
        self.start('o/r#3')
        self.assertEqual('wait', self.store.load()['onLimit'])
        self.start('o/r#4', on_limit='failover')
        self.assertEqual('failover', self.store.load()['onLimit'])
        args = self.launched_args()
        self.assertIn('-NoWaitOnLimit', args)
        self.assertNotIn('-WaitOnLimit', args)

    def test_invalid_saved_on_limit_is_refused(self):
        self.start('o/r#1')
        data = json.loads(self.store.path.read_text())
        for bad in ('Wait', True, 1):
            with self.subTest(value=bad):
                data['onLimit'] = bad
                self.store.path.write_text(json.dumps(data))
                with self.assertRaises(q.StateError):
                    self.store.load()

    def test_cli_wait_on_limit_flags(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            q.main(['start', '--spec', 'o/r#1', '--wait-on-limit', '--no-wait-on-limit'])
        with patch.object(q, 'start_queue', return_value=0) as start:
            q.main(['start', '--spec', 'o/r#1', '--wait-on-limit'])
        self.assertEqual('wait', start.call_args.kwargs['on_limit'])

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

    def fake_in_hand(self, repo, numbers, root, queue_path, gh=None, tree=None, **named):
        # The tree is passed through (None -> the live agw.tree(), which each test patches).
        return self.real_in_hand(repo, numbers, root, queue_path, gh or self.fake_gh, tree, **named)

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
        self.start_bugs('label:other', watch=True)
        self.assertEqual('other', self.store.load()['label'])

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
        for name, value in (('relay.json', {'close_pending': 42, 'branch': 'issue-7'}), ('implementer.json', {'autonomous': True}),
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

    def finished_helper(self):
        pane = 'helper-pane'
        self.text[pane] = 'revmux exit 0'
        self.tree['workspaces'][0]['sessions'].append({'id': pane, 'name': '#7 revmux r1', 'paneIds': [pane]})
        directory = self.state / 'helpers'
        directory.mkdir(exist_ok=True)
        (directory / f'{pane}.done').write_text(json.dumps({
            'kind': 'revmux', 'round': 1, 'exit': 0, 'pane': pane, 'rows': ['revmux exit 0']}), encoding='utf-8')

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

    def named(self):
        # #66: the same member in a named queue, whose sessions live in r-kimi.
        data = self.store.load()
        data.update(name='kimi', workspace='r-kimi')
        kimi = q.Store(self.store.path.with_name('r.kimi.json'))
        q.atomic_json(kimi.path, data)
        self.store.path.unlink()
        self.store = kimi
        self.w = self.worker()
        self.w.notify = Mock()

    def test_a_named_queue_sees_its_relay_in_its_own_workspace(self):
        self.named()
        self.tree['workspaces'][0]['name'] = 'R-Kimi'
        self.run_for(1200)
        self.assertEqual([], self.actions)
        self.assertIn('relay is alive', self.member(7)['closeStuck'])

    def test_a_named_queue_ignores_a_same_named_relay_in_the_repo_workspace(self):
        self.named()
        self.run_for(1000)                       # '#7 relay' in 'r' is not this member's
        self.assertIn(('close', self.PLANNER), self.actions)

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

    def pointer_in(self, pane, box, mid, *, folder=''):
        """#96: a relay pointer left typed in `pane` for mail `mid` of `box`; keys go to self.keys."""
        import peerchat
        from frames import CLAUDE_IDLE, Clock, claude
        hub_dir = self.checkout / '.workbench'
        directory = hub_dir / 'inbox' / box / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{mid}.md'
        path.write_text(f'---\nid: {mid}\nfrom: codex\nto: {box}\nsubject: IMPLEMENTED abc\n---\nbody\n', encoding='utf-8')
        pointer = closer.relay_pointer({'id': mid, 'from': 'codex', 'subject': 'IMPLEMENTED abc'}, closer.AGMSG, hub_dir)
        self.text[pane] = claude(pointer)
        self.keys = []

        def type_into(target, key):
            self.keys.append((target, key))
            self.text[target] = CLAUDE_IDLE
            if key == '\n' and path.exists():
                path.rename(path.parent / 'read' / path.name)      # the woken planner reads it
        (directory / 'read').mkdir(exist_ok=True)
        clock = Clock()
        self.enterContext(patch.object(q.agw, 'type_into', side_effect=type_into))
        self.enterContext(patch.object(q.agw, 'cursor_column', side_effect=q.agw.CtlError('no cursor in fixture')))
        self.enterContext(patch.object(peerchat, 'now', clock.now))
        self.enterContext(patch.object(peerchat, 'pause', clock.pause))

    def test_the_backstop_clears_a_stale_pointer_for_read_mail(self):
        self.relay_gone()
        self.pointer_in(self.IMPLEMENTER, 'codex', 'm1', folder='read')
        self.run_for(1000)
        self.assertEqual([(self.IMPLEMENTER, '\x15')], self.keys)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertIn("cleared a stale relay pointer for m1 from codex's composer",
                      (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_the_backstop_clears_rather_than_rings_implementer_mail_it_ignores(self):
        # r1 m2: unread implementer mail the close does not wait for (here from a sender it does not
        # count) is cleared once settled, never submitted by the faster rescue.
        self.relay_gone()
        self.pointer_in(self.IMPLEMENTER, 'codex', 'n1')
        self.run_for(1000)
        self.assertEqual([(self.IMPLEMENTER, '\x15')], self.keys)
        self.assertIn(('close', self.PLANNER), self.actions)

    def test_the_backstop_submits_a_pointer_for_unread_planner_mail(self):
        self.relay_gone()
        self.pointer_in(self.PLANNER, 'claude', 'p1')
        self.run_for(1000)
        self.assertEqual([(self.PLANNER, '\n')], self.keys)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertIn('rescued unsent pointer in claude for p1 [submitted]',
                      (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_closed_no_pr_backstop_checks_issue_before_sessions(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        self.finished_helper()
        self.w.gh = lambda *args: {'state': 'OPEN'} if args[:2] == ('issue', 'view') else self.gh(*args)
        self.run_for(1000)
        self.assertEqual([], self.actions)
        self.assertIn('reopened', self.member(7)['closeStuck'])
        self.assertIn('reopened', self.w.notify.call_args.args[0])
        self.assertIn('NOT closing: issue #7 was reopened',
                      (self.state / 'relay-close.log').read_text(encoding='utf-8'))
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))
        self.assertFalse((self.state / 'loop-done.json').exists())
        self.assertTrue((self.state / 'loop-done-refused.json').exists())

    def test_busy_done_lock_does_not_skip_backstop_end_close(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 1})
        self.relay_gone()
        self.w.gh = lambda *args: {'state': 'OPEN'} if args[:2] == ('issue', 'view') else self.gh(*args)
        original_acquire = q.Lock.acquire
        def acquire(lock):
            if lock.path.name == 'loop-done.lock':
                raise q.QueueError('lock busy')
            return original_acquire(lock)
        with patch.object(q.Lock, 'acquire', acquire):
            self.run_for(1000)
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))
        self.assertNotIn('closePending', self.member(7))
        self.assertIn('could not retire refused no-PR completion',
                      (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_replacement_no_pr_record_is_adopted_during_backstop(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 1})
        self.relay_gone()
        issue_calls = [0]
        def gh(*args):
            if args[:2] == ('issue', 'view'):
                issue_calls[0] += 1
                if issue_calls[0] == 2:
                    q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 2})
                return {'state': 'CLOSED'}
            return self.gh(*args)
        self.w.gh = gh
        self.run_for(950)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertNotIn('closeStuck', self.member(7))

    def test_replacement_at_deadline_rechecks_instead_of_refusing(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 1})
        self.relay_gone()
        issue_calls = [0]
        def gh(*args):
            if args[:2] == ('issue', 'view'):
                issue_calls[0] += 1
                if issue_calls[0] == 2:
                    self.now += closer.CLOSE_WAIT
                    q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 2})
                return {'state': 'CLOSED'}
            return self.gh(*args)
        self.w.gh = gh
        self.run_for(950)
        self.assertNotIn('closeStuck', self.member(7))
        self.assertTrue(self.member(7)['closePending'])
        self.run_for(40)
        self.assertIn(('close', self.PLANNER), self.actions)

    def test_new_closed_no_pr_report_rearms_after_backstop_reopen_refusal(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 1})
        self.relay_gone()
        self.w.gh = lambda *args: {'state': 'OPEN'} if args[:2] == ('issue', 'view') else self.gh(*args)
        self.run_for(1000)
        self.assertEqual(1, self.member(7)['closedAt'])
        self.assertIn('reopened', self.member(7)['closeStuck'])
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))

        identity = str(uuid.uuid4())
        q.atomic_json(self.state / 'claude.json', {'sessionId': identity})
        q.atomic_json(self.state / 'queue-member.json',
                      {'queue': str(self.store.path), 'repo': 'o/r', 'number': 7})
        with patch.dict(os.environ, CLAUDE_CODE_SESSION_ID=identity):
            q.write_loop_state(self.checkout, 'closed', reason='fixed another way')
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 2})
        with self.store.transaction() as data:
            self.assertTrue(q.apply_loop(data, data['members'][0], self.store.path))
        self.assertNotIn('closeStuck', self.member(7))
        self.w.gh = lambda *args: {'state': 'CLOSED'} if args[:2] == ('issue', 'view') else self.gh(*args)
        self.w.close_backstop()
        self.assertEqual('no-pr', q.read_json(self.state / 'relay.json')['close_pending'])
        self.assertTrue(self.member(7)['closePending'])
        self.run_for(1000)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertEqual(2, self.member(7)['closedAt'])

    def test_relay_completed_no_pr_close_is_not_rearmed_without_backstop_history(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='already fixed')
        q.atomic_json(self.state / 'relay.json', {'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7, 'at': 1})
        self.relay_gone()
        self.run_for(1000)
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))
        self.assertNotIn('closePending', self.member(7))
        self.assertNotIn(7, self.w.closes)
        self.assertEqual([], self.actions)

    def test_closed_no_pr_backstop_closes_and_starts_cleanup(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        self.w.gh = lambda *args: {'state': 'CLOSED'} if args[:2] == ('issue', 'view') else self.gh(*args)
        self.run_for(1000)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertEqual([None], [a[3] for a in self.cleanups])
        self.assertNotIn('close_pending', q.read_json(self.state / 'relay.json'))

    def test_ready_helper_stays_open_if_issue_reopens_during_cached_closed_window(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        self.finished_helper()
        issue_calls = [0]
        def gh(*args):
            if args[:2] == ('issue', 'view'):
                issue_calls[0] += 1
                return {'state': 'CLOSED' if issue_calls[0] == 1 else 'OPEN'}
            return self.gh(*args)
        self.w.gh = gh
        self.run_for(950)
        self.assertEqual([], self.actions)
        self.assertIn('helper #7 revmux r1 (helper-pane) stays open: issue not verified CLOSED',
                      (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_preclose_unknown_result_replaces_cached_closed_result(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        issue_calls = [0]
        def gh(*args):
            if args[:2] == ('issue', 'view'):
                issue_calls[0] += 1
                return {'state': 'CLOSED'} if issue_calls[0] == 1 else None
            return self.gh(*args)
        self.w.gh = gh
        self.run_for(950)
        self.assertEqual(2, issue_calls[0])
        self.assertEqual((None, 'invalid issue state'), self.w.closes[7]['issue_result'])
        self.assertEqual([], self.actions)

    def test_open_pr_at_final_gate_refuses_no_pr_backstop(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        self.w.gh = lambda *args: ({'state': 'CLOSED'} if args[:2] == ('issue', 'view') else
                                   [{'number': 8}] if args[:2] == ('pr', 'list') else self.gh(*args))
        self.run_for(950)
        self.assertEqual([], self.actions)
        self.assertIn('an open PR exists', self.member(7)['closeStuck'])
        self.assertFalse((self.state / 'loop-done.json').exists())
        self.assertTrue((self.state / 'loop-done-refused.json').exists())

    def test_unreadable_final_open_pr_list_blocks_no_pr_backstop(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        def gh(*args):
            if args[:2] == ('issue', 'view'):
                return {'state': 'CLOSED'}
            if args[:2] == ('pr', 'list'):
                raise OSError('offline')
            return self.gh(*args)
        self.w.gh = gh
        self.run_for(900 + closer.CLOSE_WAIT + 80)
        self.assertEqual([], self.actions)
        self.assertIn('open PR list unavailable', self.member(7)['closeStuck'])

    def test_closed_no_pr_backstop_keeps_sessions_on_lookup_failure(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        self.finished_helper()
        self.w.gh = Mock(side_effect=OSError('offline'))
        self.run_for(1000)
        self.assertEqual([], self.actions)
        self.assertTrue(self.member(7)['closePending'])
        self.assertLessEqual(self.w.gh.call_count, 2)

    def test_closed_no_pr_backstop_lookup_failure_is_logged_and_notified_at_timeout(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json', {'close_pending': 'no-pr', 'branch': 'issue-7'})
        q.atomic_json(self.state / 'loop-done.json', {'pr': None, 'noPr': True, 'issue': 7})
        self.relay_gone()
        self.finished_helper()
        self.w.gh = Mock(side_effect=OSError('offline'))
        self.run_for(900 + closer.CLOSE_WAIT + 80)
        self.assertEqual([], self.actions)
        self.assertIn('issue state unknown: offline', self.member(7)['closeStuck'])
        self.assertIn('issue state unknown: offline', self.w.notify.call_args.args[0])
        self.assertIn('NOT closing: issue state unknown: offline',
                      (self.state / 'relay-close.log').read_text(encoding='utf-8'))

    def test_closed_no_pr_handoff_keeps_the_conductor_running(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='closed', phase='closed', pr=None, reason='duplicate')
        q.atomic_json(self.state / 'relay.json',
                      {'close_pending': 'no-pr', 'branch': 'issue-7', 'close_handoff': {'pr': 'no-pr', 'reasons': ['mail']}})
        self.assertTrue(q.handed_off(self.member(7)))
        self.assertFalse(q.finished(self.store.load()))

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
        self.conductor_up()
        clock = {'t': 0.0}
        peers = [relay.Peer('claude', 'claude', self.PLANNER), relay.Peer('codex', 'claude', self.IMPLEMENTER)]
        with patch.dict(os.environ), patch.object(relay, 'now', lambda: clock['t']), \
                patch.object(relay, 'pause', lambda s: clock.update(t=clock['t'] + max(s, 1))), \
                patch.object(q.agw, 'my_pane', return_value='relay-7'):
            r = relay.Relay(self.checkout / '.workbench', peers, 'o/r', 'issue-7-fix', 5, 60)
            r.log = lambda text: None
            r.close_after_merge(42)
        hub.reload_paths()
        self.assertIn('handing the close to the queue conductor', (self.state / 'relay-close.log').read_text(encoding='utf-8'))
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

    def conductor_up(self, state='running'):
        """This queue's conductor, as the relay sees it: the worker lock held, the owner's state."""
        with self.store.transaction() as data:
            data['owner']['state'] = state
        lock = q.Lock(self.store.worker_lock, 0).acquire()
        self.addCleanup(lock.release)

    def relay_for_queue(self):
        import hub
        import relay
        q.atomic_json(self.state / 'queue-member.json', {'queue': str(self.store.path), 'repo': 'o/r', 'number': 7})
        with patch.dict(os.environ):
            r = relay.Relay(self.checkout / '.workbench', [], 'o/r', 'issue-7-fix', 5, 60)
        hub.reload_paths()
        return r

    def test_the_relay_sees_whether_the_conductor_is_running(self):
        # r1 M1: the worker lock held AND the owner running; anything else is "nobody to hand to".
        r = self.relay_for_queue()
        self.store.worker_lock.unlink()                          # start() probed it; say it never ran
        self.assertFalse(r.conductor_running())                  # never ran here
        self.assertFalse(self.store.worker_lock.exists())        # r2 m2: and the probe created nothing
        self.store.worker_lock.touch()
        self.assertFalse(r.conductor_running())                  # no conductor holds the worker lock
        self.conductor_up('finished')
        self.assertFalse(r.conductor_running())                  # held, but it has published finished
        with self.store.transaction() as data:
            data['owner']['state'] = 'running'
        self.assertTrue(r.conductor_running())
        q.atomic_json(self.state / 'queue-member.json', {'queue': str(self.root / 'missing.json')})
        self.assertFalse(r.conductor_running())                  # a removed queue: no handoff
        self.assertFalse((self.root / 'missing').exists())       # ... and nothing created for it
        q.atomic_json(self.state / 'queue-member.json', {'queue': str(self.store.path)})
        with self.store.transaction() as data:
            data['parallel'] = 99                                # r2 i1: the Store's validated read
        self.assertFalse(r.conductor_running())

    def test_the_relays_worker_lock_probe_is_under_the_state_lock(self):
        # r2 m2: -Queue start() probes the worker lock under the state lock; so must the relay, or its
        # momentary hold makes a concurrent start believe a conductor is running.
        r = self.relay_for_queue()
        self.conductor_up()
        real = q.Store.running
        seen = []

        def running(store):
            try:
                q.Lock(store.state_lock, 0).acquire().release()
                seen.append('state lock free')
            except q.QueueError:
                seen.append('state lock held')
            return real(store)
        with patch.object(q.Store, 'running', running):
            self.assertTrue(r.conductor_running())
        self.assertEqual(['state lock held'], seen)

    def test_finished_reads_no_relay_state_unless_it_would_finish(self):
        # r2 m1: every member's relay.json is read under the state lock - only when it matters.
        with patch.object(q, 'handed_off', side_effect=AssertionError('read')):
            with self.store.transaction() as data:
                data['members'][0].update(state='pending')
            self.assertFalse(q.finished(self.store.load()))
            with self.store.transaction() as data:
                data['members'][0].update(state='pr-open')
                data.update(watch=True, label='queue')              # a watching queue never finishes
            self.assertFalse(q.finished(self.store.load()))

    def test_a_handed_off_close_keeps_a_finished_queue_up_until_the_backstop_ends(self):
        # r1 M1: a non-watch queue whose only member has its PR is otherwise finished - before the
        # conductor even sees the merge. Driven through tick(), as run() does.
        with self.store.transaction() as data:
            data['members'][0].update(state='pr-open', prState='OPEN')
        self.assertTrue(q.finished(self.store.load()))           # nothing handed over: it may finish
        q.atomic_json(self.state / 'relay.json', {'close_pending': 42, 'close_handoff': {'pr': 42, 'reasons': ['x']}})
        self.relay_gone()
        self.assertFalse(q.finished(self.store.load()))
        self.pr_state = 'MERGED'
        end = self.now + 1300
        while self.now < end and not q.finished(self.store.load()):
            self.w.tick()
            self.now += 20
        self.assertTrue(q.finished(self.store.load()))
        self.assertEqual('merged', self.member(7)['state'])
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertEqual([42], [a[3] for a in self.cleanups])
        self.assertNotIn('close_handoff', q.read_json(self.state / 'relay.json'))

    def test_a_relay_alive_flag_is_cleared_when_the_backstop_takes_over(self):
        # r1 m1: flagged while the relay lived; once it is gone the attempt must keep the queue up.
        self.run_for(1000)
        self.assertIn('relay is alive', self.member(7)['closeStuck'])
        self.relay_gone()
        self.w.close_backstop()                                  # the attempt starts
        self.assertIsNotNone(self.w.closes[7]['attempt'])
        self.assertNotIn('closeStuck', self.member(7))
        self.assertFalse(q.finished(self.store.load()))
        self.run_for(200)
        self.assertIn(('close', self.PLANNER), self.actions)
        self.assertTrue(q.finished(self.store.load()))

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


class EnvironmentalBlocks(unittest.TestCase):
    """#61: a member blocked by its environment keeps its slot while its session lives; the number
    of live members is capped at parallel + 2, whatever state the slots are in."""
    terminal, start, gh, spawn, worker, member, report = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                          QueueCase.spawn, QueueCase.worker, QueueCase.member,
                                                          QueueCase.report)

    def setUp(self):
        QueueCase.setUp(self)
        self.sessions = set()

    def launched(self):
        return [n for n, _, _ in self.launches]

    def relay_episode(self, n, tool='codex', kind='warning', box='codex'):
        path = Path(self.member(n)['checkout']) / '.workbench/state/relay.json'
        q.atomic_json(path, {'limits': {box: {'kind': kind, 'tool': tool, 'line': 'Approaching rate limits',
                                              'firstSeen': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                                              'announced': True}}})

    def test_an_environmental_block_keeps_the_slot_while_its_session_lives(self):
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.report(1, 'blocked', reason='codex limited, failover off', cause='environment')
        w.tick(); w.tick()
        self.assertEqual(('blocked', False, 'environment'),
                         (self.member(1)['state'], self.member(1)['slotReleased'], self.member(1)['cause']))
        self.assertEqual([1], self.launched())
        self.assertFalse(q.finished(self.store.load()))
        self.sessions = set()                       # the human closed it: the slot goes with it
        w.tick()
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        self.assertEqual([1, 2], self.launched())

    def test_an_environmental_slot_survives_a_missed_read(self):
        # r3 M1: a terminal that misses every session for one tick must not admit anyone.
        self.start('o/r#1,2,3', parallel=2)
        w = self.worker()
        w.tick(); w.tick()
        self.assertEqual([1, 2], self.launched())
        self.sessions = {1, 2}
        for n in (1, 2):
            self.report(n, 'blocked', reason='codex limited', cause='environment')
        w.tick()
        self.sessions = set()                       # agwinterm restarting: one read lists nothing
        w.tick()
        self.assertEqual([1, 2], self.launched())
        self.assertFalse(any(self.member(n)['slotReleased'] for n in (1, 2)))
        self.assertFalse(q.finished(self.store.load()))
        self.sessions = {1, 2}
        w.tick()
        self.assertNotIn('sessionGoneSince', self.member(1))
        self.assertEqual([1, 2], self.launched())
        self.assertFalse(self.member(1)['slotReleased'])

    def test_three_members_blocked_on_a_tool_limit_launch_nothing(self):
        self.start('o/r#1,2,3,4', parallel=3)
        w = self.worker()
        w.tick(); w.tick()
        self.assertEqual([1, 2, 3], self.launched())
        with self.store.transaction() as data:
            data['parallel'] = 1
        self.sessions = {1, 2, 3}
        for n in (1, 2, 3):
            self.report(n, 'blocked', reason='codex limited', cause='environment')
        for _ in range(3):
            w.tick()
        self.assertEqual([1, 2, 3], self.launched())
        self.assertEqual('pending', self.member(4)['state'])

    def test_a_plain_block_under_an_announced_relay_limit_keeps_the_slot(self):
        # The incident's shape: the planner reported a plain `blocked`, the relay had announced a limit.
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.relay_episode(1)
        self.report(1, 'blocked', reason='codex shows the chooser; human must answer it')
        w.tick(); w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        self.assertEqual([1], self.launched())

    def test_a_question_still_releases_the_slot_and_resumed_takes_it_back(self):
        self.start('o/r#1,2,3', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.report(1, 'blocked', reason='which API?')
        w.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        self.assertEqual([1, 2], self.launched())
        self.report(1, 'resumed')
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        self.assertNotIn('cause', self.member(1))
        self.report(2, 'pr-open')
        w.tick()
        self.assertEqual([1, 2], self.launched())   # #1 holds the only slot again

    def test_a_resumed_member_with_a_pr_frees_its_slot_when_its_session_is_gone(self):
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.report(1, 'pr-open')
        w.tick()
        self.report(1, 'blocked', reason='a question on review')
        w.tick()
        self.report(1, 'resumed')
        w.tick()
        self.assertEqual(('active', False), (self.member(1)['state'], self.member(1)['slotReleased']))
        self.assertTrue(self.member(1)['pr'])
        w.tick()
        self.assertEqual([1, 2], self.launched())     # #2 took the slot #1 freed at pr-open
        self.report(2, 'pr-open')
        with self.store.transaction() as data:        # a third member waits behind #1's slot
            data['members'].append(q.new_member(3, 'o/r', self.root / 'clones'))
        w.tick()
        self.assertEqual([1, 2], self.launched())
        self.sessions = {2}                           # #1's sessions die; its PR is never merged
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])            # r2 m1: one missed read is not enough
        self.assertEqual([1, 2], self.launched())
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        self.assertEqual([1, 2, 3], self.launched())

    def test_a_watched_slot_survives_a_missed_read_and_is_taken_back(self):
        # r2 m1: re-decided every tick, with a grace; the session coming back takes the slot back.
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.report(1, 'blocked', reason='a question')
        w.tick()
        self.report(1, 'resumed')
        w.tick()
        self.assertTrue(self.member(1)['resumed'])
        self.sessions = set()                         # agwinterm restarting: the tree misses it once
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        self.sessions = {1}
        w.tick()
        self.assertNotIn('sessionGoneSince', self.member(1))
        self.sessions = set()
        w.tick()
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        self.sessions = {1, 2}                        # back after all: the slot is taken again
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])

    def test_a_resumed_member_without_a_pr_frees_its_slot_when_its_session_is_gone(self):
        # r2 m2: blocked before its PR, resumed, then its sessions died; refresh_stale keeps it active.
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.report(1, 'blocked', reason='a question')
        w.tick()
        self.assertEqual([1, 2], self.launched())
        self.report(2, 'closed', reason='duplicate')
        self.report(1, 'resumed')
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        self.assertFalse(q.finished(self.store.load()))
        self.sessions = set()
        w.tick()
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertEqual(('active', True), (self.member(1)['state'], self.member(1)['slotReleased']))
        self.assertTrue(q.finished(self.store.load()))
        self.report(1, 'pr-open')
        w.tick()
        self.assertNotIn('resumed', self.member(1))

    def test_an_orphaned_active_member_does_not_count_against_the_ceiling(self):
        # r2 M1: an active member whose session is gone and slot released is not live.
        self.start('o/r#1,2,3,4', parallel=1)
        with self.store.transaction() as data:
            for m in data['members'][:3]:
                m.update(attempt=1, token=str(uuid.uuid4()), pr=f'https://github.com/o/r/pull/{m["number"]}',
                         state='pr-open', phase='pr-open', slotReleased=True)
            data['members'][0].update(state='active', phase='active', sessionGoneSince=self.now - q.SESSION_GRACE)
        self.sessions = {2, 3}
        w = self.worker()
        w.tick()
        self.assertEqual([4], self.launched())

    def test_the_live_ceiling_holds_in_a_cascade(self):
        self.start('o/r#' + ','.join(map(str, range(1, 9))), parallel=1)
        w = self.worker()
        peak = 0
        for _ in range(12):
            w.tick()
            for m in self.store.load()['members']:
                if m['state'] == 'active' and not m['slotReleased']:
                    self.sessions.add(m['number'])
                    self.report(m['number'], 'blocked', reason='a question for the human')
            w.tick()
            live = sum(m['state'] in {'launching', 'active', 'blocked', 'pr-open'} and m['number'] in self.sessions
                       for m in self.store.load()['members'])
            peak = max(peak, live)
        self.assertEqual([1, 2, 3], self.launched())
        self.assertEqual(3, peak)
        self.sessions.discard(1)                    # one member's sessions close: one more may start
        w.tick()
        self.assertEqual([1, 2, 3], self.launched())            # r4 M1: not on one blank read
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertEqual([1, 2, 3, 4], self.launched())

    def test_an_unreadable_terminal_counts_every_member_as_live(self):
        self.start('o/r#1,2,3,4', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        with self.store.transaction() as data:
            for m in data['members'][1:3]:
                m.update(state='pr-open', phase='pr-open', slotReleased=True, attempt=1, token=str(uuid.uuid4()))
            data['members'][0]['state'] = 'blocked'
            data['members'][0]['slotReleased'] = True
        with patch.object(q.agw, 'tree', side_effect=q.agw.CtlError('no pipe')):
            w.tick()
        self.assertEqual([1], self.launched())
        self.assertFalse(any('sessionGoneSince' in m for m in self.store.load()['members']))   # nothing stamped
        w.tick()                                    # readable, and none of them has a session
        self.assertEqual([1], self.launched())      # r4 M1: one blank read admits nobody
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertEqual([1, 4], self.launched())

    def test_one_blank_read_at_the_ceiling_admits_nobody(self):
        # r4 M1: the ceiling has the same grace as the slots.
        self.start('o/r#1,2,3,4', parallel=1)
        w = self.worker()
        with self.store.transaction() as data:
            for m in data['members'][:3]:
                m.update(attempt=1, token=str(uuid.uuid4()), pr=f'https://github.com/o/r/pull/{m["number"]}',
                         state='pr-open', phase='pr-open', slotReleased=True)
        self.sessions = {1, 2, 3}
        w.tick()
        self.assertEqual([], self.launched())
        self.sessions = set()                       # agwinterm restarting
        w.tick()
        self.assertEqual([], self.launched())
        self.sessions = {1, 2, 3}                   # back: the stamps go
        w.tick()
        self.assertFalse(any('sessionGoneSince' in m for m in self.store.load()['members']))
        self.sessions = {2, 3}                      # #1's sessions really close
        w.tick()
        self.assertEqual([], self.launched())
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertEqual([4], self.launched())

    def test_an_unreadable_tree_changes_no_slot_and_no_stamp(self):
        # r4 m2: a failed read neither re-takes a released slot nor resets its stamp.
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.report(1, 'blocked', reason='x')
        w.tick()
        self.report(1, 'resumed')
        w.tick()
        self.sessions = set()
        w.tick()
        stamp = self.member(1)['sessionGoneSince']
        self.now += q.SESSION_GRACE
        w.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        with patch.object(q.agw, 'tree', side_effect=q.agw.CtlError('no pipe')):
            w.tick()
        self.assertTrue(self.member(1)['slotReleased'])
        self.assertEqual(stamp, self.member(1)['sessionGoneSince'])

    def test_an_unreadable_tree_holds_a_relay_held_slot_on_a_plain_block(self):
        # r5 m1: apply_loop releases a plain block; with the tree unreadable that tick the relay's
        # episode must still hold the slot, since no grace has started.
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.relay_episode(1)
        self.report(1, 'blocked', reason='chooser')
        with patch.object(q.agw, 'tree', side_effect=q.agw.CtlError('no pipe')):
            w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        self.assertEqual([1], self.launched())

    def test_a_readable_tree_forgets_an_old_stamp_nobody_asked_about(self):
        # r5 m3: forget_stale_stamps alone clears #2's stamp - the ceiling is not reached that tick,
        # and #1's slot watch is what reads the tree.
        self.start('o/r#1,2,3,4', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1, 2, 3}
        self.report(1, 'blocked', reason='x')
        w.tick()
        self.report(1, 'resumed')
        with self.store.transaction() as data:
            for m in data['members'][1:3]:
                m.update(attempt=1, token=str(uuid.uuid4()), pr=f'https://github.com/o/r/pull/{m["number"]}',
                         state='pr-open', phase='pr-open', slotReleased=True)
            data['members'][1]['sessionGoneSince'] = self.now - 10          # an old blank read
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])                    # count >= parallel: no ceiling check
        self.assertNotIn('sessionGoneSince', self.member(2))
        before = self.launched()                                            # #2 started while #1 was blocked
        self.now += q.SESSION_GRACE
        self.report(1, 'pr-open')
        self.sessions = set()                                               # one blank read at the ceiling
        w.tick()
        self.assertEqual('pending', self.member(4)['state'])
        self.assertEqual(before, self.launched())

    def test_an_unreadable_relay_record_keeps_a_relay_held_slot(self):
        # r4 m1: a plain block held by the relay's episode stays held when relay.json cannot be read.
        self.start('o/r#1,2', parallel=1)
        w = self.worker()
        w.tick(); w.tick()
        self.sessions = {1}
        self.relay_episode(1)
        self.report(1, 'blocked', reason='chooser')
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        (Path(self.member(1)['checkout']) / '.workbench/state/relay.json').write_text('{broken', encoding='utf-8')
        w.tick()
        self.assertFalse(self.member(1)['slotReleased'])
        self.assertEqual([1], self.launched())


class LimitWait(unittest.TestCase):
    """#77: in a queue started with -WaitOnLimit a member whose agent (or reviewer) hits its usage limit
    keeps its slot and waits; nobody new starts, nothing is failed over, and nobody is notified."""
    terminal, start, gh, worker, member, report, spawn = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                          QueueCase.worker, QueueCase.member, QueueCase.report,
                                                          QueueCase.spawn)

    def setUp(self):
        QueueCase.setUp(self)
        self.start('o/r#1,2,3', parallel=2, on_limit='wait', implementer='kimi')
        self.w = self.worker()
        self.w.notify, self.w.status = Mock(), Mock()
        self.w.tick(); self.w.tick()               # #1 and #2 active
        self.sessions = {1, 2}
        self.at = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)

    def launched(self):
        return [n for n, *_ in self.launches]

    def state_dir(self, n):
        return Path(self.member(n)['checkout']) / '.workbench/state'

    def relay_wait(self, n, wait=True, retry=None):
        episode = {'kind': 'limited', 'tool': 'kimi', 'line': 'Error: 403 5-hour usage limit',
                   'firstSeen': self.at.isoformat(timespec='seconds'), 'announced': True}
        if wait:
            episode.update(wait=True, retryAt=retry or self.at.timestamp() + 1800, probes=0, probeAt=None)
        q.atomic_json(self.state_dir(n) / 'relay.json', {'limits': {'codex': episode}})

    def out(self):
        return sys.stdout.getvalue()

    def test_a_waiting_member_keeps_its_slot_and_nobody_new_starts(self):
        self.relay_wait(1)
        self.report(2, 'pr-open')                  # a free slot: #3 would start
        self.w.tick(); self.w.tick()
        self.assertEqual([1, 2], self.launched())
        self.assertEqual('pending', self.member(3)['state'])
        m = self.member(1)
        self.assertEqual(('active', False), (m['state'], m['slotReleased']))
        self.assertEqual({'tool': 'kimi', 'since': self.at.timestamp(), 'retryAt': self.at.timestamp() + 1800,
                          'source': 'relay codex'}, m['limitWait'])
        (self.state_dir(1) / 'relay.json').unlink()   # the limit reset: the relay ended the episode
        self.w.tick()
        self.assertNotIn('limitWait', self.member(1))
        self.assertEqual([1, 2, 3], self.launched())

    def test_the_wait_is_printed_once_and_its_end_too_and_never_notified(self):
        self.relay_wait(1)
        for _ in range(3):
            self.w.tick()
        since = time.strftime('%H:%M', time.localtime(self.at.timestamp()))
        retry = time.strftime('%H:%M', time.localtime(self.at.timestamp() + 1800))
        self.assertEqual(1, self.out().count(f'waiting: #1 kimi usage limit since {since}, next try {retry}'))
        self.assertIn('#1: active waiting-limit', self.out())
        (self.state_dir(1) / 'relay.json').unlink()
        self.w.tick(); self.w.tick()
        self.assertEqual(1, self.out().count('queue resumed: usage limit cleared'))
        self.w.notify.assert_not_called()
        self.assertNotIn(('blocked',), [c.args for c in self.w.status.call_args_list])
        self.assertNotIn('toolsPaused', self.store.load())

    def test_a_blocked_member_that_starts_waiting_is_not_notified_again(self):
        self.report(1, 'blocked', reason='kimi limited', cause='environment')
        self.w.tick()
        self.assertEqual(1, self.w.notify.call_count)
        self.relay_wait(1)
        self.w.tick(); self.w.tick()
        self.assertEqual(1, self.w.notify.call_count)

    def test_a_wait_episode_is_not_a_tool_limit(self):
        self.relay_wait(1)
        self.w.tick()
        self.assertNotIn('toolLimits', self.store.load())
        self.assertEqual([], q.member_limits(self.member(1)))
        self.relay_wait(1, wait=False)                # a failover episode is still one
        self.assertEqual(['kimi'], [tool for tool, *_ in q.member_limits(self.member(1))])

    def test_a_wait_queue_never_routes_to_another_tool(self):
        limits = {'codex': {'kind': 'limited', 'member': 1, 'line': 'x', 'at': 1}}
        self.assertEqual((None, None), tool_route({'config': str(self.config), 'onLimit': 'wait',
                                                    'implementer': 'codex', 'toolLimits': limits}))
        self.assertEqual(('claude', None), tool_route({'config': str(self.config), 'onLimit': 'failover',
                                                        'implementer': 'codex', 'toolLimits': limits}))

    def test_a_review_limit_waits_too(self):
        q.atomic_json(self.state_dir(1) / 'review-limit.json',
                      {'tool': 'kimi', 'since': self.at.timestamp(), 'retryAt': self.at.timestamp() + 1800, 'round': 2})
        self.report(2, 'pr-open')
        self.w.tick(); self.w.tick()
        self.assertEqual([1, 2], self.launched())
        self.assertEqual('review', self.member(1)['limitWait']['source'])
        self.assertIn('#1 kimi reviewer usage limit since', self.out())

    def test_a_review_wait_past_its_retry_time_no_longer_gates(self):
        # FIX r1 M3: a review-limit.json nobody cleaned up must not pin the queue for days.
        self.report(1, 'blocked', reason='waiting on the human', cause='environment')
        q.atomic_json(self.state_dir(1) / 'review-limit.json',
                      {'tool': 'kimi', 'since': self.now - 3600, 'retryAt': self.now + 60, 'round': 2})
        self.report(2, 'pr-open')
        self.w.tick(); self.w.tick()
        self.assertEqual([1, 2], self.launched())
        self.assertIn('limitWait', self.member(1))
        self.now += 61
        self.w.tick(); self.w.tick()
        self.assertNotIn('limitWait', self.member(1))
        self.assertEqual([1, 2, 3], self.launched())

    def test_a_dead_member_cannot_pin_the_queue(self):
        self.relay_wait(1)
        self.report(2, 'pr-open')
        self.w.tick()
        self.assertEqual([1, 2], self.launched())
        self.sessions = {2}
        self.w.tick()                                  # first missed: the grace starts, it still waits
        self.assertIn('limitWait', self.member(1))
        self.now += q.SESSION_GRACE + 1
        self.w.tick(); self.w.tick()
        self.assertNotIn('limitWait', self.member(1))
        self.assertEqual([1, 2, 3], self.launched())

    def test_the_summary_shows_waiting_limit(self):
        self.relay_wait(1)
        self.w.tick()
        summary = q.summary(self.store.load())
        self.assertIn('| 1 | waiting-limit (active) |', summary)
        self.assertIn('| 2 | active |', summary)


class ToolLimits(unittest.TestCase):
    """#61: a usage limit recorded by any live member steers new members to the other tool, or pauses
    the queue when no tool is usable; only the human's -ClearLimit clears it."""
    terminal, start, gh, worker, member, report = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                   QueueCase.worker, QueueCase.member, QueueCase.report)

    def setUp(self):
        QueueCase.setUp(self)
        self.implementers, self.profiles, self.models = [], [], []
        self.start('o/r#1,2,3', parallel=1)
        self.w = self.worker()
        self.w.notify, self.w.status = Mock(), Mock()
        self.w.tick(); self.w.tick()               # #1 active
        self.implementers.clear()
        self.models.clear()

    def spawn(self, data, m):
        self.implementers.append((m['number'], data.get('implementer')))
        self.models.append((m['number'], data.get('implementerModel'), data.get('rosterId')))
        self.profiles.append((m['number'], data.get('revmuxProfile')))
        return QueueCase.spawn(self, data, m)

    def record(self, n, tool='codex', at=None, kind='warning', relay=False):
        at = datetime.now(timezone.utc) if at is None else at
        state = Path(self.member(n)['checkout']) / '.workbench/state'
        if relay:
            q.atomic_json(state / 'relay.json', {'limits': {'codex': {
                'kind': kind, 'tool': tool, 'line': f'{tool} {kind}', 'firstSeen': at.isoformat(timespec='seconds'),
                'announced': True}}})
        else:
            # The launcher writes [DateTime]::UtcNow.ToString('o'): seven fractional digits and a Z.
            stamp = at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '7Z'
            q.atomic_json(state / 'implementer.json', {'tool': 'claude', 'limits': {tool: {
                'at': stamp, 'line': f'{tool} {kind}', 'kind': kind}}})

    def next_member(self):
        self.report(1, 'pr-open')
        self.w.tick()

    def test_a_limited_codex_sends_new_members_to_claude(self):
        self.record(1)
        self.next_member()
        self.assertEqual([(2, 'claude')], self.implementers)
        limit = self.store.load()['toolLimits']['codex']
        self.assertEqual(('warning', 1), (limit['kind'], limit['member']))
        self.assertNotIn('implementer', self.store.load())          # the queue's setting stays the human's

    def test_a_failover_to_a_bare_tool_pins_no_model_and_no_roster_id(self):
        self.record(1)
        self.next_member()
        self.assertEqual([(2, 'claude')], self.implementers)
        self.assertEqual([(2, None, None)], self.models)           # claudeImplementerModel stays in charge

    def test_a_failover_to_a_roster_id_carries_its_model_and_id(self):
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'failoverOrder': ['claude-opus', 'kimi']}))
        self.record(1)
        self.next_member()
        self.assertEqual([(2, 'claude')], self.implementers)
        self.assertEqual([(2, 'claude-opus-5-5', 'claude-opus')], self.models)

    def test_a_failover_to_the_kimi_entry_keeps_its_roster_id(self):
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'failoverOrder': ['kimi', 'claude']}))
        self.record(1)
        self.next_member()
        self.assertEqual([(2, 'kimi')], self.implementers)
        self.assertEqual([(2, None, 'kimi')], self.models)

    def test_a_relay_episode_is_read_by_its_tool_not_its_box(self):
        self.record(1, tool='claude', kind='limited', relay=True)   # a Claude implementer in the codex box
        self.next_member()
        self.assertEqual([], self.implementers)
        self.assertIn('the planner is always Claude', self.store.load()['toolsPaused'])
        self.w.notify.assert_any_call('queue paused: ' + self.store.load()['toolsPaused'])
        self.w.status.assert_called_with('blocked')

    def test_failover_off_pauses_instead_of_switching(self):
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'failover': False}))
        self.record(1)
        self.next_member()
        self.assertEqual([], self.implementers)
        self.assertIn('failover is off', self.store.load()['toolsPaused'])

    def test_a_claude_queue_is_not_rerouted_by_a_codex_limit(self):
        self.start('o/r#1', implementer='claude')
        self.record(1)
        self.next_member()
        self.assertEqual([(2, None)], [(n, None if tool == 'claude' else tool) for n, tool in self.implementers])

    def test_a_limited_kimi_queue_fails_over_by_the_order(self):
        # #65: a kimi queue whose kimi is limited goes to the first free tool in failoverOrder.
        self.start('o/r#1', implementer='kimi')
        self.record(1, tool='kimi', kind='limited', relay=True)
        self.next_member()
        self.assertEqual([(2, 'claude')], self.implementers)
        self.assertEqual('kimi', self.store.load()['implementer'])
        self.assertEqual('kimi', self.store.load()['toolLimits']['kimi']['line'].split()[0])

    def test_a_routed_launch_drops_the_queues_revmux_profile(self):
        # #66: the queue's profile was chosen for its tool; a member routed to another tool gets that
        # tool's default from the launcher, never kimi-mixed while kimi is limited.
        self.start('o/r#4', implementer='kimi', revmux_profile='kimi-mixed')
        self.next_member()
        self.assertEqual((2, 'kimi-mixed'), self.profiles[-1])
        self.record(2, tool='kimi', kind='limited', relay=True)
        self.report(2, 'pr-open')
        self.w.tick()
        self.assertEqual([(2, 'kimi'), (3, 'claude')], self.implementers[-2:])
        self.assertEqual((3, None), self.profiles[-1])
        self.assertEqual('kimi-mixed', self.store.load()['revmuxProfile'])     # the queue's setting stays

    def test_failover_order_is_configurable_and_skips_limited_tools(self):
        self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'),
                                           'failoverOrder': ['kimi', 'codex', 'claude']}))
        self.record(1)                                               # codex limited
        self.next_member()
        self.assertEqual([(2, 'kimi')], self.implementers)
        self.assertEqual(('kimi', None), tool_route({'config': str(self.config), 'toolLimits': {
            'codex': {'kind': 'limited', 'member': 1, 'line': 'x', 'at': 1}}}))
        both = {'config': str(self.config), 'toolLimits': {
            'codex': {'kind': 'limited', 'member': 1, 'line': 'x', 'at': 1},
            'kimi': {'kind': 'limited', 'member': 2, 'line': 'y', 'at': 2}}}
        self.assertEqual(('claude', None), tool_route(both))
        self.config.write_text(json.dumps({'failoverOrder': ['codex', 'kimi']}))
        route, reason = tool_route(both)
        self.assertIsNone(route)
        self.assertIn('no tool in failoverOrder', reason)
        for bad in (['codex'], ['codex', 'codex'], ['codex', 'aider'], 'codex,claude'):
            with self.subTest(order=bad):
                self.config.write_text(json.dumps({'failoverOrder': bad}))
                route, reason = tool_route(both)
                self.assertIsNone(route)
                self.assertIn('failoverOrder', reason)

    def test_the_default_order_keeps_codex_and_claude_as_before(self):
        limits = lambda *tools: {'config': str(self.config), 'toolLimits': {
            tool: {'kind': 'limited', 'member': 1, 'line': tool, 'at': 1} for tool in tools}}
        self.assertEqual(('claude', None), tool_route(limits('codex')))
        self.assertIn('planner is always Claude', tool_route(limits('claude'))[1])
        self.assertEqual((None, None), tool_route(limits('kimi')))    # a codex queue ignores a kimi limit
        self.config.write_text(json.dumps({'failoverOrder': None}))      # null is the default too (FIX r1 m3)
        self.assertEqual(('claude', None), tool_route(limits('codex')))

    def test_clear_limit_and_implementer_accept_kimi(self):
        self.record(1, tool='kimi', kind='limited', relay=True)
        self.w.tick()
        self.assertIn('kimi', self.store.load()['toolLimits'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.start('o/r#1', clear_limit='kimi', implementer='kimi')
        self.assertNotIn('toolLimits', self.store.load())
        self.assertEqual('kimi', self.store.load()['implementer'])
        with self.assertRaises(q.UsageError):
            self.start('o/r#1', implementer='aider')

    def test_clear_limit_forgets_it_and_old_records_stay_ignored(self):
        self.record(1, at=datetime.now(timezone.utc) - timedelta(minutes=5))
        self.w.tick()
        self.assertIn('codex', self.store.load()['toolLimits'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.start('o/r#1', clear_limit='codex')                # "codex has reset"
        self.assertIn('cleared the recorded usage limit for codex (codex warning)', out.getvalue())
        self.assertNotIn('toolLimits', self.store.load())
        self.assertNotIn('implementer', self.store.load())          # nothing else changes
        self.next_member()
        self.assertEqual([(2, None)], self.implementers)             # #1's old failover does not bring it back
        self.assertNotIn('toolLimits', self.store.load())
        self.record(2, at=datetime.now(timezone.utc) + timedelta(minutes=1), relay=True)
        self.report(2, 'pr-open')
        self.w.tick()
        self.assertEqual([(2, None), (3, 'claude')], self.implementers)

    def test_clear_limit_claude_resumes_a_codex_queue_without_switching_it(self):
        with self.store.transaction() as data:
            data['implementer'] = 'codex'
        self.record(1, tool='claude', kind='limited', relay=True)
        self.next_member()
        self.assertIn('toolsPaused', self.store.load())
        self.start('o/r#1', clear_limit='claude')
        self.w.tick()
        self.assertNotIn('toolsPaused', self.store.load())
        self.assertEqual('codex', self.store.load()['implementer'])
        self.assertEqual([(2, 'codex')], self.implementers)

    def test_implementer_alone_never_clears_a_limit(self):
        self.record(1, at=datetime.now(timezone.utc) - timedelta(minutes=5))
        self.w.tick()
        self.start('o/r#1', implementer='codex')
        self.assertIn('codex', self.store.load()['toolLimits'])
        self.assertNotIn('toolLimitsClearedAt', self.store.load())
        self.next_member()
        self.assertEqual([(2, 'claude')], self.implementers)         # still routed away from the limit

    def test_dry_run_shows_the_clear_and_writes_nothing(self):
        self.record(1)
        self.w.tick()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.start('o/r#1', clear_limit='codex', dry_run=True)
        shown = json.loads(out.getvalue())
        self.assertEqual(('codex', 'codex warning'), (shown['clearLimit']['tool'], shown['clearLimit']['recorded']['line']))
        self.assertIn('codex', self.store.load()['toolLimits'])

    def test_a_transient_read_error_does_not_alert(self):
        # r1 m1: the error count resets on success, and each operation has its own key.
        self.report(1, 'blocked', reason='x')
        path = Path(self.member(1)['checkout']) / '.workbench/state/relay.json'
        for broken in (True, False, True, True):
            path.write_text('{broken' if broken else '{}', encoding='utf-8')
            self.w.tick()
        self.assertEqual([], [c for c in self.w.notify.call_args_list if 'limits #1' in c.args[0]
                              or 'environment #1' in c.args[0]])
        self.assertEqual(2, self.w.errors['environment #1'])

    def test_malformed_tool_limits_are_refused(self):
        for bad in ({'toolLimits': {'aider': {}}}, {'toolLimits': {'codex': {'at': 'x'}}}, {'implementer': 'aider'},
                    {'toolLimitsClearedAt': {'codex': 'yesterday'}}, {'toolsPaused': 3}, {'ramPaused': 3}):
            with self.subTest(bad=bad):
                data = q.read_json(self.store.path)
                q.atomic_json(self.store.path, dict(data, **bad))
                with self.assertRaises(q.StateError):
                    self.store.load()
                q.atomic_json(self.store.path, data)


class MemoryGuard(unittest.TestCase):
    """#61: below minFreeRamGB the conductor admits nothing and fails nothing, like the disk guard."""
    terminal, start, gh, spawn, worker, member = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                                  QueueCase.spawn, QueueCase.worker, QueueCase.member)

    def setUp(self):
        QueueCase.setUp(self)
        self.start('o/r#1,2,3', parallel=2)
        self.w = self.worker()
        self.w.notify = Mock()
        self.w.status = Mock()

    def test_low_memory_pauses_admission_and_resumes(self):
        self.ram = 1.1 * q.GIB
        for _ in range(3):
            self.w.tick()
        self.assertEqual([], self.launches)
        self.assertEqual('low memory: 1.1 GB free < 3 GB', self.store.load()['ramPaused'])
        self.w.notify.assert_called_once_with('queue paused: low memory: 1.1 GB free < 3 GB')
        self.w.status.assert_called_once_with('blocked')
        self.ram = 8 * q.GIB
        self.w.tick()
        self.assertEqual([1, 2], [n for n, _, _ in self.launches])
        self.assertNotIn('ramPaused', self.store.load())
        self.assertEqual('queue resumed: memory is back', self.w.notify.call_args.args[0])
        self.w.status.assert_called_with('active')

    def test_an_orphaned_launch_is_not_respawned_while_memory_is_low(self):
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', attempt=1, token=str(uuid.uuid4()), result=None,
                                      startedAt=self.now - 700, slotReleased=False)
        self.ram = 1 * q.GIB
        self.w.tick()
        self.assertEqual([], self.launches)
        self.assertEqual('launching', self.member(1)['state'])
        self.ram = 8 * q.GIB
        self.w.tick()
        self.assertIn(1, [n for n, _, _ in self.launches])
        self.assertNotEqual('failed', self.member(1)['state'])

    def test_disk_and_memory_pauses_share_one_status(self):
        self.ram, self.free = 1 * q.GIB, 5 * q.GIB
        self.w.tick()
        self.ram = 8 * q.GIB
        self.w.tick()                                # memory is back, the disk is still low
        self.assertEqual('blocked', self.w.status.call_args.args[0])
        self.assertEqual([], self.launches)

    def test_the_threshold_comes_from_the_config(self):
        self.ram = 2 * q.GIB
        for value, admitted in ((0, True), (1.5, True), (4, False)):
            with self.subTest(value=value):
                self.launches.clear()
                with self.store.transaction() as data:
                    for m in data['members']:
                        m.update(state='pending', attempt=0, slotReleased=False)
                self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'minFreeRamGB': value}))
                self.w.tick()
                self.assertEqual(admitted, bool(self.launches))

    def test_an_invalid_threshold_pauses_rather_than_admits(self):
        for value in (-1, 'x', True):
            with self.subTest(value=value):
                self.config.write_text(json.dumps({'checkoutRoot': str(self.root / 'clones'), 'minFreeRamGB': value}))
                self.w.tick()
                self.assertEqual([], self.launches)
                self.assertIn('minFreeRamGB', self.store.load()['ramPaused'])

    def test_free_ram_reads_this_machine(self):
        free = REAL_FREE_RAM()
        self.assertTrue(free is None or 0 < free < 1024 * q.GIB, free)
        if os.name == 'nt':
            self.assertIsNotNone(free)


class LaunchBackoff(unittest.TestCase):
    terminal, start, gh, worker, member = (QueueCase.terminal, QueueCase.start, QueueCase.gh,
                                          QueueCase.worker, QueueCase.member)

    def setUp(self):
        QueueCase.setUp(self)
        self.start('o/r#1,2', parallel=1)
        self.failures = 3
        self.w = self.worker()
        self.w.notify, self.w.status = Mock(), Mock()
        self.w.spawn = self.spawn

    def spawn(self, data, m):
        job = QueueCase.spawn(self, data, m)
        if self.failures:
            self.failures -= 1
            with self.store.transaction() as current:
                current['members'][m['number'] - 1]['result'] = dict(
                    result='incomplete', infra=True, stage='codex', detail='pane too narrow',
                    attempt=m['attempt'], token=m['token'])
        return job

    def test_backoff_same_member_pause_and_resume(self):
        self.w.tick()
        for failures, delay in enumerate((60, 120, 240), 1):
            self.w.tick()
            self.assertEqual('pending', self.member(1)['state'])
            self.assertEqual(failures, self.store.load()['launchBackoff']['failures'])
            self.assertEqual(self.now + delay, self.store.load()['launchBackoff']['until'])
            self.w.tick()
            self.assertEqual(failures, len(self.launches))
            self.now += delay
            self.w.tick()
        self.assertEqual([1, 1, 1, 1], [n for n, _, _ in self.launches])
        self.w.tick()
        self.assertEqual('active', self.member(1)['state'])
        self.assertNotIn('launchBackoff', self.store.load())
        self.assertNotIn('launchPaused', self.store.load())
        self.assertIn('queue resumed: launches succeeding', [call.args[0] for call in self.w.notify.call_args_list])

    def test_persisted_backoff_blocks_new_worker(self):
        self.w.tick(); self.w.tick()
        fresh = self.worker()
        fresh.spawn = self.spawn
        fresh.notify, fresh.status = Mock(), Mock()
        fresh.tick()
        self.assertEqual([1], [n for n, _, _ in self.launches])

    def test_deferred_member_is_the_probe_even_after_rank_changes(self):
        self.w.tick(); self.w.tick()
        with self.store.transaction() as data:
            data['members'][1]['priority'] = 'P0'
        self.now += 60
        self.w.tick()
        self.assertEqual([1, 1], [n for n, _, _ in self.launches])

    def test_intervals_cap_at_fifteen_minutes_and_pause_announces_once(self):
        self.failures = 6
        self.w.tick()
        for index, delay in enumerate((60, 120, 240, 480, 900, 900)):
            self.w.tick()
            self.assertEqual(self.now + delay, self.store.load()['launchBackoff']['until'])
            if index == 5:
                fresh = self.worker()
                fresh.notify, fresh.status = Mock(), Mock()
                fresh.tick()
                fresh.notify.assert_called_once()
                self.assertIn('queue paused: launches failing:', fresh.notify.call_args.args[0])
            self.now += delay - 1
            self.w.tick()
            self.assertEqual('pending', self.member(1)['state'])
            self.now += 1
            self.w.tick()
        self.assertEqual(1, sum('queue paused: launches failing:' in c.args[0]
                                for c in self.w.notify.call_args_list))

    def test_non_infrastructure_failure_does_not_set_backoff(self):
        self.failures = 0
        def fail(data, m):
            job = QueueCase.spawn(self, data, m)
            with self.store.transaction() as current:
                current['members'][m['number'] - 1]['result'] = dict(result='failed', infra=False,
                    stage='clone', detail='clone failed', attempt=m['attempt'], token=m['token'])
            return job
        self.w.spawn = fail
        self.w.tick(); self.w.tick()
        self.assertEqual('failed', self.member(1)['state'])
        self.assertNotIn('launchBackoff', self.store.load())

    def test_timed_out_launcher_closes_its_recorded_session(self):
        self.failures = 0
        self.w.tick()
        member = self.member(1)
        record = Path(member['checkout']) / '.workbench/state/queue-launch.json'
        session = str(uuid.uuid4())
        q.atomic_json(record, dict(attempt=member['attempt'], token=member['token'], sessions=[session]))
        job = self.w.jobs[1]['process']
        job.poll = lambda: None
        job.wait = lambda timeout: 0
        job.kill = lambda: None
        with self.store.transaction() as data:
            data['members'][0]['result'] = None
        self.now += 601
        with patch.object(q.agw, 'tree', side_effect=[
                {'workspaces': [{'sessions': [{'id': session}]}]},
                {'workspaces': [{'sessions': []}]}]), \
             patch.object(q.agw, 'clear_restore') as clear, \
             patch.object(q.agw, 'close_session') as close, \
             patch.object(q.subprocess, 'run'), patch.object(q.shutil, 'which', return_value='gh'):
            self.w.tick()
        clear.assert_called_once_with(session)
        close.assert_called_once_with(session)
        self.assertFalse(record.exists())
        self.assertEqual('pending', self.member(1)['state'])

    def test_disk_recovery_does_not_clear_launch_pause_status(self):
        self.failures = 2
        self.w.tick(); self.w.tick()
        self.now += 60
        self.w.tick(); self.w.tick()
        self.assertIn('launchPaused', self.store.load())
        self.free = 5 * q.GIB
        self.w.tick()
        self.free = 500 * q.GIB
        self.w.tick()
        self.assertEqual('blocked', self.w.status.call_args.args[0])

    def test_orphan_older_than_ten_minutes_defers_and_closes_session(self):
        session = str(uuid.uuid4())
        token = str(uuid.uuid4())
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', attempt=1, token=token,
                                      startedAt=self.now - 700, slotReleased=False)
        record = Path(self.member(1)['checkout']) / '.workbench/state/queue-launch.json'
        q.atomic_json(record, dict(attempt=1, token=token, sessions=[session]))
        with patch.object(q.agw, 'tree', side_effect=[
                {'workspaces': [{'sessions': [{'id': session}]}]},
                {'workspaces': [{'sessions': []}]}]), \
             patch.object(q.agw, 'clear_restore'), patch.object(q.agw, 'close_session') as close:
            self.w.tick()
        close.assert_called_once_with(session)
        self.assertEqual('pending', self.member(1)['state'])
        self.assertEqual('timeout', self.member(1)['launchResult'])
        self.assertFalse(record.exists())

    def test_locked_orphan_has_a_hard_timeout_without_deleting_its_record(self):
        token = str(uuid.uuid4())
        with self.store.transaction() as data:
            data['parallel'] = 2
            data['launchBackoff'] = dict(member=1, failures=1, until=self.now + 1, reason='prior pane failure')
            data['members'][0].update(state='launching', attempt=1, token=token,
                                      startedAt=self.now - 1801, slotReleased=False)
        record = Path(self.member(1)['checkout']) / '.workbench/state/queue-launch.json'
        q.atomic_json(record, dict(attempt=1, token=token, sessions=[str(uuid.uuid4())]))
        with patch.object(q, 'file_locked', return_value=True), patch.object(self.w, 'cleanup_launch') as cleanup:
            self.w.tick()
        cleanup.assert_not_called()
        self.assertTrue(record.exists())
        self.assertEqual('failed', self.member(1)['state'])
        self.assertTrue(self.member(1)['slotReleased'])
        self.assertIn('-Retry', self.member(1)['reason'])
        self.assertIn('launchBackoff', self.store.load())
        self.now += 1
        with patch.object(q, 'file_locked', return_value=True):
            self.w.tick()
        self.assertEqual([2], [n for n, _, _ in self.launches])

    def test_backoff_admits_one_probe_with_parallel_two(self):
        with self.store.transaction() as data:
            data['parallel'] = 2
            data['launchBackoff'] = dict(member=1, failures=1, until=self.now, reason='pane too narrow')
        def running(data, m):
            job = QueueCase.spawn(self, data, m)
            with self.store.transaction() as current:
                current['members'][m['number'] - 1]['result'] = None
            job['process'].poll = lambda: None
            return job
        self.w.spawn = running
        self.w.tick()
        self.w.tick()
        self.assertEqual([1], [n for n, _, _ in self.launches])

    def test_sibling_success_does_not_clear_a_newer_backoff(self):
        with self.store.transaction() as data:
            data['parallel'] = 2
        self.failures = 1
        self.w.tick(); self.w.tick()
        self.assertEqual('active', self.member(2)['state'])
        self.assertEqual(1, self.store.load()['launchBackoff']['member'])
        self.assertEqual('pending', self.member(1)['state'])

    def test_spawn_error_is_infrastructure_and_waits(self):
        self.w.spawn = Mock(side_effect=OSError('launcher could not start'))
        self.w.tick(); self.w.tick()
        self.assertEqual('pending', self.member(1)['state'])
        self.assertEqual([], self.launches)
        self.assertEqual('launcher', self.member(1)['reason'].split(':')[1].strip())

    def test_exit_without_result_cleans_up_and_backs_off(self):
        self.failures = 0
        self.w.tick()
        with self.store.transaction() as data:
            data['members'][0]['result'] = None
        self.w.jobs[1]['process'].poll = lambda: 7
        with patch.object(self.w, 'cleanup_launch') as cleanup:
            self.w.tick()
        cleanup.assert_called_once_with(self.member(1)['checkout'], self.member(1)['token'])
        self.assertEqual('pending', self.member(1)['state'])
        self.assertEqual('launcher', self.member(1)['reason'].split(':')[1].strip())

    def test_backoff_uses_next_pending_member_when_original_is_gone(self):
        with self.store.transaction() as data:
            data['launchBackoff'] = dict(member=1, failures=2, until=self.now, reason='pane too narrow')
            data['launchPaused'] = 'launches failing: pane too narrow'
            data['members'][0].update(state='failed', slotReleased=True)
        self.failures = 0
        self.w.tick()
        self.assertEqual([2], [n for n, _, _ in self.launches])
        self.w.tick()
        self.assertNotIn('launchBackoff', self.store.load())
        self.assertNotIn('launchPaused', self.store.load())
        self.assertIn('queue resumed: launches succeeding', [c.args[0] for c in self.w.notify.call_args_list])

    def test_deterministic_spawn_value_error_fails_member(self):
        self.w.spawn = Mock(side_effect=q.QueueError('PowerShell is not installed'))
        self.w.tick(); self.w.tick()
        self.assertEqual('failed', self.member(1)['state'])
        self.assertNotIn('launchBackoff', self.store.load())

    def test_late_member_result_command_returns_nonzero(self):
        result_path = self.root / 'late-result.json'
        q.atomic_json(result_path, dict(result='ok'))
        self.w.tick()
        with self.store.transaction() as data:
            data['members'][0]['result'] = None
        code = q.main(['member-result', '--file', str(self.store.path), '--number', '1',
                       '--attempt', '1', '--token', str(uuid.uuid4()), '--result-file', str(result_path)])
        self.assertEqual(3, code)
        self.assertEqual('launching', self.member(1)['state'])
        self.assertIsNone(self.member(1)['result'])

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
            self.now += 20 + q.SESSION_GRACE            # these members' sessions are gone (#61 ceiling)
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

    def test_the_kimi_queue_spec_admits_only_kimi_labelled_p2_p3(self):
        # #77: a regression pin (labelquery already parses it), not a fails-without test.
        self.pages = [[listed(1, '2026-01-01', 'kimi', 'priority:P2'), listed(2, '2026-01-02', 'priority:P2'),
                       listed(3, '2026-01-03', 'kimi', 'priority:P1'), listed(4, '2026-01-04', 'Kimi', 'priority:P3'),
                       listed(5, '2026-01-05', 'kimi')]]
        _, numbers, _ = q.resolve_spec('where: kimi AND priority IN [P2, P3]', 'o/r', self.fake_gh)
        self.assertEqual([1, 4], numbers)

    def test_prune_drops_a_pending_member_triage_took_kimi_from(self):
        # #77: -Watch adds members that gain the label; -Watch -Prune drops pending ones that lost it.
        self.pages = [[listed(1, '2026-01-01', 'kimi', 'priority:P2'), listed(2, '2026-01-02', 'kimi', 'priority:P3')]]
        self.query('where: kimi AND priority IN [P2, P3]', repo='o/r', watch=True)
        self.assertEqual([1, 2], [m['number'] for m in self.store.load()['members']])
        self.pages = [[listed(1, '2026-01-01', 'priority:P2'), listed(2, '2026-01-02', 'kimi', 'priority:P3')]]
        self.query('where: kimi AND priority IN [P2, P3]', repo='o/r', watch=True, prune=True)
        self.assertEqual([2], [m['number'] for m in self.store.load()['members']])

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
        self.query('where: bug AND priority IN [P1]', repo='o/r', watch=True)
        self.assertEqual('(bug AND priority IN [P1])', self.store.load()['query'])

    def test_a_label_watch_switches_to_a_query(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [], 'work')):
            self.start('label:work', watch=True)
        self.query('where: work', repo='o/r', watch=True)
        self.assertEqual(None, self.store.load()['label'])
        self.assertEqual('work', self.store.load()['query'])

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

    def test_rescan_uses_the_changed_query(self):
        self.query('where: priority IN [P1]', repo='o/r', watch=True)
        self.query('where: priority IN [P0]', repo='o/r', watch=True)
        worker = q.Worker(self.store, self.store.load()['owner']['token'], clock=lambda: self.now,
                          gh=lambda *args: [] if args[:2] == ('issue', 'list') else self.fake_gh(*args),
                          spawn=QueueCase.spawn.__get__(self))
        self.pages[1].extend([listed(12, '2026-02-01', 'priority:P0'),
                              listed(13, '2026-02-02', 'priority:P1')])
        self.now += 400
        worker.refresh_remote()
        members = [m['number'] for m in self.store.load()['members']]
        self.assertIn(12, members)
        self.assertNotIn(13, members)

    def test_stale_rescan_cannot_readd_pruned_members_after_watch_switch(self):
        with patch.object(q, 'resolve_spec', return_value=('o/r', [1], 'old')):
            self.start('label:old', watch=True)
        worker = q.Worker(self.store, self.store.load()['owner']['token'], clock=lambda: self.now,
                          spawn=QueueCase.spawn.__get__(self))

        def gh(*args):
            # The saved watch changes while the old label scan is in flight.
            with self.store.transaction() as data:
                data['label'] = 'new'
                data['members'] = [m for m in data['members'] if m['number'] != 1]
            return [[listed(1, '2026-01-01', 'old'), listed(9, '2026-01-09', 'old')]]

        worker.gh = gh
        worker.refresh_remote()
        self.assertEqual([], self.store.load()['members'])
        self.assertEqual('new', self.store.load()['label'])

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


class IdleNotice(unittest.TestCase):
    """#82: a watching queue with no pending or launching member says once that nothing is left, and says
    it again only after a member came and went; never after a failed scan, never for an unwatched queue."""
    terminal, start, spawn, worker, member, report = (QueueCase.terminal, QueueCase.start, QueueCase.spawn,
                                                      QueueCase.worker, QueueCase.member, QueueCase.report)
    SPEC = 'where: kimi AND priority IN [P2, P3]'

    def setUp(self):
        QueueCase.setUp(self)
        self.pages = [[listed(1, '2026-01-01', 'kimi', 'priority:P2')]]
        self.scan_error = None

    def gh(self, *args):
        if args[:2] == ('repo', 'view'):
            return {'nameWithOwner': 'o/r'}
        if args[0] == 'api' and args[1].startswith('repos/o/r/issues?state=open'):
            if self.scan_error:
                raise q.QueueError(self.scan_error)
            return self.pages
        return QueueCase.gh(self, *args)

    def idle_lines(self):
        return [line for line in sys.stdout.getvalue().splitlines() if 'idle:' in line]

    def rescan(self, worker):
        self.now += 300                                   # the watch rescans every 300 s
        worker.tick()

    def test_a_watching_queue_says_once_when_it_is_idle(self):
        self.start(self.SPEC, repo='o/r', gh=self.gh, watch=True)
        worker = self.worker()
        worker.tick()                                     # #1 launching: work left
        self.assertEqual([], self.idle_lines())
        worker.tick()                                     # #1 active: nothing left to start
        worker.tick()
        self.rescan(worker)
        self.assertEqual([f'idle: no issues left for {q.watched_spec(self.store.load())}'], self.idle_lines())
        self.assertIn('kimi AND priority IN [P2, P3]', self.idle_lines()[0])
        self.pages = [[listed(1, '2026-01-01', 'kimi', 'priority:P2'), listed(2, '2026-01-02', 'kimi', 'priority:P3')]]
        self.report(1)                                    # #1's PR frees the slot
        self.rescan(worker)                               # #2 found and launched
        self.assertEqual('launching', self.member(2)['state'])
        self.assertEqual(1, len(self.idle_lines()))
        worker.tick()                                     # #2 active: dry again
        self.assertEqual(2, len(self.idle_lines()))
        worker.notify = Mock()
        worker.tick()
        worker.notify.assert_not_called()

    def test_an_unwatched_queue_never_says_idle(self):
        self.start('o/r#1')
        worker = self.worker()
        for _ in range(4):
            worker.tick()
        self.assertEqual('active', self.member(1)['state'])
        self.assertEqual([], self.idle_lines())

    def test_a_failed_scan_is_not_idle(self):
        self.start(self.SPEC, repo='o/r', gh=self.gh, watch=True)
        with self.store.transaction() as data:
            data['members'] = []                          # nothing queued, and the next scan fails
        self.scan_error = 'HTTP 502'
        worker = self.worker()
        worker.tick()
        self.assertIn('label scan', worker.errors)
        self.assertEqual([], self.idle_lines())
        self.pages = [[]]
        self.scan_error = None
        self.rescan(worker)                               # the scan works: the queue really is empty
        self.assertEqual(1, len(self.idle_lines()))

    def test_a_failed_rescan_does_not_repeat_the_idle_line(self):
        # #82 r1: announced, then a passing 502, then a working scan: still one line, not one per error.
        self.start(self.SPEC, repo='o/r', gh=self.gh, watch=True)
        with self.store.transaction() as data:
            data['members'] = []
        self.pages = [[]]
        worker = self.worker()
        worker.tick()
        self.assertEqual(1, len(self.idle_lines()))
        self.scan_error = 'HTTP 502'
        self.rescan(worker)
        self.assertIn('label scan', worker.errors)
        self.scan_error = None
        self.rescan(worker)
        self.assertNotIn('label scan', worker.errors)
        self.assertEqual(1, len(self.idle_lines()))


class Specs(unittest.TestCase):
    def test_lists_and_repositories(self):
        self.assertEqual(('o/r', [3, 4], None), q.resolve_spec('o/r#3,#4,3'))
        self.assertEqual(('o/r', [3, 4], None), q.resolve_spec('3,4', 'o/r'))
        self.assertEqual(('o/r', [3], None), q.resolve_spec('3', gh=None, origin=lambda: 'O/r'))
        for spec in ('', '0', '-1', 'o/r#1,x/y#2', '1,', 'label:'):
            with self.subTest(spec=spec), self.assertRaises(q.QueueError):
                q.resolve_spec(spec, 'o/r')

    def test_without_a_repo_the_spec_takes_the_cwds_origin_not_gh(self):
        # #71: `gh repo view` answers a fork's checkout with the parent.
        def gh(*args):
            self.assertNotEqual(('repo', 'view'), args[:2])
            return [[{'number': 4, 'created_at': 'a'}]]
        folder = Path(__file__).resolve().parent.parent / ('test conductor fork ' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder)
        subprocess.run(['git', 'init', '-q', str(folder)], check=True)
        subprocess.run(['git', '-C', str(folder), 'remote', 'add', 'origin', 'git@github.com:Fork/R.git'], check=True)
        here = os.getcwd()
        os.chdir(folder)
        self.addCleanup(os.chdir, here)
        self.assertEqual('Fork/R', q.cwd_repo())
        for spec, expected in (('4', ('fork/r', [4], None)), ('label:bug', ('fork/r', [4], 'bug'))):
            with self.subTest(spec=spec):
                self.assertEqual(expected, q.resolve_spec(spec, None, gh))
        subprocess.run(['git', 'remote', 'remove', 'origin'], check=True)
        with self.assertRaises(q.UsageError):
            q.resolve_spec('4', None, gh)

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


class NamedQueues(unittest.TestCase):
    """#66: a repo runs more than one queue. A named queue has its own file, workspace, checkouts and
    settings; an issue one queue of the repo holds is skipped by every other; named queues do not triage."""
    terminal, gh, spawn, worker, member = (QueueCase.terminal, QueueCase.gh, QueueCase.spawn,
                                           QueueCase.worker, QueueCase.member)
    fake_in_hand, fake_gh, start_bugs = QueueBugs.fake_in_hand, QueueBugs.fake_gh, QueueBugs.start_bugs

    def setUp(self):
        QueueBugs.setUp(self)
        self.queues = self.root / 'queues'
        self.kimi = q.Store(self.queues / 'o/r.kimi.json')

    def start(self, spec='o/r#1,2,3', **kwargs):
        with patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            return QueueCase.start(self, spec, **kwargs)

    def numbers(self, store):
        return [m['number'] for m in store.load()['members']]

    def output(self):
        return sys.stdout.getvalue()

    def test_a_named_queue_has_its_own_file_workspace_checkouts_and_conductor(self):
        self.start('o/r#1', name='KIMI', implementer='kimi')
        data = self.kimi.load()
        self.assertEqual(('kimi', 'r-kimi', 'kimi'), (data['name'], data['workspace'], data['implementer']))
        self.assertFalse(self.store.path.exists())
        self.assertEqual(str(self.root / 'clones' / 'r-kimi-issue-1'), data['members'][0]['checkout'])
        new = [kwargs['args'] for command, kwargs in self.requests if command == 'session.new']
        self.assertEqual([('#queue o/r (kimi)', 'r-kimi', True)],
                         [(a['name'], a['workspace-name'], a['create-workspace']) for a in new])
        self.assertEqual(['session', 'new', '--name', '#queue o/r (kimi)', '--workspace-name', 'r-kimi',
                          '--create-workspace'],
                         q.agw._cli_args(dict(cmd='session.new', args={k: new[0][k] for k in
                                                                        ('name', 'workspace-name', 'create-workspace')})))
        self.start('o/r#2')                                       # the main queue: unchanged
        main = self.store.load()
        self.assertNotIn('name', main)
        self.assertNotIn('workspace', main)
        self.assertNotIn('workspace-name', [kwargs['args'] for c, kwargs in self.requests if c == 'session.new'][-1])
        self.assertEqual(str(self.root / 'clones' / 'r-issue-2'), main['members'][0]['checkout'])
        self.assertIn('Queue o/r (kimi) — snapshot', q.summary(data))

    def test_member_context_carries_the_workspace_and_queue_name(self):
        self.start('o/r#1', name='kimi', workspace='kimi-lab')
        token = str(uuid.uuid4())
        with self.kimi.transaction() as data:
            data['members'][0].update(state='launching', attempt=1, token=token)
        context = q.member_context(self.kimi.path, 1, 1, token)
        self.assertEqual(('kimi', 'kimi-lab'), (context['queueName'], context['workspace']))
        self.start('o/r#2')
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', attempt=1, token=token)
        context = q.member_context(self.store.path, 2, 1, token)
        self.assertEqual((None, 'r'), (context['queueName'], context['workspace']))

    def test_names_and_workspaces_are_validated_before_anything_is_written(self):
        for bad in ('main', 'MAIN', '-x', 'a.b', 'a_b', 'x' * 33, 'k m'):
            with self.subTest(name=bad), self.assertRaises(q.UsageError):
                self.start('o/r#1', name=bad)
        with self.assertRaises(q.UsageError):
            self.start('o/r#1', workspace='r-kimi')                 # -Workspace without -QueueName
        for bad in ('r', 'R', 'a b', '../x'):
            with self.subTest(workspace=bad), self.assertRaises(q.UsageError):
                self.start('o/r#1', name='kimi', workspace=bad)
        with self.assertRaises(q.UsageError) as refused:
            self.start('o/r#1', name='kimi', triage_on=True)
        self.assertIn('-Triage -Repo <owner/name> -Watch', str(refused.exception))
        self.assertEqual([], list(self.queues.glob('o/*.json')) if self.queues.exists() else [])
        self.start('o/r#1', name='kimi')
        with self.assertRaises(q.UsageError):
            self.start('o/r#2', name='kimi', workspace='elsewhere')  # its members already live in r-kimi
        self.start('o/r#2', name='kimi', workspace='R-KIMI')         # the same one, in another case
        with self.assertRaises(q.UsageError):
            self.start('o/r#3', name='gpt', workspace='r-kimi')      # another queue's workspace
        self.assertFalse(q.queue_path(self.queues, 'o/r', 'gpt').exists())
        self.assertEqual([1, 2], self.numbers(self.kimi))

    def test_a_file_must_be_the_one_its_repo_and_name_give(self):
        self.start('o/r#1', name='kimi')
        data = json.loads(self.kimi.path.read_text())
        for change in ({'name': None}, {'name': 'gpt'}, {'workspace': None}, {'triage': True},
                       {'revmuxProfile': 'a b'}, {'name': 'main'}):
            with self.subTest(change=change):
                self.kimi.path.write_text(json.dumps(dict(data, **change)))
                with self.assertRaises(q.StateError):
                    self.kimi.load()
        self.kimi.path.write_text(json.dumps(data))
        main = json.loads(self.kimi.path.read_text())
        main.pop('name'), main.pop('workspace')
        q.atomic_json(self.queues / 'o/r.json', dict(main, workspace='r'))   # a main queue has no workspace
        with self.assertRaises(q.StateError):
            self.store.load()

    def test_a_dotted_repos_main_queue_is_not_a_named_queue(self):
        # `o/r -QueueName kimi` is r.kimi.json, which can be repo o/r.kimi's main queue.
        dotted = dict(version=1, repo='o/r.kimi', parallel=1, watch=False, label=None, yes=False,
                      config=str(self.config), members=[q.new_member(9, 'o/r.kimi', self.root)], owner=None)
        q.atomic_json(self.kimi.path, dotted)
        before = self.kimi.path.read_text()
        self.assertEqual('o/r.kimi', self.kimi.load()['repo'])
        with self.assertRaises(q.QueueError) as refused:
            self.start('o/r#1', name='kimi')
        self.assertIn('repository mismatch', str(refused.exception))
        self.assertEqual(before, self.kimi.path.read_text())
        self.start('o/r#9')                    # not a sibling of o/r: its #9 claims nothing here
        self.assertEqual([9], self.numbers(self.store))

    # --- exclusive claims -------------------------------------------------------------------

    def test_explicit_lists_never_admit_one_issue_twice_in_either_order(self):
        for first, second in ((None, 'kimi'), ('kimi', None)):
            with self.subTest(first=first or 'main'):
                for path in self.queues.glob('o/*.json'):
                    path.unlink()
                self.start('o/r#1,2', name=first)
                self.start('o/r#2,3', name=second)
                one = q.Store(q.queue_path(self.queues, 'o/r', first))
                two = q.Store(q.queue_path(self.queues, 'o/r', second))
                self.assertEqual(([1, 2], [3]), (self.numbers(one), self.numbers(two)))
                tag = f'({second}) ' if second else ''
                self.assertIn(f'{tag}#2 skipped: claimed by queue {first or "main"}', self.output())

    def test_label_specs_disjoint_and_overlapping(self):
        self.start_bugs('o/r#1,2')
        self.start_bugs('bugs', name='kimi')          # the fake bug label lists 1..5
        self.assertEqual([3, 4, 5], self.numbers(self.kimi))
        self.start_bugs('bugs')                       # and back: the main queue skips the kimi queue's
        self.assertEqual([1, 2], self.numbers(self.store))
        out = self.output()
        self.assertIn('(kimi) #1 skipped: claimed by queue main', out)
        self.assertIn('#3 skipped: claimed by queue kimi', out)

    def test_only_merged_and_closed_members_release_their_issue(self):
        self.start('o/r#1,2,3,4,5,6')
        with self.store.transaction() as data:
            for m, state in zip(data['members'], ('merged', 'closed', 'failed', 'pr-open', 'closed', 'closed')):
                m['state'] = state
            # r1 m3: a closed member whose close is still pending or stuck can be revived by its loop.
            data['members'][4]['closePending'] = True
            data['members'][5]['closeStuck'] = 'the relay is alive but its close has been pending'
        self.start('o/r#1,2,3,4,5,6', name='kimi')
        self.assertEqual([1, 2], self.numbers(self.kimi))

    def test_a_closed_member_whose_session_is_open_still_claims(self):
        # r2 m2: a reopened no-PR loop (or one left open with autonomy off) has no close flag once the
        # relay gives up its close; its open issue session keeps the claim, for an explicit list too.
        self.start('o/r#1,2', name='kimi')
        with self.kimi.transaction() as data:
            for m in data['members']:
                m['state'] = 'closed'
        self.tree = {'workspaces': [{'name': 'r-kimi', 'sessions': [{'id': 's1', 'name': '#1 fix'},
                                                                    {'id': 's2', 'name': '#2 relay'}]},
                                    {'name': 'r', 'sessions': [{'id': 's3', 'name': '#2 fix'}]}]}
        self.start('o/r#1,2')                        # #2's issue session is in r, not in the kimi workspace
        self.assertEqual([2], self.numbers(self.store))
        self.assertIn('#1 skipped: claimed by queue kimi', self.output())
        with patch.object(q.agw, 'tree', side_effect=q.agw.CtlError('agwinterm is not running')):
            with self.assertRaises(q.agw.CtlError):       # an unread terminal is never "unclaimed"
                q.start_queue('o/r#1', root=self.queues, name='gpt')
        self.assertFalse(q.queue_path(self.queues, 'o/r', 'gpt').exists())

    def test_the_rescan_keeps_a_closed_member_with_an_open_session_claimed(self):
        self.start('o/r#1', name='kimi')
        with self.kimi.transaction() as data:
            data['members'][0]['state'] = 'closed'
        self.start_bugs('o/r#9', watch=False)
        self.start_bugs('bugs', watch=True)
        with self.store.transaction() as data:
            data['members'] = []
        self.tree = {'workspaces': [{'name': 'r-kimi', 'sessions': [{'id': 's1', 'name': '#1 fix'}]}]}
        worker = q.Worker(self.store, self.store.load()['owner']['token'], gh=self.fake_gh, clock=lambda: self.now)
        with patch.object(q, 'gh_json', self.fake_gh), patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            worker.refresh_remote()
        self.assertEqual('claimed by queue kimi', worker.last_skips[1])
        self.assertNotIn(1, self.numbers(self.store))

    def test_a_claim_wins_over_an_in_hand_reason_at_start_and_on_a_rescan(self):
        # r1 m4: the main queue's member's own checkout is not something the kimi queue should tell
        # the human to resume or delete.
        self.start_bugs('o/r#4')
        q.atomic_json(self.root / 'clones/r-issue-4/.workbench/state/queue-member.json',
                      {'queue': str(self.store.path), 'repo': 'o/r', 'number': 4})
        self.start_bugs('bugs', name='kimi', watch=True)
        out = self.output()
        self.assertIn('(kimi) #4 skipped: claimed by queue main', out)
        self.assertNotIn('#4 skipped: checkout exists', out)
        with self.kimi.transaction() as data:
            data['members'] = []
        worker = q.Worker(self.kimi, self.kimi.load()['owner']['token'], gh=self.fake_gh, clock=lambda: self.now)
        with patch.object(q, 'gh_json', self.fake_gh), patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            worker.refresh_remote()
        self.assertEqual('claimed by queue main', worker.last_skips[4])

    def test_a_named_checkout_without_this_queues_membership_is_to_be_deleted(self):
        # r2 m3: the skip fires exactly when no membership makes the checkout resumable.
        (self.root / 'clones/r-kimi-issue-3/.workbench/state').mkdir(parents=True)
        self.start_bugs('bugs', name='kimi')
        self.assertIn('(kimi) #3 skipped: checkout exists without a queue membership this queue can use', self.output())
        self.assertIn('delete it (a member of this queue is relaunched with github-workbench -Queue <spec> '
                      '-QueueName kimi -Retry)', self.output())
        (self.root / 'clones/r-issue-3/.workbench/state').mkdir(parents=True)
        self.start_bugs('bugs')                       # kimi claims 1, 2, 4, 5; #3 is the main queue's to judge
        self.assertIn('resume with github-workbench o/r#3 or delete it', self.output())

    def test_a_default_workspace_too_long_to_load_is_refused_before_writing(self):
        # r1 m5: `<repo>-<name>` can exceed a workspace name's 64 characters.
        repo = 'o/' + 'r' * 40
        with self.assertRaises(q.UsageError) as refused:
            self.start(f'{repo}#1', name='k' * 30)
        self.assertIn('pass -Workspace', str(refused.exception))
        self.assertEqual([], list((self.queues / 'o').glob('*.json')) if (self.queues / 'o').exists() else [])
        self.start(f'{repo}#1', name='k' * 30, workspace='long-lab')
        self.assertEqual('long-lab', q.Store(q.queue_path(self.queues, repo, 'k' * 30)).load()['workspace'])

    def test_the_second_add_waits_for_the_claims_lock_and_sees_the_first(self):
        # The main queue's add holds the claims lock; the kimi queue's add of the same issue waits
        # for it and then sees the main queue's member.
        self.start('o/r#9')
        held = q.claims_lock(self.queues, 'o/r').acquire()
        done = threading.Event()
        errors = []

        def second():
            try:
                with patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
                    q.start_queue('o/r#1', root=self.queues, name='kimi')
            except BaseException as err:        # noqa: BLE001 - reported below
                errors.append(err)
            done.set()

        thread = threading.Thread(target=second)
        thread.start()
        self.assertFalse(done.wait(0.5))      # blocked on the claims lock
        with self.store.transaction() as data:
            data['members'].append(q.new_member(1, 'o/r', self.root / 'clones'))
        held.release()
        thread.join(30)
        self.assertEqual([], errors)
        self.assertEqual([9, 1], self.numbers(self.store))
        self.assertEqual([], self.numbers(self.kimi))

    def test_an_unreadable_sibling_is_an_error_and_an_unrelated_repo_is_never_read(self):
        self.start('o/r#1', name='kimi')
        (self.queues / 'o/other.json').write_text('{broken')
        (self.queues / 'o/rr.json').write_text('{broken')
        self.start('o/r#2')                             # neither is a queue of o/r
        self.kimi.path.write_text('{broken')
        with self.assertRaises(q.StateError):
            self.start('o/r#3')
        self.assertEqual([2], self.numbers(self.store))

    def test_the_watch_rescan_skips_another_queues_claims(self):
        self.start_bugs('o/r#2')
        self.start_bugs('bugs', name='kimi', watch=True)
        with self.kimi.transaction() as data:
            data['members'] = []
        worker = q.Worker(self.kimi, self.kimi.load()['owner']['token'], gh=self.fake_gh, clock=lambda: self.now)
        with patch.object(q, 'gh_json', self.fake_gh), patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            worker.refresh_remote()
        self.assertEqual([1, 3, 4, 5], self.numbers(self.kimi))
        self.assertIn('(kimi) #2 skipped: claimed by queue main', self.output())
        self.kimi.path.with_name('r.json').write_text('{broken')        # the main queue: unreadable
        with self.kimi.transaction() as data:
            data['members'] = []
        worker.next_scan = 0
        with patch.object(q, 'gh_json', self.fake_gh), patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            worker.refresh_remote()
        self.assertEqual([], self.numbers(self.kimi))
        self.assertIn('label scan', worker.errors)

    # --- the in-hand checks see the other queues' footprints --------------------------------

    def test_in_hand_sees_sessions_in_every_queue_workspace_and_the_plain_checkout(self):
        self.start('o/r#9', name='kimi')
        self.tree = {'workspaces': [{'name': 'R-Kimi', 'sessions': [{'id': 's1', 'name': '#1 fix'}]},
                                    {'name': 'r', 'sessions': [{'id': 's2', 'name': '#2 fix'}]},
                                    {'name': 'other', 'sessions': [{'id': 's3', 'name': '#3 fix'}]}]}
        foreign = self.root / 'clones' / 'r-issue-4' / '.workbench' / 'state'
        foreign.mkdir(parents=True)
        self.start_bugs('bugs', name='kimi')
        self.assertEqual([9, 3, 5], self.numbers(self.kimi))
        out = self.output()
        self.assertIn('(kimi) #1 skipped: session', out)
        self.assertIn('(kimi) #2 skipped: session', out)
        self.assertIn('(kimi) #4 skipped: checkout exists from an earlier loop', out)
        self.start_bugs('bugs')                        # the main queue sees the kimi workspace's #1 too
        self.assertEqual([], self.numbers(self.store))
        out = self.output().splitlines()
        self.assertIn('#1 skipped: session: a live workbench session is open for it', out)
        self.assertIn('#3 skipped: claimed by queue kimi', out)

    def test_a_named_worker_counts_live_sessions_in_its_own_workspace_only(self):
        self.start('o/r#1,2', name='kimi')
        worker = q.Worker(self.kimi, self.kimi.load()['owner']['token'], gh=self.gh, clock=lambda: self.now)
        self.tree = {'workspaces': [{'name': 'r-kimi', 'sessions': [{'id': 's1', 'name': '#1 fix'}]},
                                    {'name': 'r', 'sessions': [{'id': 's2', 'name': '#2 fix'}]}]}
        (self.queues / 'o/r.json').write_text('{broken')          # a broken sibling changes nothing here
        with patch.object(q.agw, 'tree', side_effect=lambda: self.tree):
            self.assertEqual({1}, worker.live_numbers(self.kimi.load()))

    def test_status_lines_and_titles_name_the_queue(self):
        self.start('o/r#1', name='kimi')
        worker = q.Worker(self.kimi, self.kimi.load()['owner']['token'], gh=self.gh, clock=lambda: self.now)
        worker.notify('queue paused: x')
        q.agw.notify.assert_called_with(unittest.mock.ANY, 'queue paused: x', title='Workbench queue (kimi)')
        self.start('o/r#2')
        worker = q.Worker(self.store, self.store.load()['owner']['token'], gh=self.gh, clock=lambda: self.now)
        worker.notify('queue paused: y')
        q.agw.notify.assert_called_with(unittest.mock.ANY, 'queue paused: y', title='Workbench queue')

    def test_dry_run_reports_the_name_workspace_and_claims(self):
        self.start('o/r#1')
        self.start('o/r#1,2', name='kimi', dry_run=True)
        result = json.loads(self.output().strip().splitlines()[-1])
        self.assertEqual(('kimi', 'r-kimi', [2]), (result['name'], result['workspace'], result['members']))
        self.assertEqual([{'number': 1, 'reason': 'claimed by queue main'}], result['skipped'])
        self.assertFalse(self.kimi.path.exists())

    # --- the queue's revmux profile ---------------------------------------------------------

    def launched_args(self, store):
        data = store.load()
        m = dict(q.find_member(data, 1), token=str(uuid.uuid4()))
        worker = q.Worker(store, data['owner']['token'], gh=self.gh, clock=lambda: self.now)
        # The arguments are the subject, not the machine: a PowerShell is found whether or not one
        # is installed (macOS has none by default, #60).
        real_which = shutil.which
        fake_which = lambda name, *a, **k: '/usr/bin/pwsh' if name in ('pwsh', 'powershell.exe') else real_which(name, *a, **k)
        with patch.object(q.subprocess, 'Popen') as popen, patch.object(q.shutil, 'which', side_effect=fake_which):
            job = worker.spawn_launcher(data, m)
        job['stream'].close()
        return popen.call_args.args[0]

    def test_an_explicit_profile_is_saved_and_passed_and_the_default_never_is(self):
        self.start('o/r#1', name='kimi', implementer='kimi')
        self.assertNotIn('revmuxProfile', self.kimi.load())
        self.assertNotIn('-RevmuxProfile', self.launched_args(self.kimi))
        self.start('o/r#2', name='kimi', revmux_profile='kimi-only')
        self.assertEqual('kimi-only', self.kimi.load()['revmuxProfile'])
        args = self.launched_args(self.kimi)
        self.assertEqual(['-RevmuxProfile', 'kimi-only'], args[args.index('-RevmuxProfile'):][:2])
        self.assertIn('settings: revmuxProfile null -> "kimi-only"', self.output())
        with self.assertRaises(q.UsageError):
            self.start('o/r#3', name='kimi', revmux_profile='a b')


STAMPED = r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d '


class Timestamps(unittest.TestCase):
    """#78: the `#queue` pane (`run`) stamps every line; what is parsed or written to a file does not."""
    terminal, gh = QueueCase.terminal, QueueCase.gh

    def setUp(self):
        QueueCase.setUp(self)
        self.install = self.enterContext(patch.object(q.tslog, 'install', wraps=tslog.install))
        self.real_start = q.start_queue
        self.enterContext(patch.object(q, 'start_queue', self.start))    # main() has no root argument

    def start(self, spec, *args, **kwargs):
        return self.real_start(spec, *args, **dict(kwargs, root=self.root / 'queues'))

    def launching(self, number):
        self.start(f'o/r#{number}')
        sys.stdout.seek(0)
        sys.stdout.truncate()
        token = str(uuid.uuid4())
        with self.store.transaction() as data:
            data['members'][0].update(state='launching', attempt=1, token=token)
        return token

    def test_run_stamps_every_line_of_the_pane(self):
        class Worker:
            def __init__(self, store, token):
                pass

            def run(self):
                print('queue paused: low memory: 2.9 GB free < 3 GB', flush=True)
                print('queue: boom', file=sys.stderr, flush=True)
                print('Queue o/r — snapshot\n\n| Issue |', flush=True)
                return 0

        err = io.StringIO()
        with patch.object(q, 'Worker', Worker), contextlib.redirect_stderr(err):
            self.assertEqual(0, q.main(['run', '--file', str(self.store.path), '--token', 'T']))
        lines = sys.stdout.getvalue().splitlines()
        self.assertRegex(lines[0], STAMPED + r'queue paused: low memory: 2\.9 GB free < 3 GB$')
        self.assertEqual(4, len(lines))
        for line in lines[1:]:
            self.assertRegex(line, r'^\d\d:\d\d:\d\d ')
        self.assertRegex(err.getvalue(), r'^\d\d:\d\d:\d\d queue: boom\n$')     # one date state for both

    def test_dry_run_json_is_not_stamped(self):
        self.assertEqual(0, q.main(['start', '--spec', 'o/r#1', '--dry-run']))
        self.install.assert_not_called()
        self.assertEqual([1], json.loads(sys.stdout.getvalue())['members'])

    def test_member_context_is_exactly_its_json(self):
        token = self.launching(1)
        self.assertEqual(0, q.main(['member-context', '--file', str(self.store.path), '--number', '1',
                                    '--attempt', '1', '--token', token]))
        self.install.assert_not_called()
        self.assertTrue(sys.stdout.getvalue().startswith('{'))
        self.assertEqual(1, json.loads(sys.stdout.getvalue())['number'])

    def test_mark_is_not_stamped(self):
        self.launching(1)
        with self.store.transaction() as data:
            data['members'][0]['state'] = 'active'
        self.assertEqual(0, q.main(['mark', '--file', str(self.store.path), '--number', '1',
                                    '--pr', 'https://github.com/o/r/pull/9']))
        self.install.assert_not_called()
        self.assertEqual('marked #1 PR https://github.com/o/r/pull/9\n', sys.stdout.getvalue())


class AutoRouting(unittest.TestCase):
    """#109: `-Implementer auto` is a saved queue setting; the conductor asks the router once per member."""
    terminal, start, gh, member, report = (QueueCase.terminal, QueueCase.start, QueueCase.gh, QueueCase.member, QueueCase.report)
    ANSWER = dict(implementer='codex-sol', tool='codex', model='gpt-6.1-sol', reason='workhorse', rule='default-code',
                  source='router')

    def setUp(self):
        QueueCase.setUp(self)
        self.spawned, self.routed, self.answers, self.ignored = [], [], {}, []
        self.start('o/r#1,2,3', parallel=1, implementer='auto')
        self.w = self.worker()
        self.w.notify, self.w.status = Mock(), Mock()

    def worker(self):
        return q.Worker(self.store, self.store.load()['owner']['token'], gh=self.gh, clock=lambda: self.now,
                        spawn=self.spawn, route=self.route)

    def route(self, repo, number, settings, limited, ignore_label=None):
        self.routed.append((number, tuple(limited)))
        self.ignored.append(ignore_label)
        outcome = self.answers.get(number, self.ANSWER)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    def spawn(self, data, m):
        self.spawned.append((m['number'], data.get('implementer'), data.get('implementerModel'), data.get('rosterId')))
        return QueueCase.spawn(self, data, m)

    def settings(self, **extra):
        self.config.write_text(json.dumps(dict({'checkoutRoot': str(self.root / 'clones')}, **extra)))

    def limit(self, tool, member=1):
        with self.store.transaction() as data:
            data.setdefault('toolLimits', {})[tool] = {'kind': 'limited', 'member': member, 'line': f'{tool} limited', 'at': 1}

    def test_auto_is_a_saved_queue_setting_and_other_values_are_refused(self):
        self.assertEqual('auto', self.store.load()['implementer'])
        with self.assertRaises(q.UsageError):
            self.start('o/r#1', implementer='aider')
        data = json.loads(self.store.path.read_text())
        data['implementer'] = 'aider'
        self.store.path.write_text(json.dumps(data))
        with self.assertRaises(q.StateError):
            self.store.load()

    def test_each_member_is_routed_once_recorded_and_launched_with_its_tool_and_model(self):
        self.w.tick(); self.w.tick()
        self.assertEqual([(1, ())], self.routed)
        self.assertEqual([(1, 'codex', 'gpt-6.1-sol', 'codex-sol')], self.spawned)
        route = self.member(1)['route']
        self.assertEqual(('codex-sol', 'workhorse', 'default-code', 'router'),
                         (route['implementer'], route['reason'], route['rule'], route['source']))
        self.assertEqual('auto', self.store.load()['implementer'])           # the queue's setting stays auto
        self.report(1, 'pr-open')
        self.answers[2] = dict(self.ANSWER, implementer='kimi', tool='kimi', source='label')
        self.answers[2].pop('model')
        self.w.tick(); self.w.tick()
        self.assertEqual([(1, ()), (2, ())], self.routed)
        self.assertEqual((2, 'kimi', None, 'kimi'), self.spawned[-1])

    def test_a_restart_or_retry_does_not_route_again(self):
        self.w.tick(); self.w.tick()
        with self.store.transaction() as data:
            m = q.find_member(data, 1)
            m.update(state='pending', slotReleased=False)
        self.worker().tick(); self.worker().tick()
        self.assertEqual([(1, ())], self.routed)
        self.assertEqual(2, len(self.spawned))
        self.assertEqual(self.spawned[0][1:], self.spawned[1][1:])

    def test_a_routed_tool_that_has_since_been_limited_is_routed_once_more(self):
        self.w.tick(); self.w.tick()
        with self.store.transaction() as data:
            q.find_member(data, 1).update(state='pending', slotReleased=False)
        self.limit('codex')
        self.answers[1] = dict(self.ANSWER, implementer='claude-sonnet', tool='claude', model='claude-sonnet-5-5')
        self.worker().tick(); self.worker().tick()
        self.assertEqual([(1, ()), (1, ('codex',))], self.routed)
        self.assertEqual('claude-sonnet', self.member(1)['route']['implementer'])
        self.assertEqual((1, 'claude', 'claude-sonnet-5-5', 'claude-sonnet'), self.spawned[-1])

    def test_a_router_failure_defers_the_member_like_any_launch_deferral(self):
        self.answers[1] = __import__('route').RouteError('the router chose nothing usable')
        self.w.tick(); self.w.tick()
        self.assertEqual([], self.spawned)
        member = self.member(1)
        self.assertEqual('pending', member['state'])
        self.assertIn('launch deferred: route: the router chose nothing usable', member['reason'])
        self.assertNotIn('route', member)
        self.assertEqual(1, self.store.load()['launchBackoff']['failures'])
        self.assertEqual(1, self.store.load()['launchBackoff']['member'])

    def test_the_router_is_told_which_tools_are_limited(self):
        self.limit('kimi')
        self.w.tick(); self.w.tick()
        self.assertEqual([(1, ('kimi',))], self.routed)

    def test_the_launcher_gets_the_tool_the_model_and_the_roster_id(self):
        data = dict(self.store.load(), implementer='codex', implementerModel='gpt-6.1-sol', rosterId='codex-sol')
        worker = q.Worker(self.store, data['owner']['token'], gh=self.gh, clock=lambda: self.now)
        real_which = shutil.which
        with patch.object(q.subprocess, 'Popen') as popen, patch.object(
                q.shutil, 'which', side_effect=lambda n, *a, **k: '/usr/bin/pwsh' if n == 'pwsh' else real_which(n, *a, **k)):
            job = worker.spawn_launcher(data, dict(self.member(1), token=str(uuid.uuid4())))
        job['stream'].close()
        args = popen.call_args.args[0]
        self.assertEqual(['-Implementer', 'codex', '-ImplementerModel', 'gpt-6.1-sol', '-RosterId', 'codex-sol'],
                         args[args.index('-Implementer'):args.index('-Implementer') + 6])

    # tool_route (#61) with the roster (#109)

    def limits(self, *tools, **extra):
        return dict({'config': str(self.config), 'toolLimits': {
            t: {'kind': 'limited', 'member': 1, 'line': t, 'at': 1} for t in tools}}, **extra)

    def test_an_auto_queue_routes_nothing_by_tool_unless_every_roster_entry_is_limited(self):
        self.assertEqual((None, None), tool_route(self.limits('codex', implementer='auto')))
        self.assertEqual((None, None), tool_route(self.limits('codex', 'kimi', implementer='auto')))
        self.settings(implementerRoster=[{'id': 'only', 'tool': 'codex', 'note': 'x'}, {'id': 'k', 'tool': 'kimi', 'note': 'y'}])
        route, reason = tool_route(self.limits('codex', 'kimi', implementer='auto'))
        self.assertIsNone(route)
        self.assertIn('every implementerRoster entry is on a limited tool', reason)
        self.settings(implementerRoster=[{'id': 'only', 'tool': 'codex', 'note': 'x'}])
        self.assertIn('every implementerRoster entry', tool_route(self.limits('codex', implementer='auto'))[1])
        self.assertIn('planner is always Claude', tool_route(self.limits('claude', implementer='auto'))[1])

    def test_failover_order_may_name_roster_ids_and_the_chosen_entry_carries_its_model(self):
        self.settings(failoverOrder=['claude-opus', 'codex', 'kimi'])
        entry, reason = q.route_entry(self.limits('codex', implementer='codex'))
        self.assertEqual(('claude-opus', 'claude', 'claude-opus-5-5', None), (entry['id'], entry['tool'], entry['model'], reason))
        self.assertEqual(('claude', None), tool_route(self.limits('codex', implementer='codex')))

    def test_a_limited_tool_is_not_replaced_by_another_entry_of_the_same_tool(self):
        self.settings(failoverOrder=['claude-sonnet', 'claude-opus', 'codex-luna', 'kimi'])
        entry, _ = q.route_entry(self.limits('claude', implementer='claude'))
        self.assertIsNone(entry)                                   # claude limited: the planner cannot run
        entry, _ = q.route_entry(self.limits('codex', implementer='codex'))
        self.assertEqual('claude-sonnet', entry['id'])        # the order names the id itself

    def test_a_bare_tool_in_the_order_is_the_bare_tool_with_no_model_and_no_roster_id(self):
        entry, _ = q.route_entry(self.limits('codex', implementer='codex'))
        self.assertEqual(('claude', True, None), (entry['id'], entry.get('bare'), entry.get('model')))
        self.settings(failoverOrder=['codex', 'kimi'])
        entry, _ = q.route_entry(self.limits('codex', implementer='codex'))
        self.assertEqual(('kimi', None, None), (entry['id'], entry.get('bare'), entry.get('model')))   # kimi is a roster id

    def test_an_invalid_failover_order_or_roster_is_a_pause_reason(self):
        for settings in ({'failoverOrder': ['codex', 'nobody']}, {'failoverOrder': ['claude-sonnet', 'claude-opus']},
                         {'implementerRoster': []}):
            with self.subTest(settings=settings):
                self.settings(**settings)
                route, reason = tool_route(self.limits('codex', implementer='codex'))
                self.assertIsNone(route)
                self.assertRegex(reason, 'failoverOrder|implementerRoster')

    def default_worker(self):
        """A worker whose `route` is left at its default: Worker.route_issue, over a patched router."""
        return q.Worker(self.store, self.store.load()['owner']['token'], gh=self.gh, clock=lambda: self.now,
                        spawn=self.spawn)

    def test_the_default_route_asks_the_router_to_write_the_label_and_launches(self):
        with patch.object(q.router, 'route_issue', return_value=dict(self.ANSWER)) as ask:
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual(('o/r', 1), ask.call_args.args)
        self.assertEqual((True, None, []), (ask.call_args.kwargs['label'], ask.call_args.kwargs['ignore_label'],
                                            ask.call_args.kwargs['limited']))
        self.assertEqual([(1, 'codex', 'gpt-6.1-sol', 'codex-sol')], self.spawned)

    def test_the_label_is_written_for_a_router_answer(self):
        calls, patched = self.default_route_with(['priority:P2'])
        with patched:
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual([('label', 'create', 'impl:claude-sonnet')], [c[:3] for c in calls if c[0] == 'label'])
        self.assertEqual(1, len(self.spawned))

    def test_no_label_is_written_for_an_owner_label(self):
        calls, patched = self.default_route_with(['impl:codex-luna'])
        with patched:
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual([], [c for c in calls if c[0] in ('label', 'issue')])
        self.assertEqual('label', self.member(1)['route']['source'])
        self.assertEqual((1, 'codex', 'gpt-6-luna', 'codex-luna'), self.spawned[0])

    def test_a_label_that_cannot_be_written_never_stops_the_launch(self):
        with patch.object(q.router, 'route_issue', return_value=dict(self.ANSWER, warnings=['the impl: label was not written: denied'])):
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual(1, len(self.spawned))
        self.assertEqual('codex-sol', self.member(1)['route']['implementer'])

    def default_route_with(self, issue_labels):
        """Worker.route_issue over the real router.route_issue with a fake gh and a fake model: the issue
        carries `issue_labels`, the model answers claude-sonnet. Returns (gh calls, the patched context)."""
        calls = []
        real = q.router.route_issue
        issue = {'number': 1, 'title': 't', 'body': 'b', 'state': 'open', 'labels': [{'name': n} for n in issue_labels],
                 'author_association': 'OWNER'}

        def gh(*args, timeout=120):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, json.dumps(issue) if args[0] == 'api' else '', '')

        def model(argv, cwd):
            answer = dict(implementer='claude-sonnet', reason='fine', rule='default-code')
            return subprocess.CompletedProcess(argv, 0, json.dumps({'is_error': False, 'structured_output': answer}), '')
        patched = patch.object(q.router, 'route_issue', side_effect=lambda *a, **k: real(*a, gh=gh, model=model, records=[], **k))
        return calls, patched

    def route_again(self, route_record, issue_labels):
        """Route member 1 once, then make its recorded choice `route_record`, limit codex and route again."""
        calls, patched = self.default_route_with(issue_labels)
        with patched:
            w = self.default_worker()
            w.tick(); w.tick()
            with self.store.transaction() as data:
                m = q.find_member(data, 1)
                m.update(state='pending', slotReleased=False, route=route_record)
            self.limit('codex')
            calls.clear()
            w = self.default_worker()
            w.tick(); w.tick()
        return calls, w

    def test_a_re_route_after_a_limit_replaces_the_routers_own_stale_label(self):
        record = dict(implementer='codex-sol', tool='codex', model='gpt-6.1-sol', reason='r', rule='x', source='router')
        calls, _ = self.route_again(record, ['impl:codex-sol'])
        self.assertEqual('claude-sonnet', self.member(1)['route']['implementer'])
        self.assertNotIn('routeStale', self.member(1))
        edit = next(c for c in calls if c[:2] == ('issue', 'edit'))
        self.assertEqual(['--add-label', 'impl:claude-sonnet', '--remove-label', 'impl:codex-sol'], list(edit[edit.index('--add-label'):]))
        self.assertEqual('claude', self.spawned[-1][1])

    def test_a_re_route_that_fails_keeps_the_stale_label_ignored_for_the_next_attempt(self):
        record = dict(implementer='codex-sol', tool='codex', model='gpt-6.1-sol', reason='r', rule='x', source='router')
        calls, patched = self.default_route_with(['impl:codex-sol'])
        with patched:
            w = self.default_worker()
            w.tick(); w.tick()
            with self.store.transaction() as data:
                q.find_member(data, 1).update(state='pending', slotReleased=False, route=record)
            self.limit('codex')
        # a deferral (no claude on this machine): the member is pending again, its route dropped, the stale id remembered
        with patch.object(q.router, 'route_issue', side_effect=q.router.RouteError('no claude')):
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual(('codex-sol', None), (self.member(1).get('routeStale'), self.member(1).get('route')))
        self.assertIn('launch deferred: route: no claude', self.member(1)['reason'])
        with patch.object(q.router, 'route_issue', return_value=dict(self.ANSWER, implementer='claude-sonnet', tool='claude')) as ask:
            self.now += 3600
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual('codex-sol', ask.call_args.kwargs['ignore_label'])
        self.assertNotIn('routeStale', self.member(1))

    def test_a_re_route_of_an_owners_label_on_a_limited_tool_defers(self):
        record = dict(implementer='codex-sol', tool='codex', model='gpt-6.1-sol', reason='r', rule='x', source='label')
        _, w = self.route_again(record, ['impl:codex-sol'])
        self.assertNotIn('routeStale', self.member(1))
        member = self.member(1)
        self.assertEqual('pending', member['state'])
        self.assertIn('usage limit', member['reason'])

    def test_a_router_error_of_the_default_route_defers_the_member(self):
        with patch.object(q.router, 'route_issue', side_effect=q.router.RouteError('no claude')):
            w = self.default_worker()
            w.tick(); w.tick()
        self.assertEqual([], self.spawned)
        self.assertIn('launch deferred: route: no claude', self.member(1)['reason'])


class OutcomeRecording(unittest.TestCase):
    """#109: a member that merged or closed hands its loop's outcome to the router's statistics, once."""
    terminal, start, gh, spawn, worker, member = (QueueCase.terminal, QueueCase.start, QueueCase.gh, QueueCase.spawn,
                                                  QueueCase.worker, QueueCase.member)

    def setUp(self):
        QueueCase.setUp(self)
        self.start('o/r#1,2')
        self.w = self.worker()
        self.w.notify, self.w.status = Mock(), Mock()
        self.w.tick(); self.w.tick()

    def finish(self, n, state, pr=None):
        with self.store.transaction() as data:
            q.find_member(data, n).update(state=state, slotReleased=True, pr=pr, phase='pr-open' if pr else 'closed')

    def test_a_merged_member_is_recorded_once_with_its_pr_while_the_checkout_exists(self):
        self.finish(1, 'merged', 'https://github.com/o/r/pull/7')
        self.w.tick()
        self.outcomes.assert_called_once()
        args, kwargs = self.outcomes.call_args
        self.assertEqual((self.member(1)['checkout'], 'merged', 7), (str(args[0]), args[1], kwargs['pr']))
        self.assertTrue(self.member(1)['outcomeRecorded'])
        self.w.tick(); self.w.tick()
        self.outcomes.assert_called_once()

    def test_an_issue_closed_without_a_pr_is_a_closed_outcome_and_live_members_record_nothing(self):
        self.finish(1, 'closed')
        self.w.tick()
        self.assertEqual([(self.member(1)['checkout'], 'closed')], [(str(c.args[0]), c.args[1]) for c in self.outcomes.call_args_list])
        self.assertNotIn('outcomeRecorded', self.member(2))

    def test_a_checkout_that_is_already_gone_is_skipped_and_a_router_error_never_fails_the_tick(self):
        self.finish(1, 'merged', 'https://github.com/o/r/pull/7')
        shutil.rmtree(self.member(1)['checkout'])
        self.w.tick()
        self.outcomes.assert_not_called()
        self.finish(2, 'merged', 'https://github.com/o/r/pull/8')
        self.outcomes.side_effect = OSError('disk full')
        self.w.tick()
        self.assertIn('route outcomes', self.w.errors)
