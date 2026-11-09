"""Explicit RnD plans, stale-task revalidation and attributed complexity/problem feedback."""

import json
from uuid import uuid4

from .issues import DomainError, bounded_text, timestamp

COMPLEXITY_POINTS = (1, 2, 3, 5, 8, 13)


def assessment(value, actor):
    """Validate relative Fibonacci points with a human-readable basis; estimates are never runtime metrics."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != {'points', 'reason'}:
        raise DomainError('invalid_complexity', 'Complexity requires points and reason.')
    if type(value['points']) is not int or value['points'] not in COMPLEXITY_POINTS:
        raise DomainError('invalid_complexity', 'Choose relative complexity 1, 2, 3, 5, 8 or 13.')
    return {'points': value['points'], 'reason': bounded_text(value['reason'], 2048, empty=False),
            'actor': bounded_text(actor, 128, empty=False), 'recordedAt': timestamp()}


class Planning:
    """Own planning actions independently of work leases, Git commands and caller/model execution."""

    def __init__(self, store):
        self.store = store

    def create_plan(self, value, actor='agent'):
        """Save title/Markdown and optional issue metadata as an RnD parent with plan.md.

        Actor is declared provenance. Return the full created task; invalid input raises
        DomainError and rolls back both writes. No children, branch or work session start.
        """
        allowed = {'title', 'markdown', 'spaceId', 'repoId', 'estimatedComplexity', 'priority', 'assignee', 'dueDate', 'externalRefs', 'dor', 'dod', 'parentId', 'intendedRole', 'codeTask', 'languages', 'systems', 'projectKey', 'projectName', 'projectPrefix'}
        if type(value) is not dict or set(value) - allowed or not {'title', 'markdown'} <= set(value):
            raise DomainError('invalid_plan', 'A plan requires title and markdown.')
        markdown = self.store.documents.validate_content(value['markdown'])
        # The outer transaction also covers the document; a failed attachment cannot leave a half-plan.
        with self.store.db.transaction() as connection:
            data = {key: item for key, item in value.items() if key != 'markdown'}
            data.update(kind='rnd', intendedRole=data.get('intendedRole') or 'analyst')
            identifier = self.store._insert(data, actor, connection)
            self.store.documents.save_in(connection, identifier, {'name': 'plan.md', 'content': markdown, 'purpose': 'plan'}, actor, invalidate=False)
        return self.store.get(identifier)

    def mark_stale(self, connection, issue_id, actor):
        """Stop outdated ordinary descendants as well as their parent; generated critics are superseded, not resumed."""
        rows = connection.execute("""WITH RECURSIVE descendants(id) AS (
            SELECT id FROM issues WHERE id=? UNION SELECT i.id FROM issues i JOIN descendants d ON i.parent_id=d.id
            WHERE i.kind!='review' AND i.deleted_at IS NULL)
            SELECT issues.* FROM issues JOIN descendants USING(id) WHERE status NOT IN ('done','cancelled')""", (issue_id,)).fetchall()
        for issue in rows:
            self.store.results.supersede(connection, issue['id'], 'marked_stale', issue['result_generation'] + 1)
            session = connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (issue['id'],)).fetchone()
            if session:
                self.store.execution._close(connection, session)
            connection.execute("UPDATE issues SET status='stale',updated_at=? WHERE id=?", (timestamp(), issue['id']))
            if issue['id'] != issue_id:
                self.store._audit(connection, issue['id'], 'ancestor_stale', actor, {'ancestorId': issue_id})

    def revalidate(self, issue_id, value):
        """Only an explicit human decision reopens stale work; prior DoR evidence and acceptance are invalidated."""
        if type(value) is not dict or set(value) != {'reason'}:
            raise DomainError('invalid_revalidation', 'Explain why this task is relevant again.')
        reason = bounded_text(value['reason'], 2048, empty=False)
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection)
            if issue['status'] != 'stale' or issue['archived_at'] or issue['kind'] == 'review':
                raise DomainError('not_stale', 'Restore a stale ordinary task before revalidating it.', 409)
            dor = json.loads(issue['dor'])
            for item in dor:
                item.update(passed=False, evidence='')
            self.store.results.supersede(connection, issue_id, 'revalidated', issue['result_generation'] + 1)
            connection.execute("UPDATE issues SET status='backlog',dor=?,definition_revision=definition_revision+1,updated_at=? WHERE id=?",
                               (json.dumps(dor), timestamp(), issue_id))
            self.store._audit(connection, issue_id, 'revalidated', 'user', {'reason': reason})
        return self.store.get(issue_id)

    def record_actual(self, issue_id, value, actor='agent'):
        """Record a post-implementation complexity assessment only for a submitted result, preserving earlier audit."""
        content = assessment(value, actor)
        if content is None:
            raise DomainError('invalid_complexity', 'An actual assessment cannot be empty.')
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection)
            if issue['archived_at']:
                raise DomainError('issue_read_only', 'Restore the task before changing its assessment.', 409)
            if issue['status'] == 'stale':
                raise DomainError('revalidation_required', 'Revalidate the task before assessing new implementation.', 409)
            self.store.results.guard(issue_id, connection)
            result = connection.execute('SELECT * FROM results WHERE issue_id=? ORDER BY generation DESC LIMIT 1', (issue_id,)).fetchone()
            if result is None or result['definition_revision'] != issue['definition_revision']:
                raise DomainError('result_required', 'Submit a current implementation result before rating actual complexity.', 409)
            content.update(resultGeneration=result['generation'], definitionRevision=result['definition_revision'])
            connection.execute('UPDATE issues SET actual_complexity=?,updated_at=? WHERE id=?', (json.dumps(content), timestamp(), issue_id))
            self.store._audit(connection, issue_id, 'complexity_assessed', actor, content)
        return self.store.get(issue_id)

    def problems(self, issue_id, limit=50, offset=0):
        """Return one bounded page of task-scoped findings; querying does not execute their text or follow links."""
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise DomainError('invalid_page', 'Invalid problem pagination.')
        with self.store.db.lock:
            self.store._row(issue_id)
            rows = self.store.db.connection.execute('SELECT * FROM problems WHERE issue_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?', (issue_id, limit + 1, offset)).fetchall()
            items, used = [], 0
            for row in rows[:limit]:
                item = dict(row)
                amount = len(json.dumps(item, ensure_ascii=True).encode())
                if items and used + amount > 128 * 1024:
                    break
                used += amount
                items.append(item)
            more = len(rows) > len(items)
            return {'items': items, 'hasMore': more, 'nextOffset': offset + len(items) if more else None}

    def problem(self, issue_id, value, actor='agent', problem_id=None):
        """Append a structured blocker/lesson or resolve it explicitly; audit retains all earlier problem versions."""
        fields = {'title', 'severity', 'state', 'details', 'resolution'}
        if type(value) is not dict or set(value) - fields:
            raise DomainError('invalid_problem', 'Unsupported problem fields.')
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection)
            if issue['archived_at']:
                raise DomainError('issue_read_only', 'Restore the task before recording findings.', 409)
            current = connection.execute('SELECT * FROM problems WHERE id=? AND issue_id=?', (problem_id, issue_id)).fetchone() if problem_id else None
            if problem_id and current is None:
                raise DomainError('problem_not_found', 'Task problem not found.', 404)
            if current is None and connection.execute('SELECT COUNT(*) FROM problems WHERE issue_id=?', (issue_id,)).fetchone()[0] >= 1000:
                raise DomainError('problem_limit', 'At most 1000 structured problems per task.', 409)
            item = dict(current) if current else {'severity': 'medium', 'state': 'open', 'details': '', 'resolution': ''}
            item.update(value)
            for key, maximum in [('title', 256), ('details', 8192), ('resolution', 4096)]:
                item[key] = bounded_text(item.get(key), maximum, empty=key != 'title')
            if item['severity'] not in ('low', 'medium', 'high', 'critical') or item['state'] not in ('open', 'resolved'):
                raise DomainError('invalid_problem', 'Invalid problem state or severity.')
            if item['state'] == 'resolved' and not item['resolution'].strip():
                raise DomainError('resolution_required', 'Describe the resolution before closing a problem.')
            identifier, now = problem_id or uuid4().hex, timestamp()
            if current:
                connection.execute('UPDATE problems SET title=?,severity=?,state=?,details=?,resolution=?,updated_at=? WHERE id=?',
                                   (item['title'], item['severity'], item['state'], item['details'], item['resolution'], now, identifier))
            else:
                connection.execute('INSERT INTO problems(id,issue_id,title,severity,state,details,resolution,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                   (identifier, issue_id, item['title'], item['severity'], item['state'], item['details'], item['resolution'], actor, now, now))
            connection.execute('UPDATE problems SET details_preview=substr(details,1,1024),resolution_preview=substr(resolution,1,1024) WHERE id=?',(identifier,))
            connection.execute('UPDATE issues SET updated_at=? WHERE id=?', (now, issue_id))
            self.store._audit(connection, issue_id, 'problem_recorded' if not current else 'problem_updated', actor,
                               {'problemId': identifier, **{key: item[key] for key in fields}})
        return {'id': identifier, **{key: item[key] for key in fields}}
