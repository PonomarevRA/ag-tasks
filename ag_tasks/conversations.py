"""Harness-neutral chat identity and task associations; no model or process control."""

from uuid import uuid4

from .issues import DomainError, bounded_text, timestamp
from .service_values import fields, identifier, page

OPERATIONS = ('conversation.bind', 'conversation.reconnect')
IDENTITY = {'harness', 'instanceId', 'remoteSessionId'}


class Conversations:
    """Keep explicit chat bindings across restarts and transient-session retention."""

    def __init__(self, store):
        """Reuse the service store; adapters never create another authority."""
        self.store, self.db = store, store.db

    @staticmethod
    def identity(payload):
        """
        Validate the caller's stable host identity without guessing a current chat.

        Args:
            payload: harness, stable instanceId and opaque remoteSessionId.
        Returns:
            The exact, case-sensitive identity tuple; no paths are resolved.
        Raises:
            DomainError: An identity is missing or exceeds its scalar bounds.
        """
        return (identifier(payload['harness']), identifier(payload['instanceId']),
                bounded_text(payload['remoteSessionId'], 512, empty=False))

    def _row(self, conversation_id, connection):
        """Resolve one local binding ID or raise conversation_not_found (404)."""
        identifier(conversation_id)
        row = connection.execute('SELECT * FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        if row is None:
            raise DomainError('conversation_not_found', 'No registered chat has this identity.', 404)
        return row

    def _value(self, row, connection):
        """
        Project stored chat provenance without claiming live model activity.

        Args:
            row: One stored conversations row under the owner lock.
            connection: Current connection for resolving the task's published key.
        Returns:
            Detached JSON with current task link, lineage and unverified connection.
        """
        key = None
        if row['issue_id']:
            issue = connection.execute('''SELECT s.prefix,i.space_number FROM issues i
                JOIN spaces s ON s.id=i.space_id WHERE i.id=?''', (row['issue_id'],)).fetchone()
            key = f"{issue['prefix']}-{issue['space_number']}" if issue else None
        result = {'id': row['id'], 'harness': row['harness'], 'instanceId': row['instance_id'],
                'remoteSessionId': row['remote_id'], 'title': row['title'], 'workspace': row['workspace'],
                'issueId': row['issue_id'], 'issueKey': key, 'revision': row['revision'],
                'continuedFrom': row['continued_from'],
                'connection': {'id': row['connection_id'], 'activity': 'unknown',
                               'reportedToCurrentOwner': row['owner_incarnation'] == self.db.queue_incarnation},
                'createdAt': row['created_at'], 'updatedAt': row['updated_at']}
        return result

    def get(self, conversation_id):
        """Read one durable binding without restoring or starting a harness session."""
        with self.db.lock:
            return self._value(self._row(conversation_id, self.db.connection), self.db.connection)

    def resolve(self, harness, instance_id, remote_session_id):
        """
        Recover a binding from a harness's saved stable identity after a restart.

        Args:
            harness: Exact product/adapter ID, not an MCP client display name.
            instance_id: Installation/profile identity retained by the adapter.
            remote_session_id: Actual chat ID supplied explicitly by its owner.
        Returns:
            Current binding and revision; missing bindings raise a domain 404.
        """
        identity = self.identity({'harness': harness, 'instanceId': instance_id, 'remoteSessionId': remote_session_id})
        with self.db.lock:
            row = self.db.connection.execute('SELECT * FROM conversations WHERE harness=? AND instance_id=? AND remote_id=?', identity).fetchone()
            if row is None:
                raise DomainError('conversation_not_found', 'No registered chat has this identity.', 404)
            return self._value(row, self.db.connection)

    def list(self, limit=20, after=0, issue_id=None):
        """
        Page all chats or one task's chats in stable ascending creation order.

        Args:
            limit: Page size 1..50; boolean values are rejected.
            after: Exclusive nonnegative creation cursor, zero for the first page.
            issue_id: Optional task ID/alias; absent lists all registered chats.
        Returns:
            Metadata items, hasMore and an unchanged nextCursor for the next read.
        Raises:
            DomainError: Invalid pagination or an unknown task filter.
        """
        page(limit, after)
        with self.db.lock:
            connection = self.db.connection
            query, args = 'SELECT * FROM conversations WHERE number>?', [after]
            if issue_id is not None:
                query += ' AND issue_id=?'
                args.append(self.store._row(issue_id, connection)['id'])
            rows = connection.execute(query + ' ORDER BY number LIMIT ?', (*args, limit + 1)).fetchall()
            return {'items': [self._value(row, connection) for row in rows[:limit]],
                    'hasMore': len(rows) > limit, 'nextCursor': rows[limit - 1]['number'] if len(rows) > limit else None}

    @staticmethod
    def _expect(row, revision):
        """Fence changed bindings with a real integer revision; booleans are invalid."""
        if type(revision) is not int or revision != row['revision']:
            raise DomainError('conversation_conflict', 'Read the current chat binding before changing it.', 409)

    def bind(self, payload):
        """
        Register or explicitly change a chat's task association with revision CAS.

        Args:
            payload: Stable identity, connectionId, issueId (null unlinks), title,
                optional workspace hint/continuedFrom and expectedRevision (0 for creation).
        Returns:
            Durable binding. Exact initial registration reuse creates no duplicate.
        Raises:
            DomainError: Invalid fields, stale revision/connection or missing task.

        Binding never starts task work, refreshes task aging or grants filesystem
        access. A fork must use its own remote chat ID rather than reuse a parent.
        """
        allowed = IDENTITY | {'connectionId', 'issueId', 'title', 'workspace', 'expectedRevision', 'continuedFrom'}
        fields(payload, allowed, allowed - {'workspace', 'continuedFrom'})
        identity = self.identity(payload)
        connection_id = identifier(payload['connectionId'])
        title = bounded_text(payload['title'], 240, empty=False)
        workspace = bounded_text(payload.get('workspace', ''), 1024)
        revision = payload['expectedRevision']
        if type(revision) is not int or revision < 0:
            raise DomainError('invalid_revision', 'Expected a nonnegative integer binding revision.')
        with self.db.transaction() as connection:
            issue_id = self.store._row(payload['issueId'], connection)['id'] if payload['issueId'] is not None else None
            row = connection.execute('SELECT * FROM conversations WHERE harness=? AND instance_id=? AND remote_id=?', identity).fetchone()
            if row is None:
                if revision != 0:
                    raise DomainError('conversation_conflict', 'Initial registration requires revision zero.', 409)
                continued_from = payload.get('continuedFrom')
                if continued_from is not None:
                    previous = self._row(continued_from, connection)
                    if issue_id is None or previous['issue_id'] != issue_id:
                        raise DomainError('conversation_lineage_conflict', 'Continue from a chat bound to the same task.', 409)
                chat_id, now = uuid4().hex, timestamp()
                connection.execute('''INSERT INTO conversations(id,harness,instance_id,remote_id,title,workspace,
                    issue_id,connection_id,owner_incarnation,continued_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (chat_id, *identity, title, workspace, issue_id, connection_id, self.db.queue_incarnation, continued_from, now, now))
            else:
                chat_id = row['id']
                if 'continuedFrom' in payload and payload['continuedFrom'] != row['continued_from']:
                    raise DomainError('conversation_lineage_conflict', 'Chat continuation provenance is immutable.', 409)
                same = (row['issue_id'], row['title'], row['workspace'], row['connection_id']) == (issue_id, title, workspace, connection_id)
                # Semantic registration dedup is distinct from immutable command retries.
                if not (revision == 0 and row['revision'] == 1 and same):
                    self._expect(row, revision)
                    if row['connection_id'] != connection_id or row['owner_incarnation'] != self.db.queue_incarnation:
                        raise DomainError('conversation_reconnect_required', 'Reconnect this chat before changing its task.', 409)
                if not same:
                    connection.execute('''UPDATE conversations SET issue_id=?,title=?,workspace=?,revision=revision+1,
                        updated_at=? WHERE id=?''', (issue_id, title, workspace, timestamp(), chat_id))
            return self._value(self._row(chat_id, connection), connection)

    def reconnect(self, payload):
        """
        Replace only the transport binding, preserving the saved task and chat ID.

        Args:
            payload: conversationId, expectedRevision and the new connectionId.
        Returns:
            Current binding; another concurrent reconnect rejects with CAS conflict.
        Raises:
            DomainError: Unknown chat, malformed identity or stale revision.
        """
        fields(payload, {'conversationId', 'expectedRevision', 'connectionId'},
               {'conversationId', 'expectedRevision', 'connectionId'})
        connection_id = identifier(payload['connectionId'])
        with self.db.transaction() as connection:
            row = self._row(payload['conversationId'], connection)
            self._expect(row, payload['expectedRevision'])
            if row['connection_id'] != connection_id or row['owner_incarnation'] != self.db.queue_incarnation:
                connection.execute('''UPDATE conversations SET connection_id=?,owner_incarnation=?,
                    revision=revision+1,updated_at=? WHERE id=?''',
                    (connection_id, self.db.queue_incarnation, timestamp(), row['id']))
            return self._value(self._row(row['id'], connection), connection)
