"""Semantic and bounded-work regressions for compact reads; fixtures never use the benchmark or user database."""

import json
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.issues import DomainError, IssueStore
from workflow_helpers import metadata, review_ready


@pytest.fixture
def store(tmp_path):
    """Own one isolated SQLite authority; no standalone process or benchmark runs."""
    value = IssueStore(tmp_path / 'state')
    try:
        yield value
    finally:
        value.close()


def result_history(store):
    """Submit twelve legitimate large generations so detail-byte retention and compact work differ materially."""
    issue = store.create({'title': 'Large result fixture', **metadata()})
    for generation in range(12):
        active = store.execution.start(issue['id'], {'agentId': 'producer', 'sessionKey': str(generation)})
        issue = store.results.submit(issue['id'], {
            'agentId': 'producer', 'sessionKey': str(generation), 'sessionId': active['work']['activeSession']['sessionId'],
            'result': {'summary': f'Generation {generation}: ' + 's' * 1500,
                       'changes': [f'{generation}-{number}: ' + 'x' * 990 for number in range(20)],
                       'validation': ['Declared fixture evidence'], 'limitations': 'Fixture only', 'artifacts': []}})
    return issue


def capture_result_decodes(monkeypatch, store, issue_id):
    """Observe actual stored-result JSON decodes without counting unrelated criteria or request parsing."""
    payloads = store.db.connection.execute('SELECT payload FROM results WHERE issue_id=? ORDER BY generation', (issue_id,)).fetchall()
    tracked = {row['payload'] for row in payloads}
    observed = []
    original = json.loads

    def decode(value, *args, **kwargs):
        """Record matching immutable payloads and preserve JSON parsing behavior exactly."""
        if isinstance(value, str) and value in tracked:
            observed.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, 'loads', decode)
    return observed, payloads[-1]['payload']


def test_compact_decodes_current_result_once_and_preserves_shape(store, monkeypatch):
    """A list must not decode historical bodies merely to discard them, even with twelve large generations."""
    issue = result_history(store)
    decoded, latest_payload = capture_result_decodes(monkeypatch, store, issue['id'])
    statements = []
    store.db.connection.set_trace_callback(statements.append)
    try:
        items = store.list(status='in_review')
    finally:
        store.db.connection.set_trace_callback(None)
    assert len(items) == 1 and items[0]['id'] == issue['id']
    item = items[0]
    assert item['resultsCount'] == 12 and item['criticReview'] is None
    assert 'results' not in item and 'resultsTruncated' not in item
    assert item['currentResult'] == {
        'generation': 12, 'definitionRevision': issue['definitionRevision'], 'producerAgentId': 'producer',
        'reviewedHead': None, 'createdAt': issue['currentResult']['createdAt'],
        'summary': issue['currentResult']['summary'][:512]}
    assert Counter(decoded) == Counter({latest_payload: 1})
    selects = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
    assert len(selects) <= 10  # One page/lease preflight plus at most eight reads for its sole card.


def test_detail_retains_order_count_and_byte_bounded_history_without_redecoding_current(store, monkeypatch):
    """Detail remains newest-first, keeps total counts and full current content, and respects the original 128KiB history bound."""
    issue = result_history(store)
    decoded, latest_payload = capture_result_decodes(monkeypatch, store, issue['id'])
    detail = store.get(issue['id'])
    assert detail['currentResult'] == issue['currentResult']
    assert detail['resultsCount'] == 12 and detail['resultsTruncated'] is True
    assert 1 < len(detail['results']) < 10
    assert [item['generation'] for item in detail['results']] == list(range(12, 12 - len(detail['results']), -1))
    assert sum(len(json.dumps(item, ensure_ascii=True).encode('ascii')) for item in detail['results']) <= 128 * 1024
    assert decoded.count(latest_payload) == 1


def test_compact_critic_freshness_still_invalidates_new_producer_work(store):
    """Passing an already locked issue row cannot reuse a former PASS after a fresh work session or bypass completion."""
    issue = store.create({'title': 'Current critic proof', **metadata()})
    passed = review_ready(store, issue['id'])
    compact = next(item for item in store.list(status='done') if item['id']==issue['id'])
    assert compact['criticReview']['verdict'] == 'pass'
    assert compact['criticReview']['generation'] == passed['resultGeneration']
    store.update(issue['id'],{'status':'ready'},actor='user')
    active = store.execution.start(issue['id'], {'agentId': 'next-producer', 'sessionKey': 'new-work'})
    store.execution.stop(issue['id'], {'agentId': 'next-producer', 'sessionKey': 'new-work',
                                     'sessionId': active['work']['activeSession']['sessionId']})
    assert store.list(status='ready')[0]['criticReview'] is None
    with pytest.raises(DomainError) as error:
        store.update(issue['id'], {'status': 'done'}, actor='user')
    assert error.value.status == 409


def test_existence_probe_keeps_all_filters_and_never_projects_a_card(store, monkeypatch):
    """Continuation counts the same active/archived space and hierarchy rows without touching result bodies."""
    parent = store.create({'title': 'Parent'})
    child = store.create({'title': 'Child', 'status': 'ready', 'parentId': parent['id']})
    space = store.spaces.write({'name': 'Other'})
    store.create({'title': 'Other space', 'status': 'ready', 'spaceId': space['id']})
    archived = store.create({'title': 'Archived'})
    store.update(archived['id'], {'status': 'cancelled'}, actor='user')
    store.spaces.archive(archived['id'])

    def forbidden_projection(*args, **kwargs):
        """Fail immediately if an existence-only query expands any issue or evidence."""
        raise AssertionError('An existence probe must not project cards')

    monkeypatch.setattr(store, '_issue', forbidden_projection)
    cases = [({}, 3), ({'space_id': 'default'}, 2), ({'parent_id': parent['id']}, 1),
             ({'parent_id': child['id']}, 0), ({'status': 'ready'}, 2), ({'repo_id': 'unknown'}, 0),
             ({'include_archived': True}, 4), ({'space_id': 'default', 'include_archived': True}, 3)]
    for filters, total in cases:
        for offset in range(total + 2):
            assert store.has_more(offset=offset, **filters) is (offset < total)
    with pytest.raises(DomainError):
        store.has_more(status='not-a-status')


def test_http_has_more_projects_only_requested_page(tmp_path, monkeypatch):
    """A public one-card page must not decode another card for hasMore or change its continuation shape."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:18995') as client:
        for title in ('First', 'Second', 'Third'):
            assert client.post('/api/v1/agent/issues', json={'title': title}).status_code == 201
        store = client.app.state.store
        original, projected = store._issue, []

        def project(row, **options):
            """Count real public-read projections while preserving the original projector and locked rows."""
            projected.append(row['id'])
            return original(row, **options)

        monkeypatch.setattr(store, '_issue', project)
        statements = []
        store.db.connection.set_trace_callback(statements.append)
        try:
            response = client.get('/api/v1/issues?limit=1')
        finally:
            store.db.connection.set_trace_callback(None)
        assert response.status_code == 200
        page = response.json()
        assert len(page['items']) == 1 and page['items'][0]['title'] == 'Third'
        assert page['hasMore'] is True and page['nextOffset'] == 1
        assert len(projected) == 1
        assert len([sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]) <= 12
        tail = client.get('/api/v1/issues?limit=1&offset=2').json()
        assert tail['items'][0]['title'] == 'First' and tail['hasMore'] is False and tail['nextOffset'] is None


@pytest.mark.parametrize('table,column,index', [('comments', 'issue_id', 'comments_issue'), ('review_reports', 'parent_id', 'reports_parent')])
def test_evidence_indexes_replace_full_scans_without_schema_downgrade(store, table, column, index):
    """Use actual SQLite EXPLAIN on the temporary service schema; no timing or production database claim is made."""
    connection = store.db.connection
    query = f'SELECT * FROM {table} WHERE {column}=? ORDER BY id DESC LIMIT 20'
    connection.execute(f'DROP INDEX {index}')
    before = [row['detail'] for row in connection.execute('EXPLAIN QUERY PLAN ' + query, ('fixture',))]
    connection.execute(f'CREATE INDEX {index} ON {table}({column},id)')
    after = [row['detail'] for row in connection.execute('EXPLAIN QUERY PLAN ' + query, ('fixture',))]
    assert any('SCAN ' + table in detail for detail in before)
    assert any('SEARCH ' + table + ' USING INDEX ' + index in detail for detail in after)
    assert connection.execute('PRAGMA user_version').fetchone()[0] == 6
