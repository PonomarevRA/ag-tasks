"""Manager acceptance: current operational queues remain scoped, bounded and read-only."""

from datetime import UTC, datetime, timedelta
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from ag_tasks.app import create_app
from ag_tasks.manager_analytics import management_analytics

NOW = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
PATH = '/api/v1/analytics/management'


@pytest.fixture
def client(tmp_path):
    """Own a disposable service and its database; never use the installed user store."""
    with TestClient(create_app(tmp_path), base_url='http://localhost') as value:
        yield value


def _task(store, title, *, status='backlog', kind='task', archived=False, deleted=False, **fields):
    """Create through the domain, then seed explicit historical status/kind fixture metadata."""
    issue = store.create({'title': title, 'kind': 'task' if kind == 'review' else kind, **fields})
    connection = store.db.connection
    connection.execute('''UPDATE issues SET status=?,kind=?,last_activity_at=?,archived_at=?,deleted_at=?
        WHERE id=?''', (status, kind, NOW.isoformat(), NOW.isoformat() if archived else None,
                        NOW.isoformat() if deleted else None, issue['id']))
    return issue


def _session(store, issue, expires, *, elapsed=0):
    """Seed persisted lease evidence with known effort, independent of worker execution."""
    store.db.connection.execute('''INSERT INTO work_sessions(id,issue_id,agent_id,session_key,
        started_at,last_heartbeat_at,lease_expires_at,elapsed_ms,state) VALUES(?,?,?,?,?,?,?,?,?)''',
        (uuid4().hex, issue['id'], 'fixture-agent', uuid4().hex, NOW.isoformat(), NOW.isoformat(),
         expires, elapsed, 'running'))


def _report(store, **filters):
    """Use the production aggregate at one fixed UTC observation instant."""
    return management_analytics(store.db, now=NOW, **filters)


def test_default_task_open_queue_and_full_scope_counts_precede_pagination(client):
    """Default task/open excludes containers and terminal rows while KPI/charts retain full scope."""
    store = client.app.state.store
    _task(store, 'Unassigned')
    blocked = _task(store, 'Blocked', status='blocked', assignee='Alice', priority='urgent')
    review = _task(store, 'Review', status='in_review', assignee='Alice')
    active = _task(store, 'Declared work', status='in_progress', assignee='Bob', priority='high')
    _task(store, 'Done', status='done')
    _task(store, 'Cancelled', status='cancelled')
    _task(store, 'Research', kind='rnd')
    _task(store, 'Generated critic', kind='review')
    _task(store, 'Archived', archived=True)
    _task(store, 'Deleted', deleted=True)
    _session(store, active, (NOW + timedelta(seconds=1)).isoformat())
    report = _report(store, limit=1)
    assert report['summary'] == {
        'totalCount': 6, 'openCount': 4, 'overdueCount': 0, 'dueSoonCount': 0,
        'unassignedCount': 1, 'activeCount': 1, 'attentionCount': 2, 'reviewCount': 1,
    }
    assert (report['totalCount'], report['hasMore'], report['nextOffset']) == (4, True, 1)
    assert report['items'][0]['id'] == blocked['id']
    assert sum(group['openCount'] for group in report['byProject']) == 4
    assert sum(group['openCount'] for group in report['byAssignee']) == 4
    assert dict((row['id'], row['count']) for row in report['queues']) == {
        'open': 4, 'attention': 2, 'unassigned': 1, 'overdue': 0, 'review': 1, 'all': 6,
    }
    attention = _report(store, queue='attention')
    assert attention['summary'] == report['summary']
    assert [row['id'] for row in attention['items']] == [blocked['id'], review['id']]
    assert attention['byProject'] == report['byProject']
    assert attention['byAssignee'] == report['byAssignee']
    all_kinds = _report(store, kind='', queue='all')
    assert all_kinds['totalCount'] == 8
    second = _report(store, offset=1, limit=1)
    assert second['items'][0]['id'] != blocked['id']


def test_deadlines_are_inclusive_utc_dates_and_terminal_cards_are_not_late(client):
    """Yesterday is overdue; today and day7 are soon; missing dates never imply lateness."""
    store = client.app.state.store
    yesterday = _task(store, 'Yesterday', dueDate='2026-11-04')
    today = _task(store, 'Today', dueDate='2026-11-05')
    seventh = _task(store, 'Seventh day', dueDate='2026-11-12')
    eighth = _task(store, 'Eighth day', dueDate='2026-11-13')
    missing = _task(store, 'No deadline')
    done = _task(store, 'Completed past deadline', status='done', dueDate='2026-11-04')
    cancelled = _task(store, 'Cancelled past deadline', status='cancelled', dueDate='2026-11-04')
    report = _report(store, queue='all')
    cards = {row['id']: row for row in report['items']}
    assert cards[yesterday['id']]['overdue'] is True
    assert cards[yesterday['id']]['reasons'] == ['overdue']
    for issue in (today, seventh):
        assert cards[issue['id']]['dueSoon'] is True
        assert cards[issue['id']]['overdue'] is False
    for issue in (eighth, missing, done, cancelled):
        assert cards[issue['id']]['overdue'] is False
        assert cards[issue['id']]['dueSoon'] is False
    assert report['summary']['overdueCount'] == 1
    assert report['summary']['dueSoonCount'] == 2
    assert [row['id'] for row in _report(store, queue='overdue')['items']] == [yesterday['id']]


def test_lease_boundary_distinguishes_declared_active_and_expired_without_mutation(client):
    """An exact-expiry lease is attention evidence; only a later instant counts as active."""
    store = client.app.state.store
    active = _task(store, 'Active', assignee='Owner')
    expired = _task(store, 'Exact boundary', assignee='Owner')
    _session(store, active, (NOW + timedelta(microseconds=1)).isoformat(), elapsed=123)
    _session(store, expired, NOW.isoformat())
    report = _report(store)
    cards = {row['id']: row for row in report['items']}
    assert report['summary']['activeCount'] == 1
    assert cards[active['id']]['activeAgent'] == 'fixture-agent'
    assert cards[active['id']]['elapsedMs'] == 123
    assert cards[expired['id']]['active'] is False
    assert cards[expired['id']]['activeAgent'] is None
    assert cards[expired['id']]['reasons'] == ['expired_session']
    assert store.db.connection.execute("SELECT COUNT(*) FROM work_sessions WHERE state='running'").fetchone()[0] == 2


def test_filters_use_exact_owner_and_literal_unicode_query_with_independent_facets(client):
    """No-match remains empty; project/kind/owner/status/priority constrain only matching cards."""
    store = client.app.state.store
    project = store.spaces.write({'name': 'Other project', 'prefix': 'OTHER'})
    matching = _task(store, 'Исследование Ёж', kind='rnd', status='ready', priority='high',
                     assignee='  Alice  ', spaceId=project['id'])
    _task(store, 'Исследование ёж', assignee='alice', priority='high')
    _task(store, 'Other item', assignee='Bob')
    report = _report(store, kind='rnd', space_id=project['id'], assignee='Alice',
                     priority='high', status='ready', query=' ЁЖ ')
    assert [row['id'] for row in report['items']] == [matching['id']]
    assert report['items'][0]['assignee'] == 'Alice'
    assert {row['id'] for row in report['facets']['assignees']} == {'Alice', 'alice', 'Bob'}
    assert len(report['facets']['projects']) == 2
    assert _report(store, kind='', query=matching['key'].lower())['totalCount'] == 1
    for filters in ({'assignee': 'not-an-owner'}, {'query': '%'}, {'query': "' OR 1=1 --"},
                    {'space_id': 'not-a-project'}, {'query': 'not-a-title'}):
        empty = _report(store, **filters)
        assert empty['totalCount'] == empty['summary']['totalCount'] == 0
        assert empty['items'] == []
        assert empty['hasMore'] is False
        assert empty['nextOffset'] is None


def test_unassigned_queue_is_distinct_from_all_owners(client):
    """Whitespace-only ownership is unassigned; terminal tasks do not enter that queue."""
    store = client.app.state.store
    null_owner = _task(store, 'Null owner')
    blank_owner = _task(store, 'Whitespace owner', assignee='   ')
    _task(store, 'Assigned', assignee='Alice')
    _task(store, 'Done unassigned', status='done')
    report = _report(store, queue='unassigned')
    assert {row['id'] for row in report['items']} == {null_owner['id'], blank_owner['id']}
    assert report['summary']['totalCount'] == 4
    assert report['summary']['unassignedCount'] == 2
    assert _report(store, assignee='Alice')['totalCount'] == 1


def test_oldest_activity_sort_uses_instants_not_timezone_text(client):
    """Offset notation must not reverse the chronological order of two risk cards."""
    store = client.app.state.store
    older = _task(store, 'Older instant', status='blocked')
    newer = _task(store, 'Newer instant', status='blocked')
    store.db.connection.execute('UPDATE issues SET last_activity_at=? WHERE id=?',
                                ('2026-11-05T03:00:00+03:00', older['id']))
    store.db.connection.execute('UPDATE issues SET last_activity_at=? WHERE id=?',
                                ('2026-11-05T01:00:00+00:00', newer['id']))
    assert [row['id'] for row in _report(store)['items']] == [older['id'], newer['id']]


def test_manager_route_is_read_only_even_for_cold_cards_and_expired_leases(client, monkeypatch):
    """SQLite authorizer catches writes/body reads; reader cannot restore or expire task data."""
    store = client.app.state.store
    issue = _task(store, 'Cold metadata', assignee='Owner')
    _session(store, issue, '2000-01-01T00:00:00+00:00')
    connection = store.db.connection
    connection.execute("UPDATE issues SET storage_zip='missing-fixture.zip' WHERE id=?", (issue['id'],))

    def forbidden(*args, **kwargs):
        """Disallow mutating domain projection helpers in operational analytics."""
        raise AssertionError('Manager reads must not restore or expire task state')

    for owner, name in ((store, 'list'), (store, 'get'), (store.execution, 'expire'),
                        (store.execution, 'expire_all'), (store.cold, 'restore')):
        monkeypatch.setattr(owner, name, forbidden)

    def authorize(action, table, column, database, source):
        """Keep the query at metadata-only SELECTs at SQLite's own boundary."""
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and (table, column) in {
            ('issues', 'description'), ('results', 'payload'), ('history', 'payload'),
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    before = connection.total_changes
    connection.set_authorizer(authorize)
    try:
        response = client.get(PATH)
        assert response.status_code == 200, response.text
        assert response.json()['items'][0]['id'] == issue['id']
        assert response.json()['items'][0]['reasons'] == ['expired_session']
        assert connection.total_changes == before
        assert connection.execute('SELECT state FROM work_sessions WHERE issue_id=?', (issue['id'],)).fetchone()[0] == 'running'
    finally:
        connection.set_authorizer(None)


@pytest.mark.parametrize('params', [
    {'queue': 'not-a-queue'}, {'kind': 'not-a-kind'}, {'status': 'not-a-status'},
    {'priority': 'not-a-priority'}, {'offset': -1}, {'limit': 0}, {'limit': 101},
])
def test_manager_route_rejects_unknown_filters_and_page_bounds(client, params):
    """Invalid named choices and page sizes do not silently broaden the selection."""
    response = client.get(PATH, params=params)
    assert 400 <= response.status_code < 500


def test_pagination_visits_each_open_card_once_with_whole_scope_totals(client):
    """A manager can traverse the full queue without duplicates or shrinking aggregate counts."""
    store = client.app.state.store
    expected = set()
    for index in range(37):
        issue = _task(store, f'Paged {index}', assignee='Owner' if index % 2 else None)
        expected.add(issue['id'])
    for index in range(3):
        _task(store, f'Finished {index}', status='done')
    _task(store, 'Archived page member', archived=True)
    offset = 0
    visited = []
    while True:
        page = _report(store, offset=offset, limit=11)
        assert page['totalCount'] == 37
        assert page['summary']['totalCount'] == 40
        assert page['summary']['openCount'] == 37
        visited.extend(row['id'] for row in page['items'])
        if not page['hasMore']:
            assert page['nextOffset'] is None
            break
        assert page['nextOffset'] > offset
        offset = page['nextOffset']
    assert len(visited) == len(set(visited)) == len(expected)
    assert set(visited) == expected
