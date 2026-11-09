"""Real local HTTP and MCP-adapter contracts against the shared domain command service."""

import asyncio
import json

from fastapi.testclient import TestClient
import httpx

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp


def envelope(operation, payload, key='command-1'):
    """Use one stable client intent identity across multiple transports."""
    return {'clientId': 'integration', 'commandId': key, 'operation': operation, 'payload': payload}


def test_http_agent_ui_and_outcome_share_the_same_command(tmp_path):
    """Retry from another adapter returns the committed reply, while the guarded UI retains its authority."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        headers = {'X-Ag-Tasks-UI': client.get('/api/v1/ui-session').json()['token']}
        body = envelope('issue.create', {'title': 'Universal task'})
        first = client.post('/api/v1/agent/commands', json=body)
        assert first.status_code == 200, first.text
        again = client.post('/api/v1/commands', headers=headers, json=body)
        assert again.json() == first.json()
        assert len(client.get('/api/v1/issues').json()['items']) == 1
        outcome = client.get('/api/v1/commands/integration/command-1').json()
        assert outcome['response'] == first.json()
        assert client.post('/api/v1/commands', json=body).status_code == 403
        changed = envelope('issue.create', {'title': 'Different task'})
        assert client.post('/api/v1/agent/commands', json=changed).status_code == 409
        assert client.get('/api/v1/status').json()['serviceDataContract']['managedExecution'] is False


def test_http_session_events_and_link_are_bounded_passive_and_safe(tmp_path):
    """A complete register/ingest/read/rebuild path retains evidence without creating tasks or execution."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        registered = client.post('/api/v1/agent/commands', json=envelope('session.register', {
            'title': 'Cursor source', 'agent': 'cursor', 'protocol': 'acp', 'protocolVersion': 1,
            'remoteSessionId': 'remote', 'connectionId': 'epoch'})).json()
        sid = registered['id']
        events = {'sessionId': sid, 'producerId': 'producer', 'producerEpoch': 'first', 'events': [
            {'sequence': 1, 'connectionId': 'epoch', 'direction': 'agent_to_client', 'origin': 'live',
             'payload': {'jsonrpc': '2.0', 'method': '_custom/future', '_meta': {'future': 'keep'}}}]}
        response = client.post('/api/v1/agent/commands', json=envelope('events.append', events, 'ingest'))
        assert response.status_code == 200 and response.json()['acknowledgedThrough'] == 1
        # Another submission key still hits per-event deduplication.
        again = client.post('/api/v1/agent/commands', json=envelope('events.append', events, 'ingest-retry'))
        assert again.json()['inserted'] == 0
        assert client.get(f'/api/v1/sessions/{sid}/events').json()['items'][0]['payload']['_meta']['future'] == 'keep'
        assert client.get('/api/v1/sessions?limit=51').status_code == 422
        assert client.get(f'/api/v1/sessions/{sid}').json()['mode'] == 'observed'
        assert client.post('/api/v1/agent/commands', json=envelope('session.rebuild', {'sessionId':sid}, 'rebuild')).status_code == 200
        assert client.get('/api/v1/issues').json()['items'] == []


def test_new_command_adapter_cannot_spoof_actor_or_bypass_existing_workflow(tmp_path):
    """Generic dispatch is not an arbitrary method invoker, human approval or process launch capability."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        issue = client.post('/api/v1/agent/commands', json=envelope('issue.create', {'title':'test'})).json()
        denied = client.post('/api/v1/agent/commands', json=envelope('issue.update', {'issueId':issue['id'],'changes':{'status':'done'}}, 'finish'))
        assert denied.status_code >= 400
        assert client.get('/api/v1/issues/'+issue['id']).json()['status'] == 'backlog'
        assert client.post('/api/v1/agent/commands', json=envelope('process.start', {}, 'start')).status_code == 400
        body = envelope('issue.create', {'title':'spoof'}, 'spoof'); body['actor'] = 'user'
        assert client.post('/api/v1/agent/commands', json=body).status_code == 400
        invalid = envelope('issue.update', {'issueId':issue['id'],'changes':[{}]}, 'bad-shape')
        assert client.post('/api/v1/agent/commands', json=invalid).status_code == 400
        assert client.post('/api/v1/agent/commands', json=envelope('issue.create', {'title':'cross'}, 'cross'), headers={'Origin':'https://elsewhere.invalid'}).status_code == 403


def test_mcp_routes_explicit_commands_to_the_common_core(tmp_path):
    """Fresh MCP tool handlers use fixed shared-domain routes; they never implement their own storage."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as http:
        def handle(request):
            """Bridge real HTTP requests without opening the installed service or a second store."""
            response = http.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        bridge = build_mcp(origin='http://127.0.0.1:8895', client=httpx.Client(transport=httpx.MockTransport(handle)))
        async def run():
            """Negotiate and invoke actual FastMCP tools, including immutable command retries."""
            names = {tool.name for tool in await bridge.list_tools()}
            assert {'tasks_command','tasks_session_events','tasks_sessions'} <= names
            arguments = {'client_id':'mcp','command_id':'stable','operation':'issue.create','payload':{'title':'MCP task'}}
            first = await bridge.call_tool('tasks_command', arguments)
            second = await bridge.call_tool('tasks_command', arguments)
            # FastMCP returns content plus structured data in this installed SDK.
            def decoded(reply):
                """Read the wire-facing text response rather than a private domain callback."""
                content = reply[0] if isinstance(reply, tuple) else reply
                return json.loads(content[0].text)
            assert decoded(first)['id'] == decoded(second)['id']
        asyncio.run(run())
        assert len(http.get('/api/v1/issues').json()['items']) == 1
