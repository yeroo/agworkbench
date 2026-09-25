"""#41: deleting finished checkouts. Real git clones in a temp folder under the test root; gh, the
session tree, the clock and process start are stubbed. Nothing here touches a real checkout."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import cleanup  # noqa: E402
import conductor  # noqa: E402
import triage  # noqa: E402

EMPTY_TREE = {'workspaces': [{'name': 'repo', 'sessions': []}]}


def run(cwd, *args):
    done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if done.returncode:
        raise AssertionError(f'git {args}: {done.stderr}')
    return done.stdout.strip()


def junction(link, target):
    """A directory junction: no privilege needed on Windows."""
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


class Clones(unittest.TestCase):
    def setUp(self):
        self.base = ROOT / ('test cleanup ' + uuid.uuid4().hex)
        self.root = self.base / 'clones'
        self.root.mkdir(parents=True)
        self.addCleanup(lambda: triage.remove_tree(cleanup.long_path(self.base)))

    def clone(self, number=7, name='repo', origin='https://github.com/o/repo.git', pushed=True):
        """A workbench-shaped clone: `<root>/<name>-issue-<N>` on `issue-<N>-fix`, `.workbench/` excluded
        the way the launcher excludes it, the branch on a remote-tracking ref when `pushed`."""
        checkout = self.root / f'{name}-issue-{number}'
        checkout.mkdir()
        run(checkout, 'init', '-q', '-b', f'issue-{number}-fix')
        run(checkout, 'config', 'user.email', 't@example.com')
        run(checkout, 'config', 'user.name', 'T')
        run(checkout, 'config', 'core.autocrlf', 'false')
        (checkout / 'README.md').write_text('hi\n', encoding='utf-8')
        run(checkout, 'add', 'README.md')
        run(checkout, 'commit', '-q', '-m', 'one')
        run(checkout, 'remote', 'add', 'origin', origin)
        (checkout / '.git' / 'info' / 'exclude').write_text('.workbench/\n', encoding='utf-8')
        (checkout / '.workbench' / 'state').mkdir(parents=True)
        (checkout / '.workbench' / 'state' / 'relay-close.log').write_text('', encoding='utf-8')
        if pushed:
            self.push(checkout)
        return checkout

    def ignore(self, checkout, pattern):
        with (checkout / '.git' / 'info' / 'exclude').open('a', encoding='utf-8') as handle:
            handle.write(pattern + '\n')

    def push(self, checkout):
        run(checkout, 'update-ref', f'refs/remotes/origin/{run(checkout, "branch", "--show-current")}', 'HEAD')

    def commit(self, checkout, name='more.txt'):
        (checkout / name).write_text(name, encoding='utf-8')
        run(checkout, 'add', name)
        run(checkout, 'commit', '-q', '-m', name)
        return run(checkout, 'rev-parse', 'HEAD')

    def check(self, checkout, tree=EMPTY_TREE, **kwargs):
        options = dict(repo='o/repo', issue=7, root=self.root, tree=tree)
        options.update(kwargs)
        return cleanup.check(checkout, **options)


class Check(Clones):
    def assertReason(self, text, reasons):
        self.assertTrue(any(text in reason for reason in reasons), reasons)

    def test_a_finished_clean_checkout_is_safe(self):
        self.assertEqual([], self.check(self.clone()))

    def test_any_session_of_the_issue_in_the_repo_workspace_keeps_it(self):
        checkout = self.clone()
        for name in ('#7 fix the thing', '#7 relay', '#7 revmux r2', '#7 your review', '#7'):
            with self.subTest(name=name):
                tree = {'workspaces': [{'name': 'Repo', 'sessions': [{'id': 's', 'name': name}]}]}
                self.assertReason(f'session open: {name}', self.check(checkout, tree))
        for workspace, name in (('other', '#7 relay'), ('repo', '#70 relay'), ('repo', 'notes #7')):
            with self.subTest(workspace=workspace, name=name):
                tree = {'workspaces': [{'name': workspace, 'sessions': [{'id': 's', 'name': name}]}]}
                self.assertEqual([], self.check(checkout, tree))

    def test_an_unreadable_session_tree_keeps_it(self):
        self.assertReason('sessions: unknown', self.check(self.clone(), tree=None))

    def test_uncommitted_or_untracked_work_keeps_it_but_ignored_files_do_not(self):
        checkout = self.clone()
        (checkout / '.workbench' / 'state' / 'more.json').write_text('{}', encoding='utf-8')
        self.assertEqual([], self.check(checkout))                              # ignored
        (checkout / 'notes.txt').write_text('draft', encoding='utf-8')
        self.assertReason('uncommitted changes: 1 path(s), first notes.txt', self.check(checkout))
        (checkout / 'notes.txt').unlink()
        (checkout / 'README.md').write_text('changed\n', encoding='utf-8')
        self.assertReason('uncommitted changes', self.check(checkout))

    def test_a_stash_keeps_it(self):
        checkout = self.clone()
        (checkout / 'README.md').write_text('changed\n', encoding='utf-8')
        run(checkout, 'stash', '-q')
        self.assertReason('stash: 1', self.check(checkout))

    def test_a_commit_on_no_remote_keeps_it_unless_the_merged_pr_contains_it(self):
        checkout = self.clone()
        head = self.commit(checkout)
        self.assertReason('unpushed: issue-7-fix has 1 commit(s)', self.check(checkout))
        self.assertEqual([], self.check(checkout, pr_head=head))                # squash-merged; branch deleted, not pruned
        self.commit(checkout, 'after-merge.txt')
        self.assertReason('unpushed: issue-7-fix has 2 commit(s)', self.check(checkout, pr_head=head))
        self.assertReason('unpushed', self.check(checkout, pr_head='0' * 40))  # an object we do not have

    def test_commits_on_another_branch_or_a_detached_head_keep_it(self):
        checkout = self.clone()
        run(checkout, 'checkout', '-q', '-b', 'side')
        self.commit(checkout)
        run(checkout, 'checkout', '-q', 'issue-7-fix')
        self.assertReason('unpushed: side has 1 commit(s)', self.check(checkout))
        run(checkout, 'branch', '-q', '-D', 'side')
        run(checkout, 'checkout', '-q', '--detach')
        self.commit(checkout, 'detached.txt')
        self.assertReason('unpushed: HEAD has 1 commit(s)', self.check(checkout))

    def test_a_git_lock_a_launcher_or_a_linked_worktree_keeps_it(self):
        checkout = self.clone()
        (checkout / '.git' / 'index.lock').write_text('', encoding='utf-8')
        self.assertReason('.git/index.lock exists', self.check(checkout))
        (checkout / '.git' / 'index.lock').unlink()
        with patch.object(conductor, 'checkout_locked', return_value=True):
            self.assertReason('a launcher holds its launch.lock', self.check(checkout))
        run(checkout, 'worktree', 'add', '-q', str(self.base / 'linked'), '-b', 'wt')
        self.push(checkout)                    # the new branch's commit is already on origin/issue-7-fix
        self.assertReason('it has linked worktrees', self.check(checkout))

    def test_submodules_keep_it(self):
        # r1 M2: a submodule's unpushed commits live in .git/modules, where no other check looks.
        checkout = self.clone()
        (checkout / '.gitmodules').write_text('[submodule "lib"]\n\tpath = lib\n\turl = https://github.com/o/lib.git\n',
                                              encoding='utf-8')
        run(checkout, 'add', '.gitmodules')
        run(checkout, 'commit', '-q', '-m', 'submodule')
        self.push(checkout)
        self.assertReason('it has submodules', self.check(checkout))
        run(checkout, 'rm', '-q', '.gitmodules')
        run(checkout, 'commit', '-q', '-m', 'no submodule')
        self.push(checkout)
        self.assertEqual([], self.check(checkout))
        (checkout / '.git' / 'modules' / 'lib').mkdir(parents=True)             # left behind by a removed one
        self.assertReason('it has submodules', self.check(checkout))

    def test_a_queue_still_running_the_member_keeps_it(self):
        checkout = self.clone()
        queue = self.base / 'queues' / 'o' / 'repo.json'
        membership = checkout / '.workbench' / 'state' / 'queue-member.json'
        conductor.atomic_json(membership, {'queue': str(queue), 'repo': 'o/repo', 'number': 7})
        self.assertEqual([], self.check(checkout))                              # the queue file is gone
        for state, kept in (('active', True), ('pending', True), ('launching', True), ('merged', False), ('pr-open', False)):
            with self.subTest(state=state):
                conductor.atomic_json(queue, {'members': [{'number': 7, 'state': state}]})
                reasons = self.check(checkout)
                if kept:
                    self.assertReason(f'still runs it ({state})', reasons)
                else:
                    self.assertEqual([], reasons)
        membership.write_text('{broken', encoding='utf-8')
        self.assertReason('unreadable queue membership', self.check(checkout))

    def test_only_its_own_clone_under_the_root(self):
        checkout = self.clone()
        self.assertReason('not directly under the checkout root', self.check(checkout, root=self.base))
        self.assertReason('it is o/repo#7, not o/repo#8', self.check(checkout, issue=8))
        self.assertReason('it is o/repo#7, not x/repo#7', self.check(checkout, repo='x/repo'))
        for origin, reason in (('https://gitlab.com/o/repo.git', 'origin is not a GitHub repository'),
                               ('git@github.com:o/other.git', 'does not match origin o/other')):
            with self.subTest(origin=origin):
                run(checkout, 'remote', 'set-url', 'origin', origin)
                self.assertReason(reason, self.check(checkout))
        plain = self.root / 'repo-issue-8'
        plain.mkdir()
        self.assertReason('no .git directory', self.check(plain, issue=8))
        self.assertReason('not a workbench checkout name', self.check(self.root, root=self.base))

    def test_origin_spellings(self):
        for url in ('https://github.com/O/Repo.git', 'https://github.com/o/repo', 'https://x@github.com/o/repo.git/',
                    'git@github.com:o/repo.git', 'ssh://git@github.com/o/repo.git'):
            with self.subTest(url=url):
                self.assertEqual('o/repo', cleanup.origin_repo(url))
        self.assertIsNone(cleanup.origin_repo('https://github.com.evil/o/repo.git'))


class Remove(Clones):
    def test_merged_deletes_everything_read_only_git_objects_included(self):
        checkout = self.clone()
        packed = next((checkout / '.git' / 'objects').rglob('*'))
        self.assertTrue(packed.exists())
        freed = cleanup.remove(checkout, 'merged', pause=lambda s: None)
        self.assertFalse(checkout.exists())
        self.assertGreater(freed, 0)
        self.assertEqual([], list(self.root.iterdir()))                         # no .deleting- leftover

    @unittest.skipUnless(os.name == 'nt', 'Windows refuses to rename a directory in use')
    def test_a_checkout_in_use_is_not_touched(self):
        checkout = self.clone()
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=checkout)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        pauses = []
        with self.assertRaisesRegex(cleanup.CleanupError, 'in use, nothing deleted'):
            cleanup.remove(checkout, 'merged', pause=pauses.append)
        self.assertEqual([1.0, 1.0], pauses)                                    # three tries
        self.assertTrue((checkout / '.git' / 'HEAD').exists())
        self.assertTrue((checkout / 'README.md').exists())

    def test_build_deletes_only_untracked_build_directories(self):
        checkout = self.clone()
        for path in ('target/debug/app.exe', 'web/node_modules/pkg/node_modules/x/i.js', 'src/obj/a.o'):
            (checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (checkout / path).write_bytes(b'x' * 100)
        (checkout / 'bin').mkdir()
        (checkout / 'bin' / 'tool.ps1').write_text('tracked', encoding='utf-8')
        run(checkout, 'add', 'bin/tool.ps1')
        run(checkout, 'commit', '-q', '-m', 'tool')
        (checkout / 'bin' / 'built.exe').write_bytes(b'x')
        (checkout / '.git' / 'target').mkdir()
        freed = cleanup.remove(checkout, 'build')
        self.assertEqual(300, freed)
        for gone in ('target', 'web/node_modules', 'src/obj'):
            self.assertFalse((checkout / gone).exists(), gone)
        for kept in ('bin/tool.ps1', 'bin/built.exe', '.git/target', 'README.md', 'web', 'src'):
            self.assertTrue((checkout / kept).exists(), kept)

    def test_read_only_files_delete_on_python_before_3_12(self):
        # r1 M1: shutil.rmtree has no onexc before 3.12; the project's helper falls back to onerror.
        checkout = self.clone()
        (checkout / 'locked.txt').write_text('x', encoding='utf-8')
        os.chmod(checkout / 'locked.txt', 0o444)
        real = shutil.rmtree

        def old_rmtree(path, ignore_errors=False, onerror=None, **kwargs):
            if kwargs:
                raise TypeError(f'rmtree() got unexpected keyword arguments {sorted(kwargs)}')
            return real(path, ignore_errors, onerror)
        with patch.object(triage.sys, 'version_info', (3, 11, 9)), patch.object(shutil, 'rmtree', old_rmtree):
            cleanup.remove(checkout, 'merged', pause=lambda s: None)
        self.assertEqual([], list(self.root.iterdir()))

    def test_build_mode_needs_no_is_junction(self):
        # r1 M1: Path.is_junction is 3.12+.
        checkout = self.clone()
        (checkout / 'target').mkdir()
        with patch.object(type(checkout), 'is_junction', None, create=True):
            self.assertEqual([checkout / 'target'], cleanup.build_outputs(checkout))

    @unittest.skipUnless(os.name == 'nt', 'directory junctions are Windows only')
    def test_build_mode_never_follows_a_junction(self):
        checkout = self.clone()
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep', encoding='utf-8')
        junction(checkout / 'target', outside)
        self.assertEqual([], cleanup.build_outputs(checkout))
        cleanup.remove(checkout, 'build')
        self.assertTrue((outside / 'precious').exists())
        os.rmdir(checkout / 'target')                                           # the junction, not its target

    def test_a_second_cleanup_of_the_same_checkout_waits_its_turn(self):
        checkout = self.clone()
        with conductor.Lock(cleanup.lock_path(self.root, checkout), 0):
            deleted, reasons, _ = cleanup.clean(checkout, repo='o/repo', issue=7, root=self.root, mode='merged',
                                                tree=EMPTY_TREE, pr_head=None)
        self.assertFalse(deleted)
        self.assertEqual(['another cleanup is running on it'], reasons)
        self.assertTrue(checkout.exists())


class AfterClose(Clones):
    def setUp(self):
        super().setUp()
        self.t = 0.0
        self.trees = []
        self.head = None
        self.gh_calls = []

    def tree(self):
        return self.trees.pop(0) if len(self.trees) > 1 else self.trees[0]

    def gh(self, *args):
        self.gh_calls.append(args)
        return {'state': 'MERGED', 'headRefOid': self.head}

    def pause(self, seconds):
        self.t += seconds

    def after_close(self, checkout, mode='merged'):
        return cleanup.after_close(checkout, 'o/repo', 7, 42, mode, read_tree=self.tree, gh=self.gh,
                                   clock=lambda: self.t, pause=self.pause)

    def root_log(self):
        return (self.root / cleanup.LOG_NAME).read_text(encoding='utf-8')

    def relay_log(self, checkout):
        return (checkout / '.workbench' / 'state' / 'relay-close.log').read_text(encoding='utf-8')

    def test_merged_and_clean_is_deleted_once_the_last_session_is_gone(self):
        checkout = self.clone()
        self.head = self.commit(checkout)                                       # only in the merged PR
        relay = {'workspaces': [{'name': 'repo', 'sessions': [{'id': 'r', 'name': '#7 relay'}]}]}
        self.trees = [relay, relay, EMPTY_TREE]
        self.assertEqual(0, self.after_close(checkout))
        self.assertFalse(checkout.exists())
        self.assertEqual(10.0, self.t)                                          # two polls
        self.assertEqual([('pr', 'view', '42', '--repo', 'o/repo', '--json', 'state,headRefOid')], self.gh_calls)
        self.assertIn('deleted (merged): freed', self.root_log())

    def test_dirty_unpushed_or_stashed_is_kept_with_the_reason_logged(self):
        def dirty(checkout):
            (checkout / 'notes.txt').write_text('draft', encoding='utf-8')

        def unpushed(checkout):
            self.commit(checkout)

        def stashed(checkout):
            (checkout / 'README.md').write_text('changed\n', encoding='utf-8')
            run(checkout, 'stash', '-q')
        for number, (arrange, reason) in enumerate(((dirty, 'uncommitted changes'), (unpushed, 'unpushed: issue-7-fix'),
                                                    (stashed, 'stash: 1')), start=1):
            with self.subTest(reason=reason):
                triage.remove_tree(cleanup.long_path(self.root))
                self.root.mkdir()
                checkout = self.clone()
                arrange(checkout)
                self.trees = [EMPTY_TREE]
                self.assertEqual(1, self.after_close(checkout))
                self.assertTrue((checkout / '.git').is_dir())
                self.assertIn('kept: ' + reason, self.root_log())
                self.assertIn('cleanup: kept: ' + reason, self.relay_log(checkout))

    def test_a_session_that_stays_open_keeps_it(self):
        checkout = self.clone()
        self.trees = [{'workspaces': [{'name': 'repo', 'sessions': [{'id': 'r', 'name': '#7 your review'}]}]}]
        self.assertEqual(1, self.after_close(checkout))
        self.assertTrue(checkout.exists())
        self.assertGreaterEqual(self.t, cleanup.AFTER_CLOSE_WAIT)
        self.assertIn('kept: still open after 600s: #7 your review', self.root_log())
        self.assertIn('still open after 600s', self.relay_log(checkout))
        self.assertEqual([], self.gh_calls)

    def test_build_mode_keeps_the_clone(self):
        checkout = self.clone()
        self.ignore(checkout, 'target/')                                        # as a Rust repo's .gitignore does
        (checkout / 'target').mkdir()
        (checkout / 'target' / 'big').write_bytes(b'x' * 10)
        self.trees = [EMPTY_TREE]
        self.assertEqual(0, self.after_close(checkout, 'build'))
        self.assertFalse((checkout / 'target').exists())
        self.assertTrue((checkout / 'README.md').exists())

    def test_an_unignored_build_directory_is_untracked_work(self):
        # Only ignored build outputs are provably the build's; anything else untracked keeps the clone.
        checkout = self.clone()
        (checkout / 'target').mkdir()
        (checkout / 'target' / 'big').write_bytes(b'x' * 10)
        self.trees = [EMPTY_TREE]
        self.assertEqual(1, self.after_close(checkout, 'build'))
        self.assertTrue((checkout / 'target' / 'big').exists())

    def test_the_pr_head_lookup_failing_falls_back_to_remote_refs(self):
        checkout = self.clone()
        self.trees = [EMPTY_TREE]
        self.gh = Mock(side_effect=conductor.QueueError('gh: offline'))
        self.assertEqual(0, self.after_close(checkout))                         # everything was pushed
        self.assertIn('PR head unknown', self.root_log())


class Start(unittest.TestCase):
    """How after-close is started so that closing the caller's agwinterm session does not end it. Checked
    by hand: agwinterm's session job refuses breakaway and kills a DETACHED_PROCESS child on close,
    while a Win32_Process.Create child outlives it."""
    def setUp(self):
        self.checkout = ROOT / 'nowhere' / 'repo-issue-7'
        self.enterContext(patch.dict(os.environ, {'AGWINTERM_PIPE': 'agwinterm-test'}))

    def argv(self):
        return [sys.executable, str(ROOT / 'lib' / 'cleanup.py'), 'after-close', '--checkout', str(self.checkout.resolve()),
                '--repo', 'o/repo', '--issue', '7', '--pr', '42', '--mode', 'merged', '--pipe', 'agwinterm-test']

    def test_breakaway_when_the_job_allows_it_with_its_cwd_outside_the_checkout(self):
        popen = Mock(return_value=Mock(pid=5))
        wmi = Mock()
        pid, how = cleanup.start_after_close(self.checkout, 'o/repo', 7, 42, 'merged', popen=popen, wmi=wmi)
        self.assertEqual(self.argv(), popen.call_args.args[0])
        options = popen.call_args.kwargs
        self.assertEqual(str(self.checkout.resolve().parent), options['cwd'])
        self.assertIs(subprocess.DEVNULL, options['stdin'])
        wmi.assert_not_called()
        if os.name == 'nt':
            self.assertEqual((5, 'breakaway'), (pid, how))
            self.assertEqual(cleanup.DETACHED_PROCESS | cleanup.CREATE_NEW_PROCESS_GROUP | cleanup.CREATE_BREAKAWAY_FROM_JOB,
                             options['creationflags'])

    @unittest.skipUnless(os.name == 'nt', 'job breakaway and WMI are Windows only')
    def test_a_job_that_forbids_breakaway_gets_a_wmi_process(self):
        popen = Mock(side_effect=PermissionError(5, 'Access is denied'))
        wmi = Mock(return_value=77)
        self.assertEqual((77, 'wmi'), cleanup.start_after_close(self.checkout, 'o/repo', 7, 42, 'merged', popen=popen, wmi=wmi))
        self.assertEqual((subprocess.list2cmdline(self.argv()), str(self.checkout.resolve().parent)), wmi.call_args.args)

    @unittest.skipUnless(os.name == 'nt', 'job breakaway and WMI are Windows only')
    def test_without_wmi_a_plain_detached_process_is_the_last_resort(self):
        popen = Mock(side_effect=[PermissionError(5, 'Access is denied'), Mock(pid=6)])
        wmi = Mock(side_effect=OSError('Win32_Process.Create failed'))
        self.assertEqual((6, 'detached'), cleanup.start_after_close(self.checkout, 'o/repo', 7, 42, 'build', popen=popen, wmi=wmi))
        self.assertEqual(cleanup.DETACHED_PROCESS | cleanup.CREATE_NEW_PROCESS_GROUP, popen.call_args.kwargs['creationflags'])

    @unittest.skipUnless(os.name == 'nt', 'WMI is Windows only')
    def test_wmi_create_runs_the_command_line_as_given(self):
        folder = ROOT / ('test cleanup wmi ' + uuid.uuid4().hex)
        folder.mkdir()
        self.addCleanup(shutil.rmtree, folder, True)
        out = folder / "it's here.txt"
        line = subprocess.list2cmdline([sys.executable, '-c', 'import os, sys; open(sys.argv[1], "w").write(os.getcwd())', str(out)])
        pid = cleanup.wmi_create(line, str(folder))
        self.assertGreater(pid, 0)
        deadline = time.monotonic() + 30
        while not (out.exists() and out.read_text()) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(str(folder), out.read_text())


class Sweep(Clones):
    def setUp(self):
        super().setUp()
        self.issues = {}
        self.prs = {}
        self.fail = False
        self.lines = []

    def gh(self, *args):
        if self.fail:
            raise conductor.QueueError('gh: HTTP 502')
        if args[:2] == ('issue', 'view'):
            return {'state': self.issues.get(int(args[2]), 'OPEN')}
        if args[:2] == ('pr', 'list'):
            return self.prs.get(args[args.index('--head') + 1], [])
        raise AssertionError(args)

    def sweep(self, tree=EMPTY_TREE, **kwargs):
        self.lines = []
        read = (lambda: tree) if tree is not None else Mock(side_effect=OSError('no pipe'))
        return cleanup.sweep(self.root, gh=self.gh, read_tree=read, out=self.lines.append, **kwargs)

    def output(self):
        return '\n'.join(self.lines)

    def fixture(self):
        merged = self.clone(7)
        self.prs['issue-7-fix'] = [{'number': 40, 'state': 'MERGED', 'headRefOid': run(merged, 'rev-parse', 'HEAD')}]
        still_open = self.clone(8)
        dirty = self.clone(9)
        self.prs['issue-9-fix'] = [{'number': 41, 'state': 'CLOSED', 'headRefOid': 'x'}]
        (dirty / 'notes.txt').write_text('draft', encoding='utf-8')
        closed_issue = self.clone(10)
        self.issues[10] = 'CLOSED'
        reopened = self.clone(11)
        self.prs['issue-11-fix'] = [{'number': 43, 'state': 'CLOSED'}, {'number': 44, 'state': 'OPEN'}]
        other = self.clone(3, name='other', origin='https://github.com/o/other.git')
        self.prs['issue-3-fix'] = [{'number': 1, 'state': 'MERGED'}]
        leftover = self.root / 'repo-issue-5.deleting-1700000000'
        (leftover / 'deep').mkdir(parents=True)
        (leftover / 'deep' / 'f').write_bytes(b'x' * 5)
        (self.root / 'not-a-checkout').mkdir()
        return merged, still_open, dirty, closed_issue, reopened, other, leftover

    def test_dry_run_lists_sizes_and_deletes_nothing(self):
        paths = self.fixture()
        self.assertEqual(1, self.sweep(dry_run=True, repo='o/repo'))
        merged, still_open, dirty, closed_issue, reopened, other, leftover = paths
        out = self.output()
        self.assertRegex(out, rf'candidate {repr(str(merged))[1:-1]} [0-9.]+ (KB|MB) \(PR #40 merged\)')
        self.assertIn(f'candidate {closed_issue} ', out)
        self.assertIn(f'skip {still_open}: issue open and no merged or closed PR', out)
        self.assertIn(f'keep {dirty}: uncommitted changes', out)
        self.assertIn(f'skip {reopened}: PR #44 is open', out)
        self.assertIn(f'leftover {leftover} 5 B', out)
        self.assertNotIn(str(other), out)                                       # -Repo filters by origin
        self.assertNotIn('not-a-checkout', out)
        for path in paths:
            self.assertTrue(path.exists(), path)

    def test_the_real_run_deletes_exactly_the_candidates_that_pass(self):
        merged, still_open, dirty, closed_issue, reopened, other, leftover = self.fixture()
        self.assertEqual(1, self.sweep(repo='o/repo'))
        for gone in (merged, closed_issue, leftover):
            self.assertFalse(gone.exists(), gone)
        for kept in (still_open, dirty, reopened, other):
            self.assertTrue(kept.exists(), kept)
        self.assertIn(f'deleted {merged}: freed', self.output())
        self.assertTrue(self.output().endswith('B'), self.output())             # the total freed
        self.assertEqual({self.root / 'not-a-checkout', other, still_open, reopened, dirty},
                         {p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith('.')})
        self.assertIn('deleted (merged)', (self.root / cleanup.LOG_NAME).read_text(encoding='utf-8'))

    def test_a_closed_issue_with_an_open_pr_is_skipped(self):
        # r1 m4: an open PR on the branch wins over a closed issue.
        checkout = self.clone(12)
        self.issues[12] = 'CLOSED'
        self.prs['issue-12-fix'] = [{'number': 50, 'state': 'OPEN'}]
        self.assertEqual(0, self.sweep())
        self.assertIn(f'skip {checkout}: PR #50 is open', self.output())
        self.assertTrue(checkout.exists())

    @unittest.skipUnless(os.name == 'nt', 'directory junctions are Windows only')
    def test_a_leftover_that_is_a_link_is_never_followed(self):
        # r1 m3
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'precious').write_text('keep', encoding='utf-8')
        link = self.root / 'repo-issue-5.deleting-1700000000'
        junction(link, outside)
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                self.assertEqual(1, self.sweep(dry_run=dry_run))
                self.assertIn(f'keep {link}: is a link', self.output())
                self.assertTrue((outside / 'precious').exists())
                self.assertTrue(link.exists())
        os.rmdir(link)                                                          # the junction, not its target

    def test_without_repo_every_repo_is_swept(self):
        *_, other, _ = self.fixture()
        self.sweep()
        self.assertFalse(other.exists())

    def test_build_only_keeps_the_clones(self):
        merged = self.fixture()[0]
        self.ignore(merged, 'target/')
        (merged / 'target').mkdir()
        (merged / 'target' / 'x').write_bytes(b'x' * 7)
        self.sweep(repo='o/repo', build_only=True)
        self.assertTrue((merged / 'README.md').exists())
        self.assertFalse((merged / 'target').exists())
        self.assertIn(f'cleaned {merged}: freed 7 B', self.output())

    def test_an_unreadable_session_tree_keeps_every_candidate(self):
        merged = self.fixture()[0]
        self.assertEqual(1, self.sweep(tree=None, repo='o/repo'))
        self.assertTrue(merged.exists())
        self.assertIn(f'keep {merged}: sessions: unknown', self.output())

    def test_a_github_error_is_exit_2_and_deletes_nothing(self):
        merged = self.fixture()[0]
        self.fail = True
        self.assertEqual(2, self.sweep(repo='o/repo'))
        self.assertTrue(merged.exists())
        self.assertIn('GitHub lookup failed: gh: HTTP 502', self.output())

    def test_nothing_to_do_is_exit_0(self):
        self.clone(8)
        self.assertEqual(0, self.sweep())
        self.assertEqual(0, cleanup.sweep(self.base / 'missing', gh=self.gh, read_tree=lambda: EMPTY_TREE, out=self.lines.append))


if __name__ == '__main__':
    unittest.main()
