"""Read-only task snapshots and recorded completion flow, independent of effort accounting."""

from datetime import UTC, datetime, timedelta

from .config import STATUSES
from .work_analytics import _window

ATTENTION_LIMIT = 10
ATTENTION_STATUSES = {'blocked', 'rework', 'stale', 'in_review'}


def _scope_rows(connection, space_id, kind, query):
    """Read current metadata and lease/problem evidence without restoring issue bodies."""
    rows = connection.execute('''
        WITH running AS (
            SELECT issue_id,lease_expires_at FROM work_sessions WHERE state='running'
        ), open_problems AS (
            SELECT issue_id,COUNT(*) AS amount,
                   MAX(CASE WHEN severity IN ('high','critical') THEN 1 ELSE 0 END) AS severe
              FROM problems WHERE state='open' GROUP BY issue_id
        )
        SELECT i.id,i.title,i.status,i.kind,i.created_at,i.last_activity_at,i.archived_at,
               s.prefix || '-' || i.space_number AS key,
               r.lease_expires_at,
               COALESCE(p.amount,0) AS open_problems,COALESCE(p.severe,0) AS severe_problem
          FROM issues i JOIN spaces s ON s.id=i.space_id
          LEFT JOIN running r ON r.issue_id=i.id
          LEFT JOIN open_problems p ON p.issue_id=i.id
         WHERE i.deleted_at IS NULL AND (?='' OR i.space_id=?) AND (?='' OR i.kind=?)
    ''', (space_id, space_id, kind, kind)).fetchall()
    needle = query.strip().casefold()
    return [row for row in rows if needle in row['key'].casefold() or needle in row['title'].casefold()]


def _attention(row, expired):
    """Explain each distinct risky card and put expired ownership and blockers first."""
    reasons = []
    if row['status'] in ATTENTION_STATUSES:
        reasons.append(row['status'])
    if expired:
        reasons.append('expired_session')
    if row['open_problems']:
        reasons.append('open_problem')
    if not reasons:
        return None
    rank = 3
    if expired:
        rank = 0
    elif row['status'] == 'blocked':
        rank = 1
    elif row['severe_problem']:
        rank = 2
    item = {'id': row['id'], 'key': row['key'], 'title': row['title'], 'status': row['status'],
            'reasons': reasons, 'lastActivityAt': row['last_activity_at']}
    activity = _instant(row['last_activity_at'] or row['created_at'])
    return (rank, activity, row['key'], row['id']), item


def _snapshot(rows, as_of):
    """Count all current cards before limiting the deterministic attention list to ten."""
    statuses = dict.fromkeys(STATUSES, 0)
    total, active, problems = 0, 0, 0
    attention = []
    for row in rows:
        if row['archived_at']:
            continue
        total += 1
        statuses[row['status']] = statuses.get(row['status'], 0) + 1
        expires = _instant(row['lease_expires_at']) if row['lease_expires_at'] else None
        active += bool(expires and expires > as_of)
        problems += row['open_problems']
        candidate = _attention(row, expired=bool(expires and expires <= as_of))
        if candidate:
            attention.append(candidate)
    attention.sort(key=lambda candidate: candidate[0])
    return {'totalCount': total, 'activeSessionCount': active,
            'reviewCount': statuses['in_review'], 'attentionCount': len(attention),
            'openProblemCount': problems,
            'statuses': [{'id': status, 'count': count} for status, count in statuses.items()],
            'attention': [item for _, item in attention[:ATTENTION_LIMIT]], 'attentionLimit': ATTENTION_LIMIT}


def _instant(value):
    """Interpret stored timestamps in UTC while preserving microsecond boundary precision."""
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC)


def _events(connection, rows, start, end):
    """Count creations and unique task/day completions; archived cards retain recorded flow."""
    days = {}
    cursor = start
    while cursor < end:
        days[cursor.date().isoformat()] = {'createdCount': 0, 'completedCount': 0}
        cursor += timedelta(days=1)
    identifiers = {row['id'] for row in rows}
    created_count = 0
    for row in rows:
        created = _instant(row['created_at'])
        if start <= created < end:
            days[created.date().isoformat()]['createdCount'] += 1
            created_count += 1

    # SQLite rounds timestamps to milliseconds. A widened SQL prefilter keeps
    # near-midnight candidates; exact Python comparisons decide membership.
    lower = start - timedelta(milliseconds=1) if start.year > 1 else start
    upper = end + timedelta(milliseconds=1) if end.year < 9999 else end
    completions = connection.execute('''
        SELECT issue_id,created_at FROM history WHERE action='auto_completed'
           AND julianday(created_at)>=julianday(?) AND julianday(created_at)<=julianday(?)
    ''', (lower.isoformat(), upper.isoformat())).fetchall()
    task_days, completed_ids = set(), set()
    for event in completions:
        if event['issue_id'] not in identifiers:
            continue
        completed = _instant(event['created_at'])
        if not start <= completed < end:
            continue
        key = event['issue_id'], completed.date().isoformat()
        if key not in task_days:
            task_days.add(key)
            days[key[1]]['completedCount'] += 1
        completed_ids.add(event['issue_id'])

    weeks = {}
    by_day = []
    for label, counts in days.items():
        by_day.append({'label': label, **counts})
        day = datetime.fromisoformat(label)
        week = (day - timedelta(days=day.weekday())).date().isoformat()
        weekly = weeks.setdefault(week, {'createdCount': 0, 'completedCount': 0})
        # Weekly counts sum task/day events, rather than silently deduplicating
        # a task that was completed again on a later day after being reopened.
        weekly['createdCount'] += counts['createdCount']
        weekly['completedCount'] += counts['completedCount']
    return {'byDay': by_day, 'byWeek': [{'label': label, **counts} for label, counts in weeks.items()],
            'createdCount': created_count, 'completedUniqueCount': len(completed_ids)}


def execution_analytics(db, start_date, end_date, space_id='', kind='', query='', *, now=None):
    """Return a current UTC snapshot and recorded period events using only locked SELECTs.

    Snapshot ignores the selected dates and excludes archived/deleted cards.
    Events include archived cards, exclude deleted cards, and use current
    project/type/title filters. No lease reconciliation or ZIP restoration runs.
    """
    start, end = _window(start_date, end_date)
    with db.lock:
        as_of = (now or datetime.now(UTC)).astimezone(UTC)
        rows = _scope_rows(db.connection, space_id, kind, query)
        return {'asOf': as_of.isoformat(), 'snapshot': _snapshot(rows, as_of),
                'events': _events(db.connection, rows, start, end)}
