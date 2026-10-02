#!/usr/bin/env python3
"""Persistent issue queue. Only the conductor admits work; issue relays retain human review."""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import getpass
import re
import shutil
import subprocess
import sys
import time
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import agw
import closer
import labelquery
import triage
import tslog

HERE = Path(__file__).resolve().parent
STATES = {'pending', 'launching', 'active', 'pr-open', 'blocked', 'failed', 'merged', 'closed'}


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


def retire_no_pr_done(path, *, expected_at=..., issue=None):
    """Preserve a refused no-PR latch only if it is still the record this close saw."""
    path = Path(path)
    with Lock(path.with_name('loop-done.lock')):
        try:
            done = read_json(path)
        except (OSError, ValueError):
            return False
        if (not isinstance(done, dict) or done.get('noPr') is not True or done.get('pr') is not None
                or (issue is not None and str(done.get('issue')) != str(issue))
                or (expected_at is not ... and done.get('at') != expected_at)):
            return False
        os.replace(path, path.with_name('loop-done-refused.json'))
        return True


def repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise UsageError(f'invalid repository: {value!r}')
    return value.lower()


# --- named queues (#66) ------------------------------------------------------------------------
# A repo's main queue is `<owner>/<repo>.json`; a named one `<owner>/<repo>.<name>.json`, with its own
# agwinterm workspace (default `<repo>-<name>`) and checkouts `<repo>-<name>-issue-N`. Repo names may
# contain dots, so a file name alone never says whose queue it is: the recorded repo and name do.

QUEUE_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')
WORKSPACE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
REVMUX_PROFILE = re.compile(r'[A-Za-z0-9._-]+')
TERMINAL_STATES = {'merged', 'closed'}


def queue_name(value):
    """The queue name -QueueName gives, case-folded; None for the main queue."""
    if value is None or value == '':
        return None
    name = value.casefold() if isinstance(value, str) else value
    if not isinstance(name, str) or not QUEUE_NAME.fullmatch(name) or name == 'main':
        raise UsageError(f'invalid queue name {value!r}: 1-32 of a-z, 0-9 and -, not starting with -; "main" is the '
                         'unnamed queue')
    return name


def queue_file_name(repo, name=None):
    return repo.split('/')[1] + (f'.{name}' if name else '') + '.json'


def queue_path(root, repo, name=None):
    return Path(root) / repo.split('/')[0] / queue_file_name(repo, name)


def queue_label(data):
    """`o/r` for the main queue, `o/r (kimi)` for a named one: every line and title that names the queue."""
    return data['repo'] + (f' ({data["name"]})' if data.get('name') else '')


def queue_workspace(data):
    """The agwinterm workspace of the queue's sessions: its own for a named queue, else the repo's name."""
    return data.get('workspace') or data['repo'].split('/')[1]


def checkout_name(repo, number, name=None):
    return f'{repo.split("/")[1]}{"-" + name if name else ""}-issue-{number}'


def claims_lock(root, repo):
    """The repo-wide lock every member add of every queue of the repo takes (#66), so the sibling
    check and the write are one step. Order: gh/terminal lookups with no lock -> this lock -> the
    siblings, each read through its own Store.load (never nested in our state lock) -> our own state
    lock. Nothing takes it while holding a state lock, and nothing slow runs under it. The Worker's
    other transactions never take it: they change states or remove members. That releases claims,
    with one exception: a `closed` member (a no-PR close) accepts its loop's `resumed` report after a
    reopen (apply_loop) and so claims again without this lock. A closed member therefore still counts
    as a claim while its close is pending or stuck, or while its issue session is open in its queue's
    workspace (`claims`, `live_closed`) - a reopened no-PR loop, or one left open with autonomy off.
    Only a loop whose sessions are gone and whose close is over can be revived after another queue
    took its issue, and then the conductor only records the old loop's report - a closed member is
    never relaunched."""
    owner, name = repo.split('/')
    return Lock(Path(root) / owner / f'{name}.claims.lock', 30)


def sibling_queues(root, repo, own):
    """Every other queue of this repo, loaded: only the candidate files `<repo>.json` and
    `<repo>.<q>.json`, so another repo's queue is never read. A candidate that records another repo
    (`docxy.kimi.json` may be repo `docxy.kimi`'s) is not a sibling; an unreadable one raises
    StateError - never "unclaimed"."""
    owner, name = repo.split('/')
    directory = Path(root) / owner
    prefix = name.casefold() + '.'
    found = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        file = path.name.casefold()
        if not file.startswith(prefix) or not file.endswith('.json') or not path.is_file():
            continue
        middle = file[len(prefix):-len('.json')]
        if middle and not QUEUE_NAME.fullmatch(middle):
            continue
        if not middle and file != name.casefold() + '.json':
            continue
        if Path(own).resolve() == path.resolve():
            continue
        data = Store(path).load()
        if data['repo'] == repo:
            found.append(data)
    return found


def repo_workspaces(repo, siblings, own=None):
    """Every workspace an issue of the repo can be worked in (#66): the repo's own - manual launches
    and the main queue, whether or not it has a file - every sibling's, and this queue's."""
    return list(dict.fromkeys([repo.split('/')[1], *(queue_workspace(d) for d in siblings), *([own] if own else [])]))


def closing(m):
    return bool(m.get('closePending') or m.get('closeStuck'))


def live_closed(repo, siblings, fresh, tree=None):
    """{sibling workspace (case-folded): issue numbers with a live issue session} - read only when a
    sibling has a closed member among `fresh` that no close flag already keeps claimed (#66 r2 m2).
    A terminal that cannot be read raises: never "unclaimed"."""
    wanted = [d for d in siblings if any(m['state'] == 'closed' and m['number'] in fresh and not closing(m)
                                         for m in d['members'])]
    if not wanted:
        return {}
    tree = agw.tree() if tree is None else tree
    return {queue_workspace(d).casefold(): session_numbers(repo, tree, [queue_workspace(d)]) for d in wanted}


def claims(siblings, live=None):
    """{issue number: queue name} for every sibling member that is not merged or closed: a pending,
    live, blocked or failed member holds its issue (#66). So does a closed one whose close is still
    pending or stuck, or whose issue session is open in its queue's workspace (`live`, from
    live_closed), since its loop can still come back (see claims_lock)."""
    live = live or {}
    return {m['number']: data.get('name') or 'main'
            for data in siblings for m in data['members']
            if m['state'] not in TERMINAL_STATES or
            (m['state'] == 'closed' and (closing(m) or m['number'] in live.get(queue_workspace(data).casefold(), ())))}


def skip_claimed(skipped, claimed, fresh):
    """Record the claims among `fresh` in `skipped`. A claim wins over an in-hand reason (#66): the
    other queue's own checkout or session is not something to resume or delete."""
    skipped.update({n: f'claimed by queue {q}' for n, q in claimed.items() if n in fresh})


CLOSE_BACKSTOP_AFTER = 900.0   # a merged or no-PR closed member's pending close gets the backstop (#33)
CLOSE_ISSUE_CHECK_INTERVAL = 60.0  # GitHub CLOSED gate during a no-PR backstop wait
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


def cwd_repo():
    """The repository of the current directory's origin (#71): `gh repo view` answers a fork's
    checkout with the fork's parent."""
    done = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    repo = triage.github_repo(done.stdout) if done.returncode == 0 else None
    if not repo:
        raise UsageError('this directory is not a checkout with a GitHub origin; name the repository')
    return repo


def resolve_spec(spec, hint=None, gh=gh_json, origin=cwd_repo):
    """(repo, numbers, watched): `watched` is the label of a `label:` spec, a labelquery.Query for a
    `where:` spec (#38), else None. Without a hint the repo is the cwd's origin, never gh's guess."""
    label = None
    if spec[:6].casefold() == 'where:':
        # Parsed before any network: a malformed query changes nothing and costs no call.
        try:
            node, query = labelquery.compile_query(spec[6:])
        except labelquery.QueryError as err:
            raise UsageError(f'invalid query: {err}') from None
        repo = repo_name(hint or origin())
        pages = gh('api', f'repos/{repo}/issues?state=open&per_page=100', '--paginate', '--slurp')
        issues = [issue for page in pages for issue in page if 'pull_request' not in issue and
                  labelquery.evaluate(node, labelquery.labels_of(label.get('name') for label in issue.get('labels') or []))]
        return repo, list(dict.fromkeys(i['number'] for i in sorted(issues, key=lambda i: (i['created_at'], i['number'])))), query
    if spec.startswith('label:'):
        label = spec[6:].strip()
        if not label:
            raise UsageError('label must not be empty')
        repo = repo_name(hint or origin())
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
    repo = repo or repo_name(origin())
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


def session_numbers(repo, tree, workspaces=None):
    """Issue numbers with a live issue session in the given workspaces (Find-IssueSession's rule);
    by default the repo's own workspace (#66: a named queue's members live in the queue's)."""
    wanted = {w.casefold() for w in (workspaces or [repo.split('/')[1]])}
    numbers = set()
    for workspace, session in agw.sessions(tree):
        if (workspace.get('name') or '').casefold() not in wanted:
            continue
        match = re.match(r'#(\d+) ', session.get('name') or '')
        if match and not HELPER_SESSION.fullmatch(session.get('name') or ''):
            numbers.add(int(match[1]))
    return numbers


def skip_reasons(numbers, repo, root, queue_path, prs, sessions, name=None):
    """{number: reason} for issues someone is already handling, from the given PR and session
    lookups plus the checkouts on disk (a held launch.lock, a foreign .workbench). A named queue
    (#66) checks its own checkout and the repo's plain `<repo>-issue-N` one, which a manual launch
    or the main queue uses."""
    reasons = {}
    for n in numbers:
        for q in dict.fromkeys((name, None)):
            # The queue's own named checkout (#66), then the plain one a manual launch or the main queue uses.
            reason = checkout_reason(Path(root) / checkout_name(repo, n, q), repo, n, queue_path, q)
            if reason:
                break
        if n in prs:
            reasons[n] = prs[n]
        elif n in sessions:
            reasons[n] = 'session: a live workbench session is open for it'
        elif reason:
            reasons[n] = reason
    return reasons


def checkout_reason(checkout, repo, n, queue_path, name=None):
    state = checkout / '.workbench' / 'state'
    membership = state / 'queue-member.json'
    if checkout_locked(checkout):
        return 'checkout-lock: a launcher holds its checkout'
    if state.is_dir():
        try:
            owner = read_json(membership).get('queue') if membership.exists() else None
        except (OSError, ValueError):
            owner = None
        if owner is None or Path(owner).resolve() != Path(queue_path).resolve():
            if name:
                # A named checkout without this queue's membership cannot be resumed by one (#66 r2 m3).
                return (f'checkout exists without a queue membership this queue can use ({checkout}); '
                        f'delete it (a member of this queue is relaunched with '
                        f'github-workbench -Queue <spec> -QueueName {name} -Retry)')
            return (f'checkout exists from an earlier loop ({checkout}); resume with '
                    f'github-workbench {repo}#{n} or delete it')
    return None


def in_hand(repo, numbers, root, queue_path, gh=gh_json, tree=None, *, name=None, workspaces=None):
    """The in-hand skips (#28) of one queue: `workspaces` are every workspace of the repo's queues (#66)."""
    if not numbers:
        return {}
    return skip_reasons(numbers, repo, root, queue_path, pr_reasons(repo, numbers, gh),
                        session_numbers(repo, tree, workspaces), name)


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


GIB = 1024 ** 3                 # minFreeGB counts what Explorer labels "GB" (#41)
CEILING_EXTRA = 2               # live members beyond -Parallel the conductor tolerates (#61)
SESSION_GRACE = 120             # seconds a slot-holding member's session may be missing before its slot goes (#61)
LIVE_STATES = {'active', 'blocked', 'pr-open'}
IMPLEMENTER_TOOLS = ('codex', 'claude', 'kimi')     # #65
DEFAULT_FAILOVER_ORDER = ['claude', 'codex', 'kimi']
_UNREAD = object()


def free_setting(config, key, default):
    """A GiB threshold from the config: `default` when absent, 0 turns its guard off. Anything else is an error."""
    settings = read_json(config) if Path(config).exists() else {}
    value = settings.get(key, default)
    if value is None:
        return default
    if type(value) not in (int, float) or value < 0:
        raise QueueError(f'{key} in {config} must be a number >= 0 (got {value!r})')
    return value


def min_free_gb(config):
    return free_setting(config, 'minFreeGB', 20)          # #41


def min_free_ram_gb(config):
    return free_setting(config, 'minFreeRamGB', 3)        # #61


def free_ram():
    """Available physical memory in bytes, or None where this platform has no way to say (#61)."""
    if os.name == 'nt':
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError('GlobalMemoryStatusEx failed')
        return status.ullAvailPhys
    try:
        with open('/proc/meminfo', encoding='ascii') as stream:
            for line in stream:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except FileNotFoundError:
        return None
    raise OSError('no MemAvailable in /proc/meminfo')


def free_bytes(path):
    """(free bytes, drive) for the drive holding `path`, from its nearest existing ancestor."""
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    return shutil.disk_usage(path).free, path.anchor or str(path)


def valid_tool_limits(data):
    """toolLimits (#61): tool -> {at, line, kind, member}; toolLimitsClearedAt: tool -> epoch seconds."""
    limits, cleared = data.get('toolLimits', {}), data.get('toolLimitsClearedAt', {})
    if not isinstance(limits, dict) or not isinstance(cleared, dict):
        return False
    for tool, entry in limits.items():
        if (tool not in IMPLEMENTER_TOOLS or not isinstance(entry, dict) or
                type(entry.get('at')) not in (int, float) or not isinstance(entry.get('line'), str) or
                entry.get('kind') not in ('limited', 'warning') or type(entry.get('member')) is not int):
            return False
    return all(tool in IMPLEMENTER_TOOLS and type(at) in (int, float) for tool, at in cleared.items())


def epoch(value):
    """An ISO time (the launcher's `o` format or the relay's) as epoch seconds; None when unreadable."""
    moment = closer.parse_time(value)
    return moment.timestamp() if moment else None


def member_limits(m):
    """The usage limits a member's checkout recorded (#61): [(tool, at, line, kind)]. The launcher's
    implementer.json `limits` (what -Failover switched away from) and the relay's announced episodes
    (the tool comes from the episode, whichever box it is in)."""
    directory = Path(m['checkout']) / '.workbench/state'
    found = []
    path = directory / 'implementer.json'
    if path.exists():
        for tool, entry in (read_json(path).get('limits') or {}).items():
            if tool in IMPLEMENTER_TOOLS and isinstance(entry, dict):
                found.append((tool, epoch(entry.get('at')), str(entry.get('line') or ''),
                              'warning' if entry.get('kind') == 'warning' else 'limited'))
    path = directory / 'relay.json'
    if path.exists():
        for episode in (read_json(path).get('limits') or {}).values():
            # A limit waited out (#77) is not a tool to route around: the member keeps it and waits.
            if (isinstance(episode, dict) and episode.get('announced') and not episode.get('wait') and
                    episode.get('tool') in IMPLEMENTER_TOOLS and episode.get('kind') in ('limited', 'warning')):
                found.append((episode['tool'], epoch(episode.get('firstSeen')), str(episode.get('line') or ''),
                              episode['kind']))
    return [item for item in found if item[1] is not None]


def relay_limited(m):
    """Whether the member's relay has announced a usage-limit episode (#61): its block is environmental."""
    path = Path(m['checkout']) / '.workbench/state/relay.json'
    if not path.exists():
        return False
    episodes = read_json(path).get('limits') or {}
    return any(isinstance(e, dict) and e.get('announced') for e in episodes.values())


def limit_wait(m, now):
    """The usage limit a member waits out (#77): {tool, since, retryAt, source} from its relay's wait
    episode (relay.json) or a review round's reviewer limit (review-limit.json) until its retryAt; None
    when it waits for nothing. Times are epoch seconds, None when unreadable. A relay episode ends only
    when the pane clears; a review wait past its retryAt is over - the rerun holds its own slot, and a
    file nobody cleaned up must not pin the queue."""
    directory = Path(m['checkout']) / '.workbench/state'
    path = directory / 'relay.json'
    if path.exists():
        for box, episode in sorted((read_json(path).get('limits') or {}).items()):
            if isinstance(episode, dict) and episode.get('wait') and episode.get('announced'):
                retry = episode.get('retryAt')
                return dict(tool=str(episode.get('tool') or '?'), since=epoch(episode.get('firstSeen')),
                            retryAt=retry if type(retry) in (int, float) else None, source=f'relay {box}')
    path = directory / 'review-limit.json'
    if path.exists():
        record = read_json(path)
        if isinstance(record, dict):
            since, retry = record.get('since'), record.get('retryAt')
            if type(retry) in (int, float) and retry <= now:
                return None
            return dict(tool=str(record.get('tool') or '?'), since=since if type(since) in (int, float) else None,
                        retryAt=retry if type(retry) in (int, float) else None, source='review')
    return None


def local_clock(epoch_value):
    """HH:MM local time, '?' for a time that is missing or out of range (a display never fails a tick)."""
    try:
        return time.strftime('%H:%M', time.localtime(epoch_value)) if epoch_value is not None else '?'
    except (OSError, ValueError, OverflowError):
        return '?'


def wait_text(m):
    wait = m['limitWait']
    what = 'reviewer usage limit' if wait['source'] == 'review' else 'usage limit'
    return (f'#{m["number"]} {wait["tool"]} {what} since {local_clock(wait["since"])}, '
            f'next try {local_clock(wait["retryAt"])}')


def tool_route(data):
    """(implementer for the next launch, pause reason) from the queue's recorded tool limits (#61).
    (None, None) launches with the queue's own settings, and always in a queue that waits limits out (#77)."""
    if data.get('onLimit') == 'wait':
        return None, None
    limits = data.get('toolLimits') or {}
    if not limits:
        return None, None
    said = ', '.join(f"{tool} {entry['kind']} (#{entry['member']}: {entry['line']})"
                     for tool, entry in sorted(limits.items()))
    if 'claude' in limits:
        return None, f'tool limits: {said}; the planner is always Claude, so no member can run'
    try:
        settings = read_json(data['config']) if Path(data['config']).exists() else {}
    except (OSError, ValueError) as err:
        return None, f'tool limits: {said}; cannot read {data["config"]}: {err}'
    wanted = data.get('implementer') or settings.get('implementer') or 'codex'
    if wanted not in limits:
        return None, None
    if settings.get('failover') is False:
        return None, f'tool limits: {said}; failover is off'
    # The launcher's -Failover rule (#65): the first tool in failoverOrder that is not the limited one
    # and has no recorded limit. Whether it is installed is the launcher's check, at the launch.
    order = settings.get('failoverOrder')
    if order is None:                   # absent or null: the default, as the launcher reads it
        order = DEFAULT_FAILOVER_ORDER
    if (not isinstance(order, list) or len(order) < 2 or len(set(map(str, order))) != len(order)
            or any(tool not in IMPLEMENTER_TOOLS for tool in order)):
        return None, f'tool limits: {said}; failoverOrder in {data["config"]} is invalid: {order!r}'
    target = next((tool for tool in order if tool != wanted and tool not in limits), None)
    if target is None:
        return None, f'tool limits: {said}; no tool in failoverOrder {order} is free of a recorded limit'
    return target, None


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.directory = self.path.with_suffix('')
        self.state_lock = self.directory / 'state.lock'
        self.worker_lock = self.directory / 'worker.lock'
        self.root = self.path.parent.parent        # <root>/<owner>/<file>: where the repo's other queues are

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
            if data.get('implementer') not in (None, *IMPLEMENTER_TOOLS):
                raise ValueError('invalid implementer')
            if data.get('autonomous') is not None and type(data['autonomous']) is not bool:
                raise ValueError('invalid autonomous')
            if 'autoMerge' in data and data['autoMerge'] is not None and type(data['autoMerge']) is not bool:
                raise ValueError('invalid autoMerge')
            if data.get('bigReview') is not None and type(data['bigReview']) is not bool:
                raise ValueError('invalid bigReview')
            if data.get('onLimit') not in (None, 'wait', 'failover'):
                raise ValueError('invalid onLimit')
            if data.get('triage') is not None and type(data['triage']) is not bool:
                raise ValueError('invalid triage')
            # #66: the file name is the one its recorded repo and name give - which also tells a dotted
            # repo's main queue (`docxy.kimi.json` of `o/docxy.kimi`) from a named one.
            name = data.get('name')
            if name is not None and (not isinstance(name, str) or not QUEUE_NAME.fullmatch(name) or name == 'main'):
                raise ValueError('invalid name')
            if self.path.name.casefold() != queue_file_name(data['repo'], name).casefold():
                raise ValueError(f'the file name does not match its queue ({queue_file_name(data["repo"], name)})')
            workspace = data.get('workspace')
            if (workspace is not None or name is not None) and (
                    not isinstance(workspace, str) or not WORKSPACE_NAME.fullmatch(workspace) or name is None):
                raise ValueError('invalid workspace')
            if name is not None and data.get('triage'):
                raise ValueError('a named queue does not triage')
            if data.get('revmuxProfile') is not None and (
                    not isinstance(data['revmuxProfile'], str) or not REVMUX_PROFILE.fullmatch(data['revmuxProfile'])):
                raise ValueError('invalid revmuxProfile')
            for key in ('diskPaused', 'ramPaused', 'toolsPaused'):
                if data.get(key) is not None and not isinstance(data[key], str):
                    raise ValueError(f'invalid {key}')
            if not valid_tool_limits(data):
                raise ValueError('invalid toolLimits')
            backoff = data.get('launchBackoff')
            if backoff is not None and (not isinstance(backoff, dict) or
                    type(backoff.get('member')) is not int or backoff['member'] <= 0 or
                    type(backoff.get('failures')) is not int or backoff['failures'] < 1 or
                    type(backoff.get('until')) not in (int, float) or
                    not isinstance(backoff.get('reason'), str)):
                raise ValueError('invalid launchBackoff')
            if data.get('launchPaused') is not None and not isinstance(data['launchPaused'], str):
                raise ValueError('invalid launchPaused')
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
                        m['consumedRev'] < 0 or m['phase'] not in {'active', 'pr-open', 'blocked', 'closed'} or
                        (m['attempt'] > 0 and not valid_uuid(m.get('token'))) or
                        m.get('priority') not in (None, *triage.PRIORITIES) or
                        m.get('cause') not in (None, 'environment') or
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


def new_member(number, repo, root, name=None):
    return dict(number=number, state='pending', phase='active', attempt=0, slotReleased=False,
                checkout=str(root / checkout_name(repo, number, name)), checkoutEstablished=False,
                pr=None, prState=None, reason=None, consumedLoop=None, consumedRev=0, since=time.time())


def find_member(data, number):
    return next((m for m in data['members'] if m['number'] == number), None)


def mark_pr(path, number, pr, reason=None):
    """Operator recovery for a missing planner PR report, with a durable audit line."""
    store = Store(path)
    with store.transaction() as data:
        try:
            url = pr_url(pr, data['repo'])
        except QueueError as err:
            raise UsageError(str(err)) from err
        member = find_member(data, number)
        if member is None:
            raise UsageError(f'unknown queue member #{number}')
        if member['state'] not in {'active', 'blocked', 'pr-open'}:
            raise UsageError(f'cannot mark #{number} while {member["state"]}')
        audit = dict(at=time.time(), pr=url, user=getpass.getuser(), reason=reason)
        member.update(pr=url, prState=None, state='pr-open', phase='pr-open',
                      slotReleased=True, reason=None, operatorMark=audit)
    store.directory.mkdir(parents=True, exist_ok=True)
    with (store.directory / 'operator.log').open('a', encoding='utf-8') as log:
        log.write(json.dumps(dict(number=number, **audit)) + '\n')
    return audit


def admission_key(m):
    """Pending members are admitted P0, P1, untriaged, P2, P3 (#34); oldest issue first within a
    rank, then the order they were queued."""
    created = m.get('createdAt')
    return triage.RANK.get(m.get('priority'), 2), created is None, created or '', m.get('since') or 0, m['number']


def defer_launch(data, member, reason, now):
    """Persist an infrastructure deferral and its next single-member probe."""
    failures = data.get('launchBackoff', {}).get('failures', 0) + 1
    data['launchBackoff'] = dict(member=member['number'], failures=failures,
                                 until=now + min(60 * 2 ** min(failures - 1, 4), 900), reason=reason)
    if failures >= 2:
        data['launchPaused'] = 'launches failing: ' + reason
    member.update(state='pending', slotReleased=False, reason=reason)


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


def settings_changes(data, parallel, yes, implementer, auto_merge, autonomous, watch, label, triage_on=False,
                     revmux_profile=None, big_review=None, on_limit=None):
    """What this start or append changes in a queue's saved settings (#28): {key: [old, new]}. A revmux
    profile is saved only when the human passed one (#66): the kimi default is the launcher's, per launch."""
    current = data or {}
    wanted = {'parallel': parallel, 'yes': True if yes else None, 'implementer': implementer,
              'autoMerge': auto_merge, 'autonomous': autonomous, 'triage': True if triage_on else None,
              'revmuxProfile': revmux_profile, 'bigReview': big_review, 'onLimit': on_limit}
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


def start_workspace(repo, name, workspace, existing):
    """The workspace a named queue's start or append uses (#66): the saved one; on the first start the
    given one or `<repo>-<name>`. A different one later is refused, since the members already live in
    the saved one. None for the main queue."""
    if name is None:
        return None
    saved = (existing or {}).get('workspace')
    if saved and workspace and workspace.casefold() != saved.casefold():
        raise UsageError(f'queue {repo} ({name}) runs in workspace {saved!r}; it cannot move to {workspace!r}')
    workspace = saved or workspace or f'{repo.split("/")[1]}-{name}'
    if not WORKSPACE_NAME.fullmatch(workspace):
        # The default `<repo>-<name>` can be longer than a workspace name may be: never write a file
        # that no later load, this queue's or a sibling's, could read.
        raise UsageError(f'workspace {workspace!r} is not a valid workspace name (1-64 of A-Z a-z 0-9 . _ -); '
                         'pass -Workspace <name>')
    if workspace.casefold() == repo.split('/')[1].casefold():
        raise UsageError(f"workspace {workspace!r} is the repo's own, the main queue's; a named queue needs its own")
    return workspace


def check_workspace(workspace, siblings):
    """A named queue's workspace is its own: no other queue of the repo may use it (#66)."""
    for data in siblings:
        if workspace is not None and queue_workspace(data).casefold() == workspace.casefold():
            raise UsageError(f'workspace {workspace!r} belongs to queue {queue_label(data)}')


def start_queue(spec, repo=None, parallel=None, watch=False, retry=False, yes=False, dry_run=False, root=None,
                implementer=None, auto_merge=None, autonomous=None, gh=gh_json, triage_on=False,
                prune=False, clear_limit=None, name=None, workspace=None, revmux_profile=None, big_review=None,
                on_limit=None):
    name = queue_name(name)
    if workspace is not None and name is None:
        raise UsageError('-Workspace requires -QueueName: the main queue uses the workspace named after the repo')
    if workspace is not None and not WORKSPACE_NAME.fullmatch(workspace):
        raise UsageError(f'invalid workspace name {workspace!r}')
    if triage_on and name is not None:
        raise UsageError('a named queue does not triage; triage is the repo-wide '
                         'github-workbench -Triage -Repo <owner/name> -Watch')
    if revmux_profile is not None and not REVMUX_PROFILE.fullmatch(revmux_profile):
        raise UsageError(f'invalid revmux profile {revmux_profile!r}')
    repo, numbers, label = resolve_spec(expand_spec(spec, config_path()), repo, gh)
    matched = set(numbers)
    matches = len(numbers)
    if watch and not label:
        raise UsageError('-Watch requires a label:, bugs or where: spec')
    if prune and (not watch or not label):
        raise UsageError('-Prune requires -Queue with a watched label:, bugs or where: spec')
    root = Path(root or os.environ.get('AGWORKBENCH_QUEUE_ROOT', Path.home() / '.agworkbench/queues')).resolve()
    store = Store(queue_path(root, repo, name))
    if parallel is not None and not 1 <= parallel <= 8:
        raise UsageError('-Parallel must be between 1 and 8')
    if implementer not in (None, *IMPLEMENTER_TOOLS):
        raise UsageError('-Implementer must be codex, claude or kimi')
    if clear_limit not in (None, *IMPLEMENTER_TOOLS):
        raise UsageError('-ClearLimit must be codex, claude or kimi')
    existing = store.load() if store.path.exists() else None      # under the state lock, like every other read
    if existing and existing['repo'] != repo:
        # `o/docxy -QueueName kimi` is `docxy.kimi.json`, which can be repo o/docxy.kimi's main queue (#66).
        raise QueueError(f'queue repository mismatch in {store.path}: it is {queue_label(existing)}')
    workspace = start_workspace(repo, name, workspace, existing)
    tag = f'({name}) ' if name else ''
    known = {m['number']: m['state'] for m in existing['members']} if existing else {}
    pruned = [m['number'] for m in existing['members']
              if m['state'] == 'pending' and m['number'] not in matched] if existing and prune else []
    skipped = {n: f'queued ({known[n]})' for n in numbers if n in known}
    fresh = [n for n in numbers if n not in known]
    # The repo's other queues (#66), read here with no lock for the in-hand lookup and the dry run. The
    # claims that decide are read again under the claims lock, right before the write.
    siblings = sibling_queues(root, repo, store.path)
    check_workspace(workspace, siblings)
    live = live_closed(repo, siblings, fresh)          # a terminal lookup: before any lock
    if label:
        # Only a label spec is filtered: an explicit list is what the human named. Before any write.
        skipped.update(in_hand(repo, fresh, checkout_root((existing or {}).get('config') or config_path()),
                               store.path, gh, name=name, workspaces=repo_workspaces(repo, siblings, workspace)))
    # A claim holds against an explicit list too: two queues never both work on one issue.
    skip_claimed(skipped, claims(siblings, live), fresh)
    numbers = [n for n in numbers if n not in skipped]
    if dry_run:
        live = store.running() if store.worker_lock.exists() else False
        mode = 'start' if existing is None else ('append to a running queue' if live else 'append to a stopped queue')
        changes = settings_changes(existing, parallel, yes, implementer, auto_merge, autonomous, watch, label, triage_on,
                                   revmux_profile, big_review, on_limit)
        query = dict(query=label.text, matches=matches) if isinstance(label, labelquery.Query) else {}
        limits = dict(clearLimit=dict(tool=clear_limit, recorded=((existing or {}).get('toolLimits') or {}).get(clear_limit))
                      ) if clear_limit else {}
        print(json.dumps(dict(repo=repo, name=name or 'main', workspace=workspace or repo.split('/')[1], **query,
                              members=numbers, running=live, mode=mode,
                              skipped=[dict(number=n, reason=r) for n, r in sorted(skipped.items())],
                              settings=changes, pruned=pruned, owner=existing.get('owner') if existing else None,
                              **limits)))
        return 0
    token = None
    with claims_lock(root, repo):
        siblings = sibling_queues(root, repo, store.path)
        check_workspace(workspace, siblings)
        claimed = claims(siblings, live)
        with Lock(store.state_lock):
            if store.path.exists():
                data = store._load()
                if data['repo'] != repo:
                    raise QueueError(f'queue repository mismatch in {store.path}')
            else:
                data = dict(version=1, repo=repo, parallel=parallel or 1, watch=watch,
                            **(watch_fields(label) if watch else {'label': None}),
                            yes=yes, config=str(config_path()), members=[], owner=None)
                if name:
                    data.update(name=name, workspace=workspace)
            # One mapping decides and applies every switch, and is what gets reported (#28). All apply to
            # members launched from now on; autonomous/autoMerge/bigReview/onLimit are saved explicitly, false included,
            # and -Watch onto a queue started from a list turns watching on.
            changes = settings_changes(data, parallel, yes, implementer, auto_merge, autonomous, watch, label,
                                       triage_on, revmux_profile, big_review, on_limit)
            for key, (_, new) in changes.items():
                data[key] = new
            cleared = None
            if clear_limit:
                # The human says this tool's limit has reset (#61), and nothing else changes: records of it
                # from before now are ignored, so a member's old failover never brings it back.
                data.setdefault('toolLimitsClearedAt', {})[clear_limit] = time.time()
                cleared = (data.get('toolLimits') or {}).pop(clear_limit, None)
                if not data.get('toolLimits'):
                    data.pop('toolLimits', None)
            pruned = [m['number'] for m in data['members']
                      if m['state'] == 'pending' and m['number'] not in matched] if prune else []
            if pruned:
                removed = set(pruned)
                data['members'] = [m for m in data['members'] if m['number'] not in removed]
                if data.get('launchBackoff', {}).get('member') in removed:
                    data.pop('launchBackoff', None)
                    if (data.get('launchPaused') or '').startswith('launches failing: '):
                        data.pop('launchPaused', None)
            known = {m['number'] for m in data['members']}
            skip_claimed(skipped, claimed, [n for n in numbers if n not in known])
            added = [n for n in numbers if n not in known and n not in claimed]
            data['members'].extend(new_member(n, repo, checkout_root(data['config']), name) for n in added)
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
                    print(f'{tag}previous conductor session {owner["session"]} left untouched')
                data['owner'] = dict(token=token, state='starting', session=None, pinned=False, reservedAt=time.time())
            atomic_json(store.path, data)
    # Reported only once written: a refused or failed write never shows changes that did not happen.
    for n, reason in sorted(skipped.items()):
        print(f'{tag}#{n} skipped: {reason}')
    for n in pruned:
        print(f'{tag}#{n} pruned: no longer matches the watched spec')
    for key, (old, new) in changes.items():
        suffix = '' if key in ('label', 'query') else ' (for every member launched from now on)'
        print(f'{tag}settings: {key} {json.dumps(old)} -> {json.dumps(new)}{suffix}')
    if clear_limit:
        was = f' ({cleared["line"]})' if cleared else ' (none was recorded)'
        print(f'{tag}settings: cleared the recorded usage limit for {clear_limit}{was}; older records are ignored')
    if token is None:
        if owner.get('session') and not owner.get('pinned'):
            pin_conductor(store, owner)
        print(f'{tag}queue running/starting in session {owner.get("session") or "pending"}; appended {len(added)}')
        return 0
    line = conductor_command(store, token)
    args = {'name': f'#queue {queue_label(data)}', 'cwd': str(HERE.parent), 'command': line}
    if name:
        # A named queue's conductor lives in its workspace (#66), created when missing; the main
        # queue's opens beside its caller, as before.
        args.update({'workspace-name': workspace, 'create-workspace': True})
    session = str(agw.request('session.new', args=args)).split()[0]
    if not valid_uuid(session):
        raise QueueError(f'invalid conductor session id: {session}')
    with store.transaction() as data:
        if data['owner']['token'] == token:
            data['owner']['session'] = session
    pin_conductor(store, dict(session=session, token=token))
    print(f'{tag}queue running in session {session}: {store.path}')
    return 0


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
                    checkout=m['checkout'], checkoutEstablished=m['checkoutEstablished'], config=data['config'],
                    queueName=data.get('name'), workspace=queue_workspace(data))


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


def write_loop_state(root, state, pr=None, reason=None, *, loop_id=None, cause=None):
    """A loop report from the planner's Claude runtime, or - with `loop_id` - from the relay, which is
    not a Claude runtime and passes the id claude.json holds (#45: a stall it escalates). `cause`
    "environment" marks a block the human cannot answer (a limited tool, low disk or memory, #61):
    that member keeps its slot."""
    if state not in {'pr-open', 'blocked', 'resumed', 'closed'}:
        raise QueueError('invalid loop state')
    if cause not in (None, 'environment') or (cause and state != 'blocked'):
        raise QueueError('only a blocked report can have an environmental cause')
    root = Path(root)
    directory = root / '.workbench/state'
    member = read_json(directory / 'queue-member.json')
    identity = read_json(directory / 'claude.json')
    if loop_id is None:
        loop_id = os.environ.get('CLAUDE_CODE_SESSION_ID')
    if not valid_uuid(loop_id) or identity['sessionId'] != loop_id:
        raise QueueError('Claude runtime identity does not match this workbench')
    repo = repo_name(member['repo'])
    if state == 'pr-open':
        pr_url(pr, repo)
    if state in {'blocked', 'closed'} and (not isinstance(reason, str) or not reason.strip()):
        raise QueueError(f'{state} requires --reason')
    with Lock(directory / 'loop.lock'):
        path = directory / 'loop.json'
        previous = read_json(path) if path.exists() else {}
        same = previous.get('loopId') == loop_id and previous.get('queue') == member['queue']
        record = dict(queue=member['queue'], repo=repo, number=member['number'], loopId=loop_id,
                      rev=previous.get('rev', 0) + 1 if same else 1, state=state,
                      pr=None if state == 'closed' else pr or (previous.get('pr') if same else None),
                      reason=reason, at=time.time())
        if cause:
            record['cause'] = cause
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
            report.get('state') not in {'pr-open', 'blocked', 'resumed', 'closed'} or
            type(report.get('rev')) is not int or report['rev'] < 1):
        raise QueueError(f'ignored stale/foreign/malformed loop report for #{member["number"]}')
    revision = member['consumedRev'] if member['consumedLoop'] == loop else 0
    if report['rev'] <= revision:
        return False
    if report['state'] == 'pr-open':
        pr_url(report.get('pr'), data['repo'])
    if report['state'] in {'blocked', 'closed'} and (not isinstance(report.get('reason'), str) or not report['reason'].strip()):
        raise QueueError(f"{report['state']} loop report requires a reason")
    if report.get('cause') not in (None, 'environment') or (report.get('cause') and report['state'] != 'blocked'):
        raise QueueError('invalid loop report cause')
    if report.get('pr'):
        pr_url(report['pr'], data['repo'])
    # Defensive for direct callers: tick normally skips reports on merged members.
    if (report['state'] == 'pr-open' and member['state'] == 'merged' and
            member.get('prState') == 'MERGED' and
            pr_number(member.get('pr')) == pr_number(report['pr'])):
        member.update(consumedLoop=loop, consumedRev=report['rev'], slotReleased=True)
        return True
    if report.get('pr'):
        if member.get('pr') != report['pr']:
            member.update(pr=report['pr'], prState=None)
    state = 'active' if report['state'] == 'resumed' else report['state']
    if member['state'] in {'closed', 'merged'} and state not in {'closed', 'merged'}:
        # This is a live loop again; no prior close attempt or stuck reason applies.
        member.pop('closePending', None)
        member.pop('closeStuck', None)
    if member['state'] == 'closed' and state == 'closed':
        # A newer closed report starts a new no-PR completion, even after a refused close.
        member.pop('closePending', None)
        member.pop('closeStuck', None)
    member.update(phase=state, state=state, reason=report.get('reason'), consumedLoop=loop, consumedRev=report['rev'])
    member.pop('cause', None)
    member.pop('sessionGoneSince', None)        # a fresh report: the session watch starts over
    if report['state'] == 'resumed':
        member['resumed'] = True               # its slot is watched like a PR member's (#61)
    else:
        member.pop('resumed', None)
    if state == 'closed':
        member.update(pr=None, prState=None)
    if state == 'blocked' and report.get('cause'):
        member['cause'] = report['cause']      # environmental: the slot stays taken (#61)
    elif state in {'pr-open', 'blocked', 'closed'}:
        member['slotReleased'] = True
    elif state == 'active':
        member['slotReleased'] = False         # resumed: live work again, counted again (#61)
    return True


def adopt_done(data, member):
    """Recover a current attempt's PR when the planner skipped its queue report."""
    if member['state'] not in {'active', 'blocked'} or member.get('pr'):
        return False
    path = Path(member['checkout']) / '.workbench/state/loop-done.json'
    if not path.exists():
        return False
    done = read_json(path)
    started = member.get('startedAt')
    at = done.get('at') if isinstance(done, dict) else None
    number = done.get('pr') if isinstance(done, dict) else None
    if (not isinstance(done, dict) or done.get('noPr') is True or
            type(started) not in (int, float) or not math.isfinite(started) or
            type(at) not in (int, float) or not math.isfinite(at) or at < started or
            type(number) is not int or number <= 0):
        return False
    member.update(pr=f"https://github.com/{data['repo']}/pull/{number}", prState=None,
                  state='pr-open', phase='pr-open', reason=None, slotReleased=True)
    return True


def summary(data):
    lines = [f'Queue {queue_label(data)} — snapshot at {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}',
             'Rerun github-workbench -Queue <spec> to refresh.', '', '| Issue | State | PR | Reason |', '|---|---|---|---|']
    for m in data['members']:
        state = m['state'] + (' (PR closed)' if m.get('prState') == 'CLOSED' else '')
        if m.get('limitWait'):
            state = f'waiting-limit ({state})'      # #77: an overlay, the state is unchanged
        values = [str(m['number']), state, m.get('pr') or '', m.get('reason') or '']
        lines.append('| ' + ' | '.join(str(v).replace('|', '\\|').replace('\n', ' ') for v in values) + ' |')
    counts = {state: sum(m['state'] == state for m in data['members']) for state in STATES}
    lines.insert(2, 'Counts: ' + ', '.join(f'{state} {counts[state]}' for state in
                 ('merged', 'closed', 'pr-open', 'blocked', 'failed', 'active', 'launching', 'pending')))
    return '\n'.join(lines) + '\n'


class Worker:
    def __init__(self, store, token, *, gh=gh_json, clock=time.time, spawn=None, spawn_triage=None, disk_free=None,
                 ram_free=None):
        self.store, self.token, self.gh, self.clock = store, token, gh, clock
        self.disk_free = disk_free or free_bytes
        self.ram_free = ram_free or free_ram
        self.disk_announced = False   # whether this worker has announced a disk pause (#41)
        self.ram_announced = False    # ... a memory pause (#61)
        self.tools_announced = False  # ... a tool-limits pause (#61)
        self.waits_announced = ()     # the members whose usage-limit wait this worker printed (#77)
        self.idle_announced = False   # whether this worker said the watched queue has nothing left (#82)
        self.tick_sessions = _UNREAD  # issue numbers with a live session, read at most once per tick
        self.launch_announced = False
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
        self._name = _UNREAD          # the queue's name (#66), read once: it never changes

    def error(self, key, err):
        count = self.errors.get(key, 0) + 1
        self.errors[key] = count
        if count == 1:
            print(f'{key}: {err}', flush=True)
        if count == 3:
            self.alerts.append(f'{key}: {err}')

    def cleanup_launch(self, checkout, token=None):
        """Close only sessions recorded as created by an interrupted queue launch."""
        path = Path(checkout) / '.workbench' / 'state' / 'queue-launch.json'
        if not path.exists():
            return
        try:
            record = read_json(path)
            if token is not None and record.get('token') != token:
                return
            sessions = record.get('sessions', [])
            before = agw.tree()
            for session_id in sessions:
                found = agw.find_session(session_id, before)
                if not found:
                    continue
                _, session = found
                for pane in agw.panes_of(session):
                    try:
                        agw.clear_restore(pane)
                    except (agw.CtlError, OSError) as err:
                        self.error(f'cleanup restore {pane}', err)
                try:
                    agw.close_session(session_id)
                except (agw.CtlError, OSError) as err:
                    self.error(f'cleanup session {session_id}', err)
            after = agw.tree()
            still = {s['id'] for _, s in agw.sessions(after)}
            record['sessions'] = [sid for sid in sessions if sid in still]
            if record['sessions']:
                atomic_json(path, record)
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError, agw.CtlError) as err:
            self.error(f'cleanup launch {checkout}', err)

    @property
    def tag(self):
        """`(kimi) ` for a named queue's lines and titles (#66): two conductors print into one terminal."""
        if self._name is _UNREAD:
            try:
                self._name = self.store.load().get('name')
            except (OSError, ValueError):
                return ''                 # an unreadable queue: its own error says which
        return f'({self._name}) ' if self._name else ''

    def notify(self, message):
        try:
            agw.notify(agw.my_pane() or 'active', message, title=f'Workbench queue {self.tag}'.strip())
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
        if data.get('revmuxProfile'):
            args += ['-RevmuxProfile', data['revmuxProfile']]     # the human's explicit one (#66)
        if data.get('autonomous') is not None:
            args.append('-Autonomous' if data['autonomous'] else '-NoAutonomous')
        if data.get('autoMerge') is not None and not (data.get('autonomous') and not data['autoMerge']):
            args.append('-AutoMerge' if data['autoMerge'] else '-NoAutoMerge')
        if data.get('bigReview') is not None:
            args.append('-BigReview' if data['bigReview'] else '-NoBigReview')     # #75
        if data.get('onLimit') is not None:
            args.append('-WaitOnLimit' if data['onLimit'] == 'wait' else '-NoWaitOnLimit')     # #77
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
            missing = bool(previous and previous['state'] == 'launching' and
                           previous.get('token') == job['token'] and previous['attempt'] == job['attempt'] and
                           not previous.get('result') and code != 75)
            if missing:
                self.cleanup_launch(previous['checkout'], job['token'])
            established = bool(previous and (previous['checkoutEstablished'] or usable_checkout(previous['checkout'])))
            with self.store.transaction() as data:
                m = find_member(data, number)
                if m and m.get('token') == job['token'] and m['attempt'] == job['attempt'] and m['state'] == 'launching':
                    if not m.get('result') and code != 75:
                        m['result'] = dict(result='timeout' if timed_out else 'failed', infra=True,
                                           stage='launcher', token=job['token'],
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
            self.refresh_stale(data)
        if data['watch'] and self.clock() >= self.next_scan:
            self.next_scan = self.clock() + 300
            try:
                _, numbers, _ = resolve_spec(watched_spec(data), data['repo'], self.gh)
                known = {m['number'] for m in data['members']}
                fresh = [n for n in numbers if n not in known]
                root = self.store.root
                siblings = sibling_queues(root, data['repo'], self.store.path)
                skipped = in_hand(data['repo'], fresh, checkout_root(data['config']), self.store.path, self.gh,
                                  name=data.get('name'),
                                  workspaces=repo_workspaces(data['repo'], siblings, queue_workspace(data)))
                live = live_closed(data['repo'], siblings, fresh)
                accepted = False
                # The claims (#66) decide under the repo's claims lock, taken before our state lock.
                with claims_lock(root, data['repo']):
                    skip_claimed(skipped, claims(sibling_queues(root, data['repo'], self.store.path), live), fresh)
                    with self.store.transaction() as current:
                        if current['watch'] and watch_key(current) == watch_key(data):
                            known = {m['number'] for m in current['members']}
                            current['members'].extend(new_member(n, data['repo'], checkout_root(data['config']),
                                                                 data.get('name'))
                                                      for n in fresh if n not in known and n not in skipped)
                            accepted = True
                if accepted:
                    for n, reason in sorted(skipped.items()):
                        if self.last_skips.get(n) != reason:
                            print(f'{self.tag}#{n} skipped: {reason}', flush=True)
                    self.last_skips = skipped
                self.errors.pop('label scan', None)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError, QueueError, agw.CtlError) as err:
                self.error('label scan', err)

    def refresh_stale(self, data):
        """Resolve a lost active loop only after its sessions have been absent for a grace period."""
        now = self.clock()
        candidates = [m for m in data['members'] if m['state'] == 'active' and not m.get('pr') and
                      type(m.get('startedAt')) in (int, float) and math.isfinite(m['startedAt']) and
                      (now - m['startedAt'] >= 1800 or m.get('goneSince') is not None)]
        if not candidates:
            return
        try:
            import cleanup
            tree = agw.tree()
        except (OSError, ValueError, KeyError, TypeError, agw.CtlError) as err:
            self.error('stale sessions', err)
            return
        for m in candidates:
            key = f'stale #{m["number"]}'
            try:
                live = cleanup.live_sessions(data['repo'], m['number'], tree, queue_workspace(data))
                if live:
                    with self.store.transaction() as current:
                        member = find_member(current, m['number'])
                        if member and member['state'] == 'active':
                            member.pop('goneSince', None)
                    self.errors.pop(key, None)
                    continue
                with self.store.transaction() as current:
                    member = find_member(current, m['number'])
                    if not member or member['state'] != 'active' or member.get('pr'):
                        continue
                    if type(member.get('goneSince')) not in (int, float):
                        member['goneSince'] = now
                    gone = member['goneSince']
                if now - gone < 1800:
                    continue
                checkout = Path(m['checkout'])
                relay = checkout / '.workbench/state/relay.json'
                saved = read_json(relay) if relay.exists() else {}
                branch = saved.get('branch', '') if isinstance(saved, dict) else ''
                if not branch:
                    result = subprocess.run([shutil.which('git') or 'git', '-C', str(checkout),
                                             'branch', '--show-current'], capture_output=True, text=True,
                                            timeout=30)
                    branch = result.stdout.strip() if result.returncode == 0 else ''
                if (not isinstance(branch, str) or
                        not re.fullmatch(r'issue-' + str(m['number']) + r'(?:-.*)?', branch)):
                    raise QueueError(f'cannot identify issue branch for #{m["number"]}: {branch!r}')
                prs = self.gh('pr', 'list', '--repo', data['repo'], '--head', branch,
                              '--state', 'all', '--json', 'number,state,url,isCrossRepository')
                if not isinstance(prs, list) or any(not isinstance(p, dict) or
                    type(p.get('number')) is not int or p['number'] <= 0 or
                    p.get('state') not in {'OPEN', 'CLOSED', 'MERGED'} or
                    type(p.get('isCrossRepository')) is not bool for p in prs):
                    raise QueueError('invalid PR list response')
                local_prs = [p for p in prs if not p['isCrossRepository']]
                if any(p['state'] == 'OPEN' for p in local_prs):
                    self.errors.pop(key, None)
                    continue
                merged = next((p for p in local_prs if p['state'] == 'MERGED'), None)
                issue = None if merged else self.gh('issue', 'view', str(m['number']),
                                                    '--repo', data['repo'], '--json', 'state')
                if issue is not None and (not isinstance(issue, dict) or issue.get('state') not in {'OPEN', 'CLOSED'}):
                    raise QueueError('invalid issue response')
                # The original snapshot may predate a newly opened review/helper session.
                if cleanup.live_sessions(data['repo'], m['number'], agw.tree(), queue_workspace(data)):
                    with self.store.transaction() as current:
                        member = find_member(current, m['number'])
                        if member and member['state'] == 'active':
                            member.pop('goneSince', None)
                    self.errors.pop(key, None)
                    continue
                with self.store.transaction() as current:
                    member = find_member(current, m['number'])
                    if not member or member['state'] != 'active' or member.get('pr'):
                        continue
                    if merged:
                        member.update(pr=f"https://github.com/{data['repo']}/pull/{merged['number']}",
                                      prState='MERGED', state='merged', phase='pr-open', slotReleased=True)
                    elif issue['state'] == 'CLOSED':
                        member.update(state='closed', phase='closed', slotReleased=True,
                                      reason='stale: issue closed and no session remains')
                self.errors.pop(key, None)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, agw.CtlError) as err:
                self.error(key, err)

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
            print(f'{self.tag}#{number}: triage {"done" if code == 0 else "failed; admitted untriaged"} '
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
    # The relay closes a merged or no-PR closed member's sessions. When it is gone (killed, closed, never
    # restarted) and its close has been pending for CLOSE_BACKSTOP_AFTER, the conductor runs the same
    # close (closer.py) one step per tick. While the relay is alive it only flags `closeStuck`: one
    # closer at a time. Never on a timeout alone: after CLOSE_WAIT only unread pre-close implementer
    # mail is overridden (#44). A relay whose own close gave up in queue mode hands it over (#44):
    # it keeps close_pending, records close_handoff and closes its session, and this retries it once.

    def mark(self, number, **fields):
        with self.store.transaction() as data:
            member = find_member(data, number)
            if member is None:
                return
            for key, value in fields.items():
                if value is None:
                    member.pop(key, None)
                else:
                    member[key] = value

    def close_backstop(self):
        data = self.store.load()
        for m in data['members']:
            if m['state'] not in {'merged', 'closed'}:
                self.closes.pop(m['number'], None)
                continue
            number = m['number']
            key = close_key(m)
            pr = closer.pending_number(key)
            hub_dir = Path(m['checkout']) / '.workbench'
            try:
                relay_state = read_json(hub_dir / 'state' / 'relay.json') if (hub_dir / 'state' / 'relay.json').exists() else {}
            except (OSError, ValueError):
                relay_state = {}
            if m['state'] == 'closed' and relay_state.get('close_pending') != closer.NO_PR:
                try:
                    done = read_json(hub_dir / 'state' / 'loop-done.json')
                    at = done.get('at') if isinstance(done, dict) else None
                    last = m.get('closedAt')
                    if (isinstance(done, dict) and done.get('noPr') is True and done.get('pr') is None
                            and str(done.get('issue')) == str(number) and type(last) in (int, float)
                            and type(at) in (int, float)
                            and at > last and not closer.relay_alive(data['repo'], str(number), agw.tree(),
                                                                    queue_workspace(data))):
                        relay_state['close_pending'] = closer.NO_PR
                        relay_state['close_merged_at'] = datetime.fromtimestamp(at, timezone.utc).isoformat()
                        atomic_json(hub_dir / 'state' / 'relay.json', relay_state)
                        self.mark(number, closePending=True, closeStuck=None)
                except (OSError, ValueError, OverflowError, agw.CtlError):
                    pass  # An unreadable record or relay state is never a reason to re-arm.
            if key is None or relay_state.get('close_pending') != key:
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
        if closer.relay_alive(data['repo'], str(number), agw.tree(), queue_workspace(data)):
            if watch['attempt'] is not None:
                watch['attempt'].log('the relay is back; the conductor leaves the close to it')
                watch['attempt'] = None
            if not m.get('closeStuck'):
                print(f'{self.tag}#{number}: {RELAY_ALIVE}', flush=True)
                self.mark(number, closeStuck=RELAY_ALIVE)
            return
        if watch['attempt'] is None:
            watch.pop('issue_result', None)
            watch.pop('next_issue_check', None)
            watch.pop('no_pr_at', None)
            registry = read_json(hub_dir / 'state' / 'agents.json')['agents']
            peers = [types.SimpleNamespace(box=box, tool=registry[box].get('tool', box), pane=registry[box]['pane'])
                     for box in ('claude', 'codex')]
            watch['attempt'] = closer.Closer(hub_dir, data['repo'], number, peers, workspace=queue_workspace(data),
                                             log=lambda text: print(f'{self.tag}#{number} {text}', flush=True), clock=self.clock)
            subject = f'PR #{pr} (issue #{number}) merged' if pr is not None else f'issue #{number} closed without a PR'
            watch['attempt'].log(f'{subject} and its relay is gone; the conductor runs the close')
            if str(m.get('closeStuck', '')).startswith(RELAY_ALIVE):
                # The relay went (or handed its close over, #44): the flag is resolved, and a flagged
                # member would let the conductor finish in the middle of this attempt.
                self.mark(number, closeStuck=None)
        attempt = watch['attempt']
        def refuse(reason, *, stuck=None, live_loop=False):
            attempt.log(f'NOT closing: {reason}')
            self.notify(f'#{number}: autonomous close stopped: {reason}')
            if live_loop:
                try:
                    if retire_no_pr_done(hub_dir / 'state' / 'loop-done.json',
                                         expected_at=watch.get('no_pr_at'), issue=number):
                        attempt.log('preserved refused no-PR completion as loop-done-refused.json')
                except (OSError, ValueError) as err:
                    attempt.log(f'could not retire refused no-PR completion: {err}')
            self.end_close(m, pr, hub_dir, stuck=stuck or reason)

        def no_pr_preclose():
            """Fresh gates: (verdict, reason); None means ready, adopted re-gates next tick."""
            closed, detail = attempt.issue_closed(self.gh)
            watch['issue_result'] = (closed, detail)
            watch['next_issue_check'] = self.clock() + CLOSE_ISSUE_CHECK_INTERVAL
            if closed is False:
                return 'refuse', f'issue #{number} was reopened'
            if closed is None:
                return 'wait', f'issue state unknown: {detail}'
            relay_state = read_json(hub_dir / 'state' / 'relay.json')
            branch = relay_state.get('branch')
            open_pr, detail = attempt.open_prs(self.gh, branch)
            if open_pr is True:
                return 'refuse', detail
            if open_pr is None:
                watch['issue_result'] = (None, detail)
                return 'wait', detail
            current_done = attempt.no_pr_done_record()
            if current_done is None:
                return 'refuse', 'no-PR completion changed during the close'
            if current_done.get('at') != watch.get('no_pr_at'):
                watch['no_pr_at'] = current_done.get('at')
                return 'adopted', 'new no-PR completion needs a fresh close check'
            return None, None

        if pr is None:
            done_record = attempt.no_pr_done_record()
            if done_record is None:
                if attempt.timed_out():
                    refuse('the planner has not recorded `wb.py loop-state done --no-pr`')
                return
            watch.setdefault('no_pr_at', done_record.get('at'))
            if self.clock() >= watch.get('next_issue_check', 0):
                watch['issue_result'] = attempt.issue_closed(self.gh)
                watch['next_issue_check'] = self.clock() + CLOSE_ISSUE_CHECK_INTERVAL
            closed, detail = watch['issue_result']
            if closed is False:
                refuse(f'issue #{number} was reopened', live_loop=True)
                return
            if closed is None:
                if attempt.timed_out():
                    refuse(f'issue state unknown: {detail}')
                return
        attempt.step_helpers(gate=(lambda: attempt.issue_closed(self.gh)[0] is True) if pr is None else None)
        try:
            # #96: a pointer for mail still unread is submitted; one nobody needs to read is cleared.
            attempt.rescue_pointers()
            attempt.clear_stale_pointers(pr)
        except Exception as err:  # noqa: BLE001 - the blockers below still decide
            attempt.log(f'pointer check failed: {type(err).__name__}: {err}')
        reasons = attempt.agent_blockers(pr)
        if not reasons or attempt.overdue_ok():
            if attempt.autonomous():
                if pr is None:
                    verdict, reason = no_pr_preclose()
                    if verdict == 'adopted':
                        return
                    if verdict == 'refuse' or (verdict == 'wait' and attempt.timed_out()):
                        refuse(reason, live_loop=verdict == 'refuse')
                    if verdict is not None:
                        return
                attempt.close_issue_session()
                attempt.start_cleanup(pr)
            else:
                attempt.log('NOT closing: autonomy was turned off')
            self.end_close(m, pr, hub_dir, stuck=None)
        elif attempt.timed_out():
            detail = '; '.join(reasons)
            refuse(f'still waiting after {closer.CLOSE_WAIT:.0f}s: {detail}',
                   stuck='the backstop close timed out: ' + detail)

    def end_close(self, m, pr, hub_dir, stuck):
        path = hub_dir / 'state' / 'relay.json'
        state = read_json(path)
        if state.get('close_pending') == closer.pending_key(pr):
            # Only the close this attempt ran: a relay may have rewritten the file since.
            state.pop('close_pending')
            state.pop('close_merged_at', None)
            state.pop('close_handoff', None)
            atomic_json(path, state)
        watch = self.closes.pop(m['number'], None) or {}
        fields = {'closePending': None, 'closeStuck': stuck}
        if pr is None and type(watch.get('no_pr_at')) in (int, float):
            fields['closedAt'] = watch['no_pr_at']
        self.mark(m['number'], **fields)

    def disk_pause(self, config):
        """Why admissions are paused for disk space (#41), or None. Low disk must never fail members:
        a clone that cannot be written fails, so none is started until space returns."""
        try:
            minimum = min_free_gb(config)
            if not minimum:
                return None
            free, drive = self.disk_free(checkout_root(config))
        except (OSError, ValueError) as err:
            return f'disk check failed: {err}'
        if free < minimum * GIB:
            return f'low disk: {free / GIB:.1f} GB free < {minimum:g} GB on {drive}'
        return None

    def ram_pause(self, config):
        """Why admissions are paused for memory (#61), or None. Like the disk guard, it fails nothing."""
        try:
            minimum = min_free_ram_gb(config)
            if not minimum:
                return None
            free = self.ram_free()
        except (OSError, ValueError) as err:
            return f'memory check failed: {err}'
        if free is not None and free < minimum * GIB:
            return f'low memory: {free / GIB:.1f} GB free < {minimum:g} GB'
        return None

    def live_numbers(self, data):
        """Issue numbers with a live issue session, read once per tick and only when needed; None when
        the terminal cannot be read. session_live passes None on: the ceiling then counts every member
        as live, and slot decisions hold a slot whose grace has not started and otherwise change nothing."""
        if self.tick_sessions is _UNREAD:
            try:
                # Only this queue's workspace (#66): its own members' sessions, and no sibling is read.
                self.tick_sessions = session_numbers(data['repo'], agw.tree(), [queue_workspace(data)])
                self.errors.pop('live sessions', None)
            except (OSError, ValueError, KeyError, TypeError, agw.CtlError) as err:
                self.error('live sessions', err)
                self.tick_sessions = None
        return self.tick_sessions

    def session_live(self, data, m):
        """The one answer to "is this member's issue session there" (#61): None when the terminal
        cannot be read (nothing is stamped or cleared then), True when it is seen or has been missing
        for less than SESSION_GRACE seconds - one blank read of a restarting terminal changes
        nothing - and False after that. `sessionGoneSince` records when it was first missed."""
        live = self.live_numbers(data)
        if live is None:
            return None
        if m['number'] in live:
            m.pop('sessionGoneSince', None)
            return True
        since = m.get('sessionGoneSince')
        if type(since) not in (int, float):
            since = m['sessionGoneSince'] = self.clock()
        return self.clock() - since < SESSION_GRACE

    def hold_environment(self, data, m):
        """A blocked member keeps its slot while the cause is environmental - its report says so, or
        its relay announced a usage limit, or its relay record cannot be read - and its session is
        live (slot_by_session). A question for the human frees it at once."""
        key = f'environment #{m["number"]}'
        try:
            environmental = m.get('cause') == 'environment' or relay_limited(m)
            self.errors.pop(key, None)
        except (OSError, ValueError, AttributeError) as err:
            self.error(key, err)
            environmental = True               # unknown: kept, and the session grace still ends it
        if environmental:
            self.slot_by_session(data, m)
        else:
            m['slotReleased'] = True           # its session's stamp stays: the ceiling reads it

    def slot_by_session(self, data, m):
        """The slot follows the member's session, re-decided every tick (#61): held while
        session_live, released once it is not, taken back when the session is seen again. An
        unreadable terminal changes nothing. Used for environmental blocks, and for active members
        with a PR or a resumed loop, whose slots refresh_stale never reclaims (it skips members with
        a PR, and an open issue without one stays active). An unreadable terminal holds the slot while
        no grace has started (the session was last seen) and otherwise changes nothing."""
        live = self.session_live(data, m)
        if live is not None:
            m['slotReleased'] = not live
        elif 'sessionGoneSince' not in m:
            m['slotReleased'] = False

    def forget_stale_stamps(self, data):
        """After a readable tree, a member whose session is there has no gone-since stamp, whether or
        not anything asked about it this tick: a stamp from an old blank read must not count later."""
        if isinstance(self.tick_sessions, set):
            for m in data['members']:
                if m['number'] in self.tick_sessions:
                    m.pop('sessionGoneSince', None)

    def collect_tool_limits(self, data):
        """Merge live members' recorded usage limits into the queue's toolLimits (#61). A record not
        newer than the human's last `-ClearLimit <tool>` is ignored; nothing here ever clears one."""
        limits = data.get('toolLimits') or {}
        cleared = data.get('toolLimitsClearedAt') or {}
        for m in data['members']:
            if m['state'] not in LIVE_STATES or not m['checkoutEstablished']:
                continue
            key = f'limits #{m["number"]}'
            try:
                found = member_limits(m)
                self.errors.pop(key, None)
            except (OSError, ValueError, AttributeError) as err:
                self.error(key, err)
                continue
            for tool, at, line, kind in found:
                if at > cleared.get(tool, 0) and tool not in limits:
                    limits[tool] = dict(at=at, line=line, kind=kind, member=m['number'])
                    self.alerts.append(f'#{m["number"]}: {tool} {kind}: new members avoid it until '
                                       f'-Queue ... -ClearLimit {tool} clears it')
        if limits:
            data['toolLimits'] = limits

    def collect_limit_waits(self, data):
        """Stamp `limitWait` on each live member that waits out a usage limit (#77), clear it on the rest;
        the members that wait. A member whose session is gone waits for nothing: it cannot pin the queue."""
        waiting = []
        for m in data['members']:
            found = None
            if m['state'] in LIVE_STATES and m['checkoutEstablished']:
                key = f'limit wait #{m["number"]}'
                try:
                    found = limit_wait(m, self.clock())
                    self.errors.pop(key, None)
                except (OSError, ValueError, AttributeError) as err:
                    self.error(key, err)
                if found and self.session_live(data, m) is False:
                    found = None
            if found:
                m['limitWait'] = found
                waiting.append(m)
            else:
                m.pop('limitWait', None)
        return waiting

    def live_count(self, data):
        """Members with running agent sessions (#61): launching ones always, active ones that hold a
        slot, and active, blocked, pr-open and close-pending ones while session_live is not False -
        an unreadable terminal counts them all, and a blank read counts them until the grace ends."""
        count = 0
        for m in data['members']:
            if m['state'] == 'launching' or (m['state'] == 'active' and not m['slotReleased']):
                count += 1
            elif ((m['state'] in {'active', 'blocked', 'pr-open'} or m.get('closePending')) and
                  self.session_live(data, m) is not False):
                count += 1
        return count

    def tick(self):
        self.tick_sessions = _UNREAD
        self.poll_jobs()
        with self.store.transaction() as data:
            for m in data['members']:
                result = m.get('result')
                if m['state'] == 'launching' and result and result.get('attempt') == m['attempt'] and result.get('token') == m.get('token'):
                    ok = result['result'] == 'ok'
                    infra = not ok and (result.get('infra') is True or result['result'] == 'timeout')
                    detail = ((result.get('detail') or '').splitlines() or [''])[0]
                    reason = f"launch deferred: {result.get('stage') or result['result']}: {detail}" if infra else result.get('detail')
                    m.update(state='active' if ok else 'pending' if infra else 'failed',
                             launchResult=result['result'], reason=reason)
                    for key in ('sessionId', 'claudePane', 'codexPane', 'relaySession'):
                        if key in result:
                            m[key] = result[key]
                    if ok:
                        backoff = data.get('launchBackoff')
                        if not backoff or m['number'] == backoff['member'] or m.get('startedAt', 0) >= backoff['until']:
                            data.pop('launchBackoff', None)
                            data.pop('launchPaused', None)
                    elif infra:
                        defer_launch(data, m, reason, self.clock())
                    else:
                        m['slotReleased'] = True
                if m['state'] in {'active', 'pr-open', 'blocked', 'closed'}:
                    try:
                        if apply_loop(data, m, self.store.path):
                            self.next_pr = 0
                            self.errors.pop(f'loop #{m["number"]}', None)
                    except (OSError, ValueError, KeyError, TypeError) as err:
                        # A partial/unrelated report never turns into an admission signal.
                        self.error(f'loop #{m["number"]}', err)
                if m['state'] == 'blocked':
                    self.hold_environment(data, m)
                elif m['state'] == 'active' and (m.get('pr') or m.get('resumed')):
                    self.slot_by_session(data, m)      # a PR or resumed loop's slot (#61)
                if m['state'] in {'active', 'blocked'} and not m.get('pr'):
                    try:
                        if adopt_done(data, m):
                            self.next_pr = 0
                            self.errors.pop(f'done #{m["number"]}', None)
                    except (OSError, ValueError, KeyError, TypeError) as err:
                        self.error(f'done #{m["number"]}', err)
        self.refresh_remote()
        self.refresh_priorities()
        self.step_triage()
        self.close_backstop()
        launches = []
        orphan_timeouts = []
        config = self.store.load()['config']
        disk, ram = self.disk_pause(config), self.ram_pause(config)
        with self.store.transaction() as data:
            self.collect_tool_limits(data)
            route, tools = tool_route(data)
            # A member waiting out a usage limit keeps its slot, and nobody new starts meanwhile (#77).
            # Not a pause: nothing is wrong, so no notification and no blocked status.
            waiting = self.collect_limit_waits(data)
            # Announced on pause and on resume, by this worker (a restarted conductor announces a pause it
            # finds); the figures in the text are refreshed silently.
            for key, reason in (('diskPaused', disk), ('ramPaused', ram), ('toolsPaused', tools)):
                if reason:
                    data[key] = reason
                else:
                    data.pop(key, None)
            # Low disk or memory, or no usable tool, never fails a member (#41, #61): one gate for all.
            pause = disk or ram or tools
            count = sum(m['state'] in {'launching', 'active', 'blocked'} and not m['slotReleased']
                        for m in data['members'])
            live = sum(m['state'] == 'launching' or m['state'] in LIVE_STATES or bool(m.get('closePending'))
                       for m in data['members'])
            ceiling = data['parallel'] + CEILING_EXTRA
            # While triage is paused (a usage limit, or facts it could not read) nobody waits for it.
            paused = self.clock() < self.triage_paused_until
            backoff = data.get('launchBackoff')
            pending = sorted((m for m in data['members'] if m['state'] == 'pending'), key=admission_key)
            if backoff:
                preferred = next((m for m in pending if m['number'] == backoff['member']), None)
                if preferred:
                    pending.remove(preferred)
                    pending.insert(0, preferred)
            for m in pending:
                launch_in_flight = any(other['state'] == 'launching' for other in data['members'])
                if pause or waiting or (backoff and (self.clock() < backoff['until'] or launch_in_flight)):
                    break                  # disk/memory/tools/limit wait/back-off/probe gate: keep other members pending
                if count >= data['parallel'] or (awaits_triage(data, m) and not paused):
                    break                  # strictly in order: nothing behind a member still being triaged
                # The ceiling (#61): the terminal is read only when the members could reach it.
                if live >= ceiling and self.live_count(data) >= ceiling:
                    break
                if m['checkoutEstablished'] and not Path(m['checkout']).is_dir():
                    m.update(state='failed', slotReleased=True, reason=f'checkout moved or deleted: {m["checkout"]}; restore it or remove the member')
                    continue
                m.update(state='launching', attempt=m['attempt'] + 1, token=str(uuid.uuid4()),
                         result=None, startedAt=self.clock(), slotReleased=False)
                launches.append(dict(m))
                count += 1
                live += 1
            admitted = {m['number'] for m in launches}
            for m in data['members']:
                if m['state'] == 'launching' and m['number'] not in self.jobs and not m.get('result') and m['number'] not in admitted:
                    # A predecessor may still be running after its conductor died. The
                    # checkout lock or fresh durable start intent gives it time to report.
                    if pause:
                        # Low disk or memory never fails a member (#41, #61): no re-spawn, and its window
                        # restarts, so it is re-spawned, not timed out, once the pause ends.
                        m['startedAt'] = self.clock()
                    elif self.clock() - m['startedAt'] >= 600:
                        unlocked = (not file_locked(self.store.directory / f'member-{m["number"]}.lock') and
                                    not checkout_locked(Path(m['checkout'])))
                        if unlocked or self.clock() - m['startedAt'] >= 1800:
                            orphan_timeouts.append((m['number'], m['attempt'], m.get('token'), m['checkout'], unlocked))
                    elif not file_locked(self.store.directory / f'member-{m["number"]}.lock') and not checkout_locked(Path(m['checkout'])):
                        launches.append(dict(m))
            self.forget_stale_stamps(data)
            settings = dict(data)
            if route:
                settings['implementer'] = route     # this launch only; the queue's setting is the human's
                # A profile chosen for the queue's tool is not the routed tool's (#66): that one's default applies.
                settings.pop('revmuxProfile', None)
        for number, attempt, token, checkout, cleanup in orphan_timeouts:
            if cleanup:
                self.cleanup_launch(checkout, token)
            with self.store.transaction() as data:
                m = find_member(data, number)
                if m and m['state'] == 'launching' and m['attempt'] == attempt and m.get('token') == token and not m.get('result'):
                    if cleanup:
                        reason = 'launch deferred: timeout: interrupted launcher produced no result'
                        defer_launch(data, m, reason, self.clock())
                    else:
                        m.update(state='failed', slotReleased=True,
                                 reason='interrupted launcher still holds the member or checkout lock after 1800 s; stop it and use -Retry')
                    m['launchResult'] = 'timeout'
        for m in launches:
            try:
                self.jobs[m['number']] = self.spawn(settings, m)
            except (OSError, ValueError) as err:
                member_result(self.store.path, m['number'], m['attempt'], m['token'],
                              dict(result='failed', infra=isinstance(err, OSError),
                                   stage='launcher', detail=str(err)))
        current = self.store.load()
        launch_paused = bool(current.get('launchPaused'))
        changed = False
        for attribute, reason, resumed in (('disk_announced', disk, 'disk space is back'),
                                           ('ram_announced', ram, 'memory is back'),
                                           ('tools_announced', tools, 'a usable tool is back'),
                                           ('launch_announced', current.get('launchPaused'), 'launches succeeding')):
            if bool(reason) != getattr(self, attribute):
                setattr(self, attribute, bool(reason))
                message = f'queue paused: {reason}' if reason else f'queue resumed: {resumed}'
                print(self.tag + message, flush=True)
                self.notify(message)
                changed = True
        if changed:
            self.status('blocked' if pause or launch_paused else 'active')
        # A limit wait (#77) is printed when it starts and when it ends, never notified.
        waits = tuple(sorted(m['number'] for m in current['members'] if m.get('limitWait')))
        if waits != self.waits_announced:
            if waits:
                texts = '; '.join(wait_text(m) for m in current['members'] if m['number'] in waits)
                print(f'{self.tag}waiting: {texts}; no new member starts meanwhile', flush=True)
            else:
                print(f'{self.tag}queue resumed: usage limit cleared', flush=True)
            self.waits_announced = waits
        # A watching queue with nothing left to start says so once (#82). While the last scan failed, nothing
        # is known: no claim that the spec is empty, and no reset (a passing gh error does not repeat it).
        # Not a pause: no notification, no status change.
        if 'label scan' not in self.errors:
            idle = current['watch'] and not any(m['state'] in {'pending', 'launching'} for m in current['members'])
            if idle and not self.idle_announced:
                print(f'{self.tag}idle: no issues left for {watched_spec(current)}', flush=True)
            self.idle_announced = idle
        for m in current['members']:
            display = (m['state'], m.get('prState'), m.get('reason'))
            shown = display + (bool(m.get('limitWait')),)
            if self.last_display.get(m['number']) != shown:
                suffix = ' waiting-limit' if m.get('limitWait') else ''
                print(f'{self.tag}#{m["number"]}: {display[0]}{suffix} {display[1] or ""} {display[2] or ""}', flush=True)
                # Only a change of state or reason notifies: a limit wait coming or going does not.
                if m['state'] in {'blocked', 'failed'} and (self.last_display.get(m['number']) or ())[:3] != display:
                    self.notify(f'#{m["number"]}: {m["state"]}: {m.get("reason") or ""}')
                self.last_display[m['number']] = shown
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


def close_key(m):
    return closer.NO_PR if m['state'] == 'closed' else pr_number(m.get('pr'))


def handed_off(m):
    """A relay handed this member's close to the conductor (#44): its relay.json holds close_handoff
    and close_pending for the close key (PR number or "no-pr"). Read under the queue's state lock by run(), so a relay that
    saw this conductor running before closing its session is never left without one."""
    pr = close_key(m)
    if pr is None:
        return False
    try:
        state = read_json(Path(m['checkout']) / '.workbench' / 'state' / 'relay.json')
    except (OSError, ValueError):
        return False
    return (isinstance(state, dict) and state.get('close_pending') == pr
            and isinstance(state.get('close_handoff'), dict) and state['close_handoff'].get('pr') == pr)


def finished(data):
    # A merged or no-PR closed member whose close is still pending keeps the conductor up for the backstop (#33),
    # unless it is flagged stuck (then it is the human's, and never keeps the queue alive forever).
    if any(m.get('closePending') and not m.get('closeStuck') for m in data['members']):
        return False
    if data['watch'] or any(m['state'] == 'pending' or m['state'] == 'launching' or
                            (m['state'] in {'active', 'blocked'} and not m['slotReleased']) for m in data['members']):
        return False
    # A close handed over by its relay (#44) keeps it up too, before the member is even seen merged.
    # Read only when it would otherwise finish: every relay.json, under the queue's state lock.
    return not any(handed_off(m) for m in data['members'])


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
    for flag in ('watch', 'retry', 'yes', 'dry-run', 'triage', 'prune'):
        start.add_argument('--' + flag, action='store_true')
    start.add_argument('--implementer', choices=IMPLEMENTER_TOOLS)
    start.add_argument('--name', help='a named queue of the repo (#66); omitted: the main queue')
    start.add_argument('--workspace', help="the named queue's agwinterm workspace (#66)")
    start.add_argument('--revmux-profile', help="the queue's revmux profile, passed to every member launch (#66)")
    start.add_argument('--clear-limit', choices=IMPLEMENTER_TOOLS,
                       help="forget the queue's recorded usage limit of this tool (#61)")
    merge = start.add_mutually_exclusive_group()
    merge.add_argument('--auto-merge', dest='auto_merge', action='store_const', const=True)
    merge.add_argument('--no-auto-merge', dest='auto_merge', action='store_const', const=False)
    autonomy = start.add_mutually_exclusive_group()
    autonomy.add_argument('--autonomous', dest='autonomous', action='store_const', const=True)
    autonomy.add_argument('--no-autonomous', dest='autonomous', action='store_const', const=False)
    big = start.add_mutually_exclusive_group()
    big.add_argument('--big-review', dest='big_review', action='store_const', const=True,
                     help='every member is a big issue: up to review.maxRoundsBig revmux rounds (#75)')
    big.add_argument('--no-big-review', dest='big_review', action='store_const', const=False)
    wait = start.add_mutually_exclusive_group()
    wait.add_argument('--wait-on-limit', dest='on_limit', action='store_const', const='wait',
                      help='a member whose agent hits its usage limit waits it out: no failover, no new members (#77)')
    wait.add_argument('--no-wait-on-limit', dest='on_limit', action='store_const', const='failover')
    run = sub.add_parser('run')
    run.add_argument('--file', required=True)
    run.add_argument('--token', required=True)
    mark = sub.add_parser('mark')
    mark.add_argument('--file', required=True)
    mark.add_argument('--number', required=True, type=int)
    mark.add_argument('--pr', required=True)
    mark.add_argument('--reason')
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
                               autonomous=args.autonomous, triage_on=args.triage, prune=args.prune,
                               clear_limit=args.clear_limit, name=args.name, workspace=args.workspace,
                               revmux_profile=args.revmux_profile, big_review=args.big_review,
                               on_limit=args.on_limit)
        if args.command == 'run':
            tslog.install()         # the #queue pane: every line timestamped (#78)
            return Worker(Store(args.file), args.token).run()
        if args.command == 'mark':
            mark_pr(args.file, args.number, args.pr, args.reason)
            print(f'marked #{args.number} PR {args.pr}')
            return 0
        if args.command == 'member-context':
            print(json.dumps(member_context(args.file, args.number, args.attempt, args.token)))
        else:
            if not member_result(args.file, args.number, args.attempt, args.token, read_json(args.result_file)):
                return 3
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, agw.CtlError) as err:
        print(f'queue: {err}', file=sys.stderr)
        return 2 if isinstance(err, UsageError) else 1


if __name__ == '__main__':
    sys.exit(main())
