"""Explicit one-attempt ACP v1 worker; upstream agents remain independent executables.

There is no automatic scheduler or retry. Successful foreground completion keeps the
lane reserved until an operator proves the runtime (including children) has stopped.
"""

import argparse
import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4

from .acp_worker_transport import AcpProcess, AcpTransportError, MAX_FRAME_BYTES
from .agent_worker_client import HttpWorkerClient
from .service_values import encode


class WorkerStopped(RuntimeError):
    """Current ownership or the service cannot safely authorize another external action."""


class AcpWorker:
    """Drive one fresh subprocess/session through the transport-independent attempt API."""

    def __init__(self, client, worker_id, worker_epoch, *, poll_seconds=0.2):
        """Use execute(operation,payload,command_id) and attempt(id) callbacks; never infer transport authority."""
        self.client, self.worker_id, self.worker_epoch = client, worker_id, worker_epoch
        self.poll_seconds = poll_seconds
        self.identity = None
        self.claim = None
        self.process = None
        self.source_id = None
        self.remote_id = None
        self.connection_id = uuid4().hex
        self.sequence = 0
        self.handshake = []
        self.cancel_sent = False
        self.last_heartbeat = 0
        self.deadline = 0

    def command(self, operation, payload):
        """Create one new intent identity; ambiguous commands are never automatically resubmitted."""
        return self.client.execute(operation, payload, uuid4().hex)

    def current(self):
        """Validate exact live ownership before side effects, including after a durable command ACK."""
        if time.monotonic() >= self.deadline:
            raise WorkerStopped('ACP attempt deadline exceeded.')
        if time.monotonic() - self.last_heartbeat >= 10:
            self.command('attempt.heartbeat', self.identity)
            self.last_heartbeat = time.monotonic()
        current = self.client.attempt(self.identity['attemptId'])
        for key, expected in self.identity.items():
            actual = current.get('id') if key == 'attemptId' else current.get(key)
            if actual != expected:
                raise WorkerStopped('Attempt ownership changed.')
        if current.get('incarnation') != self.claim.get('incarnation') or current.get('profile') != self.claim.get('profile'):
            raise WorkerStopped('Attempt binding changed.')
        if current.get('state') not in ('dispatched', 'running'):
            raise WorkerStopped('Attempt is no longer authorized for ACP traffic.')
        return current

    def archive(self, direction, message):
        """Persist one bounded observation before another protocol action; failure stops this worker."""
        event = {'sequence': self.sequence + 1, 'connectionId': self.connection_id,
                 'direction': direction, 'origin': 'live', 'payload': message}
        encode(event, 16384)
        if self.source_id is None:
            self.handshake.append(event)
            # Handshake may be buffered only before any prompt. A failed handshake never runs queued text.
            if len(self.handshake) > 100:
                raise WorkerStopped('ACP handshake exceeded its event budget.')
            encode(self.handshake, 32768)
        else:
            self.command('events.append', {'sessionId': self.source_id, 'producerId': self.worker_id,
                         'producerEpoch': self.worker_epoch, 'events': [event]})
        self.sequence += 1

    def authorize_send(self, message):
        """Require live ownership and respect cancellation of an as-yet unsent prompt."""
        current = self.current()
        if message.get('method') == 'session/prompt' and current.get('cancelRequested'):
            raise WorkerStopped('Cancelled before prompt submission.')

    async def send(self, message):
        """Record outgoing intent before writing it; archival intent is not proof of delivery."""
        self.authorize_send(message)
        self.archive('client_to_agent', message)
        # Archival may involve slow I/O. Revalidate afterwards instead of treating its ACK as a lease.
        self.authorize_send(message)
        await self.process.send(message)

    async def cancel_if_requested(self, current):
        """Send one best-effort cancellation notification, without releasing runtime ownership."""
        if not current.get('cancelRequested') or self.cancel_sent:
            return
        if self.remote_id is None:
            raise WorkerStopped('Cancelled before the ACP session was ready.')
        self.cancel_sent = True
        await self.send({'jsonrpc': '2.0', 'method': 'session/cancel',
                         'params': {'sessionId': self.remote_id}})

    async def rpc(self, method, params, rpc_id):
        """Wait for one matching v1 response while checking cancellation and current ownership."""
        await self.send({'jsonrpc': '2.0', 'id': rpc_id, 'method': method, 'params': params})
        while True:
            current = self.current()
            await self.cancel_if_requested(current)
            message = await self.process.poll(self.poll_seconds)
            if message is None:
                continue
            self.archive('agent_to_client', message)
            if 'method' in message:
                if type(message['method']) is not str or type(message.get('params', {})) is not dict:
                    raise AcpTransportError('Invalid ACP request or notification.')
                if 'id' in message:
                    await self.reject_interaction(message)
                continue
            if message.get('id') != rpc_id or ('result' in message) == ('error' in message):
                raise AcpTransportError('Unexpected or ambiguous ACP response.')
            if 'error' in message:
                raise AcpTransportError('ACP returned an RPC error.')
            if type(message['result']) is not dict:
                raise AcpTransportError('ACP result must be an object.')
            return message['result']

    async def reject_interaction(self, message):
        """Explicitly deny unsupported permission/interaction requests; never fabricate user approval."""
        if message['method'] == 'session/request_permission':
            response = {'jsonrpc': '2.0', 'id': message['id'],
                        'result': {'outcome': {'outcome': 'cancelled'}}}
        else:
            response = {'jsonrpc': '2.0', 'id': message['id'],
                        'error': {'code': -32601, 'message': 'This worker does not support interactive requests.'}}
        await self.send(response)
        raise WorkerStopped('ACP requires an interaction broker unsupported by this worker.')

    def bind_source(self, capabilities):
        """Create a fresh observation binding; a queued session reference grants no runtime authority."""
        item = self.claim['item']
        payload = {'title': 'ACP attempt ' + self.claim['id'], 'agent': self.claim['profile']['id'],
                   'protocol': 'acp', 'protocolVersion': 1, 'remoteSessionId': self.remote_id,
                   'connectionId': self.connection_id, 'capabilities': capabilities}
        if item.get('issueId') is not None:
            payload['issueId'] = item['issueId']
        source = self.command('session.register', payload)
        self.source_id = source['id']
        if self.handshake:
            self.command('events.append', {'sessionId': self.source_id, 'producerId': self.worker_id,
                         'producerEpoch': self.worker_epoch, 'events': self.handshake})
            self.handshake.clear()

    def preflight_prompt(self):
        """Reject known oversized input before dispatch or spawn, reserving space for the future session ID."""
        prompt = {'jsonrpc': '2.0', 'id': 'prompt', 'method': 'session/prompt', 'params': {
                  'sessionId': '', 'prompt': [{'type': 'text', 'text': self.claim['item']['text']}]}}
        # Remote IDs are bounded to 512 valid Unicode characters; reserve their worst-case UTF-8 size.
        encode(prompt, MAX_FRAME_BYTES - 512 * 4)

    async def run_once(self, lane_id, profile, argv, cwd, *, timeout=300):
        """Claim and dispatch once, preserving unknown delivery on every interruption or service failure.

        The profile is an explicit operator pin, compared to the immutable claim. Only
        ACP v1 is supported. This object is single-use and cannot resume a prior attempt.
        """
        if self.identity is not None:
            raise ValueError('AcpWorker instances are single-use.')
        if type(profile) is not dict or profile.get('protocol') != 'acp' or profile.get('protocolVersion') != 1:
            raise ValueError('This worker supports an explicit ACP v1 profile only.')
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError('ACP timeout must be between zero and 3600 seconds.')
        self.deadline = time.monotonic() + timeout
        self.claim = self.command('attempt.claim', {'laneId': lane_id,
                                  'workerId': self.worker_id, 'workerEpoch': self.worker_epoch})
        self.identity = {'attemptId': self.claim['id'], 'workerId': self.worker_id,
                         'workerEpoch': self.worker_epoch, 'fence': self.claim['fence']}
        try:
            if self.claim['profile'] != profile:
                raise WorkerStopped('Claimed profile differs from the operator pin.')
            self.preflight_prompt()
            # A cached command outcome is insufficient: current() must confirm the live binding afterwards.
            self.command('attempt.dispatch', self.identity)
            current = self.current()
            if current.get('cancelRequested'):
                raise WorkerStopped('Cancelled before process launch.')
            self.process = await AcpProcess.start(argv, cwd)
            initialized = await self.rpc('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                         'clientInfo': {'name': 'ag-tasks-worker', 'version': '1'}}, 'initialize')
            if initialized.get('protocolVersion') != 1:
                raise WorkerStopped('Agent negotiated an unsupported ACP protocol version.')
            created = await self.rpc('session/new', {'cwd': str(Path(cwd)), 'mcpServers': []}, 'new')
            self.remote_id = created.get('sessionId')
            if type(self.remote_id) is not str or not self.remote_id or len(self.remote_id) > 512:
                raise AcpTransportError('ACP session/new did not return a bounded session ID.')
            self.bind_source(initialized.get('agentCapabilities', {}))
            self.command('attempt.report', {**self.identity, 'state': 'running'})
            current = self.current()
            if current.get('cancelRequested'):
                raise WorkerStopped('Cancelled before prompt submission.')
            result = await self.rpc('session/prompt', {'sessionId': self.remote_id,
                                    'prompt': [{'type': 'text', 'text': self.claim['item']['text']}]}, 'prompt')
            stop_reason = result.get('stopReason')
            if stop_reason not in ('end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'):
                raise AcpTransportError('ACP v1 response did not contain a supported stop reason.')
            attempt = self.command('attempt.report', {**self.identity, 'state': 'finished', 'outcome': stop_reason})
            return {'attempt': attempt, 'sessionId': self.source_id,
                    'foregroundOutcome': stop_reason, 'runtimeReleased': False}
        except BaseException:
            # An unavailable service may reject this report; retained dispatch intent still blocks replay.
            try:
                self.command('attempt.report', {**self.identity, 'state': 'unknown', 'outcome': 'worker_interrupted'})
            except Exception:
                pass
            raise
        finally:
            if self.process is not None:
                await self.process.close()


def main():
    """Run one explicit local worker; JSON profile and process argv are operator configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--profile', required=True, help='Path to an exact JSON profile pin.')
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    profile = json.loads(Path(args.profile).read_text())
    worker_id, epoch = 'acp-worker', uuid4().hex
    client = HttpWorkerClient(args.origin, 'acp-worker-' + epoch)
    try:
        result = asyncio.run(AcpWorker(client, worker_id, epoch).run_once(
            args.lane, profile, argv, args.cwd, timeout=args.timeout))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        client.close()


if __name__ == '__main__':
    main()
