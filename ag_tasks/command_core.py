"""Transport-independent atomic command identity; adapters never implement deduplication."""

import json
from uuid import uuid4

from .issues import DomainError, timestamp
from .service_values import encode, fingerprint, identifier, page


class CommandJournal:
    """Commit a domain effect and its immutable reply together, including stable domain rejections."""

    def __init__(self, database):
        """Use the service's existing single-owner connection, never a second SQLite authority."""
        self.db = database

    def execute(self, client_id, command_id, operation, payload, apply):
        """Return the original result on retry; a key cannot name a different operation or payload."""
        identifier(client_id)
        identifier(command_id)
        digest = fingerprint({'operation': operation, 'payload': payload})
        with self.db.transaction() as connection:
            old = connection.execute('SELECT * FROM service_commands WHERE client_id=? AND command_id=?',
                                     (client_id, command_id)).fetchone()
            if old:
                if old['fingerprint'] != digest:
                    raise DomainError('idempotency_conflict', 'Command key already names different data.', 409)
                response, status = json.loads(old['response']), old['status']
            else:
                try:
                    # The inner savepoint rolls back a rejected partial effect, not the saved rejection.
                    with self.db.transaction():
                        response = apply(operation, payload)
                        encoded = encode(response, 1_900_000, maximum_nodes=262144)
                    status = 200
                except DomainError as error:
                    status = error.status
                    response = {'error': {'code': error.code, 'message': error.message, 'details': error.details}}
                    encoded = encode(response)
                connection.execute('INSERT INTO service_commands VALUES(?,?,?,?,?,?,?)',
                                   (client_id, command_id, operation, digest, encoded, status, timestamp()))
                subject_id = response.get('sessionId') or response.get('id') or payload.get('sessionId') or payload.get('issueId')
                if type(subject_id) is not str:
                    subject_id = None
                connection.execute('''INSERT INTO service_changes(id,client_id,command_id,operation,status,subject_id,created_at)
                    VALUES(?,?,?,?,?,?,?)''', (uuid4().hex, client_id, command_id, operation, status, subject_id, timestamp()))
        if status >= 400:
            error = response['error']
            raise DomainError(error['code'], error['message'], status, details=error.get('details'))
        return response

    def outcome(self, client_id, command_id):
        """Read a durable reply after a lost ACK; no missing command is automatically replayed."""
        identifier(client_id)
        identifier(command_id)
        with self.db.lock:
            row = self.db.connection.execute('SELECT * FROM service_commands WHERE client_id=? AND command_id=?',
                                             (client_id, command_id)).fetchone()
            if row is None:
                raise DomainError('command_not_found', 'No committed command outcome is known.', 404)
            return {'clientId': client_id, 'commandId': command_id, 'operation': row['operation'],
                    'status': row['status'], 'response': json.loads(row['response']), 'createdAt': row['created_at']}

    def events(self, limit=20, after=0):
        """Read universal command facts, independent of source protocol; retries emit no second fact."""
        page(limit, after)
        with self.db.lock:
            rows = self.db.connection.execute('SELECT * FROM service_changes WHERE cursor>? ORDER BY cursor LIMIT ?',
                                              (after, limit + 1)).fetchall()
            items = [{'id': row['id'], 'cursor': row['cursor'], 'clientId': row['client_id'],
                      'commandId': row['command_id'], 'operation': row['operation'], 'status': row['status'],
                      'type': 'command.committed' if row['status'] < 400 else 'command.rejected',
                      'subjectId': row['subject_id'], 'createdAt': row['created_at']} for row in rows[:limit]]
            return {'items': items, 'hasMore': len(rows) > limit,
                    'nextCursor': items[-1]['cursor'] if len(rows) > limit else None}
