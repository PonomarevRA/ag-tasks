"""HTTP and negotiated MCP adapters share chat identity and fresh context semantics."""

import asyncio
import json

from fastapi.testclient import TestClient
import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp


@pytest.mark.parametrize('identity', ['../../status', 'task/other', '..'])
def test_context_tool_rejects_path_traversal_before_transport(identity):
    """Malformed local keys cannot redirect a fixed read tool to another HTTP route."""
    requests = []
    def handle(request):
        """Record any unexpected network call from a rejected tool argument."""
        requests.append(request.url.path)
        return httpx.Response(200, json={})
    bridge = build_mcp(origin='http://127.0.0.1:8895', client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(ToolError, match='Invalid task/chat identity'):
        asyncio.run(bridge.call_tool('tasks_task_context', {'issue_id': identity}))
    assert requests == []


def test_http_registry_supports_cross_chat_continuation_and_cas(tmp_path):
    """HTTP clients use one neutral command contract; reading never grants execution."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        issue = client.post('/api/v1/agent/issues', json={'title': 'HTTP task'}).json()
        payload = {'harness': 'other-harness', 'instanceId': 'installation', 'remoteSessionId': 'chat/with space',
                   'connectionId': 'first', 'title': 'Chat', 'issueId': issue['id'], 'expectedRevision': 0}
        envelope = {'clientId': 'http', 'commandId': 'bind', 'operation': 'conversation.bind', 'payload': payload}
        response = client.post('/api/v1/agent/commands', json=envelope)
        assert response.status_code == 200, response.text
        chat = response.json()
        resolved = client.get('/api/v1/conversations/resolve', params={key: payload[key] for key in ('harness', 'instanceId', 'remoteSessionId')}).json()
        assert resolved['id'] == chat['id']
        assert client.get('/api/v1/conversations', params={'issueId': issue['key']}).json()['items'][0]['id'] == chat['id']
        packet = client.get(f"/api/v1/conversations/{chat['id']}/context").json()
        assert packet['issue']['id'] == issue['id']
        assert client.get(f"/api/v1/issues/{issue['key']}/context").json()['issue']['id'] == issue['id']
        assert client.get(f"/api/v1/issues/{issue['id']}/context?maxBytes=100").status_code == 422
        assert client.post('/api/v1/commands', json=envelope).status_code == 403
        assert client.post('/api/v1/agent/commands', json=envelope, headers={'Origin': 'https://other.invalid'}).status_code == 403
        assert client.get('/api/v1/issues/' + issue['id']).json()['work']['state'] == 'never_started'


def test_mcp_new_tools_add_to_legacy_codex_cursor_contract(tmp_path):
    """Actual FastMCP handlers bind two clients and preserve old tools and shared HTTP data."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        def handle(request):
            """Route the adapter to the real private owner, with its usual HTTP guards."""
            response = client.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        bridge = build_mcp(origin='http://127.0.0.1:8895', client=httpx.Client(transport=httpx.MockTransport(handle)))
        async def call(name, arguments):
            """Decode the SDK's wire-facing tool response, not a private domain callback."""
            reply = await bridge.call_tool(name, arguments)
            content = reply[0] if isinstance(reply, tuple) else reply
            return json.loads(content[0].text)
        async def scenario():
            """Negotiate both old and additive tools, then continue a task in another harness."""
            names = {tool.name for tool in await bridge.list_tools()}
            assert {'tasks_list', 'tasks_get', 'tasks_work_start', 'tasks_command', 'tasks_sessions',
                    'tasks_conversations', 'tasks_task_context', 'tasks_bind_conversation',
                    'tasks_resolve_conversation', 'tasks_reconnect_conversation'} <= names
            issue = await call('tasks_create', {'title': 'MCP task'})
            args = {'client_id': 'codex-client', 'command_id': 'bind', 'harness': 'codex', 'instance_id': 'codex-local',
                    'remote_session_id': 'codex-chat', 'connection_id': 'codex-connection',
                    'issue_id': issue['id'], 'title': 'Codex work'}
            first = await call('tasks_bind_conversation', args)
            assert 'error' not in first, first
            args.update(client_id='cursor-client', harness='cursor', instance_id='cursor-profile',
                        remote_session_id='cursor-chat', connection_id='cursor-connection',
                        title='Cursor continuation', continued_from=first['id'])
            second = await call('tasks_bind_conversation', args)
            chats = await call('tasks_conversations', {'issue_id': issue['key']})
            assert [item['id'] for item in chats['items']] == [first['id'], second['id']]
            assert second['continuedFrom'] == first['id']
            assert (await call('tasks_task_context', {'conversation_id': second['id']}))['issue']['id'] == issue['id']
            assert (await call('tasks_task_context', {}))['error']['code'] == 'invalid_context_identity'
            restored = await call('tasks_resolve_conversation', {'harness': 'cursor', 'instance_id': 'cursor-profile',
                                   'remote_session_id': 'cursor-chat'})
            renewed = await call('tasks_reconnect_conversation', {'client_id': 'cursor-client', 'command_id': 'reconnect',
                                 'conversation_id': restored['id'], 'expected_revision': restored['revision'], 'connection_id': 'next'})
            assert renewed['id'] == second['id'] and renewed['revision'] == 2
            assert (await call('tasks_get', {'issue_id': issue['id']}))['status'] == 'backlog'
        asyncio.run(scenario())
