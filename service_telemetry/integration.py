"""Compose independent journals and capture bounded HTTP metadata without polling feedback."""

import asyncio
from contextvars import ContextVar
from datetime import datetime
import json
import logging
import time
from threading import Lock
from urllib.parse import parse_qs
from uuid import uuid4

from .agent_activity.service import ActivityJournal
from .agent_activity.http_api import register_http_activity_routes
from .technical_logs.service import TechnicalJournal, TechnicalHandler
from .technical_logs.http_api import register_technical_routes
from .technical_logs.metrics import register_diagnostics_routes
from .technical_logs.events import log_context, safe_text, utc_now

CAPTURE_BYTES = 8192
POLL_PATHS = {'/api/v1/status', '/api/v1/ui-session'}
SENSITIVE = ('password', 'secret', 'token', 'authorization', 'cookie', 'credential', 'apikey', 'sessionkey', 'connectionid')


request_scope = ContextVar('telemetry_http_scope', default=None)
PRESENCE_ROUTE = '/api/v1/agent/sessions/{session_id}/presence'


def request_diagnostics(**fields):
    """Carry validated product metadata back across the async request's worker boundary."""
    scope = request_scope.get()
    if scope is not None:
        scope.setdefault('execution_diagnostics', {}).update(fields)


def resolved_task(requested, canonical):
    """Attribute the primary path target only after it has resolved in domain storage."""
    scope = request_scope.get()
    if scope and scope.get('path_params', {}).get('identifier') == requested:
        request_diagnostics(taskId=canonical)


def summary(raw):
    """Project a bounded JSON prefix; secrets and nested bodies are omitted, never repr'd."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {'summary': '[body omitted]', 'truncated': bool(raw)}
    result = {}
    if isinstance(value, dict):
        for name, item in list(value.items())[:12]:
            if any(word in name.lower().replace('-', '').replace('_', '') for word in SENSITIVE):
                result[name] = '[REDACTED]'
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[name] = safe_text(item, 128) if isinstance(item, str) else item
            elif isinstance(item, list):
                result[name] = {'count': len(item)}
            elif name == 'error' and isinstance(item, dict):
                result[name] = {'code': safe_text(item.get('code'), 64)}
            else:
                result[name] = '[structured value]'
    elif isinstance(value, list):
        result = {'count': len(value)}
    else:
        result = '[scalar body omitted]'
    text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    return {'summary': text[:512], 'truncated': len(text) > 512}


def project_event(event, _):
    """Restrict HTTP request summaries to the v1 public fields while retaining durable evidence."""
    value = dict(event)
    request = event.get('request')
    if request is not None:
        value['request'] = {name: request[name] for name in ('tool', 'summary') if name in request}
    return value


class Telemetry:
    """Own activity and technical journals under the service data root, independently of consumers."""

    def __init__(self, directory, application):
        """Open quota-limited journal owners; storage failures are exposed as degraded health."""
        self.application = application
        self.activity = ActivityJournal(directory)
        self.technical = TechnicalJournal(directory)
        self.session = uuid4().hex
        self.presence_lock = Lock()
        self.presence_pulses = 0
        # Keep existing root level; error capture must not turn httpx polling into INFO feedback.
        handler = TechnicalHandler(self.technical)
        logger = logging.getLogger()
        logger.addHandler(handler)
        self.technical.handlers.append((logger, handler))

    async def close(self):
        """Drain both bounded writers concurrently off the UI loop, retaining each owned lock."""
        await asyncio.gather(asyncio.to_thread(self.activity.close), asyncio.to_thread(self.technical.close))

    def coverage(self):
        """Measure the observed window of each persisted journal at its current sequence bounds."""
        result = {}
        for name, journal in (('activity', self.activity), ('technical', self.technical)):
            window = {'oldestAt': None, 'latestAt': None, 'coverageSeconds': None}
            result[name] = window
            if journal.disk is None:
                continue
            try:
                with journal.disk.lock:
                    oldest, latest = journal.disk.bounds()
                    if oldest > latest:
                        continue
                    first = journal.disk.read(after=oldest - 1, limit=1)[0]['occurredAt']
                    last = journal.disk.read(before=latest + 1, limit=1)[0]['occurredAt']
                age = int((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds())
                window.update(oldestAt=first, latestAt=last, coverageSeconds=max(0, age))
            except (OSError, ValueError, IndexError, KeyError):
                # A missing or unreadable window is unknown, not evidence of an empty journal.
                continue
        return result

    def record_presence_expired(self, session_id):
        """Record an observed lease transition without inventing an HTTP request or an agent action."""
        record = logging.LogRecord(self.application + '.presence', logging.INFO, '', 0,
                                   'MCP connection presence lease expired', (), None)
        record.eventName = 'connection.presence.expired'
        record.sessionId = session_id
        record.properties = {'presenceState': 'stale'}
        self.technical.submit(record)

    def record(self, scope, request_body, response_body, status, duration):
        """Emit one completed operation and correlated technical record with safe route summaries."""
        route = getattr(scope.get('route'), 'path', '/api/v1/unmatched')
        operation = f"{scope['method']} {route}"[:128]
        operation_id = scope['telemetry_operation_id']
        facts = scope.get('execution_diagnostics', {})
        presence = route == PRESENCE_ROUTE
        if presence and status < 400 and facts.get('presenceWasStale'):
            self.record_presence_expired(facts['sessionId'])
        if presence and status < 400 and facts.get('presenceState') == 'unchanged':
            with self.presence_lock:
                self.presence_pulses += 1
            return
        execution = '/issues/{identifier}/' in route and route.endswith((
            '/work/start', '/work/heartbeat', '/work/stop', '/results', '/review-report',
            '/review-assignment', '/review-assignment/release', '/execution-diagnostics'))
        request = {'summary': operation, 'truncated': False}
        if request_body and not (presence or execution):
            request = summary(request_body)
        # Query values can contain private input; keep only a bounded safe allowlist of useful filters.
        query = parse_qs(scope.get('query_string', b'')[:4096].decode('utf-8', 'replace'))
        filters = {}
        for key in ('query', 'q', 'category', 'tag', 'component_id', 'status', 'spaceId', 'taskId', 'limit'):
            if key in query:
                filters[key] = safe_text(query[key][0], 128)
        if filters and not (presence or execution):
            request = summary(json.dumps(filters).encode())
        if presence or execution:
            error_code = facts.get('errorCode')
            response = {'summary': f'HTTP {status}' + (f' · {error_code}' if error_code else ''), 'truncated': False}
        else:
            response = summary(response_body)
            response['summary'] = f"HTTP {status} · {response['summary']}"[:512]
        event = {'schemaVersion': 'agent-activity/v1', 'application': self.application,
                 'eventId': uuid4().hex, 'occurredAt': utc_now(), 'source': 'http', 'clientKind': 'http',
                 'identityQuality': 'process', 'sessionId': self.session, 'agentId': None,
                 'operationId': operation_id, 'operation': operation, 'phase': 'finished',
                 'outcome': 'failed' if status >= 400 else 'succeeded', 'durationMs': duration,
                 'request': {'tool': operation, **request}, 'response': response}
        self.activity.submit(event)
        record = logging.LogRecord(self.application + '.http', logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO,
                                   '', 0, '%s returned HTTP %s', (operation, status), None)
        record.eventName = 'http.request.completed'
        record.operationId, record.sessionId = operation_id, facts.get('sessionId', self.session)
        record.taskId = facts.get('taskId')
        record.properties = {'durationMs': duration, 'statusCode': status,
                             **{key: facts[key] for key in ('errorCode', 'agentId', 'presenceState') if key in facts}}
        if 'agentId' in facts:
            record.properties['identityQuality'] = 'declared'
        self.technical.submit(record)


class TelemetryMiddleware:
    """Tee bounded request/response prefixes while preserving streaming, errors and cancellation."""

    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        """Observe product API operations only; skip health, static assets and all telemetry polling."""
        path = scope.get('path', '')
        if scope['type'] != 'http' or not path.startswith('/api/v1/') or path in POLL_PATHS:
            return await self.app(scope, receive, send)
        request_body, response_body = bytearray(), bytearray()
        status, started = 500, time.monotonic()

        async def capture_receive():
            """Copy only the prefix the application actually consumes; never pre-drain its stream."""
            message = await receive()
            if message['type'] == 'http.request':
                request_body.extend(message.get('body', b'')[:max(0, CAPTURE_BYTES - len(request_body))])
            return message

        async def capture_send(message):
            """Forward each original message untouched while capturing bounded JSON evidence."""
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            elif message['type'] == 'http.response.body':
                response_body.extend(message.get('body', b'')[:max(0, CAPTURE_BYTES - len(response_body))])
            await send(message)

        scope['telemetry_operation_id'] = operation_id = uuid4().hex
        token = request_scope.set(scope)
        try:
            with log_context(operationId=operation_id):
                await self.app(scope, capture_receive, capture_send)
        finally:
            request_scope.reset(token)
            owner = self.owner()
            if owner is not None:
                # Logging failure may degrade observability, never the application response.
                try:
                    owner.record(scope, bytes(request_body), bytes(response_body), status, (time.monotonic() - started) * 1000)
                except Exception:
                    owner.activity.dropped['invalid_event'] += 1


class DeferredJournal:
    """Resolve the lifespan-owned journal only when a live HTTP endpoint is called."""

    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def __getattr__(self, name):
        return getattr(getattr(self.owner(), self.name), name)


def install_routes(app, owner, directory):
    """Bind portable read routes before static mounting without opening a second storage owner."""
    activity, technical = DeferredJournal(owner, 'activity'), DeferredJournal(owner, 'technical')
    register_http_activity_routes(app, activity, None, project_event)
    register_technical_routes(app, technical)
    register_diagnostics_routes(app, [activity, technical], directory)
