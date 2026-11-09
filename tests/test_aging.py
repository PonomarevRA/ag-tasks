"""Inactivity acceptance: relevance follows real activity, not storage reads or session loss."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from ag_tasks.app import create_app
from ag_tasks.issues import IssueStore, DomainError
from workflow_helpers import metadata, review_ready


@pytest.fixture
def store(tmp_path):
    """Keep all mutations in an isolated database."""
    value = IssueStore(tmp_path / 'state')
    yield value
    value.close()


def age(store, issue_id, days=4, now=None):
    """Set only the test clock's persisted age; production exposes no age-write API."""
    instant = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    store.db.connection.execute('UPDATE issues SET last_activity_at=?,updated_at=? WHERE id=?', (instant.isoformat(), instant.isoformat(), issue_id))


def test_three_day_boundary_idempotence_and_human_revalidation(store):
    now = datetime.now(timezone.utc)
    issue = store.create({'title': 'Old', **metadata()})
    age(store, issue['id'], 3, now)
    assert store.aging.sweep(now=now - timedelta(microseconds=1)) == []
    assert store.aging.sweep(now=now) == [issue['id']]
    assert store.get(issue['id'])['status'] == 'stale'
    assert store.aging.sweep(now=now) == []
    with pytest.raises(DomainError, match='Restore/reopen'):
        store.execution.start(issue['id'], {'agentId': 'worker', 'sessionKey': 'not-authorized'})
    with pytest.raises(DomainError) as error:
        store.update(issue['id'], {'status': 'ready'})
    assert error.value.code == 'revalidation_required'
    reopened = store.planning.revalidate(issue['id'], {'reason': 'Human confirmed relevance'})
    assert reopened['status'] == 'backlog' and not reopened['dor'][0]['passed']
    assert store.aging.sweep() == []


def test_active_or_recent_child_protects_old_parent_and_terminal_records(store):
    parent = store.create({'title': 'Plan', 'kind': 'rnd'})
    child = store.create({'title': 'Child', 'parentId': parent['id'], **metadata()})
    age(store, parent['id'])
    assert store.aging.sweep() == []
    active = store.execution.start(child['id'], {'agentId': 'worker', 'sessionKey': 'live'})
    age(store, child['id'])
    age(store, parent['id'])
    assert store.aging.sweep() == []
    store.execution.stop(child['id'], {'agentId': 'worker', 'sessionKey': 'live', 'sessionId': active['work']['activeSession']['sessionId']})
    assert store.aging.sweep() == []  # accepted stop is real activity for its parent
    done = store.create({'title': 'Done', **metadata()})
    review_ready(store, done['id'])
    age(store, done['id'])
    assert store.aging.sweep() == []


def test_pending_result_expires_with_critic_and_retains_evidence(store):
    issue = store.create({'title': 'Pending', **metadata()})
    active = store.execution.start(issue['id'], {'agentId': 'producer', 'sessionKey': 'work'})
    result = store.results.submit(issue['id'], {'agentId': 'producer', 'sessionKey': 'work', 'sessionId': active['work']['activeSession']['sessionId'],
        'result': {'summary': 'Done by author', 'changes': ['change'], 'validation': ['check'], 'limitations': '', 'artifacts': []}})
    age(store, issue['id'])
    age(store, result['criticReviewId'])
    assert store.aging.sweep() == [issue['id']]
    assert store.get(result['criticReviewId'])['status'] == 'cancelled'
    saved = store.get(issue['id'])
    assert saved['status'] == 'stale' and saved['currentResult']['summary'] == 'Done by author'
    assert saved['resultsCount'] == 1


def test_zip_restore_does_not_renew_relevance(store):
    issue = store.create({'title': 'Cold'})
    age(store, issue['id'], 8)
    prior = store.get(issue['id'])['lastActivityAt']
    assert store.cold.sweep() == [issue['id']]
    assert store.get(issue['id'])['lastActivityAt'] == prior
    assert store.aging.sweep() == [issue['id']]


def test_startup_catches_up_without_ui_reads(tmp_path):
    data = tmp_path / 'startup'
    original = IssueStore(data)
    issue = original.create({'title': 'App was closed'})
    age(original, issue['id'])
    original.close()
    app = create_app(data)
    with TestClient(app, base_url='http://127.0.0.1:8895'):
        row = app.state.store.db.connection.execute('SELECT status FROM issues WHERE id=?', (issue['id'],)).fetchone()
        assert row['status'] == 'stale'


def test_expired_lease_is_not_a_critic_rejection_and_blockers_are_explicit(store):
    issue = store.create({'title': 'Lease', **metadata()})
    active = store.execution.start(issue['id'], {'agentId': 'worker', 'sessionKey': 'old'})
    sid = active['work']['activeSession']['sessionId']
    store.execution.ticks[sid] -= 100
    expired = store.get(issue['id'])
    assert expired['status'] == 'ready' and expired['lifecycle']['phase'] == 'lease_expired'
    active = store.execution.start(issue['id'], {'agentId': 'worker', 'sessionKey': 'new'})
    paused = store.execution.stop(issue['id'], {'agentId': 'worker', 'sessionKey': 'new', 'sessionId': active['work']['activeSession']['sessionId'],
        'reason': 'blocked', 'nextAction': 'Restore access to the test host'})
    assert paused['status'] == 'blocked'
    assert paused['work']['nextAction'] == 'Restore access to the test host'


def test_batch_bound_and_no_housekeeping_activity(store):
    for number in range(4):
        issue = store.create({'title': str(number)})
        age(store, issue['id'])
    assert len(store.aging.sweep(limit=2)) == 2
    assert len(store.aging.sweep(limit=2)) == 2
    assert store.aging.sweep() == []
