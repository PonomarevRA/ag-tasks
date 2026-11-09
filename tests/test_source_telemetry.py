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
