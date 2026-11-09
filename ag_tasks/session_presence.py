"""Process-local connection leases; presence never measures model turns or task effort."""

from threading import Lock
import time

from .issues import DomainError, timestamp


class SessionPresence:
    """Keep bounded, volatile heartbeat state separate from the durable event archive."""

    def __init__(self, *, clock=time.monotonic, lease_seconds=90, capacity=1024):
        """Use monotonic age so wall-clock corrections cannot prolong a connection lease."""
        self.clock, self.lease_seconds, self.capacity = clock, lease_seconds, capacity
        self.entries, self.lock = {}, Lock()

    def touch(self, session_id, state):
        """Refresh one connection or close it permanently; no heartbeat creates journal events."""
        if state not in ('connected', 'disconnected'):
            raise DomainError('invalid_presence', 'Use connected or disconnected presence.')
        with self.lock:
            now = self.clock()
            existing = self.entries.get(session_id)
            if existing and existing['state'] == 'disconnected':
                return self._snapshot(existing, now)
            if existing is None and len(self.entries) >= self.capacity:
                expired = [key for key, value in self.entries.items()
                           if value['state'] == 'disconnected' or value['expires'] <= now]
                for key in expired:
                    del self.entries[key]
                if len(self.entries) >= self.capacity:
                    raise DomainError('presence_capacity', 'Connection presence capacity reached.', 503)
            entry = {'state': state, 'lastSeenAt': timestamp(), 'expires': now + self.lease_seconds}
            self.entries[session_id] = entry
            return self._snapshot(entry, now)

    def connected_ids(self):
        """Filter the durable session page to connections whose current lease is still confirmed."""
        with self.lock:
            now = self.clock()
            return [key for key, entry in self.entries.items()
                    if entry['state'] == 'connected' and entry['expires'] > now]

    def snapshot(self, session_id):
        """Unknown after owner restart is intentional; an old database row proves no live connection."""
        with self.lock:
            return self._snapshot(self.entries.get(session_id), self.clock())

    def _snapshot(self, entry, now):
        """Expose lease freshness without leaking process-local monotonic timestamps."""
        if entry is None:
            return {'state': 'unknown', 'lastSeenAt': None, 'leaseSeconds': self.lease_seconds}
        state = entry['state']
        if state == 'connected' and entry['expires'] <= now:
            state = 'stale'
        return {'state': state, 'lastSeenAt': entry['lastSeenAt'], 'leaseSeconds': self.lease_seconds}
