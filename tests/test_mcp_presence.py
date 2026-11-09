"""Exercise the real SDK handshake through observed streams and a private HTTP owner."""

from contextlib import asynccontextmanager
import json

import anyio
from fastapi.testclient import TestClient
import httpx
from mcp import ClientSession, types

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp
from ag_tasks.mcp_presence import ConnectionAnnouncer


def test_real_mcp_handshake_and_eof_register_distinct_connections(tmp_path, monkeypatch):
    """Two real protocol clients appear automatically; EOF closes them and tool input is never archived."""
    async def scenario(http):
        def handle(request):
            response = http.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        bridge = build_mcp(origin='http://127.0.0.1:8895', client=httpx.Client(transport=httpx.MockTransport(handle)))
        identities = []
        for name in ('Codex test', 'Cursor test'):
            client_write, server_read = anyio.create_memory_object_stream(0)
            server_write, client_read = anyio.create_memory_object_stream(0)
            @asynccontextmanager
            async def stdio():
                yield server_read, server_write
            monkeypatch.setattr('ag_tasks.mcp_presence.stdio_server', stdio)
            ended = anyio.Event()
            async def serve():
                try:
                    await bridge.run_stdio_async()
                finally:
                    ended.set()
            async with anyio.create_task_group() as group:
                group.start_soon(serve)
                async with ClientSession(client_read, client_write,
                                         client_info=types.Implementation(name=name, version='1.0')) as client:
                    await client.initialize()
                    assert (await client.list_tools()).tools
                    await client.call_tool('tasks_list', {'space_id':'private-tool-argument','limit':1})
                    with anyio.fail_after(3):
                        while True:
                            sessions = http.get('/api/v1/sessions').json()['items']
                            current = [item for item in sessions if item['agent'] == name]
                            if current and current[0]['connection']['state'] == 'connected':
                                break
                            await anyio.sleep(0.01)
                    identities.append(current[0]['id'])
                    assert current[0]['projection']['foregroundState'] == 'unknown'
                with anyio.fail_after(3):
                    await ended.wait()
            assert http.get('/api/v1/sessions/' + identities[-1]).json()['connection']['state'] == 'disconnected'
        assert len(set(identities)) == 2
        records = http.get('/api/v1/sessions').json()['items']
        assert all(item['eventCount'] == 0 for item in records)
        assert 'private-tool-argument' not in json.dumps(records)
        assert http.get('/api/v1/issues').json()['items'] == []
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as http:
        anyio.run(scenario, http)


def test_registration_retry_keeps_identity_and_ignores_tool_metadata():
    """An unavailable owner can recover without a new registration intent or transcript leakage."""
    async def scenario():
        calls=[]
        def request(method, route, payload, **options):
            calls.append((route, json.loads(json.dumps(payload))))
            if len(calls)==1:
                return {'error':{'code':'tasks_unavailable'}}
            return {'id':'a'*32} if route.endswith('/commands') else {'state':'connected'}
        observer=ConnectionAnnouncer(request)
        from mcp.shared.message import SessionMessage
        def receive(value):
            observer.observe(SessionMessage(types.JSONRPCMessage.model_validate(value)))
        receive({'jsonrpc':'2.0','id':1,'method':'initialize',
                 'params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'Codex','version':'test'}}})
        receive({'jsonrpc':'2.0','method':'notifications/initialized'})
        receive({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'private-tool-argument'}})
        observer.pulse();observer.pulse()
        assert calls[0] == calls[1]
        assert len(calls)==3
        assert 'private-tool-argument' not in json.dumps(calls)
    anyio.run(scenario)
