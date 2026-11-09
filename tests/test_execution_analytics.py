"""Public monitoring contract: current evidence and recorded flow have separate scopes."""

from datetime import UTC, datetime
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from ag_tasks.app import create_app
from ag_tasks.execution_analytics import execution_analytics

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
PERIOD = {'start': '2026-11-01', 'end': '2026-11-04'}
PATH = '/api/v1/analytics/execution'


def _task(store, title, *, status='backlog', created='2026-10-01T00:00:00+00:00',
          archived=False, deleted=False, **fields):
    """Create a real identity, then seed historical metadata without running a worker."""
    issue = store.create({'title': title, **fields})
    store.db.connection.execute('''UPDATE issues SET status=?,created_at=?,last_activity_at=?,
        archived_at=?,deleted_at=? WHERE id=?''',
        (status, created, created, created if archived else None, created if deleted else None, issue['id']))
    return issue


def _session(store, issue, expires, *, state='running'):
    """Seed a zero-effort lease to distinguish persisted evidence from effort totals."""
    store.db.connection.execute('''INSERT INTO work_sessions(id,issue_id,agent_id,session_key,
        started_at,last_heartbeat_at,lease_expires_at,elapsed_ms,state) VALUES(?,?,?,?,?,?,?,0,?)''',
        (uuid4().hex, issue['id'], 'fixture', uuid4().hex, NOW.isoformat(), NOW.isoformat(), expires, state))


def _completion(store, issue, instant, *, payload='{}'):
    """Record accepted completion facts whose archived payload is deliberately unavailable."""
    store.db.connection.execute('INSERT INTO history(issue_id,action,actor,payload,created_at) VALUES(?,?,?,?,?)',
        (issue['id'], 'auto_completed', 'fixture', payload, instant))


def _problem(store, issue, severity='medium', state='open'):
    """Use the structured problem API so counts reflect real state and severity fields."""
    store.planning.problem(issue['id'], {'title': 'Reported difficulty', 'severity': severity,
                                       'state': state, 'resolution': 'Checked fix' if state == 'resolved' else ''})


def test_snapshot_counts_zero_effort_cards_and_distinct_attention(tmp_path):
    """Dates cannot hide current cards; risk totals cover all rows before the top-ten list."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost'):
        store = app.state.store
        active = _task(store, 'Active', status='in_progress')
        expired = _task(store, 'Expired', status='ready')
        blocked = _task(store, 'Blocked', status='blocked')
        review = _task(store, 'Review waiting', status='in_review')
        _task(store, 'Rework', status='rework')
        _task(store, 'Stale', status='stale')
        _task(store, 'Done', status='done')
        _task(store, 'Never started')
        _task(store, 'Archived', archived=True)
        _task(store, 'Deleted', deleted=True)
        _session(store, active, '2026-11-04T12:00:00.000001+00:00')
        _session(store, expired, NOW.isoformat())
        _problem(store, blocked, 'critical')
        _problem(store, blocked, state='resolved')
        _problem(store, review)
        report = execution_analytics(store.db, '2001-01-01', '2001-01-01', now=NOW)
        snapshot = report['snapshot']
        assert report['asOf'] == NOW.isoformat()
        assert (snapshot['totalCount'], snapshot['activeSessionCount'], snapshot['reviewCount'],
                snapshot['attentionCount'], snapshot['openProblemCount']) == (8, 1, 1, 5, 2)
        assert dict((row['id'], row['count']) for row in snapshot['statuses']) == {
            'backlog': 1, 'ready': 1, 'in_progress': 1, 'blocked': 1, 'in_review': 1,
            'rework': 1, 'done': 1, 'cancelled': 0, 'stale': 1,
        }
        assert snapshot['attention'][0]['id'] == expired['id']
        assert snapshot['attention'][0]['reasons'] == ['expired_session']
        assert snapshot['attention'][1]['id'] == blocked['id']
        assert snapshot['attention'][1]['reasons'] == ['blocked', 'open_problem']
        assert report['events']['createdCount'] == report['events']['completedUniqueCount'] == 0


def test_current_metadata_filters_match_titles_keys_space_and_kind(tmp_path):
    """Project/type/query filters use current metadata with Unicode casefold and literal search."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost') as client:
        store = app.state.store
        project = store.spaces.write({'name': 'Second project', 'prefix': 'OTHER'})
        matching = _task(store, 'Исследование Ёж', kind='rnd', spaceId=project['id'],
                         created='2026-11-02T00:00:00+00:00')
        _task(store, 'Исследование Ёж', created='2026-11-02T00:00:00+00:00')
        _task(store, 'Исследование ёж', spaceId=project['id'])
        _completion(store, matching, '2026-11-03T10:00:00+00:00')
        response = client.get(PATH, params={**PERIOD, 'spaceId': project['id'], 'kind': 'rnd', 'q': ' ЁЖ '})
        assert response.status_code == 200
        report = response.json()
        assert report['snapshot']['totalCount'] == 1
        assert (report['events']['createdCount'], report['events']['completedUniqueCount']) == (1, 1)
        assert client.get(PATH, params={**PERIOD, 'q': matching['key'].lower()}).json()['snapshot']['totalCount'] == 1
        for query in ('absent', '%', "' OR 1=1 --"):
            empty = client.get(PATH, params={**PERIOD, 'q': query}).json()
            assert empty['snapshot']['totalCount'] == empty['events']['createdCount'] == 0
            assert all(item['count'] == 0 for item in empty['snapshot']['statuses'])


def test_flow_uses_exact_utc_boundaries_archived_metadata_and_distinct_task_days(tmp_path):
    """Recompletion counts once per day; whole-period unique count remains one task."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost') as client:
        store = app.state.store
        first = _task(store, 'Repeated completion', created='2026-11-01T03:00:00+03:00')
        archived = _task(store, 'Archived flow', archived=True, created='2026-11-04T23:59:59.999999+00:00')
        deleted = _task(store, 'Deleted flow', deleted=True, created='2026-11-02T00:00:00+00:00')
        _task(store, 'Before window', created='2026-10-31T23:59:59.999999+00:00')
        _task(store, 'After window', created='2026-11-05T00:00:00+00:00')
        for instant in ('2026-10-31T23:59:59.999999+00:00', '2026-11-01T03:00:00+03:00',
                        '2026-11-01T12:00:00+00:00', '2026-11-02T09:00:00+00:00',
                        '2026-11-05T00:00:00+00:00'):
            _completion(store, first, instant)
        _completion(store, archived, '2026-11-04T23:59:59.999999+00:00')
        _completion(store, deleted, '2026-11-02T00:00:00+00:00')
        events = client.get(PATH, params=PERIOD).json()['events']
        assert events == {
            'byDay': [
                {'label': '2026-11-01', 'createdCount': 1, 'completedCount': 1},
                {'label': '2026-11-02', 'createdCount': 0, 'completedCount': 1},
                {'label': '2026-11-03', 'createdCount': 0, 'completedCount': 0},
                {'label': '2026-11-04', 'createdCount': 1, 'completedCount': 1},
            ],
            'byWeek': [
                {'label': '2026-10-26', 'createdCount': 1, 'completedCount': 1},
                {'label': '2026-11-02', 'createdCount': 1, 'completedCount': 2},
            ],
            'createdCount': 2, 'completedUniqueCount': 2,
        }


def test_attention_is_full_scope_but_returns_ten_in_stable_risk_order(tmp_path):
    """Critical problems outrank older review waits; the count is not a page count."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost'):
        store = app.state.store
        waiting = [_task(store, f'Waiting {index}', status='in_review') for index in range(13)]
        severe = _task(store, 'Critical problem', created='2026-11-03T00:00:00+00:00')
        _problem(store, severe, 'high')
        first = execution_analytics(store.db, '2026-11-01', '2026-11-04', now=NOW)
        second = execution_analytics(store.db, '2026-11-01', '2026-11-04', now=NOW)
        snapshot = first['snapshot']
        assert snapshot == second['snapshot']
        assert (snapshot['totalCount'], snapshot['reviewCount'], snapshot['attentionCount'],
                snapshot['attentionLimit'], len(snapshot['attention'])) == (14, 13, 14, 10, 10)
        assert snapshot['attention'][0]['id'] == severe['id']
        assert [row['key'] for row in snapshot['attention'][1:]] == sorted(issue['key'] for issue in waiting)[:9]


def test_attention_orders_activity_by_instant_across_timezone_offsets(tmp_path):
    """An offset timestamp must not make the older task appear more recently active."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost'):
        store = app.state.store
        newer = _task(store, 'Newer activity', status='rework', created='2026-11-01T01:00:00+00:00')
        older = _task(store, 'Older activity', status='rework', created='2026-11-01T03:00:00+03:00')
        snapshot = execution_analytics(store.db, '2026-11-01', '2026-11-04', now=NOW)['snapshot']
        assert [row['id'] for row in snapshot['attention']] == [older['id'], newer['id']]


def test_route_never_writes_reconciles_restores_or_reads_cold_payloads(tmp_path, monkeypatch):
    """A SQL authorizer enforces read-only access even with expired leases and ZIP bodies."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost') as client:
        store = app.state.store
        issue = _task(store, 'Cold evidence', status='ready', created='2026-11-01T00:00:00+00:00')
        _session(store, issue, '2000-01-01T00:00:00+00:00')
        _completion(store, issue, '2026-11-02T00:00:00+00:00')
        store.db.connection.execute("UPDATE issues SET storage_zip='missing-fixture.zip' WHERE id=?", (issue['id'],))

        def forbidden(*args, **kwargs):
            """Fail if the analytics reader calls a mutating issue projection."""
            raise AssertionError('Monitoring must not restore or reconcile task state')

        for owner, name in ((store, 'list'), (store, 'get'), (store.execution, 'expire'),
                            (store.execution, 'expire_all'), (store.cold, 'restore')):
            monkeypatch.setattr(owner, name, forbidden)

        def authorize(action, table, column, database, source):
            """Deny database writes and archived payload columns at SQLite's boundary."""
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table in ('history', 'results') and column == 'payload':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection = store.db.connection
        before = connection.total_changes
        connection.set_authorizer(authorize)
        try:
            response = client.get(PATH, params=PERIOD)
            assert response.status_code == 200
            assert connection.total_changes == before
            assert response.json()['events']['completedUniqueCount'] == 1
            assert response.json()['snapshot']['attention'][0]['reasons'] == ['expired_session']
            assert connection.execute('SELECT state FROM work_sessions WHERE issue_id=?', (issue['id'],)).fetchone()[0] == 'running'
        finally:
            connection.set_authorizer(None)


@pytest.mark.parametrize('period', [
    {'start': '2026-11-04', 'end': '2026-11-03'},
    {'start': '2026-02-30', 'end': '2026-03-01'},
    {'start': '2025-01-01', 'end': '2026-01-02'},
    {'start': '2026-1-01', 'end': '2026-01-02'},
])
def test_route_reuses_validated_inclusive_calendar_window(tmp_path, period):
    """Invalid calendar dates, reversed ranges and overlong windows return the existing error."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://localhost') as client:
        response = client.get(PATH, params=period)
        assert response.status_code == 422
        assert response.json()['error']['code'] == 'invalid_period'
