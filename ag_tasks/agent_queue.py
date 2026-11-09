"""Protocol-neutral queued task intents and bounded read models for HTTP, MCP and UI."""

import json
from uuid import uuid4

from .issues import DomainError, bounded_text
from .service_values import encode, fields, identifier, page
from .agent_queue_values import MAX_PENDING_ITEMS, attempt_value, expect_revision, item_value, lease_clock, profile

QUEUE_OPERATIONS = ('queue.create', 'queue.configure', 'queue.enqueue', 'queue.move', 'queue.cancel')
ATTEMPT_OPERATIONS = ('attempt.claim', 'attempt.dispatch', 'attempt.heartbeat', 'attempt.report',
                      'attempt.release', 'attempt.reconcile')
OPERATIONS = QUEUE_OPERATIONS + ATTEMPT_OPERATIONS


class QueueCore:
    """Own durable queue rules independently of transports, ACP wire data and issue completion."""

    def __init__(self, store, *, clock=None):
        """Share the command journal's database; inject time only for deterministic lease tests."""
        self.store, self.db = store, store.db
        self._clock_source = clock if clock is not None else lease_clock()
        self._latest_time = None
        from .agent_attempts import Attempts
        self.worker = Attempts(self)

    def clock(self):
        """Keep injected clocks nondecreasing too; observed expiry stays unknown until explicit closure."""
        with self.db.lock:
            now = self._clock_source()
            if self._latest_time is None or now > self._latest_time:
                self._latest_time = now
            return self._latest_time

    def apply(self, operation, payload, actor='agent'):
        """Apply fixed operations inside the caller's atomic effect/outcome transaction."""
        if operation in ATTEMPT_OPERATIONS:
            return self.worker.apply(operation, payload, actor)
        actions = {'queue.create': self.create, 'queue.configure': self.configure, 'queue.enqueue': self.enqueue,
                   'queue.move': self.move, 'queue.cancel': self.cancel}
        action = actions.get(operation)
        if action is None:
            raise DomainError('unsupported_operation', 'Use an advertised queue operation.')
        return action(payload)

    def _lane(self, lane_id, connection):
        """Resolve local lane identity; it never names a directory or executable."""
        identifier(lane_id)
        row = connection.execute('SELECT * FROM agent_lanes WHERE id=?', (lane_id,)).fetchone()
        if row is None:
            raise DomainError('queue_not_found', 'Agent queue not found.', 404)
        return row

    def _item(self, item_id, connection):
        """Resolve immutable queued intent identity independently of the linked issue."""
        identifier(item_id)
        row = connection.execute('SELECT * FROM agent_queue_items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise DomainError('queue_item_not_found', 'Queued intent not found.', 404)
        return row

    def _attempt(self, attempt_id, connection):
        """Resolve a local execution attempt; remote RPC identifiers have a different namespace."""
        identifier(attempt_id)
        row = connection.execute('SELECT * FROM agent_attempts WHERE id=?', (attempt_id,)).fetchone()
        if row is None:
            raise DomainError('attempt_not_found', 'Agent attempt not found.', 404)
        return row

    def _item_value(self, row, connection):
        """Expose uncertain ownership on items even before the next mutating command persists it."""
        result = item_value(row)
        active = connection.execute("SELECT * FROM agent_attempts WHERE item_id=? AND state!='released'", (row['id'],)).fetchone()
        if active and attempt_value(active, self.db.queue_incarnation, self.clock())['state'] == 'unknown':
            result['state'] = 'unknown'
        return result

    def _attempt_value(self, row, connection):
        """Return a bounded attempt with the exact captured input; no command is sent by reading."""
        result = attempt_value(row, self.db.queue_incarnation, self.clock())
        result['item'] = self._item_value(self._item(row['item_id'], connection), connection)
        return result

    def _lane_value(self, row, connection):
        """Keep queue activity separate from drain and the foreground completion of its attempt."""
        attempt = self._attempt(row['active_attempt_id'], connection) if row['active_attempt_id'] else None
        active = self._attempt_value(attempt, connection) if attempt else None
        state = 'ready'
        if active:
            state = 'quarantined' if active['state'] == 'unknown' else 'busy'
        count = connection.execute("SELECT COUNT(*) FROM agent_queue_items WHERE lane_id=? AND state='queued'", (row['id'],)).fetchone()[0]
        return {'id': row['id'], 'laneId': row['id'], 'title': row['title'], 'profile': json.loads(row['profile']),
                'revision': row['revision'], 'draining': bool(row['draining']), 'state': state,
                'activeAttempt': active, 'queuedCount': count, 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}

    def get(self, lane_id):
        """Read metadata and active ownership; pending inputs use their separate bounded page."""
        with self.db.lock:
            return self._lane_value(self._lane(lane_id, self.db.connection), self.db.connection)

    def item(self, item_id):
        """Read one immutable input and its cancellation state without granting dispatch authority."""
        with self.db.lock:
            return self._item_value(self._item(item_id, self.db.connection), self.db.connection)

    def attempt(self, attempt_id):
        """Read the current fenced attempt; a read never extends the worker's lease."""
        with self.db.lock:
            return self._attempt_value(self._attempt(attempt_id, self.db.connection), self.db.connection)

    def list(self, limit=20, after=0):
        """Page lanes by immutable creation number; later changes cannot shift the cursor."""
        page(limit, after)
        with self.db.lock:
            connection = self.db.connection
            rows = connection.execute('SELECT * FROM agent_lanes WHERE number>? ORDER BY number LIMIT ?', (after, limit + 1)).fetchall()
            values = [self._lane_value(row, connection) for row in rows[:limit]]
            return self._page(rows, values, limit, 'number')

    @staticmethod
    def _page(rows, values, limit, cursor):
        """Use one stable shape for all bounded queue read surfaces."""
        more = len(rows) > limit
        return {'items': values, 'hasMore': more, 'nextCursor': rows[limit - 1][cursor] if more else None}

    def items(self, lane_id, limit=20, after=0):
        """Page immutable creation history; use pending() for the current dispatch order."""
        page(limit, after)
        with self.db.lock:
            connection = self.db.connection
            self._lane(lane_id, connection)
            rows = connection.execute('SELECT * FROM agent_queue_items WHERE lane_id=? AND number>? ORDER BY number LIMIT ?', (lane_id, after, limit + 1)).fetchall()
            values = [self._item_value(row, connection) for row in rows[:limit]]
            return self._page(rows, values, limit, 'number')

    def pending(self, lane_id, limit=20, after=0):
        """Page actual dispatch order; reorder invalidates position cursors, so clients reload after edits."""
        page(limit, after)
        with self.db.lock:
            connection = self.db.connection
            self._lane(lane_id, connection)
            rows = connection.execute("SELECT * FROM agent_queue_items WHERE lane_id=? AND state='queued' AND position>? ORDER BY position,number LIMIT ?", (lane_id, after, limit + 1)).fetchall()
            return self._page(rows, [item_value(row) for row in rows[:limit]], limit, 'position')

    def attempts(self, lane_id, limit=20, after=0):
        """Page all attempts with durable creation cursors, including uncertain and released history."""
        page(limit, after)
        with self.db.lock:
            connection = self.db.connection
            self._lane(lane_id, connection)
            rows = connection.execute('SELECT * FROM agent_attempts WHERE lane_id=? AND number>? ORDER BY number LIMIT ?', (lane_id, after, limit + 1)).fetchall()
            values = [self._attempt_value(row, connection) for row in rows[:limit]]
            return self._page(rows, values, limit, 'number')

    def create(self, payload):
        """Create a logical lane with a pinned future-attempt profile; no process is launched."""
        fields(payload, {'laneId', 'title', 'profile'}, {'laneId', 'title', 'profile'})
        lane_id = identifier(payload['laneId'])
        title = bounded_text(payload['title'], 240, empty=False)
        selected = encode(profile(payload['profile']))
        now = self.clock().isoformat()
        with self.db.transaction() as connection:
            if connection.execute('SELECT 1 FROM agent_lanes WHERE id=?', (lane_id,)).fetchone():
                raise DomainError('queue_exists', 'Agent queue identity already exists.', 409)
            connection.execute('INSERT INTO agent_lanes(id,title,profile,created_at,updated_at) VALUES(?,?,?,?,?)', (lane_id, title, selected, now, now))
        return self.get(lane_id)

    def configure(self, payload):
        """Drain future claims or select the next profile; active attempts keep their immutable snapshot."""
        fields(payload, {'laneId', 'expectedRevision', 'draining', 'profile'}, {'laneId', 'expectedRevision'})
        if not {'draining', 'profile'} & payload.keys():
            raise DomainError('invalid_fields', 'Configure at least one queue property.')
        selected = encode(profile(payload['profile'])) if 'profile' in payload else None
        if 'draining' in payload and type(payload['draining']) is not bool:
            raise DomainError('invalid_field', 'Draining must be a boolean.')
        with self.db.transaction() as connection:
            lane = self._lane(payload['laneId'], connection)
            expect_revision(payload['expectedRevision'], lane)
            connection.execute('UPDATE agent_lanes SET profile=?,draining=?,revision=revision+1,updated_at=? WHERE id=?',
                               (selected if selected is not None else lane['profile'], int(payload.get('draining', lane['draining'])), self.clock().isoformat(), lane['id']))
        return self.get(lane['id'])

    def enqueue(self, payload):
        """Persist one bounded task intent, optionally linked to an issue and observed session."""
        fields(payload, {'laneId', 'issueId', 'sessionId', 'text'}, {'laneId', 'text'})
        text = bounded_text(payload['text'], 8192, empty=False)
        item_id, now = uuid4().hex, self.clock().isoformat()
        with self.db.transaction() as connection:
            lane = self._lane(payload['laneId'], connection)
            count = connection.execute("""SELECT COUNT(*) FROM agent_queue_items i WHERE lane_id=? AND
                (state='queued' OR EXISTS(SELECT 1 FROM agent_attempts a WHERE a.item_id=i.id AND a.state!='released'))""", (lane['id'],)).fetchone()[0]
            if count >= MAX_PENDING_ITEMS:
                raise DomainError('queue_capacity', 'A lane accepts at most 1000 unresolved intents.', 409)
            issue_id = self.store._row(payload['issueId'], connection)['id'] if payload.get('issueId') is not None else None
            session_id = None
            if payload.get('sessionId') is not None:
                session_id = self.store.service.sessions.row(payload['sessionId'], connection)['id']
            position = connection.execute('SELECT COALESCE(MAX(position),0)+1 FROM agent_queue_items WHERE lane_id=?', (lane['id'],)).fetchone()[0]
            connection.execute('INSERT INTO agent_queue_items(id,lane_id,issue_id,session_id,text,state,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                               (item_id, lane['id'], issue_id, session_id, text, 'queued', position, now, now))
            self._touch(connection, lane['id'], now)
        return self.item(item_id)

    @staticmethod
    def _touch(connection, lane_id, now):
        """Invalidate lane metadata edits after queue or ownership changes."""
        connection.execute('UPDATE agent_lanes SET revision=revision+1,updated_at=? WHERE id=?', (now, lane_id))

    def move(self, payload):
        """Move only a waiting intent, atomically reindexing the bounded pending queue."""
        fields(payload, {'itemId', 'expectedRevision', 'beforeItemId'}, {'itemId', 'expectedRevision', 'beforeItemId'})
        with self.db.transaction() as connection:
            item = self._item(payload['itemId'], connection)
            expect_revision(payload['expectedRevision'], item)
            if item['state'] != 'queued':
                raise DomainError('queue_item_not_waiting', 'Only a waiting intent can move.', 409)
            before = payload['beforeItemId']
            if before == item['id']:
                raise DomainError('invalid_move', 'Choose another waiting intent or null for the tail.')
            rows = connection.execute("SELECT id,position FROM agent_queue_items WHERE lane_id=? AND state='queued' ORDER BY position,number", (item['lane_id'],)).fetchall()
            order = [row['id'] for row in rows if row['id'] != item['id']]
            if before is not None:
                identifier(before)
                if before not in order:
                    raise DomainError('invalid_move', 'The target must wait in the same lane.', 409)
                order.insert(order.index(before), item['id'])
            else:
                order.append(item['id'])
            now = self.clock().isoformat()
            for position, item_id in enumerate(order, 1):
                connection.execute('UPDATE agent_queue_items SET position=?,revision=revision+1,updated_at=? WHERE id=? AND position!=?', (position, now, item_id, position))
            self._touch(connection, item['lane_id'], now)
        return self.item(item['id'])

    def cancel(self, payload):
        """Cancel queued intent immediately; retain ownership after a worker has claimed it."""
        fields(payload, {'itemId', 'expectedRevision'}, {'itemId', 'expectedRevision'})
        with self.db.transaction() as connection:
            item = self._item(payload['itemId'], connection)
            expect_revision(payload['expectedRevision'], item)
            active = connection.execute("SELECT 1 FROM agent_attempts WHERE item_id=? AND state!='released'", (item['id'],)).fetchone()
            if item['state'] != 'queued' and not active:
                raise DomainError('queue_item_closed', 'The intent is already closed.', 409)
            now = self.clock().isoformat()
            state = 'cancelled' if item['state'] == 'queued' else 'cancel_requested'
            connection.execute('UPDATE agent_queue_items SET state=?,revision=revision+1,updated_at=? WHERE id=?', (state, now, item['id']))
            connection.execute("UPDATE agent_attempts SET cancel_requested=1,revision=revision+1,updated_at=? WHERE item_id=? AND state!='released'", (now, item['id']))
            self._touch(connection, item['lane_id'], now)
        return self.item(item['id'])
