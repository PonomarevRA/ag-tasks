"""Thin first-party adapters to the common service domain; no transport owns business rules."""

import asyncio

from fastapi import Query, Request


def register(app, store, object_body):
    """Publish the same command/read contracts to UI and local agent clients."""

    from .codex_sessions import CodexSessions
    codex = CodexSessions()
    from .companion_routes import register as register_companion
    register_companion(app, store)
    from .process_routes import register as register_process
    register_process(app, store)

    @app.get('/api/v1/codex-sessions')
    def codex_sessions():
        """Read live local writers and explicit lifecycle evidence without importing chat history."""
        return codex.snapshot()

    @app.get('/api/v1/service-data')
    def capabilities():
        """Discover universal commands, observation limits and explicitly unavailable runtime operations."""
        return store.service.capabilities()

    @app.get('/api/v1/project-trees/{root_id}')
    def project_tree(root_id: str):
        """Read exact metadata-only membership for a requested migration, without restoring cold bodies."""
        return store.service.project_migration.inspect(root_id)

    @app.post('/api/v1/commands')
    @app.post('/api/v1/agent/commands')
    async def command(request: Request):
        """Apply one domain command off the event loop; actor comes from the guarded route, never JSON."""
        value = await object_body(request, {'clientId', 'commandId', 'operation', 'payload'},
                                  {'clientId', 'commandId', 'operation', 'payload'})
        actor = 'agent' if request.url.path.startswith('/api/v1/agent/') else 'user'
        return await asyncio.to_thread(store.service.execute, value, actor)

    @app.get('/api/v1/commands/{client_id}/{command_id}')
    def outcome(client_id: str, command_id: str):
        """Read the committed command answer after ACK loss without replaying any effect."""
        return store.service.commands.outcome(client_id, command_id)

    @app.post('/api/v1/agent/sessions/{session_id}/presence')
    async def presence(session_id: str, request: Request):
        """Refresh a process-local connection lease without appending durable heartbeat events."""
        value = await object_body(request, {'connectionId', 'state'}, {'connectionId', 'state'})
        return await asyncio.to_thread(store.service.sessions.touch_presence, session_id, value)

    @app.get('/api/v1/sessions')
    def sessions(limit: int = Query(20, ge=1, le=50), after: str = Query('0', max_length=1024),
                 issueId: str | None = None, activeOnly: bool = False, sort: str = 'newest'):
        """Read a bounded activity-ordered session page; after is its filter-bound continuation token."""
        return store.service.sessions.list(limit, after, issueId, activeOnly, sort)

    @app.get('/api/v1/events')
    def service_events(limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0)):
        """Read transport-independent committed/rejected command facts, never raw credentials or instructions."""
        return store.service.commands.events(limit, after)

    @app.get('/api/v1/sessions/{session_id}')
    def session(session_id: str):
        """Read observations and projections; this route cannot acquire process ownership."""
        return store.service.sessions.get(session_id)

    @app.get('/api/v1/sessions/{session_id}/events')
    def events(session_id: str, limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0)):
        """Page the durable event journal with explicit redaction and replay provenance."""
        return store.service.sessions.events(session_id, limit, after)

    @app.get('/api/v1/queues')
    def queues(limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0)):
        """Page protocol-neutral lanes without claiming work or changing issue workflow."""
        return store.service.queue.list(limit, after)

    @app.get('/api/v1/queues/{lane_id}')
    def queue(lane_id: str):
        """Read current lane revision, profile and reserved attempt, including uncertain ownership."""
        return store.service.queue.get(lane_id)

    @app.get('/api/v1/queues/{lane_id}/items')
    def queue_items(lane_id: str, limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0)):
        """Page durable queue history by creation cursor; order is separately represented by position."""
        return store.service.queue.items(lane_id, limit, after)

    @app.get('/api/v1/queues/{lane_id}/pending')
    def pending(lane_id: str, limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0)):
        """Read bounded waiting items in dispatch order; refresh from the start after a reorder."""
        return store.service.queue.pending(lane_id, limit, after)

    @app.get('/api/v1/queues/{lane_id}/attempts')
    def attempts(lane_id: str, limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0)):
        """Page attempts independently of model turns and task work-session accounting."""
        return store.service.queue.attempts(lane_id, limit, after)

    @app.get('/api/v1/queue-items/{item_id}')
    def queue_item(item_id: str):
        """Read a cancellable intent's latest CAS revision without changing its position."""
        return store.service.queue.item(item_id)

    @app.get('/api/v1/attempts/{attempt_id}')
    def attempt(attempt_id: str):
        """Read the exact runtime binding; observing an attempt grants no dispatch authority."""
        return store.service.queue.attempt(attempt_id)
