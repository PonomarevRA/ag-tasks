"""Fresh standard MCP client and bounded session owner for local agents."""
import asyncio
import json
import sys
import os
from contextlib import contextmanager
from datetime import timedelta

MAX_LINE = 131072


def decode_result(result):
    """Unwrap only a successful object response; retain domain failures without retrying writes."""
    for content in result.content:
        if content.type == 'text':
            value = json.loads(content.text)
            if isinstance(value, dict):
                return value
    raise ValueError('MCP returned no object')


class Console:
    """Keep one declared work lease only while this bounded JSONL controller is alive."""

    def __init__(self, session, names, *, idle_seconds=300, output=None):
        self.session, self.names = session, set(names)
        self.idle_seconds = idle_seconds
        self.output = output or (lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        self.active = None
        self.call_lock = asyncio.Lock()
        self.last_input = asyncio.get_running_loop().time()
        self.failed = False

    async def invoke(self, name, arguments):
        """Serialize commands with lease renewal so completion cannot race a queued heartbeat."""
        async with self.call_lock:
            return await self._invoke(name, arguments)

    async def _invoke(self, name, arguments):
        """Call one advertised tool under call_lock; preserve rejected work without retrying writes."""
        if name not in self.names or not name.startswith('tasks_') or not isinstance(arguments, dict):
            raise ValueError('Tool not advertised by the fresh MCP connection')
        if name == 'tasks_work_start' and self.active and (arguments.get('issue_id'), arguments.get('agent_id'), arguments.get('session_key')) != (self.active['issue_id'], self.active['agent_id'], self.active['session_key']):
            raise ValueError('Stop the owned work session before starting another')
        reply = await self.session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=35))
        value = decode_result(reply)
        if reply.isError or 'error' in value:
            error = value.get('error', {})
            if self.active:
                exact_session = all(arguments.get(key) == self.active[key] for key in self.active)
                rework_issue = error.get('details', {}).get('issueId') == self.active['issue_id']
                session_lost = error.get('code') == 'work_session_unavailable' and exact_session
                definition_closed = error.get('code') == 'definition_failed' and rework_issue
                if session_lost or definition_closed:
                    self.active = None
            return value
        if name == 'tasks_work_start':
            live = value.get('work', {}).get('activeSession')
            if not live:
                raise ValueError('Start returned no active lease; inspect the issue before retrying')
            self.active = {'issue_id': value['id'], 'agent_id': arguments['agent_id'], 'session_key': arguments['session_key'], 'session_id': live['sessionId']}
        elif name in ('tasks_work_stop', 'tasks_pause_work', 'tasks_submit_result', 'tasks_review_report') and self.active and arguments.get('session_id') == self.active['session_id']:
            self.active = None
        if self.active and value.get('id') == self.active['issue_id'] and 'work' in value:
            current = value['work'].get('activeSession')
            if not current or current['sessionId'] != self.active['session_id']:
                self.active = None
        return value

    async def pause(self):
        """Serialize exact-session stop with commands; server expiry bounds an unconfirmed stop."""
        async with self.call_lock:
            if not self.active:
                return
            credentials, self.active = self.active, None
            try:
                result = await self._invoke('tasks_work_stop', credentials)
                self.output({'event': 'work_stopped', 'issueId': credentials['issue_id'], 'error': result.get('error')})
            except Exception:
                self.output({'event': 'stop_unconfirmed', 'issueId': credentials['issue_id']})
                self.failed = True

    async def renew(self, credentials):
        """Skip a queued renewal if a preceding command already closed or replaced its owned lease."""
        async with self.call_lock:
            if self.active != credentials:
                return None
            return await self._invoke('tasks_work_heartbeat', credentials)

    async def maintain(self):
        """Renew independently of UI/model polling; stop on idle, failure or lost ownership."""
        while True:
            await asyncio.sleep(25)
            if not self.active:
                continue
            if asyncio.get_running_loop().time() - self.last_input >= self.idle_seconds:
                await self.pause()
                self.output({'event': 'idle_paused'})
                continue
            try:
                result = await self.renew(self.active.copy())
                if result is not None and 'error' in result:
                    if result['error'].get('code') == 'tasks_unavailable':
                        # A brief owner restart does not revoke this external
                        # worker's credentials. Retry only the idempotent pulse;
                        # the server alone decides whether its lease expired.
                        self.output({'event':'heartbeat_deferred', 'error':result['error']})
                        continue
                    self.active = None
                    self.failed = True
                    self.output({'event': 'lease_lost', 'error': result['error']})
            except Exception:
                await self.pause()
                self.failed = True
                self.output({'event': 'heartbeat_failed'})

    async def process(self, line):
        """Validate one bounded JSONL request; text remains data and cannot select a process or URL."""
        if len(line) > MAX_LINE:
            raise ValueError('Request exceeds 128 KiB')
        request = json.loads(line)
        if request == {'control':'close'}:
            self.output({'event':'closing'})
            return False
        if type(request) is not dict or set(request) - {'id', 'tool', 'arguments'} or 'tool' not in request:
            raise ValueError('Expected id/tool/arguments')
        self.last_input = asyncio.get_running_loop().time()
        value = await self.invoke(request['tool'], request.get('arguments', {}))
        self.output({'id': request.get('id'), 'result': value})

    async def run(self, reader):
        """Read until EOF; always pause owned work before closing the MCP transport."""
        heartbeat = asyncio.create_task(self.maintain())
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    if await self.process(line) is False:
                        break
                except (ValueError, TypeError, KeyError):
                    self.output({'error': {'code': 'invalid_console_request'}})
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self.pause()


@contextmanager
def terminal_input():
    """Avoid POSIX canonical-line truncation in the console's own PTY; restore it on normal exit."""
    saved = None
    if os.name == 'posix' and sys.stdin.isatty():
        import termios
        saved = termios.tcgetattr(sys.stdin.fileno())
        mode = termios.tcgetattr(sys.stdin.fileno())
        mode[3] &= ~(termios.ICANON | termios.ECHO)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, mode)
    try:
        yield
    finally:
        if saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)


async def run_console(command, arguments, *, list_only=False, idle_seconds=300):
    """Negotiate tools from the installed executable, never from cached client configuration."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(command=command, args=arguments)) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            if initialized.serverInfo.name != 'ag-tasks':
                raise ValueError('MCP service identity mismatch')
            discovered = await session.list_tools()
            names = [tool.name for tool in discovered.tools]
            if list_only:
                print(json.dumps({'server': initialized.serverInfo.model_dump(), 'tools': [tool.model_dump() for tool in discovered.tools]}, ensure_ascii=False), flush=True)
                return
            class Input:
                async def readline(self):
                    """Drain an oversized record so its tail cannot become a new request."""
                    line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1)
                    if len(line) > MAX_LINE:
                        chunk = line
                        while chunk and not chunk.endswith(b'\n'):
                            chunk = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1)
                    return line
            reader = Input()
            with terminal_input():
                print(json.dumps({'event': 'ready', 'tools': names, 'idlePauseSeconds': idle_seconds}), flush=True)
                await Console(session, names, idle_seconds=idle_seconds).run(reader)
