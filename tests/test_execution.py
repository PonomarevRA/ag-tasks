"""Explicit leased effort, rollback/restart recovery and storage migration use only temporary state."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from ag_tasks.execution import Execution
from ag_tasks.issues import DomainError, IssueStore


class Clock:
    """Advance monotonic duration independently of wall time to test clock discontinuities."""
    def __init__(self):
        self.seconds = 0
        self.wall = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.seconds += seconds
        self.wall += timedelta(seconds=seconds)


def prepared(store, title='Task'):
    """Create explicit, evidence-bearing definitions without starting any agent work."""
    items = [{'id': 'checked', 'text': 'Checked definition', 'passed': True, 'evidence': 'Declared check'}]
    return store.create({'title': title, 'intendedRole': 'developer', 'dor': items, 'dod': items})


def start(store, issue, key='run'):
    """Read the server-issued session ID; caller identity stays a declaration."""
    value = store.execution.start(issue['id'], {'agentId': 'worker', 'sessionKey': key})
    return {'agentId': 'worker', 'sessionKey': key, 'sessionId': value['work']['activeSession']['sessionId']}


def test_explicit_work_idempotence_heartbeat_stop_and_wall_clock_jump(tmp_path):
    """Metadata/status cannot create duration; only a bounded real session accounts elapsed time."""
    store = IssueStore(tmp_path)
    clock = Clock()
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        issue = prepared(store)
        clock.advance(3600)
        assert store.get(issue['id'])['work']['elapsedMs'] == 0
        with pytest.raises(DomainError):
            store.update(issue['id'], {'status': 'in_progress'})
        keys = start(store, issue)
        assert start(store, issue) == keys
        clock.advance(30)
        store.execution.heartbeat(issue['id'], keys)
        clock.wall += timedelta(days=10)
        clock.advance(10)
        paused = store.execution.stop(issue['id'], keys)
        assert paused['status'] == 'ready' and paused['work']['elapsedMs'] == 40000
        assert paused['work']['state'] == 'paused'
        assert store.execution.stop(issue['id'], keys)['work']['elapsedMs'] == 40000
    finally:
        store.close()


def test_expired_lease_caps_effort_and_missing_definition_persists_rework(tmp_path):
    """A missed heartbeat cannot charge hours; failed admission commits rework before returning409."""
    store = IssueStore(tmp_path)
    clock = Clock()
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        empty = store.create({'title': 'Needs definition'})
        with pytest.raises(DomainError) as failure:
            start(store, empty)
        assert failure.value.details['status'] == 'rework'
        assert store.get(empty['id'])['status'] == 'rework'
        issue = prepared(store)
        keys = start(store, issue)
        clock.advance(3600)
        with pytest.raises(DomainError):
            store.execution.heartbeat(issue['id'], keys)
        expired = store.get(issue['id'])
        assert expired['work']['state'] == 'expired' and expired['work']['elapsedMs'] == 90000
        assert expired['status'] == 'ready'
        assert expired['lifecycle']['phase'] == 'lease_expired'
    finally:
        store.close()


def test_restart_expired_lease_retains_only_committed_effort(tmp_path):
    """A lease that expired while its owner was absent cannot resume or charge its unknown gap."""
    store = IssueStore(tmp_path)
    clock = Clock()
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    issue = prepared(store)
    keys = start(store, issue)
    clock.advance(12)
    store.execution.heartbeat(issue['id'], keys)
    clock.advance(1200)
    store.close()
    reopened = IssueStore(tmp_path)
    reopened.execution = Execution(reopened, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        value = reopened.get(issue['id'])
        assert value['work'] == {'state': 'expired', 'elapsedMs': 12000, 'activeSession': None, 'pauseReason': None, 'nextAction': None}
        assert value['status'] == 'ready'
        assert value['lifecycle']['phase'] == 'lease_expired'
        assert value['history'][-1]['action'] == 'work_expired'
        with pytest.raises(DomainError, match='unavailable'):
            reopened.execution.heartbeat(issue['id'], keys)
    finally:
        reopened.close()


def test_restart_retains_external_session_and_counts_only_after_first_renewal(tmp_path):
    """A new HTTP owner keeps an unexpired session identity without charging an unobserved interval."""
    clock = Clock()
    store = IssueStore(tmp_path)
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    issue = prepared(store)
    credentials = start(store, issue)
    clock.advance(12)
    before = store.execution.heartbeat(issue['id'], credentials)
    clock.advance(20)
    store.close()
    reopened = IssueStore(tmp_path)
    clock.seconds = 9000  # A different process has an unrelated monotonic origin.
    reopened.execution = Execution(reopened, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        value = reopened.get(issue['id'])
        assert value['status'] == 'in_progress'
        assert value['work'] == before['work']
        assert start(reopened, issue) == credentials
        assert not reopened.execution.ticks
        first = reopened.execution.heartbeat(issue['id'], credentials)
        assert first['work']['elapsedMs'] == 12000
        clock.advance(7)
        assert reopened.execution.heartbeat(issue['id'], credentials)['work']['elapsedMs'] == 19000
        clock.advance(4)
        stopped = reopened.execution.stop(issue['id'], credentials)
        assert stopped['work']['elapsedMs'] == 23000
        assert len(stopped['workHistory']['items']) == 1
        actions = [item['action'] for item in stopped['history']]
        assert actions.count('work_started') == 1 and 'work_interrupted' not in actions
    finally:
        reopened.close()


@pytest.mark.parametrize('operation', ['stop', 'pause', 'result'])
def test_first_explicit_closure_after_restart_does_not_acquire_anchor(tmp_path, operation):
    """Stop, pause and result use the original credentials but never invent effort since the old owner."""
    clock = Clock()
    store = IssueStore(tmp_path)
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    issue = prepared(store)
    credentials = start(store, issue)
    clock.advance(3)
    store.execution.heartbeat(issue['id'], credentials)
    clock.advance(20)
    store.close()
    reopened = IssueStore(tmp_path)
    reopened.execution = Execution(reopened, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        if operation == 'result':
            result = {'summary':'Checked', 'changes':['Bounded change'], 'validation':['Actual check'],
                      'limitations':'Isolated restart fixture', 'artifacts':[]}
            value = reopened.results.submit(issue['id'], {**credentials, 'result':result})
            assert value['currentResult']['generation'] == 1
        else:
            supplied = dict(credentials)
            if operation == 'pause':
                supplied.update(reason='paused', nextAction='Continue verification')
            value = reopened.execution.stop(issue['id'], supplied)
        assert value['work']['elapsedMs'] == 3000
        assert value['work']['activeSession'] is None
        assert not reopened.execution.ticks
    finally:
        reopened.close()


def test_failed_first_renewal_does_not_install_restart_anchor(tmp_path, monkeypatch):
    """A transaction failure preserves original expiry/effort and cannot make the failed heartbeat owned time."""
    import ag_tasks.aging as aging
    clock = Clock()
    store = IssueStore(tmp_path)
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    issue = prepared(store)
    credentials = start(store, issue)
    clock.advance(3)
    before = store.execution.heartbeat(issue['id'], credentials)
    clock.advance(20)
    store.close()
    reopened = IssueStore(tmp_path)
    reopened.execution = Execution(reopened, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    original = aging.touch
    def reject(*_):
        """Fail after the SQL renewal so the test observes a real rollback."""
        raise RuntimeError('injected heartbeat failure')
    try:
        monkeypatch.setattr(aging, 'touch', reject)
        with pytest.raises(RuntimeError, match='heartbeat failure'):
            reopened.execution.heartbeat(issue['id'], credentials)
        assert not reopened.execution.ticks
        assert reopened.get(issue['id'])['work'] == before['work']
        monkeypatch.setattr(aging, 'touch', original)
        clock.advance(5)
        assert reopened.execution.heartbeat(issue['id'], credentials)['work']['elapsedMs'] == 3000
        clock.advance(7)
        assert reopened.execution.stop(issue['id'], credentials)['work']['elapsedMs'] == 10000
    finally:
        reopened.close()


@pytest.mark.parametrize('gap', [89.999, 90, 90.001])
def test_restart_respects_exact_original_lease_boundary(tmp_path, gap):
    """An old owner cannot grant a replacement a fresh90seconds; the original expiry remains authoritative."""
    clock = Clock()
    store = IssueStore(tmp_path)
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    issue = prepared(store)
    credentials = start(store, issue)
    store.close()
    clock.advance(gap)
    reopened = IssueStore(tmp_path)
    reopened.execution = Execution(reopened, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        if gap < 90:
            value = reopened.execution.heartbeat(issue['id'], credentials)
            assert value['work']['activeSession']['sessionId'] == credentials['sessionId']
        else:
            with pytest.raises(DomainError, match='unavailable'):
                reopened.execution.heartbeat(issue['id'], credentials)
            replay = reopened.execution.start(issue['id'], {'agentId':'worker','sessionKey':'run'})
            assert replay['work']['state'] == 'expired'
            assert replay['work']['activeSession'] is None
        assert reopened.get(issue['id'])['work']['elapsedMs'] == 0
        assert len(reopened.execution.history(issue['id'])['items']) == 1
    finally:
        reopened.close()


def test_failed_mutation_keeps_monotonic_anchor_and_rolls_back_sqlite(tmp_path, monkeypatch):
    """A failed audit after closing a session cannot silently discard still-running effort."""
    store = IssueStore(tmp_path)
    clock = Clock()
    store.execution = Execution(store, clock=lambda: clock.wall, monotonic=lambda: clock.seconds)
    try:
        issue = prepared(store)
        original = store._audit
        monkeypatch.setattr(store, '_audit', lambda *args: (_ for _ in ()).throw(RuntimeError('injected audit failure')))
        with pytest.raises(RuntimeError):
            start(store, issue)
        assert not store.execution.ticks and store.get(issue['id'])['work']['elapsedMs'] == 0
        monkeypatch.setattr(store, '_audit', original)
        keys = start(store, issue)
        clock.advance(30)
        monkeypatch.setattr(store, '_audit', lambda *args: (_ for _ in ()).throw(RuntimeError('injected audit failure')))
        with pytest.raises(RuntimeError):
            store.execution.stop(issue['id'], keys)
        clock.advance(10)
        monkeypatch.setattr(store, '_audit', original)
        assert store.execution.stop(issue['id'], keys)['work']['elapsedMs'] == 40000
    finally:
        store.close()


def test_future_schema_is_rejected_without_downgrade_or_schema_mutation(tmp_path):
    """An older binary never overwrites a future service's version or tables."""
    file = tmp_path / 'issues.sqlite3'
    with sqlite3.connect(file) as connection:
        connection.execute('PRAGMA user_version=99')
        connection.execute('CREATE TABLE future(value TEXT)')
        connection.execute("INSERT INTO future VALUES('preserve')")
    with pytest.raises(ValueError):
        IssueStore(tmp_path)
    with sqlite3.connect(file) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 99
        assert connection.execute('SELECT value FROM future').fetchone()[0] == 'preserve'
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('future',)]


def test_session_role_is_immutable_paginated_and_never_guessed_for_legacy(tmp_path):
    store=IssueStore(tmp_path)
    try:
        issue=prepared(store)
        credentials=start(store,issue)
        store.execution.stop(issue['id'],credentials)
        store.update(issue['id'],{'intendedRole':'qa'})
        next_session=start(store,issue,key='qa')
        history=store.execution.history(issue['key'],limit=1)
        assert history['hasMore'] and history['items'][0]['intendedRole']=='qa'
        assert 'sessionKey' not in history['items'][0]
        older=store.execution.history(issue['id'],limit=1,offset=1)
        assert older['items'][0]['intendedRole']=='developer'
        assert store.get(issue['id'])['work']['activeSession']['intendedRole']=='qa'
        store.execution.stop(issue['id'],next_session)
        # Existing v4 rows have no trustworthy captured role, even when the issue has one.
        store.db.connection.execute('ALTER TABLE work_sessions DROP COLUMN intended_role')
        store.db.connection.execute('PRAGMA user_version=4')
    finally:store.close()
    store=IssueStore(tmp_path)
    try:
        assert store.execution.history(issue['id'])['items'][0]['intendedRole'] is None
        assert store.db.connection.execute('PRAGMA user_version').fetchone()[0]==6
    finally:store.close()
