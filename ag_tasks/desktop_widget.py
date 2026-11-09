"""Bounded process-local HTTP operation metadata, never task bodies or credentials."""

from collections import deque
import json
from datetime import datetime, timezone
import threading
import time


def timestamp():
    """Capture UTC completion/snapshot instants without inferring event ordering from wall time."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def bounded_snapshot(result):
    """Keep actual serialized UTF-8 below the native client's cap, dropping oldest rows only."""
    rows = result['requests']
    original_count = len(rows)
    while len(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > 60 * 1024:
        if not rows:
            raise ValueError('desktop_snapshot_metadata_exceeds_budget')
        rows.pop()
        if len(rows) == original_count - 1:
            result['source'] += ' Older rows omitted to keep the snapshot within60KiB.'
    return result


class RecentOperations:
    """Retain at most 32 completed requests; snapshot exposes the newest eight and never reads files."""

    def __init__(self):
        """Keep the queue in memory; restarting intentionally loses this limited metadata history."""
        self.rows = deque(maxlen=32)
        self.lock = threading.Lock()

    def record(self, request, started, status_code=None):
        """Record only a matched static API route and actual response status/duration, without input bodies."""
        try:
            route = getattr(request.scope.get('route'), 'path', '')
            if not route.startswith('/api/v1/') or route in {'/api/v1/status', '/api/v1/ui-session'}:
                return
            operation = (request.method + ' ' + route)[:512]
            row = {'timestamp': timestamp(), 'operation': operation,
                   'actorLabel': 'Agent API client (unidentified)' if route.startswith('/api/v1/agent/') else 'HTTP client (unidentified)',
                   'status': 'succeeded' if status_code is not None and status_code < 400 else 'failed',
                   'durationMs': max(0, (time.monotonic() - started) * 1000), 'requestSummary': operation,
                   'responseSummary': 'HTTP ' + str(status_code) if status_code is not None else 'Request raised an exception; response not observed'}
            with self.lock:
                self.rows.appendleft(row)
        except Exception:
            # Optional observability cannot change a request result or recursively log failures.
            return

    def snapshot(self, origin, version):
        """Expose bounded real metadata, explicitly distinguishing unknown clients and empty history."""
        with self.lock:
            rows = list(self.rows)[:8]
        source = 'Process-local recent HTTP operation metadata; request/response bodies and query values are not captured. Clients are unidentified; agent API route is not authenticated agent identity.'
        if not rows:
            source += ' No completed operations captured since startup.'
        result = {'schemaVersion': 'ag-desktop/widget-v1', 'appId': 'ag-tasks', 'applicationVersion': version,
                'capturedAt': timestamp(), 'origin': origin, 'status': 'ok', 'requests': rows, 'source': source}
        return bounded_snapshot(result)
