"""Contract checks against a real subprocess, with service failures injected at intent boundaries."""

import asyncio
import copy
import json
from pathlib import Path
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from ag_tasks.acp_worker import AcpWorker, WorkerStopped
from ag_tasks.acp_worker_transport import AcpTransportError
from ag_tasks.agent_worker_client import HttpWorkerClient
from ag_tasks.app import create_app
from ag_tasks.issues import DomainError

PROFILE = {'id': 'fixture', 'version': '1.0', 'protocol': 'acp', 'protocolVersion': 1}
FIXTURE = Path(__file__).parent / 'fixtures' / 'acp_worker_fixture.py'


class ServiceDouble:
    """Model only remote command replies; protocol execution happens in the actual fixture subprocess."""

    def __init__(self, *, cancel_after_prompt=False, fail=None, stale=False):
        """Inject a known failure before dispatch/start or after an outgoing prompt intent."""
        self.operations, self.events = [], []
        self.cancel_after_prompt, self.fail, self.stale = cancel_after_prompt, fail, stale
        self.marker = None
        self.value = {'id': 'attempt', 'attemptId': 'attempt', 'workerId': 'worker', 'workerEpoch': 'epoch',
                      'fence': 1, 'incarnation': 'service-first', 'profile': PROFILE, 'state': 'claimed',
                      'cancelRequested': False, 'item': {'id': 'item', 'text': 'Do not execute as shell', 'issueId': None}}

    def execute(self, operation, payload, command_id):
        """Retain wire records and fault at an explicit service boundary, with no implicit retry."""
        self.operations.append((operation, copy.deepcopy(payload), command_id))
        if operation == self.fail:
            raise OSError('service unavailable')
        if operation == 'attempt.claim':
            return copy.deepcopy(self.value)
        if operation == 'attempt.dispatch':
            self.value['state'] = 'dispatched'
        elif operation == 'attempt.report':
            self.value['state'] = payload['state']
            self.value['outcome'] = payload.get('outcome')
        elif operation == 'session.register':
            return {'id': 'source'}
        elif operation == 'events.append':
            self.events.extend(copy.deepcopy(payload['events']))
        return copy.deepcopy(self.value)

    def attempt(self, attempt_id):
        """Expose cancellation only after prompt intent exists, or simulate a fresh service incarnation."""
        if self.stale:
            return {**self.value, 'incarnation': 'service-restarted', 'state': 'unknown'}
        if self.cancel_after_prompt:
            self.value['cancelRequested'] = self.marker is not None and self.marker.exists()
        return copy.deepcopy(self.value)


def run_fixture(tmp_path, service, mode='success', **kwargs):
    """Run one fresh ACP process with a marker recording whether a prompt reached its stdin."""
    marker = tmp_path / 'prompt.json'
    service.marker = marker
    worker = AcpWorker(service, 'worker', 'epoch', poll_seconds=0.01)
    result = asyncio.run(worker.run_once('lane', PROFILE, [sys.executable, str(FIXTURE), mode, str(marker)],
                                        str(tmp_path), timeout=kwargs.get('timeout', 3)))
    return result, marker, worker


def test_real_split_ndjson_foreground_response_does_not_release_runtime(tmp_path):
    """A real v1 reply is archived, but it does not authorize the next queued task."""
    service = ServiceDouble()
    result, marker, worker = run_fixture(tmp_path, service)
    assert json.loads(marker.read_text())['params']['prompt'][0]['text'] == 'Do not execute as shell'
    assert result['foregroundOutcome'] == 'end_turn' and result['runtimeReleased'] is False
    assert service.value['state'] == 'finished'
    assert 'attempt.release' not in [operation for operation, _, _ in service.operations]
    assert worker.process.process.returncode is not None
    assert [event['sequence'] for event in service.events] == list(range(1, len(service.events) + 1))
    methods = [event['payload'].get('method') for event in service.events]
    assert methods[:2] == ['initialize', None]
    assert 'session/new' in methods and 'session/prompt' in methods and 'session/update' in methods


def test_cancel_request_reaches_process_without_claiming_its_children_stopped(tmp_path):
    """Cancel is a desired action; a cancellation response still leaves the runtime reserved."""
    service = ServiceDouble(cancel_after_prompt=True)
    result, _, _ = run_fixture(tmp_path, service, 'cancel')
    assert result['foregroundOutcome'] == 'cancelled' and result['runtimeReleased'] is False
    cancels = [e for e in service.events if e['payload'].get('method') == 'session/cancel']
    assert len(cancels) == 1
    assert service.value['state'] == 'finished'


@pytest.mark.parametrize('mode', ['eof', 'malformed', 'oversized', 'wait', 'v2', 'permission'])
def test_protocol_faults_are_sticky_unknown_and_never_resubmit_prompt(tmp_path, mode):
    """EOF, bad framing, timeout, version drift and unsupported interaction have no retry path."""
    service = ServiceDouble()
    with pytest.raises((AcpTransportError, WorkerStopped)):
        run_fixture(tmp_path, service, mode, timeout=0.35)
    assert service.value['state'] == 'unknown'
    prompts = [e for e in service.events if e['payload'].get('method') == 'session/prompt']
    assert len(prompts) <= 1
    assert 'attempt.release' not in [operation for operation, _, _ in service.operations]
    if mode == 'v2':
        assert not (tmp_path / 'prompt.json').exists()
    if mode == 'permission':
        replies = [e['payload'] for e in service.events if e['direction'] == 'client_to_agent' and e['payload'].get('id') == 'permission']
        assert replies == [{'jsonrpc': '2.0', 'id': 'permission', 'result': {'outcome': {'outcome': 'cancelled'}}}]


@pytest.mark.parametrize('failure', ['attempt.dispatch', 'attempt.heartbeat'])
def test_service_failure_before_launch_never_starts_process(tmp_path, failure):
    """Even a successful durable dispatch response requires fresh live ownership before spawn."""
    service = ServiceDouble(fail=failure)
    worker = AcpWorker(service, 'worker', 'epoch')
    with pytest.raises(OSError, match='service unavailable'):
        asyncio.run(worker.run_once('lane', PROFILE, [sys.executable, str(FIXTURE), 'success', str(tmp_path / 'prompt.json')], str(tmp_path)))
    assert worker.process is None
    assert not (tmp_path / 'prompt.json').exists()


def test_replayed_dispatch_outcome_after_restart_is_not_send_authority(tmp_path):
    """A stale service incarnation blocks process start even when the command journal remembers dispatch."""
    service = ServiceDouble(stale=True)
    worker = AcpWorker(service, 'worker', 'epoch')
    with pytest.raises(WorkerStopped):
        asyncio.run(worker.run_once('lane', PROFILE, [sys.executable, str(FIXTURE), 'success', str(tmp_path / 'prompt.json')], str(tmp_path)))
    assert worker.process is None


def test_lost_dispatch_ack_does_not_start_or_retry_process(tmp_path):
    """A committed dispatch with an unread ACK reserves the lane; it never becomes an automatic retry."""
    service = ServiceDouble()
    original = service.execute
    def lose_ack(operation, payload, command_id):
        """Commit the effect first, then emulate a broken response channel."""
        result = original(operation, payload, command_id)
        if operation == 'attempt.dispatch':
            raise OSError('dispatch ACK lost after commit')
        return result
    service.execute = lose_ack
    worker = AcpWorker(service, 'worker', 'epoch')
    with pytest.raises(OSError, match='ACK lost'):
        asyncio.run(worker.run_once('lane', PROFILE, [sys.executable, str(FIXTURE), 'success', str(tmp_path / 'prompt.json')], str(tmp_path)))
    assert worker.process is None and not (tmp_path / 'prompt.json').exists()
    assert service.value['state'] == 'unknown'
    assert [op for op, _, _ in service.operations].count('attempt.dispatch') == 1


def test_service_loss_after_prompt_intent_prevents_the_actual_send(tmp_path):
    """A recorded outgoing intent is not permission to send if the final ownership read fails."""
    service = ServiceDouble()
    original = service.attempt
    def unavailable_after_intent(attempt_id):
        """Stop authorization after archival but before the subprocess receives prompt text."""
        if any(event['payload'].get('method') == 'session/prompt' for event in service.events):
            raise OSError('service lost after intent')
        return original(attempt_id)
    service.attempt = unavailable_after_intent
    with pytest.raises(OSError, match='after intent'):
        run_fixture(tmp_path, service)
    assert not (tmp_path / 'prompt.json').exists()
    assert service.value['state'] == 'unknown'
    prompts = [event for event in service.events if event['payload'].get('method') == 'session/prompt']
    assert len(prompts) == 1


def test_event_storage_failure_before_prompt_keeps_text_out_of_agent(tmp_path):
    """The adapter fails closed when handshake evidence cannot be durably recorded."""
    service = ServiceDouble(fail='events.append')
    with pytest.raises(OSError, match='service unavailable'):
        run_fixture(tmp_path, service)
    assert not (tmp_path / 'prompt.json').exists()
    assert service.value['state'] == 'unknown'


def test_profile_mismatch_does_not_start_configured_binary(tmp_path):
    """A queued profile update cannot silently replace the operator's pinned execution profile."""
    service = ServiceDouble()
    service.value['profile'] = {**PROFILE, 'version': '2.0'}
    with pytest.raises(WorkerStopped, match='operator pin'):
        run_fixture(tmp_path, service)
    assert not (tmp_path / 'prompt.json').exists()
    assert 'attempt.dispatch' not in [operation for operation, _, _ in service.operations]


def test_multibyte_prompt_budget_is_rejected_before_dispatch_or_spawn(tmp_path):
    """Character-valid generic queue input may exceed ACP bytes; reject it before any external action."""
    service = ServiceDouble()
    service.value['item']['text'] = '\U0001f642' * 8192
    worker = AcpWorker(service, 'worker', 'epoch')
    with pytest.raises(DomainError) as failure:
        asyncio.run(worker.run_once('lane', PROFILE, [sys.executable, str(FIXTURE), 'success', str(tmp_path / 'prompt.json')], str(tmp_path)))
    assert failure.value.code == 'payload_too_large'
    assert worker.process is None and service.value['state'] == 'unknown'
    assert 'attempt.dispatch' not in [operation for operation, _, _ in service.operations]


def test_http_adapter_uses_fixed_common_commands_and_current_attempt_route():
    """HTTP sends the same explicit identities as MCP and cannot redirect commands to a remote host."""
    seen = []
    def handle(request):
        """Capture the actual request envelope rather than calling private domain methods."""
        seen.append(request)
        return httpx.Response(200, json={'id': 'attempt'})
    transport = httpx.Client(transport=httpx.MockTransport(handle))
    client = HttpWorkerClient('http://127.0.0.1:8895', 'worker', client=transport)
    client.execute('attempt.heartbeat', {'attemptId': 'attempt'}, 'key')
    client.attempt('opaque:id')
    assert json.loads(seen[0].content) == {'clientId': 'worker', 'commandId': 'key', 'operation': 'attempt.heartbeat', 'payload': {'attemptId': 'attempt'}}
    assert seen[1].url.path == '/api/v1/attempts/opaque:id'
    client.close()
    for origin in ('https://127.0.0.1', 'http://example.com', 'http://user@127.0.0.1', 'http://127.0.0.1/path'):
        with pytest.raises(ValueError):
            HttpWorkerClient(origin, 'worker')


def test_real_service_http_worker_archives_session_and_requires_operator_release(tmp_path):
    """Exercise HTTP + common journal + durable queue + real process, without the installed store."""
    origin = 'http://127.0.0.1:8895'
    with TestClient(create_app(tmp_path / 'store'), base_url=origin) as http:
        def handle(request):
            """Pass actual HTTP envelopes into guarded application routes, not private callbacks."""
            reply = http.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)
        client = HttpWorkerClient(origin, 'worker-integration', client=httpx.Client(transport=httpx.MockTransport(handle)))
        client.execute('queue.create', {'laneId': 'lane', 'title': 'Fixture', 'profile': PROFILE}, 'create')
        issue = client.execute('issue.create', {'title': 'The issue workflow stays independent'}, 'issue')
        old = client.execute('session.register', {'title': 'Evidence only', 'agent': 'fixture', 'protocol': 'acp',
            'protocolVersion': 1, 'remoteSessionId': 'old-session', 'connectionId': 'old-connection'}, 'old')
        first = client.execute('queue.enqueue', {'laneId': 'lane', 'text': 'first', 'issueId': issue['id'], 'sessionId': old['id']}, 'first')
        second = client.execute('queue.enqueue', {'laneId': 'lane', 'text': 'second'}, 'second')
        marker = tmp_path / 'prompt.json'
        worker = AcpWorker(client, 'worker', 'epoch', poll_seconds=0.01)
        result = asyncio.run(worker.run_once('lane', PROFILE, [sys.executable, str(FIXTURE), 'success', str(marker)], str(tmp_path), timeout=3))
        assert result['foregroundOutcome'] == 'end_turn' and result['runtimeReleased'] is False
        source_id = result['sessionId']
        assert source_id != old['id']
        source = http.get('/api/v1/sessions/' + source_id).json()
        assert source['remoteSessionId'] == 'fixture-session' and source['issueId'] == issue['id']
        events = http.get('/api/v1/sessions/' + source_id + '/events?limit=50').json()['items']
        assert [event['sequence'] for event in events] == list(range(1, len(events) + 1))
        answer = next(event for event in events if event['payload'].get('method') == 'session/update')
        assert answer['payload']['_meta']['token'] == '[redacted]'
        assert answer['redactions'] == ['/_meta/token']
        assert http.get('/api/v1/issues/' + issue['id']).json()['status'] == 'backlog'
        lane = http.get('/api/v1/queues/lane').json()
        assert lane['state'] == 'busy' and lane['activeAttempt']['state'] == 'finished'
        assert http.get('/api/v1/queue-items/' + second['id']).json()['state'] == 'queued'
        with pytest.raises(httpx.HTTPStatusError) as blocked:
            client.execute('attempt.claim', {'laneId': 'lane', 'workerId': 'next', 'workerEpoch': 'new'}, 'next')
        assert blocked.value.response.status_code == 409
        attempt = client.attempt(result['attempt']['id'])
        payload = {'attemptId': attempt['id'], 'expectedRevision': attempt['revision'],
                   'resolution': 'owned_runtime_stopped', 'evidence': 'Fixture has no child launch path; direct process awaited by this test.'}
        with pytest.raises(httpx.HTTPStatusError) as denied:
            client.execute('attempt.reconcile', payload, 'deny-human')
        assert denied.value.response.status_code == 403
        ui_token = http.get('/api/v1/ui-session').json()['token']
        released = http.post('/api/v1/commands', headers={'X-Ag-Tasks-UI': ui_token}, json={
            'clientId': 'operator', 'commandId': 'release', 'operation': 'attempt.reconcile', 'payload': payload})
        assert released.status_code == 200, released.text
        assert released.json()['releaseProof']['source'] == 'operator'
        assert http.get('/api/v1/queues/lane').json()['state'] == 'ready'
        assert http.get('/api/v1/queue-items/' + first['id']).json()['state'] == 'completed'
        client.close()
