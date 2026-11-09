"""Presence must be transport evidence, with no authority over task execution or stored transcripts."""

from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.session_presence import SessionPresence


def register(client):
    """Register the bridge-owned observation envelope, not a synthetic agent work session."""
    response = client.post('/api/v1/agent/commands', json={
        'clientId': 'bridge-test', 'commandId': 'register', 'operation': 'session.register',
        'payload': {'title': 'MCP client', 'agent': 'test', 'protocol': 'mcp', 'protocolVersion': 1,
                    'remoteSessionId': 'connection', 'connectionId': 'connection',
                    'capabilities': {'sourceKind': 'mcp_connection'}}})
    assert response.status_code == 200
    return response.json()['id']


def test_presence_expiry_and_disconnection_are_not_running_model_claims():
    """A missing pulse expires by monotonic time; late pulses cannot reopen an explicit disconnect."""
    clock = [10.0]
    registry = SessionPresence(clock=lambda: clock[0], capacity=2)
    assert registry.snapshot('one')['state'] == 'unknown'
    assert registry.touch('one', 'connected')['state'] == 'connected'
    clock[0] += 90
    assert registry.snapshot('one')['state'] == 'stale'
    registry.touch('one', 'disconnected')
    assert registry.touch('one', 'connected')['state'] == 'disconnected'
    assert SessionPresence().snapshot('one')['state'] == 'unknown'


def test_http_presence_binding_guard_and_no_durable_heartbeat_growth(tmp_path):
    """Only the matching MCP binding can pulse, and frequent pulses do not append archive entries."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        sid = register(client)
        route = '/api/v1/agent/sessions/' + sid + '/presence'
        baseline = client.get('/api/v1/events').json()
        assert client.get('/api/v1/sessions').json()['items'][0]['connection']['state'] == 'unknown'
        assert client.get('/api/v1/sessions?activeOnly=true').json()['items'] == []
        assert client.post(route, json={'connectionId': 'different', 'state': 'connected'}).status_code == 409
        assert client.post(route, json={'connectionId': 'connection', 'state': 'running'}).status_code == 400
        for _ in range(5):
            assert client.post(route, json={'connectionId': 'connection', 'state': 'connected'}).json()['state'] == 'connected'
        session = client.get('/api/v1/sessions/' + sid).json()
        assert session['connection']['state'] == 'connected'
        assert client.get('/api/v1/sessions?activeOnly=true').json()['items'][0]['id'] == sid
        assert session['projection']['foregroundState'] == 'unknown'
        assert session['eventCount'] == 0
        assert client.get('/api/v1/events').json() == baseline
        assert client.get('/api/v1/issues').json()['items'] == []
        assert client.post(route, json={'connectionId':'connection','state':'disconnected'}, headers={'Origin':'https://elsewhere.invalid'}).status_code == 403
        assert client.post(route, json={'connectionId':'connection','state':'disconnected'}).json()['state'] == 'disconnected'
        assert client.get('/api/v1/sessions?activeOnly=true').json()['items'] == []
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        assert client.get('/api/v1/sessions/' + sid).json()['connection']['state'] == 'unknown'
