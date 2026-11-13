"""Explicit, idempotent leased work sessions; server time never infers effort from task preparation."""

from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from .criteria import failures
from .issues import DomainError, bounded_text

LEASE_SECONDS = 90


class Execution:
    """Persist accepted elapsed intervals; monotonic time bounds leases within one owner process."""

    def __init__(self, store, *, clock=None, monotonic=None):
        """Clock injection supports deterministic expiry/restart tests without user state or sleeps."""
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.ticks = {}

    def _stamp(self):
        """Return the server's UTC instant for provenance; elapsed accounting uses monotonic ticks."""
        return self.clock().isoformat()

    @staticmethod
    def credentials(value, *, start=False):
        """Treat caller identity as a bounded declaration, not authentication or user approval."""
        fields = {'agentId', 'sessionKey'} | (set() if start else {'sessionId'})
        if type(value) is not dict or set(value) != fields:
            raise DomainError('invalid_work_session', 'Explicit agentId/sessionKey and sessionId are required.')
        return {key: bounded_text(item, 128, empty=False) for key, item in value.items()}

    @staticmethod
    def editable(issue):
        """Terminal, archived or tombstoned work is never resumed implicitly."""
        if issue['archived_at'] or issue['deleted_at'] or issue['status'] in ('done', 'cancelled', 'stale'):
            raise DomainError('issue_closed', 'Restore/reopen the issue before starting work.', 409)

    def _elapsed(self, session):
        """Limit a live interval to one lease, excluding wall-clock jumps and unobserved restart downtime."""
        then = self.ticks.get(session['id'])
        if then is None:
            return 0
        uncommitted = self.monotonic() - then - session['elapsed_ms'] / 1000
        return int(min(LEASE_SECONDS, max(0, uncommitted)) * 1000)

    def _expired(self, session):
        """Use monotonic lease age while owned; unknown sessions fall back to stored expiry metadata."""
        then = self.ticks.get(session['id'])
        if then is not None:
            return self.monotonic() - then - session['elapsed_ms'] / 1000 >= LEASE_SECONDS
        return self.clock() >= datetime.fromisoformat(session['lease_expires_at'])

    def _close(self, connection, session, state='paused'):
        """Commit only the last bounded owned interval; repeated closure never adds effort twice."""
        if session['state'] != 'running':
            return
        now = self._stamp()
        connection.execute('UPDATE work_sessions SET elapsed_ms=elapsed_ms+?,state=?,ended_at=? WHERE id=? AND state=?',
                           (self._elapsed(session), state, now, session['id'], 'running'))

    def rework(self, connection, issue_id, actor, problems, action):
        """Persist failed definition evidence inside the caller transaction before a public409 is raised."""
        active = connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone()
        if active:
            self._close(connection, active)
        issue = self.store._row(issue_id, connection)
        connection.execute("UPDATE issues SET status='rework',updated_at=? WHERE id=?", (self._stamp(), issue_id))
        self.store._audit(connection, issue_id, action, actor, {'criterionFailures': problems, 'fromStatus': issue['status'],
            'toStatus': 'rework', 'reason': 'definition_failed'})
        return DomainError('definition_failed', 'The issue needs rework before this operation.', 409,
                           details={'issueId': issue_id, 'status': 'rework', 'criterionFailures': problems})

    def expire(self, issue_id):
        """Materialize a stale lease lazily, without a timer, extra journal or unbounded worker."""
        with self.store.db.lock:
            active = self.store.db.connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone()
            if active is None or not self._expired(active):
                return
        with self.store.db.transaction() as connection:
            active = connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone()
            if active is None or not self._expired(active):
                return
            prior = connection.execute('SELECT status FROM issues WHERE id=?', (issue_id,)).fetchone()['status']
            self._close(connection, active, 'expired')
            connection.execute("UPDATE issues SET status=CASE WHEN status='in_progress' THEN 'ready' ELSE status END,updated_at=? WHERE id=? AND status NOT IN ('done','cancelled')", (self._stamp(), issue_id))
            self.store._audit(connection, issue_id, 'work_expired', 'system', {'sessionId': active['id'],
                'agentId': active['agent_id'], 'leaseSeconds': LEASE_SECONDS, 'fromStatus': prior,
                'toStatus': 'ready' if prior == 'in_progress' else prior, 'reason': 'lease_expired'})

    def expire_all(self):
        """Check only active session identifiers; one active row per issue keeps polling independent of history size."""
        with self.store.db.lock:
            active = self.store.db.connection.execute("SELECT id,issue_id FROM work_sessions WHERE state='running'").fetchall()
            live = {row['id'] for row in active}
            self.ticks = {identifier: tick for identifier, tick in self.ticks.items() if identifier in live}
            identifiers = [row['issue_id'] for row in active]
        for identifier in identifiers:
            self.expire(identifier)

    def prune(self):
        """Drop closed session anchors only after their transaction committed; never alter persisted effort."""
        with self.store.db.lock:
            live = {row[0] for row in self.store.db.connection.execute("SELECT id FROM work_sessions WHERE state='running'")}
            self.ticks = {identifier: tick for identifier, tick in self.ticks.items() if identifier in live}

    def start(self, issue_id, value, actor='agent'):
        """Only this explicit operation starts work after role, readiness, blockers and session ownership checks."""
        supplied = self.credentials(value, start=True)
        self.expire(issue_id)
        error = None
        identifier = None
        try:
            with self.store.db.transaction() as connection:
                issue = self.store._row(issue_id, connection)
                self.editable(issue)
                ancestor_id = issue['parent_id']
                while ancestor_id:
                    ancestor = self.store._row(ancestor_id, connection)
                    if ancestor['status'] == 'stale' or ancestor['archived_at']:
                        raise DomainError('ancestor_unavailable', 'Revalidate/restore the parent plan before starting its tasks.', 409)
                    ancestor_id = ancestor['parent_id']
                previous = connection.execute('SELECT id FROM work_sessions WHERE issue_id=? AND agent_id=? AND session_key=?',
                                              (issue_id, supplied['agentId'], supplied['sessionKey'])).fetchone()
                if not previous:
                    if issue['kind'] == 'review':
                        parent = self.store._row(issue['parent_id'], connection)
                        self.editable(parent)
                        if parent['critic_review_id'] != issue_id:
                            raise DomainError('stale_review', 'This generated critic task has been superseded.', 409)
                        producer = connection.execute('SELECT * FROM results WHERE issue_id=? ORDER BY generation DESC LIMIT 1', (parent['id'],)).fetchone()
                        latest_work = connection.execute('SELECT id FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT 1', (parent['id'],)).fetchone()
                        if (producer is None or parent['status'] != 'in_review' or producer['definition_revision'] != parent['definition_revision'] or
                                producer['reviewed_head'] != parent['reviewed_head'] or latest_work is None or latest_work['id'] != producer['producer_session_id']):
                            raise DomainError('stale_review', 'The critic task no longer describes the current result/work definition.', 409)
                        if producer and producer['producer_agent_id'] == supplied['agentId']:
                            raise DomainError('distinct_critic_required', 'The producer cannot acquire its own critic session.', 409)
                        if issue['assignee'] and issue['assignee'] != supplied['agentId']:
                            raise DomainError('reviewer_assignment_mismatch', 'This review is assigned to another reviewer.', 409)
                    if connection.execute("SELECT 1 FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone():
                        raise DomainError('work_already_active', 'Another explicit work session is active.', 409)
                    self.store._ensure_unblocked(issue_id, connection)
                    problems = failures(json.loads(issue['dor']))
                    if issue['repo_id'] and not issue['worktree']:
                        raise DomainError('prepare_required', 'Prepare the issue worktree before starting agent work.', 409)
                    if not issue['intended_role']:
                        problems.append({'id': 'intendedRole', 'text': 'Intended role is required', 'reason': 'role_required'})
                    if problems:
                        error = self.rework(connection, issue_id, actor, problems, 'readiness_failed')
                    else:
                        now, identifier = self._stamp(), uuid4().hex
                        if issue['kind'] != 'review':
                            self.store.results.supersede(connection, issue_id, 'producer_work_started', issue['result_generation'] + 1)
                            from .completion import invalidate_ancestors
                            invalidate_ancestors(self.store, connection, issue, 'child_work_started')
                        expiry = (self.clock() + timedelta(seconds=LEASE_SECONDS)).isoformat()
                        connection.execute('INSERT INTO work_sessions(id,issue_id,agent_id,session_key,started_at,last_heartbeat_at,lease_expires_at,elapsed_ms,state,ended_at,intended_role) VALUES(?,?,?,?,?,?,?,0,?,NULL,?)',
                                           (identifier, issue_id, supplied['agentId'], supplied['sessionKey'], now, now, expiry, 'running', issue['intended_role']))
                        # One immutable monotonic anchor: heartbeats/close derive pending time
                        # from the committed elapsed_ms, so SQLite rollback cannot reset time.
                        self.ticks[identifier] = self.monotonic()
                        connection.execute("UPDATE issues SET status='in_progress',updated_at=? WHERE id=?", (now, issue_id))
                        self.store._audit(connection, issue_id, 'work_started', actor, {'agentId': supplied['agentId'], 'sessionId': identifier, 'intendedRole': issue['intended_role'],
                            'fromStatus': issue['status'], 'toStatus': 'in_progress'})
        except BaseException:
            if identifier:
                self.ticks.pop(identifier, None)
            raise
        if error:
            raise error
        self.prune()
        result = self.store.get(issue_id)
        active = result['work']['activeSession']
        if active and active['sessionId'] == identifier:
            from service_telemetry.integration import request_diagnostics
            request_diagnostics(sessionId=identifier, agentId=supplied['agentId'])
        return result

    def session(self, connection, issue_id, supplied, *, running=True):
        """Match all session fields; caller-provided labels alone never select a different task's running session."""
        session = connection.execute('SELECT * FROM work_sessions WHERE id=? AND issue_id=? AND agent_id=? AND session_key=?',
                                     (supplied['sessionId'], issue_id, supplied['agentId'], supplied['sessionKey'])).fetchone()
        if session is not None:
            from service_telemetry.integration import request_diagnostics, resolved_task
            resolved_task(issue_id, session['issue_id'])
            request_diagnostics(sessionId=session['id'], agentId=session['agent_id'])
        if session is None or running and session['state'] != 'running':
            raise DomainError('work_session_unavailable', 'Explicit work session is unavailable or expired.', 409)
        return session

    def heartbeat(self, issue_id, value, actor='agent'):
        """Renew an exact unexpired lease, adopting a new monotonic anchor after an owner restart.

        The first accepted heartbeat after restart preserves already committed
        effort and charges no unknown gap. Only subsequent owned intervals count.
        An expired lease still requires an explicit new session.
        """
        supplied = self.credentials(value)
        self.expire(issue_id)
        with self.store.db.lock:
            with self.store.db.transaction() as connection:
                session = self.session(connection, issue_id, supplied)
                adopt = session['id'] not in self.ticks
                now = self._stamp()
                connection.execute('UPDATE work_sessions SET elapsed_ms=elapsed_ms+?,last_heartbeat_at=?,lease_expires_at=? WHERE id=?',
                                   (self._elapsed(session), now, (self.clock() + timedelta(seconds=LEASE_SECONDS)).isoformat(), session['id']))
                from .aging import touch
                touch(connection, issue_id, now)
            if adopt:
                # Publish only after COMMIT, under the same lock. Failed writes
                # and racing stop/result operations cannot acquire an anchor.
                self.ticks[session['id']] = self.monotonic() - session['elapsed_ms'] / 1000
        return self.store.get(issue_id)

    def stop(self, issue_id, value, actor='agent'):
        """Close at most one bounded interval; retrying a completed stop is harmless."""
        from .work_pause import pause_details
        supplied, reason, next_action = pause_details(value, self.credentials)
        self.expire(issue_id)
        with self.store.db.transaction() as connection:
            session = self.session(connection, issue_id, supplied, running=False)
            if session['state'] == 'running':
                prior = self.store._row(issue_id, connection)['status']
                new_status = 'blocked' if reason == 'blocked' else 'ready'
                self._close(connection, session)
                connection.execute('UPDATE work_sessions SET pause_reason=?,next_action=? WHERE id=?', (reason, next_action, session['id']))
                connection.execute("UPDATE issues SET status=CASE WHEN status='in_progress' THEN ? ELSE status END,updated_at=? WHERE id=?", (new_status, self._stamp(), issue_id))
                self.store._audit(connection, issue_id, 'work_stopped', actor, {'sessionId': session['id'], 'reason': reason, 'nextAction': next_action,
                    'agentId': session['agent_id'], 'fromStatus': prior, 'toStatus': new_status if prior == 'in_progress' else prior})
        return self.store.get(issue_id)

    def project(self, issue_id):
        """Return persisted totals plus only the current bounded owned interval, never status-derived elapsed time."""
        connection = self.store.db.connection
        total = connection.execute('SELECT COALESCE(SUM(elapsed_ms),0) FROM work_sessions WHERE issue_id=?', (issue_id,)).fetchone()[0]
        last = connection.execute('SELECT * FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT 1', (issue_id,)).fetchone()
        if last is None:
            return {'state': 'never_started', 'elapsedMs': 0, 'activeSession': None}
        active = None
        if last['state'] == 'running':
            total += self._elapsed(last)
            active = {'sessionId': last['id'], 'agentId': last['agent_id'], 'intendedRole': last['intended_role'], 'startedAt': last['started_at'],
                      'lastHeartbeatAt': last['last_heartbeat_at'], 'leaseExpiresAt': last['lease_expires_at']}
        else:
            self.ticks.pop(last['id'], None)
        return {'state': last['state'], 'elapsedMs': total, 'activeSession': active, 'pauseReason': last['pause_reason'], 'nextAction': last['next_action']}


    def history(self, issue_id, limit=20, offset=0):
        """Read a bounded page of immutable role/session evidence, excluding reusable session keys."""
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise DomainError('invalid_page', 'Invalid session history pagination.')
        with self.store.db.lock:
            issue = self.store._row(issue_id)
            identifier = issue['id']
            self.expire(identifier)
            rows = self.store.db.connection.execute('SELECT * FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?', (identifier, limit+1, offset)).fetchall()
            items = []
            for row in rows[:limit]:
                items.append({'sessionId':row['id'], 'agentId':row['agent_id'], 'intendedRole':row['intended_role'],
                              'startedAt':row['started_at'], 'endedAt':row['ended_at'], 'state':row['state'],
                              'elapsedMs':row['elapsed_ms'] + (self._elapsed(row) if row['state'] == 'running' else 0)})
            more = len(rows) > limit
            return {'items':items, 'hasMore':more, 'nextOffset':offset+len(items) if more else None}
