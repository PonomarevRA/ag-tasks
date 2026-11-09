"""Bounded public values shared by queue and attempt transitions."""

import json
from datetime import datetime, timedelta, timezone
from time import monotonic

from .issues import DomainError, bounded_text
from .service_values import fields, identifier

LEASE_SECONDS = 90
MAX_PENDING_ITEMS = 1000


def utc_now():
    """Use UTC wall time for persisted lease deadlines; an owner incarnation fences restarts."""
    return datetime.now(timezone.utc)


def lease_clock():
    """Anchor UTC once, then measure this owner's leases with elapsed monotonic time."""
    anchor, started = utc_now(), monotonic()
    def now():
        """Wall-clock corrections cannot extend or resurrect a lease within this owner."""
        return anchor + timedelta(seconds=monotonic() - started)
    return now


def profile(value):
    """Store only a versioned adapter identity, never executable paths, arguments or credentials."""
    fields(value, {'id', 'version', 'protocol', 'protocolVersion'}, {'id', 'version', 'protocol', 'protocolVersion'})
    identifier(value['id'])
    bounded_text(value['version'], 128, empty=False)
    identifier(value['protocol'])
    if type(value['protocolVersion']) is not int or not 1 <= value['protocolVersion'] <= 999:
        raise DomainError('invalid_protocol_version', 'Use a protocol version from 1 to 999.')
    return dict(value)


def expect_revision(value, row):
    """Reject stale edits, including booleans masquerading as numeric revisions."""
    if type(value) is not int or value != row['revision']:
        raise DomainError('revision_conflict', 'Read the current revision before changing this record.', 409)


def unknown(row, owner, now):
    """An expired or replaced owner is uncertain; no elapsed-time calculation proves quiescence."""
    if row['state'] == 'released':
        return False
    return row['state'] == 'unknown' or row['incarnation'] != owner or datetime.fromisoformat(row['lease_expires_at']) <= now


def attempt_value(row, owner, now):
    """Project the attempt identity and evidence without allowing an old worker to regain authority."""
    state = 'unknown' if unknown(row, owner, now) else row['state']
    return {'id': row['id'], 'attemptId': row['id'], 'laneId': row['lane_id'], 'itemId': row['item_id'],
            'workerId': row['worker_id'], 'workerEpoch': row['worker_epoch'], 'incarnation': row['incarnation'],
            'fence': row['number'], 'profile': json.loads(row['profile']), 'state': state,
            'cancelRequested': bool(row['cancel_requested']), 'leaseExpiresAt': row['lease_expires_at'],
            'revision': row['revision'], 'outcome': row['outcome'],
            'releaseProof': json.loads(row['release_proof']) if row['release_proof'] else None,
            'createdAt': row['created_at'], 'updatedAt': row['updated_at']}


def item_value(row):
    """Project task intent separately from foreground state and process ownership."""
    return {'id': row['id'], 'laneId': row['lane_id'], 'issueId': row['issue_id'], 'sessionId': row['session_id'],
            'text': row['text'], 'state': row['state'], 'position': row['position'], 'revision': row['revision'],
            'createdAt': row['created_at'], 'updatedAt': row['updated_at']}
