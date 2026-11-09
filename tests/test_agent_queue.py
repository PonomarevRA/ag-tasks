"""Queue contract and fault checks use isolated storage; no actual provider or user chat is opened."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from uuid import uuid4
from threading import Barrier

import pytest

from ag_tasks.agent_queue import QueueCore
from ag_tasks.issues import DomainError, IssueStore

PROFILE = {'id': 'fake-acp', 'version': '1.0.0', 'protocol': 'acp', 'protocolVersion': 1}


@pytest.fixture
def domain(tmp_path):
    """Use the production command journal with a deterministic runtime clock."""
    store = IssueStore(tmp_path)
    now = [datetime(2026, 10, 25, 10, tzinfo=timezone.utc)]
    queue = QueueCore(store, clock=lambda: now[0])
    yield store, queue, now
    store.close()


def command(domain, operation, payload, key=None, actor='agent'):
    """Keep command retries in the shared journal, rather than implementing transport deduplication."""
    store, queue, _ = domain
    return store.service.commands.execute('queue-tests', key or uuid4().hex, operation, payload,
                                         lambda operation, data: queue.apply(operation, data, actor))


def lane(domain, lane_id='lane'):
    """Create a logical queue with no launch path or provider credentials."""
    return command(domain, 'queue.create', {'laneId': lane_id, 'title': 'Agent work', 'profile': PROFILE})


def enqueue(domain, text='Do work', lane_id='lane'):
    """Persist a new task intent with its own immutable identity."""
    return command(domain, 'queue.enqueue', {'laneId': lane_id, 'text': text})


def claim(domain, worker='worker', epoch='epoch', lane_id='lane'):
    """Claim the head intent without authorizing an external send."""
    return command(domain, 'attempt.claim', {'laneId': lane_id, 'workerId': worker, 'workerEpoch': epoch})


def identity(attempt):
    """Copy only the explicit worker identity, not revision or a remote RPC identifier."""
    return {'attemptId': attempt['id'], 'workerId': attempt['workerId'],
            'workerEpoch': attempt['workerEpoch'], 'fence': attempt['fence']}


def assert_error(code, callback):
    """Check safe domain codes, rather than matching incidental wording or SQLite failures."""
    with pytest.raises(DomainError) as failure:
        callback()
    assert failure.value.code == code


def test_retry_reorder_pending_pages_and_cancel_preserve_one_intent(domain):
    """Lost ACK retries cannot duplicate inputs; bounded pending pages reflect dispatch order."""
    lane(domain)
    first = command(domain, 'queue.enqueue', {'laneId': 'lane', 'text': 'First'}, 'stable-enqueue')
    assert command(domain, 'queue.enqueue', {'laneId': 'lane', 'text': 'First'}, 'stable-enqueue') == first
    second, third = enqueue(domain, 'Second'), enqueue(domain, 'Third')
    moved = command(domain, 'queue.move', {'itemId': third['id'], 'expectedRevision': third['revision'], 'beforeItemId': first['id']})
    assert moved['position'] == 1
    queue = domain[1]
    page = queue.pending('lane', 2)
    assert [item['text'] for item in page['items']] == ['Third', 'First']
    assert page['hasMore']
    assert [item['text'] for item in queue.pending('lane', 2, page['nextCursor'])['items']] == ['Second']
    assert [item['id'] for item in queue.items('lane')['items']] == [first['id'], second['id'], third['id']]
    assert_error('revision_conflict', lambda: command(domain, 'queue.cancel', {'itemId': first['id'], 'expectedRevision': first['revision']}))
    current = queue.item(first['id'])
    cancelled = command(domain, 'queue.cancel', {'itemId': first['id'], 'expectedRevision': current['revision']})
    assert cancelled['state'] == 'cancelled'
    assert claim(domain)['itemId'] == third['id']
    assert_error('idempotency_conflict', lambda: command(domain, 'queue.enqueue', {'laneId': 'lane', 'text': 'Different'}, 'stable-enqueue'))


def test_two_workers_cannot_claim_same_lane(domain):
    """The transaction and active-attempt constraint reserve exactly one writer."""
    lane(domain)
    enqueue(domain)
    enqueue(domain)
    def run(number):
        """Return rejected competitors without weakening the accepted ownership assertion."""
        try:
            return claim(domain, worker=f'worker-{number}')
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    assert sum(type(result) is dict for result in results) == 1
    assert results.count('queue_busy') == 5
    assert domain[1].get('lane')['queuedCount'] == 1


def test_cancel_before_claim_never_dispatches_and_does_not_change_issue_workflow(domain):
    """Queue cancellation closes only this intent; issue result and critic gates remain independent."""
    store, queue, _ = domain
    lane(domain)
    issue = store.create({'title': 'Linked issue'})
    item = command(domain, 'queue.enqueue', {'laneId': 'lane', 'text': 'Work', 'issueId': issue['id']})
    command(domain, 'queue.cancel', {'itemId': item['id'], 'expectedRevision': item['revision']})
    assert_error('queue_empty', lambda: claim(domain))
    assert store.get(issue['id'])['status'] == 'backlog'
    assert queue.get('lane')['activeAttempt'] is None


def test_cancel_after_claim_blocks_dispatch_but_requires_runtime_release(domain):
    """A claimed worker must see the cancellation fence before it can obtain send authorization."""
    lane(domain)
    item = enqueue(domain)
    attempt = claim(domain)
    item = domain[1].item(item['id'])
    command(domain, 'queue.cancel', {'itemId': item['id'], 'expectedRevision': item['revision']})
    assert_error('dispatch_cancelled', lambda: command(domain, 'attempt.dispatch', identity(attempt)))
    assert_error('queue_busy', lambda: claim(domain, worker='other'))
    released = command(domain, 'attempt.release', {**identity(attempt), 'proof': {'kind': 'owned_runtime_stopped', 'evidence': 'Nothing was spawned before cancelled authorization.'}})
    assert released['state'] == 'released'
    assert released['item']['state'] == 'cancelled'
    assert domain[1].get('lane')['state'] == 'ready'


def test_dispatch_cancel_race_reserves_lane_and_never_duplicates_send_authorization(domain):
    """The winning command fixes desired cancellation, while dispatched work retains ownership."""
    lane(domain)
    item = enqueue(domain)
    attempt = claim(domain)
    item = domain[1].item(item['id'])
    dispatched = command(domain, 'attempt.dispatch', identity(attempt), 'dispatch-key')
    assert dispatched['state'] == 'dispatched'
    assert command(domain, 'attempt.dispatch', identity(attempt), 'dispatch-key') == dispatched
    command(domain, 'queue.cancel', {'itemId': item['id'], 'expectedRevision': item['revision']})
    assert domain[1].attempt(attempt['id'])['cancelRequested']
    assert_error('dispatch_cancelled', lambda: command(domain, 'attempt.dispatch', identity(attempt)))
    assert_error('queue_busy', lambda: claim(domain))


def test_concurrent_cancel_and_dispatch_have_one_atomic_order(domain):
    """Competing clients cannot dispatch after a committed pre-send cancellation or free an active lane."""
    for number in range(12):
        lane_id = f'race-{number}'
        lane(domain, lane_id)
        item = enqueue(domain, lane_id=lane_id)
        attempt = claim(domain, lane_id=lane_id)
        item = domain[1].item(item['id'])
        barrier = Barrier(2)
        def dispatch():
            """Race the exact authorization transaction against the cancellation transaction."""
            barrier.wait()
            try:
                return command(domain, 'attempt.dispatch', identity(attempt))['state']
            except DomainError as error:
                return error.code
        def cancel():
            """Cancellation keeps the already claimed reservation even when it wins first."""
            barrier.wait()
            return command(domain, 'queue.cancel', {'itemId': item['id'], 'expectedRevision': item['revision']})
        with ThreadPoolExecutor(max_workers=2) as pool:
            sent, cancelled = pool.submit(dispatch), pool.submit(cancel)
            dispatch_result, cancel_result = sent.result(), cancelled.result()
        assert dispatch_result in ('dispatched', 'dispatch_cancelled')
        assert cancel_result['state'] == 'cancel_requested'
        current = domain[1].attempt(attempt['id'])
        assert current['cancelRequested']
        assert current['state'] == ('dispatched' if dispatch_result == 'dispatched' else 'claimed')
        assert domain[1].get(lane_id)['activeAttempt']['id'] == attempt['id']


def test_foreground_finished_and_cancel_notification_do_not_release_runtime(domain):
    """v1 stopReason or v2 idle is not a claim that descendants can no longer write."""
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    command(domain, 'attempt.dispatch', identity(attempt))
    finished = command(domain, 'attempt.report', {**identity(attempt), 'state': 'finished', 'outcome': 'end_turn'})
    assert finished['item']['state'] == 'active'
    assert domain[1].get('lane')['state'] == 'busy'
    assert_error('queue_busy', lambda: claim(domain))
    assert_error('user_required', lambda: command(domain, 'attempt.reconcile', {'attemptId': attempt['id'], 'expectedRevision': finished['revision'], 'resolution': 'owned_runtime_stopped', 'evidence': 'Checked runtime stopped.'}))
    closed = command(domain, 'attempt.reconcile', {'attemptId': attempt['id'], 'expectedRevision': finished['revision'], 'resolution': 'owned_runtime_stopped', 'evidence': 'Operator verified process tree and detached workers stopped.'}, actor='user')
    assert closed['state'] == 'released' and closed['item']['state'] == 'completed'
    assert closed['outcome'] == 'end_turn'


def test_expired_owner_cannot_renew_dispatch_report_or_release(domain):
    """Expiry exposes unknown without side effects, and old credentials cannot regain the lane."""
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    command(domain, 'attempt.dispatch', identity(attempt))
    domain[2][0] += timedelta(seconds=91)
    current = domain[1].attempt(attempt['id'])
    assert current['state'] == 'unknown' and current['item']['state'] == 'unknown'
    assert domain[1].get('lane')['state'] == 'quarantined'
    for operation, extra in [('attempt.heartbeat', {}), ('attempt.dispatch', {}), ('attempt.report', {'state': 'finished'}),
                             ('attempt.release', {'proof': {'kind': 'owned_runtime_stopped', 'evidence': 'old worker'}})]:
        assert_error('attempt_stale', lambda operation=operation, extra=extra: command(domain, operation, {**identity(attempt), **extra}))
    closed = command(domain, 'attempt.reconcile', {'attemptId': attempt['id'], 'expectedRevision': current['revision'], 'resolution': 'owned_runtime_stopped', 'evidence': 'Operator checked all side effects and stopped old runtime.'}, actor='user')
    assert closed['item']['state'] == 'unknown'
    assert domain[1].get('lane')['activeAttempt'] is None
    assert not domain[1].pending('lane')['items']
    assert_error('queue_item_closed', lambda: command(domain, 'queue.cancel', {'itemId': closed['itemId'], 'expectedRevision': closed['item']['revision']}))


def test_heartbeat_extends_current_lease_and_explicit_unknown_is_sticky(domain):
    """Worker liveness can extend its current reservation, while uncertainty is not renewable."""
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    domain[2][0] += timedelta(seconds=50)
    renewed = command(domain, 'attempt.heartbeat', identity(attempt))
    assert datetime.fromisoformat(renewed['leaseExpiresAt']) - domain[2][0] == timedelta(seconds=90)
    command(domain, 'attempt.dispatch', identity(attempt))
    uncertain = command(domain, 'attempt.report', {**identity(attempt), 'state': 'unknown', 'outcome': 'transport_eof'})
    assert uncertain['state'] == 'unknown' and domain[1].get('lane')['state'] == 'quarantined'
    assert_error('attempt_stale', lambda: command(domain, 'attempt.heartbeat', identity(attempt)))
    assert_error('attempt_stale', lambda: command(domain, 'attempt.report', {**identity(attempt), 'state': 'finished'}))


def test_observed_expiry_cannot_be_resurrected_by_clock_rollback(domain):
    """A clock correction must not make an already rejected worker live again."""
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    domain[2][0] += timedelta(seconds=91)
    assert domain[1].attempt(attempt['id'])['state'] == 'unknown'
    domain[2][0] -= timedelta(seconds=120)
    assert domain[1].attempt(attempt['id'])['state'] == 'unknown'
    assert_error('attempt_stale', lambda: command(domain, 'attempt.heartbeat', identity(attempt)))


def test_failed_claim_preflight_can_quarantine_without_dispatch(domain):
    """A profile mismatch before spawning can close worker authority without pretending it dispatched."""
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    failed = command(domain, 'attempt.report', {**identity(attempt), 'state': 'unknown', 'outcome': 'profile_mismatch'})
    assert failed['state'] == 'unknown'
    assert_error('attempt_stale', lambda: command(domain, 'attempt.dispatch', identity(attempt)))


def test_restart_keeps_dispatch_and_fences_old_incarnation(tmp_path):
    """A new service owner quarantines the old runtime and never requeues dispatched work."""
    first = IssueStore(tmp_path)
    old = QueueCore(first)
    domain = first, old, None
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    command(domain, 'attempt.dispatch', identity(attempt), 'before-restart')
    first.close()
    second = IssueStore(tmp_path)
    try:
        queue = QueueCore(second)
        current = second, queue, None
        assert queue.attempt(attempt['id'])['state'] == 'unknown'
        assert queue.get('lane')['activeAttempt']['id'] == attempt['id']
        assert_error('attempt_stale', lambda: command(current, 'attempt.heartbeat', identity(attempt)))
        assert_error('queue_busy', lambda: claim(current, worker='new-worker', epoch='new-epoch'))
        assert queue.pending('lane')['items'] == []
        assert second.service.commands.outcome('queue-tests', 'before-restart')['response']['state'] == 'dispatched'
    finally:
        second.close()


def test_profile_updates_apply_only_to_future_attempts_and_drain_blocks_claim(domain):
    """An upstream version update cannot change a running attempt's captured binding."""
    lane(domain)
    enqueue(domain)
    first = claim(domain)
    queue = domain[1]
    current = queue.get('lane')
    changed = command(domain, 'queue.configure', {'laneId': 'lane', 'expectedRevision': current['revision'], 'profile': {**PROFILE, 'version': '2.0.0'}, 'draining': True})
    assert changed['profile']['version'] == '2.0.0'
    assert queue.attempt(first['id'])['profile']['version'] == '1.0.0'
    item = queue.item(first['itemId'])
    command(domain, 'queue.cancel', {'itemId': item['id'], 'expectedRevision': item['revision']})
    command(domain, 'attempt.release', {**identity(first), 'proof': {'kind': 'owned_runtime_stopped', 'evidence': 'No process was started.'}})
    enqueue(domain)
    assert_error('queue_draining', lambda: claim(domain))
    current = queue.get('lane')
    command(domain, 'queue.configure', {'laneId': 'lane', 'expectedRevision': current['revision'], 'draining': False})
    second = claim(domain, worker='second', epoch='second-epoch')
    assert second['profile']['version'] == '2.0.0' and second['fence'] > first['fence']
    assert_error('attempt_stale', lambda: command(domain, 'attempt.heartbeat', identity(first)))


def test_failed_outcome_write_rolls_back_queue_effect(domain):
    """A storage failure between mutation and outcome cannot leave an untracked queued intent."""
    store, queue, _ = domain
    lane(domain)
    with store.db.lock:
        store.db.connection.execute("CREATE TRIGGER fail_queue_command BEFORE INSERT ON service_commands WHEN NEW.command_id='fault' BEGIN SELECT RAISE(ABORT,'full'); END")
    with pytest.raises(sqlite3.IntegrityError):
        command(domain, 'queue.enqueue', {'laneId': 'lane', 'text': 'Must roll back'}, 'fault')
    assert queue.items('lane')['items'] == []
    assert_error('command_not_found', lambda: store.service.commands.outcome('queue-tests', 'fault'))


def test_future_queue_schema_is_rejected_before_any_startup_mutation(tmp_path):
    """Compatibility rejection must preserve legacy work state and queue reservation exactly."""
    store = IssueStore(tmp_path)
    store.db.connection.execute('UPDATE agent_queue_version SET version=999')
    store.close()
    connection = sqlite3.connect(tmp_path / 'issues.sqlite3')
    before = connection.execute('SELECT * FROM history').fetchall()
    connection.close()
    with pytest.raises(ValueError, match='future agent queue schema'):
        IssueStore(tmp_path)
    connection = sqlite3.connect(tmp_path / 'issues.sqlite3')
    try:
        assert connection.execute('SELECT * FROM history').fetchall() == before
        assert connection.execute('SELECT version FROM agent_queue_version').fetchone()[0] == 999
    finally:
        connection.close()


def test_validation_authority_and_worker_binding_are_explicit(domain):
    """Malformed input cannot become a filesystem path, fake fence or human runtime override."""
    lane(domain)
    enqueue(domain)
    attempt = claim(domain)
    assert_error('attempt_stale', lambda: command(domain, 'attempt.heartbeat', {**identity(attempt), 'fence': True}))
    assert_error('attempt_stale', lambda: command(domain, 'attempt.heartbeat', {**identity(attempt), 'workerEpoch': 'other'}))
    assert_error('worker_required', lambda: command(domain, 'attempt.heartbeat', identity(attempt), actor='user'))
    assert_error('invalid_field', lambda: command(domain, 'queue.enqueue', {'laneId': 'lane', 'text': ''}))
    assert_error('invalid_fields', lambda: command(domain, 'queue.create', {'laneId': 'other', 'title': 'Other', 'profile': {**PROFILE, 'command': 'sh'}}))
    assert_error('attempt_state_conflict', lambda: command(domain, 'attempt.reconcile', {'attemptId': attempt['id'], 'expectedRevision': attempt['revision'], 'resolution': 'owned_runtime_stopped', 'evidence': 'Not enough while live.'}, actor='user'))
