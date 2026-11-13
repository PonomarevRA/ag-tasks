"""Public release metadata and optional, rate-limited hints after completed work."""

from datetime import timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
import time

from desktop_support.updates import check_release

RELEASES_URL = 'https://github.com/PonomarevRA/ag-tasks/releases'
CHECK_INTERVAL = timedelta(hours=12).total_seconds()
RETRY_INTERVAL = timedelta(hours=1).total_seconds()
REMINDER_INTERVAL = timedelta(days=3).total_seconds()
MAX_CACHE_AGE = timedelta(days=2).total_seconds()
LOG = logging.getLogger(__name__)


class ReleaseUpdates:
    """One owner per app profile. Network checks never hold the reminder state lock."""

    def __init__(self, data_dir, current_version, *, fetch=check_release, clock=time.time):
        """Store only bounded reminder timestamps outside the task database."""
        self.current_version = current_version
        self.fetch, self.clock = fetch, clock
        self.path = Path(data_dir) / 'release-reminders.json'
        self._state_lock = Lock()
        self._fetch_lock = Lock()
        self._stop = Event()
        self._worker = None
        self._latest = None
        self._checked = None
        self._shown = self._read_reminders()

    def start(self):
        """Start one background metadata check without delaying application startup."""
        if self._worker is None:
            self._worker = Thread(target=self._run, name='ag-tasks-release-check', daemon=True)
            self._worker.start()

    def stop(self):
        """Wake the worker and let a bounded in-flight network operation finish independently."""
        self._stop.set()

    def _run(self):
        """Fetch immediately and then periodically, with a slower retry than normal polling traffic."""
        delay = 0
        while not self._stop.wait(delay):
            try:
                self.check()
                delay = CHECK_INTERVAL
            except (OSError, ValueError):
                delay = RETRY_INTERVAL

    def check(self, *, force=False):
        """Fetch stable public metadata on the caller's worker, serializing manual and background checks."""
        with self._fetch_lock:
            now = self.clock()
            if not force and self._checked is not None and 0 <= now - self._checked < CHECK_INTERVAL:
                return self._latest
            latest = self.fetch('ag-tasks', self.current_version)
            with self._state_lock:
                self._latest, self._checked = latest, self.clock()
            return latest

    def recommendation(self):
        """Claim an eligible release hint from cache only. Failure leaves task completion intact."""
        if not self._state_lock.acquire(blocking=False):
            return None
        try:
            latest, checked = self._latest, self._checked
            now = self.clock()
            if not latest or not latest['available'] or not 0 <= now - checked <= MAX_CACHE_AGE:
                return None
            version = latest['version']
            previous = self._shown.get(version)
            if previous is not None and now - previous < REMINDER_INTERVAL:
                return None
            shown = {**self._shown, version: now}
            shown = dict(sorted(shown.items(), key=lambda item: item[1], reverse=True)[:16])
            self._write_reminders(shown)
            self._shown = shown
            return {'version': version, 'installedVersion': self.current_version,
                    'url': latest['url'], 'changelog': latest['changelog'],
                    'delivery': 'final_response_only'}
        except (OSError, ValueError):
            LOG.debug('Release reminder state unavailable', exc_info=True)
            return None
        finally:
            self._state_lock.release()

    def _read_reminders(self):
        """Ignore missing, corrupt, oversized or linked reminder files."""
        try:
            if self.path.is_symlink():
                return {}
            with self.path.open('rb') as source:
                raw = source.read(4097)
            value = json.loads(raw) if len(raw) <= 4096 else None
            if not isinstance(value, dict) or len(value) > 16:
                return {}
            return {version: timestamp for version, timestamp in value.items()
                    if re.fullmatch(r'v[0-9]{1,6}(?:\.[0-9]{1,6}){2}', version)
                    and type(timestamp) in (int, float) and 0 <= timestamp <= 253402300799
                    and math.isfinite(timestamp)}
        except (OSError, ValueError):
            return {}

    def _write_reminders(self, value):
        """Atomically replace the owner's compact reminder state with a private file."""
        temporary = NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                       prefix='.release-reminder-', delete=False)
        path = Path(temporary.name)
        try:
            with temporary:
                json.dump(value, temporary, separators=(',', ':'))
                temporary.flush()
                os.fsync(temporary.fileno())
            path.replace(self.path)
        finally:
            path.unlink(missing_ok=True)
