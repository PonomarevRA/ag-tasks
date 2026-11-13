"""HTTP adapters for explicit work and review; fixed routes, not JSON fields, select caller authority."""

import asyncio

from fastapi import Query, Request
from fastapi.responses import JSONResponse


def register(app, store, object_body):
    """Register graph before the generic issue-ID reader and keep lifecycle mutations off the event loop."""

    def actor(request):
        """Agent identity is declared provenance; only the UI route keeps human final-action capability."""
        return 'agent' if request.url.path.startswith('/api/v1/agent/') else 'user'

    @app.get('/api/v1/problems/digest')
    def problem_digest(limit: int = Query(default=20,ge=1,le=100),offset: int = Query(default=0,ge=0),spaceId: str | None = None):
        """Read retained problem evidence across active/completed tasks without opening ZIP or invoking a model."""
        from .problem_digest import read
        return read(store,limit=limit,offset=offset,space_id=spaceId)

    @app.get('/api/v1/issues/status-audit')
    def status_audit(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), spaceId: str | None = None):
        """Read bounded next-gate evidence, including comment-only candidates, without rewriting tasks."""
        from .lifecycle import audit
        return audit(store, limit=limit, offset=offset, space_id=spaceId)

    @app.get('/api/v1/issues/reviews')
    def review_inbox(limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0), spaceId: str | None = None):
        """Expose actionable current reviews independently of the ordinary history filter."""
        return store.reviews.list(space_id=spaceId, limit=limit, offset=offset)

    @app.post('/api/v1/issues/{identifier}/review-assignment')
    async def assign_review(identifier: str, request: Request):
        """An explicit user assignment may enqueue once, but never launches a process."""
        value = await object_body(request, {'reviewerId', 'laneId'}, {'reviewerId'})
        return await asyncio.to_thread(store.reviews.assign, identifier, value['reviewerId'], value.get('laneId'))

    @app.post('/api/v1/issues/{identifier}/review-assignment/release')
    async def release_review(identifier: str, request: Request):
        """Release a current inactive assignment; running/unknown workers require reconciliation."""
        value = await object_body(request, {'expectedAssignedAt'}, {'expectedAssignedAt'})
        return await asyncio.to_thread(store.reviews.release, identifier, value['expectedAssignedAt'])

    @app.post('/api/v1/spaces/{identifier}/review-route')
    async def configure_review_route(identifier: str, request: Request):
        """Set a declared reviewer and optional existing worker lane for future results."""
        value = await object_body(request, {'reviewerId', 'laneId'}, {'reviewerId'})
        return await asyncio.to_thread(store.reviews.configure, identifier, value['reviewerId'], value.get('laneId'))

    @app.get('/api/v1/spaces')
    def spaces():
        """Read organizational spaces without granting repository or merge authority."""
        return {'items': store.spaces.list()}

    @app.post('/api/v1/spaces', status_code=201)
    async def create_space(request: Request):
        """Human UI creates a named space under the existing same-origin action token."""
        return await asyncio.to_thread(store.spaces.write, await object_body(request, {'name', 'prefix'}, {'name'}))

    @app.patch('/api/v1/spaces/{identifier}')
    async def rename_space(identifier: str, request: Request):
        """Rename metadata only; IDs, task ownership and repository registration do not change."""
        return await asyncio.to_thread(store.spaces.write, await object_body(request, {'name', 'prefix'}, {'name'}), identifier)

    @app.get('/api/v1/issues/graph')
    def graph(spaceId: str = 'default', limit: int = Query(default=200, ge=1, le=200), activeOnly: bool = False):
        """Read real dependencies, optionally selecting live work before the page limit."""
        return JSONResponse(store.spaces.graph(spaceId, limit, active_only=activeOnly))

    @app.get('/api/v1/issues/{identifier}/execution-diagnostics')
    def execution_diagnostics(identifier: str, limit: int = Query(default=20, ge=1, le=100),
                              before: int | None = Query(default=None, ge=1, le=2**63-1)):
        """Read bounded, committed diagnostics for the producer and its generated reviews."""
        from .execution_diagnostics import read
        return read(store, identifier, limit=limit, before=before)

    @app.get('/api/v1/issues/{identifier}/work/history')
    def work_history(identifier: str, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        """Read captured session roles and durations; history never starts or renews work."""
        return store.execution.history(identifier, limit, offset)

    @app.post('/api/v1/issues/{identifier}/work/start')
    @app.post('/api/v1/agent/issues/{identifier}/work/start')
    async def start(identifier: str, request: Request):
        """Start only explicit leased work after role/readiness/blocker admission."""
        value = await object_body(request, {'agentId', 'sessionKey'}, {'agentId', 'sessionKey'})
        return await asyncio.to_thread(store.execution.start, identifier, value, actor(request))

    @app.post('/api/v1/issues/{identifier}/work/heartbeat')
    @app.post('/api/v1/agent/issues/{identifier}/work/heartbeat')
    async def heartbeat(identifier: str, request: Request):
        """Only an explicit agent/runner renews a lease; browser polling cannot fabricate work."""
        fields = {'agentId', 'sessionId', 'sessionKey'}
        return await asyncio.to_thread(store.execution.heartbeat, identifier, await object_body(request, fields, fields), actor(request))

    @app.post('/api/v1/issues/{identifier}/work/stop')
    @app.post('/api/v1/agent/issues/{identifier}/work/stop')
    async def stop(identifier: str, request: Request):
        """Stop one matching real session, preserving retry idempotence."""
        fields = {'agentId', 'sessionId', 'sessionKey'}
        value = await object_body(request, fields | {'reason', 'nextAction'}, fields)
        return await asyncio.to_thread(store.execution.stop, identifier, value, actor(request))

    @app.post('/api/v1/issues/{identifier}/results')
    @app.post('/api/v1/agent/issues/{identifier}/results')
    async def submit_result(identifier: str, request: Request):
        """Persist structured results and current critic admission, never implicit human completion."""
        fields = {'agentId', 'sessionId', 'sessionKey', 'result'}
        return await asyncio.to_thread(store.results.submit, identifier, await object_body(request, fields, fields), actor(request))

    @app.post('/api/v1/issues/{identifier}/review-report')
    @app.post('/api/v1/agent/issues/{identifier}/review-report')
    async def report(identifier: str, request: Request):
        """Record a distinct declared critic's exact-generation report without granting merge approval."""
        fields = {'agentId', 'sessionId', 'sessionKey', 'generation', 'definitionRevision', 'reviewedHead', 'verdict', 'summary', 'findings', 'evidence'}
        value = await object_body(request, fields, fields)
        source = actor(request)
        result = await asyncio.to_thread(store.results.report, identifier, value, source)
        if source == 'agent' and value['verdict'] == 'pass' and result['status'] == 'done':
            recommendation = await asyncio.to_thread(app.state.release_updates.recommendation)
            if recommendation:
                result['updateRecommendation'] = recommendation
        return result

    @app.post('/api/v1/issues/{identifier}/decompose')
    @app.post('/api/v1/agent/issues/{identifier}/decompose')
    async def decompose(identifier: str, request: Request):
        """Create only bounded child metadata; planning cannot start timers or mutate Git."""
        return await asyncio.to_thread(store.spaces.decompose, identifier, await object_body(request, {'children'}, {'children'}), actor(request))

    @app.post('/api/v1/issues/{identifier}/archive')
    async def archive(identifier: str, request: Request):
        """Human UI archives terminal tasks; no agent endpoint exposes this action."""
        await object_body(request, set())
        return await asyncio.to_thread(store.spaces.archive, identifier)

    @app.post('/api/v1/issues/{identifier}/restore')
    async def restore(identifier: str, request: Request):
        """Human UI restores retained state without touching an existing Git path."""
        await object_body(request, set())
        return await asyncio.to_thread(store.spaces.restore, identifier)

    @app.delete('/api/v1/issues/{identifier}')
    async def delete(identifier: str):
        """Human UI creates an audit tombstone for an archived terminal leaf only."""
        return await asyncio.to_thread(store.spaces.delete, identifier)
