"""External agent-neutral task driver; Tasks core never launches models or selects a vendor."""

import asyncio
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .issues import DomainError
from .memory_recommendation import memory_recommendation
from .service_values import encode


class ArtifactCheck:
    """Host-owned read-only pilot verifier, independent of model output and recipe choice."""

    def __init__(self, workspace, relative_path, expected_digest, criterion_id='artifact'):
        """Bind an exact regular artifact under the explicitly configured workspace."""
        self.workspace = Path(workspace).resolve(strict=True)
        self.relative = Path(relative_path)
        if self.relative.is_absolute() or '..' in self.relative.parts or not self.relative.parts:
            raise ValueError('Choose an artifact relative to the configured workspace.')
        self.expected_digest, self.criterion_id = expected_digest, criterion_id

    def check(self):
        """Verify bytes at the last responsible moment; symlink escapes and large artifacts fail closed."""
        path = (self.workspace / self.relative).resolve(strict=True)
        if not path.is_relative_to(self.workspace) or not path.is_file() or path.stat().st_size > 1048576:
            raise DomainError('process_artifact_unavailable', 'Artifact is outside the bounded workspace.', 409)
        content = path.read_bytes()
        digest = sha256(content).hexdigest()
        if digest != self.expected_digest:
            raise DomainError('process_artifact_changed', 'Artifact digest changed.', 409)
        return {'path': self.relative.as_posix(), 'sha256': digest, 'bytes': len(content)}


class ProcessWorker:
    """Translate only role-approved actions into existing fenced task APIs."""

    def __init__(self, client, adapter, *, worker_id, timeout=120):
        """Use explicit dependencies and a bounded provider timeout, with no implicit executable/model."""
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError('Worker timeout must be1..120seconds.')
        self.client, self.adapter = client, adapter
        self.worker_id, self.timeout = worker_id, timeout
        self.epoch = uuid4().hex

    def _command(self, operation, payload, key=None):
        """Issue one new intent; ambiguous responses never cause blind replay or changed keys."""
        return self.client.command('process.' + operation, payload, key or uuid4().hex)

    def _work(self, issue_id, operation, credentials):
        """Keep real work/session credentials on the driver side of the adapter boundary."""
        return self.client.post('issues/' + quote(issue_id, safe='') + '/work/' + operation, credentials)

    def _ready(self, issue, verifier):
        """Populate only the exact machine-verifiable pilot criterion; arbitrary tasks require their own verifier."""
        artifact = verifier.check()
        evidence = 'Artifact SHA256 verified: ' + artifact['sha256']
        updates = {}
        for section in ('dor', 'dod'):
            items = issue[section]
            if not items or any(item['id'] != verifier.criterion_id for item in items):
                raise DomainError('process_verifier_required', 'This pilot cannot attest unrelated task criteria.', 409)
            updates[section] = [{**item, 'passed': True, 'evidence': evidence} for item in items]
        if issue['dor'] != updates['dor'] or issue['dod'] != updates['dod']:
            issue = self.client.command('issue.update', {'issueId': issue['id'], 'changes': updates}, uuid4().hex)
        return issue

    async def task_cycle(self, issue_id, verifier, *, run_key):
        """Run producer and fresh artifact-first critic without human forms or manufactured acceptance."""
        issue = self._ready(self.client.issue(issue_id), verifier)
        produced = await self._role(issue, 'producer', verifier, run_key + ':producer')
        critic = self.client.issue(produced['criticReviewId'])
        reviewed = await self._role(critic, 'critic', verifier, run_key + ':critic')
        evidence = self.client.read('/api/v1/process/evidence?issueId=' + issue['id'])
        return {'issue': reviewed, 'evidence': evidence, 'memoryRecommendation': memory_recommendation()}

    async def _role(self, issue, role, verifier, run_id):
        """Reserve before inference, renew while running and record one terminal observation on every exit."""
        try:
            self.client.read('/api/v1/process/runs/' + quote(run_id, safe=''))
        except DomainError as error:
            if error.status != 404:
                raise
        else:
            raise DomainError('process_run_exists', 'Inspect the original delivery instead of replaying it.', 409)
        actor = self.worker_id + ':' + role
        key = self._key(run_id, 'work')
        credentials = {'agentId': actor, 'sessionKey': key}
        started = self._work(issue['id'], 'start', credentials)
        credentials['sessionId'] = started['work']['activeSession']['sessionId']
        identity = {'runId': run_id, 'workerId': actor, 'workerEpoch': self.epoch, 'fence': 1}
        reserved = False
        finished = False
        try:
            attempt = self._admit_attempt(issue['id'], run_id, actor)
            identity['fence'] = attempt['fence']
            self._command('run.reserve', {'runId': run_id, 'issueId': issue['id'], 'purpose': 'task',
                'workerId': actor, 'workerEpoch': self.epoch, 'attemptId': attempt['attemptId'], 'fence': attempt['fence'],
                'expectedDefinitionRevision': started['definitionRevision'],
                'expectedResultGeneration': started['resultGeneration']}, self._key(run_id, 'reserve'))
            reserved = True
            self.client.command('attempt.dispatch', {name: attempt[name] for name in ('attemptId', 'workerId', 'workerEpoch', 'fence')},
                                self._key(run_id, 'dispatch'))
            params = self._params(started, role, verifier, run_id)
            provider = await self._invoke(params, started, credentials, identity, verifier)
            artifact = verifier.check()
            result = provider['result']
            if not any(receipt['kind'] == 'artifact_read' and receipt['artifactDigest'] == artifact['sha256']
                       for receipt in result['receipts']):
                raise DomainError('process_artifact_receipt_required', 'Adapter did not report inspecting this artifact.', 409)
            reply = self._command('run.commit', {**identity, 'credentials': credentials,
                'action': result['action'], 'output': result['output'], 'usage': result['usage'],
                'provenance': self._provenance(provider['profile'], artifact, result)}, self._key(run_id, 'commit'))
            finished = True
            return reply['issue']
        finally:
            if reserved and not finished:
                try:
                    current = self.client.issue(issue['id'])
                    self._command('run.finish', {**identity, 'outcome': 'unknown',
                        'coverage': 'External adapter interrupted or output could not be safely committed.',
                        'expectedDefinitionRevision': current['definitionRevision'],
                        'expectedResultGeneration': current['resultGeneration']}, self._key(run_id, 'unknown'))
                except Exception:
                    # The server's bounded sweep owns abandoned evidence when this adapter cannot report it.
                    pass
            try:
                self._work(issue['id'], 'stop', credentials)
            except Exception:
                pass

    @staticmethod
    def _key(run_id, stage):
        """Keep stable command identities bounded even for a maximum-length external run key."""
        return 'process-' + sha256(run_id.encode()).hexdigest() + ':' + stage

    def _admit_attempt(self, issue_id, run_id, actor):
        """Reuse queue claim/dispatch/release semantics rather than invent process ownership."""
        lane = 'process-' + sha256(run_id.encode()).hexdigest()
        self.client.command('queue.create', {'laneId': lane, 'title': 'External worker protocol request',
            'profile': {'id': 'external-worker-v1', 'version': '1', 'protocol': 'worker-json', 'protocolVersion': 1}},
            self._key(run_id, 'lane'))
        self.client.command('queue.enqueue', {'laneId': lane, 'issueId': issue_id,
            'text': 'Host-configured external role request; no executable or model selected by task data.'}, self._key(run_id, 'enqueue'))
        return self.client.command('attempt.claim', {'laneId': lane, 'workerId': actor, 'workerEpoch': self.epoch},
                                   self._key(run_id, 'claim'))

    @staticmethod
    def _provenance(profile, artifact, result=None):
        """Keep actual verifier evidence distinct from adapter-reported tool/release observations."""
        return {'profile': profile, 'artifacts': [artifact],
            'checks': [{'kind': 'artifact_sha256', 'artifactDigest': artifact['sha256'], 'source': 'host-artifact-verifier/v1'}],
            'receipts': result['receipts'] if result else [],
            'release': result['release'] if result else {'state': 'unknown', 'evidence': 'External request not completed'}}

    def _params(self, issue, role, verifier, run_id):
        """Give a critic requirements/artifact pins without the producer's conclusions."""
        artifact = verifier.check()
        if role == 'critic':
            parent = self.client.issue(issue['parentId'])
            source = {'issueId': parent['id'], 'definitionRevision': parent['definitionRevision'],
                      'resultGeneration': parent['resultGeneration'], 'reviewedHead': parent['reviewedHead']}
            inputs = {'requirements': {'title': parent['title'], 'description': parent['description'], 'dod': parent['dod']},
                      'target': source, 'instruction': 'Inspect the artifact independently before reviewing the requirements.'}
        else:
            inputs = self.client.read('/api/v1/issues/' + issue['id'] + '/context?maxBytes=8192')
        inputs['memoryRecommendation'] = memory_recommendation()
        params = {'runId': run_id, 'role': role,
            'scope': {key: issue[key] for key in ('id', 'spaceId', 'definitionRevision', 'resultGeneration')},
            'workspace': {'id': 'configured-workspace', 'path': str(verifier.workspace)},
            'artifacts': [artifact], 'input': inputs,
            'budget': {'timeoutSeconds': self.timeout, 'maxOutputBytes': 16384}}
        params['scope']['issueId'] = params['scope'].pop('id')
        encode(params, 24000)
        return params

    async def _invoke(self, params, issue, credentials, identity, verifier):
        """Drive liveness independently of model output and cancel inference if ownership is lost."""
        async def check_owner(profile):
            """Recheck current task/run immediately before the only external send."""
            run = self._command('run.heartbeat', identity)
            if run['cancelRequested']:
                raise DomainError('process_cancelled', 'Owned request was cancelled before send.', 409)
            self._command('run.observe', {**identity, 'provenance': self._provenance(profile, verifier.check())})

        async def renew():
            """Renew both existing work and delivery leases every25seconds; cancellation stays explicit."""
            while True:
                await asyncio.sleep(25)
                self._work(issue['id'], 'heartbeat', credentials)
                run = self._command('run.heartbeat', identity)
                if run['cancelRequested']:
                    raise DomainError('process_cancelled', 'Owned request was cancelled.', 409)

        inference = asyncio.create_task(self.adapter.invoke(params, before_send=check_owner))
        heartbeat = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait((inference, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
            return await inference
        finally:
            inference.cancel()
            heartbeat.cancel()
            await asyncio.gather(inference, heartbeat, return_exceptions=True)


def main():
    """Run one explicit host-configured audit cycle; no installed agent is auto-discovered."""
    import argparse
    import json
    from .process_client import ProcessClient
    from .service_values import fields
    from .worker_transports import HttpAgent, JsonlAgent
    parser = argparse.ArgumentParser(description='Run one agent-neutral artifact audit through Tasks.')
    parser.add_argument('--config', type=Path, required=True, help='Explicit host-owned JSON configuration')
    options = parser.parse_args()
    if options.config.stat().st_size > 16384:
        parser.error('Configuration exceeds16KiB')
    profile = json.loads(options.config.read_text(encoding='utf-8'))
    required = {'tasksOrigin', 'workerId', 'runKey', 'issueId', 'workspace', 'artifact', 'sha256', 'adapter'}
    fields(profile, required, required)
    adapter = profile['adapter']
    if adapter.get('transport') == 'jsonl':
        fields(adapter, {'transport', 'argv'}, {'transport', 'argv'})
        provider = JsonlAgent(adapter['argv'])
    elif adapter.get('transport') == 'http':
        fields(adapter, {'transport', 'origin'}, {'transport', 'origin'})
        provider = HttpAgent(adapter['origin'])
    else:
        parser.error('Choose an explicit jsonl or http adapter')
    client = ProcessClient(profile['tasksOrigin'], profile['workerId'])
    try:
        verifier = ArtifactCheck(profile['workspace'], profile['artifact'], profile['sha256'])
        result = asyncio.run(ProcessWorker(client, provider, worker_id=profile['workerId']).task_cycle(
            profile['issueId'], verifier, run_key=profile['runKey']))
        print(encode({'issueId': result['issue']['id'], 'status': result['issue']['status'],
                      'evidence': result['evidence'], 'memoryRecommendation': result['memoryRecommendation']}))
    finally:
        client.close()


if __name__ == '__main__':
    main()
