"""Code classification and reject/rework/re-review admission tests on private temporary databases."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.issues import DomainError, IssueStore
from tests.workflow_helpers import metadata


@pytest.fixture
def store(tmp_path):
    value = IssueStore(tmp_path)
    yield value
    value.close()


def work(store, identifier, agent, key):
    """Start a real fixture session and retain its exact server-issued identity."""
    active = store.execution.start(identifier, {'agentId': agent, 'sessionKey': key})
    return {'agentId': agent, 'sessionKey': key, 'sessionId': active['work']['activeSession']['sessionId']}


def content():
    return {'summary': 'Implemented fixture', 'changes': ['Changed a function'], 'validation': ['Fixture checks'], 'limitations': '', 'artifacts': []}


def review(store, parent, verdict, findings):
    """Submit the current generated critic child using a separate declared work identity."""
    credentials = work(store, parent['criticReviewId'], 'critic', 'review-' + str(parent['resultGeneration']))
    return store.results.report(parent['criticReviewId'], {**credentials, 'generation': parent['resultGeneration'],
        'definitionRevision': parent['definitionRevision'], 'reviewedHead': parent['reviewedHead'],
        'verdict': verdict, 'summary': 'Reviewed exact result', 'findings': findings, 'evidence': 'Inspected fixture source'})


def test_code_result_rework_requires_reasons_and_fresh_review(store):
    """Rejected findings survive new work/results; old approval cannot finish the corrected generation."""
    issue = store.create({'title': 'Code change', 'projectKey':'fixture', 'codeTask': True, 'languages': ['Python', 'SQL'], 'systems': ['ag-tasks', 'ag-observer'], **metadata()})
    assert issue['codeReviewStage'] == 'pending'
    first = store.results.submit(issue['id'], {**work(store, issue['id'], 'producer', 'first'), 'result': content()})
    assert first['status'] == 'in_review' and first['codeReviewStage'] == 'in_review'
    critic = store.get(first['criticReviewId'])
    assert critic['title'].startswith('Code review:') and critic['languages'] == issue['languages']
    assert first['currentResult']['workContext']['systems'] == ['ag-tasks', 'ag-observer']
    rejected = review(store, first, 'rework', ['Handle empty SQL results'])
    assert rejected['status'] == 'rework' and rejected['codeReviewStage'] == 'rework'
    assert rejected['work']['activeSession'] is None
    with pytest.raises(DomainError, match='critic'):
        store.update(issue['id'], {'status': 'done'}, actor='user')
    credentials = work(store, issue['id'], 'producer', 'fix')
    while_fixing = store.get(issue['id'])
    assert while_fixing['criticReview'] is None
    assert while_fixing['reviewReports'][0]['findings'] == ['Handle empty SQL results']
    second = store.results.submit(issue['id'], {**credentials, 'result': content()})
    assert second['criticReviewId'] != first['criticReviewId']
    with pytest.raises(DomainError):
        store.update(issue['id'], {'status': 'done'}, actor='user')
    approved = review(store, second, 'pass', [])
    assert approved['codeReviewStage'] == 'passed' and len(approved['reviewReports']) == 2
    assert store.update(issue['id'], {'status': 'done'}, actor='user')['status'] == 'done'


def test_rejection_without_reason_is_atomic(store):
    issue = store.create({'title': 'Reject reason', **metadata()})
    parent = store.results.submit(issue['id'], {**work(store, issue['id'], 'producer', 'first'), 'result': content()})
    with pytest.raises(DomainError) as error:
        review(store, parent, 'rework', [])
    assert error.value.code == 'review_findings_required'
    assert store.get(issue['id'])['status'] == 'in_review'
    assert store.get(issue['id'])['reviewReports'] == []


def test_labels_are_bounded_normalized_and_invalidate_acceptance(store):
    issue = store.create({'title': 'Classification', 'projectKey':'fixture', 'languages': ['Python', 'python', ' SQL '], 'systems': ['Atlas'], **metadata()})
    assert issue['codeTask'] is True and issue['languages'] == ['Python', 'SQL']
    parent = store.results.submit(issue['id'], {**work(store, issue['id'], 'producer', 'first'), 'result': content()})
    review(store, parent, 'pass', [])
    changed = store.update(issue['id'], {'systems': ['Atlas', 'Observer']})
    assert changed['status'] == 'rework' and changed['definitionRevision'] == 1
    assert changed['criticReview'] is None and len(changed['reviewReports']) == 1
    assert changed['currentResult']['workContext']['systems'] == ['Atlas']
    assert next(row for row in store.list() if row['id'] == issue['id'])['systems'] == ['Atlas', 'Observer']
    for patch in [{'languages': 'Python'}, {'languages': ['']}, {'systems': ['x'] * 17}, {'systems': ['x\nname']}, {'codeTask': 1}]:
        with pytest.raises(DomainError):
            store.update(issue['id'], patch)


def test_code_result_needs_languages_and_systems_without_closing_session(store):
    issue = store.create({'title': 'Draft code', 'projectKey':'fixture', 'codeTask': True, **metadata()})
    credentials = work(store, issue['id'], 'producer', 'draft')
    with pytest.raises(DomainError) as error:
        store.results.submit(issue['id'], {**credentials, 'result': content()})
    assert error.value.code == 'code_context_required'
    current = store.get(issue['id'])
    assert current['work']['activeSession']['sessionId'] == credentials['sessionId'] and current['resultsCount'] == 0


def test_v3_migration_retains_keys_history_and_defaults(tmp_path):
    original = IssueStore(tmp_path)
    issue = original.create({'title': 'Existing task'})
    original.close()
    with sqlite3.connect(tmp_path / 'issues.sqlite3') as connection:
        for field in ('code_task', 'languages', 'systems'):
            connection.execute('ALTER TABLE issues DROP COLUMN ' + field)
        connection.execute('PRAGMA user_version=3')
    reopened = IssueStore(tmp_path)
    try:
        current = reopened.get(issue['id'])
        assert current['key'] == issue['key'] and current['historyCount'] == issue['historyCount']
        assert current['codeTask'] is False and current['systems'] == []
        assert reopened.db.connection.execute('PRAGMA user_version').fetchone()[0] == 6
    finally:
        reopened.close()


def test_http_plans_tasks_and_decomposition_keep_classification(tmp_path):
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        plan = client.post('/api/v1/agent/plans', json={'title':'Code plan', 'markdown':'# Plan', 'projectKey':'fixture', 'codeTask':True, 'languages':['Python'], 'systems':['Tasks']})
        assert plan.status_code == 201, plan.text
        parent = plan.json()
        children = client.post('/api/v1/agent/issues/' + parent['id'] + '/decompose', json={'children':[{'title':'Implement', 'intendedRole':'developer', 'codeTask':True, 'languages':['SQL'], 'systems':['Tasks']}]})
        assert children.status_code == 200, children.text
        child = children.json()['items'][0]
        assert child['codeTask'] and child['languages'] == ['SQL']
        updated = client.patch('/api/v1/agent/issues/' + child['id'], json={'languages':['Python','SQL'], 'systems':['Tasks','Atlas']})
        assert updated.status_code == 200 and updated.json()['systems'] == ['Tasks','Atlas']


def test_cold_metadata_does_not_claim_a_missing_or_passed_review():
    from ag_tasks.code_context import stage
    assert stage({'codeTask':True,'kind':'task','status':'done','storage':{'tier':'zip'}}) == 'unknown'
