"""Automatic task completion from actual review evidence; Git integration remains human-only."""

import json
from .criteria import failures
from .issues import DomainError, timestamp


def complete_ready(store, connection, issue_id):
    """Close eligible current work and ancestors in the caller transaction, without starting work or invoking Git."""
    visited = set()
    while issue_id and issue_id not in visited and len(visited) < 128:
        visited.add(issue_id)
        issue = store._row(issue_id, connection)
        if not issue['archived_at'] and issue['status'] not in ('done', 'cancelled', 'stale', 'blocked', 'in_progress'):
            if _aggregate_ready(store, connection, issue):
                _finish(store, connection, issue, 'reviewed_children')
            elif issue['status'] == 'in_review':
                try:
                    store.results.guard(issue_id, connection, complete=True)
                except DomainError as error:
                    if error.code not in {'children_unfinished', 'critic_review_required', 'result_required', 'stale_result', 'role_required', 'code_context_required'}:
                        raise
                else:
                    _finish(store, connection, issue, 'independent_critic')
        issue_id = issue['parent_id']


def _aggregate_ready(store, connection, issue):
    """A pure RnD container closes from reviewed children; explicit unmet criteria and actual parent code work still block it."""
    if issue['kind'] != 'rnd' or issue['result_generation'] or issue['branch']:
        return False
    criteria = json.loads(issue['dod'])
    if criteria and failures(criteria):
        return False
    rows = connection.execute("SELECT * FROM issues WHERE parent_id=? AND kind!='review' AND deleted_at IS NULL LIMIT 201", (issue['id'],)).fetchall()
    if not rows or len(rows) > 200 or not any(row['status'] == 'done' for row in rows):
        return False
    for child in rows:
        if child['status'] == 'cancelled':
            continue
        if child['status'] != 'done' or not _reviewed_child(store, connection, child):
            return False
    return True


def _reviewed_child(store, connection, child):
    """Legacy terminal status is not proof; require a current result review or our revision-bound aggregate audit."""
    if child['kind'] == 'rnd' and not child['result_generation']:
        audit = connection.execute("SELECT payload FROM history WHERE issue_id=? AND action='auto_completed' ORDER BY id DESC LIMIT 1", (child['id'],)).fetchone()
        return bool(audit and json.loads(audit[0]).get('definitionRevision') == child['definition_revision'])
    try:
        store.results.guard(child['id'], connection, complete=True)
    except DomainError:
        return False
    return True


def invalidate_ancestors(store, connection, issue, reason):
    """Reopened ordinary work invalidates ancestor acceptance; generated critic work is not a scope change."""
    if issue['kind'] == 'review':
        return
    parent_id, visited = issue['parent_id'], set()
    while parent_id and parent_id not in visited and len(visited) < 128:
        visited.add(parent_id)
        parent = store._row(parent_id, connection)
        if parent['status'] in ('done', 'in_review'):
            store.definition_changed(connection, parent, reason)
            store._audit(connection, parent_id, 'child_acceptance_invalidated', 'system', {'childId':issue['id'], 'reason':reason})
        parent_id = parent['parent_id']


def _finish(store, connection, issue, basis):
    """Record the completion basis without fabricating a producer result, critic vote, historical time or merge."""
    connection.execute("UPDATE issues SET status='done',updated_at=? WHERE id=?", (timestamp(), issue['id']))
    store._audit(connection, issue['id'], 'auto_completed', 'system', {'basis':basis, 'generation':issue['result_generation'], 'definitionRevision':issue['definition_revision'], 'mergeApproved':False, 'fromStatus':issue['status'], 'toStatus':'done', 'reason':basis})


def migrate(connection):
    """Persist verified merge receipts outside compressible history; infer nothing from a terminal task status."""
    connection.execute('BEGIN IMMEDIATE')
    try:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(issues)')}
        if 'merged_head' not in columns:
            connection.execute('ALTER TABLE issues ADD COLUMN merged_head TEXT')
            connection.execute("UPDATE issues SET merged_head=(SELECT json_extract(payload,'$.head') FROM history WHERE issue_id=issues.id AND action='merge_approved' ORDER BY id DESC LIMIT 1) WHERE branch IS NOT NULL")
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
