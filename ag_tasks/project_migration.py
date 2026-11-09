"""Atomic organizational moves preserve task/review/work identities and cross-space relations."""

from .issues import DomainError
from .prefixes import allocate, issue_key, remember_key
from .service_values import fields, identifier


class ProjectMigration:
    """Move a complete root tree without modifying acceptance, activity timestamps or work leases."""

    def __init__(self, store):
        self.store = store

    def inspect(self, root_id):
        """
        Read the exact bounded root membership, including archived/deleted records and critics.

        Args:
            root_id: Canonical root identity (a legacy/current task key is also accepted).
        Returns:
            rootId, expectedSpaceId, expectedIds and compact metadata for an atomic move.
        Raises:
            DomainError: Root is missing, is a child, has split ownership, or exceeds 2000 records.

        This read does not restore cold bodies or renew work/activity timestamps.
        """
        identifier(root_id)
        with self.store.db.lock:
            connection = self.store.db.connection
            alias = connection.execute('SELECT issue_id FROM issue_keys WHERE key=? COLLATE NOCASE', (root_id,)).fetchone()
            if alias:
                root_id = alias['issue_id']
            root = connection.execute('SELECT id,parent_id,space_id FROM issues WHERE id=?', (root_id,)).fetchone()
            if root is None:
                raise DomainError('issue_not_found', 'Root task not found.', 404)
            if root['parent_id'] is not None:
                raise DomainError('root_required', 'Inspect the complete top-level root.', 409)
            rows = connection.execute('''WITH RECURSIVE tree(id) AS (
                SELECT id FROM issues WHERE id=? UNION SELECT i.id FROM issues i JOIN tree t ON i.parent_id=t.id)
                SELECT issues.* FROM issues JOIN tree USING(id) ORDER BY number LIMIT 2001''', (root_id,)).fetchall()
            if len(rows) > 2000:
                raise DomainError('tree_too_large', 'A project move accepts at most 2000 records.', 409)
            if any(row['space_id'] != root['space_id'] for row in rows):
                raise DomainError('split_tree', 'The hierarchy already spans spaces; reconcile its ownership first.', 409)
            return {'rootId':root_id,'expectedSpaceId':root['space_id'],
                    'expectedIds':[row['id'] for row in rows],
                    'items':[{'id':row['id'],'key':issue_key(connection,row),'parentId':row['parent_id'],
                              'spaceId':row['space_id'],'kind':row['kind'],'status':row['status']} for row in rows]}

    def move(self, payload, actor='agent'):
        """
        Move one exact root tree, including archived/deleted descendants and generated critics.

        Args:
            payload: rootId, projectKey, expectedSpaceId and exact expectedIds (at most 2000).
            actor: Caller provenance recorded in organizational audit entries.
        Returns:
            Root/project/space identity and old/new key mapping for each moved task.
        Raises:
            DomainError: Root, project, current space or exact tree membership differs.

        IDs, parent/links, status, results, revisions and lease credentials remain
        unchanged. Existing keys become permanent aliases before new allocation.
        A concurrent child/review causes a conflict instead of a partial move.
        """
        fields(payload, {'rootId','projectKey','expectedSpaceId','expectedIds'},
               {'rootId','projectKey','expectedSpaceId','expectedIds'})
        expected = payload['expectedIds']
        if type(expected) is not list or not 1 <= len(expected) <= 2000:
            raise DomainError('invalid_tree', 'Provide the exact root tree IDs, at most 2000.')
        for value in expected:
            identifier(value)
        if len(set(expected)) != len(expected):
            raise DomainError('invalid_tree', 'Tree identities must be unique.')
        identifier(payload['expectedSpaceId'])
        identifier(payload['rootId'])
        with self.store.db.transaction() as connection:
            # Normal detail reads restore cold bodies. Organizational moves must
            # keep the exact storage representation and immutable payloads intact.
            root_id = payload['rootId']
            alias = connection.execute('SELECT issue_id FROM issue_keys WHERE key=? COLLATE NOCASE', (root_id,)).fetchone()
            if alias:
                root_id = alias['issue_id']
            root = connection.execute('SELECT * FROM issues WHERE id=?', (root_id,)).fetchone()
            if root is None:
                raise DomainError('issue_not_found', 'Root task not found.', 404)
            if root['parent_id'] is not None:
                raise DomainError('root_required', 'Move the whole top-level tree, never an individual child.', 409)
            target = self.store.service.projects.get(payload['projectKey'], connection)
            rows = connection.execute('''WITH RECURSIVE tree(id) AS (
                SELECT id FROM issues WHERE id=? UNION SELECT i.id FROM issues i JOIN tree t ON i.parent_id=t.id)
                SELECT issues.* FROM issues JOIN tree USING(id) ORDER BY number LIMIT 2001''', (root['id'],)).fetchall()
            if len(rows) > 2000:
                raise DomainError('tree_too_large', 'A project move accepts at most 2000 records.', 409)
            if {row['id'] for row in rows} != set(expected):
                raise DomainError('tree_changed', 'Tree membership changed; read it again before migrating.', 409)
            if any(row['space_id'] != payload['expectedSpaceId'] for row in rows):
                raise DomainError('space_changed', 'Tree ownership changed; read it again before migrating.', 409)
            mapping = []
            for row in rows:
                before = issue_key(connection, row)
                remember_key(connection, row['id'], before)
                if row['space_id'] != target['id']:
                    number = allocate(connection, target['id'])
                    connection.execute('UPDATE issues SET space_id=?,space_number=? WHERE id=?',
                                       (target['id'], number, row['id']))
                current = connection.execute('SELECT * FROM issues WHERE id=?', (row['id'],)).fetchone()
                after = issue_key(connection, current)
                remember_key(connection, row['id'], after)
                if before != after:
                    self.store._audit(connection, row['id'], 'project_space_moved', actor,
                        {'rootId':root['id'],'projectKey':target['project_key'],
                         'fromSpaceId':row['space_id'],'toSpaceId':target['id'],'oldKey':before,'newKey':after})
                mapping.append({'id':row['id'],'oldKey':before,'newKey':after})
            return {'rootId':root['id'],'projectKey':target['project_key'],'spaceId':target['id'],
                    'moved':sum(item['oldKey'] != item['newKey'] for item in mapping),'items':mapping}
