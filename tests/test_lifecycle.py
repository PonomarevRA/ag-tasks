"""Lifecycle evidence must not manufacture completion, time or independent approval."""

from fastapi.testclient import TestClient
from ag_tasks.app import create_app
from ag_tasks.issues import IssueStore
from ag_tasks.lifecycle import audit
from workflow_helpers import metadata, review_ready


def test_parent_progress_and_paused_work_are_explicit(tmp_path):
    """Parent progress includes off-page children while pauses require reporting or resumption."""
    store = IssueStore(tmp_path / 'state')
    try:
        parent = store.create({'title':'Parent'})
        child = store.create({'title':'Child','parentId':parent['id'],**metadata()})
        active = store.execution.start(child['id'], {'agentId':'producer','sessionKey':'real'})
        assert store.get(parent['id'])['lifecycle']['phase'] == 'children_working'
        listed = next(item for item in store.list() if item['id'] == parent['id'])
        assert listed['childProgress']['byStatus'] == {'in_progress':1}
        assert listed['lifecycle']['phase'] == 'children_working'
        graph_parent = next(item for item in store.spaces.graph()['nodes'] if item['id'] == parent['id'])
        assert graph_parent['lifecycle']['phase'] == 'children_working'
        assert graph_parent['childProgress'] == listed['childProgress']
        store.execution.stop(child['id'], {'agentId':'producer','sessionKey':'real','sessionId':active['work']['activeSession']['sessionId']})
        assert store.get(child['id'])['lifecycle']['phase'] == 'paused'
        assert store.get(parent['id'])['work']['elapsedMs'] == 0
    finally:
        store.close()


def test_review_and_human_gate_are_distinct_and_fresh_work_invalidates_result(tmp_path):
    """A critic pass still needs the human; a later producer lease cannot reuse old acceptance."""
    store = IssueStore(tmp_path / 'state')
    try:
        issue = store.create({'title':'Reviewed',**metadata()})
        issue = review_ready(store, issue['id'])
        assert issue['status'] == 'done'
        assert issue['currentResultIsCurrent']
        assert issue['lifecycle']['phase'] == 'done'
        store.update(issue['id'], {'status':'ready'}, actor='user')
        store.execution.start(issue['id'], {'agentId':'producer','sessionKey':'new-generation'})
        assert not store.get(issue['id'])['currentResultIsCurrent']
    finally:
        store.close()


def test_audit_flags_comment_only_without_parsing_or_mutating(tmp_path):
    """Any prose is merely a triage candidate; no historical result or duration is inferred."""
    store = IssueStore(tmp_path / 'state')
    try:
        issue = store.create({'title':'Legacy'})
        store.comment(issue['id'], 'Completed, all tests passed (unverified text)')
        before = store.get(issue['id'])
        page = audit(store, limit=1)
        assert page['items'][0]['commentOnlyCandidate']
        assert page['items'][0]['lifecycle']['phase'] == 'planned'
        assert not page['automaticCompletion'] and page['leaseExpiryMayReconcile']
        assert store.get(issue['id']) == before
    finally:
        store.close()


def test_http_audit_precedes_generic_issue_route_and_pages(tmp_path):
    """Audit is a bounded read-only route with explicit continuation and next-gate data."""
    with TestClient(create_app(tmp_path / 'http'), base_url='http://127.0.0.1:18995') as client:
        for index in range(3):
            assert client.post('/api/v1/agent/issues',json={'title':f'Task {index}'}).status_code == 201
        page = client.get('/api/v1/issues/status-audit?limit=2').json()
        assert page['contract'] == 'ag-tasks-lifecycle/v1'
        assert len(page['items']) == 2 and page['hasMore'] and page['nextOffset'] == 2
        assert len(client.get('/api/v1/issues/status-audit?limit=2&offset=2').json()['items']) == 1
        assert client.get('/api/v1/issues/status-audit?limit=201').status_code == 422


def test_rework_and_cold_storage_never_look_like_unstarted_or_missing_reports():
    """Explicit rework and unloaded cold evidence remain distinct from planned or absent results."""
    from ag_tasks.lifecycle import project
    base = {'status':'rework','work':{'state':'never_started','elapsedMs':0}}
    assert project(base)['phase'] == 'rework'
    assert project({**base,'status':'in_review','storage':{'tier':'zip'}})['phase'] == 'stored_cold'
    assert project({**base,'status':'in_review'})['phase'] == 'result_missing'
