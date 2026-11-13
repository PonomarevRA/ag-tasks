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
    assert registry.expire() == ['one']
    assert registry.expire() == []
    registry.touch('one', 'disconnected')
    assert registry.touch('one', 'connected')['state'] == 'disconnected'
    assert SessionPresence().snapshot('one')['state'] == 'unknown'


def test_http_presence_binding_guard_and_no_durable_heartbeat_growth(tmp_path):
    """Only the matching MCP binding can pulse, and frequent pulses do not append archive entries."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895', client=('127.0.0.1', 41000)) as client:
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
        owner = client.app.state.telemetry
        assert owner.technical.flush() and owner.activity.flush()
        metrics = client.get('/api/diagnostics/v1/execution').json()
        assert metrics['presencePulsesCoalesced'] == 4
        assert all(entry['coverageSeconds'] >= 0 for entry in metrics['journalCoverage'].values())
        technical = client.get('/api/technical-logs/v1/events').json()['events']
        presence = [event for event in technical if '/presence' in event['message']]
        assert sorted(event['properties']['statusCode'] for event in presence) == [200, 200, 400, 409]
        assert {e['properties'].get('presenceState') for e in presence} == {'connected', 'disconnected', None}
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895', client=('127.0.0.1', 41000)) as client:
        assert client.get('/api/v1/sessions/' + sid).json()['connection']['state'] == 'unknown'


def test_reconnect_before_maintenance_records_expiry_once(tmp_path):
    """A reconnect preserves the expired transition even between periodic sweeps."""
    app = create_app(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8895', client=('127.0.0.1', 41000)) as client:
        sid = register(client)
        path = f'/api/v1/agent/sessions/{sid}/presence'
        clock = [0]
        app.state.store.service.sessions.presence.clock = lambda: clock[0]
        pulse = {'connectionId': 'connection', 'state': 'connected'}
        assert client.post(path, json=pulse).status_code == 200
        clock[0] = 91
        assert client.post(path, json=pulse).status_code == 200
        assert app.state.store.service.sessions.expire_presence() == []
        assert app.state.telemetry.technical.flush()
        events = client.get('/api/technical-logs/v1/events').json()['events']
        expired = [event for event in events if event['eventName'] == 'connection.presence.expired']
        assert len(expired) == 1 and expired[0]['sessionId'] == sid
