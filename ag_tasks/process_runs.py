"""Single-use external delivery journal, fenced by real task work and UTC leases."""

import json

from .issues import DomainError, timestamp, bounded_text
from .service_values import fields, identifier, encode

IDENTITY = {'runId', 'workerId', 'workerEpoch', 'fence'}


class ProcessRuns:
    """Never relaunch uncertain delivery or treat foreground completion as runtime release."""

    def __init__(self, store, evidence):
        """Share authoritative work storage; no model client or process is owned here."""
        self.store, self.evidence = store, evidence

    def get(self, run_id):
        """Read the original delivery receipt without renewing its lease."""
        row = self.store.db.connection.execute('SELECT payload FROM process_runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise DomainError('process_run_not_found', 'Run not found.', 404)
        return json.loads(row['payload'])

    def save(self, run):
        """Persist the bounded state and searchable terminal marker in one transaction."""
        self.store.db.connection.execute('UPDATE process_runs SET state=?,payload=? WHERE id=?',
                                        (run['state'], encode(run), run['runId']))

    def reserve(self, payload):
        """Commit intent before external delivery, using an already admitted exact work session."""
        required = {'runId', 'issueId', 'purpose', 'workerId', 'workerEpoch',
                    'attemptId', 'fence', 'expectedDefinitionRevision', 'expectedResultGeneration'}
        fields(payload, required, required)
        for name in ('runId', 'workerId', 'workerEpoch'):
            identifier(payload[name])
        if payload['purpose'] != 'task':
            raise DomainError('invalid_process_purpose', 'Unknown run purpose.')
        existing = self.store.db.connection.execute('SELECT id FROM process_runs WHERE id=?', (payload['runId'],)).fetchone()
        if existing:
            old = self.get(payload['runId'])
            if old['intent'] != payload:
                raise DomainError('process_run_conflict', 'Run identity already names another intent.', 409)
            return old
        issue = self.store.get(payload['issueId'])
        self._versions(issue, payload['expectedDefinitionRevision'], payload['expectedResultGeneration'])
        active = issue['work']['activeSession']
        if not active or active['agentId'] != payload['workerId']:
            raise DomainError('process_work_required', 'Start real task work before reserving delivery.', 409)
        attempt = self.store.service.queue.worker._owned(
            {name: payload[name] for name in ('attemptId', 'workerId', 'workerEpoch', 'fence')}, self.store.db.connection)
        item = self.store.service.queue.item(attempt['item_id'])
        if item['issueId'] != issue['id']:
            raise DomainError('process_attempt_conflict', 'Attempt belongs to another task.', 409)
        busy = False
        rows = self.store.db.connection.execute('SELECT payload FROM process_runs WHERE issue_id=?', (issue['id'],)).fetchall()
        for row in rows:
            old = json.loads(row['payload'])
            if old['runtimeRelease'] == 'proved':
                continue
            previous = self.store.service.queue.attempt(old['attemptId'])
            if previous['state'] == 'released' and previous['releaseProof']:
                if old['intent']['purpose'] == 'task':
                    old['runtimeRelease'], old['releaseProof'] = 'proved', previous['releaseProof']
                    self.save(old)
            else:
                busy = True
        if busy:
            raise DomainError('process_run_busy', 'An admitted or uncertain delivery already owns this task.', 409)
        run = {'runId': payload['runId'], 'issueId': issue['id'], 'intent': payload,
               'workerId': payload['workerId'], 'workerEpoch': payload['workerEpoch'], 'fence': payload['fence'],
               'attemptId': payload['attemptId'], 'conversationId': None,
               'state': 'reserved', 'workSessionId': active['sessionId'], 'terminalRevision': 0,
               'incarnation': self.store.db.queue_incarnation, 'cancelRequested': False,
               'leaseUntil': attempt['lease_expires_at'], 'createdAt': timestamp(), 'runtimeRelease': 'unknown'}
        if issue['kind'] == 'review' and issue['parentId']:
            parent = self.store.get(issue['parentId'])
            run['reviewTarget'] = {name: parent[name] for name in ('id', 'definitionRevision', 'resultGeneration', 'reviewedHead')}
        self.store.db.connection.execute('INSERT INTO process_runs VALUES(?,?,?,?,?,?,?,?)',
            (run['runId'], issue['id'], payload['purpose'], 'reserved', payload['workerId'],
             issue['definitionRevision'], issue['resultGeneration'], encode(run)))
        return run

    @staticmethod
    def _versions(issue, definition, generation):
        """Refuse booleans and stale generations, even when their numeric values compare equal."""
        if (type(definition) is not int or type(generation) is not int or
                definition != issue['definitionRevision'] or generation != issue['resultGeneration']):
            raise DomainError('process_version_conflict', 'Task versions changed.', 409)

    def owned(self, payload):
        """Validate immutable worker identity and current lease before every external effect."""
        run = self.get(payload['runId'])
        if run['intent']['purpose'] != 'task':
            raise DomainError('historical_run_read_only', 'Historical learning runs are inert and read-only.', 409)
        same = all(payload[name] == run[name] for name in IDENTITY)
        if (not same or type(payload['fence']) is not int or run['state'] != 'reserved' or
                run['incarnation'] != self.store.db.queue_incarnation or run['leaseUntil'] <= timestamp()):
            raise DomainError('process_ownership_lost', 'Read the run; uncertain delivery cannot be reclaimed.', 409)
        self.store.service.queue.worker._owned(self.attempt_identity(run), self.store.db.connection)
        return run

    @staticmethod
    def attempt_identity(run):
        """Reuse the existing queue fence and release/reconciliation authority."""
        return {name: run[name] for name in ('attemptId', 'workerId', 'workerEpoch', 'fence')}

    def heartbeat(self, payload):
        """Renew only live ownership; refresh neither task acceptance nor another worker's lease."""
        fields(payload, IDENTITY, IDENTITY)
        run = self.owned(payload)
        issue = self.store.get(run['issueId'])
        self._versions(issue, run['intent']['expectedDefinitionRevision'], run['intent']['expectedResultGeneration'])
        active = issue['work']['activeSession']
        if not active or active['sessionId'] != run['workSessionId']:
            raise DomainError('process_ownership_lost', 'Task work session changed.', 409)
        attempt = self.store.service.queue.worker.heartbeat(self.attempt_identity(run))
        run['leaseUntil'] = attempt['leaseExpiresAt']
        run['cancelRequested'] = run['cancelRequested'] or attempt['cancelRequested']
        self.save(run)
        return run

    def observe(self, payload):
        """Retain bounded adapter/check provenance before external run delivery or output commit."""
        fields(payload, IDENTITY | {'provenance'}, IDENTITY | {'provenance'})
        run = self.owned(payload)
        run['provenance'] = self.validate_provenance(payload['provenance'])
        self.save(run)
        return run

    @staticmethod
    def validate_provenance(value):
        """Persist only profile, artifact/check descriptors and declared tool/release receipts."""
        allowed = {'profile', 'artifacts', 'checks', 'receipts', 'release'}
        fields(value, allowed, {'profile', 'artifacts', 'checks', 'receipts', 'release'})
        from .worker_protocol import capabilities
        capabilities(value['profile'], 'producer')
        for name in ('artifacts', 'checks', 'receipts'):
            if type(value[name]) is not list or len(value[name]) > 20:
                raise DomainError('invalid_process_provenance', 'Evidence collections are bounded.')
        import re
        from pathlib import PurePosixPath
        for artifact in value['artifacts']:
            fields(artifact, {'path', 'sha256', 'bytes'}, {'path', 'sha256', 'bytes'})
            path = bounded_text(artifact['path'], 1024, empty=False)
            if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path:
                raise DomainError('invalid_process_provenance', 'Artifact path must be relative.')
            if type(artifact['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', artifact['sha256']):
                raise DomainError('invalid_process_provenance', 'Artifact digest is required.')
            if type(artifact['bytes']) is not int or artifact['bytes'] < 0:
                raise DomainError('invalid_process_provenance', 'Artifact byte count is invalid.')
        for entry in value['receipts'] + value['checks']:
            fields(entry, {'kind', 'artifactDigest', 'source'}, {'kind', 'artifactDigest', 'source'})
            if entry['kind'] not in ('artifact_read', 'tool_result', 'artifact_sha256'):
                raise DomainError('invalid_process_provenance', 'Unknown evidence kind.')
            if type(entry['artifactDigest']) is not str or not re.fullmatch('[a-f0-9]{64}', entry['artifactDigest']):
                raise DomainError('invalid_process_provenance', 'Evidence digest is required.')
            bounded_text(entry['source'], 128, empty=False)
        fields(value['release'], {'state', 'evidence'}, {'state', 'evidence'})
        if value['release']['state'] not in ('proved', 'unknown'):
            raise DomainError('invalid_process_provenance', 'Release must retain uncertainty.')
        bounded_text(value['release']['evidence'], 512)
        encode(value, 16384)
        return value

    def commit(self, payload):
        """Atomically fence output consumption, existing result/review gates and terminal evidence."""
        fields(payload, IDENTITY | {'credentials', 'action', 'output', 'provenance', 'usage'},
               IDENTITY | {'credentials', 'action', 'output', 'provenance', 'usage'})
        run = self.owned(payload)
        issue = self.store.get(run['issueId'])
        attempt = self.store.service.queue.attempt(run['attemptId'])
        if run['cancelRequested'] or attempt['cancelRequested']:
            raise DomainError('process_cancelled', 'Cancelled delivery cannot commit model output.', 409)
        original = run['intent']
        self._versions(issue, original['expectedDefinitionRevision'], original['expectedResultGeneration'])
        credentials = self.store.execution.credentials(payload['credentials'])
        if credentials['agentId'] != run['workerId'] or credentials['sessionId'] != run['workSessionId']:
            raise DomainError('process_ownership_lost', 'Work credentials differ from admitted delivery.', 409)
        self.store.execution.session(self.store.db.connection, issue['id'], credentials)
        run['provenance'] = self.validate_provenance(payload['provenance'])
        from .worker_protocol import role_result
        role_result({'action': payload['action'], 'output': payload['output'],
                     'receipts': run['provenance']['receipts'], 'release': run['provenance']['release'],
                     'usage': payload['usage']}, 'critic' if issue['kind'] == 'review' else 'producer')
        self.save(run)
        if issue['kind'] == 'review':
            if payload['action'] != 'review_report':
                raise DomainError('worker_action_forbidden', 'Critic cannot submit a producer result.', 403)
            parent = self.store.get(issue['parentId'])
            target = run['reviewTarget']
            if any(parent[name] != target[name] for name in target):
                raise DomainError('process_version_conflict', 'Review target changed after admission.', 409)
            output = payload['output']
            fields(output, {'verdict', 'findings', 'evidence'}, {'verdict', 'findings', 'evidence'})
            result = self.store.results.report(issue['id'], {**credentials,
                'generation': target['resultGeneration'], 'definitionRevision': target['definitionRevision'],
                'reviewedHead': target['reviewedHead'], 'verdict': output['verdict'],
                'findings': output['findings'], 'summary': 'External agent artifact review', 'evidence': output['evidence']})
        else:
            if payload['action'] != 'submit_result':
                raise DomainError('worker_action_forbidden', 'Producer cannot report a critic verdict.', 403)
            result = self.store.results.submit(issue['id'], {**credentials, 'result': payload['output']})
        from .worker_protocol import validate_usage
        validate_usage(payload['usage'])
        run['committedAction'] = payload['action']
        run['usage'] = payload['usage']
        terminal = self._terminal(run, 'partial', 'Foreground action committed atomically; acceptance/release are separate.')
        return {'issue': result, 'run': terminal}

    def cancel(self, payload):
        """Request cancellation without claiming the adapter or its descendants stopped."""
        fields(payload, IDENTITY, IDENTITY)
        run = self.owned(payload)
        run['cancelRequested'] = True
        self.save(run)
        return run

    def finish(self, payload):
        """Record one exact terminal revision; output consumption never permits a second dispatch."""
        required = IDENTITY | {'outcome', 'coverage', 'expectedDefinitionRevision', 'expectedResultGeneration'}
        fields(payload, required | {'usage'}, required)
        run = self.owned(payload)
        issue = self.store.get(run['issueId'])
        self._versions(issue, payload['expectedDefinitionRevision'], payload['expectedResultGeneration'])
        original = run['intent']
        if payload['expectedDefinitionRevision'] != original['expectedDefinitionRevision']:
            raise DomainError('process_version_conflict', 'Definition changed during execution.', 409)
        generation = payload['expectedResultGeneration']
        latest = self.store.db.connection.execute(
            'SELECT id FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT 1', (run['issueId'],)).fetchone()
        if latest is None or latest['id'] != run['workSessionId']:
            raise DomainError('process_ownership_lost', 'A different work session superseded this delivery.', 409)
        if generation != original['expectedResultGeneration']:
            result = issue['currentResult'] or {}
            source = self.store.db.connection.execute(
                'SELECT producer_session_id FROM results WHERE issue_id=? AND generation=?',
                (run['issueId'], generation)).fetchone()
            if (generation != original['expectedResultGeneration'] + 1 or result.get('producerAgentId') != run['workerId'] or
                    source is None or source['producer_session_id'] != run['workSessionId']):
                raise DomainError('process_version_conflict', 'New result was not created by this worker.', 409)
        if payload['outcome'] not in ('accepted', 'rework', 'failed', 'partial', 'unknown'):
            raise DomainError('invalid_process_outcome', 'Unknown terminal outcome.')
        coverage = bounded_text(payload['coverage'], 512, empty=False)
        if 'usage' in payload:
            # Only aggregate counters are retained; adapter output/credentials are never evidence fields.
            from .worker_protocol import validate_usage
            validate_usage(payload['usage'])
            run['usage'] = payload['usage']
        return self._terminal(run, payload['outcome'], coverage)

    def _terminal(self, run, outcome, coverage):
        """Commit a deterministic source terminal revision and one durable execution evidence record."""
        run['state'] = 'unknown' if outcome == 'unknown' else 'finished'
        run['outcome'], run['terminalRevision'] = outcome, 1
        run['endedAt'] = timestamp()
        self.save(run)
        # Report through the existing attempt state machine; finished still reserves its lane.
        try:
            self.store.service.queue.worker.report({**self.attempt_identity(run),
                'state': 'unknown' if outcome == 'unknown' else 'finished', 'outcome': outcome})
        except DomainError:
            if outcome != 'unknown':
                raise
        from hashlib import sha256
        source = 'run:' + sha256(run['runId'].encode()).hexdigest() + ':1'
        evidence = self.evidence.capture({'issueId': run['issueId'], 'sourceId': source,
            'outcome': outcome, 'coverage': coverage, 'purpose': 'task'})
        run['evidenceId'] = evidence['id']
        self.save(run)
        return run

    def sweep(self, *, restart=False, limit=100):
        """Recover abandoned delivery without a live driver; never spawn or free an uncertain lane."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError('invalid_page', 'Choose limit1..100.')
        expired = []
        with self.store.db.transaction() as connection:
            condition = "json_extract(payload,'$.leaseUntil') <= ?"
            arguments = [timestamp()]
            if restart:
                condition += " OR json_extract(payload,'$.incarnation') != ?"
                arguments.append(self.store.db.queue_incarnation)
            rows = connection.execute("SELECT payload FROM process_runs WHERE state='reserved' AND purpose='task' AND json_extract(payload,'$.intent.purpose')='task' AND (" + condition +
                                      ') ORDER BY rowid LIMIT ?', (*arguments, limit)).fetchall()
            for row in rows:
                run = json.loads(row['payload'])
                if run['leaseUntil'] <= timestamp() or restart and run['incarnation'] != self.store.db.queue_incarnation:
                    self._terminal(run, 'unknown', 'Driver lease expired or owner restarted; delivery/release unknown.')
                    expired.append(run['runId'])
        return expired
