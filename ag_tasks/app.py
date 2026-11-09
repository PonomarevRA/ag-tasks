"""Local HTTP composition with separate agent actions and explicit user approval routes."""

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
from pathlib import Path
import secrets
import re
import sqlite3
import sys
from tempfile import NamedTemporaryFile
import uuid
import time

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from service_telemetry.integration import Telemetry, TelemetryMiddleware, install_routes as install_telemetry_routes

from . import __version__
from .issues import DomainError, IssueStore
from .git_workflow import GitWorkflow
from .desktop_widget import RecentOperations
from .config import STATUSES
from .criteria import ROLES
from .work_analytics import work_analytics
from .execution_analytics import execution_analytics


def default_directory():
    """Keep issues and worktrees in this service's own data directory."""
    from .config import default_data_dir
    return default_data_dir()


def publish_endpoint(endpoint, discovery):
    """Atomically publish a freshly created, mode-0600 discovery file."""
    stream = NamedTemporaryFile(mode="w", encoding="utf-8", dir=endpoint.parent,
                                prefix=".endpoint-", delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(discovery, stream)
        temporary.replace(endpoint)
    finally:
        temporary.unlink(missing_ok=True)


async def object_body(request, allowed, required=()):
    """Accept only documented JSON object fields within the middleware byte budget."""
    try:
        value = await request.json()
    except (ValueError, UnicodeError) as error:
        raise DomainError("invalid_json", "Expected JSON object") from error
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise DomainError("invalid_fields", "Unexpected or missing fields")
    return value


def create_app(data_dir=None, *, origin=None, desktop_automation=None):
    """Compose independent issue storage, read APIs, agent actions and user approval."""
    directory = Path(data_dir) if data_dir is not None else default_directory()
    store = IssueStore(directory)
    workflow = GitWorkflow(store)
    instance_id, ui_token = uuid.uuid4().hex, secrets.token_urlsafe(32)
    recent_operations = RecentOperations()

    @asynccontextmanager
    async def lifespan(_):
        """Publish discovery only for this owned server, then release its database lock."""
        endpoint = directory / "endpoint.json"
        if origin:
            discovery = {"origin": origin, "instanceId": instance_id}
            if desktop_automation is not None:
                discovery["desktopToken"] = desktop_automation.token
            publish_endpoint(endpoint, discovery)
        app.state.telemetry = Telemetry(directory, 'ag-tasks')
        try:
            await asyncio.to_thread(store.service.session_retention.sweep)
        except Exception as error:
            store.service.session_retention.last_error = type(error).__name__
        # Startup catch-up handles tasks that aged while the app was closed.
        try:
            await asyncio.to_thread(store.aging.sweep)
        except Exception as error:
            store.aging.last_error = type(error).__name__
        store.cold.start()
        try:
            yield
        finally:
            if origin:
                endpoint.unlink(missing_ok=True)
            await app.state.telemetry.close()
            store.close()

    app = FastAPI(title="Ag Tasks", version=__version__, lifespan=lifespan)
    from desktop_support.activity import install_activity
    install_activity(app, 'ag-tasks')
    app.state.store, app.state.workflow = store, workflow
    app.state.telemetry = None
    install_telemetry_routes(app, lambda: app.state.telemetry, directory)
    app.add_middleware(TelemetryMiddleware, owner=lambda: app.state.telemetry)

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        """Require local same-origin traffic; isolate UI writes from agent capabilities."""
        try:
            host = request.url.hostname or ""
            peer = request.client.host if request.client else ""
            local_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
            local_peer = peer == "testclient" or ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local_host = local_peer = False
        expected = str(request.base_url).rstrip("/")
        if not local_host or not local_peer or request.headers.get("origin", expected) != expected or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": {"code": "forbidden", "message": "Local same-origin access required"}}, status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 65536:
                    return JSONResponse({"error": {"code": "too_large", "message": "Request exceeds 64 KiB"}}, status_code=413)
            request._body = bytes(content)
            if not request.url.path.startswith("/api/v1/agent/"):
                supplied = request.headers.get("x-ag-tasks-ui", "")
                if not secrets.compare_digest(supplied, ui_token):
                    return JSONResponse({"error": {"code": "ui_action_required", "message": "Use the local UI for this action"}}, status_code=403)
        # Human-readable task aliases resolve to canonical IDs before any route mutation.
        matched = re.match(r'^(/api/v1/(?:agent/)?issues/)([A-Za-z][A-Za-z0-9]{0,31}-[0-9]+)(/.*)?$', request.scope['path'])
        if matched:
            with store.db.lock:
                alias = store.db.connection.execute('SELECT issue_id FROM issue_keys WHERE key=? COLLATE NOCASE', (matched[2],)).fetchone()
            if alias:
                request.scope['path'] = matched[1] + alias[0] + (matched[3] or '')
                request.scope['raw_path'] = request.scope['path'].encode('ascii')
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            recent_operations.record(request, started)
            raise
        recent_operations.record(request, started, response.status_code)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_, error):
        """Return bounded domain messages without raw Git process output or shell arguments."""
        payload = {"code": error.code, "message": error.message}
        if error.details is not None:
            payload['details'] = error.details
        return JSONResponse({"error": payload}, status_code=error.status)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(_, error):
        """Keep storage implementation details out of HTTP failures."""
        return JSONResponse({"error": {"code": "storage_unavailable", "message": "Local issue storage unavailable"}}, status_code=503)

    @app.get("/healthz")
    def health():
        """Expose process liveness independently of Git repository availability."""
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def status():
        """Identify this instance for the standalone MCP bridge."""
        return {"application": "ag-tasks", "version": __version__, "instanceId": instance_id,
                "statuses": list(STATUSES), 'agentContract': {'version':'ag-tasks-agent/v3', 'freshMcpConsole':True, 'sessionRoleHistory':True, 'statusAudit':True, 'pendingReviews':True, 'reviewAssignmentLaunchesAgent':False, 'completionPolicy':'after_independent_critic', 'mergeApproval':'human_exact_revision', 'lifecycleContract':'ag-tasks-lifecycle/v1', 'implementationIncludesDecomposition':True}, 'contractVersion': 'ag-tasks-workflow/v2', 'roles': list(ROLES), 'leaseSeconds': 90, 'planningContract': 'ag-tasks-planning/v1', 'codeReviewContract': 'ag-tasks-code-review/v1', 'serviceDataContract': store.service.capabilities(), 'aging': {'staleAfterDays': 3, 'activityField': 'lastActivityAt', 'maintenanceError': store.aging.last_error}, 'storage': {'coldAfterDays': 7, 'maintenanceError': store.cold.last_error}}

    @app.get("/api/desktop/v1/widget")
    def desktop_widget(request: Request):
        """Expose safe process-local metadata without task bodies or widget-poll feedback."""
        return recent_operations.snapshot(str(request.base_url).rstrip('/'), __version__)

    @app.get("/api/v1/ui-session")
    def ui_session():
        """Issue a same-origin UI action token; this is a local workflow guard, not authentication."""
        return {"token": ui_token}

    from .workflow_routes import register
    register(app, store, object_body)
    from .planning_routes import register as register_planning
    register_planning(app, store, object_body)
    from .service_routes import register as register_service_data
    register_service_data(app, store, object_body)

    @app.get("/api/v1/issues")
    def issues(status: str | None = None, parentId: str | None = None, repoId: str | None = None,
               limit: int = Query(default=200, ge=1, le=200), offset: int = Query(default=0, ge=0), spaceId: str | None = None, includeArchived: bool = False, board: bool = False):
        """Read a bounded issue page, optionally projecting only Kanban card fields."""
        if board:
            from .board import page
            return JSONResponse(page(store, status=status, parent_id=parentId, repo_id=repoId,
                                     space_id=spaceId, include_archived=includeArchived, limit=limit, offset=offset))
        items = store.list(status=status, parent_id=parentId, repo_id=repoId, limit=limit, offset=offset, space_id=spaceId, include_archived=includeArchived)
        next_offset = offset + len(items)
        more = store.has_more(status=status, parent_id=parentId, repo_id=repoId, offset=next_offset, space_id=spaceId, include_archived=includeArchived) if items else False
        # Store output already contains JSON-native values; avoid a second recursive conversion.
        return JSONResponse({"items": items, "hasMore": more, "nextOffset": next_offset if more else None})

    @app.get("/api/v1/issues/{identifier}")
    def issue(identifier: str):
        """Read an issue, evidence links, work branch and append-only history."""
        return JSONResponse(store.get(identifier))

    async def create_issue(request, actor):
        """Share field validation while keeping caller authority fixed by the route."""
        value = await object_body(request, {"title", "description", "status", "priority", "parentId", "assignee", "dueDate", "repoId", "externalRefs", 'spaceId', 'intendedRole', 'dor', 'dod', 'kind', 'estimatedComplexity', 'codeTask', 'languages', 'systems', 'projectKey', 'projectName', 'projectPrefix'}, {"title"})
        return store.create(value, actor=actor)

    @app.post("/api/v1/issues", status_code=201)
    async def user_create(request: Request):
        """Create an issue from the local UI."""
        return await create_issue(request, "user")

    @app.post("/api/v1/agent/issues", status_code=201)
    async def agent_create(request: Request):
        """Create an issue from analysis without granting approval or filesystem authority."""
        return await create_issue(request, "agent")

    async def update_issue(identifier, request, actor):
        """Update documented issue fields under the caller's fixed authority."""
        value = await object_body(request, {"title", "description", "status", "priority", "parentId", "assignee", "dueDate", "repoId", "externalRefs", 'spaceId', 'intendedRole', 'dor', 'dod', 'kind', 'estimatedComplexity', 'codeTask', 'languages', 'systems'})
        return store.update(identifier, value, actor=actor)

    @app.patch("/api/v1/issues/{identifier}")
    async def user_update(identifier: str, request: Request):
        """Edit issue metadata and non-code completion from the UI."""
        return await update_issue(identifier, request, "user")

    @app.patch("/api/v1/agent/issues/{identifier}")
    async def agent_update(identifier: str, request: Request):
        """Progress an issue without bypassing repository review and approval."""
        return await update_issue(identifier, request, "agent")

    @app.post("/api/v1/agent/issues/{identifier}/links")
    @app.post("/api/v1/issues/{identifier}/links")
    async def link(identifier: str, request: Request):
        """Relate issues or record explicit blockers; actor never comes from JSON."""
        value = await object_body(request, {"relatedId", "kind"}, {"relatedId"})
        actor = "agent" if request.url.path.startswith("/api/v1/agent/") else "user"
        return store.link(identifier, value["relatedId"], value.get("kind", "related"), actor=actor)

    @app.post("/api/v1/agent/issues/{identifier}/comments")
    @app.post("/api/v1/issues/{identifier}/comments")
    async def comment(identifier: str, request: Request):
        """Append findings or agent-reported validation as attributed text."""
        value = await object_body(request, {"text"}, {"text"})
        actor = "agent" if request.url.path.startswith("/api/v1/agent/") else "user"
        return store.comment(identifier, value["text"], actor=actor)

    @app.get("/api/v1/repositories")
    def repositories():
        """Read explicitly registered local repository identities."""
        return {"items": store.repos()}

    @app.post("/api/v1/repositories", status_code=201)
    async def register(request: Request):
        """Only the local UI can grant access to a repository path and base branch."""
        value = await object_body(request, {"path", "name", "baseBranch"}, {"path"})
        return await asyncio.to_thread(store.register_repo, value["path"], value.get("name"), value.get("baseBranch"))

    @app.post("/api/v1/agent/issues/{identifier}/prepare")
    @app.post("/api/v1/issues/{identifier}/prepare")
    async def prepare(identifier: str):
        """Create one managed worktree from the repository's committed base."""
        return await asyncio.to_thread(workflow.prepare, identifier)

    @app.get("/api/v1/issues/{identifier}/review")
    async def review(identifier: str):
        """Read current exact revisions and bounded diff without merging anything."""
        return await asyncio.to_thread(workflow.review, identifier)

    @app.post("/api/v1/issues/{identifier}/approve-merge")
    async def approve(identifier: str, request: Request):
        """Merge only the explicitly reviewed base/head pair; no agent route exposes this action."""
        value = await object_body(request, {"expectedHead", "expectedBase"}, {"expectedHead", "expectedBase"})
        return await asyncio.to_thread(workflow.approve_merge, identifier, value["expectedHead"], value["expectedBase"])

    @app.get("/api/v1/analytics/work")
    async def analytics_work(start: str, end: str, spaceId: str = Query("", max_length=64),
                             kind: str = Query("", max_length=32), q: str = Query("", max_length=200),
                             offset: int = Query(0, ge=0)):
        """Read the committed work ledger across spaces without changing task state."""
        return await asyncio.to_thread(work_analytics, store.db, start, end, spaceId, kind, q, offset)

    @app.get("/api/v1/analytics/execution")
    async def analytics_execution(start: str, end: str, spaceId: str = Query("", max_length=64),
                                  kind: str = Query("", max_length=32), q: str = Query("", max_length=200)):
        """Read current execution evidence and recorded UTC events without changing task state."""
        return await asyncio.to_thread(execution_analytics, store.db, start, end, spaceId, kind, q)

    from .manager_routes import register as register_manager
    register_manager(app, store)
    from .desktop_automation import register as register_desktop
    register_desktop(app, desktop_automation)
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "web"
    app.mount("/", StaticFiles(directory=assets, html=True), name="web")
    return app
