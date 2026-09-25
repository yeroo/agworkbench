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
import types
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import agw
import closer
import labelquery
import triage

HERE = Path(__file__).resolve().parent
STATES = {'pending', 'launching', 'active', 'pr-open', 'blocked', 'failed', 'merged'}


class QueueError(ValueError):
    pass


class UsageError(QueueError):
    pass


class StateError(QueueError):
    """Saved queue data was refused; retrying must not overwrite it."""


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


CLOSE_BACKSTOP_AFTER = 900.0   # a merged member's close pending this long gets the backstop (#33)
TRIAGE_JOB_TIMEOUT = 600.0     # one triage.py run for one member (#34)
TRIAGE_PAUSE = 1800.0          # after a triage run stopped on a usage limit/auth or incomplete facts
RELAY_ALIVE = 'the relay is alive but its close has been pending for 15 minutes'


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


def pr_number(value):
    """The PR number from a member's PR URL (`.../pull/42`), or None."""
    match = re.search(r'/pull/([1-9][0-9]*)$', value or '')
    return int(match.group(1)) if match else None


def run_gh(args, timeout=60):
    argv = [shutil.which('gh') or 'gh', *args]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # repo clone can have a git child; terminating just gh would leave it
        # writing into a checkout that a later Retry is about to repair.
        triage.kill_tree(process)
        process.communicate(timeout=10)
        raise
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


def gh_json(*args):
    done = run_gh(args)
    if done.returncode:
        raise QueueError((done.stderr or done.stdout).decode('utf-8', errors='replace').strip() or f'gh exited {done.returncode}')
    return json.loads(done.stdout.decode('utf-8-sig'))


def resolve_spec(spec, hint=None, gh=gh_json):
    """(repo, numbers, watched): `watched` is the label of a `label:` spec, a labelquery.Query for a
    `where:` spec (#38), else None."""
    label = None
    if spec[:6].casefold() == 'where:':
        # Parsed before any network: a malformed query changes nothing and costs no call.
        try:
            node, query = labelquery.compile_query(spec[6:])
        except labelquery.QueryError as err:
            raise UsageError(f'invalid query: {err}') from None
        repo = repo_name(hint or gh('repo', 'view', '--json', 'nameWithOwner')['nameWithOwner'])
        pages = gh('api', f'repos/{repo}/issues?state=open&per_page=100', '--paginate', '--slurp')
        issues = [issue for page in pages for issue in page if 'pull_request' not in issue and
                  labelquery.evaluate(node, labelquery.labels_of(label.get('name') for label in issue.get('labels') or []))]
        return repo, list(dict.fromkeys(i['number'] for i in sorted(issues, key=lambda i: (i['created_at'], i['number'])))), query
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


def bug_label(config):
    """`bugLabel` from the config (#28): the label `-Queue bugs` stands for. Default `bug`."""
    settings = read_json(config) if Path(config).exists() else {}
    label = triage.bug_label_of(settings)             # one rule for -Queue bugs and -Triage (#34)
    if label is None:
        raise UsageError(f'bugLabel in {config} must be a non-empty label name without a comma '
                         f'(got {settings.get("bugLabel")!r})')
    return label


def expand_spec(spec, config):
    return 'label:' + bug_label(config) if spec.strip().lower() == 'bugs' else spec


# --- issues already in hand (#28) ---------------------------------------------------------------
# A label spec queues only what nobody is handling yet. The GitHub and terminal lookups are injected
# into the decision (tests fake them); the checkout rules read the disk. A failed lookup is an error,
# never "nothing in hand". Names compare case-insensitively: repo_name() lowercases, GitHub and the
# launcher's workspace keep the real case, and Find-IssueSession's `-eq` ignores it.

HELPER_SESSION = re.compile(r'#\d+ (relay|revmux r\d+|your review)')


def pr_reasons(repo, numbers, gh=gh_json):
    """{number: reason} for issues with an OPEN pull request: one that will close it (GitHub's
    closing references), or one on the workbench's own `issue-<N>-*` branch in this repo."""
    reasons = {}
    pages = gh('api', f'repos/{repo}/pulls?state=open&per_page=100', '--paginate', '--slurp')
    wanted = set(numbers)
    for pr in (pr for page in pages for pr in page):
        head = pr.get('head') or {}
        match = re.match(r'issue-(\d+)-', head.get('ref') or '')
        if (match and int(match[1]) in wanted
                and ((head.get('repo') or {}).get('full_name') or '').casefold() == repo.casefold()):
            reasons.setdefault(int(match[1]), f"pr: open PR #{pr['number']} on {head['ref']}")
    owner, name = repo.split('/')
    for start in range(0, len(numbers), 100):
        chunk = numbers[start:start + 100]
        fields = ' '.join(f'i{n}: issue(number: {n}) {{ closedByPullRequestsReferences(first: 10, '
                          f'includeClosedPrs: false) {{ nodes {{ number state }} }} }}' for n in chunk)
        query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
        result = gh('api', 'graphql', '-f', f'query={query}')
        issues = ((result or {}).get('data') or {}).get('repository')
        if not isinstance(issues, dict):
            raise QueueError(f'closing-PR lookup failed for {repo}: {result!r}'[:300])
        for n in chunk:
            nodes = (((issues.get(f'i{n}') or {}).get('closedByPullRequestsReferences')) or {}).get('nodes') or []
            open_prs = [node['number'] for node in nodes if node.get('state') == 'OPEN']
            if open_prs:
                reasons.setdefault(n, f'pr: open PR #{open_prs[0]} will close it')
    return reasons


def session_numbers(repo, tree):
    """Issue numbers with a live issue session in the repo's workspace (Find-IssueSession's rule)."""
    workspace_name = repo.split('/')[1].casefold()
    numbers = set()
    for workspace, session in agw.sessions(tree):
        if (workspace.get('name') or '').casefold() != workspace_name:
            continue
        match = re.match(r'#(\d+) ', session.get('name') or '')
        if match and not HELPER_SESSION.fullmatch(session.get('name') or ''):
            numbers.add(int(match[1]))
    return numbers


def skip_reasons(numbers, repo, root, queue_path, prs, sessions):
    """{number: reason} for issues someone is already handling, from the given PR and session
    lookups plus the checkouts on disk (a held launch.lock, a foreign .workbench)."""
    reasons = {}
    for n in numbers:
        checkout = Path(root) / f'{repo.split("/")[1]}-issue-{n}'
        state = checkout / '.workbench' / 'state'
        membership = state / 'queue-member.json'
        if n in prs:
            reasons[n] = prs[n]
        elif n in sessions:
            reasons[n] = 'session: a live workbench session is open for it'
        elif checkout_locked(checkout):
            reasons[n] = 'checkout-lock: a launcher holds its checkout'
        elif state.is_dir():
            try:
                owner = read_json(membership).get('queue') if membership.exists() else None
            except (OSError, ValueError):
                owner = None
            if owner is None or Path(owner).resolve() != Path(queue_path).resolve():
                reasons[n] = (f'checkout exists from an earlier loop ({checkout}); resume with '
                              f'github-workbench {repo}#{n} or delete it')
    return reasons


def in_hand(repo, numbers, root, queue_path, gh=gh_json, tree=None):
    if not numbers:
        return {}
    return skip_reasons(numbers, repo, root, queue_path, pr_reasons(repo, numbers, gh), session_numbers(repo, tree))


def valid_watch(data):
    """A watching queue names exactly one spec: a label or a query (#38); `query` is a string or absent."""
    label, query = data.get('label'), data.get('query')
    if query is not None and (not isinstance(query, str) or not query):
        return False
    if not data['watch']:
        return True
    has_label = isinstance(label, str) and bool(label)
    return has_label != (query is not None)


def watch_fields(watched):
    """The queue-file fields for a watched spec."""
    if isinstance(watched, labelquery.Query):
        return {'label': None, 'query': watched.text}
    return {'label': watched, 'query': None}


def watch_key(data):
    """The identity of a queue's watched spec: the label, or the query's normalised form."""
    if data.get('query'):
        try:
            return ('query', labelquery.compile_query(data['query'])[1].key)
        except labelquery.QueryError:
            return ('query', data['query'])
    return ('label', data.get('label'))


def spec_key(watched):
    return ('query', watched.key) if isinstance(watched, labelquery.Query) else ('label', watched)


def watched_spec(data):
    """The spec a watching queue rescans: rebuilt from the saved fields, never from the environment."""
    return 'where: ' + data['query'] if data.get('query') else 'label:' + data['label']


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
            if data.get('implementer') not in (None, 'codex', 'claude'):
                raise ValueError('invalid implementer')
            if data.get('autonomous') is not None and type(data['autonomous']) is not bool:
                raise ValueError('invalid autonomous')
            if 'autoMerge' in data and data['autoMerge'] is not None and type(data['autoMerge']) is not bool:
                raise ValueError('invalid autoMerge')
            if data.get('triage') is not None and type(data['triage']) is not bool:
                raise ValueError('invalid triage')
            if (not isinstance(data['config'], str) or not Path(data['config']).is_absolute() or
                    type(data['yes']) is not bool or not isinstance(data['members'], list) or
                    not valid_watch(data)):
                raise ValueError('invalid settings/members')
            seen = set()
            for m in data['members']:
                if (type(m['number']) is not int or m['number'] <= 0 or m['number'] in seen or m['state'] not in STATES or
                        type(m['attempt']) is not int or m['attempt'] < 0 or type(m['slotReleased']) is not bool or
                        not isinstance(m['checkout'], str) or not Path(m['checkout']).is_absolute() or
                        type(m['checkoutEstablished']) is not bool or type(m['consumedRev']) is not int or
                        m['consumedRev'] < 0 or m['phase'] not in {'active', 'pr-open', 'blocked'} or
                        (m['attempt'] > 0 and not valid_uuid(m.get('token'))) or
                        m.get('priority') not in (None, *triage.PRIORITIES) or
                        not isinstance(m.get('createdAt') or '', str)):
                    raise ValueError('invalid member')
                seen.add(m['number'])
            return data
        except (ValueError, KeyError, TypeError) as err:
            raise StateError(f'Cannot read queue {self.path}: {err}; repair this file, do not reset it') from err

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


def admission_key(m):
    """Pending members are admitted P0, P1, untriaged, P2, P3 (#34); oldest issue first within a
    rank, then the order they were queued."""
    created = m.get('createdAt')
    return triage.RANK.get(m.get('priority'), 2), created is None, created or '', m.get('since') or 0, m['number']


def awaits_triage(data, m):
    """With -Triage, an untriaged member waits for its triage run before it may be admitted."""
    return bool(data.get('triage')) and m.get('priority') is None and 'triageResult' not in m


def command_line(parts):
    # Commands supplied to a terminal shell use PowerShell quoting; subprocess argv never does.
    return ' '.join("'" + str(p).replace("'", "''") + "'" for p in parts)


def conductor_command(store, token):
    return '& ' + command_line([sys.executable, str(HERE / 'conductor.py'), 'run', '--file', str(store.path), '--token', token])


def pin_conductor(store, owner):
    session, token = owner['session'], owner['token']
    line = conductor_command(store, token)
    reply = agw.request('session.restore', target=session, args={'command': line})
    if not isinstance(reply, dict) or reply.get('action') != 'pinned' or reply.get('pane') != session or reply.get('command') != line:
        raise QueueError(f'conductor pin failed; repair: agwintermctl session restore {command_line([line]).strip()} --target {session}')
    with store.transaction() as data:
        if data['owner']['token'] == token and data['owner'].get('session') == session:
            data['owner']['pinned'] = True


def settings_changes(data, parallel, yes, implementer, auto_merge, autonomous, watch, label, triage_on=False):
    """What this start or append changes in a queue's saved settings (#28): {key: [old, new]}."""
    current = data or {}
    wanted = {'parallel': parallel, 'yes': True if yes else None, 'implementer': implementer,
              'autoMerge': auto_merge, 'autonomous': autonomous, 'triage': True if triage_on else None}
    changes = {key: [current.get(key), value] for key, value in wanted.items()
               if value is not None and data is not None and current.get(key) != value}
    if watch and data is not None:
        if not current.get('watch'):
            changes['watch'] = [current.get('watch'), True]
        if watch_key(current) != spec_key(label):
            # Another spelling of the same query is not a change (#38).
            for key, value in watch_fields(label).items():
                if current.get(key) != value:
                    changes[key] = [current.get(key), value]
    return changes


def start_queue(spec, repo=None, parallel=None, watch=False, retry=False, yes=False, dry_run=False, root=None,
                implementer=None, auto_merge=None, autonomous=None, gh=gh_json, triage_on=False):
    repo, numbers, label = resolve_spec(expand_spec(spec, config_path()), repo, gh)
    matches = len(numbers)
    if watch and not label:
        raise UsageError('-Watch requires a label:, bugs or where: spec')
    root = Path(root or os.environ.get('AGWORKBENCH_QUEUE_ROOT', Path.home() / '.agworkbench/queues')).resolve()
    store = Store(root / (repo + '.json'))
    if parallel is not None and not 1 <= parallel <= 8:
        raise UsageError('-Parallel must be between 1 and 8')
    if implementer not in (None, 'codex', 'claude'):
        raise UsageError('-Implementer must be codex or claude')
    existing = store.load() if store.path.exists() else None      # under the state lock, like every other read
    validate_append(existing, watch, label)
    known = {m['number']: m['state'] for m in existing['members']} if existing else {}
    skipped = {n: f'queued ({known[n]})' for n in numbers if n in known}
    fresh = [n for n in numbers if n not in known]
    if label:
        # Only a label spec is filtered: an explicit list is what the human named. Before any write.
        skipped.update(in_hand(repo, fresh, checkout_root((existing or {}).get('config') or config_path()),
                               store.path, gh))
    numbers = [n for n in numbers if n not in skipped]
    if dry_run:
        live = store.running() if store.worker_lock.exists() else False
        mode = 'start' if existing is None else ('append to a running queue' if live else 'append to a stopped queue')
        changes = settings_changes(existing, parallel, yes, implementer, auto_merge, autonomous, watch, label, triage_on)
        query = dict(query=label.text, matches=matches) if isinstance(label, labelquery.Query) else {}
        print(json.dumps(dict(repo=repo, **query, members=numbers, running=live, mode=mode,
                              skipped=[dict(number=n, reason=r) for n, r in sorted(skipped.items())],
                              settings=changes, owner=existing.get('owner') if existing else None)))
        return 0
    token = None
    with Lock(store.state_lock):
        if store.path.exists():
            data = store._load()
            if data['repo'] != repo:
                raise QueueError(f'queue repository mismatch in {store.path}')
        else:
            data = dict(version=1, repo=repo, parallel=parallel or 1, watch=watch,
                        **(watch_fields(label) if watch else {'label': None}),
                        yes=yes, config=str(config_path()), members=[], owner=None)
        validate_append(data, watch, label)
        # One mapping decides and applies every switch, and is what gets reported (#28). All apply to
        # members launched from now on; autonomous/autoMerge are saved explicitly, false included,
        # and -Watch onto a queue started from a list turns watching on.
        changes = settings_changes(data, parallel, yes, implementer, auto_merge, autonomous, watch, label, triage_on)
        for key, (_, new) in changes.items():
            data[key] = new
        known = {m['number'] for m in data['members']}
        added = [n for n in numbers if n not in known]
        data['members'].extend(new_member(n, repo, checkout_root(data['config'])) for n in added)
        if retry:
            for m in data['members']:
                if m['state'] == 'failed':
                    m.update(state='pending', reason=None, slotReleased=False, consumedLoop=None, consumedRev=0)
        owner = data.get('owner') or {}
        if store.running() or (owner.get('state') == 'starting' and time.time() - owner['reservedAt'] < 90):
            owner = dict(owner)
        else:
            token = str(uuid.uuid4())
            if owner.get('session'):
                print(f'previous conductor session {owner["session"]} left untouched')
            data['owner'] = dict(token=token, state='starting', session=None, pinned=False, reservedAt=time.time())
        atomic_json(store.path, data)
    # Reported only once written: a refused or failed write never shows changes that did not happen.
    for n, reason in sorted(skipped.items()):
        print(f'#{n} skipped: {reason}')
    for key, (old, new) in changes.items():
        print(f'settings: {key} {json.dumps(old)} -> {json.dumps(new)} (for every member launched from now on)')
    if token is None:
        if owner.get('session') and not owner.get('pinned'):
            pin_conductor(store, owner)
        print(f'queue running/starting in session {owner.get("session") or "pending"}; appended {len(added)}')
        return 0
    line = conductor_command(store, token)
    session = str(agw.request('session.new', args={'name': f'#queue {repo}', 'cwd': str(HERE.parent), 'command': line})).split()[0]
    if not valid_uuid(session):
        raise QueueError(f'invalid conductor session id: {session}')
    with store.transaction() as data:
        if data['owner']['token'] == token:
            data['owner']['session'] = session
    pin_conductor(store, dict(session=session, token=token))
    print(f'queue running in session {session}: {store.path}')
    return 0


def validate_append(data, watch, label):
    # One watched spec per queue. An unwatched queue may start watching (#28); a watched one keeps its
    # label or query (#38: compared in normalised form, so another spelling of the same query passes).
    if data and watch and data['watch'] and watch_key(data) != spec_key(label):
        raise UsageError('one watched label or query per queue; cannot change saved watch semantics')


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
                    checkout=m['checkout'], checkoutEstablished=m['checkoutEstablished'], config=data['config'])


def usable_checkout(path):
    # Do not let git discover an ancestor repository in an empty clone directory.
    if not (Path(path) / '.git').exists():
        return False
    done = subprocess.run([shutil.which('git') or 'git', '-C', str(path), '--git-dir', '.git', 'rev-parse', 'HEAD'],
                          capture_output=True, timeout=30)
    return done.returncode == 0


def member_result(path, number, attempt, token, result):
    store = Store(path)
    previous = find_member(store.load(), number)
    established = bool(previous and (previous['checkoutEstablished'] or usable_checkout(previous['checkout'])))
    with store.transaction() as data:
        m = find_member(data, number)
        if not m or m['attempt'] != attempt or m.get('token') != token or m['state'] != 'launching':
            print('ignored late launcher result', file=sys.stderr)
            return False
        if result.get('result') not in {'ok', 'incomplete', 'failed', 'timeout'}:
            raise QueueError('invalid launch result')
        m['result'] = dict(result, attempt=attempt, token=token)
        m['checkoutEstablished'] = m['checkoutEstablished'] or established
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
    def __init__(self, store, token, *, gh=gh_json, clock=time.time, spawn=None, spawn_triage=None):
        self.store, self.token, self.gh, self.clock = store, token, gh, clock
        self.spawn = spawn or self.spawn_launcher
        self.spawn_triage = spawn_triage or self.spawn_triage_run
        self.jobs = {}
        self.triage_job = None        # the one running triage.py for a pending member (#34)
        self.triage_paused_until = 0
        self.next_pr = self.next_scan = self.next_labels = 0
        self.errors = {}
        self.alerts = []
        self.last_display = {}
        self.last_skips = {}          # the last rescan's skip reasons, kept in memory only (#28)
        self.closes = {}              # member -> {'since', 'attempt'}: the close backstop (#33), memory only

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
        if data.get('implementer'):
            args += ['-Implementer', data['implementer']]
        if data.get('autonomous') is not None:
            args.append('-Autonomous' if data['autonomous'] else '-NoAutonomous')
        if data.get('autoMerge') is not None and not (data.get('autonomous') and not data['autoMerge']):
            args.append('-AutoMerge' if data['autoMerge'] else '-NoAutoMerge')
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
                triage.kill_tree(process)
                process.wait(timeout=30)
            job['stream'].close()
            tail = '\n'.join(job['path'].read_text(encoding='utf-8', errors='replace').splitlines()[-20:])
            previous = find_member(self.store.load(), number)
            established = bool(previous and (previous['checkoutEstablished'] or usable_checkout(previous['checkout'])))
            with self.store.transaction() as data:
                m = find_member(data, number)
                if m and m.get('token') == job['token'] and m['attempt'] == job['attempt'] and m['state'] == 'launching':
                    if not m.get('result') and code != 75:
                        m['result'] = dict(result='timeout' if timed_out else 'failed', token=job['token'],
                                           attempt=job['attempt'], childPid=process.pid,
                                           detail='launcher timed out' if timed_out else f'launcher exited {code} without a result\n{tail}')
                    m['checkoutEstablished'] = m['checkoutEstablished'] or established
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
                _, numbers, _ = resolve_spec(watched_spec(data), data['repo'], self.gh)
                known = {m['number'] for m in data['members']}
                fresh = [n for n in numbers if n not in known]
                skipped = in_hand(data['repo'], fresh, checkout_root(data['config']), self.store.path, self.gh)
                for n, reason in sorted(skipped.items()):
                    if self.last_skips.get(n) != reason:
                        print(f'#{n} skipped: {reason}', flush=True)
                self.last_skips = skipped
                with self.store.transaction() as current:
                    known = {m['number'] for m in current['members']}
                    current['members'].extend(new_member(n, data['repo'], checkout_root(data['config']))
                                              for n in fresh if n not in known and n not in skipped)
                self.errors.pop('label scan', None)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError, QueueError, agw.CtlError) as err:
                self.error('label scan', err)

    # --- priorities (#34) ----------------------------------------------------------------------------
    # Pending members are admitted by their issue's `priority:` label. The labels are read once per
    # refresh for the whole repo. With -Triage an untriaged pending member gets one triage.py run, in
    # the background and one at a time, before it may be admitted; a failed run admits it untriaged.

    def refresh_priorities(self):
        data = self.store.load()
        if not any(m['state'] == 'pending' for m in data['members']):
            return
        if self.clock() < self.next_labels:
            return
        self.next_labels = self.clock() + 300
        try:
            issues = self.gh('issue', 'list', '--repo', data['repo'], '--state', 'open', '--limit', '1000',
                             '--json', 'number,labels,createdAt')
            if not isinstance(issues, list):
                raise QueueError('invalid issue list')
            found = {issue['number']: issue for issue in issues if isinstance(issue, dict)}
            with self.store.transaction() as current:
                for m in current['members']:
                    issue = found.get(m['number'])
                    if m['state'] == 'pending' and issue:
                        priority = triage.priority_of(issue.get('labels'))
                        # A label just written by this queue's triage may not be listed yet: keep it.
                        if priority is not None or 'triageResult' not in m:
                            m['priority'] = priority
                        if isinstance(issue.get('createdAt'), str):
                            m['createdAt'] = issue['createdAt']
            self.errors.pop('labels', None)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as err:
            self.error('labels', err)

    def spawn_triage_run(self, data, m):
        output = self.store.directory / f'triage-{m["number"]}.log'
        result = self.store.directory / f'triage-{m["number"]}.json'
        result.unlink(missing_ok=True)
        stream = open(output, 'wb')
        try:
            process = subprocess.Popen([sys.executable, str(HERE / 'triage.py'), 'run', '--repo', data['repo'],
                                        '--issue', str(m['number']), '--result-file', str(result)],
                                       cwd=HERE.parent, env=dict(os.environ, AGWORKBENCH_CONFIG=data['config']),
                                       stdout=stream, stderr=subprocess.STDOUT)
        except BaseException:
            stream.close()
            raise
        return dict(process=process, stream=stream, path=output, result=result, number=m['number'], started=self.clock())

    def step_triage(self):
        job = self.triage_job
        if job is not None:
            code = job['process'].poll()
            timed_out = self.clock() - job['started'] >= TRIAGE_JOB_TIMEOUT
            if code is None and not timed_out:
                return
            if code is None:
                triage.kill_tree(job['process'])
                job['process'].wait(timeout=30)
            job['stream'].close()
            self.triage_job = None
            number = job['number']
            try:
                outcome = read_json(job['result']).get(str(number)) or {}
            except (OSError, ValueError, AttributeError):
                outcome = {}
            tail = ' '.join(job['path'].read_text(encoding='utf-8', errors='replace').splitlines()[-3:])
            if code in (triage.StopRun.code, triage.FactsError.code):
                self.triage_paused_until = self.clock() + TRIAGE_PAUSE
            with self.store.transaction() as data:
                m = find_member(data, number)
                if m is not None:
                    # A label that was written counts, even when a later step of that run failed.
                    if outcome.get('written') and outcome.get('priority') in triage.PRIORITIES:
                        m['priority'] = outcome['priority']
                    if code == 0:
                        m['triageResult'] = 'ok'
                    else:
                        reason = 'timed out' if timed_out else f'exited {code}: {tail}'
                        m['triageResult'] = f'failed: {reason}'[:300]
            print(f'#{number}: triage {"done" if code == 0 else "failed; admitted untriaged"} '
                  f'{outcome.get("priority") or ""}'.rstrip(), flush=True)
        data = self.store.load()
        if not data.get('triage') or self.triage_job is not None or self.clock() < self.triage_paused_until:
            return
        waiting = sorted((m for m in data['members'] if m['state'] == 'pending' and awaits_triage(data, m)),
                         key=admission_key)
        if waiting:
            try:
                self.triage_job = self.spawn_triage(data, waiting[0])
            except (OSError, ValueError) as err:
                self.mark(waiting[0]['number'], triageResult=f'failed: {err}'[:300])

    # --- the close backstop (#33) -------------------------------------------------------------------
    # The relay closes a merged member's sessions. When that relay is gone (killed, closed, never
    # restarted) and its close has been pending for CLOSE_BACKSTOP_AFTER, the conductor runs the same
    # close (closer.py) one step per tick. While the relay is alive it only flags `closeStuck`: one
    # closer at a time. Never on a timeout alone.

    def mark(self, number, **fields):
        with self.store.transaction() as data:
            member = find_member(data, number)
            for key, value in fields.items():
                if value is None:
                    member.pop(key, None)
                else:
                    member[key] = value

    def close_backstop(self):
        data = self.store.load()
        for m in data['members']:
            if m['state'] != 'merged':
                continue
            number = m['number']
            pr = pr_number(m.get('pr'))    # relay.json's close_pending holds the PR number, not the issue's
            hub_dir = Path(m['checkout']) / '.workbench'
            try:
                relay_state = read_json(hub_dir / 'state' / 'relay.json') if (hub_dir / 'state' / 'relay.json').exists() else {}
            except (OSError, ValueError):
                relay_state = {}
            if pr is None or relay_state.get('close_pending') != pr:
                # Nothing pending: the relay closed (or refused), or autonomy was off. A "relay alive"
                # flag is resolved by that; a refused backstop close stays flagged for the human.
                self.closes.pop(number, None)
                live_flag = str(m.get('closeStuck', '')).startswith(RELAY_ALIVE)
                if m.get('closePending') or live_flag:
                    self.mark(number, closePending=None, closeStuck=None if live_flag else m.get('closeStuck'))
                continue
            if not m.get('closePending'):
                self.mark(number, closePending=True)
            watch = self.closes.setdefault(number, {'since': self.clock(), 'attempt': None})
            if self.clock() - watch['since'] < CLOSE_BACKSTOP_AFTER:
                continue
            try:
                self.step_close(data, m, pr, hub_dir, watch)
            except (agw.CtlError, OSError, ValueError, KeyError) as err:
                self.error(f'close #{number}', err)

    def step_close(self, data, m, pr, hub_dir, watch):
        number = m['number']
        # Every tick: a relay that came back owns the close again, and this attempt is dropped.
        if closer.relay_alive(data['repo'], str(number), agw.tree()):
            if watch['attempt'] is not None:
                watch['attempt'].log('the relay is back; the conductor leaves the close to it')
                watch['attempt'] = None
            if not m.get('closeStuck'):
                print(f'#{number}: {RELAY_ALIVE}', flush=True)
                self.mark(number, closeStuck=RELAY_ALIVE)
            return
        if watch['attempt'] is None:
            registry = read_json(hub_dir / 'state' / 'agents.json')['agents']
            peers = [types.SimpleNamespace(box=box, tool=registry[box].get('tool', box), pane=registry[box]['pane'])
                     for box in ('claude', 'codex')]
            watch['attempt'] = closer.Closer(hub_dir, data['repo'], number, peers,
                                             log=lambda text: print(f'#{number} {text}', flush=True), clock=self.clock)
            watch['attempt'].log(f'PR #{pr} (issue #{number}) merged and its relay is gone; the conductor runs the close')
        attempt = watch['attempt']
        attempt.step_helpers()
        reasons = attempt.agent_blockers(pr)
        if not reasons:
            if attempt.autonomous():
                attempt.close_issue_session()
            else:
                attempt.log('NOT closing: autonomy was turned off')
            self.end_close(m, pr, hub_dir, stuck=None)
        elif attempt.timed_out():
            attempt.log(f'NOT closing, still waiting after {closer.CLOSE_WAIT:.0f}s: ' + '; '.join(reasons))
            self.notify(f'#{number}: autonomous close stopped: ' + '; '.join(reasons))
            self.end_close(m, pr, hub_dir, stuck='the backstop close timed out: ' + '; '.join(reasons))

    def end_close(self, m, pr, hub_dir, stuck):
        path = hub_dir / 'state' / 'relay.json'
        state = read_json(path)
        if state.get('close_pending') == pr:
            # Only the close this attempt ran: a relay may have rewritten the file since.
            state.pop('close_pending')
            atomic_json(path, state)
        self.closes.pop(m['number'], None)
        self.mark(m['number'], closePending=None, closeStuck=stuck)

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
        self.refresh_priorities()
        self.step_triage()
        self.close_backstop()
        launches = []
        with self.store.transaction() as data:
            count = sum(m['state'] in {'launching', 'active'} and not m['slotReleased'] for m in data['members'])
            # While triage is paused (a usage limit, or facts it could not read) nobody waits for it.
            paused = self.clock() < self.triage_paused_until
            for m in sorted((m for m in data['members'] if m['state'] == 'pending'), key=admission_key):
                if count >= data['parallel'] or (awaits_triage(data, m) and not paused):
                    break                  # strictly in order: nothing behind a member still being triaged
                if m['checkoutEstablished'] and not Path(m['checkout']).is_dir():
                    m.update(state='failed', slotReleased=True, reason=f'checkout moved or deleted: {m["checkout"]}; restore it or remove the member')
                    continue
                m.update(state='launching', attempt=m['attempt'] + 1, token=str(uuid.uuid4()),
                         result=None, startedAt=self.clock(), slotReleased=False)
                launches.append(dict(m))
                count += 1
            admitted = {m['number'] for m in launches}
            for m in data['members']:
                if m['state'] == 'launching' and m['number'] not in self.jobs and not m.get('result') and m['number'] not in admitted:
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
            initialized = False
            while True:
                try:
                    if not initialized:
                        with self.store.transaction() as data:
                            if data['owner']['token'] != self.token:
                                return 0
                            data['owner'].update(state='running', session=agw.my_pane() or data['owner'].get('session'))
                        initialized = True
                        self.status('active')
                    self.tick()
                    # Publish completion and release ownership under the state lock,
                    # so a racing append either keeps us alive or starts a successor.
                    report = None
                    with Lock(self.store.state_lock):
                        data = self.store._load()
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
                    self.errors.pop('conductor tick', None)
                except StateError as err:
                    print(str(err), flush=True)
                    self.notify(str(err))
                    self.status('blocked')
                    return 1
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as err:
                    self.error('conductor tick', err)
                    for message in self.alerts:
                        self.notify(message)
                    self.alerts.clear()
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
    # A merged member whose close is still pending keeps the conductor up for the backstop (#33),
    # unless it is flagged stuck (then it is the human's, and never keeps the queue alive forever).
    if any(m.get('closePending') and not m.get('closeStuck') for m in data['members']):
        return False
    return not data['watch'] and not any(m['state'] == 'pending' or m['state'] == 'launching' or
                                       (m['state'] == 'active' and not m['slotReleased']) for m in data['members'])


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('start')
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument('--spec')
    # The launcher passes the spec in AGWORKBENCH_QUEUE_SPEC: Windows PowerShell 5.1 strips the double
    # quotes out of a native argument, which would change a quoted label in a query (#38).
    source.add_argument('--spec-env', action='store_true')
    start.add_argument('--repo')
    start.add_argument('--parallel', type=int)
    for flag in ('watch', 'retry', 'yes', 'dry-run', 'triage'):
        start.add_argument('--' + flag, action='store_true')
    start.add_argument('--implementer', choices=('codex', 'claude'))
    merge = start.add_mutually_exclusive_group()
    merge.add_argument('--auto-merge', dest='auto_merge', action='store_const', const=True)
    merge.add_argument('--no-auto-merge', dest='auto_merge', action='store_const', const=False)
    autonomy = start.add_mutually_exclusive_group()
    autonomy.add_argument('--autonomous', dest='autonomous', action='store_const', const=True)
    autonomy.add_argument('--no-autonomous', dest='autonomous', action='store_const', const=False)
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
            # Cloning is bounded by the launcher's overall ten-minute deadline.
            done = run_gh(arguments, timeout=None if arguments[:2] == ['repo', 'clone'] else 60)
            sys.stdout.buffer.write(done.stdout)
            sys.stderr.buffer.write(done.stderr)
            return done.returncode
        if args.command == 'start':
            if os.environ.get('AGWINTERM_ENABLED') != '1' or not os.environ.get('AGWINTERM_SESSION_ID'):
                raise UsageError('queue mode requires running inside agwinterm')
            spec = args.spec
            if args.spec_env:
                spec = os.environ.get('AGWORKBENCH_QUEUE_SPEC', '')
                if not spec.strip():
                    raise UsageError('--spec-env: AGWORKBENCH_QUEUE_SPEC is empty')
            return start_queue(spec, args.repo, args.parallel, args.watch, args.retry, args.yes, args.dry_run,
                               implementer=args.implementer, auto_merge=args.auto_merge,
                               autonomous=args.autonomous, triage_on=args.triage)
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
