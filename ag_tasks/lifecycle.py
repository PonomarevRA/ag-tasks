"""Evidence-derived execution phases, separate from human-owned completion and elapsed time."""


def project(issue):
    """Explain the next real gate; never infer work, review or approval from a comment or parent status."""
    status = issue['status']
    if status == 'done' and issue.get('mergePending'):
        return phase('awaiting_merge', 'human_merge', True, complete=True)
    if issue.get('archivedAt') or status in ('done', 'cancelled', 'stale', 'blocked'):
        return phase('archived' if issue.get('archivedAt') else status, 'none' if status in ('done', 'cancelled') else 'inspect', status in ('stale', 'blocked'))
    if issue.get('work', {}).get('activeSession'):
        return phase('working', 'submit_result_or_pause', False)
    if issue.get('storage', {}).get('tier') == 'zip':
        return phase('stored_cold', 'open_to_verify', False)
    if issue.get('kind') == 'review':
        return phase('review_pending', 'assign_or_start_independent_review', True)
    work_state = issue.get('work', {}).get('state')
    if status == 'rework' and work_state in ('expired', 'interrupted') and not issue.get('criticReview'):
        # Older owners stored lease loss as rework; explain it without rewriting history.
        return phase('lease_expired' if work_state == 'expired' else 'work_interrupted', 'resume_owned_work', True)
    if status == 'rework':
        return phase('rework', 'address_findings', True)
    counts = issue.get('childProgress', {}).get('byStatus', {})
    unfinished = sum(count for state, count in counts.items() if state not in ('done', 'cancelled'))
    if unfinished:
        if counts.get('in_progress'):
            return phase('children_working', 'wait_for_children', False)
        if counts.get('rework') or counts.get('blocked') or counts.get('stale'):
            return phase('children_need_attention', 'inspect_children', True)
        if counts.get('in_review'):
            return phase('children_review', 'review_children', True)
        return phase('children_pending', 'execute_children', True)
    if counts and issue.get('kind') == 'rnd' and not unfinished:
        return phase('completion_pending', 'verify_parent_criteria', True)
    current = issue.get('currentResult')
    if current and issue.get('currentResultIsCurrent', False):
        report = issue.get('criticReview')
        if report and report['verdict'] == 'pass':
            return phase('completion_pending', 'verify_completion_gates', True, complete=True)
        if report and report['verdict'] == 'rework':
            return phase('rework', 'address_review_findings', True)
        return phase('awaiting_critic', 'independent_review', True, complete=True)
    work = issue.get('work', {})
    if work.get('state') in ('expired', 'interrupted'):
        return phase('lease_expired' if work['state'] == 'expired' else 'work_interrupted', 'resume_owned_work', True)
    if work.get('state') == 'paused' and not current:
        return phase('paused', 'resume_or_report_result', True)
    if status == 'in_review' or current or issue.get('work', {}).get('elapsedMs', 0) or issue.get('work', {}).get('state') != 'never_started':
        return phase('result_missing', 'resume_or_submit_result', True)
    return phase('planned', 'start_when_ready', False)


def phase(name, action, attention, *, complete=False):
    """Keep derived phase and producer-completion evidence explicit without granting final status authority."""
    return {'phase': name, 'nextAction': action, 'needsAttention': attention,
            'implementationComplete': complete, 'approvalPending': name in ('awaiting_user', 'awaiting_merge')}


def audit(store, *, limit=50, offset=0, space_id=None):
    """Inspect one bounded page and comment-only candidates; do not parse prose as a completion claim."""
    from .issues import DomainError
    if type(limit) is not int or not 1 <= limit <= 200:
        raise DomainError('invalid_page', 'Audit limit must be 1–200.')
    with store.db.lock:
        issues = store.list(limit=limit, offset=offset, space_id=space_id)
        commented = set()
        if issues:
            placeholders = ','.join('?' for _ in issues)
            rows = store.db.connection.execute(f'SELECT DISTINCT issue_id FROM comments WHERE issue_id IN ({placeholders})', [item['id'] for item in issues])
            commented = {row[0] for row in rows}
        items = []
        for issue in issues:
            lifecycle = issue['lifecycle']
            comment_only = not issue['currentResult'] and issue['id'] in commented and lifecycle['phase'] == 'planned'
            items.append({'id': issue['id'], 'key': issue['key'], 'status': issue['status'],
                          'lifecycle': lifecycle, 'commentOnlyCandidate': comment_only})
        more = store.has_more(offset=offset + len(issues), space_id=space_id)
    return {'contract': 'ag-tasks-lifecycle/v1', 'items': items, 'hasMore': more,
            'nextOffset': offset + len(items) if more else None, 'scope': 'page', 'automaticCompletion': False, 'leaseExpiryMayReconcile': True}


def child_progress(connection, rows):
    """Read indexed direct-child counts for one bounded list/graph page, including off-page children."""
    progress = {}
    if not rows:
        return progress
    placeholders = ','.join('?' for _ in rows)
    children = connection.execute(f"SELECT parent_id,status,COUNT(*) AS amount FROM issues WHERE parent_id IN ({placeholders}) AND kind!='review' AND deleted_at IS NULL GROUP BY parent_id,status", [row['id'] for row in rows])
    for child in children:
        progress.setdefault(child['parent_id'], {})[child['status']] = child['amount']
    return progress


def attention_child(connection, issue_id, phase_name):
    """Choose an actual child from the phase's blocking statuses, independently of page size."""
    statuses = {
        'children_need_attention': ('blocked', 'rework', 'stale'),
        'children_review': ('in_review',),
        'children_pending': ('ready', 'backlog'),
        'children_working': ('in_progress',),
    }.get(phase_name)
    if statuses is None:
        return None
    from .prefixes import issue_key
    placeholders = ','.join('?' for _ in statuses)
    row = connection.execute(f"""SELECT * FROM issues WHERE parent_id=? AND kind!='review'
        AND deleted_at IS NULL AND status IN ({placeholders})
        ORDER BY created_at,number LIMIT 1""", (issue_id, *statuses)).fetchone()
    return {'id': row['id'], 'key': issue_key(connection, row)} if row else None
