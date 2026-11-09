"""Bounded presence/election data for one shared macOS menu; no service payloads or commands."""
from dataclasses import dataclass

CATALOG = {
    'code-atlas': ('local.code-atlas.desktop', 'Code Atlas'),
    'ag-design': ('local.ag-design.desktop', 'Ag Design'),
    'ag-observer': ('local.ag-observer.desktop', 'Ag Observer'),
    'ag-tasks': ('local.ag-tasks.desktop', 'Ag Tasks'),
    'agmemory': ('com.ponomarev.agmemory', 'AgMemory'),
    'atlas-browser': ('com.code-atlas.browser', 'Atlas Browser'),
}
NAMESPACE = 'com.ag-systems.tray.v1'
EXPIRY_SECONDS = 7.0
MAX_PEERS = 32


@dataclass(frozen=True)
class Peer:
    """One OS-verified GUI process; readiness means it owns an actual native status item."""
    app_id: str
    pid: int
    ready: bool
    seen: float


def decode_presence(value, catalog=CATALOG):
    """Accept only bounded fixed-identity wire fields; no JSON, paths or arbitrary metadata."""
    if not isinstance(value, str) or len(value) > 160:
        return None
    fields = value.split('|')
    if len(fields) != 4 or fields[0] != '1' or fields[1] not in catalog or fields[3] not in {'0', '1'}:
        return None
    if not fields[2].isascii() or not fields[2].isdigit() or not 1 <= len(fields[2]) <= 10:
        return None
    pid = int(fields[2])
    if not 1 <= pid <= 2147483647:
        return None
    return fields[1], pid, fields[3] == '1'


class PeerGroup:
    """Small process-local presence table; the smallest live GUI PID is the preferred owner."""
    def __init__(self, app_id, pid, verify, *, catalog=CATALOG):
        """Inject an OS identity check; receipt of a notification alone never proves identity."""
        if app_id not in catalog:
            raise ValueError('Unknown desktop app')
        self.app_id, self.pid, self.verify, self.catalog = app_id, pid, verify, catalog
        self.peers = {}

    def receive(self, value, now):
        """Retain at most32 validated peers, ignoring a peer trying to overwrite our own readiness."""
        decoded = decode_presence(value, self.catalog)
        if decoded is None:
            return False
        app_id, pid, ready = decoded
        if pid == self.pid or not self.verify(app_id, pid):
            return False
        self.prune(now, verify=False)
        if pid not in self.peers and len(self.peers) >= MAX_PEERS:
            return False
        self.peers[pid] = Peer(app_id, pid, ready, now)
        return True

    def prune(self, now, *, verify=True):
        """Drop expired/dead/reused PID records before election or native menu creation."""
        for pid, peer in list(self.peers.items()):
            if now - peer.seen >= EXPIRY_SECONDS or (verify and not self.verify(peer.app_id, pid)):
                del self.peers[pid]

    def leader(self, now):
        """Prefer the lowest live PID; followers keep a fallback item until that owner reports ready."""
        self.prune(now)
        return min([self.pid, *self.peers])

    def ready_leader(self, now):
        """Require fresh readiness from the elected process; heartbeat absence causes local recovery."""
        peer = self.peers.get(self.leader(now))
        return peer is not None and peer.ready

    def wire(self, ready):
        """Publish only version, application identity, PID and native-item readiness."""
        return f'1|{self.app_id}|{self.pid}|{int(ready)}'


class EventBudget:
    """Bound native callback work per interval before parsing or querying OS identities."""
    def __init__(self, limit, interval):
        """Use the caller's monotonic clock, keeping no history or unbounded queue."""
        self.limit, self.interval = limit, interval
        self.start, self.used = None, 0

    def allow(self, now):
        """Drop excess messages; a later heartbeat naturally restores a valid participant."""
        if self.start is None or now - self.start >= self.interval:
            self.start, self.used = now, 0
        if self.used >= self.limit:
            return False
        self.used += 1
        return True
