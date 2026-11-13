"""Durable observed sessions and event streams, shared by every service adapter."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .issues import DomainError, bounded_text, timestamp
from .service_values import encode, fields, fingerprint, identifier, page, redact
from . import session_projection
from .session_presence import SessionPresence
from . import session_order


class SessionEvents:
    """Store facts only: importing events never starts agents or changes task workflow."""

    def __init__(self, store):
        """Share domain storage and issue identity resolution with the rest of the service."""
        self.store, self.db = store, store.db
        self.presence = SessionPresence()

    def row(self, session_id, connection):
        """Resolve an opaque local session; never use a remote session ID as a local key."""
        identifier(session_id)
        row = connection.execute('SELECT * FROM service_sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise DomainError('session_not_found', 'Observed session not found.', 404)
        return row

    def register(self, payload):
        """Register one immutable source binding; managed execution is deliberately unavailable."""
        fields(payload, {'title', 'agent', 'protocol', 'protocolVersion', 'remoteSessionId', 'connectionId',
                         'issueId', 'capabilities'}, {'title', 'agent', 'protocol', 'protocolVersion', 'remoteSessionId', 'connectionId'})
        title = bounded_text(payload['title'], 240, empty=False)
        agent = bounded_text(payload['agent'], 128, empty=False)
        protocol = identifier(payload['protocol'])
        version = payload['protocolVersion']
        if type(version) is not int or not 1 <= version <= 999:
            raise DomainError('invalid_protocol_version', 'Protocol version must be an integer from 1 to 999.')
        remote = bounded_text(payload['remoteSessionId'], 512, empty=False)
        connection_id = identifier(payload['connectionId'])
        capabilities = payload.get('capabilities', {})
        if type(capabilities) is not dict:
            raise DomainError('invalid_capabilities', 'Capabilities must be a JSON object.')
        encode(capabilities, 8192)
        capabilities, redactions = redact(capabilities)
        capabilities = encode({'observed': capabilities, 'redactions': redactions}, 16384)
        session_id, now = uuid4().hex, timestamp()
        with self.db.transaction() as connection:
            issue_id = self.store._row(payload['issueId'], connection)['id'] if payload.get('issueId') is not None else None
            connection.execute('''INSERT INTO service_sessions(id,title,agent,protocol,protocol_version,remote_id,
                connection_id,issue_id,capabilities,projection,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (session_id, title, agent, protocol, version, remote, connection_id, issue_id, capabilities,
                 encode(session_projection.initial()), now, now))
        return self.get(session_id)

    def touch_presence(self, session_id, payload):
        """
        Refresh the exact MCP binding; persist freshness at most once per minute.

        Args:
            session_id: Opaque local binding ID.
            payload: Matching connectionId and connected/disconnected state.
        Returns:
            Current volatile connection presence, independent of model activity.
        Raises:
            DomainError: Identity, source kind, or presence state is invalid.

        Disconnect always records its time so retention starts from last contact,
        even for a connection that remained open longer than three days. Pulses
        update one metadata row; they never append events or command outcomes.
        """
        fields(payload, {'connectionId', 'state'}, {'connectionId', 'state'})
        with self.db.lock:
            session = self.row(session_id, self.db.connection)
            capabilities = json.loads(session['capabilities'])['observed']
            if session['protocol'] != 'mcp' or capabilities.get('sourceKind') != 'mcp_connection':
                raise DomainError('invalid_presence', 'Only a registered MCP connection can report presence.')
            if payload['connectionId'] != session['connection_id']:
                raise DomainError('connection_conflict', 'Presence must match its registered connection.', 409)
            if payload['state'] not in ('connected', 'disconnected'):
                raise DomainError('invalid_presence', 'Use connected or disconnected presence.')
            expired = self.presence.expire(session_id)
            previous = self.presence.snapshot(session_id)['state']
            result = self.presence.touch(session_id, payload['state'])
            from service_telemetry.integration import request_diagnostics
            state = 'unchanged' if previous == result['state'] else result['state']
            request_diagnostics(sessionId=session['id'], taskId=session['issue_id'], presenceState=state,
                                presenceWasStale=bool(expired))
            now = datetime.now(timezone.utc)
            stale_metadata = datetime.fromisoformat(session['updated_at']) <= now - timedelta(minutes=1)
            if (result['state'] == 'connected' and stale_metadata) or payload['state'] == 'disconnected':
                self.db.connection.execute('UPDATE service_sessions SET updated_at=? WHERE id=?',
                                           (now.isoformat(), session_id))
            return result

    def expire_presence(self):
        """Serialize maintenance detection with the observation update for each connection."""
        with self.db.lock:
            return self.presence.expire()

    def link(self, payload):
        """Change only the task association against an explicit revision, never execution ownership."""
        fields(payload, {'sessionId', 'issueId', 'expectedRevision'}, {'sessionId', 'issueId', 'expectedRevision'})
        with self.db.transaction() as connection:
            session = self.row(payload['sessionId'], connection)
            if type(payload['expectedRevision']) is not int or payload['expectedRevision'] != session['revision']:
                raise DomainError('revision_conflict', 'Read the current session before changing its task link.', 409)
            issue_id = self.store._row(payload['issueId'], connection)['id'] if payload['issueId'] is not None else None
            connection.execute('UPDATE service_sessions SET issue_id=?,revision=revision+1,updated_at=? WHERE id=?',
                               (issue_id, timestamp(), session['id']))
        return self.get(session['id'])

    def describe(self, row, connection, detail=False):
        """Expose bounded metadata; unknown process activity is never advertised as managed idle."""
        count = connection.execute('SELECT COUNT(*) FROM service_events WHERE session_id=?', (row['id'],)).fetchone()[0]
        result = {'id': row['id'], 'title': row['title'], 'agent': row['agent'], 'protocol': row['protocol'],
                  'protocolVersion': row['protocol_version'], 'remoteSessionId': row['remote_id'],
                  'connectionId': row['connection_id'], 'issueId': row['issue_id'], 'mode': 'observed',
                  'revision': row['revision'], 'projection': json.loads(row['projection']), 'eventCount': count,
                  'createdAt': row['created_at'], 'updatedAt': row['updated_at']}
        if row['issue_id']:
            linked = connection.execute('SELECT i.space_number,s.prefix FROM issues i JOIN spaces s ON s.id=i.space_id WHERE i.id=?',
                                        (row['issue_id'],)).fetchone()
            result['issueKey'] = f"{linked['prefix']}-{linked['space_number']}" if linked else None
        capabilities = json.loads(row['capabilities'])
        if capabilities.get('observed', {}).get('sourceKind') == 'mcp_connection':
            result['connection'] = self.presence.snapshot(row['id'])
        if detail:
            result['capabilities'] = json.loads(row['capabilities'])
        return result

    def get(self, session_id):
        """Read a source binding and bounded entity projection without restoring or executing a runtime."""
        with self.db.lock:
            connection = self.db.connection
            result = self.describe(self.row(session_id, connection), connection, True)
            rows = connection.execute('SELECT * FROM service_entities WHERE session_id=? ORDER BY last_cursor LIMIT 51',
                                      (session_id,)).fetchall()
            result['entities'] = [{'kind': row['kind'], 'remoteId': row['remote_id'], 'payload': json.loads(row['payload']),
                                   'lastCursor': row['last_cursor'], 'origin': 'live' if row['live'] else 'replay'} for row in rows[:50]]
            result['entitiesTruncated'] = len(rows) > 50
            return result

    def list(self, limit=20, after=0, issue_id=None, active_only=False, sort='newest'):
        """
        Page sessions by last activity, newest first unless oldest is requested.

        Args:
            limit: Maximum returned records, from 1 through 50.
            after: Zero or the previous page's opaque nextCursor (legacy rowids accepted).
            issue_id: Optional task key/ID to filter; resolved to its canonical ID.
            active_only: Restrict to currently confirmed MCP connections.
            sort: newest or oldest; activity ties use the immutable local ID.
        Returns:
            Bounded items, hasMore and an activity/id continuation token or None.
        Raises:
            DomainError: Limit, cursor, order or task identity is invalid.

        Continuations survive deletion of the boundary row and new insertions
        ahead of it. Refresh the first page to observe newly changed activity.
        """
        page(limit, 0)
        direction, comparison = session_order.ordering(sort)
        with self.db.lock:
            connection = self.db.connection
            if issue_id is not None:
                issue_id = self.store._row(issue_id, connection)['id']
            boundary = session_order.continuation(after, sort, issue_id, active_only, connection)
            conditions, args = ['1'], []
            if boundary:
                # Service activity is persisted as UTC ISO text; lexical comparison
                # retains microseconds that SQLite julianday would round away.
                conditions.append(f'(updated_at,id){comparison}(?,?)')
                args.extend(boundary)
            if active_only:
                connected = self.presence.connected_ids()
                conditions.append('id IN (' + ','.join('?' for _ in connected) + ')' if connected else '0')
                args.extend(connected)
            if issue_id is not None:
                conditions.append('issue_id=?')
                args.append(issue_id)
            rows = connection.execute('SELECT * FROM service_sessions WHERE ' + ' AND '.join(conditions) +
                                      f' ORDER BY updated_at {direction},id {direction} LIMIT ?',
                                      (*args, limit + 1)).fetchall()
            items = [self.describe(row, connection) for row in rows[:limit]]
            return {'items': items, 'hasMore': len(rows) > limit,
                    'nextCursor': session_order.cursor(rows[limit - 1], sort, issue_id, active_only) if len(rows) > limit else None}

    def prepare_event(self, value, session):
        """Validate envelope budgets before the transaction; redact only the persisted observation copy."""
        fields(value, {'sequence', 'connectionId', 'direction', 'origin', 'payload'},
               {'sequence', 'connectionId', 'direction', 'origin', 'payload'})
        if type(value['sequence']) is not int or not 1 <= value['sequence'] <= 2**53 - 1:
            raise DomainError('invalid_sequence', 'Event sequence must be a positive safe integer.')
        if value['connectionId'] != session['connection_id']:
            raise DomainError('connection_conflict', 'Use a new binding for an unverified connection epoch.', 409)
        if value['direction'] not in ('client_to_agent', 'agent_to_client') or value['origin'] not in ('live', 'replay'):
            raise DomainError('invalid_event', 'Invalid direction or origin.')
        if type(value['payload']) is not dict:
            raise DomainError('invalid_event', 'Event payload must be an object.')
        encode(value, 16384)
        session_projection.validate_payload(session['protocol'], value['payload'])
        payload, redactions = redact(value['payload'])
        result = {**value, 'payload': payload}
        # Identity covers the validated source, while the durable body omits credential fields.
        return result, redactions, 'event-raw-v2:' + fingerprint(value)

    def append(self, payload):
        """Commit contiguous producer events, projections and checkpoint atomically; retry cannot duplicate effects."""
        fields(payload, {'sessionId', 'producerId', 'producerEpoch', 'events'},
               {'sessionId', 'producerId', 'producerEpoch', 'events'})
        producer, epoch = identifier(payload['producerId']), identifier(payload['producerEpoch'])
        values = payload['events']
        if type(values) is not list or not 1 <= len(values) <= 100:
            raise DomainError('invalid_batch', 'Send between 1 and 100 events in a batch.')
        encode(payload)
        with self.db.transaction() as connection:
            session = self.row(payload['sessionId'], connection)
            stream = connection.execute('SELECT next_sequence FROM service_streams WHERE session_id=? AND producer_id=? AND epoch=?',
                                        (session['id'], producer, epoch)).fetchone()
            expected = stream['next_sequence'] if stream else 1
            state = json.loads(session['projection'])
            inserted, last_cursor, now = 0, None, timestamp()
            for value in values:
                event, redactions, digest = self.prepare_event(value, session)
                old = connection.execute('SELECT cursor,fingerprint,redactions FROM service_events WHERE session_id=? AND producer_id=? AND epoch=? AND sequence=?',
                                         (session['id'], producer, epoch, event['sequence'])).fetchone()
                if old:
                    stored_digest = old['fingerprint']
                    if not stored_digest.startswith('event-raw-v2:'):
                        # Old redacted archives cannot recover an original identity; do not guess it on retry.
                        if json.loads(old['redactions']):
                            raise DomainError('legacy_event_identity_unverifiable',
                                              'Original legacy event identity is unavailable; reconcile the producer checkpoint.', 409)
                        digest = fingerprint(value)
                    if stored_digest != digest:
                        raise DomainError('idempotency_conflict', 'Event key already names different data.', 409)
                    last_cursor = old['cursor']
                    continue
                if event['sequence'] != expected:
                    raise DomainError('sequence_gap', 'Producer journal is not contiguous.', 409,
                                      details={'expectedSequence': expected})
                cursor = connection.execute('''INSERT INTO service_events(id,session_id,producer_id,epoch,sequence,
                    fingerprint,direction,origin,payload,redactions,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (uuid4().hex, session['id'], producer, epoch, expected, digest, event['direction'], event['origin'],
                     encode(event['payload'], 16384), encode(redactions), now)).lastrowid
                state = session_projection.project(connection, session, {**event, 'cursor': cursor}, state)
                inserted, expected, last_cursor = inserted + 1, expected + 1, cursor
            connection.execute('INSERT OR REPLACE INTO service_streams VALUES(?,?,?,?)', (session['id'], producer, epoch, expected))
            if inserted:
                connection.execute('UPDATE service_sessions SET projection=?,updated_at=? WHERE id=?', (encode(state), now, session['id']))
            return {'sessionId': session['id'], 'producerId': producer, 'producerEpoch': epoch,
                    'acknowledgedThrough': expected - 1, 'inserted': inserted, 'lastCursor': last_cursor}

    def events(self, session_id, limit=20, after=0):
        """Read original observations by durable cursor, preserving unknown payloads and omission markers."""
        page(limit, after)
        with self.db.lock:
            connection = self.db.connection
            self.row(session_id, connection)
            rows = connection.execute('SELECT * FROM service_events WHERE session_id=? AND cursor>? ORDER BY cursor LIMIT ?',
                                      (session_id, after, limit + 1)).fetchall()
            items = [{'id': row['id'], 'cursor': row['cursor'], 'producerId': row['producer_id'],
                      'producerEpoch': row['epoch'], 'sequence': row['sequence'], 'direction': row['direction'],
                      'origin': row['origin'], 'payload': json.loads(row['payload']),
                      'redactions': json.loads(row['redactions']), 'receivedAt': row['received_at']} for row in rows[:limit]]
            return {'items': items, 'hasMore': len(rows) > limit,
                    'nextCursor': items[-1]['cursor'] if len(rows) > limit else None}

    def rebuild(self, payload):
        """Recompute only disposable read models in bounded memory; never send commands or change tasks."""
        fields(payload, {'sessionId'}, {'sessionId'})
        with self.db.transaction() as connection:
            session = self.row(payload['sessionId'], connection)
            connection.execute('DELETE FROM service_entities WHERE session_id=?', (session['id'],))
            connection.execute('DELETE FROM service_requests WHERE session_id=?', (session['id'],))
            state = session_projection.initial()
            cursor = connection.execute('SELECT cursor,direction,origin,payload FROM service_events WHERE session_id=? ORDER BY cursor', (session['id'],))
            while rows := cursor.fetchmany(100):
                for row in rows:
                    event = dict(row)
                    event['payload'] = json.loads(event['payload'])
                    state = session_projection.project(connection, session, event, state)
            connection.execute('UPDATE service_sessions SET projection=? WHERE id=?', (encode(state), session['id']))
        return self.get(session['id'])
