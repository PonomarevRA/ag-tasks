"""Fenced dispatch attempts: delivery, foreground completion and runtime release are separate facts."""

from datetime import timedelta
from uuid import uuid4

from .issues import DomainError, bounded_text
from .service_values import encode, fields, identifier
from .agent_queue_values import LEASE_SECONDS, expect_revision, unknown

IDENTITY = {'attemptId', 'workerId', 'workerEpoch', 'fence'}


class Attempts:
    """Reserve one lane until a live worker or explicit operator proves its runtime stopped."""

    def __init__(self, queue):
        """Reuse queue storage and injected clock; execution accounting is an unrelated contract."""
        self.queue, self.db = queue, queue.db

    def apply(self, operation, payload, actor):
        """Restrict runtime transitions to workers and reconciliation to the guarded human surface."""
        if operation == 'attempt.reconcile':
            if actor != 'user':
                raise DomainError('user_required', 'Only an operator can reconcile uncertain runtime ownership.', 403)
            return self.reconcile(payload)
        if actor != 'agent':
            raise DomainError('worker_required', 'Use the worker command surface for runtime transitions.', 403)
        actions = {'attempt.claim': self.claim, 'attempt.dispatch': self.dispatch, 'attempt.heartbeat': self.heartbeat,
                   'attempt.report': self.report, 'attempt.release': self.release}
        action = actions.get(operation)
        if action is None:
            raise DomainError('unsupported_operation', 'Use an advertised attempt operation.')
        return action(payload)

    def _owned(self, payload, connection):
        """Reject replaced, expired and mismatched owners, even when they know the old fence."""
        row = self.queue._attempt(payload['attemptId'], connection)
        identifier(payload['workerId'])
        identifier(payload['workerEpoch'])
        same = payload['workerId'] == row['worker_id'] and payload['workerEpoch'] == row['worker_epoch']
        if type(payload['fence']) is not int or payload['fence'] != row['number'] or not same:
            raise DomainError('attempt_stale', 'The attempt fence or worker binding is stale.', 409)
        lane = self.queue._lane(row['lane_id'], connection)
        if lane['active_attempt_id'] != row['id'] or row['state'] == 'released' or unknown(row, self.db.queue_incarnation, self.queue.clock()):
            raise DomainError('attempt_stale', 'Read the current attempt; uncertain ownership requires operator reconciliation.', 409)
        return row

    def claim(self, payload):
        """Atomically reserve the first waiting intent and capture its current profile version."""
        fields(payload, {'laneId', 'workerId', 'workerEpoch'}, {'laneId', 'workerId', 'workerEpoch'})
        worker, epoch = identifier(payload['workerId']), identifier(payload['workerEpoch'])
        attempt_id, now = uuid4().hex, self.queue.clock()
        with self.db.transaction() as connection:
            lane = self.queue._lane(payload['laneId'], connection)
            if lane['draining']:
                raise DomainError('queue_draining', 'The queue is draining; no new attempt can claim it.', 409)
            if lane['active_attempt_id']:
                raise DomainError('queue_busy', 'An earlier runtime still owns this lane.', 409)
            item = connection.execute("SELECT * FROM agent_queue_items WHERE lane_id=? AND state='queued' ORDER BY position,number LIMIT 1", (lane['id'],)).fetchone()
            if item is None:
                raise DomainError('queue_empty', 'No waiting intent is available.', 409)
            connection.execute('''INSERT INTO agent_attempts(id,lane_id,item_id,worker_id,worker_epoch,incarnation,
                profile,state,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (attempt_id, lane['id'], item['id'], worker, epoch, self.db.queue_incarnation, lane['profile'], 'claimed',
                 (now + timedelta(seconds=LEASE_SECONDS)).isoformat(), now.isoformat(), now.isoformat()))
            connection.execute("UPDATE agent_queue_items SET state='active',revision=revision+1,updated_at=? WHERE id=?", (now.isoformat(), item['id']))
            connection.execute('UPDATE agent_lanes SET active_attempt_id=? WHERE id=?', (attempt_id, lane['id']))
            self.queue._touch(connection, lane['id'], now.isoformat())
        return self.queue.attempt(attempt_id)

    def dispatch(self, payload):
        """Persist a single-use send intent; the worker must receive this ACK before sending externally."""
        fields(payload, IDENTITY, IDENTITY)
        with self.db.transaction() as connection:
            row = self._owned(payload, connection)
            if row['cancel_requested']:
                raise DomainError('dispatch_cancelled', 'Cancellation won before dispatch authorization.', 409)
            if row['state'] != 'claimed':
                raise DomainError('attempt_state_conflict', 'Dispatch requires an unconsumed claim.', 409)
            self._state(connection, row['id'], 'dispatched')
        return self.queue.attempt(row['id'])

    def heartbeat(self, payload):
        """Renew only a currently live worker; an expired lease can never be resurrected."""
        fields(payload, IDENTITY, IDENTITY)
        with self.db.transaction() as connection:
            row = self._owned(payload, connection)
            now = self.queue.clock()
            connection.execute('UPDATE agent_attempts SET lease_expires_at=?,revision=revision+1,updated_at=? WHERE id=?',
                               ((now + timedelta(seconds=LEASE_SECONDS)).isoformat(), now.isoformat(), row['id']))
        return self.queue.attempt(row['id'])

    def report(self, payload):
        """Record foreground progress or uncertainty; even finished keeps exclusive runtime ownership."""
        fields(payload, IDENTITY | {'state', 'outcome'}, IDENTITY | {'state'})
        state = payload['state']
        if state not in ('running', 'finished', 'unknown'):
            raise DomainError('invalid_attempt_state', 'Report running, finished or unknown.')
        outcome = bounded_text(payload.get('outcome'), 128, nullable=True, empty=False)
        with self.db.transaction() as connection:
            row = self._owned(payload, connection)
            allowed = row['state'] in ('dispatched', 'running', 'finished')
            if row['state'] == 'claimed' and state == 'unknown':
                allowed = True
            if not allowed or row['state'] == 'finished' and state == 'running':
                raise DomainError('attempt_state_conflict', 'The requested foreground transition is not valid.', 409)
            self._state(connection, row['id'], state, outcome if outcome is not None else row['outcome'])
            if state == 'unknown':
                connection.execute("UPDATE agent_queue_items SET state='unknown',revision=revision+1,updated_at=? WHERE id=?", (self.queue.clock().isoformat(), row['item_id']))
        return self.queue.attempt(row['id'])

    def _state(self, connection, attempt_id, state, outcome=None):
        """Keep transition revision and UTC time together under the domain transaction."""
        connection.execute('UPDATE agent_attempts SET state=?,outcome=?,revision=revision+1,updated_at=? WHERE id=?',
                           (state, outcome, self.queue.clock().isoformat(), attempt_id))

    def release(self, payload):
        """Accept explicit live-worker runtime evidence only after foreground completion or pre-send cancellation."""
        fields(payload, IDENTITY | {'proof'}, IDENTITY | {'proof'})
        proof = payload['proof']
        fields(proof, {'kind', 'evidence'}, {'kind', 'evidence'})
        if proof['kind'] != 'owned_runtime_stopped':
            raise DomainError('invalid_release_proof', 'Runtime-stopped evidence is required.')
        evidence = bounded_text(proof['evidence'], 2048, empty=False)
        with self.db.transaction() as connection:
            row = self._owned(payload, connection)
            if row['state'] != 'finished' and not (row['state'] == 'claimed' and row['cancel_requested']):
                raise DomainError('attempt_state_conflict', 'Finish foreground execution before releasing its runtime.', 409)
            self._close(connection, row, {'kind': 'owned_runtime_stopped', 'evidence': evidence, 'source': 'worker'})
        return self.queue.attempt(row['id'])

    def reconcile(self, payload):
        """Close an uncertain or finished runtime only with current operator evidence; never replay it."""
        fields(payload, {'attemptId', 'expectedRevision', 'resolution', 'evidence'}, {'attemptId', 'expectedRevision', 'resolution', 'evidence'})
        if payload['resolution'] != 'owned_runtime_stopped':
            raise DomainError('invalid_resolution', 'Only proven runtime stop can release the lane.')
        evidence = bounded_text(payload['evidence'], 2048, empty=False)
        with self.db.transaction() as connection:
            row = self.queue._attempt(payload['attemptId'], connection)
            expect_revision(payload['expectedRevision'], row)
            uncertain = unknown(row, self.db.queue_incarnation, self.queue.clock())
            if row['state'] == 'released' or not uncertain and row['state'] != 'finished':
                raise DomainError('attempt_state_conflict', 'Reconciliation requires uncertain or finished ownership.', 409)
            self._close(connection, row, {'kind': 'owned_runtime_stopped', 'evidence': evidence, 'source': 'operator'})
        return self.queue.attempt(row['id'])

    def _close(self, connection, row, proof):
        """Release reservation and close its input atomically; unknown effects are never returned to the queue."""
        now = self.queue.clock().isoformat()
        lane = self.queue._lane(row['lane_id'], connection)
        if lane['active_attempt_id'] != row['id']:
            raise DomainError('attempt_stale', 'The lane no longer belongs to this attempt.', 409)
        state = 'cancelled' if row['cancel_requested'] else 'completed'
        # Completion of an uncertain attempt records closure, not successful execution of its business intent.
        if unknown(row, self.db.queue_incarnation, self.queue.clock()) and not row['cancel_requested']:
            state = 'unknown'
        connection.execute("UPDATE agent_attempts SET state='released',release_proof=?,revision=revision+1,updated_at=? WHERE id=?", (encode(proof), now, row['id']))
        connection.execute('UPDATE agent_queue_items SET state=?,revision=revision+1,updated_at=? WHERE id=?', (state, now, row['item_id']))
        connection.execute('UPDATE agent_lanes SET active_attempt_id=NULL,revision=revision+1,updated_at=? WHERE id=?', (now, row['lane_id']))
