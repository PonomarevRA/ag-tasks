"""Conservative local Git workflow: registered repositories, managed worktrees and exact human-reviewed fast-forward."""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import threading

from .issues import DomainError

GIT_TIMEOUT = 30
MAX_DIFF_BYTES = 200_000
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_thread_locks = {}
_locks_gate = threading.Lock()


def environment():
    """Ignore inherited git repository/config injection; disable interactive prompting and optional filesystem locks."""
    result = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', LC_ALL='C')
    return result


def execute(arguments, *, limit, accepted, mutation=False):
    """Bound output in memory without temporary disk spooling; terminate timed-out or overlong read commands."""
    with subprocess.Popen(arguments, env=environment(), stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL if mutation else subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        expired = threading.Event()
        def timeout():
            """Terminate only this owned Git child when its fixed operation deadline expires."""
            expired.set()
            try:
                process.kill()
            except ProcessLookupError:
                pass
        timer = threading.Timer(GIT_TIMEOUT, timeout)
        timer.daemon = True
        timer.start()
        try:
            raw = b'' if mutation else process.stdout.read(limit + 1)
            truncated = len(raw) > limit
            if truncated and process.poll() is None:
                process.terminate()
            try:
                code = process.wait(timeout=2 if truncated else GIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                # A truncated producer may ignore TERM. Reap our child before Popen.__exit__ can wait forever.
                process.kill()
                process.wait(timeout=2)
                raise
        finally:
            timer.cancel()
        if expired.is_set():
            raise DomainError('git_timeout', 'The local Git operation exceeded its deadline.', 409)
        if not truncated and code not in accepted:
            raise DomainError('git_operation_failed', 'Git operation failed; inspect the registered repository.', 409)
        return raw[:limit].decode('utf-8', errors='replace'), truncated, code


def command_prefix(path):
    """Disable hooks, fsmonitor and all configured checkout filters before any worktree/merge operation."""
    prefix = ['git', '--no-pager', '-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false', '-c', 'commit.gpgSign=false', '-c', 'maintenance.auto=false', '-c', 'gc.auto=0', '-c', 'submodule.recurse=false', '-c', 'merge.autoStash=false', '-C', str(path)]
    names, truncated, _ = execute(prefix + ['config', '--name-only', '--get-regexp', r'^filter\..*\.(process|smudge|clean|required)$'], limit=65536, accepted=(0, 1))
    if truncated:
        raise DomainError('git_configuration_unsupported', 'Git filter configuration is too large.', 409)
    for key in names.splitlines():
        if not re.fullmatch(r'filter\.[^\x00\r\n=]+\.(process|smudge|clean|required)', key):
            raise DomainError('git_configuration_unsupported', 'Unsupported Git filter configuration.', 409)
        prefix += ['-c', key + ('=false' if key.endswith('.required') else '=')]
    return prefix


def git(path, *arguments, limit=65536, accepted=(0,)):
    """Run argv-only Git with bounded in-memory stdout, discarded stderr and no configured command hooks."""
    try:
        prefix = command_prefix(path)
        return execute(prefix + list(arguments), limit=limit, accepted=accepted,
                       mutation=arguments[0] in ('worktree', 'merge'))
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise DomainError('git_unavailable', 'Git could not finish the local operation.', 409) from error


def inspect_repo(path, base_branch=None):
    """Verify an explicit human-selected local working repository and committed local base branch; reject unborn/bare roots."""
    if not isinstance(path, (str, Path)):
        raise DomainError('invalid_repository', 'Choose a local repository directory.')
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise DomainError('invalid_repository', 'Repository directory does not exist.')
    top, _, _ = git(root, 'rev-parse', '--show-toplevel')
    if Path(top.strip()).resolve() != root:
        raise DomainError('invalid_repository', 'Register the exact Git working repository root.')
    branch = base_branch
    if branch is None:
        branch, _, code = git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD', accepted=(0, 1))
        branch = branch.strip() if code == 0 else None
    if type(branch) is not str or not branch or len(branch) > 128 or branch.startswith('-'):
        raise DomainError('invalid_base_branch', 'Choose an existing local base branch.')
    git(root, 'check-ref-format', '--branch', branch)
    commit, _, code = git(root, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch + '^{commit}', accepted=(0, 1))
    if code != 0 or not SHA.fullmatch(commit.strip()):
        raise DomainError('unborn_repository', 'Repository needs an initial commit on the selected base branch.', 409)
    return {'path': str(root), 'name': root.name, 'baseBranch': branch, 'head': commit.strip()}


class GitWorkflow:
    """Keep branch preparation separate from review and a human-only exact-SHA merge operation."""

    def __init__(self, store):
        """Use only repository IDs stored by human registration; agents never supply repository/worktree paths."""
        self.store = store

    @contextmanager
    def _guard(self, repo_id):
        """Serialize service workflow operations per repository without blocking unrelated issue reads."""
        key = str(self.store.data_dir) + ':' + repo_id
        with _locks_gate:
            lock = _thread_locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise DomainError('repository_busy', 'Another workflow operation is active for this repository.', 409)
        try:
            # The local lock covers application threads. Git itself owns ref/index locks against other processes.
            yield
        finally:
            lock.release()

    def _context(self, issue_id):
        """Revalidate a registered root and resolve only this issue's managed worktree and fixed branch."""
        issue = self.store.get(issue_id)
        if not issue['repoId']:
            raise DomainError('repository_required', 'Select a registered repository first.', 409)
        repo = self.store.repo(issue['repoId'])
        verified = inspect_repo(repo['path'], repo['baseBranch'])
        root = self.store.data_dir / 'worktrees'
        root.mkdir(exist_ok=True)
        if root.is_symlink():
            raise DomainError('invalid_worktree', 'Managed worktree directory cannot be a symlink.', 409)
        managed = root / issue['id']
        if managed.is_symlink() or managed.resolve().parent != root.resolve():
            raise DomainError('invalid_worktree', 'Invalid managed worktree path.', 409)
        return issue, repo, verified, managed

    def prepare(self, issue_id):
        """Create one codex branch/worktree from the committed base, never copying uncommitted main-tree changes."""
        issue, repo, verified, managed = self._context(issue_id)
        with self._guard(repo['id']):
            issue = self.store.get(issue_id)
            if issue['status'] in ('done', 'cancelled', 'stale'):
                raise DomainError('issue_closed', 'Reopen an issue before preparing its worktree.', 409)
            with self.store.db.lock:
                self.store._ensure_unblocked(issue_id)
            branch = 'codex/ag-tasks-' + issue['key'].lower()
            if issue['branch']:
                self._check_worktree(issue, repo, managed)
                return issue
            if managed.exists():
                raise DomainError('worktree_conflict', 'Managed worktree already exists; inspect it before retrying.', 409)
            git(repo['path'], 'worktree', 'add', '-b', branch, str(managed), verified['head'])
            return self.store._record_workflow(issue_id, {'branch': branch, 'worktree': str(managed), 'baseBranch': repo['baseBranch'],
                    'base': verified['head'], 'head': verified['head'], 'reviewedHead': None, 'reviewedBase': None}, 'worktree_prepared', actor='agent')

    @staticmethod
    def _check_worktree(issue, repo, managed):
        """Refuse replaced paths, detached HEADs or worktrees belonging to a different registered repository."""
        if not issue['branch'] or issue['worktree'] != str(managed) or not managed.is_dir():
            raise DomainError('prepare_required', 'Prepare the issue worktree first.', 409)
        branch, _, _ = git(managed, 'symbolic-ref', '--quiet', '--short', 'HEAD')
        common, _, _ = git(managed, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        original, _, _ = git(repo['path'], 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if branch.strip() != issue['branch'] or Path(common.strip()).resolve() != Path(original.strip()).resolve():
            raise DomainError('worktree_changed', 'The managed worktree no longer matches this issue.', 409)

    def _review(self, issue_id, *, persist=True):
        """Capture a bounded exact commit/diff snapshot without changing the issue's kanban status."""
        issue, repo, verified, managed = self._context(issue_id)
        self._check_worktree(issue, repo, managed)
        with self.store.db.lock:
            self.store._ensure_unblocked(issue_id)
        head, _, _ = git(managed, 'rev-parse', '--verify', 'HEAD')
        head, base = head.strip(), verified['head']
        work_status, work_truncated, _ = git(managed, 'status', '--porcelain', '--untracked-files=all')
        base_status, base_truncated, _ = git(repo['path'], 'status', '--porcelain', '--untracked-files=all')
        current_branch, _, code = git(repo['path'], 'symbolic-ref', '--quiet', '--short', 'HEAD', accepted=(0, 1))
        _, _, ancestry = git(repo['path'], 'merge-base', '--is-ancestor', base, head, accepted=(0, 1))
        names, names_truncated, _ = git(repo['path'], 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', base, head, limit=65536)
        files = names.split('\0')
        if files and files[-1] == '':
            files.pop()
        diff, truncated, _ = git(repo['path'], 'diff', '--no-ext-diff', '--no-textconv', '--no-color', base, head, '--', limit=MAX_DIFF_BYTES)
        count, _, _ = git(repo['path'], 'rev-list', '--count', base + '..' + head)
        reasons = []
        if work_status or work_truncated:
            reasons.append('worktree_dirty')
        if base_status or base_truncated:
            reasons.append('base_dirty')
        if code != 0 or current_branch.strip() != repo['baseBranch']:
            reasons.append('base_not_checked_out')
        if ancestry != 0:
            reasons.append('divergent_history')
        if head == base:
            reasons.append('no_changes')
        result = {'issueId': issue_id, 'head': head, 'base': base, 'baseBranch': repo['baseBranch'], 'clean': not work_status and not base_status,
                  'canMerge': not reasons, 'reasons': reasons, 'changedFiles': files[:500], 'changedFilesTruncated': names_truncated or len(files) > 500,
                  'commitCount': int(count.strip()), 'diff': diff, 'diffTruncated': truncated}
        if persist:
            self.store._record_workflow(issue_id, {'head': head, 'base': base, 'reviewedHead': head, 'reviewedBase': base}, 'review_captured', actor='agent')
        return result

    def review(self, issue_id):
        """Refresh real git head/base and evidence only; the caller explicitly requests in_review afterwards."""
        issue = self.store.get(issue_id)
        if not issue['repoId']:
            raise DomainError('repository_required', 'Select a registered repository first.', 409)
        with self._guard(issue['repoId']):
            return self._review(issue_id)

    def approve_merge(self, issue_id, expected_head, expected_base):
        """Human-only adapter entry: recheck exact reviewed SHAs, both clean trees and base checkout, then fast-forward locally."""
        if not all(type(value) is str and SHA.fullmatch(value) for value in (expected_head, expected_base)):
            raise DomainError('invalid_review', 'Full exact commit SHAs are required.')
        issue = self.store.get(issue_id)
        if not issue['repoId']:
            raise DomainError('repository_required', 'Repository issue required.', 409)
        with self._guard(issue['repoId']):
            issue = self.store.get(issue_id)
            if issue['status'] not in ('in_review', 'done') or issue['reviewedHead'] != expected_head or issue['reviewedBase'] != expected_base:
                raise DomainError('review_required', 'Capture and request the exact review before approving.', 409)
            snapshot = self._review(issue_id, persist=False)
            if snapshot['head'] != expected_head or snapshot['base'] != expected_base:
                raise DomainError('stale_review', 'Commits changed after review; capture a new review.', 409)
            if not snapshot['canMerge']:
                raise DomainError('merge_not_ready', 'Merge refused: ' + ', '.join(snapshot['reasons']) + '.', 409)
            with self.store.db.lock:
                current = self.store.get(issue_id)
                if current['status'] not in ('in_review', 'done') or current['archivedAt'] or current['reviewedHead'] != expected_head or current['reviewedBase'] != expected_base:
                    raise DomainError('review_required', 'The issue changed before merge admission.', 409)
                self.store.results.guard(issue_id, self.store.db.connection, complete=True)
                repo = self.store.repo(issue['repoId'])
                # A separate user's Git process is outside this application's ownership lock. Recheck immediately
                # before invoking Git, which still uses its native ref/index locks; never reset or overwrite on failure.
                final_base, _, _ = git(repo['path'], 'rev-parse', 'HEAD')
                if final_base.strip() != expected_base:
                    raise DomainError('stale_review', 'The base changed immediately before merge.', 409)
                git(repo['path'], 'merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', expected_head)
                actual, _, _ = git(repo['path'], 'rev-parse', 'HEAD')
                if actual.strip() != expected_head:
                    raise DomainError('merge_verification_failed', 'The base changed unexpectedly; inspect the repository.', 409)
                return self.store._record_workflow(issue_id, {'head': expected_head, 'base': expected_head, 'mergedHead':expected_head, 'status': 'done'}, 'merge_approved', actor='user')
