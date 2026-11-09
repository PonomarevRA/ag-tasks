"""Organizational spaces, bounded real graphs and explicit reversible archive/tombstone operations."""

import json
from uuid import uuid4

from .issues import DomainError, bounded_text, timestamp, ISSUE_COLUMNS


class Spaces:
    """Spaces organize local issues; they do not introduce authentication, repository grants or filesystem deletion."""

    def __init__(self, store):
        """Use the same durable issue owner and existing transaction boundary."""
        self.store = store

    def list(self):
        """Return whole-space counts and confirmed work activity without expanding issue bodies.

        Counts exclude archived/deleted issues; last activity also includes archived work.
        Only accepted work heartbeats establish agent activity, never generic issue edits.
        """
        self.store.execution.expire_all()
        with self.store.db.lock:
            connection = self.store.db.connection
            counts = {row['space_id']: row for row in connection.execute('''
                SELECT space_id,
                    SUM(status='backlog') AS backlog,
                    SUM(status='in_progress') AS in_progress
                FROM issues WHERE deleted_at IS NULL AND archived_at IS NULL GROUP BY space_id''')}
            activity = {row['space_id']: row for row in connection.execute('''
                SELECT i.space_id,MAX(w.last_heartbeat_at) AS last_work_at,
                    COUNT(DISTINCT CASE WHEN w.state='running' AND i.archived_at IS NULL THEN i.id END) AS active
                FROM work_sessions w JOIN issues i ON i.id=w.issue_id
                WHERE i.deleted_at IS NULL GROUP BY i.space_id''')}
            rows = self.store.db.connection.execute('''SELECT s.*,p.project_key FROM spaces s
                LEFT JOIN project_spaces p ON p.space_id=s.id ORDER BY s.created_at,s.id''').fetchall()
            items = []
            for row in rows:
                count = counts.get(row['id'])
                work = activity.get(row['id'])
                items.append({'id': row['id'], 'name': row['name'], 'createdAt': row['created_at'],
                    'archivedAt': row['archived_at'], 'prefix': row['prefix'], 'projectKey': row['project_key'],
                    'summary': {'backlog': count['backlog'] if count else 0,
                        'inProgress': count['in_progress'] if count else 0,
                        'activeTasks': work['active'] if work else 0,
                        'lastAgentWorkAt': work['last_work_at'] if work else None}})
            return items

    def write(self, value, identifier=None):
        """Create or rename one human-selected organizational space; the default space's stable ID never changes."""
        if type(value) is not dict or set(value) - {'name', 'prefix'} or 'name' not in value:
            raise DomainError('invalid_space', 'Space requires a name.')
        name = bounded_text(value['name'], 128, empty=False)
        from .prefixes import prefix, reserve, suggest, issue_key, remember_key
        with self.store.db.transaction() as connection:
            if identifier is None:
                identifier = uuid4().hex
                # Reserve after the space exists so the prefix foreign key stays valid.
                connection.execute('INSERT INTO spaces(id,name,created_at,archived_at) VALUES(?,?,?,NULL)', (identifier, name, timestamp()))
                chosen = reserve(connection, value['prefix'] if 'prefix' in value else suggest(name, connection), identifier)
                connection.execute('UPDATE spaces SET prefix=? WHERE id=?', (chosen, identifier))
            elif not connection.execute('UPDATE spaces SET name=? WHERE id=?', (name, bounded_text(identifier, 128, empty=False))).rowcount:
                raise DomainError('space_not_found', 'Space not found.', 404)
            elif 'prefix' in value:
                chosen = reserve(connection, value['prefix'], identifier)
                connection.execute('UPDATE spaces SET prefix=? WHERE id=?', (chosen, identifier))
                for issue in connection.execute('SELECT * FROM issues WHERE space_id=?', (identifier,)).fetchall():
                    remember_key(connection, issue['id'], issue_key(connection, issue))
        return next(item for item in self.list() if item['id'] == identifier)

    def validate_issue_space(self, identifier, space_id, parent_id, connection):
        """Keep composition inside one space while allowing explicit cross-space dependency/relation links."""
        if connection.execute('SELECT 1 FROM spaces WHERE id=? AND archived_at IS NULL', (space_id,)).fetchone() is None:
            raise DomainError('space_not_found', 'Space not found.', 404)
        if parent_id and self.store._row(parent_id, connection)['space_id'] != space_id:
            raise DomainError('cross_space_relation', 'Parent and child must belong to the same space.', 409)
        if identifier is None:
            return
        child = connection.execute('SELECT 1 FROM issues WHERE parent_id=? AND space_id!=? AND deleted_at IS NULL LIMIT 1', (identifier, space_id)).fetchone()
        if child:
            raise DomainError('cross_space_relation', 'Moving a parent would split its hierarchy across spaces.', 409)

    def decompose(self, parent_id, value, actor='agent'):
        """Create a bounded child plan atomically; none of its tasks or timers start implicitly."""
        if type(value) is not dict or set(value) != {'children'} or type(value['children']) is not list or not 1 <= len(value['children']) <= 50:
            raise DomainError('invalid_decomposition', 'Specify one to fifty child tasks.')
        identifiers = []
        with self.store.db.transaction() as connection:
            parent = self.store._row(parent_id, connection)
            self.store.execution.editable(parent)
            if parent['kind'] == 'review':
                raise DomainError('critic_task_read_only', 'Generated critic tasks cannot be decomposed.', 409)
            for child in value['children']:
                if type(child) is not dict or child.get('parentId', parent_id) != parent_id or child.get('spaceId', parent['space_id']) != parent['space_id']:
                    raise DomainError('cross_space_relation', 'Decomposition inherits this parent and space.', 409)
                if parent['kind'] == 'rnd' and not child.get('intendedRole'):
                    raise DomainError('role_required', 'RnD decomposition requires an intended role for every child.')
                identifiers.append(self.store._insert({**child, 'parentId': parent_id, 'spaceId': parent['space_id']}, actor, connection))
            self.store._audit(connection, parent_id, 'decomposed', actor, {'childIds': identifiers})
        with self.store.db.lock:
            items, used = [], 0
            for identifier in identifiers:
                item = self.store._issue(self.store._row(identifier), compact=True)
                size = len(json.dumps(item, ensure_ascii=True).encode('ascii'))
                if used + size > 512 * 1024:
                    break
                used += size
                items.append(item)
        return {'parentId': parent_id, 'items': items, 'createdIds': identifiers, 'itemsCount': len(identifiers), 'itemsTruncated': len(items) != len(identifiers)}

    def archive(self, issue_id):
        """Hide completed, cancelled or stale issues only; active descendants remain visible."""
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection)
            if issue['branch'] and issue['status'] == 'done' and issue['merged_head'] != issue['reviewed_head']:
                raise DomainError('merge_pending', 'Resolve the explicit merge approval before archiving this task.', 409)
            if issue['status'] not in ('done', 'cancelled', 'stale') or connection.execute("SELECT 1 FROM issues WHERE parent_id=? AND deleted_at IS NULL AND status NOT IN ('done','cancelled','stale') LIMIT 1", (issue_id,)).fetchone():
                raise DomainError('archive_not_ready', 'Archive requires terminal work and no active child issues.', 409)
            if connection.execute("SELECT 1 FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone():
                raise DomainError('work_active', 'Stop work before archiving.', 409)
            if not issue['archived_at']:
                connection.execute('UPDATE issues SET archived_at=?,updated_at=? WHERE id=?', (timestamp(), timestamp(), issue_id))
                self.store._audit(connection, issue_id, 'archived', 'user', {})
        return self.store.get(issue_id)

    def restore(self, issue_id):
        """Restore an archived/tombstoned issue without changing terminal status, branches or worktrees."""
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection, include_deleted=True)
            if issue['parent_id']:
                self.store._row(issue['parent_id'], connection)
                if issue['kind'] != 'review':
                    self.store._ensure_scheduling_edge(issue_id, issue['parent_id'], connection)
            connection.execute('UPDATE issues SET archived_at=NULL,deleted_at=NULL,updated_at=? WHERE id=?', (timestamp(), issue_id))
            self.store._audit(connection, issue_id, 'restored', 'user', {})
        return self.store.get(issue_id)

    def delete(self, issue_id):
        """Retain an explicit audit tombstone; never delete a repository, branch or managed directory."""
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection)
            children = connection.execute('SELECT 1 FROM issues WHERE parent_id=? AND deleted_at IS NULL LIMIT 1', (issue_id,)).fetchone()
            links = connection.execute('SELECT 1 FROM issue_links WHERE left_id=? OR right_id=? LIMIT 1', (issue_id, issue_id)).fetchone()
            active = connection.execute("SELECT 1 FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone()
            if not issue['archived_at'] or issue['status'] not in ('done', 'cancelled', 'stale') or children or links or active:
                raise DomainError('delete_not_ready', 'Delete requires an archived terminal issue with no children, links or active work.', 409)
            connection.execute('UPDATE issues SET deleted_at=?,updated_at=? WHERE id=?', (timestamp(), timestamp(), issue_id))
            self.store._audit(connection, issue_id, 'deleted', 'user', {'retention': 'audit_tombstone', 'gitFilesRemoved': False})
        return {'id': issue_id, 'deleted': True}

    def graph(self, space_id='default', limit=200, active_only=False):
        """Filter real running leases before bounding nodes; retain actual off-page relations."""
        bounded_text(space_id, 128, empty=False)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError('invalid_page', 'Graph limit must be1..200.')
        if type(active_only) is not bool:
            raise DomainError('invalid_filter', 'activeOnly must be boolean.')
        self.store.execution.expire_all()
        with self.store.db.lock:
            connection = self.store.db.connection
            if connection.execute('SELECT 1 FROM spaces WHERE id=?', (space_id,)).fetchone() is None:
                raise DomainError('space_not_found', 'Space not found.', 404)
            scope = 'space_id=? AND deleted_at IS NULL AND archived_at IS NULL'
            # Selecting before LIMIT keeps older active work reachable among newer idle tasks.
            if active_only:
                scope += " AND EXISTS(SELECT 1 FROM work_sessions w WHERE w.issue_id=issues.id AND w.state='running')"
            total = connection.execute('SELECT COUNT(*) FROM issues WHERE ' + scope, (space_id,)).fetchone()[0]
            rows = connection.execute('SELECT ' + ISSUE_COLUMNS + ' FROM issues WHERE ' + scope + ' ORDER BY number DESC LIMIT ?', (space_id, limit)).fetchall()
            from .lifecycle import child_progress
            progress = child_progress(connection, rows)
            nodes, used = [], 0
            for row in rows:
                node = self.store._issue(row, compact=True, child_progress=progress.get(row['id'], {}))
                size = len(json.dumps(node, ensure_ascii=True).encode('ascii'))
                if nodes and used + size > 400000:
                    break
                used += size
                nodes.append(node)
            identifiers = {item['id'] for item in nodes}
            relations = '''SELECT i.parent_id AS source,i.id AS target,'parent' AS kind FROM issues i JOIN issues p ON p.id=i.parent_id
                WHERE i.space_id=? AND i.deleted_at IS NULL AND p.deleted_at IS NULL AND i.archived_at IS NULL AND p.archived_at IS NULL
                UNION ALL SELECT l.left_id,l.right_id,l.kind FROM issue_links l JOIN issues a ON a.id=l.left_id JOIN issues b ON b.id=l.right_id
                WHERE (a.space_id=? OR b.space_id=?) AND a.deleted_at IS NULL AND b.deleted_at IS NULL AND a.archived_at IS NULL AND b.archived_at IS NULL'''
            relation_parameters = (space_id, space_id, space_id)
            if active_only:
                # Keep context links to active work; unrelated idle edges are outside this view.
                active_ids = 'SELECT id FROM issues WHERE ' + scope
                relations = f'SELECT * FROM ({relations}) WHERE source IN ({active_ids}) OR target IN ({active_ids})'
                relation_parameters += (space_id, space_id)
            edge_total = connection.execute('SELECT COUNT(*) FROM (' + relations + ')', relation_parameters).fetchone()[0]
            # SQL limits storage reads independently of graph size; all omitted coverage is explicit.
            placeholders = ','.join('?' for _ in identifiers)
            if identifiers:
                touching = f'SELECT * FROM ({relations}) WHERE source IN ({placeholders}) OR target IN ({placeholders}) LIMIT 1001'
                candidates = connection.execute(touching, (*relation_parameters, *identifiers, *identifiers)).fetchall()
            else:
                candidates = []
            edges, stubs = [], {}
            for relation in candidates:
                source, target = relation['source'], relation['target']
                if source not in identifiers and target not in identifiers:
                    continue
                missing = [identifier for identifier in (source, target) if identifier not in identifiers and identifier not in stubs]
                if len(edges) >= 500 or len(stubs) + len(missing) > 100:
                    continue
                for identifier in missing:
                    row = connection.execute('SELECT * FROM issues WHERE id=?', (identifier,)).fetchone()
                    from .prefixes import issue_key
                    stubs[identifier] = {'id': identifier, 'key': issue_key(connection, row), 'spaceId': row['space_id'], 'title': row['title'][:128], 'kind': row['kind']}
                edges.append({'from': source, 'to': target, 'kind': relation['kind']})
            return {'nodes': nodes, 'edges': edges, 'stubs': list(stubs.values()), 'nodeCount': total, 'edgeCount': edge_total,
                    'truncated': len(nodes) < total or len(edges) < edge_total}
