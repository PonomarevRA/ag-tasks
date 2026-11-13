"""Portable HTTP observability stays independent, bounded and restart-safe."""

import importlib
import logging
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import pytest

# The same suite is shipped and executed separately by each independent service.
APP_MODULE = 'ag_tasks.app'
APPLICATION = 'ag-tasks'


def app_at(directory):
    """Create the real product composition with its own temporary data owner."""
    return importlib.import_module(APP_MODULE).create_app(directory, origin='http://127.0.0.1:8899')


def test_journals_collect_real_operations_without_poll_feedback(tmp_path):
    """Collect request/response evidence and diagnostics; repeated reads never log themselves."""
    app = app_at(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8899', client=('127.0.0.1', 41000)) as client:
        route = '/api/v1/components?q=button' if APPLICATION == 'ag-design' else '/api/v1/issues?limit=5'
        response = client.get(route)
        assert response.status_code == 200
        owner = app.state.telemetry
        assert owner.activity.flush() and owner.technical.flush()
        page = client.get('/api/agent-activity/v1/events').json()
        assert page['application'] == APPLICATION and len(page['events']) == 1
        import json, jsonschema
        schema = json.loads((Path(__file__).parents[1] / 'contracts/agent-activity/http-v1/schema.json').read_text())
        jsonschema.validate(page, schema)
        event = page['events'][0]
        assert event['phase'] == 'finished' and event['durationMs'] >= 0
        assert 'HTTP 200' in event['response']['summary'] and event['request']['tool'].startswith('GET /api/v1/')
        technical = client.get('/api/technical-logs/v1/events').json()
        assert any(e.get('operationId') == event['operationId'] for e in technical['events'])
        for _ in range(3):
            assert client.get('/api/diagnostics/v1/metrics').json()['application'] == APPLICATION
            client.get('/api/v1/status')
            client.get('/api/technical-logs/v1/status')
        assert owner.activity.flush()
        assert client.get('/api/agent-activity/v1/events').json()['highWatermark'] == page['highWatermark']
        cursor = page['highWatermark']
        forward = client.get('/api/agent-activity/v1/events', params={'journalEpoch':cursor['journalEpoch'], 'afterSequence':cursor['sequence']}).json()
        assert forward['events'] == [] and forward['direction'] == 'forward'
    with TestClient(app_at(tmp_path), base_url='http://127.0.0.1:8899', client=('127.0.0.1',41001)) as client:
        restored = client.get('/api/agent-activity/v1/events').json()
        assert restored['highWatermark'] == cursor and restored['events'][0]['eventId'] == event['eventId']


@pytest.mark.parametrize('path', ['/api/agent-activity/v1/events','/api/technical-logs/v1/events'])
def test_strict_queries_and_cursor_reset(tmp_path, path):
    """Reject malformed/repeated filters and expired epochs rather than silently skipping history."""
    with TestClient(app_at(tmp_path), base_url='http://127.0.0.1:8899', client=('127.0.0.1',41000)) as client:
        for query in ['limit=0','limit=1&limit=2','q=%ZZ','unknown=x','afterSequence=1']:
            assert client.get(path+'?'+query).status_code == 400
        response = client.get(path+'?journalEpoch=old&afterSequence=0')
        assert response.status_code == 409 and response.json()['resetCursor']
        assert client.get(path,headers={'Origin':'https://evil.invalid'}).status_code == 403


def test_bounded_redacted_capture_and_log_errors(tmp_path):
    """Sensitive bodies are redacted, large JSON is omitted, and real Python errors are exported."""
    from service_telemetry.integration import summary
    assert 'secret-value' not in summary(b'{"token":"secret-value","query":"button"}')['summary']
    assert summary(b'[' + b'"x",'*5000)['truncated']
    with TestClient(app_at(tmp_path), base_url='http://127.0.0.1:8899', client=('127.0.0.1',41000)) as client:
        logging.getLogger(APPLICATION).error('Test error token=secret-value')
        client.app.state.telemetry.technical.flush()
        page = client.get('/api/technical-logs/v1/events').json()
        assert any(e['level'] == 'ERROR' for e in page['events'])
        assert 'secret-value' not in str(page)
        assert client.app.state.telemetry.activity.settings.MaxSizeMb == 50
        assert client.app.state.telemetry.technical.settings.max_size_bytes == 100_000_000


def test_execution_trace_joins_committed_transitions_and_rejected_lease(tmp_path):
    """A producer and its critic are traceable using safe fields from the real HTTP workflow."""
    from workflow_helpers import metadata

    app = app_at(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8899', client=('127.0.0.1', 41000)) as client:
        issue = app.state.store.create({'title': 'private fixture task', **metadata()})
        key = issue['key']
        path = f'/api/v1/agent/issues/{key}'
        producer = {'agentId': 'producer', 'sessionKey': 'private-producer-key'}
        response = client.post(path + '/work/start', json=producer)
        producer['sessionId'] = response.json()['work']['activeSession']['sessionId']
        result = client.post(path + '/results', json={**producer, 'result': {
            'summary': 'private fixture result', 'changes': ['change'], 'validation': ['check'], 'limitations': '', 'artifacts': []}})
        assert result.status_code == 200
        critic = result.json()['criticReviewId']
        review = f'/api/v1/agent/issues/{critic}'
        token = client.get('/api/v1/ui-session').json()['token']
        headers = {'X-Ag-Tasks-UI': token}
        assignment = f'/api/v1/issues/{critic}/review-assignment'
        first = client.post(assignment, headers=headers, json={'reviewerId': 'critic-first'}).json()
        credentials = {'agentId': 'critic-first', 'sessionKey': 'private-critic-key'}
        started = client.post(review + '/work/start', json=credentials)
        credentials['sessionId'] = sid = started.json()['work']['activeSession']['sessionId']
        app.state.store.execution.ticks[sid] -= 91
        assert client.post(review + '/work/heartbeat', json=credentials).status_code == 409
        assert client.post(assignment + '/release', headers=headers, json={'expectedAssignedAt': first['assignedAt']}).status_code == 200
        assert client.post(assignment, headers=headers, json={'reviewerId': 'critic-next'}).status_code == 200
        trace_path = f'/api/v1/issues/{key}/execution-diagnostics'
        trace = client.get(trace_path).json()
        actions = {event['action']: event for event in reversed(trace['items'])}
        expired = actions['work_expired']
        assert expired['taskId'] == critic
        assert expired['facts']['sessionId'] == sid and expired['facts']['reason'] == 'lease_expired'
        assert expired['facts']['generation'] == 1 and expired['facts']['definitionRevision'] == result.json()['definitionRevision']
        assert expired['facts']['assignmentState'] == 'assigned' and expired['facts']['criticReviewId'] == critic
        assert expired['facts']['fromStatus'] == 'in_progress' and expired['facts']['toStatus'] == 'ready'
        assert actions['result_submitted']['facts']['criticReviewId'] == critic
        assert actions['review_assigned']['facts']['reviewerId'] == 'critic-next'
        assert actions['review_assigned']['facts']['generation'] == 1
        page = client.get(trace_path, params={'limit': 2}).json()
        next_page = client.get(trace_path, params={'before': page['nextBefore']}).json()
        assert set(i['id'] for i in next_page['items']).isdisjoint(i['id'] for i in page['items'])
        assert app.state.telemetry.technical.flush() and app.state.telemetry.activity.flush()
        technical = client.get('/api/technical-logs/v1/events').json()
        activity = client.get('/api/agent-activity/v1/events').json()
        by_id = {event['operationId']: event for event in technical['events'] if event.get('operationId')}
        refusal = by_id[expired['facts']['operationId']]
        assert refusal['taskId'] == critic and refusal['sessionId'] == sid
        assert refusal['properties']['errorCode'] == 'work_session_unavailable'
        assert refusal['properties']['identityQuality'] == 'declared'
        assert by_id[actions['result_submitted']['facts']['operationId']]['taskId'] == issue['id']
        assert any(event['operationId'] == refusal['operationId'] for event in activity['events'])
        for private in ('private-producer-key', 'private-critic-key', 'private fixture task', 'private fixture result', token):
            assert private not in str((trace, technical, activity))


def test_execution_diagnostics_read_preserves_cold_storage(tmp_path):
    """Reading traces does not refresh old work or restore its unrelated bodies."""
    from datetime import datetime, timedelta, timezone
    from workflow_helpers import metadata

    with TestClient(app_at(tmp_path), base_url='http://127.0.0.1:8899') as client:
        store = client.app.state.store
        issue = store.create({'title': 'Cold trace', **metadata()})
        route = f"/api/v1/agent/issues/{issue['id']}/work"
        credentials = {'agentId': 'fixture', 'sessionKey': 'one'}
        started = client.post(route + '/start', json=credentials).json()
        credentials['sessionId'] = started['work']['activeSession']['sessionId']
        assert client.post(route + '/stop', json=credentials).status_code == 200
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        store.db.connection.execute('UPDATE issues SET updated_at=? WHERE id=?', (old, issue['id']))
        assert store.cold.sweep() == [issue['id']]
        trace = client.get(f"/api/v1/issues/{issue['id']}/execution-diagnostics").json()
        assert [event['action'] for event in trace['items']] == ['work_stopped', 'work_started']
        assert trace['items'][0]['facts']['sessionId'] == credentials['sessionId']
        row = store.db.connection.execute('SELECT storage_zip,updated_at FROM issues WHERE id=?', (issue['id'],)).fetchone()
        assert row['storage_zip'] and row['updated_at'] == old
