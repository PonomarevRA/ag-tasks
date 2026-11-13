"""Bounded inactivity policy, independent of ZIP retention and work-session leases."""

from datetime import datetime, timedelta, timezone

from .review_queue import CURRENT_REVIEWS

STALE_AFTER_DAYS = 3
HOUSEKEEPING = ('storage_restored', 'storage_compressed', 'auto_stale', 'ancestor_stale',
                'critic_superseded', 'work_expired', 'work_interrupted', 'project_space_moved')


def migrate(connection):
    """Preserve historical activity on upgrade; opening ZIP and restarting never renew relevance."""
    connection.execute('SAVEPOINT activity_migration')
    try:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(issues)')}
        if 'last_activity_at' not in columns:
            connection.execute('ALTER TABLE issues ADD COLUMN last_activity_at TEXT')
            ignored = ','.join('?' for _ in HOUSEKEEPING)
            connection.execute(f'''UPDATE issues SET last_activity_at=COALESCE(
                (SELECT MAX(created_at) FROM history WHERE issue_id=issues.id AND action NOT IN ({ignored})),created_at)''', HOUSEKEEPING)
            connection.execute('''UPDATE issues SET last_activity_at=MAX(last_activity_at,COALESCE(
                (SELECT MAX(last_heartbeat_at) FROM work_sessions WHERE issue_id=issues.id),last_activity_at))''')
        connection.execute('CREATE INDEX IF NOT EXISTS issues_activity ON issues(last_activity_at) WHERE deleted_at IS NULL AND archived_at IS NULL')
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(work_sessions)')}
        for column in ('pause_reason', 'next_action'):
            if column not in columns:
                connection.execute(f'ALTER TABLE work_sessions ADD COLUMN {column} TEXT')
        connection.execute('RELEASE activity_migration')
    except BaseException:
        connection.execute('ROLLBACK TO activity_migration')
        connection.execute('RELEASE activity_migration')
        raise


def touch(connection, issue_id, now):
    """Propagate accepted work activity to ancestors without ever starting their timers."""
    connection.execute('''WITH RECURSIVE ancestors(id) AS (
        SELECT id FROM issues WHERE id=? UNION SELECT i.parent_id FROM issues i JOIN ancestors a ON i.id=a.id WHERE i.parent_id IS NOT NULL)
        UPDATE issues SET last_activity_at=MAX(COALESCE(last_activity_at,created_at),?) WHERE id IN (SELECT id FROM ancestors)''', (issue_id, now))


class Aging:
    """Mark inactive ordinary tasks stale while retaining immutable results and review evidence."""

    def __init__(self, store):
        """Reuse the owning store and its transaction/ownership boundary."""
        self.store = store
        self.last_error = None

    def sweep(self, *, now=None, limit=100):
        """Catch up at startup and each minute; a fresh/active descendant protects its plan.

        Current reviews protect their producer and its ancestors. At most 100 ordinary
        issues change per transaction. Generated critics are
        superseded with their producer, never independently expired or auto-approved.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Aging limit must be 1..100')
        instant = now or datetime.now(timezone.utc)
        cutoff = (instant - timedelta(days=STALE_AFTER_DAYS)).isoformat()
        changed = []
        with self.store.db.transaction() as connection:
            rows = connection.execute(f'''WITH current_reviews AS ({CURRENT_REVIEWS})
                SELECT id,status,last_activity_at FROM issues
                WHERE kind!='review' AND status NOT IN ('done','cancelled','stale')
                AND archived_at IS NULL AND deleted_at IS NULL AND last_activity_at<=?
                AND NOT EXISTS(WITH RECURSIVE tree(id) AS (
                    SELECT issues.id UNION SELECT c.id FROM issues c JOIN tree t ON c.parent_id=t.id
                    WHERE c.deleted_at IS NULL AND c.archived_at IS NULL)
                    SELECT 1 FROM issues child JOIN tree ON child.id=tree.id
                    WHERE child.last_activity_at>?
                    OR EXISTS(SELECT 1 FROM work_sessions w WHERE w.issue_id=child.id AND w.state='running')
                    OR EXISTS(SELECT 1 FROM current_reviews r WHERE r.producer_id=child.id))
                ORDER BY last_activity_at,number LIMIT ?''', (cutoff, cutoff, limit)).fetchall()
            for row in rows:
                self.store.results.supersede(connection, row['id'], 'inactivity', 0)
                connection.execute("UPDATE issues SET status='stale',updated_at=? WHERE id=?", (instant.isoformat(), row['id']))
                self.store._audit(connection, row['id'], 'auto_stale', 'system', {
                    'previousStatus': row['status'], 'lastActivityAt': row['last_activity_at'], 'afterDays': STALE_AFTER_DAYS,
                    'fromStatus': row['status'], 'toStatus': 'stale', 'reason': 'inactivity'})
                changed.append(row['id'])
        return changed
