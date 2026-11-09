"""Independent workflow admission regressions; every database and task is temporary, with no repository side effects."""

from pathlib import Path
import subprocess

import pytest

from ag_tasks.git_workflow import GitWorkflow

from ag_tasks.issues import DomainError, IssueStore


@pytest.fixture
def store(tmp_path):
    """Own only a fresh temporary database and release its process lock after each scenario."""
    value = IssueStore(tmp_path / 'state')
    try:
        yield value
    finally:
        value.close()


def criteria():
    """Use explicit fixture evidence; no caller-provided claim is represented as authenticated execution."""
    return [{'id': 'inspected', 'text': 'Fixture inputs inspected', 'passed': True, 'evidence': 'Fixture source check'}]


def session(store, identifier, agent, key):
    """Return the server-issued session ID together with its original declared identity and retry key."""
    issue = store.execution.start(identifier, {'agentId': agent, 'sessionKey': key})
    return {'agentId': agent, 'sessionKey': key, 'sessionId': issue['work']['activeSession']['sessionId']}


def submitted(store):
    """Create a real work interval and immutable result, yielding its generated critic child."""
    issue = store.create({'title': 'Fixture implementation', 'status': 'ready', 'intendedRole': 'developer',
                          'dor': criteria(), 'dod': criteria()})
    producer = session(store, issue['id'], 'producer', 'produce')
    return store.results.submit(issue['id'], {**producer, 'result': {
        'summary': 'Fixture result', 'changes': ['A source change'], 'validation': ['Caller-reported inspection'],
        'limitations': 'No authenticated test execution', 'artifacts': []}})


def report(parent, credentials, verdict='pass'):
    """Bind a critic's declaration to the exact current result, definition and captured head."""
    return {**credentials, 'generation': parent['resultGeneration'], 'definitionRevision': parent['definitionRevision'],
            'reviewedHead': parent['currentResult']['reviewedHead'], 'verdict': verdict,
            'summary': 'Fixture critic report', 'findings': ['Fix the fixture implementation'] if verdict == 'rework' else [], 'evidence': 'Fixture changes and result inspected'}


def critic_passed(store):
    """Establish a valid distinct critic report before testing invalidation or final admission."""
    parent = submitted(store)
    critic = session(store, parent['criticReviewId'], 'critic', 'review')
    return store.results.report(parent['criticReviewId'], report(parent, critic))


def test_agent_cannot_create_in_review_without_a_result(store):
    """Initial status must obey the same result gate as later transitions; no audit/issue may be partially created."""
    with pytest.raises(DomainError) as error:
        store.create({'title': 'Bypass review', 'status': 'in_review'}, actor='agent')
    assert error.value.status in (400, 409)
    assert store.list() == []
    assert store.db.connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 0


def test_ui_cannot_create_done_without_result_or_critic(store):
    """Human final authority does not permit constructing a finished issue that skipped its required workflow."""
    with pytest.raises(DomainError) as error:
        store.create({'title': 'Bypass completion', 'status': 'done'}, actor='user')
    assert error.value.status in (400, 409)
    assert store.list() == []
    assert store.db.connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 0


def test_producer_cannot_acquire_own_generated_critic_session(store):
    """Reject self-review at lease acquisition, before the producer can occupy the legitimate critic's session."""
    parent = submitted(store)
    before = store.get(parent['criticReviewId'])
    with pytest.raises(DomainError) as error:
        session(store, parent['criticReviewId'], 'producer', 'self-review')
    assert error.value.status == 409
    after = store.get(parent['criticReviewId'])
    assert after['work'] == before['work']
    assert after['historyCount'] == before['historyCount']
    independent = session(store, parent['criticReviewId'], 'independent', 'legitimate')
    assert independent['sessionId']


@pytest.mark.parametrize('verdict', ['pass', 'rework'])
def test_active_critic_cannot_revive_or_report_on_human_cancelled_parent(store, verdict):
    """A current generation alone cannot override a later human terminal decision."""
    parent = submitted(store)
    critic = session(store, parent['criticReviewId'], 'critic', 'review')
    before = store.update(parent['id'], {'status': 'cancelled'}, actor='user')
    critic_before = store.get(parent['criticReviewId'])
    with pytest.raises(DomainError) as error:
        store.results.report(parent['criticReviewId'], report(parent, critic, verdict))
    assert error.value.status == 409
    after = store.get(parent['id'])
    assert after['status'] == 'cancelled' and after['historyCount'] == before['historyCount']
    assert after['criticReview'] == before['criticReview']
    assert store.get(parent['criticReviewId'])['historyCount'] == critic_before['historyCount']
    assert store.db.connection.execute('SELECT COUNT(*) FROM review_reports').fetchone()[0] == 0


@pytest.mark.parametrize('patch', [{'intendedRole': 'developer'}, {'parentId': 'other'}, {'dor': []}])
@pytest.mark.parametrize('actor', ['agent', 'user'])
def test_generated_critic_metadata_cannot_be_repurposed(store, patch, actor):
    """Neither route actor can turn a generated reviewer into ordinary work or reparent its proof to another result."""
    parent = submitted(store)
    other = store.create({'title': 'Another parent'})
    patch = dict(patch)
    if patch.get('parentId') == 'other':
        patch['parentId'] = other['id']
    before = store.get(parent['criticReviewId'])
    with pytest.raises(DomainError):
        store.update(parent['criticReviewId'], patch, actor=actor)
    after = store.get(parent['criticReviewId'])
    for key in ('parentId', 'intendedRole', 'kind', 'dor', 'definitionRevision', 'historyCount'):
        assert after[key] == before[key]
    assert store.get(parent['id'])['criticReviewId'] == before['id']


def test_definition_edit_and_done_cannot_reuse_previous_critic_admission(store):
    """An atomic rollback or accepted edit-to-rework are valid; completing a revised definition under old proof is never valid."""
    parent = critic_passed(store)
    before = store.get(parent['id'])
    assert before['criticReview']['verdict'] == 'pass'
    try:
        store.update(parent['id'], {'title': 'Revised requirement', 'status': 'done'}, actor='user')
    except DomainError as error:
        assert error.status == 409
    after = store.get(parent['id'])
    assert after['status'] != 'done'
    if after['title'] == 'Revised requirement':
        assert after['status'] == 'rework'
        assert after['definitionRevision'] > before['definitionRevision']
        assert after['criticReview'] is None
    else:
        assert after['title'] == before['title']
        assert after['definitionRevision'] == before['definitionRevision']
        assert after['historyCount'] == before['historyCount']


def test_stale_critic_pass_is_not_projected_as_current_after_definition_edit(store):
    """Retain historical proof in storage while exposing null current criticReview for a changed definition."""
    parent = critic_passed(store)
    before = store.get(parent['id'])
    changed = store.update(parent['id'], {'description': 'Changed completion expectations'}, actor='agent')
    assert changed['definitionRevision'] > before['definitionRevision']
    assert changed['status'] == 'rework' and changed['criticReview'] is None
    assert changed['currentResult']['definitionRevision'] == before['definitionRevision']
    assert store.db.connection.execute('SELECT COUNT(*) FROM review_reports').fetchone()[0] == 1
    with pytest.raises(DomainError):
        store.update(parent['id'], {'status': 'done'}, actor='user')


def fixture_git(path, *arguments):
    """Run only fixture setup/inspection commands against this test's temporary registered repository."""
    return subprocess.check_output(['git', '-C', str(path), *arguments], text=True).strip()


def test_human_cancellation_during_git_review_prevents_merge(store, tmp_path, monkeypatch):
    """Recheck human cancellation after slow Git inspection, before changing any real repository ref."""
    repo = tmp_path / 'fixture-repository'
    repo.mkdir()
    fixture_git(repo, 'init', '-b', 'main')
    fixture_git(repo, 'config', 'user.name', 'Fixture')
    fixture_git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'change.txt').write_text('base\n')
    fixture_git(repo, 'add', 'change.txt')
    fixture_git(repo, 'commit', '-m', 'fixture base')
    original = fixture_git(repo, 'rev-parse', 'HEAD')
    registered = store.register_repo(repo)
    parent = store.create({'languages': ['Python'], 'systems': ['Fixture system'], 'title': 'Reviewed fixture change', 'projectKey':'fixture-repository', 'status': 'ready', 'repoId': registered['id'],
                           'intendedRole': 'developer', 'dor': criteria(), 'dod': criteria()})
    workflow = GitWorkflow(store)
    prepared = workflow.prepare(parent['id'])
    producer = session(store, parent['id'], 'producer', 'implement')
    worktree = Path(prepared['worktree'])
    (worktree / 'change.txt').write_text('reviewed change\n')
    fixture_git(worktree, 'add', 'change.txt')
    fixture_git(worktree, 'commit', '-m', 'fixture implementation')
    parent = store.results.submit(parent['id'], {**producer, 'result': {
        'summary': 'Committed fixture change', 'changes': ['change.txt'], 'validation': ['Fixture inspection'],
        'limitations': 'Declared fixture evidence', 'artifacts': []}})
    critic = session(store, parent['criticReviewId'], 'independent-critic', 'review')
    parent = store.results.report(parent['criticReviewId'], report(parent, critic))
    assert parent['status'] == 'done' and parent['mergePending'] and parent['criticReview']['verdict'] == 'pass'
    real_review = workflow._review

    def cancel_after_inspection(identifier, *, persist=True):
        """Deterministically emulate an independent human cancellation during the unlocked Git read."""
        snapshot = real_review(identifier, persist=persist)
        assert persist is False and snapshot['canMerge']
        store.update(identifier, {'status': 'cancelled'}, actor='user')
        return snapshot

    monkeypatch.setattr(workflow, '_review', cancel_after_inspection)
    with pytest.raises(DomainError) as error:
        workflow.approve_merge(parent['id'], parent['reviewedHead'], parent['reviewedBase'])
    assert error.value.status == 409
    assert store.get(parent['id'])['status'] == 'cancelled'
    assert fixture_git(repo, 'rev-parse', 'HEAD') == original
    assert (repo / 'change.txt').read_text() == 'base\n'
    assert not any(item['action'] == 'merge_approved' for item in store.get(parent['id'])['history'])
