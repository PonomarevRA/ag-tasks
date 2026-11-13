"""Allowlisted execution facts stored atomically with existing task history."""

import json

from .review_queue import handoff
from service_telemetry.technical_logs.events import context_fields, safe_text

FIELDS = ('sessionId', 'agentId', 'fromStatus', 'toStatus', 'reason', 'generation',
          'definitionRevision', 'criticReviewId', 'parentId', 'assignmentState', 'reviewerId')
ACTIONS = (
    'work_started', 'work_stopped', 'work_expired', 'result_submitted', 'critic_review_created',
    'critic_superseded', 'critic_reported', 'review_assigned', 'review_assignment_released',
    'review_handoff_failed', 'auto_completed', 'auto_stale', 'completion_definition_failed',
    'readiness_failed',
)


def migrate(connection):
    """Keep the bounded facts beside history so ZIP compression never hides or restores them."""
    columns = {row['name'] for row in connection.execute('PRAGMA table_info(history)')}
    if 'execution_diagnostic' not in columns:
        connection.execute('ALTER TABLE history ADD COLUMN execution_diagnostic TEXT')


def snapshot(connection, task_id, action, actor, data):
    """Copy fixed domain fields and correlate only to the currently executing HTTP operation."""
    if action not in ACTIONS:
        return None
    facts = {name: safe_text(data[name], 128) if isinstance(data[name], str) else data[name]
             for name in FIELDS if name in data}
    critic = connection.execute("SELECT parent_id FROM issues WHERE id=? AND kind='review'", (task_id,)).fetchone()
    if critic and critic['parent_id']:
        parent = connection.execute('SELECT result_generation,definition_revision,critic_review_id FROM issues WHERE id=?',
                                    (critic['parent_id'],)).fetchone()
        facts.setdefault('criticReviewId', task_id)
        facts.setdefault('parentId', critic['parent_id'])
        if parent and parent['critic_review_id'] == task_id:
            facts.setdefault('generation', parent['result_generation'])
            facts.setdefault('definitionRevision', parent['definition_revision'])
    if 'criticReviewId' in facts and 'assignmentState' not in facts:
        facts['assignmentState'] = handoff(connection, facts['criticReviewId'])['state']
    facts.update(operationId=(context_fields.get() or {}).get('operationId'), source=actor,
                 identityQuality='declared' if 'agentId' in facts or 'reviewerId' in facts else 'process')
    return json.dumps(facts, ensure_ascii=False)


def read(store, identifier, *, limit, before):
    """Page committed facts across one task and its own generated critic history."""
    with store.db.lock:
        issue = store._row(identifier, restore=False)
        root = issue['parent_id'] if issue['kind'] == 'review' else issue['id']
        names = ','.join('?' for _ in ACTIONS)
        rows = store.db.connection.execute(f"""SELECT h.id,h.issue_id,h.action,h.created_at,h.execution_diagnostic
            FROM history h JOIN issues i ON i.id=h.issue_id
            WHERE (i.id=? OR (i.parent_id=? AND i.kind='review')) AND h.id<?
            AND h.action IN ({names}) ORDER BY h.id DESC LIMIT ?""",
            (root, root, before or 2**63-1, *ACTIONS, limit + 1)).fetchall()
        items = [{'id': row['id'], 'taskId': row['issue_id'], 'action': row['action'],
                  'occurredAt': row['created_at'],
                  'facts': json.loads(row['execution_diagnostic']) if row['execution_diagnostic'] else None}
                 for row in rows[:limit]]
        more = len(rows) > limit
        return {'issueId': root, 'items': items, 'hasMore': more,
                'nextBefore': items[-1]['id'] if more else None}
