"""Shared HTTP/MCP queue acceptance against real domain storage, separate from provider execution."""

import asyncio
import json

from fastapi.testclient import TestClient
import httpx

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp

PROFILE = {'id':'fixture', 'version':'1.0', 'protocol':'acp', 'protocolVersion':1}


def test_ui_agent_queue_uses_shared_keys_order_cas_and_runtime_guards(tmp_path):
    """Cross-surface retries keep one intent; cancellation and manual release preserve lane ownership."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        token = {'X-Ag-Tasks-UI':client.get('/api/v1/ui-session').json()['token']}
        count = 0

        def command(operation, payload, *, key=None, human=False, expected=200):
            """Drive the guarded routes, allowing explicit failure assertions without direct SQL changes."""
            nonlocal count
            count += 1
            envelope = {'clientId':'integration','commandId':key or f'key-{count}','operation':operation,'payload':payload}
            response = client.post('/api/v1/commands' if human else '/api/v1/agent/commands',
                                   json=envelope, headers=token if human else {})
            assert response.status_code == expected, response.text
            return response.json()

        command('queue.create', {'laneId':'shared','title':'Общая очередь','profile':PROFILE}, human=True)
        first = command('queue.enqueue', {'laneId':'shared','text':'first'}, key='same-intent', human=True)
        assert command('queue.enqueue', {'laneId':'shared','text':'first'}, key='same-intent') == first
        second = command('queue.enqueue', {'laneId':'shared','text':'second'})
        command('queue.move', {'itemId':second['id'],'expectedRevision':second['revision'],'beforeItemId':first['id']}, human=True)
        pending = client.get('/api/v1/queues/shared/pending?limit=1').json()
        assert pending['items'][0]['id'] == second['id'] and pending['hasMore']
        next_page = client.get('/api/v1/queues/shared/pending',params={'limit':1,'after':pending['nextCursor']}).json()
        assert next_page['items'][0]['id'] == first['id']
        command('queue.cancel', {'itemId':first['id'],'expectedRevision':first['revision']}, human=True, expected=409)
        attempt = command('attempt.claim', {'laneId':'shared','workerId':'worker','workerEpoch':'epoch'})
        identity = {'attemptId':attempt['id'],'workerId':'worker','workerEpoch':'epoch','fence':attempt['fence']}
        assert attempt['itemId'] == second['id']
        command('attempt.dispatch', identity, human=True, expected=403)
        command('attempt.dispatch', identity)
        latest_item = client.get('/api/v1/queue-items/'+second['id']).json()
        command('queue.cancel', {'itemId':second['id'],'expectedRevision':latest_item['revision']}, human=True)
        command('attempt.report', {**identity,'state':'finished','outcome':'cancelled'})
        lane = client.get('/api/v1/queues/shared').json()
        assert lane['state'] == 'busy' and lane['activeAttempt']['cancelRequested']
        command('attempt.claim', {'laneId':'shared','workerId':'worker2','workerEpoch':'epoch2'}, expected=409)
        recovery = {'attemptId':attempt['id'],'expectedRevision':lane['activeAttempt']['revision'],
                    'resolution':'owned_runtime_stopped','evidence':'Operator verified fixture process termination.'}
        command('attempt.reconcile', recovery, expected=403)
        command('attempt.reconcile', recovery, human=True)
        assert client.get('/api/v1/queues/shared').json()['state'] == 'ready'
        assert client.get('/api/v1/queue-items/'+second['id']).json()['state'] == 'cancelled'
        assert client.get('/api/v1/issues').json()['items'] == []
        assert client.get('/api/v1/queues?limit=51').status_code == 422
        assert client.get('/api/v1/queues/shared/attempts?limit=51').status_code == 422
        capabilities = client.get('/api/v1/service-data').json()
        assert capabilities['managedExecution'] is False
        assert capabilities['agentQueue']['completionReleasesLane'] is False
        assert capabilities['externalWorker']['qualifiedProviders'] == []


def test_queue_restart_quarantines_dispatch_even_if_client_replays_saved_ack(tmp_path):
    """An immutable old dispatch outcome cannot authorize work under a new service incarnation."""
    body = {'clientId':'worker','commandId':'dispatch','operation':'attempt.dispatch','payload':{}}
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        def send(operation,payload,key):
            """Use distinct durable worker intents for setup and dispatch."""
            return client.post('/api/v1/agent/commands',json={**body,'operation':operation,'commandId':key,'payload':payload}).json()
        send('queue.create',{'laneId':'restart','title':'Restart','profile':PROFILE},'lane')
        send('queue.enqueue',{'laneId':'restart','text':'do once'},'item')
        attempt = send('attempt.claim',{'laneId':'restart','workerId':'worker','workerEpoch':'epoch'},'claim')
        body['payload'] = {'attemptId':attempt['id'],'workerId':'worker','workerEpoch':'epoch','fence':attempt['fence']}
        ack = client.post('/api/v1/agent/commands',json=body).json()
        assert ack['state'] == 'dispatched'
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as client:
        assert client.post('/api/v1/agent/commands',json=body).json() == ack
        latest = client.get('/api/v1/attempts/'+attempt['id']).json()
        assert latest['state'] == 'unknown'
        assert client.get('/api/v1/queues/restart').json()['state'] == 'quarantined'
        heartbeat = {**body,'operation':'attempt.heartbeat','commandId':'new-heartbeat'}
        assert client.post('/api/v1/agent/commands',json=heartbeat).status_code == 409


def test_fresh_mcp_reads_and_commands_share_queue_core(tmp_path):
    """Negotiate actual MCP handlers and read the queue created through their common command surface."""
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as http:
        def handle(request):
            """Route fixed tool HTTP requests to a real private ASGI instance."""
            response = http.request(request.method,str(request.url),content=request.content,headers=dict(request.headers))
            return httpx.Response(response.status_code,content=response.content,headers=response.headers)
        bridge = build_mcp(origin='http://127.0.0.1:8895',client=httpx.Client(transport=httpx.MockTransport(handle)))

        async def run():
            """Call negotiated tools and decode wire-facing content instead of bypassing adapters."""
            names = {tool.name for tool in await bridge.list_tools()}
            assert {'tasks_queues','tasks_queue','tasks_queue_items','tasks_attempt'} <= names
            await bridge.call_tool('tasks_command',{'client_id':'mcp','command_id':'lane','operation':'queue.create',
                                                  'payload':{'laneId':'mcp-lane','title':'MCP lane','profile':PROFILE}})
            reply = await bridge.call_tool('tasks_queue',{'lane_id':'mcp-lane'})
            content = reply[0] if isinstance(reply,tuple) else reply
            assert json.loads(content[0].text)['profile'] == PROFILE
            pending = await bridge.call_tool('tasks_queue_items',{'lane_id':'mcp-lane'})
            content = pending[0] if isinstance(pending,tuple) else pending
            assert json.loads(content[0].text)['items'] == []
        asyncio.run(run())
