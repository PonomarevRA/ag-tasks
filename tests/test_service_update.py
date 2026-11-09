"""Service updates preserve external work through real parallel MCP transports and strict discovery."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from ag_tasks.mcp_server import build_mcp


ROOT = Path(__file__).resolve().parents[1]
SERVER = '''
from pathlib import Path
import socket,sys
import uvicorn
from ag_tasks.app import create_app
listener=socket.socket()
listener.bind(('127.0.0.1',0))
listener.listen(128)
origin='http://127.0.0.1:'+str(listener.getsockname()[1])
app=create_app(Path(sys.argv[1]),origin=origin)
server=uvicorn.Server(uvicorn.Config(app,log_level='error',timeout_graceful_shutdown=2))
server.run(sockets=[listener])
'''


def unwrap(reply):
    """Read the SDK's structured MCP result across direct-server and stdio client calls."""
    if isinstance(reply, tuple):
        reply = reply[1]
    if isinstance(reply, dict):
        return reply
    if getattr(reply, 'structuredContent', None) is not None:
        return reply.structuredContent
    content = reply.content if hasattr(reply, 'content') else reply
    return json.loads(next(item.text for item in content if item.type == 'text'))


def test_missing_discovery_never_connects_to_another_local_store(tmp_path, monkeypatch):
    """An explicit empty data directory is unavailable, rather than a route to historical port8895."""
    monkeypatch.delenv('AG_TASKS_URL', raising=False)
    calls = []
    def handle(request):
        """Make any attempted HTTP request visible without contacting a live service."""
        calls.append(str(request.url))
        return httpx.Response(200, json={'application':'ag-tasks','items':[]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        mcp = build_mcp(directory=tmp_path, client=transport)
        value = unwrap(asyncio.run(mcp.call_tool('tasks_list', {})))
    assert value == {'error':{'code':'tasks_unavailable'}}
    assert calls == []


def test_discovery_identity_mismatch_does_not_send_the_requested_mutation(tmp_path, monkeypatch):
    """A replacement on the same port must match the instance currently published for this store."""
    monkeypatch.delenv('AG_TASKS_URL', raising=False)
    (tmp_path / 'endpoint.json').write_text(json.dumps({'origin':'http://127.0.0.1:38991','instanceId':'expected'}))
    calls = []
    def handle(request):
        """Expose a different owner while recording every attempted route."""
        calls.append((request.method,request.url.path))
        return httpx.Response(200,json={'application':'ag-tasks','instanceId':'different'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        mcp = build_mcp(directory=tmp_path, client=transport)
        value = unwrap(asyncio.run(mcp.call_tool('tasks_create', {'title':'Must not be written'})))
    assert value == {'error':{'code':'tasks_unavailable'}}
    assert calls == [('GET','/api/v1/status')]


class Owner:
    """Start and stop only this test's ephemeral HTTP owner and retain bounded diagnostics on failure."""

    def __init__(self, directory):
        """Limit every child to the test checkout/data directory and remove ambient service routing."""
        self.directory = directory
        self.process = None
        self.log = (directory / 'owner.log').open('ab')
        self.environment = {**os.environ, 'PYTHONPATH':str(ROOT)}
        self.environment.pop('AG_TASKS_URL', None)

    def start(self, previous=None):
        """Wait for a new published owner and verified identity; every restart chooses a fresh socket."""
        self.process = subprocess.Popen([sys.executable,'-c',SERVER,str(self.directory)], cwd=ROOT,
                                        env=self.environment, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError((self.directory / 'owner.log').read_text())
            try:
                endpoint = json.loads((self.directory / 'endpoint.json').read_text())
                if endpoint['instanceId'] != previous:
                    with urlopen(endpoint['origin'] + '/api/v1/status', timeout=.5) as response:
                        status = json.load(response)
                    if status['instanceId'] == endpoint['instanceId']:
                        return endpoint
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.02)
        raise AssertionError('Isolated service did not become ready')

    def stop(self, crash=False):
        """Bound shutdown of the owned process; no process discovery, shared state, or other PID is touched."""
        if self.process is None or self.process.poll() is not None:
            return
        if crash:
            self.process.kill()
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise

    def close(self):
        """Release the final owner and diagnostics after all client scopes have closed."""
        try:
            self.stop()
        finally:
            self.log.close()


@asynccontextmanager
async def mcp_connection(directory, environment):
    """Keep one real stdio MCP client connected across all HTTP owner restarts."""
    options = StdioServerParameters(command=sys.executable,
                                   args=['-m','ag_tasks','--mcp','--data-dir',str(directory)],
                                   cwd=str(ROOT), env=environment)
    async with stdio_client(options) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def invoke(session, tool_name, **arguments):
    """Return a public MCP object and fail on transport-level tool errors, retaining ordinary domain errors."""
    reply = await session.call_tool(tool_name, arguments)
    assert not reply.isError, reply
    return unwrap(reply)


def committed_rows(directory):
    """Read committed test evidence without opening a second domain store or reconciling its sessions."""
    with sqlite3.connect((directory / 'issues.sqlite3').as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return {row['issue_id']:dict(row) for row in connection.execute('SELECT * FROM work_sessions')}


@pytest.mark.parametrize('crash', [False, True], ids=['graceful-update','owner-crash'])
def test_six_parallel_mcp_sessions_survive_owner_replacement(tmp_path, crash):
    """Six unchanged stdio clients renew their original external sessions after a fresh-port HTTP restart."""
    owner = Owner(tmp_path)
    async def scenario():
        """Exercise public creation/start/pulse/stop calls and compare exact committed session evidence."""
        async with AsyncExitStack() as stack:
            clients = [await stack.enter_async_context(mcp_connection(tmp_path, owner.environment)) for _ in range(6)]
            criteria = [{'id':'checked','text':'Isolated probe','passed':True,'evidence':'Restart fixture'}]
            space = await invoke(clients[0], 'tasks_ensure_project_space', project_key='restart-probe',
                                 name='Restart probe', prefix='PROBE', client_id='isolated-update', command_id='project')
            assert 'error' not in space, space
            issues = await asyncio.gather(*[
                invoke(client, 'tasks_create', title=f'Parallel agent {index}', space_id=space['id'],
                       intended_role='developer', dor=criteria, dod=criteria)
                for index, client in enumerate(clients)])
            starts = await asyncio.gather(*[
                invoke(client, 'tasks_work_start', issue_id=issue['id'], agent_id=f'probe-{index}', session_key=f'run-{index}')
                for index, (client, issue) in enumerate(zip(clients, issues))])
            credentials = [dict(issue_id=issue['id'],agent_id=f'probe-{index}',session_key=f'run-{index}',
                                session_id=started['work']['activeSession']['sessionId'])
                           for index, (issue, started) in enumerate(zip(issues, starts))]
            await asyncio.gather(*[invoke(client,'tasks_work_heartbeat',**keys) for client,keys in zip(clients,credentials)])
            before = committed_rows(tmp_path)
            owner.stop(crash)
            unavailable = await asyncio.gather(*[invoke(client,'tasks_work_heartbeat',**keys) for client,keys in zip(clients,credentials)])
            assert all(value == {'error':{'code':'tasks_unavailable'}} for value in unavailable)
            second = await asyncio.to_thread(owner.start, first['instanceId'])
            assert second['instanceId'] != first['instanceId']
            assert second['origin'] != first['origin']
            renewed = await asyncio.gather(*[invoke(client,'tasks_work_heartbeat',**keys) for client,keys in zip(clients,credentials)])
            after = committed_rows(tmp_path)
            for keys, value in zip(credentials, renewed):
                assert value['work']['activeSession']['sessionId'] == keys['session_id']
                assert value['status'] == 'in_progress'
                assert after[keys['issue_id']]['id'] == before[keys['issue_id']]['id']
                assert after[keys['issue_id']]['elapsed_ms'] == before[keys['issue_id']]['elapsed_ms']
            # A later accepted owner interval is accounted once, then repeated stop stays idempotent.
            await asyncio.sleep(.04)
            await asyncio.gather(*[invoke(client,'tasks_work_heartbeat',**keys) for client,keys in zip(clients,credentials)])
            await asyncio.gather(*[invoke(client,'tasks_work_stop',**keys) for client,keys in zip(clients,credentials)])
            stopped = committed_rows(tmp_path)
            await asyncio.gather(*[invoke(client,'tasks_work_stop',**keys) for client,keys in zip(clients,credentials)])
            assert committed_rows(tmp_path) == stopped
            assert len(stopped) == 6
            for issue, keys in zip(issues, credentials):
                row = stopped[issue['id']]
                assert row['state'] == 'paused'
                assert 0 <= row['elapsed_ms'] - before[issue['id']]['elapsed_ms'] < 10_000
                detail = await invoke(clients[0], 'tasks_get', issue_id=issue['id'])
                assert len(detail['workHistory']['items']) == 1
                actions = [entry['action'] for entry in detail['history']]
                assert actions.count('work_started') == actions.count('work_stopped') == 1
                assert 'work_interrupted' not in actions and 'work_expired' not in actions
            return {'clients':6,'crash':crash,'oldInstance':first['instanceId'],'newInstance':second['instanceId'],
                    'oldOrigin':first['origin'],'newOrigin':second['origin'],'sessionIdsPreserved':6,
                    'unknownGapChargedMs':0,'rows':6,'duplicateStopsAddedMs':0}
    try:
        first = owner.start()
        try:
            report = asyncio.run(scenario())
        except BaseExceptionGroup as error:
            pending, messages = [error], []
            while pending:
                current = pending.pop()
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                else:
                    messages.append(type(current).__name__ + ': ' + str(current))
            raise AssertionError('\n'.join(messages)) from None
        (tmp_path / 'restart-evidence.json').write_text(json.dumps(report,indent=2))
    finally:
        owner.close()
