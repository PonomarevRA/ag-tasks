"""Bounded cleanup of inactive observed sessions, preserving live ownership and command audit."""

from datetime import datetime, timedelta, timezone
from time import monotonic


class SessionRetention:
    """Expire Tasks-owned session observations after 72 hours without touching source chats."""

    def __init__(self, store, *, clock=monotonic):
        """
        Bind the owner store and monotonic clock used for the MCP reconnect grace.

        Args:
            store: Owning IssueStore; all changes use its transaction and lock.
            clock: Monotonic seconds source, injectable for deterministic tests.
        """
        self.store, self.clock = store, clock
        self.started = clock()
        self.last_error = None

    def sweep(self, *, now=None, limit=100):
        """
        Remove a bounded page of sessions inactive for at least three days.

        Args:
            now: Aware UTC test instant; omitted uses current UTC time.
            limit: Maximum expired candidates inspected in one transaction (1..100).
        Returns:
            IDs actually removed; active MCP/work/queue ownership always wins.
        Raises:
            ValueError: The limit is outside its bounded range.

        Dependent observations are removed atomically. Terminal queue history is
        retained with its optional session link cleared; command outcomes and
        their idempotency keys remain intact. An owner restart gives MCP clients
        90 seconds to renew before unknown old bindings become eligible.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Session sweep limit must be 1..100')
        instant = now or datetime.now(timezone.utc)
        cutoff = (instant - timedelta(days=3)).isoformat()
        removed = []
        with self.store.db.transaction() as connection:
            connected = self.store.service.sessions.presence.connected_ids()
            conditions = ['julianday(s.updated_at)<=julianday(?)']
            args = [cutoff]
            if connected:
                conditions.append('s.id NOT IN (' + ','.join('?' for _ in connected) + ')')
                args.extend(connected)
            # A queue claim stays protected even when quarantined: expiry is not release proof.
            conditions.append('''NOT EXISTS(SELECT 1 FROM agent_queue_items q WHERE q.session_id=s.id
                AND (q.state NOT IN ('completed','cancelled') OR EXISTS(
                    SELECT 1 FROM agent_attempts a WHERE a.item_id=q.id AND a.state!='released')))''')
            conditions.append('''NOT EXISTS(SELECT 1 FROM work_sessions w WHERE w.issue_id=s.issue_id
                AND w.state='running' AND julianday(w.lease_expires_at)>julianday(?))''')
            args.append(instant.isoformat())
            # Do not let temporarily unknown MCP bindings starve other expired candidates.
            if self.clock() - self.started < 90:
                conditions.append("COALESCE(json_extract(s.capabilities,'$.observed.sourceKind'),'')!='mcp_connection'")
            rows = connection.execute('SELECT s.id,s.updated_at FROM service_sessions s WHERE ' +
                                      ' AND '.join(conditions) + ' ORDER BY julianday(s.updated_at),s.id LIMIT ?',
                                      (*args, limit)).fetchall()
            for row in rows:
                # SQLite's Julian timestamp comparison rounds sub-millisecond boundaries.
                # Recheck the exact aware timestamp before any destructive statement.
                if datetime.fromisoformat(row['updated_at']) > instant - timedelta(days=3):
                    continue
                session_id = row['id']
                connection.execute('UPDATE agent_queue_items SET session_id=NULL WHERE session_id=?', (session_id,))
                for table in ('service_entities', 'service_requests', 'service_streams', 'service_events'):
                    connection.execute(f'DELETE FROM {table} WHERE session_id=?', (session_id,))
                connection.execute('DELETE FROM service_sessions WHERE id=?', (session_id,))
                removed.append(session_id)
        return removed
