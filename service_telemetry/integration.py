"""Compose independent journals and capture bounded HTTP metadata without polling feedback."""

import asyncio
import json
import logging
import time
from urllib.parse import parse_qs
from uuid import uuid4

from .agent_activity.service import ActivityJournal
from .agent_activity.http_api import register_http_activity_routes
from .technical_logs.service import TechnicalJournal, TechnicalHandler
from .technical_logs.http_api import register_technical_routes
from .technical_logs.metrics import register_diagnostics_routes
from .technical_logs.events import safe_text, utc_now

CAPTURE_BYTES = 8192
POLL_PATHS = {'/api/v1/status', '/api/v1/ui-session'}
SENSITIVE = ('password', 'secret', 'token', 'authorization', 'cookie', 'credential', 'api_key', 'apikey')


def summary(raw):
    """Project a bounded JSON prefix; secrets and nested bodies are omitted, never repr'd."""
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return {'summary': '[body omitted]', 'truncated': bool(raw)}
    result = {}
    if isinstance(value, dict):
        for name, item in list(value.items())[:12]:
            if any(word in name.lower() for word in SENSITIVE):
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
        # Keep existing root level; error capture must not turn httpx polling into INFO feedback.
        handler = TechnicalHandler(self.technical)
        logger = logging.getLogger()
        logger.addHandler(handler)
        self.technical.handlers.append((logger, handler))

    async def close(self):
        """Drain both bounded writers concurrently off the UI loop, retaining each owned lock."""
        await asyncio.gather(asyncio.to_thread(self.activity.close), asyncio.to_thread(self.technical.close))

    def record(self, scope, request_body, response_body, status, duration):
        """Emit one completed operation and correlated technical record with safe route summaries."""
        route = getattr(scope.get('route'), 'path', '/api/v1/unmatched')
        operation = f"{scope['method']} {route}"[:128]
        operation_id = uuid4().hex
        request = summary(request_body) if request_body else {'summary': operation, 'truncated': False}
        # Query values can contain private input; keep only a bounded safe allowlist of useful filters.
        query = parse_qs(scope.get('query_string', b'')[:4096].decode('utf-8', 'replace'))
        filters = {}
        for key in ('query', 'q', 'category', 'tag', 'component_id', 'status', 'spaceId', 'taskId', 'limit'):
            if key in query:
                filters[key] = safe_text(query[key][0], 128)
        if filters:
            request = summary(json.dumps(filters).encode())
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
        record.operationId, record.sessionId = operation_id, self.session
        record.properties = {'durationMs': duration, 'statusCode': status}
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

        try:
            await self.app(scope, capture_receive, capture_send)
        finally:
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
