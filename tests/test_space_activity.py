"""Whole-space activity uses isolated stores and real leased work, never live user data."""

from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.execution import Execution
from ag_tasks.issues import IssueStore
from test_execution import Clock, prepared, start


def test_summary_separates_status_recent_work_and_expired_leases(tmp_path):
    """Edits alone are not work; expiry removes live work without erasing the last heartbeat."""
    store = IssueStore(tmp_path)
    clock = Clock()
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        store.create({'title': 'Backlog'})
        live = prepared(store)
        empty = store.spaces.write({'name':'Empty'})
        summary = next(x for x in store.spaces.list() if x['id'] == 'default')['summary']
        assert summary == {'backlog':2, 'inProgress':0, 'activeTasks':0, 'lastAgentWorkAt':None}
        keys = start(store, live)
        clock.advance(30)
        store.execution.heartbeat(live['id'], keys)
        confirmed = clock.wall.isoformat()
        summary = next(x for x in store.spaces.list() if x['id'] == 'default')['summary']
        assert summary == {'backlog':1, 'inProgress':1, 'activeTasks':1, 'lastAgentWorkAt':confirmed}
        clock.advance(91)
        summary = next(x for x in store.spaces.list() if x['id'] == 'default')['summary']
        assert summary['activeTasks'] == 0 and summary['inProgress'] == 0
        assert summary['lastAgentWorkAt'] == confirmed
        assert next(x for x in store.spaces.list() if x['id'] == empty['id'])['summary']['lastAgentWorkAt'] is None
    finally:
        store.close()


def test_graph_selects_active_work_before_limit_and_keeps_space_boundaries(tmp_path):
    """Older active work survives the newest-first cap while stopped/foreign work does not."""
    store = IssueStore(tmp_path)
    try:
        live = prepared(store, 'Older live work')
        keys = start(store, live)
        for number in range(205):
            store.create({'title':f'Newer idle {number}'})
        other = store.spaces.write({'name':'Other'})
        foreign = store.create({'title':'Other live', 'spaceId':other['id'],
            'intendedRole':'developer', 'dor':live['dor'], 'dod':live['dod']})
        start(store, foreign, 'foreign')
        context = store.create({'title':'Context for active work'})
        unrelated = store.create({'title':'Unrelated idle work'})
        store.link(live['id'], context['id'])
        store.link(context['id'], unrelated['id'])
        graph = store.spaces.graph(limit=1, active_only=True)
        assert [x['id'] for x in graph['nodes']] == [live['id']]
        assert graph['nodeCount'] == 1
        assert graph['edgeCount'] == 1
        assert [x['id'] for x in graph['stubs']] == [context['id']]
        store.execution.stop(live['id'], keys)
        assert store.spaces.graph(active_only=True)['nodes'] == []
    finally:
        store.close()


def test_summary_ignores_archived_deleted_and_does_not_double_count_sessions(tmp_path):
    """History contributes recency once per task, while archive/tombstones stay out of counts."""
    store = IssueStore(tmp_path)
    try:
        issue = prepared(store)
        keys = start(store, issue)
        store.execution.stop(issue['id'], keys)
        start(store, issue, 'second')
        assert next(x for x in store.spaces.list() if x['id'] == 'default')['summary']['activeTasks'] == 1
        # Inject legacy hidden rows to check defensive read semantics without weakening write guards.
        store.db.connection.execute('UPDATE issues SET archived_at=? WHERE id=?', ('2026-10-30', issue['id']))
        summary = next(x for x in store.spaces.list() if x['id'] == 'default')['summary']
        assert summary['inProgress'] == 0 and summary['activeTasks'] == 0
        assert summary['lastAgentWorkAt'] is not None
        assert store.spaces.graph(active_only=True)['nodes'] == []
        store.db.connection.execute('UPDATE issues SET deleted_at=? WHERE id=?', ('2026-10-30', issue['id']))
        assert next(x for x in store.spaces.list() if x['id'] == 'default')['summary']['lastAgentWorkAt'] is None
    finally:
        store.close()


def test_http_graph_filter_and_space_summary(tmp_path):
    """The public browser contract exposes real live work and rejects malformed filters."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8895') as client:
        store = app.state.store
        issue = prepared(store)
        start(store, issue)
        store.create({'title':'Idle'})
        response = client.get('/api/v1/issues/graph?activeOnly=true&limit=1')
        assert response.status_code == 200
        assert [x['id'] for x in response.json()['nodes']] == [issue['id']]
        assert client.get('/api/v1/spaces').json()['items'][0]['summary']['activeTasks'] == 1
        assert client.get('/api/v1/issues/graph?activeOnly=invalid').status_code == 422
