"""Planning HTTP contract: agent data actions stay separate from human revalidation and OS file-manager actions."""

import asyncio
from urllib.parse import quote

from fastapi import Query, Request
from fastapi.responses import Response

from .documents import render_markdown


def register(app, store, object_body):
    """Add fixed, bounded routes without granting agents merge, filesystem browsing or task revalidation authority."""
    def actor(request):
        return 'agent' if request.url.path.startswith('/api/v1/agent/') else 'user'

    @app.post('/api/v1/plans', status_code=201)
    @app.post('/api/v1/agent/plans', status_code=201)
    async def create_plan(request: Request):
        """Save an RnD parent and Markdown plan atomically; creating children remains a separate requested operation."""
        value = await object_body(request, {'title', 'markdown', 'spaceId', 'repoId', 'estimatedComplexity', 'priority', 'assignee', 'dueDate', 'externalRefs', 'dor', 'dod', 'parentId', 'intendedRole', 'codeTask', 'languages', 'systems', 'projectKey', 'projectName', 'projectPrefix'}, {'title', 'markdown'})
        return await asyncio.to_thread(store.planning.create_plan, value, actor(request))

    @app.post('/api/v1/markdown/render')
    async def preview(request: Request):
        """Render inert Markdown through the same bounded parser used for attachments."""
        value = await object_body(request, {'content'}, {'content'})
        return {'html': await asyncio.to_thread(render_markdown, store.documents.validate_content(value['content']))}

    @app.get('/api/v1/issues/{identifier}/documents')
    def documents(identifier: str):
        """List document chips, restoring task content only when detail is actually requested."""
        with store.db.lock:
            store._row(identifier)
            return {'items': store.documents.metadata(identifier)}

    @app.post('/api/v1/issues/{identifier}/documents', status_code=201)
    @app.post('/api/v1/agent/issues/{identifier}/documents', status_code=201)
    async def save_document(identifier: str, request: Request):
        """Create/update one Markdown document; expectedRevision prevents overwriting a newer agent plan."""
        value = await object_body(request, {'name', 'content', 'purpose', 'expectedRevision'}, {'name', 'content'})
        return await asyncio.to_thread(store.documents.save, identifier, value, actor(request))

    @app.get('/api/v1/issues/{identifier}/documents/{document_id}')
    def read_document(identifier: str, document_id: str):
        """Read exact Markdown source, safe HTML and revision metadata."""
        return store.documents.get(identifier, document_id)

    @app.get('/api/v1/issues/{identifier}/documents/{document_id}/download')
    def download(identifier: str, document_id: str):
        """Download task-owned Markdown only; filenames never select a filesystem path."""
        value = store.documents.get(identifier, document_id)
        return Response(value['content'], media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(value['name'], safe='')})

    @app.post('/api/v1/issues/{identifier}/open-folder')
    async def open_folder(identifier: str, request: Request):
        """A guarded human click exports files and opens only this task's owned directory."""
        await object_body(request, set())
        return await asyncio.to_thread(store.documents.open_folder, identifier)

    @app.post('/api/v1/issues/{identifier}/revalidate')
    async def revalidate(identifier: str, request: Request):
        """Explicitly revalidate stale work from the human UI, resetting old readiness evidence."""
        return await asyncio.to_thread(store.planning.revalidate, identifier, await object_body(request, {'reason'}, {'reason'}))

    @app.post('/api/v1/issues/{identifier}/actual-complexity')
    @app.post('/api/v1/agent/issues/{identifier}/actual-complexity')
    async def actual_complexity(identifier: str, request: Request):
        """Store attributed post-implementation points and their basis for a current result generation."""
        return await asyncio.to_thread(store.planning.record_actual, identifier, await object_body(request, {'points', 'reason'}, {'points', 'reason'}), actor(request))

    @app.get('/api/v1/issues/{identifier}/problems')
    def problems(identifier: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        """Read paged blockers/lessons for follow-up planning without altering issue execution."""
        return store.planning.problems(identifier, limit, offset)

    @app.post('/api/v1/issues/{identifier}/problems', status_code=201)
    @app.post('/api/v1/agent/issues/{identifier}/problems', status_code=201)
    async def create_problem(identifier: str, request: Request):
        """Record an attributed difficulty separately from general discussion/comments."""
        value = await object_body(request, {'title', 'severity', 'state', 'details', 'resolution'}, {'title'})
        return await asyncio.to_thread(store.planning.problem, identifier, value, actor(request))

    @app.patch('/api/v1/issues/{identifier}/problems/{problem_id}')
    @app.patch('/api/v1/agent/issues/{identifier}/problems/{problem_id}')
    async def update_problem(identifier: str, problem_id: str, request: Request):
        """Resolve findings with explicit resolution text; earlier states remain in the task audit."""
        value = await object_body(request, {'title', 'severity', 'state', 'details', 'resolution'})
        return await asyncio.to_thread(store.planning.problem, identifier, value, actor(request), problem_id)

    @app.post('/api/v1/issues/{identifier}/storage/restore')
    @app.post('/api/v1/agent/issues/{identifier}/storage/restore')
    async def restore_content(identifier: str, request: Request):
        """Restore ZIP content only; this neither reopens stale/archived work nor grants execution authority."""
        await object_body(request, set())
        return await asyncio.to_thread(store.get, identifier)
