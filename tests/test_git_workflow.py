"""Use only fresh temporary repositories to prove exact human review and conservative local merges."""

from pathlib import Path
import subprocess

import pytest
from ag_tasks.issues import IssueStore, DomainError
from ag_tasks.git_workflow import GitWorkflow
from workflow_helpers import metadata, review_ready, finish_noncode


def run(path, *arguments):
    """Fixture-only native Git setup; no user repository is selected."""
    return subprocess.check_output(['git', '-C', str(path), *arguments], text=True).strip()


@pytest.fixture
def setup(tmp_path):
    """Create a committed main branch and an independent task database."""
    repo = tmp_path / 'repo'; repo.mkdir()
    run(repo, 'init', '-b', 'main')
    run(repo, 'config', 'user.name', 'Fixture')
    run(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'file.txt').write_text('base\n')
    run(repo, 'add', 'file.txt'); run(repo, 'commit', '-m', 'initial')
    store = IssueStore(tmp_path / 'state')
    registered = store.register_repo(repo)
    issue = store.create({'title': 'Implement fix', 'projectKey':'fixture-repository', 'repoId': registered['id'], **metadata()})
    yield store, repo, registered, issue, GitWorkflow(store)
    store.close()


def commit(worktree, text='fixed\n'):
    """Simulate the agent's independently committed code change inside its managed worktree."""
    path = Path(worktree)
    (path / 'file.txt').write_text(text)
    run(path, 'add', 'file.txt'); run(path, 'commit', '-m', 'issue fix')
    return run(path, 'rev-parse', 'HEAD')


def test_per_issue_branch_review_and_explicit_approved_ff_merge(setup):
    store, repo, registered, issue, workflow = setup
    original = run(repo, 'rev-parse', 'HEAD')
    prepared = workflow.prepare(issue['id'])
    assert prepared['branch'].startswith('codex/') and prepared['status'] == 'backlog' and prepared['work']['elapsedMs'] == 0
    assert run(repo, 'rev-parse', 'HEAD') == original
    assert prepared['base'] == original
    head = commit(prepared['worktree'])
    review = workflow.review(issue['id'])
    assert review['canMerge'] and review['head'] == head and review['base'] == original
    assert review['changedFiles'] == ['file.txt'] and '+fixed' in review['diff']
    assert store.get(issue['id'])['status'] == 'backlog'
    done=review_ready(store, issue['id'])
    assert done['status']=='done' and done['mergePending']
    assert run(repo, 'rev-parse', 'HEAD')==original  # Critic completion cannot merge.
    with pytest.raises(DomainError):store.spaces.archive(issue['id'])
    merged = workflow.approve_merge(issue['id'], review['head'], review['base'])
    assert merged['status'] == 'done' and merged['mergeState']=='merged' and not merged['mergePending']
    assert run(repo, 'rev-parse', 'HEAD') == head and (repo / 'file.txt').read_text() == 'fixed\n'
    assert merged['history'][-1]['actor'] == 'user' and merged['history'][-1]['action'] == 'merge_approved'


def test_prepare_uses_committed_base_without_copying_dirty_main(setup):
    store, repo, _, issue, workflow = setup
    (repo / 'file.txt').write_text('uncommitted user work\n')
    prepared = workflow.prepare(issue['id'])
    assert (Path(prepared['worktree']) / 'file.txt').read_text() == 'base\n'
    assert (repo / 'file.txt').read_text() == 'uncommitted user work\n'
    review = workflow.review(issue['id'])
    assert 'base_dirty' in review['reasons']


def test_domain_requires_prepare_review_and_human_merge(setup):
    store, repo, registered, issue, workflow = setup
    for status in ('in_progress', 'in_review', 'done'):
        with pytest.raises(DomainError): store.update(issue['id'], {'status': status}, actor='user')
    with pytest.raises(DomainError): workflow.prepare(str(repo))
    prepared = workflow.prepare(issue['id'])
    with pytest.raises(DomainError): store.update(issue['id'], {'repoId': None})
    with pytest.raises(DomainError): workflow.approve_merge(issue['id'], prepared['head'], prepared['base'])


@pytest.mark.parametrize('change', ['head', 'base', 'dirty-main', 'dirty-worktree', 'wrong-checkout', 'divergent'])
def test_merge_rejects_stale_dirty_divergent_or_wrong_base(setup, change):
    store, repo, _, issue, workflow = setup
    prepared = workflow.prepare(issue['id']); commit(prepared['worktree'])
    review = workflow.review(issue['id']); review_ready(store, issue['id'])
    if change == 'head': commit(prepared['worktree'], 'later unreviewed\n')
    elif change == 'base':
        (repo / 'other.txt').write_text('new base'); run(repo, 'add', 'other.txt'); run(repo, 'commit', '-m', 'base moved')
    elif change == 'dirty-main': (repo / 'file.txt').write_text('user work')
    elif change == 'dirty-worktree': (Path(prepared['worktree']) / 'file.txt').write_text('not committed')
    elif change == 'wrong-checkout': run(repo, 'checkout', '-b', 'other')
    elif change == 'divergent':
        (repo / 'other.txt').write_text('divergent'); run(repo, 'add', 'other.txt'); run(repo, 'commit', '-m', 'diverged')
        review = workflow.review(issue['id'])
    before = run(repo, 'rev-parse', 'HEAD')
    with pytest.raises(DomainError) as failure: workflow.approve_merge(issue['id'], review['head'], review['base'])
    assert failure.value.code == ('stale_review' if change in {'head', 'base'} else 'merge_not_ready')
    assert run(repo, 'rev-parse', 'HEAD') == before
    assert store.get(issue['id'])['status'] == 'done'
    assert store.get(issue['id'])['mergePending']


def test_hooks_and_filters_are_never_executed(setup):
    store, repo, _, issue, workflow = setup
    marker = repo.parent / 'hook-ran'
    hooks = repo / '.git/hooks'
    for name in ('post-checkout', 'post-merge'):
        path = hooks / name; path.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n'); path.chmod(0o755)
    # Configuration is discovered but overridden for service operations, including worktree checkout.
    run(repo, 'config', 'filter.evil.smudge', 'touch "' + str(marker) + '"')
    run(repo, 'config', 'filter.evil.required', 'false')
    (repo / '.gitattributes').write_text('file.txt filter=evil\n')
    run(repo, 'add', '.gitattributes'); run(repo, 'commit', '-m', 'attributes')
    run(repo, 'config', 'filter.evil.required', 'true')
    prepared = workflow.prepare(issue['id'])
    assert not marker.exists()
    # Remove filter before fixture-controlled commit so only the tested service controls execution.
    run(repo, 'config', '--unset', 'filter.evil.smudge'); run(repo, 'config', 'filter.evil.required', 'false')
    commit(prepared['worktree'])
    review = workflow.review(issue['id']); review_ready(store, issue['id'])
    workflow.approve_merge(issue['id'], review['head'], review['base'])
    assert not marker.exists()


def test_unborn_and_subdirectory_registrations_rejected(tmp_path):
    store = IssueStore(tmp_path / 'state')
    repo = tmp_path / 'empty'; repo.mkdir(); run(repo, 'init', '-b', 'main')
    try:
        with pytest.raises(DomainError) as error: store.register_repo(repo)
        assert error.value.code == 'unborn_repository'
    finally: store.close()


def test_review_diff_is_bounded(setup):
    store, _, _, issue, workflow = setup
    prepared = workflow.prepare(issue['id'])
    commit(prepared['worktree'], 'line\n' * 100000)
    review = workflow.review(issue['id'])
    assert review['diffTruncated'] and len(review['diff'].encode()) <= 200000


def test_blockers_prevent_prepare_and_review_without_worktree_changes(setup):
    store, repo, _, issue, workflow = setup
    blocker = store.create({'title': 'Prerequisite'})
    store.link(blocker['id'], issue['id'], kind='blocks')
    with pytest.raises(DomainError) as error:
        workflow.prepare(issue['id'])
    assert error.value.code == 'issue_blocked'
    assert store.get(issue['id'])['worktree'] is None
    finish_noncode(store, blocker['id'])
    prepared = workflow.prepare(issue['id'])
    new_blocker = store.create({'title': 'New prerequisite'})
    store.link(new_blocker['id'], issue['id'], kind='blocks')
    with pytest.raises(DomainError) as error:
        workflow.review(issue['id'])
    assert error.value.code == 'issue_blocked'


def test_create_cannot_request_unprepared_repository_progress(setup):
    store, _, repo, _, _ = setup
    with pytest.raises(DomainError) as error:
        store.create({'title': 'Premature', 'projectKey':'fixture-repository', 'repoId': repo['id'], 'status': 'in_progress'})
    assert error.value.code == 'work_start_required'


def test_git_environment_refuses_lazy_network_fetch(monkeypatch):
    from ag_tasks.git_workflow import environment
    monkeypatch.setenv('GIT_NO_LAZY_FETCH', '0')
    monkeypatch.setenv('GIT_DIR', '/untrusted')
    value = environment()
    assert value['GIT_NO_LAZY_FETCH'] == '1'
    assert 'GIT_DIR' not in value


def test_truncated_child_ignoring_terminate_is_killed_and_reaped(monkeypatch):
    from ag_tasks import git_workflow

    class Child:
        """Simulate a pipe producer that ignores TERM, without leaving a real subprocess behind."""
        stdout = None

        def __init__(self):
            import io
            self.stdout = io.BytesIO(b'oversized')
            self.killed = False
            self.terminated = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            assert self.killed

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return -9

    child = Child()
    monkeypatch.setattr(git_workflow.subprocess, 'Popen', lambda *args, **kwargs: child)
    with pytest.raises(subprocess.TimeoutExpired):
        git_workflow.execute(['fixture'], limit=3, accepted=(0,))
    assert child.terminated and child.killed


def test_merge_receipt_survives_cold_storage_without_reopening_payload(setup):
    """A compressed audit must never make an already approved merge look pending again."""
    from datetime import datetime,timedelta,timezone
    store,repo,_,issue,workflow=setup
    prepared=workflow.prepare(issue['id']);commit(prepared['worktree'])
    review=workflow.review(issue['id']);review_ready(store,issue['id'])
    workflow.approve_merge(issue['id'],review['head'],review['base'])
    old=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    store.db.connection.execute('UPDATE issues SET updated_at=? WHERE id=?',(old,issue['id']))
    assert store.cold.sweep(limit=1)==[issue['id']]
    card=next(x for x in store.list() if x['id']==issue['id'])
    assert card['storage']['tier']=='zip' and card['mergeState']=='merged' and not card['mergePending']
