"""Observe the actual stdio handshake, without retaining tool arguments or conversation data."""

from uuid import uuid4

import anyio
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server


class ConnectionAnnouncer:
    """Publish one observed binding per initialized stdio transport with bounded HTTP retries."""

    def __init__(self, request):
        """Share the bridge's identity-checked HTTP adapter; never open service storage here."""
        self.request = request
        self.connection_id = uuid4().hex
        self.session_id, self.registration = None, None
        self.ready = anyio.Event()

    def observe(self, message):
        """Read only validated handshake metadata and pass every original message through unchanged."""
        if isinstance(message, Exception):
            return
        root = message.message.root
        if isinstance(root, types.JSONRPCRequest) and root.method == 'initialize' and self.registration is None:
            try:
                initialized = types.InitializeRequest.model_validate(root.model_dump(by_alias=True))
            except ValueError:
                return
            info = initialized.params.clientInfo
            name = ''.join(character for character in info.name if character.isprintable()).strip()[:80] or 'MCP client'
            self.registration = {
                'clientId': 'mcp-' + self.connection_id, 'commandId': 'register', 'operation': 'session.register',
                'payload': {'title': name + ' · MCP', 'agent': name, 'protocol': 'mcp', 'protocolVersion': 1,
                            'remoteSessionId': self.connection_id, 'connectionId': self.connection_id,
                            'capabilities': {'sourceKind': 'mcp_connection', 'clientVersion': info.version[:64],
                                             'mcpProtocolVersion': initialized.params.protocolVersion[:64]}}}
        elif isinstance(root, types.JSONRPCNotification) and root.method == 'notifications/initialized' and self.registration:
            self.ready.set()

    def pulse(self, state='connected'):
        """Reuse the exact registration intent after ambiguous ACK loss; presence stays ephemeral."""
        if self.session_id is None:
            if state == 'disconnected':
                return
            registered = self.request('POST', '/api/v1/agent/commands', self.registration, timeout=2)
            if 'error' in registered:
                return
            session_id = registered.get('id')
            if not isinstance(session_id, str) or len(session_id) != 32 or any(char not in '0123456789abcdef' for char in session_id):
                return
            self.session_id = session_id
        result = self.request('POST', '/api/v1/agent/sessions/' + self.session_id + '/presence',
                              {'connectionId': self.connection_id, 'state': state}, timeout=2)
        if result.get('error', {}).get('code') == 'session_not_found':
            # A different owner store needs its own binding; the same registration key remains safe.
            self.session_id = None

    async def run(self):
        """Renew independently of model turns; outages leave the last server lease to expire."""
        await self.ready.wait()
        while True:
            await anyio.to_thread.run_sync(self.pulse)
            await anyio.sleep(25)


class ObservedReceiveStream:
    """Decorate the SDK receive stream without copying, buffering or changing protocol messages."""

    def __init__(self, stream, observer):
        self.stream, self.observer = stream, observer

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *arguments):
        return await self.stream.__aexit__(*arguments)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.stream.__anext__()
        self.observer.observe(message)
        return message


class ObservedMCP(FastMCP):
    """Keep the SDK tool server unchanged while attaching presence to its real stdio lifetime."""

    def __init__(self, name, *, request, **options):
        super().__init__(name, **options)
        self.presence_request = request

    async def run_stdio_async(self):
        """Mirror FastMCP's stdio adapter and close presence on EOF or cancellation."""
        observer = ConnectionAnnouncer(self.presence_request)
        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as group:
                group.start_soon(observer.run)
                try:
                    await self._mcp_server.run(ObservedReceiveStream(read_stream, observer), write_stream,
                                               self._mcp_server.create_initialization_options())
                finally:
                    group.cancel_scope.cancel()
                    # Finish an in-flight pulse before disconnect, so a late heartbeat cannot reopen it.
                    with anyio.CancelScope(shield=True):
                        await anyio.to_thread.run_sync(observer.pulse, 'disconnected')
