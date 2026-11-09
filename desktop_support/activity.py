"""Volatile request counts for the desktop indicator; never retain paths, headers or payloads."""
from threading import Lock
from time import monotonic


class RequestActivity:
    """Count concurrent observed API requests and retain only the most recent outcome."""
    def __init__(self, app_id, clock=monotonic):
        """Bind a fixed product identity and injectable monotonic seconds clock."""
        self.app_id, self.clock = app_id, clock
        self.lock = Lock()
        self.active = 0
        self.finished = None
        self.failed = False

    def begin(self):
        """Increment before dispatch; no user-controlled request data enters the tracker."""
        with self.lock:
            self.active += 1

    def end(self, failed):
        """Release exactly one request even on cancellation; remember an outcome for fifteen seconds."""
        with self.lock:
            self.active = max(0, self.active - 1)
            self.finished, self.failed = self.clock(), failed

    def snapshot(self):
        """Return only fixed enums and counts, with no identifiers, summaries or disk access."""
        with self.lock:
            state = 'idle'
            if self.active:
                state = 'busy'
            elif self.finished is not None and self.clock() - self.finished < 15:
                state = 'error' if self.failed else 'success'
            return {'schemaVersion': 'ag-desktop/activity-v1', 'appId': self.app_id,
                    'state': state, 'activeRequests': min(self.active, 2147483647),
                    'coverage': 'observed-api-requests; client identity unverified'}


class ActivityMiddleware:
    """Observe API request lifetime without reading bodies or adding response buffering."""
    def __init__(self, app, tracker):
        """Wrap the application with one process-local tracker, inside its existing HTTP boundary."""
        self.app, self.tracker = app, tracker

    async def __call__(self, scope, receive, send):
        """Preserve exceptions and cancellation, while excluding telemetry/status/stream polling."""
        path = scope.get('path', '')
        excluded = ('/api/desktop/', '/api/agent-activity', '/api/technical-logs',
                    '/api/telemetry', '/api/v1/status', '/api/v1/ui-session')
        observe = (scope['type'] == 'http' and path.startswith('/api/')
                   and not path.startswith(excluded) and not path.endswith(('/events', '/stream')))
        if not observe:
            await self.app(scope, receive, send)
            return
        status = 500

        async def observed_send(message):
            """Capture only the HTTP status code; forward the original ASGI message unchanged."""
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)

        self.tracker.begin()
        failed = True
        try:
            await self.app(scope, receive, observed_send)
            failed = status >= 400
        finally:
            self.tracker.end(failed)


def install_activity(app, app_id):
    """Register bounded read-only telemetry within the host's existing local HTTP protections."""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from urllib.parse import urlsplit
    import ipaddress
    tracker = RequestActivity(app_id)
    app.add_middleware(ActivityMiddleware, tracker=tracker)

    @app.get('/api/desktop/v1/activity')
    def desktop_activity(request: Request):
        """Reject remote/cross-origin callers and query parameters before exposing safe counts."""
        try:
            host = urlsplit('http://' + request.headers.get('host', '')).hostname
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local, host = False, None
        origin = str(request.base_url).rstrip('/')
        allowed = (local and host in {'127.0.0.1', 'localhost', '::1'}
                   and request.headers.get('origin', origin) == origin
                   and request.headers.get('sec-fetch-site') != 'cross-site')
        if not allowed or request.query_params:
            return JSONResponse({'error': 'activity_access_denied'}, status_code=403,
                                headers={'Cache-Control': 'no-store'})
        return JSONResponse(tracker.snapshot(), headers={'Cache-Control': 'no-store'})
    return tracker
