"""Project the fields needed by Kanban cards from current task evidence."""

import json

from .code_context import snapshot
from .issues import DomainError
from .lifecycle import child_progress, project
from .prefixes import issue_key


def page(store, *, status, parent_id, repo_id, space_id, include_archived, limit, offset):
    """Read a bounded Kanban page from the owner store after reconciling leases.

    Status, parent, repository and space select the same records as the agent list.
    Archived tasks are included only on request. Limit is 1–200, and offset counts
    rows in newest-first order. Return card items and the next offset, if any.
    """
    if not 1 <= limit <= 200 or not 0 <= offset <= 9223372036854775807:
        raise DomainError('invalid_page', 'Invalid issue pagination.')
    where, params = store._list_filters(status, parent_id, repo_id, space_id, include_archived)
    store.execution.expire_all()
    with store.db.lock:
        connection = store.db.connection
        rows = connection.execute('''
            SELECT id, title, status, priority, parent_id, assignee, space_id, space_number,
                   kind, intended_role, archived_at, deleted_at, branch, merged_head, reviewed_head,
                   storage_zip, code_task, languages, systems, estimated_complexity, actual_complexity,
                   critic_review_id, definition_revision,
                   (SELECT prefix FROM spaces WHERE id=issues.space_id) AS space_prefix,
                   (SELECT name FROM spaces WHERE id=issues.space_id) AS space_name
            FROM issues
        ''' + where + ' ORDER BY number DESC LIMIT ? OFFSET ?', (*params, limit + 1, offset)).fetchall()
        visible = rows[:limit]
        progress = child_progress(connection, visible)
        items = [card(store, row, progress.get(row['id'], {})) for row in visible]
        more = len(rows) > limit
        return {'items': items, 'hasMore': more, 'nextOffset': offset + len(items) if more else None}


def card(store, row, counts):
    """Build a card from one selected row and its direct-child status counts.

    The owner store supplies live work and current review evidence under its lock.
    Return display fields and the shared lifecycle phase without result bodies.
    """
    work = store.execution.project(row['id'])
    cold = bool(row['storage_zip'])
    issue = {
        'id': row['id'], 'key': issue_key(store.db.connection, row), 'title': row['title'],
        'status': row['status'], 'priority': row['priority'], 'parentId': row['parent_id'],
        'assignee': row['assignee'], 'spaceId': row['space_id'], 'spaceName': row['space_name'],
        'kind': row['kind'], 'intendedRole': row['intended_role'], 'archivedAt': row['archived_at'],
        'deletedAt': row['deleted_at'], 'storage': {'tier': 'zip' if cold else 'hot'},
        'estimatedComplexity': json.loads(row['estimated_complexity']) if row['estimated_complexity'] else None,
        'actualComplexity': json.loads(row['actual_complexity']) if row['actual_complexity'] else None,
        'mergePending': bool(row['branch'] and row['status'] == 'done' and row['merged_head'] != row['reviewed_head']),
        **snapshot(row),
    }
    evidence = {} if cold else store.results.project(row['id'], compact=True, issue_row=row)
    issue['lifecycle'] = project({**issue, **evidence, 'work': work, 'childProgress': {'byStatus': counts}})
    issue['work'] = {key: work.get(key) for key in ('state', 'elapsedMs', 'nextAction')}
    return issue
