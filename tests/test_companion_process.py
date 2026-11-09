"""Real HTTP owner and fresh stdio MCP clients on disposable task stores."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


def start_owner(directory):
    """Start one test-owned HTTP process, returning only after its actual status responds."""
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    process = subprocess.Popen([sys.executable, '-m', 'ag_tasks', '--data-dir', str(directory), '--port', str(port)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(trust_env=False, timeout=.3) as client:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Private HTTP owner exited before readiness')
                try:
                    if client.get(origin + '/api/v1/status').json().get('application') == 'ag-tasks':
                        return process, origin
                except (httpx.HTTPError, ValueError):
                    time.sleep(.05)
        raise TimeoutError('Private HTTP owner did not become ready')
    except BaseException:
        stop_owner(process)
        raise


def stop_owner(process):
    """Stop only this test's subprocess; never signal an installed owner or foreign child."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def use_stdio(origin, directory, client_name, scenario):
    """Negotiate MCP over actual subprocess pipes with the supplied IDE client identity."""
    environment = dict(os.environ, AG_TASKS_URL=origin, AG_TASKS_DATA_DIR=str(directory))
    parameters = StdioServerParameters(command=sys.executable, args=['-m', 'ag_tasks', '--mcp'], env=environment)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, client_info=types.Implementation(name=client_name, version='test')) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert {'tasks_list', 'tasks_get', 'tasks_sessions', 'tasks_bind_conversation', 'tasks_task_context'} <= names
            async def call(name, arguments):
                """Reject protocol/domain errors before inspecting the real returned data."""
                reply = await session.call_tool(name, arguments)
                assert not reply.isError, reply
                value = json.loads(reply.content[0].text)
                assert 'error' not in value, value
                return value
            return await scenario(call)


def test_real_cursor_codex_mcp_restart_and_other_agent_context(tmp_path):
    """Two real protocol clients keep distinct chats and recover the same task after owner restart."""
    process, origin = start_owner(tmp_path)
    try:
        async def codex(call):
            """Create the first chat through public tools without starting task execution."""
            issue = await call('tasks_create', {'title': 'Cross-harness continuation'})
            chat = await call('tasks_bind_conversation', {'client_id': 'codex-adapter', 'command_id': 'bind',
                 'harness': 'codex', 'instance_id': 'profile', 'remote_session_id': 'codex-chat',
                 'connection_id': 'one', 'issue_id': issue['id'], 'title': 'Codex start'})
            return issue, chat
        issue, first = asyncio.run(use_stdio(origin, tmp_path, 'Codex', codex))
        async def cursor(call):
            """Continue in another chat/agent without replacing the original or its task."""
            chat = await call('tasks_bind_conversation', {'client_id': 'cursor-adapter', 'command_id': 'bind',
                 'harness': 'cursor', 'instance_id': 'profile', 'remote_session_id': 'cursor-chat',
                 'connection_id': 'one', 'issue_id': issue['id'], 'title': 'Cursor continuation', 'continued_from': first['id']})
            context = await call('tasks_task_context', {'conversation_id': chat['id']})
            assert context['issue']['id'] == issue['id']
            assert len((await call('tasks_conversations', {'issue_id': issue['id']}))['items']) == 2
            return chat
        second = asyncio.run(use_stdio(origin, tmp_path, 'Cursor', cursor))
    finally:
        stop_owner(process)
    process, origin = start_owner(tmp_path)
    try:
        async def recover(call):
            """Read fresh binding revision after restart before recording a new transport."""
            saved = await call('tasks_resolve_conversation', {'harness': 'cursor', 'instance_id': 'profile',
                                'remote_session_id': 'cursor-chat'})
            assert saved['id'] == second['id'] and not saved['connection']['reportedToCurrentOwner']
            renewed = await call('tasks_reconnect_conversation', {'client_id': 'cursor-adapter', 'command_id': 'reconnect',
                                 'conversation_id': saved['id'], 'expected_revision': saved['revision'], 'connection_id': 'two'})
            assert renewed['id'] == second['id'] and renewed['continuedFrom'] == first['id']
            context = await call('tasks_task_context', {'issue_id': issue['key']})
            assert len(context['sections']['chats']['items']) == 2
            task = await call('tasks_get', {'issue_id': issue['id']})
            assert task['work']['state'] == 'never_started' and task['status'] == 'backlog'
            assert len(task['conversations']['items']) == 2
        asyncio.run(use_stdio(origin, tmp_path, 'Other agent', recover))
    finally:
        stop_owner(process)
