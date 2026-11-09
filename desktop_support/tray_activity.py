"""Bounded native indicator projection; requests remain local and never reach a model."""
import json
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

STATES = {
    'busy': ('Запрос обрабатывается', 'systemBlueColor'),
    'success': ('Запрос завершён недавно', 'systemGreenColor'),
    'error': ('Ошибка запроса', 'systemRedColor'),
    'stale': ('Состояние устарело', 'systemOrangeColor'),
    'idle': ('Нет активных запросов', 'secondaryLabelColor'),
    'unknown': ('Нет телеметрии запросов', 'secondaryLabelColor'),
}
PRIORITY = ('busy', 'error', 'stale', 'success', 'idle', 'unknown')


def decode_activity(value, catalog):
    """Accept only fixed product/PID/state fields; no arbitrary notification text reaches UI."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    fields = value.split('|')
    if len(fields) != 4 or fields[0] != '1' or fields[1] not in catalog or fields[3] not in STATES:
        return None
    if not fields[2].isascii() or not fields[2].isdigit() or len(fields[2]) > 10:
        return None
    pid = int(fields[2])
    return (fields[1], pid, fields[3]) if 0 < pid <= 2147483647 else None


class NoRedirect(HTTPRedirectHandler):
    """A local service must never redirect telemetry to another origin."""
    def redirect_request(self, *args, **kwargs):
        """Refuse every redirect, including a second loopback target."""
        return None


class ActivityPoller:
    """One bounded worker per GUI reads its own exact loopback service; AppKit never waits for HTTP."""
    def __init__(self, app_id, origin):
        """Validate the bound origin and retain only the latest enum plus monotonic receipt time."""
        parsed = urlsplit(origin)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1'}
                or not parsed.port or parsed.username or parsed.password or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment):
            raise ValueError('Indicator requires the actual numeric loopback origin')
        self.app_id, self.origin = app_id, origin.rstrip('/')
        self.lock, self.stopping = Lock(), Event()
        self.latest = ('unknown', monotonic())
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.worker = Thread(target=self.run, name='desktop-activity-indicator', daemon=True)
        self.worker.start()

    def read(self):
        """Expose stale after eight seconds without a fresh bounded response."""
        with self.lock:
            state, received = self.latest
        return state if monotonic() - received < 8 else 'stale'

    def run(self):
        """Poll every two seconds without overlapping requests; keep at most two KiB of response data."""
        while not self.stopping.is_set():
            try:
                with self.opener.open(self.origin + '/api/desktop/v1/activity', timeout=1) as response:
                    body = response.read(2049)
                    if response.status != 200 or len(body) > 2048:
                        raise ValueError('Invalid activity response')
                value = json.loads(body)
                state = value.get('state')
                if (value.get('schemaVersion') != 'ag-desktop/activity-v1'
                        or value.get('appId') != self.app_id or state not in STATES):
                    raise ValueError('Activity identity mismatch')
            except HTTPError as error:
                state = 'unknown' if error.code == 404 else None
            except Exception:
                state = None
            if state is not None:
                with self.lock:
                    self.latest = (state, monotonic())
            self.stopping.wait(2)

    def close(self):
        """Signal cancellation without joining a socket worker on the native main thread."""
        self.stopping.set()
