"""Issue domain, bounded references, hierarchy constraints and attributable append-only history."""

from datetime import datetime, timezone
import json
from urllib.parse import urlsplit
from uuid import uuid4

from .config import STATUSES, PRIORITIES
from .database import Database


# Scalar index lookups keep list metadata in the same read statement; attachment bodies remain deferred.
ISSUE_COLUMNS = """issues.*,
    (SELECT prefix FROM spaces WHERE id=issues.space_id) AS space_prefix,
    (SELECT name FROM spaces WHERE id=issues.space_id) AS space_name,
    (SELECT COUNT(*) FROM problems WHERE issue_id=issues.id) AS problems_count"""


class DomainError(Exception):
    """Carries a safe public code/message/status rather than exposing SQL or arbitrary git output."""
    def __init__(self, code, message, status=400, *, details=None):
        super().__init__(message)
        self.code, self.message, self.status, self.status_code = code, message, status, status
        self.details = details


def timestamp():
    """Return an explicit UTC instant for domain mutations and audit entries."""
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value, maximum, *, nullable=False, empty=True):
    """Validate native Unicode text and documented scalar bounds without implicit object conversion."""
    if value is None and nullable:
        return None
    if type(value) is not str or len(value) > maximum or any(0xD800 <= ord(c) <= 0xDFFF or ord(c) < 32 and c not in '\n\t' for c in value):
        raise DomainError('invalid_field', 'Invalid or oversized text field.')
    if not empty and not value.strip():
        raise DomainError('invalid_field', 'A nonempty value is required.')
    return value


def external_refs(value, *, field='externalRefs'):
    """Validate bounded HTTP(S) references and report the failing input path without echoing its contents."""
    def reject(message, path):
        """Return only contract guidance; submitted URLs may contain private information."""
        raise DomainError('invalid_references', message, details={
            'field': path,
            'expected': 'At most 20 objects with url (HTTP(S)), optional title and kind.',
            'recovery': 'Correct the references and resubmit. Put local paths in validation text or a task Markdown document.'})

    if type(value) is not list or len(value) > 20:
        reject('At most twenty external references are allowed.', field)
    result = []
    for index, item in enumerate(value):
        path = f'{field}[{index}]'
        if type(item) is not dict or set(item) - {'url', 'title', 'kind'}:
            reject('Reference objects accept only url, title and kind.', path)
        text_fields = {'url': (2048, False, None), 'title': (256, True, ''), 'kind': (64, False, 'evidence')}
        entry = {}
        for name, (maximum, empty, default) in text_fields.items():
            try:
                entry[name] = bounded_text(item.get(name, default), maximum, empty=empty)
            except DomainError:
                reject('Reference text is missing, invalid or oversized.', path + '.' + name)
        url = entry['url']
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
            parsed.port  # Reject invalid/out-of-range explicit ports.
        except ValueError:
            valid = False
        if not valid or any(char.isspace() for char in url):
            reject('References require a safe HTTP(S) URL without credentials or whitespace.', path + '.url')
        result.append(entry)
    return result


class IssueStore:
    """Owns durable issues and repositories; read APIs never alter git or invoke an observer."""

    def __init__(self, data_dir):
        """Open independent service state and keep managed worktree paths under its data directory."""
        self.db = Database(data_dir)
        self.data_dir = self.db.data_dir
        from .scheduling import Scheduling
        self.scheduling = Scheduling(self)
        from .execution import Execution
        from .results import Results
        from .spaces import Spaces
        self.execution, self.results, self.spaces = Execution(self), Results(self), Spaces(self)
        from .documents import Documents
        from .planning import Planning
        from .cold_storage import ColdStorage
        self.documents, self.planning, self.cold = Documents(self), Planning(self), ColdStorage(self)
        from .service_core import ServiceCore
        self.service = ServiceCore(self)
        from .aging import Aging
        self.aging = Aging(self)
        from .review_queue import ReviewQueue
        self.reviews = ReviewQueue(self)
        self.service.process.sweep(restart=True)

    def close(self):
        """Close SQLite without removing history, repository registration or branches."""
        self.cold.stop()
        self.db.close()

    def _row(self, issue_id, connection=None, *, include_deleted=False):
        """Resolve an opaque issue ID; unknown IDs return a stable domain 404."""
        bounded_text(issue_id, 128, empty=False)
        owner = connection or self.db.connection
        alias = owner.execute('SELECT issue_id FROM issue_keys WHERE key=? COLLATE NOCASE', (issue_id,)).fetchone() if '-' in issue_id else None
        if alias:
            issue_id = alias['issue_id']
        value = owner.execute('SELECT ' + ISSUE_COLUMNS + ' FROM issues WHERE id=?', (issue_id,)).fetchone()
        if value is None or value['deleted_at'] and not include_deleted:
            raise DomainError('issue_not_found', 'Issue not found.', 404)
        if value['storage_zip']:
            self.cold.restore(value['id'], connection or self.db.connection)
            value = (connection or self.db.connection).execute('SELECT ' + ISSUE_COLUMNS + ' FROM issues WHERE id=?', (value['id'],)).fetchone()
        return value

    def _issue(self, row, *, details=False, compact=False, child_progress=None):
        """Project fixed camelCase fields; list omits comment/history bodies and all reads bound collections."""
        from .prefixes import issue_key
        result = {'id': row['id'], 'key': issue_key(self.db.connection, row), 'title': row['title'], 'description': row['description'],
                  'status': row['status'], 'priority': row['priority'], 'parentId': row['parent_id'], 'assignee': row['assignee'],
                  'repoId': row['repo_id'], 'externalRefs': json.loads(row['external_refs']),
                  'createdAt': row['created_at'], 'updatedAt': row['updated_at'], 'lastActivityAt': row['last_activity_at']}
        metadata = row if 'space_name' in row.keys() else self.db.connection.execute('SELECT name AS space_name,(SELECT COUNT(*) FROM problems WHERE issue_id=?) AS problems_count FROM spaces WHERE id=?', (row['id'], row['space_id'])).fetchone()
        result['spaceName'] = metadata['space_name']
        if details:
            result['dueDate'] = self.scheduling.read(row['id'])
            result['workHistory'] = self.execution.history(row['id'])
            counts = self.db.connection.execute("SELECT status,COUNT(*) AS amount FROM issues WHERE parent_id=? AND kind!='review' AND deleted_at IS NULL GROUP BY status", (row['id'],)).fetchall()
            child_progress = {item['status']:item['amount'] for item in counts}
        result['childProgress'] = {'total':sum((child_progress or {}).values()), 'byStatus':child_progress or {}, 'scope':'direct-children'}
        result.update(spaceId=row['space_id'], kind=row['kind'], intendedRole=row['intended_role'],
                      dor=json.loads(row['dor']), dod=json.loads(row['dod']), definitionRevision=row['definition_revision'],
                      resultGeneration=row['result_generation'], criticReviewId=row['critic_review_id'],
                      archivedAt=row['archived_at'], deletedAt=row['deleted_at'], work=self.execution.project(row['id']))
        result.update(estimatedComplexity=json.loads(row['estimated_complexity']) if row['estimated_complexity'] else None,
                      actualComplexity=json.loads(row['actual_complexity']) if row['actual_complexity'] else None,
                      storage={'tier': 'zip' if row['storage_zip'] else 'hot', 'compressedAt': row['storage_archived_at']},
                      documents=self.documents.metadata(row['id']))
        from .code_context import snapshot, stage
        result.update(snapshot(row))
        result['problemsCount'] = metadata['problems_count']
        if row['storage_zip']:
            result.update(currentResult=None, resultsCount=self.db.connection.execute('SELECT COUNT(*) FROM results WHERE issue_id=?', (row['id'],)).fetchone()[0], criticReview=None)
        else:
            result.update(self.results.project(row['id'], details=details, compact=compact, issue_row=row))
        merged_head = row['merged_head'] if 'merged_head' in row.keys() else None
        result['mergeState'] = ('merged' if row['branch'] and merged_head == row['reviewed_head'] else
                                'awaiting_approval' if row['branch'] and row['status'] == 'done' else
                                'awaiting_review' if row['branch'] else 'not_applicable')
        result['mergePending'] = result['mergeState'] == 'awaiting_approval'
        result['codeReviewStage'] = stage(result)
        from .lifecycle import project
        result['lifecycle'] = project(result)
        for name, column in (('branch', 'branch'), ('worktree', 'worktree'), ('baseBranch', 'base_branch'), ('base', 'base'),
                             ('head', 'head'), ('reviewedHead', 'reviewed_head'), ('reviewedBase', 'reviewed_base')):
            result[name] = row[column]
        links = self.db.connection.execute('''SELECT l.left_id,l.right_id,l.kind,i.id AS related_id,i.space_id,i.space_number,s.prefix
            FROM issue_links l JOIN issues i ON i.id=CASE WHEN l.left_id=? THEN l.right_id ELSE l.left_id END
            JOIN spaces s ON s.id=i.space_id WHERE l.left_id=? OR l.right_id=? ORDER BY l.kind,l.left_id,l.right_id LIMIT 201''',
            (row['id'], row['id'], row['id'])).fetchall()
        result['links'] = []
        result['linkedIssues'] = {}
        for link in links[:200]:
            outgoing = link['left_id'] == row['id']
            kind = 'blocked_by' if link['kind'] == 'blocks' and not outgoing else link['kind']
            result['links'].append({'issueId': link['right_id'] if outgoing else link['left_id'], 'kind': kind})
            result['linkedIssues'][link['related_id']] = {'key': f"{link['prefix']}-{link['space_number']}", 'spaceId': link['space_id']}
        result['linksCount'] = self.db.connection.execute('SELECT COUNT(*) FROM issue_links WHERE left_id=? OR right_id=?', (row['id'], row['id'])).fetchone()[0]
        if compact:
            for definition in ('dor', 'dod'):
                items = result[definition]
                result[definition + 'Count'] = len(items)
                result[definition + 'PassedCount'] = sum(item['passed'] for item in items)
                result[definition + 'Truncated'] = len(items) > 3 or any(len(item['text']) > 128 or len(item['evidence']) > 128 for item in items[:3])
                result[definition] = [{**item, 'text': item['text'][:128], 'evidence': item['evidence'][:128]} for item in items[:3]]
            result['descriptionTruncated'] = len(result['description']) > 512
            result['description'] = result['description'][:512]
            result['externalRefsCount'] = len(result['externalRefs'])
            result['externalRefs'] = result['externalRefs'][:2]
            result['externalRefsTruncated'] = result['externalRefsCount'] > len(result['externalRefs'])
            result['links'] = result['links'][:10]
        result['linkedIssues'] = {link['issueId']: result['linkedIssues'][link['issueId']] for link in result['links']}
        result['linksTruncated'] = result['linksCount'] > len(result['links'])
        if details:
            result['conversations'] = self.service.conversations.list(20, 0, row['id'])
            from .documents import render_markdown
            result['descriptionHtml'] = render_markdown(row['description'])
            result['problems'] = self.planning.problems(row['id'])
            result['aliases'] = [item[0] for item in self.db.connection.execute('SELECT key FROM issue_keys WHERE issue_id=? ORDER BY key LIMIT 100', (row['id'],))]
            for table, field in (('comments', 'comments'), ('history', 'history')):
                rows = self.db.connection.execute(f'SELECT * FROM {table} WHERE issue_id=? ORDER BY id DESC LIMIT 20', (row['id'],)).fetchall()
                entries = []
                size = 0
                for item in rows:
                    entry = {'id': str(item['id']), 'actor': item['actor'], 'createdAt': item['created_at']}
                    if table == 'comments':
                        entry['text'] = item['text']
                    else:
                        entry.update(action=item['action'], data=json.loads(item['payload']))
                    amount = len(json.dumps(entry, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
                    maximum = 150000 if table == 'comments' else 400000
                    if size + amount > maximum:
                        break
                    size += amount
                    entries.append(entry)
                result[field] = list(reversed(entries))
                result[field + 'Count'] = self.db.connection.execute(f'SELECT COUNT(*) FROM {table} WHERE issue_id=?', (row['id'],)).fetchone()[0]
                result[field + 'Truncated'] = result[field + 'Count'] > len(entries)
        return result

    def list(self, status=None, parent_id=None, repo_id=None, limit=200, offset=0, space_id=None, include_archived=False):
        """Return a compact filtered page, newest first; metadata is bounded independently of audit volume."""
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or not 0 <= offset <= 9223372036854775807:
            raise DomainError('invalid_page', 'Invalid issue pagination.')
        self.execution.expire_all()
        where, parameters = self._list_filters(status, parent_id, repo_id, space_id, include_archived)
        with self.db.lock:
            rows = self.db.connection.execute('SELECT ' + ISSUE_COLUMNS + ' FROM issues' + where + ' ORDER BY number DESC LIMIT ? OFFSET ?', (*parameters, limit, offset)).fetchall()
            from .lifecycle import child_progress
            progress = child_progress(self.db.connection, rows)
            result, size = [], 0
            for row in rows:
                item = self._issue(row, compact=True, child_progress=progress.get(row['id'], {}))
                amount = len(json.dumps(item, ensure_ascii=True, separators=(',', ':')).encode('ascii'))
                if result and size + amount > 1000000:
                    break
                result.append(item)
                size += amount
            return result

    def _list_filters(self, status, parent_id, repo_id, space_id, include_archived):
        """Share the exact fixed-column filters between a projected page and its existence continuation probe."""
        clauses, parameters = ['deleted_at IS NULL'], []
        if not include_archived:
            clauses.append('archived_at IS NULL')
        for column, value in (('status', status), ('parent_id', parent_id), ('repo_id', repo_id), ('space_id', space_id)):
            if value is not None:
                bounded_text(value, 128, empty=False)
                if column == 'status' and value not in STATUSES:
                    raise DomainError('invalid_status', 'Invalid issue status filter.')
                clauses.append(column + '=?')
                parameters.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return where, parameters

    def has_more(self, *, status=None, parent_id=None, repo_id=None, offset=0, space_id=None, include_archived=False):
        """Check the next ordered matching row without decoding evidence or projecting another issue."""
        if type(offset) is not int or not 0 <= offset <= 9223372036854775807:
            raise DomainError('invalid_page', 'Invalid issue pagination.')
        self.execution.expire_all()
        where, parameters = self._list_filters(status, parent_id, repo_id, space_id, include_archived)
        with self.db.lock:
            row = self.db.connection.execute('SELECT 1 FROM issues' + where + ' ORDER BY number DESC LIMIT 1 OFFSET ?', (*parameters, offset)).fetchone()
            return row is not None

    def get(self, issue_id):
        """Read one issue with the latest twenty byte-bounded comments/audit entries and total counts; history remains retained."""
        with self.db.lock:
            identifier = self._row(issue_id)['id']
            self.execution.expire(identifier)
            return self._issue(self._row(identifier), details=True)

    def _audit(self, connection, issue_id, action, actor, data):
        """Append an audit record in the same transaction as its domain mutation."""
        from .aging import HOUSEKEEPING, touch
        if action not in HOUSEKEEPING:
            touch(connection, issue_id, timestamp())
        connection.execute('INSERT INTO history(issue_id,action,actor,payload,created_at) VALUES(?,?,?,?,?)',
                           (issue_id, action, bounded_text(actor, 128, empty=False), json.dumps(data, ensure_ascii=False), timestamp()))

    def _validate(self, values, current, connection, actor):
        """Validate mutable fields, parent ancestry and workflow-controlled status transitions before writing."""
        allowed = {'title', 'description', 'status', 'priority', 'parentId', 'assignee', 'dueDate', 'repoId', 'externalRefs', 'spaceId', 'intendedRole', 'dor', 'dod', 'kind', 'estimatedComplexity', 'codeTask', 'languages', 'systems'}
        if type(values) is not dict or set(values) - allowed:
            raise DomainError('invalid_fields', 'Unsupported issue fields.')
        result = dict(current)
        result.update(values)
        if 'dueDate' in values:
            from .scheduling import due_date
            result['dueDate'] = due_date(values['dueDate'])
        kind = result.get('kind', 'task')
        if kind not in ('task', 'rnd') or (current.get('id') and kind != current.get('kind')):
            raise DomainError('invalid_kind', 'Choose task or rnd at creation; generated review tasks use their own workflow.')
        result['kind'] = kind
        from .planning import assessment
        if 'estimatedComplexity' in values:
            result['estimatedComplexity'] = assessment(values['estimatedComplexity'], actor)
        else:
            result.setdefault('estimatedComplexity', None)
        result['title'] = bounded_text(result.get('title'), 256, empty=False)
        result['description'] = bounded_text(result.get('description', ''), 16384)
        result['assignee'] = bounded_text(result.get('assignee'), 128, nullable=True)
        if result.get('status', 'backlog') not in STATUSES or result.get('priority', 'normal') not in PRIORITIES:
            raise DomainError('invalid_field', 'Invalid status or priority.')
        result.setdefault('status', 'backlog')
        result.setdefault('priority', 'normal')
        result['externalRefs'] = external_refs(result.get('externalRefs', []))
        result['parentId'] = bounded_text(result.get('parentId'), 128, nullable=True, empty=False)
        if result['parentId']:
            result['parentId'] = self._row(result['parentId'], connection)['id']
        result['repoId'] = bounded_text(result.get('repoId'), 128, nullable=True, empty=False)
        from .code_context import validate
        result.update(validate(result))
        from .criteria import ROLES, criteria
        role = result.get('intendedRole')
        if role is not None and (type(role) is not str or role not in ROLES):
            raise DomainError('invalid_role', 'Choose a supported intended role.')
        result['intendedRole'] = role
        result['dor'], result['dod'] = criteria(result.get('dor', [])), criteria(result.get('dod', []))
        if result.get('spaceId') is None:
            result['spaceId'] = self._row(result['parentId'], connection)['space_id'] if result['parentId'] else 'default'
        result['spaceId'] = bounded_text(result['spaceId'], 128, empty=False)
        self.spaces.validate_issue_space(current.get('id'), result['spaceId'], result['parentId'], connection)
        parent, visited = result['parentId'], {current.get('id')}
        if parent and (not current.get('id') or parent != current.get('parentId')):
            self.execution.editable(self._row(parent, connection))
            if current.get('id'):
                self._ensure_scheduling_edge(current['id'], parent, connection)
        while parent is not None:
            if parent in visited:
                raise DomainError('hierarchy_cycle', 'Issue hierarchy cannot contain cycles.')
            visited.add(parent)
            parent = self._row(parent, connection)['parent_id']
        if result['repoId'] is not None and connection.execute('SELECT id FROM repositories WHERE id=?', (result['repoId'],)).fetchone() is None:
            raise DomainError('repository_not_found', 'Repository not found.', 404)
        if current.get('branch') and result['repoId'] != current.get('repoId'):
            raise DomainError('workflow_active', 'A prepared issue cannot change repositories.', 409)
        changed_status = result['status'] != current.get('status')
        if current.get('status') == 'stale' and changed_status:
            raise DomainError('revalidation_required', 'A human must explicitly revalidate stale work before changing its status.', 409)
        if result['status'] == 'stale' and current.get('status') in ('done', 'cancelled'):
            raise DomainError('issue_closed', 'Reopen terminal work before marking it stale.', 409)
        if not current.get('id') and result['status'] in ('in_review', 'done'):
            raise DomainError('result_required', 'New tasks cannot bypass explicit work, results and review.', 409)
        if values.get('status') == 'in_progress':
            raise DomainError('work_start_required', 'Start an explicit work session instead of patching status.', 409)
        if changed_status and result['status'] in ('done', 'cancelled') and actor != 'user':
            raise DomainError('human_review_required', 'Only a human can complete or cancel an issue.', 403)
        if result['repoId'] and changed_status:
            if result['status'] == 'done':
                raise DomainError('merge_required', 'Repository issues finish only after an approved merge.', 409)
            if result['status'] == 'in_review' and not current.get('reviewedHead'):
                raise DomainError('review_required', 'Capture a git review before requesting review status.', 409)
        if current.get('id') and changed_status and result['status'] in ('in_review', 'done'):
            if any(result[key] != current[key] for key in ('title', 'description', 'intendedRole', 'dor', 'dod', 'codeTask', 'languages', 'systems')):
                raise DomainError('definition_changed', 'Save definition changes before requesting completion/review.', 409)
            self.results.guard(current['id'], connection, complete=result['status'] == 'done', proposed=result)
        return result

    def _ensure_unblocked(self, issue_id, connection=None):
        """A blocks B: B cannot start until every incoming blocker is done or cancelled."""
        connection = connection or self.db.connection
        blocker = connection.execute("SELECT 1 FROM issue_links JOIN issues ON issues.id=issue_links.left_id WHERE issue_links.right_id=? AND issue_links.kind='blocks' AND issues.status NOT IN ('done','cancelled') LIMIT 1", (issue_id,)).fetchone()
        if blocker is not None:
            raise DomainError('issue_blocked', 'Resolve incoming blocking issues before starting this issue.', 409)

    def _ensure_scheduling_edge(self, prerequisite, dependent, connection):
        """Reject mixed prerequisite cycles: parents finish after ordinary children/current critic, not before them.

        Composition is drawn parent-to-child, while its completion dependency runs
        in the opposite direction. Related/duplicate visual links do not schedule work.
        """
        pending, visited = [prerequisite], set()
        while pending:
            identifier = pending.pop()
            if identifier == dependent:
                raise DomainError('dependency_cycle', 'Scheduling prerequisites cannot contain a cycle.', 409)
            if identifier in visited:
                continue
            visited.add(identifier)
            rows = connection.execute('''SELECT left_id AS id FROM issue_links WHERE right_id=? AND kind='blocks'
                UNION SELECT id FROM issues WHERE parent_id=? AND kind!='review' AND deleted_at IS NULL
                UNION SELECT critic_review_id AS id FROM issues WHERE id=? AND critic_review_id IS NOT NULL''',
                (identifier, identifier, identifier)).fetchall()
            pending.extend(row['id'] for row in rows)

    def _insert(self, data, actor, connection):
        """Create task metadata under an existing transaction, reserving a never-reused per-space key."""
        from .prefixes import allocate, issue_key, remember_key
        data = self.service.projects.resolve_creation(data, actor, connection)
        values = self._validate(data, {}, connection, actor)
        identifier, now = uuid4().hex, timestamp()
        number = allocate(connection, values['spaceId'])
        connection.execute('INSERT INTO issues(id,title,description,status,priority,parent_id,assignee,repo_id,external_refs,created_at,updated_at,space_id,intended_role,dor,dod,kind,estimated_complexity,space_number) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier, values['title'], values['description'], values['status'], values['priority'], values['parentId'], values['assignee'], values['repoId'], json.dumps(values['externalRefs']), now, now,
                            values['spaceId'], values['intendedRole'], json.dumps(values['dor']), json.dumps(values['dod']), values['kind'], json.dumps(values['estimatedComplexity']) if values['estimatedComplexity'] else None, number))
        connection.execute('UPDATE issues SET code_task=?,languages=?,systems=? WHERE id=?',
                           (int(values['codeTask']), json.dumps(values['languages']), json.dumps(values['systems']), identifier))
        remember_key(connection, identifier, issue_key(connection, self._row(identifier, connection)))
        if 'dueDate' in values:
            self.scheduling.write(connection, identifier, values['dueDate'])
        self._audit(connection, identifier, 'created', actor, values)
        return identifier

    def create(self, data, actor='agent'):
        """Create one task atomically; branch preparation, plan decomposition and work start remain separate actions."""
        with self.db.transaction() as connection:
            identifier = self._insert(data, actor, connection)
        return self.get(identifier)

    def definition_changed(self, connection, issue, reason):
        """Invalidate acceptance when a plan changes, stopping current work rather than attributing it to a new definition."""
        self.results.supersede(connection, issue['id'], reason, issue['result_generation'] + 1)
        session = connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (issue['id'],)).fetchone()
        if session:
            self.execution._close(connection, session)
        connection.execute("UPDATE issues SET definition_revision=definition_revision+1,status=CASE WHEN status IN ('done','in_review','in_progress') THEN 'rework' ELSE status END WHERE id=?", (issue['id'],))

    def update(self, issue_id, patch, actor='agent'):
        """Update explicit domain fields; agents cannot alter workflow hashes or bypass independent review/merge approval."""
        with self.db.transaction() as connection:
            current = self._issue(self._row(issue_id, connection))
            if current['archivedAt'] or current['kind'] == 'review':
                raise DomainError('issue_read_only', 'Restore the task or use its dedicated critic workflow.', 409)
            values = self._validate(patch, current, connection, actor)
            definition_changed = any(values[key] != current[key] for key in ('title', 'description', 'intendedRole', 'dor', 'dod', 'codeTask', 'languages', 'systems'))
            if values['status'] == 'stale' and current['status'] != 'stale':
                self.planning.mark_stale(connection, issue_id, actor)
            if definition_changed:
                self.results.supersede(connection, issue_id, 'definition_changed', current['resultGeneration'] + 1)
            if definition_changed and values['status'] in ('done', 'in_review', 'in_progress'):
                values['status'] = 'rework'
            if values['status'] != 'in_progress':
                session = connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone()
                if session:
                    self.execution._close(connection, session)
            from .prefixes import allocate, issue_key, remember_key
            number = allocate(connection, values['spaceId']) if values['spaceId'] != current['spaceId'] else self._row(issue_id, connection)['space_number']
            connection.execute('UPDATE issues SET space_number=?,estimated_complexity=? WHERE id=?', (number, json.dumps(values['estimatedComplexity']) if values['estimatedComplexity'] else None, issue_id))
            connection.execute('UPDATE issues SET title=?,description=?,status=?,priority=?,parent_id=?,assignee=?,repo_id=?,external_refs=?,updated_at=?,space_id=?,intended_role=?,dor=?,dod=?,definition_revision=definition_revision+? WHERE id=?',
                               (values['title'], values['description'], values['status'], values['priority'], values['parentId'], values['assignee'], values['repoId'], json.dumps(values['externalRefs']), timestamp(),
                                values['spaceId'], values['intendedRole'], json.dumps(values['dor']), json.dumps(values['dod']), int(definition_changed), issue_id))
            connection.execute('UPDATE issues SET code_task=?,languages=?,systems=? WHERE id=?',
                               (int(values['codeTask']), json.dumps(values['languages']), json.dumps(values['systems']), issue_id))
            remember_key(connection, issue_id, issue_key(connection, self._row(issue_id, connection)))
            if 'dueDate' in patch:
                self.scheduling.write(connection, issue_id, values['dueDate'])
            self._audit(connection, issue_id, 'updated', actor, patch)
            from .completion import complete_ready, invalidate_ancestors
            if definition_changed or current['status'] == 'done' and values['status'] != 'done':
                invalidate_ancestors(self, connection, self._row(issue_id, connection), 'child_definition_or_status_changed')
            complete_ready(self, connection, issue_id)
        return self.get(issue_id)

    def link(self, issue_id, related_id, kind='related', actor='agent'):
        """Add related/duplicate symmetry or directed A-blocks-B, rejecting dependency cycles and self-links."""
        if issue_id == related_id or kind not in ('related', 'blocks', 'duplicates'):
            raise DomainError('invalid_link', 'Invalid issue relation.')
        with self.db.transaction() as connection:
            self._row(issue_id, connection)
            source, related = self._row(issue_id, connection), self._row(related_id, connection)
            issue_id, related_id = source['id'], related['id']
            if kind == 'blocks':
                self._ensure_scheduling_edge(issue_id, related_id, connection)
                left, right = issue_id, related_id
            else:
                left, right = sorted((issue_id, related_id))
            changed = connection.execute('INSERT OR IGNORE INTO issue_links VALUES(?,?,?)', (left, right, kind)).rowcount
            if changed:
                self._audit(connection, issue_id, 'linked', actor, {'issueId': related_id, 'kind': kind})
                self._audit(connection, related_id, 'linked', actor, {'issueId': issue_id, 'kind': kind})
        return self.get(issue_id)

    def comment(self, issue_id, text, actor='agent'):
        """Append a bounded comment and audit entry; existing text is never silently overwritten."""
        text = bounded_text(text, 8192, empty=False)
        with self.db.transaction() as connection:
            self._row(issue_id, connection)
            connection.execute('INSERT INTO comments(issue_id,text,actor,created_at) VALUES(?,?,?,?)', (issue_id, text, bounded_text(actor, 128, empty=False), timestamp()))
            self._audit(connection, issue_id, 'commented', actor, {'length': len(text)})
        return self.get(issue_id)

    def repos(self):
        """List only repositories explicitly registered by a human; callers cannot select paths through issue IDs."""
        with self.db.lock:
            return [self._repo(row) for row in self.db.connection.execute('SELECT * FROM repositories ORDER BY created_at,id')]

    @staticmethod
    def _repo(row):
        """Project the stable repository identifier, validated path and chosen local base branch."""
        return {'id': row['id'], 'path': row['path'], 'name': row['name'], 'baseBranch': row['base_branch'], 'createdAt': row['created_at']}

    def repo(self, repo_id):
        """Resolve an allowlisted repository; no fallback from unknown IDs to filesystem paths is permitted."""
        bounded_text(repo_id, 128, empty=False)
        with self.db.lock:
            row = self.db.connection.execute('SELECT * FROM repositories WHERE id=?', (repo_id,)).fetchone()
            if row is None:
                raise DomainError('repository_not_found', 'Repository not found.', 404)
            return self._repo(row)

    def register_repo(self, path, name=None, base_branch=None):
        """Human-only adapter entry: validate a local committed Git root before storing its opaque repo ID."""
        from .git_workflow import inspect_repo
        verified = inspect_repo(path, base_branch)
        name = bounded_text(verified['name'] if name is None else name, 128, empty=False)
        with self.db.transaction() as connection:
            existing = connection.execute('SELECT * FROM repositories WHERE path=?', (verified['path'],)).fetchone()
            if existing is not None:
                if existing['base_branch'] != verified['baseBranch']:
                    raise DomainError('repository_conflict', 'Repository is already registered with a different base.', 409)
                return self._repo(existing)
            identifier = uuid4().hex
            connection.execute('INSERT INTO repositories VALUES(?,?,?,?,?)', (identifier, verified['path'], name, verified['baseBranch'], timestamp()))
        return self.repo(identifier)

    def _record_workflow(self, issue_id, patch, action, actor='system'):
        """Internal GitWorkflow-only update: persist verified metadata/status and its audit after real git operations."""
        columns = {'branch': 'branch', 'worktree': 'worktree', 'baseBranch': 'base_branch', 'base': 'base', 'head': 'head',
                   'reviewedHead': 'reviewed_head', 'reviewedBase': 'reviewed_base', 'mergedHead':'merged_head', 'status': 'status'}
        if not patch or set(patch) - columns.keys():
            raise DomainError('invalid_workflow', 'Invalid workflow metadata.')
        with self.db.transaction() as connection:
            self._row(issue_id, connection)
            if patch.get('status') == 'in_progress':
                raise DomainError('work_start_required', 'Only explicit agent work starts an issue.', 409)
            if patch.get('status') in ('in_review', 'done'):
                self.results.guard(issue_id, connection, complete=patch['status'] == 'done')
            assignments = [columns[key] + '=?' for key in patch]
            connection.execute('UPDATE issues SET ' + ','.join(assignments) + ',updated_at=? WHERE id=?', (*patch.values(), timestamp(), issue_id))
            self._audit(connection, issue_id, action, actor, patch)
        return self.get(issue_id)
