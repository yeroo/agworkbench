#!/usr/bin/env python3
"""cleanup - delete an issue's checkout once its work is finished (#41).

Every issue gets a full clone under checkoutRoot, and nothing else ever deletes one. Two callers,
one set of safety checks (`check`), one way to delete (`remove`):

- `after-close`: started detached by the autonomous close (relay or conductor backstop) once the
  issue session is closed. It waits until no `#N ...` session is left in the repo's workspace, then
  checks and deletes. It runs with its cwd outside the checkout: on Windows nothing can delete a
  directory that is some process's cwd.
- `sweep` (`github-workbench -Cleanup`): every checkout under checkoutRoot whose branch has no open PR,
  and whose issue is closed or whose branch has a merged or closed PR.

A checkout is deleted only when it is provably ours and provably finished with: the directory is
`<root>/<name>-issue-<N>` with a `.git` whose origin is the repo, no session of the issue is open, no
launcher holds it, no queue still runs it, nothing is uncommitted or stashed, it has no linked
worktree or submodule, and every local commit is on a remote-tracking ref or inside the merged PR's head. Anything
else keeps it, with the reason logged. The delete renames the directory first (`.deleting-<ts>`):
on Windows the rename fails as a whole while anything holds a file or cwd inside it, so a checkout in
use is never half deleted, and a half-deleted one never looks like a checkout.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import agw
import conductor
import triage

HERE = Path(__file__).resolve().parent
MODES = ('merged', 'build', 'off')
BUILD_DIRS = frozenset({'target', 'node_modules', 'bin', 'obj'})
AFTER_CLOSE_WAIT = 600.0        # how long after-close waits for the issue's sessions to go
AFTER_CLOSE_POLL = 5.0
RENAME_TRIES = 3
LOG_NAME = '.agworkbench-cleanup.log'
LOCK_DIR = '.agworkbench-cleanup'
CHECKOUT_NAME = re.compile(r'(?P<name>.+)-issue-(?P<number>[1-9][0-9]*)')
LEFTOVER_NAME = re.compile(r'.+-issue-[1-9][0-9]*\.deleting-[0-9]+')
QUEUE_BUSY = {'pending', 'launching', 'active'}


class CleanupError(Exception):
    pass


# --- logging ---------------------------------------------------------------------------------

def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def log(root: Path, checkout: Path | str, text: str, echo: Callable[[str], None] | None = None) -> None:
    """One line per decision in `<checkoutRoot>/.agworkbench-cleanup.log`, which outlives the checkout."""
    line = f"{checkout}: {text}"
    if echo:
        echo(line)
    try:
        path = Path(root) / LOG_NAME
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f"{stamp()} {line}\n")
    except OSError:
        pass


def close_log(checkout: Path, text: str) -> None:
    """A refusal also goes to the checkout's own close log, next to the close it follows."""
    path = Path(checkout) / '.workbench' / 'state' / 'relay-close.log'
    if not path.parent.is_dir():
        return
    try:
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f"{stamp()} cleanup: {text}\n")
    except OSError:
        pass


# --- identity --------------------------------------------------------------------------------

def git(checkout: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(checkout), *args], capture_output=True, text=True, encoding='utf-8',
                          errors='replace', stdin=subprocess.DEVNULL, timeout=120)


def origin_repo(url: str | None) -> str | None:
    """`owner/name` (lowercase) of a GitHub remote URL in https, ssh or scp form, else None."""
    match = re.fullmatch(r'(?:https?://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)'
                         r'([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?/?',
                         (url or '').strip(), re.I)
    return f"{match[1]}/{match[2]}".lower() if match else None


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def checkout_identity(checkout: Path) -> dict:
    """{'repo', 'number'} of a workbench clone: the number from its directory name, the repo from its
    origin. Raises CleanupError for anything that is not one."""
    checkout = Path(checkout)
    match = CHECKOUT_NAME.fullmatch(checkout.name)
    if not match:
        raise CleanupError(f'not a workbench checkout name (<repo>-issue-<N>): {checkout.name}')
    if not (checkout / '.git').is_dir():
        raise CleanupError('no .git directory')
    done = git(checkout, 'remote', 'get-url', 'origin')
    repo = origin_repo(done.stdout) if done.returncode == 0 else None
    if repo is None:
        raise CleanupError(f'origin is not a GitHub repository: {done.stdout.strip() or done.stderr.strip()}')
    if repo.split('/')[1] != match['name'].casefold():
        raise CleanupError(f"directory name {checkout.name} does not match origin {repo}")
    return {'repo': repo, 'number': int(match['number'])}


def identity_reasons(checkout: Path, repo: str, issue: int, root: Path) -> list[str]:
    checkout = Path(checkout)
    if not same_path(checkout.parent, root):
        return [f'not directly under the checkout root {root}']
    try:
        found = checkout_identity(checkout)
    except CleanupError as err:
        return [str(err)]
    if found['repo'] != repo.lower() or found['number'] != int(issue):
        return [f"it is {found['repo']}#{found['number']}, not {repo.lower()}#{issue}"]
    return []


# --- the safety checks -----------------------------------------------------------------------

def live_sessions(repo: str, issue: int | str, tree) -> list[str]:
    """Every session of issue N in the repo's workspace: the issue session and its helpers alike
    (relay, revmux, your review). Unlike conductor.session_numbers, helpers count: one still open
    means the loop is not over."""
    workspace_name = repo.split('/')[-1].casefold()
    prefix = f'#{issue}'
    return [session.get('name') for workspace, session in agw.sessions(tree)
            if (workspace.get('name') or '').casefold() == workspace_name
            and ((session.get('name') or '') == prefix or (session.get('name') or '').startswith(prefix + ' '))]


def queue_reason(checkout: Path) -> str | None:
    path = Path(checkout) / '.workbench' / 'state' / 'queue-member.json'
    if not path.exists():
        return None
    try:
        membership = conductor.read_json(path)
        queue = Path(membership['queue'])
        number = membership['number']
    except (OSError, ValueError, KeyError, TypeError) as err:
        return f'unreadable queue membership: {err}'
    if not queue.exists():
        return None
    try:
        member = conductor.find_member(conductor.read_json(queue), number)
    except (OSError, ValueError, KeyError, TypeError) as err:
        return f'unreadable queue {queue}: {err}'
    if member and member.get('state') in QUEUE_BUSY:
        return f"queue {queue} still runs it ({member['state']})"
    return None


def unpushed_reasons(checkout: Path, pr_head: str | None) -> list[str]:
    """Local commits that are on no remote-tracking ref and not inside the merged PR's head."""
    done = git(checkout, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads')
    if done.returncode:
        return [f'git for-each-ref failed: {done.stderr.strip()}']
    tips = [line.rsplit(' ', 1) for line in done.stdout.splitlines() if line.strip()]
    head = git(checkout, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}')
    if head.returncode == 0 and head.stdout.strip():
        tips.append(['HEAD', head.stdout.strip()])
    reasons = []
    for name, sha in tips:
        missing = git(checkout, 'rev-list', sha, '--not', '--remotes')
        if missing.returncode:
            reasons.append(f'git rev-list failed for {name}: {missing.stderr.strip()}')
            continue
        count = len(missing.stdout.split())
        if not count:
            continue
        if pr_head and git(checkout, 'merge-base', '--is-ancestor', sha, pr_head).returncode == 0:
            continue
        reasons.append(f'unpushed: {name} has {count} commit(s) on no remote and not in the merged PR')
    return reasons


def check(checkout: Path, *, repo: str, issue: int | str, root: Path, tree, pr_head: str | None = None) -> list[str]:
    """Why this checkout must be kept; empty when it is safe to delete. `tree` is the session tree,
    or None when it could not be read (then it is kept: nothing proves no session uses it)."""
    checkout = Path(checkout)
    reasons = identity_reasons(checkout, repo, int(issue), root)
    if reasons:
        return reasons                  # never run anything else on a directory that is not ours
    if tree is None:
        reasons.append('sessions: unknown (the session tree could not be read)')
    else:
        reasons += [f'session open: {name}' for name in live_sessions(repo, issue, tree)]
    if conductor.checkout_locked(checkout):
        reasons.append('a launcher holds its launch.lock')
    queued = queue_reason(checkout)
    if queued:
        reasons.append(queued)
    if (checkout / '.git' / 'index.lock').exists():
        reasons.append('.git/index.lock exists')
    status = git(checkout, 'status', '--porcelain', '--untracked-files=all')
    if status.returncode:
        reasons.append(f'git status failed: {status.stderr.strip()}')
    elif status.stdout.strip():
        paths = status.stdout.splitlines()
        reasons.append(f'uncommitted changes: {len(paths)} path(s), first {paths[0][3:]}')
    stash = git(checkout, 'stash', 'list')
    if stash.returncode:
        reasons.append(f'git stash list failed: {stash.stderr.strip()}')
    elif stash.stdout.strip():
        reasons.append(f'stash: {len(stash.stdout.splitlines())} entr(ies)')
    worktrees = git(checkout, 'worktree', 'list', '--porcelain')
    if worktrees.returncode:
        reasons.append(f'git worktree list failed: {worktrees.stderr.strip()}')
    elif sum(line.startswith('worktree ') for line in worktrees.stdout.splitlines()) > 1:
        reasons.append('it has linked worktrees')
    # A submodule's own commits live in .git/modules and none of the checks above see them.
    if (checkout / '.gitmodules').exists() or (checkout / '.git' / 'modules').exists():
        reasons.append('it has submodules (their unpushed commits are not checked)')
    reasons += unpushed_reasons(checkout, pr_head)
    return reasons


# --- deleting --------------------------------------------------------------------------------

def long_path(path: Path) -> str:
    """An extended-length path on Windows, so node_modules deeper than 260 characters deletes."""
    text = str(Path(path).resolve())
    if os.name == 'nt' and not text.startswith('\\\\?\\'):
        return '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    return text


def rmtree(path: Path) -> None:
    # git's pack and object files are read-only: triage's helper clears the bit, on every Python.
    triage.remove_tree(long_path(path))


def is_link(path: Path) -> bool:
    """A symlink or a directory junction (Path.is_junction is Python 3.12+; the reparse-point bit is not)."""
    path = Path(path)
    if path.is_symlink():
        return True
    try:
        return bool(getattr(os.lstat(path), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    except OSError:
        return False


def dir_size(path: Path) -> int:
    total = 0
    for directory, _, files in os.walk(long_path(path)):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def build_outputs(checkout: Path) -> list[Path]:
    """`target/`, `node_modules/`, `bin/` and `obj/` directories outside .git that hold no tracked file."""
    checkout = Path(checkout)
    found = []
    for directory, subdirs, _ in os.walk(checkout):
        keep = []
        for name in subdirs:
            path = Path(directory) / name
            if name == '.git' or is_link(path):
                continue
            if name in BUILD_DIRS:
                tracked = git(checkout, 'ls-files', '--', str(path.relative_to(checkout)).replace(os.sep, '/'))
                if tracked.returncode == 0 and not tracked.stdout.strip():
                    found.append(path)
                    continue                # never descend into one we delete
            keep.append(name)
        subdirs[:] = keep
    return found


def remove(checkout: Path, mode: str = 'merged', *, pause: Callable[[float], None] = time.sleep) -> int:
    """Delete the checkout (`merged`) or only its build outputs (`build`). Returns the bytes freed.
    Raises CleanupError when nothing, or not everything, could be deleted."""
    checkout = Path(checkout)
    if mode == 'build':
        freed = 0
        for path in build_outputs(checkout):
            size = dir_size(path)
            try:
                rmtree(path)
            except OSError as err:
                raise CleanupError(f'could not delete {path}: {err}') from err
            freed += size
        return freed
    if mode != 'merged':
        raise CleanupError(f'unknown cleanup mode {mode!r}')
    size = dir_size(checkout)
    target = checkout.with_name(f'{checkout.name}.deleting-{int(time.time())}')
    error = None
    for attempt in range(RENAME_TRIES):
        try:
            os.rename(checkout, target)
            break
        except OSError as err:          # a process holds a file or its cwd in it (or AV, briefly)
            error = err
            if attempt + 1 < RENAME_TRIES:
                pause(1.0)
    else:
        raise CleanupError(f'in use, nothing deleted (the rename failed: {error})')
    try:
        rmtree(target)
    except OSError as err:
        raise CleanupError(f'partly deleted: {target} remains ({err}); the next -Cleanup removes it') from err
    return size


def lock_path(root: Path, checkout: Path) -> Path:
    return Path(root) / LOCK_DIR / f'{Path(checkout).name}.lock'


def clean(checkout: Path, *, repo: str, issue: int | str, root: Path, mode: str, tree, pr_head: str | None,
          echo: Callable[[str], None] | None = None, pause: Callable[[float], None] = time.sleep) -> tuple[bool, list[str], int]:
    """Check and delete one checkout under its own lock: (deleted, reasons kept, bytes freed)."""
    lock = conductor.Lock(lock_path(root, checkout), 0)
    try:
        lock.acquire()
    except conductor.QueueError:
        reasons = ['another cleanup is running on it']
        log(root, checkout, 'kept: ' + '; '.join(reasons), echo)
        return False, reasons, 0
    try:
        reasons = check(checkout, repo=repo, issue=issue, root=root, tree=tree, pr_head=pr_head)
        if reasons:
            log(root, checkout, 'kept: ' + '; '.join(reasons), echo)
            return False, reasons, 0
        log(root, checkout, f'deleting ({mode})', echo)
        try:
            freed = remove(checkout, mode, pause=pause)
        except CleanupError as err:
            log(root, checkout, f'kept: {err}', echo)
            return False, [str(err)], 0
        log(root, checkout, f'deleted ({mode}): freed {human(freed)}', echo)
        return True, [], freed
    finally:
        lock.release()


def human(size: float) -> str:
    unit = 'B'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            break
        size /= 1024
    return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'


# --- starting the detached after-close -------------------------------------------------------

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def after_close_argv(checkout: Path, repo: str, issue: int | str, pr: int, mode: str) -> list[str]:
    argv = [sys.executable, str(HERE / 'cleanup.py'), 'after-close', '--checkout', str(checkout), '--repo', repo,
            '--issue', str(issue), '--pr', str(pr), '--mode', mode]
    if os.environ.get('AGWINTERM_PIPE'):
        # A process started through WMI gets the user's default environment, not ours.
        argv += ['--pipe', os.environ['AGWINTERM_PIPE']]
    return argv


def wmi_create(command_line: str, cwd: str) -> int:
    """Start a process through WMI (Win32_Process.Create). Its parent is the WMI provider host, so it
    is outside the job of the agwinterm session that asked for it and outlives that session's close."""
    def quoted(text):
        return "'" + text.replace("'", "''") + "'"
    script = ('$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=' +
              quoted(command_line) + '; CurrentDirectory=' + quoted(cwd) + "}; 'rc=' + $r.ReturnValue + ' pid=' + $r.ProcessId")
    shell = shutil.which('powershell.exe') or shutil.which('pwsh') or 'powershell.exe'
    done = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL, timeout=60)
    match = re.search(r'rc=0 pid=([0-9]+)', done.stdout)
    if not match:
        raise OSError(f'Win32_Process.Create failed: {(done.stdout + done.stderr).strip()[:300]}')
    return int(match[1])


def start_after_close(checkout: Path, repo: str, issue: int | str, pr: int, mode: str, *, popen=subprocess.Popen,
                      wmi=wmi_create) -> tuple[int | None, str]:
    """Start `after-close` so that closing the caller's session does not end it, with its cwd outside
    the checkout. Returns (pid, how). agwinterm kills a session's job when the session closes and its
    job refuses breakaway, so on Windows: breakaway if the job allows it, else WMI, else (logged by the
    caller) a plain detached process, which such a job still ends."""
    checkout = Path(checkout).resolve()
    argv = after_close_argv(checkout, repo, issue, pr, mode)
    options = dict(cwd=str(checkout.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    if os.name != 'nt':
        return popen(argv, start_new_session=True, **options).pid, 'detached'
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        return popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **options).pid, 'breakaway'
    except OSError:
        pass                            # the session's job forbids breakaway
    try:
        return wmi(subprocess.list2cmdline(argv), str(checkout.parent)), 'wmi'
    except (OSError, subprocess.SubprocessError):
        return popen(argv, creationflags=flags, **options).pid, 'detached'


# --- after-close -----------------------------------------------------------------------------

def pr_head_of(repo: str, pr: int, gh=conductor.gh_json) -> str | None:
    data = gh('pr', 'view', str(pr), '--repo', repo, '--json', 'state,headRefOid')
    return data.get('headRefOid') if isinstance(data, dict) and data.get('state') == 'MERGED' else None


def after_close(checkout: Path, repo: str, issue: int, pr: int, mode: str, *, read_tree=None, gh=conductor.gh_json,
                clock: Callable[[], float] = time.monotonic, pause: Callable[[float], None] = time.sleep,
                echo: Callable[[str], None] | None = None) -> int:
    read_tree = read_tree or agw.tree
    checkout = Path(checkout).resolve()
    root = checkout.parent
    if mode not in ('merged', 'build'):
        log(root, checkout, f'nothing to do (cleanup {mode!r})', echo)
        return 0
    log(root, checkout, f'PR #{pr} merged and the issue session closed; waiting for #{issue} sessions to go', echo)
    deadline = clock() + AFTER_CLOSE_WAIT
    while True:
        try:
            snapshot = read_tree()
            live = live_sessions(repo, issue, snapshot)
        except (agw.CtlError, OSError) as err:
            snapshot, live = None, [f'the session tree could not be read: {err}']
        if not live:
            break
        if clock() >= deadline:
            reason = f'kept: still open after {AFTER_CLOSE_WAIT:.0f}s: ' + ', '.join(live)
            log(root, checkout, reason, echo)
            close_log(checkout, reason)
            return 1
        pause(AFTER_CLOSE_POLL)
    try:
        head = pr_head_of(repo, pr, gh)
    except (OSError, ValueError, subprocess.SubprocessError) as err:
        # Without the PR head only remote-tracking refs vouch for local commits: the safe direction.
        log(root, checkout, f'PR head unknown ({err}); checking against remote-tracking refs only', echo)
        head = None
    deleted, reasons, _ = clean(checkout, repo=repo, issue=issue, root=root, mode=mode, tree=snapshot,
                                pr_head=head, echo=echo, pause=pause)
    if not deleted:
        close_log(checkout, 'kept: ' + '; '.join(reasons))
        return 1
    return 0


# --- sweep -----------------------------------------------------------------------------------

def candidacy(repo: str, number: int, branch: str | None, gh) -> tuple[bool, str, str | None]:
    """(candidate, why, merged PR head): the branch has no open PR, and either the issue is closed or
    the branch has a merged or closed PR."""
    issue = gh('issue', 'view', str(number), '--repo', repo, '--json', 'state')
    prs = gh('pr', 'list', '--repo', repo, '--head', branch, '--state', 'all', '--json', 'number,state,headRefOid') if branch else []
    open_prs = [pr['number'] for pr in prs if pr.get('state') == 'OPEN']
    merged = [pr for pr in prs if pr.get('state') == 'MERGED']
    head = merged[0].get('headRefOid') if merged else None
    if open_prs:
        return False, f'PR #{open_prs[0]} is open', head
    if (issue or {}).get('state') == 'CLOSED':
        return True, 'issue closed', head
    done = [pr for pr in prs if pr.get('state') in ('MERGED', 'CLOSED')]
    if done:
        return True, f"PR #{done[0]['number']} {done[0]['state'].lower()}", head
    return False, 'issue open and no merged or closed PR', head


def sweep(root: Path, *, repo: str | None = None, dry_run: bool = False, build_only: bool = False, gh=conductor.gh_json,
          read_tree=None, out: Callable[[str], None] = print) -> int:
    root = Path(root).resolve()
    if not root.is_dir():
        out(f'no checkout root {root}')
        return 0
    read_tree = read_tree or agw.tree
    try:
        snapshot = read_tree()
    except (agw.CtlError, OSError):
        snapshot = None                  # check() then keeps every checkout: "sessions: unknown"
    mode = 'build' if build_only else 'merged'
    wanted = repo.lower() if repo else None
    kept = gh_failed = False
    freed = 0
    for path in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if not path.is_dir():
            continue
        if LEFTOVER_NAME.fullmatch(path.name):
            if is_link(path):
                # Deleting through a link would delete its target; never follow one.
                kept = True
                out(f'keep {path}: is a link')
                continue
            if dry_run:
                out(f'leftover {path} {human(dir_size(path))}')
                continue
            size = dir_size(path)
            try:
                rmtree(path)
                freed += size
                out(f'removed leftover {path} ({human(size)})')
                log(root, path, f'removed leftover ({human(size)})')
            except OSError as err:
                kept = True
                out(f'keep {path}: leftover could not be deleted: {err}')
            continue
        match = CHECKOUT_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            found = checkout_identity(path)
        except CleanupError as err:
            out(f'skip {path}: {err}')
            continue
        if wanted and found['repo'] != wanted:
            continue
        branch = git(path, 'branch', '--show-current').stdout.strip() or None
        try:
            candidate, why, head = candidacy(found['repo'], found['number'], branch, gh)
        except (OSError, ValueError, subprocess.SubprocessError) as err:
            gh_failed = True
            out(f'skip {path}: GitHub lookup failed: {err}')
            continue
        if not candidate:
            out(f'skip {path}: {why}')
            continue
        if dry_run:
            reasons = check(path, repo=found['repo'], issue=found['number'], root=root, tree=snapshot, pr_head=head)
            if reasons:
                kept = True
                out(f'keep {path}: ' + '; '.join(reasons))
            else:
                size = sum(dir_size(p) for p in build_outputs(path)) if build_only else dir_size(path)
                out(f'candidate {path} {human(size)} ({why})')
            continue
        deleted, reasons, size = clean(path, repo=found['repo'], issue=found['number'], root=root, mode=mode,
                                       tree=snapshot, pr_head=head)
        if deleted:
            freed += size
            out(f"{'cleaned' if build_only else 'deleted'} {path}: freed {human(size)} ({why})")
        else:
            kept = True
            out(f'keep {path}: ' + '; '.join(reasons))
    if not dry_run:
        out(f'freed {human(freed)}')
    return 2 if gh_failed else 1 if kept else 0


# --- command line ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    parser = argparse.ArgumentParser(prog='cleanup.py')
    sub = parser.add_subparsers(dest='command', required=True)
    after = sub.add_parser('after-close')
    after.add_argument('--checkout', required=True)
    after.add_argument('--repo', required=True)
    after.add_argument('--issue', required=True, type=int)
    after.add_argument('--pr', required=True, type=int)
    after.add_argument('--mode', required=True, choices=MODES)
    after.add_argument('--pipe')
    swept = sub.add_parser('sweep')
    swept.add_argument('--root')
    swept.add_argument('--repo')
    swept.add_argument('--dry-run', action='store_true')
    swept.add_argument('--build-only', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'after-close':
        if args.pipe:
            os.environ['AGWINTERM_PIPE'] = args.pipe
        try:
            return after_close(Path(args.checkout), args.repo, args.issue, args.pr, args.mode)
        except Exception as err:        # noqa: BLE001 - detached: nobody sees a traceback, the log must
            checkout = Path(args.checkout)
            log(checkout.parent, checkout, f'kept: after-close failed: {err!r}')
            close_log(checkout, f'kept: after-close failed: {err!r}')
            return 1
    try:
        if args.repo:
            conductor.repo_name(args.repo)
        root = Path(args.root) if args.root else conductor.checkout_root(conductor.config_path())
    except (OSError, ValueError) as err:
        print(f'cleanup: {err}', file=sys.stderr)
        return 2
    return sweep(root, repo=args.repo, dry_run=args.dry_run, build_only=args.build_only)


if __name__ == '__main__':
    sys.exit(main())
