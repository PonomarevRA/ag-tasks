"""Fault/transport checks use a private store and deterministic external agents, never user data."""

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from ag_tasks.app import create_app
from ag_tasks.issues import IssueStore, DomainError
from ag_tasks.process_client import ProcessClient
from ag_tasks.process_worker import ArtifactCheck, ProcessWorker
from ag_tasks.worker_transports import JsonlAgent, HttpAgent
from ag_tasks.worker_protocol import response, request, role_result
from workflow_helpers import metadata, review_ready

FIXTURE = Path(__file__).parent / 'fixtures' / 'process_worker.py'


def command(store, operation, payload, key=None):
    """Go through the actual atomic service command journal."""
    return store.service.execute({'clientId': 'process-tests', 'commandId': key or uuid4().hex,
                                 'operation': 'process.' + operation, 'payload': payload})


def working(store):
    """Admit real test work; the source session is separate from delivery ownership."""
    issue = store.create({'title': 'Private task', **metadata()})
    return store.execution.start(issue['id'], {'agentId': 'worker', 'sessionKey': 'work'})


def reserve(store, issue, run='run-a'):
    """Reserve one immutable intent under current source versions."""
    def queue(operation, payload):
        """Use the existing queue owner for every new delivery reservation."""
        return store.service.execute({'clientId': 'process-tests', 'commandId': uuid4().hex,
            'operation': operation, 'payload': payload})
    try:
        return store.service.process.runs.get(run)
    except DomainError as error:
        if error.status != 404:
            raise
    lane = queue('queue.create', {'laneId': run, 'title': 'Fixture lane',
        'profile': {'id': 'fixture', 'version': '1', 'protocol': 'worker-json', 'protocolVersion': 1}})
    queue('queue.enqueue', {'laneId': lane['id'], 'issueId': issue['id'], 'text': 'Fixture role'})
    attempt = queue('attempt.claim', {'laneId': lane['id'], 'workerId': 'worker', 'workerEpoch': 'epoch'})
    queue('attempt.dispatch', {key: attempt[key] for key in ('attemptId', 'workerId', 'workerEpoch', 'fence')})
    return command(store, 'run.reserve', {'runId': run, 'issueId': issue['id'], 'workerId': 'worker',
        'attemptId': attempt['attemptId'], 'fence': attempt['fence'],
        'workerEpoch': 'epoch', 'purpose': 'task', 'expectedDefinitionRevision': issue['definitionRevision'],
        'expectedResultGeneration': issue['resultGeneration']})


def identity(run):
    """Select the exact fence expected by heartbeat/finish/cancel."""
    return {key: run[key] for key in ('runId', 'workerId', 'workerEpoch', 'fence')}


def test_abandoned_run_survives_restart_and_retains_one_terminal_record(tmp_path):
    """A dead external process is not needed to produce its unknown execution evidence."""
    store = IssueStore(tmp_path)
    issue = working(store)
    run = reserve(store, issue)
    store.close()
    store = IssueStore(tmp_path)
    try:
        saved = store.service.process.runs.get(run['runId'])
        assert saved['state'] == 'unknown' and saved['runtimeRelease'] == 'unknown'
        evidence = store.service.process.evidence.get(saved['evidenceId'])
        assert evidence['manifest']['outcome'] == 'unknown'
        assert store.service.process.runs.sweep(restart=True) == []
        assert store.db.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='process_reflections'").fetchone()[0] == 0
        with pytest.raises(DomainError, match='uncertain'):
            command(store, 'run.heartbeat', identity(run))
        assert evidence['digest'] == store.service.process.evidence.get(saved['evidenceId'])['digest']
    finally:
        store.close()


def test_command_ack_retry_fences_and_source_conflicts(tmp_path):
    """Lost ACK is recoverable while another intent/worker cannot reuse ownership."""
    store = IssueStore(tmp_path)
    try:
        issue = working(store)
        run = reserve(store, issue)
        again = reserve(store, issue)
        assert run == again
        bad = {**identity(run), 'workerEpoch': 'other'}
        with pytest.raises(DomainError):
            command(store, 'run.heartbeat', bad)
        with pytest.raises(DomainError):
            reserve(store, issue, 'second-run')
        final = command(store, 'run.finish', {**identity(run), 'outcome': 'partial', 'coverage': 'Fixture interruption',
            'expectedDefinitionRevision': issue['definitionRevision'], 'expectedResultGeneration': 0}, 'finish')
        assert final['terminalRevision'] == 1
        assert command(store, 'run.finish', {**identity(run), 'outcome': 'partial', 'coverage': 'Fixture interruption',
            'expectedDefinitionRevision': issue['definitionRevision'], 'expectedResultGeneration': 0}, 'finish') == final
        manifest = store.service.process.evidence.get(final['evidenceId'])
        assert 'sessionKey' not in json.dumps(manifest)
        assert store.db.connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='process_reflections'").fetchone()[0] == 0
        with pytest.raises(DomainError, match='uncertain delivery'):
            reserve(store, issue, 'after-finished-but-unreleased')
    finally:
        store.close()


def test_expired_lease_never_revives_and_retention_does_not_remove_manifest(tmp_path):
    """Expiry has one terminal revision, independent of transient observations."""
    store = IssueStore(tmp_path)
    try:
        issue = working(store)
        run = reserve(store, issue)
        run['leaseUntil'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        store.service.process.runs.save(run)
        with pytest.raises(DomainError):
            command(store, 'run.heartbeat', identity(run))
        expired = store.service.process.runs.get(run['runId'])
        store.service.session_retention.sweep(now=datetime.now(timezone.utc) + timedelta(days=4))
        assert store.service.process.evidence.get(expired['evidenceId'])['manifest']['outcome'] == 'unknown'
    finally:
        store.close()


def test_current_critic_automatically_creates_evidence_and_future_schema_is_refused(tmp_path):
    """Existing completion remains intact; new evidence is derived from the real review result."""
    store = IssueStore(tmp_path)
    issue = store.create({'title': 'Reviewed'})
    review_ready(store, issue['id'])
    rows = store.service.process.evidence.list(issue['id'])['items']
    assert len(rows) == 1
    assert store.service.process.evidence.current(rows[0]['id'])['manifest']['outcome'] == 'accepted'
    store.db.connection.execute('UPDATE process_schema SET version=999')
    store.close()
    with pytest.raises(ValueError, match='process schema'):
        IssueStore(tmp_path)


@pytest.mark.parametrize('transport', ['jsonl', 'http'])
def test_two_neutral_transports_run_same_task_and_fresh_critic_without_manual_input(tmp_path, transport):
    """The product works with independently transported deterministic adapters and no vendor CLI."""
    artifact = tmp_path / 'artifact.txt'
    artifact.write_text('fixed audit artifact')
    verifier = ArtifactCheck(tmp_path, 'artifact.txt', sha256(artifact.read_bytes()).hexdigest())
    spec = importlib.util.spec_from_file_location('fixture_worker', FIXTURE)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    with TestClient(create_app(tmp_path / 'store'), base_url='http://127.0.0.1:8895') as http:
        def handle(request):
            """Use the actual HTTP owner while keeping synthetic tasks outside the live store."""
            reply = http.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)
        client = ProcessClient('http://127.0.0.1:8895', 'external-test', client=httpx.Client(transport=httpx.MockTransport(handle)))
        criterion = [{'id': 'artifact', 'text': 'Verify fixed artifact bytes', 'passed': False, 'evidence': ''}]
        issue = client.command('issue.create', {'title': 'Audit artifact', 'intendedRole': 'developer',
            'dor': criterion, 'dod': criterion}, 'create')
        seen_roles = []
        previous = tmp_path / 'artifacts' / 'ag-tasks' / 'existing.json'
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b'prior-user-artifact')
        prior_files = {p.relative_to(previous.parent): p.read_bytes() for p in previous.parent.rglob('*') if p.is_file()}
        async def agent_reply(request):
            """HTTP fixture uses identical frames and real artifact reads in a separate handler."""
            frame = json.loads(request.content)
            if frame['method'] == 'run':
                advice = frame['params']['input']['memoryRecommendation']
                assert advice['required'] is False and advice['whenUnavailable'] == 'skip'
                seen_roles.append(frame['params']['role'])
            return httpx.Response(200, json={'protocol': frame['protocol'], 'requestId': frame['requestId'],
                                            'result': fixture.reply(frame)})
        async def run():
            """Exercise actual adapter boundaries, heartbeat admission and result/critic gates."""
            if transport == 'jsonl':
                adapter = JsonlAgent([sys.executable, str(FIXTURE)])
                return await ProcessWorker(client, adapter, worker_id='external').task_cycle(issue['id'], verifier, run_key='cycle')
            async with httpx.AsyncClient(transport=httpx.MockTransport(agent_reply)) as agent_http:
                adapter = HttpAgent('http://127.0.0.1:7777', client=agent_http)
                return await ProcessWorker(client, adapter, worker_id='external').task_cycle(issue['id'], verifier, run_key='cycle')
        result = asyncio.run(run())
        assert result['issue']['status'] == 'done'
        assert result['memoryRecommendation']['blocksCompletion'] is False
        assert result['memoryRecommendation']['selectionOwner'] == 'agent'
        assert 'artifactExport' not in result
        assert {p.relative_to(previous.parent): p.read_bytes() for p in previous.parent.rglob('*') if p.is_file()} == prior_files
        assert result['issue']['criticReview']['agentId'] != result['issue']['currentResult']['producerAgentId']
        assert any(http.app.state.store.service.process.evidence.get(row['id'])['manifest']['outcome'] == 'accepted'
                   for row in result['evidence']['items'])
        accepted = next(http.app.state.store.service.process.evidence.get(row['id']) for row in result['evidence']['items']
            if http.app.state.store.service.process.evidence.get(row['id'])['manifest']['outcome'] == 'accepted')
        execution = accepted['manifest']['execution']
        assert execution and all(run['attemptId'] and run['workSessionId'] for run in execution)
        assert all(run['provenance']['profile']['providerId'] == 'fixture-agent' for run in execution)
        assert any(run['provenance']['receipts'][0]['artifactDigest'] == verifier.expected_digest for run in execution)
        if transport == 'http':
            assert seen_roles == ['producer', 'critic']
        with pytest.raises(DomainError):
            asyncio.run(run())
        assert client.issue(issue['id'])['status'] == 'done'
        client.close()


def test_protocol_rejects_wrong_ids_roles_unknown_cost_and_traversal():
    """Adapter text cannot become an arbitrary action or escape the selected artifact workspace."""
    frame = request('negotiate', 'request-1', {})
    with pytest.raises(DomainError):
        response({'protocol': frame['protocol'], 'requestId': 'wrong', 'result': {}}, frame)
    with pytest.raises(DomainError):
        role_result({'action': 'review_report'}, 'producer')


def test_new_work_same_agent_cannot_finish_old_delivery(tmp_path):
    """Declared identity reuse is insufficient when the underlying admitted session changed."""
    store = IssueStore(tmp_path)
    try:
        issue = working(store)
        run = reserve(store, issue)
        old = {'agentId': 'worker', 'sessionKey': 'work', 'sessionId': issue['work']['activeSession']['sessionId']}
        store.execution.stop(issue['id'], old)
        store.execution.start(issue['id'], {'agentId': 'worker', 'sessionKey': 'new-work'})
        with pytest.raises(DomainError, match='different work session'):
            command(store, 'run.finish', {**identity(run), 'outcome': 'partial', 'coverage': 'Old output',
                'expectedDefinitionRevision': issue['definitionRevision'], 'expectedResultGeneration': 0})
    finally:
        store.close()


def test_sweep_does_not_starve_expired_runs_behind_live_runs(tmp_path):
    """The SQL admission filter selects expired rows before the bounded batch limit."""
    store = IssueStore(tmp_path)
    try:
        first = reserve(store, working(store), 'live')
        second = reserve(store, working(store), 'expired')
        second['leaseUntil'] = '2000-01-01T00:00:00+00:00'
        store.service.process.runs.save(second)
        assert store.service.process.runs.sweep(limit=1) == ['expired']
        assert store.service.process.runs.get(first['runId'])['state'] == 'reserved'
    finally:
        store.close()


def test_expired_delivery_cannot_atomically_commit_a_result(tmp_path):
    """A late valid model answer must not create an accepted generation after ownership expired."""
    store = IssueStore(tmp_path)
    try:
        issue = working(store)
        run = reserve(store, issue)
        run['leaseUntil'] = '2000-01-01T00:00:00+00:00'
        store.service.process.runs.save(run)
        with pytest.raises(DomainError):
            command(store, 'run.commit', {**identity(run), 'credentials': {'agentId': 'worker',
                'sessionKey': 'work', 'sessionId': run['workSessionId']}, 'action': 'submit_result',
                'output': {'summary': 'Late', 'changes': [], 'validation': [], 'limitations': '', 'artifacts': []},
                'provenance': {}, 'usage': {'inputTokens': None, 'outputTokens': None, 'cost': None}})
        assert store.get(issue['id'])['resultGeneration'] == 0
        assert store.service.process.runs.get(run['runId'])['state'] == 'unknown'
    finally:
        store.close()


def test_existing_attempt_reconciliation_is_only_way_to_release_unknown_runtime(tmp_path):
    """Foreground finish stays exclusive until the existing guarded operator proof is recorded."""
    store = IssueStore(tmp_path)
    try:
        issue = working(store)
        run = reserve(store, issue)
        command(store, 'run.finish', {**identity(run), 'outcome': 'partial', 'coverage': 'Foreground only',
            'expectedDefinitionRevision': issue['definitionRevision'], 'expectedResultGeneration': 0})
        attempt = store.service.queue.attempt(run['attemptId'])
        assert attempt['state'] == 'finished' and attempt['releaseProof'] is None
        payload = {'attemptId': attempt['id'], 'expectedRevision': attempt['revision'],
            'resolution': 'owned_runtime_stopped', 'evidence': 'Fixture operator observed owned runtime stopped'}
        with pytest.raises(DomainError, match='operator'):
            store.service.execute({'clientId': 'test-release', 'commandId': 'denied',
                'operation': 'attempt.reconcile', 'payload': payload})
        store.service.execute({'clientId': 'test-release', 'commandId': 'allowed',
            'operation': 'attempt.reconcile', 'payload': payload}, actor='user')
        assert reserve(store, issue, 'next-after-proof')['state'] == 'reserved'
        assert store.service.process.runs.get(run['runId'])['runtimeRelease'] == 'proved'
    finally:
        store.close()


@pytest.mark.parametrize('cancel_source', ['run', 'queue'])
def test_cancelled_delivery_cannot_commit_valid_output(tmp_path, cancel_source):
    """Cancellation wins inside the same transaction that would create a result generation."""
    store = IssueStore(tmp_path)
    try:
        issue = working(store)
        run = reserve(store, issue)
        if cancel_source == 'run':
            command(store, 'run.cancel', identity(run))
        else:
            attempt = store.service.queue.attempt(run['attemptId'])
            item = store.service.queue.item(attempt['itemId'])
            store.service.execute({'clientId': 'cancel-test', 'commandId': 'cancel', 'operation': 'queue.cancel',
                'payload': {'itemId': item['id'], 'expectedRevision': item['revision']}})
        profile = {'providerId': 'fixture', 'providerVersion': '1', 'capabilities': {
            name: True for name in ('freshContext', 'boundedWorkspace', 'readArtifact', 'cancel', 'releaseEvidence', 'usage')}}
        with pytest.raises(DomainError) as rejected:
            command(store, 'run.commit', {**identity(run), 'credentials': {'agentId': 'worker',
                'sessionKey': 'work', 'sessionId': run['workSessionId']}, 'action': 'submit_result',
                'output': {'summary': 'Valid but cancelled', 'changes': [], 'validation': [], 'limitations': '', 'artifacts': []},
                'provenance': {'profile': profile, 'artifacts': [], 'checks': [], 'receipts': [],
                    'release': {'state': 'unknown', 'evidence': ''}},
                'usage': {'inputTokens': None, 'outputTokens': None, 'cost': None}})
        assert rejected.value.code == 'process_cancelled'
        assert store.get(issue['id'])['resultGeneration'] == 0
        assert command(store, 'run.heartbeat', identity(run))['cancelRequested'] is True
    finally:
        store.close()
