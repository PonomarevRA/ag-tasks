"""Transcript-free immutable observations, separate from model interpretations."""

import json
from hashlib import sha256
from uuid import uuid4

from .issues import DomainError, timestamp
from .service_values import encode, fingerprint, fields
from .task_context import compact

OUTCOMES = ('accepted', 'rework', 'failed', 'partial', 'unknown')


def source_digest(value):
    """Hash already bounded task data without adding a second smaller payload limit."""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return 'sha256:' + sha256(raw.encode('utf-8')).hexdigest()


class ProcessEvidence:
    """Keep compact evidence for task execution beyond transient session retention."""

    def __init__(self, store):
        """Share the owning store and its existing transaction boundary."""
        self.store = store

    def capture(self, payload):
        """Capture explicit runtime failure or a current reviewed result; retries deduplicate by source."""
        fields(payload, {'issueId', 'sourceId', 'outcome', 'coverage', 'purpose'},
               {'issueId', 'sourceId', 'outcome', 'coverage'})
        from .issues import bounded_text
        source = bounded_text(payload['sourceId'], 128, empty=False)
        coverage = bounded_text(payload['coverage'], 512, empty=False)
        outcome = payload['outcome']
        if outcome not in OUTCOMES or payload.get('purpose', 'task') != 'task':
            raise DomainError('invalid_process_outcome', 'Unknown terminal outcome or purpose.')
        issue = self.store.get(payload['issueId'])
        purpose = 'task'
        if outcome in ('accepted', 'rework'):
            review = issue['criticReview']
            expected = 'pass' if outcome == 'accepted' else 'rework'
            if not review or review['verdict'] != expected or not issue['currentResultIsCurrent']:
                raise DomainError('process_review_required', 'A current critic report must establish this outcome.', 409)
        omissions = {}
        review = compact(issue['criticReview'], 'review', omissions)
        run_rows = self.store.db.connection.execute(
            "SELECT payload FROM process_runs WHERE (issue_id=? OR issue_id=?) AND purpose='task' "
            "AND json_extract(payload,'$.intent.purpose')='task' ORDER BY rowid DESC LIMIT 6",
            (issue['id'], issue['criticReviewId'] or '')).fetchall()
        runs = []
        for row in run_rows[:5]:
            run = json.loads(row['payload'])
            item = {key: run.get(key) for key in ('runId', 'attemptId', 'workSessionId', 'conversationId',
                'workerId', 'workerEpoch', 'fence', 'runtimeRelease', 'provenance', 'usage', 'terminalRevision')}
            item['admittedVersions'] = {key: run['intent'][key] for key in ('expectedDefinitionRevision', 'expectedResultGeneration')}
            runs.append(item)
        if len(run_rows) > 5:
            omissions['execution'] = 'newest_five_runs_only'
        execution = compact(runs, 'execution', omissions)
        manifest = {
            'contract': 'ag-tasks-evidence/v1', 'issueId': issue['id'], 'spaceId': issue['spaceId'],
            'definitionRevision': issue['definitionRevision'], 'resultGeneration': issue['resultGeneration'],
            'outcome': outcome, 'sourceId': source, 'coverage': coverage,
            'purpose': purpose, 'observedAt': timestamp(),
            'workState': issue['work']['state'], 'resultDigest': source_digest(issue['currentResult']),
            'reviewDigest': source_digest(issue['criticReview']), 'review': review,
            'execution': execution,
            'runtimeLinks': {'conversationId': None, 'conversationCoverage': 'not_supplied_by_external_adapter',
                             'toolVersion': None, 'toolVersionCoverage': 'not_reported_by_worker_v1'},
            'claims': {'result': 'agent_reported', 'review': 'distinct_declared_critic'},
            'omitted': {'transcript': 'not_collected', 'private_reasoning': 'not_collected',
                        'work_session_credentials': 'not_collected', 'raw_tool_output': 'not_collected', **omissions}}
        # The database source key is immutable even when a later retry observes new state.
        source_key = fingerprint({'issue': issue['id'], 'source': source})
        connection = self.store.db.connection
        old = connection.execute('SELECT * FROM process_evidence WHERE source_key=?', (source_key,)).fetchone()
        if old:
            saved = json.loads(old['payload'])
            if saved['outcome'] != outcome or saved['purpose'] != manifest['purpose']:
                raise DomainError('process_source_conflict', 'Source identity already names a different outcome.', 409)
            return self.get(old['id'])
        # Receipts remain inspectable through their run IDs even when a combined manifest needs trimming.
        try:
            encoded = encode(manifest, 49152)
        except DomainError as error:
            if error.code != 'payload_too_large':
                raise
            manifest['execution'] = [{key: run[key] for key in ('runId', 'attemptId', 'workSessionId')} for run in runs]
            manifest['omitted']['execution/provenance'] = 'byte_budget; read linked run receipts'
            encoded = encode(manifest, 49152)
        identifier, digest = uuid4().hex, fingerprint(manifest)
        connection.execute('INSERT INTO process_evidence VALUES(?,?,?,?,?,?)',
                           (identifier, source_key, issue['id'], digest, encoded, timestamp()))
        return self.get(identifier)

    def current(self, identifier):
        """Reject stale source revisions before any knowledge reuse or mutation."""
        evidence = self.get(identifier)
        manifest = evidence['manifest']
        issue = self.store.get(manifest['issueId'])
        if (issue['definitionRevision'] != manifest['definitionRevision'] or
                issue['resultGeneration'] != manifest['resultGeneration'] or
                source_digest(issue['currentResult']) != manifest['resultDigest'] or
                source_digest(issue['criticReview']) != manifest['reviewDigest']):
            raise DomainError('process_source_changed', 'Evidence no longer matches current task revisions.', 409)
        return evidence

    def get(self, identifier):
        """Read one immutable manifest; it never renews activity or starts a model."""
        row = self.store.db.connection.execute('SELECT * FROM process_evidence WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise DomainError('process_evidence_not_found', 'Evidence not found.', 404)
        return {'id': row['id'], 'digest': row['digest'], 'manifest': json.loads(row['payload'])}

    def list(self, issue_id, limit=20):
        """Read at most50manifest references for one canonical task without raw history."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DomainError('invalid_page', 'Choose limit1..50.')
        issue = self.store._row(issue_id)
        rows = self.store.db.connection.execute(
            'SELECT id,digest,created_at FROM process_evidence WHERE issue_id=? ORDER BY rowid DESC LIMIT ?',
            (issue['id'], limit + 1)).fetchall()
        return {'items': [dict(row) for row in rows[:limit]], 'hasMore': len(rows) > limit}
