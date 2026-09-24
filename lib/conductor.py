#!/usr/bin/env python3
"""Persistent issue queue. Only the conductor admits work; issue relays retain human review."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import agw

HERE = Path(__file__).resolve().parent
STATES = {'pending', 'launching', 'active', 'pr-open', 'blocked', 'failed', 'merged'}


class QueueError(ValueError):
    pass


class UsageError(QueueError):
    pass


class Lock:
    """Process-held OS lock; no PID-based liveness and no deletion of lock files."""
    def __init__(self, path: Path, timeout=10):
        self.path, self.timeout, self.stream = path, timeout, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, 'a+b', buffering=0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.stream = stream
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise QueueError(f'lock busy: {self.path}')
                time.sleep(.05)

    def release(self):
        if self.stream is not None:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise UsageError(f'invalid repository: {value!r}')
    return value.lower()


def valid_uuid(value):
    try:
        return bool(uuid.UUID(str(value)).int)
    except (ValueError, AttributeError):
        return False


def pr_url(value, repo):
    parsed = urlparse(value or '')
    if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com' or parsed.query or parsed.fragment or
            not re.fullmatch('/' + re.escape(repo) + r'/pull/[1-9][0-9]*', parsed.path, re.I)):
        raise QueueError(f'PR URL must belong to {repo}: {value!r}')
    return value


def run_gh(args):
    argv = [shutil.which('gh') or 'gh', *args]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        # repo clone can have a git child; terminating just gh would leave it
        # writing into a checkout that a later Retry is about to repair.
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=30)
        else:
            process.kill()
        process.communicate(timeout=10)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


def gh_json(*args):
    done = run_gh(args)
    if done.returncode:
        raise QueueError((done.stderr or done.stdout).decode('utf-8', errors='replace').strip() or f'gh exited {done.returncode}')
    return json.loads(done.stdout.decode('utf-8-sig'))


def resolve_spec(spec, hint=None, gh=gh_json):
    label = None
    if spec.startswith('label:'):
        label = spec[6:].strip()
        if not label:
            raise UsageError('label must not be empty')
        repo = repo_name(hint or gh('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner'])
        pages = gh('api', f'repos/{repo}/issues?labels={quote(label, safe="")}&state=open&per_page=100', '--paginate', '--slurp')
        issues = [issue for page in pages for issue in page if 'pull_request' not in issue]
        return repo, list(dict.fromkeys(i['number'] for i in sorted(issues, key=lambda i: (i['created_at'], i['number'])))), label
    items, repo = [], repo_name(hint) if hint else None
    for item in spec.split(','):
        item = item.strip()
        match = re.fullmatch(r'(?:(?P<repo>[^#]+)#|#)?(?P<number>[1-9][0-9]*)', item)
        if not match:
            raise UsageError(f'invalid queue item: {item!r}')
        if match['repo']:
            qualified = repo_name(match['repo'])
            if repo and repo != qualified:
                raise UsageError('a queue must contain exactly one repository')
            repo = qualified
        items.append(int(match['number']))
    repo = repo or repo_name(gh('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner'])
    return repo, list(dict.fromkeys(items)), label


def config_path():
    return Path(os.environ.get('AGWORKBENCH_CONFIG', Path.home() / '.agworkbench.json')).resolve()


def checkout_root(config):
    settings = read_json(config) if Path(config).exists() else {}
    return Path(settings.get('checkoutRoot', Path.home() / 'source/workbench')).expanduser().resolve()


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.directory = self.path.with_suffix('')
        self.state_lock = self.directory / 'state.lock'
        self.worker_lock = self.directory / 'worker.lock'

    def load(self):
        with Lock(self.state_lock):
            return self._load()

    def _load(self):
        try:
            data = read_json(self.path)
            if data['version'] != 1 or repo_name(data['repo']) != data['repo']:
                raise ValueError('unsupported version or repository')
            if type(data['parallel']) is not int or not 1 <= data['parallel'] <= 8 or type(data['watch']) is not bool:
                raise ValueError('invalid settings')
            if (not isinstance(data['config'], str) or not Path(data['config']).is_absolute() or
                    type(data['yes']) is not bool or not isinstance(data['members'], list) or
                    (data['watch'] and (not isinstance(data['label'], str) or not data['label']))):
                raise ValueError('invalid settings/members')
            seen = set()
            for m in data['members']:
                if (type(m['number']) is not int or m['number'] <= 0 or m['number'] in seen or m['state'] not in STATES or
                        type(m['attempt']) is not int or m['attempt'] < 0 or type(m['slotReleased']) is not bool or
                        not isinstance(m['checkout'], str) or not Path(m['checkout']).is_absolute() or
                        type(m['checkoutEstablished']) is not bool or type(m['consumedRev']) is not int or
                        m['consumedRev'] < 0 or m['phase'] not in {'active', 'pr-open', 'blocked'} or
                        (m['attempt'] > 0 and not valid_uuid(m.get('token')))):
                    raise ValueError('invalid member')
                seen.add(m['number'])
            return data
        except (OSError, ValueError, KeyError, TypeError) as err:
            raise QueueError(f'Cannot read queue {self.path}: {err}; repair this file, do not reset it') from err

    @contextlib.contextmanager
    def transaction(self):
        with Lock(self.state_lock):
            data = self._load()
            yield data
            atomic_json(self.path, data)

    def running(self):
        try:
            with Lock(self.worker_lock, 0):
                return False
        except QueueError:
            return True


def new_member(number, repo, root):
    return dict(number=number, state='pending', phase='active', attempt=0, slotReleased=False,
                checkout=str(root / f'{repo.split("/")[1]}-issue-{number}'), checkoutEstablished=False,
                pr=None, prState=None, reason=None, consumedLoop=None, consumedRev=0, since=time.time())


def find_member(data, number):
    return next((m for m in data['members'] if m['number'] == number), None)


def command_line(parts):
    # Commands supplied to a terminal shell use PowerShell quoting; subprocess argv never does.
    return ' '.join("'" + str(p).replace("'", "''") + "'" for p in parts)


def start_queue(spec, repo=None, parallel=None, watch=False, retry=False, yes=False, dry_run=False, root=None):
    repo, numbers, label = resolve_spec(spec, repo)
    if watch and not label:
        raise UsageError('-Watch requires a label spec')
    root = Path(root or os.environ.get('AGWORKBENCH_QUEUE_ROOT', Path.home() / '.agworkbench/queues')).resolve()
    store = Store(root / (repo + '.json'))
    if parallel is not None and not 1 <= parallel <= 8:
        raise UsageError('-Parallel must be between 1 and 8')
    if dry_run:
        data = store._load() if store.path.exists() else None
        validate_append(data, watch, label)
        live = store.running() if store.worker_lock.exists() else False
        print(json.dumps(dict(repo=repo, members=numbers, running=live, owner=data.get('owner') if data else None)))
        return 0
    token = None
    with Lock(store.state_lock):
        if store.path.exists():
            data = store._load()
            if data['repo'] != repo:
                raise QueueError(f'queue repository mismatch in {store.path}')
        else:
            data = dict(version=1, repo=repo, parallel=parallel or 1, watch=watch, label=label if watch else None,
                        yes=yes, config=str(config_path()), members=[], owner=None)
        validate_append(data, watch, label)
        if parallel is not None:
            data['parallel'] = parallel
        if yes:
            data['yes'] = True
        known = {m['number'] for m in data['members']}
        added = [n for n in numbers if n not in known]
        data['members'].extend(new_member(n, repo, checkout_root(data['config'])) for n in added)
        if retry:
            for m in data['members']:
                if m['state'] == 'failed':
                    m.update(state='pending', reason=None, slotReleased=False, consumedLoop=None, consumedRev=0)
        owner = data.get('owner') or {}
        if store.running() or (owner.get('state') == 'starting' and time.time() - owner['reservedAt'] < 90):
            atomic_json(store.path, data)
            print(f'queue running/starting in session {owner.get("session", "pending")}; appended {len(added)}')
            return 0
        token = str(uuid.uuid4())
        if owner.get('session'):
            print(f'previous conductor session {owner["session"]} left untouched')
        data['owner'] = dict(token=token, state='starting', session=None, reservedAt=time.time())
        atomic_json(store.path, data)
    line = '& ' + command_line([sys.executable, str(HERE / 'conductor.py'), 'run', '--file', str(store.path), '--token', token])
    session = str(agw.request('session.new', args={'name': f'#queue {repo}', 'cwd': str(HERE.parent), 'command': line})).split()[0]
    if not valid_uuid(session):
        raise QueueError(f'invalid conductor session id: {session}')
    with store.transaction() as data:
        if data['owner']['token'] == token:
            data['owner']['session'] = session
    reply = agw.request('session.restore', target=session, args={'command': line})
    if not isinstance(reply, dict) or reply.get('action') != 'pinned' or reply.get('pane') != session or reply.get('command') != line:
        raise QueueError(f'conductor pin failed; repair: agwintermctl session restore {command_line([line]).strip()} --target {session}')
    print(f'queue running in session {session}: {store.path}')
    return 0


def validate_append(data, watch, label):
    if data and watch and (not data['watch'] or data['label'] != label):
        raise UsageError('one watched label per queue; cannot change saved watch semantics')


def member_context(path, number, attempt, token):
    store = Store(path)
    with Lock(store.state_lock):
        data = store._load()
        m = find_member(data, number)
        if not m or m['state'] != 'launching' or m['attempt'] != attempt or m.get('token') != token:
            raise QueueError('stale or invalid queue launch token')
        if m['checkoutEstablished'] and not Path(m['checkout']).is_dir():
            raise QueueError(f'checkout moved or deleted: {m["checkout"]}; restore it or remove the member')
        return dict(queue=str(store.path), repo=data['repo'], number=number, attempt=attempt, token=token,
                    checkout=m['checkout'], config=data['config'])


def member_result(path, number, attempt, token, result):
    store = Store(path)
    with store.transaction() as data:
        m = find_member(data, number)
        if not m or m['attempt'] != attempt or m.get('token') != token or m['state'] != 'launching':
            print('ignored late launcher result', file=sys.stderr)
            return False
        if result.get('result') not in {'ok', 'incomplete', 'failed', 'timeout'}:
            raise QueueError('invalid launch result')
        m['result'] = dict(result, attempt=attempt, token=token)
        m['checkoutEstablished'] = Path(m['checkout']).is_dir()
    return True


def write_loop_state(root, state, pr=None, reason=None):
    if state not in {'pr-open', 'blocked', 'resumed'}:
        raise QueueError('invalid loop state')
    root = Path(root)
    directory = root / '.workbench/state'
    member = read_json(directory / 'queue-member.json')
    identity = read_json(directory / 'claude.json')
    loop_id = os.environ.get('CLAUDE_CODE_SESSION_ID')
    if not valid_uuid(loop_id) or identity['sessionId'] != loop_id:
        raise QueueError('Claude runtime identity does not match this workbench')
    repo = repo_name(member['repo'])
    if state == 'pr-open':
        pr_url(pr, repo)
    if state == 'blocked' and not reason:
        raise QueueError('blocked requires --reason')
    with Lock(directory / 'loop.lock'):
        path = directory / 'loop.json'
        previous = read_json(path) if path.exists() else {}
        same = previous.get('loopId') == loop_id and previous.get('queue') == member['queue']
        record = dict(queue=member['queue'], repo=repo, number=member['number'], loopId=loop_id,
                      rev=previous.get('rev', 0) + 1 if same else 1, state=state,
                      pr=pr or (previous.get('pr') if same else None), reason=reason, at=time.time())
        atomic_json(path, record)
    return record


def apply_loop(data, member, path):
    directory = Path(member['checkout']) / '.workbench/state'
    report_path = directory / 'loop.json'
    if not report_path.exists():
        return False
    report, identity = read_json(report_path), read_json(directory / 'claude.json')
    loop = identity['sessionId']
    if (report.get('queue') != str(path) or report.get('repo') != data['repo'] or
            report.get('number') != member['number'] or report.get('loopId') != loop or not valid_uuid(loop) or
            report.get('state') not in {'pr-open', 'blocked', 'resumed'} or
            type(report.get('rev')) is not int or report['rev'] < 1):
        raise QueueError(f'ignored stale/foreign/malformed loop report for #{member["number"]}')
    revision = member['consumedRev'] if member['consumedLoop'] == loop else 0
    if report['rev'] <= revision:
        return False
    if report['state'] == 'pr-open':
        pr_url(report.get('pr'), data['repo'])
    if report['state'] == 'blocked' and (not isinstance(report.get('reason'), str) or not report['reason'].strip()):
        raise QueueError('blocked loop report requires a reason')
    if report.get('pr'):
        pr_url(report['pr'], data['repo'])
        if member.get('pr') != report['pr']:
            member.update(pr=report['pr'], prState=None)
    state = 'active' if report['state'] == 'resumed' else report['state']
    member.update(phase=state, state=state, reason=report.get('reason'), consumedLoop=loop, consumedRev=report['rev'])
    if state in {'pr-open', 'blocked'}:
        member['slotReleased'] = True
    return True


def summary(data):
    lines = [f'Queue {data["repo"]} — snapshot at {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}',
             'Rerun github-workbench -Queue <spec> to refresh.', '', '| Issue | State | PR | Reason |', '|---|---|---|---|']
    for m in data['members']:
        state = m['state'] + (' (PR closed)' if m.get('prState') == 'CLOSED' else '')
        values = [str(m['number']), state, m.get('pr') or '', m.get('reason') or '']
        lines.append('| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in values) + ' |')
    return '\n'.join(lines) + '\n'


class Worker:
    def __init__(self, store, token, *, gh=gh_json, clock=time.time, spawn=None):
        self.store, self.token, self.gh, self.clock = store, token, gh, clock
        self.spawn = spawn or self.spawn_launcher
        self.jobs = {}
        self.next_pr = self.next_scan = 0
        self.errors = {}
        self.alerts = []
        self.last_display = {}

    def error(self, key, err):
        count = self.errors.get(key, 0) + 1
        self.errors[key] = count
        if count == 1:
            print(f'{key}: {err}', flush=True)
        if count == 3:
            self.alerts.append(f'{key}: {err}')

    def notify(self, message):
        try:
            agw.notify(agw.my_pane() or 'active', message, title='Workbench queue')
        except (agw.CtlError, OSError) as err:
            print(f'notification failed: {err}', flush=True)

    def spawn_launcher(self, data, m):
        output = self.store.directory / f'launch-{m["number"]}-{m["attempt"]}.log'
        stream = open(output, 'wb')
        env = dict(os.environ, AGWORKBENCH_CONFIG=data['config'])
        shell = shutil.which('pwsh') or shutil.which('powershell.exe')
        if not shell:
            stream.close()
            raise QueueError('PowerShell is not installed')
        args = [shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(HERE / 'github-workbench.ps1'),
                f'{data["repo"]}#{m["number"]}', '-NewSession', '-QueueMember', str(self.store.path),
                '-QueueAttempt', str(m['attempt']), '-QueueToken', m['token']]
        if data['yes']:
            args.append('-Yes')
        try:
            process = subprocess.Popen(args, cwd=HERE.parent, env=env, stdout=stream, stderr=subprocess.STDOUT)
        except BaseException:
            stream.close()
            raise
        return dict(process=process, stream=stream, path=output, started=self.clock(), attempt=m['attempt'], token=m['token'])

    def poll_jobs(self):
        for number, job in list(self.jobs.items()):
            process = job['process']
            code = process.poll()
            timed_out = self.clock() - job['started'] >= 600
            if code is None and not timed_out:
                continue
            if code is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=30)
                else:
                    process.kill()
                process.wait(timeout=30)
            job['stream'].close()
            tail = '\n'.join(job['path'].read_text(encoding='utf-8', errors='replace').splitlines()[-20:])
            with self.store.transaction() as data:
                m = find_member(data, number)
                if m and m.get('token') == job['token'] and m['attempt'] == job['attempt'] and m['state'] == 'launching':
                    if not m.get('result') and code != 75:
                        m['result'] = dict(result='timeout' if timed_out else 'failed', token=job['token'],
                                           attempt=job['attempt'], childPid=process.pid,
                                           detail='launcher timed out' if timed_out else f'launcher exited {code} without a result\n{tail}')
                    m['checkoutEstablished'] = Path(m['checkout']).is_dir()
            del self.jobs[number]

    def refresh_remote(self):
        data = self.store.load()
        if self.clock() >= self.next_pr:
            self.next_pr = self.clock() + 300
            for m in data['members']:
                if not m.get('pr') or m.get('prState') == 'MERGED':
                    continue
                key = f'PR #{m["number"]}'
                try:
                    state = self.gh('pr', 'view', m['pr'], '--json', 'state')['state']
                    if state not in {'OPEN', 'CLOSED', 'MERGED'}:
                        raise QueueError('invalid PR response')
                    with self.store.transaction() as current:
                        member = find_member(current, m['number'])
                        if member.get('pr') == m['pr']:
                            member['prState'] = state
                            if state == 'MERGED':
                                member.update(state='merged', slotReleased=True)
                    self.errors.pop(key, None)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as err:
                    self.error(key, err)
        if data['watch'] and self.clock() >= self.next_scan:
            self.next_scan = self.clock() + 300
            try:
                _, numbers, _ = resolve_spec('label:' + data['label'], data['repo'], self.gh)
                with self.store.transaction() as current:
                    known = {m['number'] for m in current['members']}
                    current['members'].extend(new_member(n, data['repo'], checkout_root(data['config'])) for n in numbers if n not in known)
                self.errors.pop('label scan', None)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as err:
                self.error('label scan', err)

    def tick(self):
        self.poll_jobs()
        with self.store.transaction() as data:
            for m in data['members']:
                result = m.get('result')
                if m['state'] == 'launching' and result and result.get('attempt') == m['attempt'] and result.get('token') == m.get('token'):
                    ok = result['result'] == 'ok'
                    m.update(state='active' if ok else 'failed', launchResult=result['result'], reason=result.get('detail'))
                    for key in ('sessionId', 'claudePane', 'codexPane', 'relaySession'):
                        if result.get(key):
                            m[key] = result[key]
                    if not ok:
                        m['slotReleased'] = True
                if m['state'] in {'active', 'pr-open', 'blocked'}:
                    try:
                        if apply_loop(data, m, self.store.path):
                            self.next_pr = 0
                            self.errors.pop(f'loop #{m["number"]}', None)
                    except (OSError, ValueError, KeyError, TypeError) as err:
                        # A partial/unrelated report never turns into an admission signal.
                        self.error(f'loop #{m["number"]}', err)
        self.refresh_remote()
        launches = []
        with self.store.transaction() as data:
            count = sum(m['state'] in {'launching', 'active'} and not m['slotReleased'] for m in data['members'])
            for m in data['members']:
                if m['state'] == 'pending' and count < data['parallel']:
                    if m['checkoutEstablished'] and not Path(m['checkout']).is_dir():
                        m.update(state='failed', slotReleased=True, reason=f'checkout moved or deleted: {m["checkout"]}; restore it or remove the member')
                        continue
                    m.update(state='launching', attempt=m['attempt'] + 1, token=str(uuid.uuid4()),
                             result=None, startedAt=self.clock(), slotReleased=False)
                    launches.append(dict(m))
                    count += 1
                elif m['state'] == 'launching' and m['number'] not in self.jobs and not m.get('result'):
                    # A predecessor may still be running after its conductor died. The
                    # checkout lock or fresh durable start intent gives it time to report.
                    if self.clock() - m['startedAt'] >= 600:
                        m.update(state='failed', slotReleased=True, launchResult='timeout', reason='interrupted launcher produced no result; use -Retry')
                    elif not file_locked(self.store.directory / f'member-{m["number"]}.lock') and not checkout_locked(Path(m['checkout'])):
                        launches.append(dict(m))
            settings = dict(data)
        for m in launches:
            try:
                self.jobs[m['number']] = self.spawn(settings, m)
            except (OSError, ValueError) as err:
                member_result(self.store.path, m['number'], m['attempt'], m['token'], dict(result='failed', detail=str(err)))
        current = self.store.load()
        for m in current['members']:
            display = (m['state'], m.get('prState'), m.get('reason'))
            if self.last_display.get(m['number']) != display:
                print(f'#{m["number"]}: {display[0]} {display[1] or ""} {display[2] or ""}', flush=True)
                if m['state'] in {'blocked', 'failed'}:
                    self.notify(f'#{m["number"]}: {m["state"]}: {m.get("reason") or ""}')
                self.last_display[m['number']] = display
        for message in self.alerts:
            self.notify(message)
        self.alerts.clear()

    def run(self):
        lock = Lock(self.store.worker_lock, 30)
        with lock:
            with self.store.transaction() as data:
                if data['owner']['token'] != self.token:
                    return 0
                data['owner'].update(state='running', session=agw.my_pane() or data['owner'].get('session'))
            self.status('active')
            while True:
                self.tick()
                # Append and final admission check share the same transaction. Release
                # worker ownership before another invocation can decide whether to start.
                report = None
                with self.store.transaction() as data:
                    if finished(data):
                        report = summary(data)
                        self.store.path.with_suffix('.md').write_text(report, encoding='utf-8')
                        data['owner']['state'] = 'finished'
                        atomic_json(self.store.path, data)
                        lock.release()
                if report is not None:
                    print(report, flush=True)
                    self.status('completed')
                    return 0
                time.sleep(20)

    def status(self, value):
        try:
            agw.set_status(value)
        except (OSError, agw.CtlError) as err:
            print(f'status unavailable: {err}', flush=True)


def checkout_locked(checkout):
    return file_locked(checkout / '.workbench/state/launch.lock')


def file_locked(path):
    if not path.exists():
        return False
    try:
        with open(path, 'r+b'):
            return False
    except OSError:
        return True


def finished(data):
    return not data['watch'] and not any(m['state'] == 'pending' or m['state'] == 'launching' or
                                       (m['state'] == 'active' and not m['slotReleased']) for m in data['members'])


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('start')
    start.add_argument('--spec', required=True)
    start.add_argument('--repo')
    start.add_argument('--parallel', type=int)
    for flag in ('watch', 'retry', 'yes', 'dry-run'):
        start.add_argument('--' + flag, action='store_true')
    run = sub.add_parser('run')
    run.add_argument('--file', required=True)
    run.add_argument('--token', required=True)
    proxy = sub.add_parser('gh-proxy')
    proxy.add_argument('arguments', nargs=argparse.REMAINDER)
    for verb in ('member-context', 'member-result'):
        child = sub.add_parser(verb)
        child.add_argument('--file', required=True)
        child.add_argument('--number', required=True, type=int)
        child.add_argument('--attempt', required=True, type=int)
        child.add_argument('--token', required=True)
        if verb == 'member-result':
            child.add_argument('--result-file', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'gh-proxy':
            arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
            done = run_gh(arguments)
            sys.stdout.buffer.write(done.stdout)
            sys.stderr.buffer.write(done.stderr)
            return done.returncode
        if args.command == 'start':
            if os.environ.get('AGWINTERM_ENABLED') != '1' or not os.environ.get('AGWINTERM_SESSION_ID'):
                raise UsageError('queue mode requires running inside agwinterm')
            return start_queue(args.spec, args.repo, args.parallel, args.watch, args.retry, args.yes, args.dry_run)
        if args.command == 'run':
            return Worker(Store(args.file), args.token).run()
        if args.command == 'member-context':
            print(json.dumps(member_context(args.file, args.number, args.attempt, args.token)))
        else:
            member_result(args.file, args.number, args.attempt, args.token, read_json(args.result_file))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, agw.CtlError) as err:
        print(f'queue: {err}', file=sys.stderr)
        return 2 if isinstance(err, UsageError) else 1


if __name__ == '__main__':
    sys.exit(main())
