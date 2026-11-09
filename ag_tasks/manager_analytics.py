"""Read-only operational portfolio; current filters never masquerade as historical metrics."""

from datetime import UTC, datetime, timedelta

from .config import PRIORITIES, STATUSES
from .issues import DomainError

TERMINAL = {'done', 'cancelled'}
QUEUE_IDS = ('open', 'attention', 'unassigned', 'overdue', 'review', 'all')
PRIORITY_RANK = {'urgent': 0, 'high': 1, 'normal': 2, 'low': 3}


def _card(row, now):
    """
    Project one metadata row and classify current risk at the server instant.

    Args:
        row: A joined issue row, without restored descriptions or history bodies.
        now: A timezone-aware UTC observation instant.
    Returns:
        Display metadata, committed milliseconds and evidence-based risk flags.
    """
    open_card = row['status'] not in TERMINAL
    deadline = row['due_date']
    today = now.date().isoformat()
    reasons = []
    if row['status'] in {'blocked', 'rework', 'stale', 'in_review'}:
        reasons.append(row['status'])
    expires = datetime.fromisoformat(row['lease_expires_at']) if row['lease_expires_at'] else None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    active = bool(expires and expires > now)
    if expires and not active:
        reasons.append('expired_session')
    if row['problem_count']:
        reasons.append('open_problem')
    overdue = bool(open_card and deadline and deadline < today)
    if overdue:
        reasons.append('overdue')
    return {
        'id': row['id'], 'key': row['key'], 'title': row['title'], 'kind': row['kind'],
        'spaceId': row['space_id'], 'project': row['project'], 'status': row['status'],
        'priority': row['priority'], 'assignee': (row['assignee'] or '').strip() or None,
        'dueDate': deadline, 'lastActivityAt': row['last_activity_at'],
        'elapsedMs': row['elapsed_ms'], 'active': active,
        'activeAgent': row['agent_id'] if active else None,
        'leaseExpiresAt': row['lease_expires_at'] if active else None,
        'open': open_card, 'overdue': overdue,
        'dueSoon': bool(open_card and deadline and today <= deadline <= (now.date() + timedelta(days=7)).isoformat()),
        'unassigned': bool(open_card and not (row['assignee'] or '').strip()),
        'attention': bool(open_card and reasons), 'reasons': reasons,
    }


def _in_queue(card, queue):
    """
    Select a card by a named operational queue.

    Args:
        card: A projected card with risk flags.
        queue: A validated QUEUE_IDS member.
    Returns:
        True when the table's queue includes this card.
    """
    if queue == 'all':
        return True
    if queue == 'review':
        return card['status'] == 'in_review'
    return card[queue]


def _groups(cards, field):
    """
    Aggregate open workload over the full filtered scope before table pagination.

    Args:
        cards: The cards after explicit filters, before the table queue.
        field: spaceId or assignee; missing owners form an explicit group.
    Returns:
        Deterministically sorted project/owner groups with open, active and late counts.
    """
    groups = {}
    for card in cards:
        if not card['open']:
            continue
        key = card[field] or ''
        label = card['project'] if field == 'spaceId' else card['assignee'] or 'Без ответственного'
        group = groups.setdefault(key, {'id': key, 'label': label, 'openCount': 0, 'activeCount': 0, 'overdueCount': 0})
        group['openCount'] += 1
        group['activeCount'] += card['active']
        group['overdueCount'] += card['overdue']
    return sorted(groups.values(), key=lambda row: (-row['openCount'], row['label'].casefold(), row['id']))


def _activity(value):
    """
    Compare activity by UTC instants rather than timezone-offset text.

    Args:
        value: Stored ISO timestamp, or None for unknown activity.
    Returns:
        An aware UTC instant; unknown activity sorts before known instants.
    """
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC)


def management_analytics(db, *, space_id='', kind='task', status='', priority='', assignee='',
                         query='', queue='open', offset=0, limit=50, now=None):
    """
    Read a current operational snapshot without expiring leases or restoring ZIP bodies.

    Args:
        db: The service-owned Database; reads hold its lock for one consistent projection.
        space_id: Exact project ID; empty means all projects.
        kind: task by default; empty includes research and review cards explicitly.
        status: Exact saved status, or empty for all.
        priority: Exact saved priority, or empty for all.
        assignee: Exact trimmed owner label, or empty for all owners.
        query: Literal case-insensitive key/title substring.
        queue: Table-only queue. KPI and charts stay on the explicit filter scope.
        offset: Nonnegative position in the deterministic risk/priority/date order.
        limit: Page size, from one through one hundred.
        now: Optional aware UTC instant for deterministic tests; default is server time.
    Returns:
        Whole-scope KPI/facets/charts and one page of current nonarchived cards.
    Raises:
        DomainError: A named filter or pagination value is unsupported.
    """
    if (kind not in ('', 'task', 'rnd', 'review') or status and status not in STATUSES
            or priority and priority not in PRIORITIES or queue not in QUEUE_IDS):
        raise DomainError('invalid_manager_filter', 'Choose a supported management filter.')
    if not 1 <= limit <= 100 or not 0 <= offset <= 9223372036854775807:
        raise DomainError('invalid_page', 'Invalid management pagination.')
    with db.lock:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        rows = db.connection.execute('''
            SELECT i.id,i.title,i.kind,i.space_id,i.status,i.priority,i.assignee,i.last_activity_at,
                   s.prefix || '-' || i.space_number AS key,s.name AS project,m.due_date,
                   w.agent_id,w.lease_expires_at,COALESCE(e.elapsed_ms,0) AS elapsed_ms,
                   COALESCE(p.amount,0) AS problem_count
              FROM issues i JOIN spaces s ON s.id=i.space_id
              LEFT JOIN issue_management m ON m.issue_id=i.id
              LEFT JOIN work_sessions w ON w.issue_id=i.id AND w.state='running'
              LEFT JOIN (SELECT issue_id,SUM(elapsed_ms) AS elapsed_ms FROM work_sessions GROUP BY issue_id) e ON e.issue_id=i.id
              LEFT JOIN (SELECT issue_id,COUNT(*) AS amount FROM problems WHERE state='open' GROUP BY issue_id) p ON p.issue_id=i.id
             WHERE i.deleted_at IS NULL AND i.archived_at IS NULL
        ''').fetchall()
        all_cards = [_card(row, instant) for row in rows]
    needle = query.strip().casefold()
    cards = []
    for card in all_cards:
        if space_id and card['spaceId'] != space_id or kind and card['kind'] != kind:
            continue
        if status and card['status'] != status or priority and card['priority'] != priority:
            continue
        if assignee and card['assignee'] != assignee.strip():
            continue
        if needle not in card['key'].casefold() and needle not in card['title'].casefold():
            continue
        cards.append(card)
    summary = {'totalCount': len(cards)}
    for flag in ('open', 'overdue', 'dueSoon', 'unassigned', 'active', 'attention'):
        summary[flag + 'Count'] = sum(card[flag] for card in cards)
    summary['reviewCount'] = sum(card['status'] == 'in_review' for card in cards)
    selected = [card for card in cards if _in_queue(card, queue)]
    selected.sort(key=lambda card: (
        not card['attention'], PRIORITY_RANK[card['priority']], card['dueDate'] or '9999-12-31',
        _activity(card['lastActivityAt']), card['id']))
    projects, owners = {}, set()
    for card in all_cards:
        projects[card['spaceId']] = card['project']
        if card['assignee']:
            owners.add(card['assignee'])
    facets = {
        'projects': [{'id': key, 'label': value} for key, value in sorted(projects.items(), key=lambda pair: pair[1].casefold())],
        'assignees': [{'id': owner, 'label': owner} for owner in sorted(owners, key=str.casefold)],
        'kinds': ['task', 'rnd', 'review'], 'statuses': list(STATUSES), 'priorities': list(PRIORITIES),
    }
    items = selected[offset:offset + limit]
    more = offset + len(items) < len(selected)
    return {'asOf': instant.isoformat(), 'summary': summary, 'facets': facets,
            'queues': [{'id': name, 'count': sum(_in_queue(card, name) for card in cards)} for name in QUEUE_IDS],
            'byProject': _groups(cards, 'spaceId'), 'byAssignee': _groups(cards, 'assignee'),
            'items': items, 'totalCount': len(selected), 'hasMore': more,
            'nextOffset': offset + len(items) if more else None}
