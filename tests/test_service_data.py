"""Contract checks for the universal service core, independent of ACP/MCP/UI adapters."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from uuid import uuid4

import pytest

from ag_tasks.issues import DomainError, IssueStore


@pytest.fixture
def store(tmp_path):
    """Own an isolated service database; never use the installed user's data."""
    value = IssueStore(tmp_path)
    yield value
    value.close()


def command(store, operation, payload, key=None, client='agent-1'):
    """Submit a caller-owned key through the same domain entry used by every adapter."""
    return store.service.execute({'clientId': client, 'commandId': key or uuid4().hex,
                                  'operation': operation, 'payload': payload})


def register(store, protocol='acp', version=1):
    """Create a passive source binding with explicit connection and remote identity."""
    return command(store, 'session.register', {'title': 'Observed agent', 'agent': 'fixture', 'protocol': protocol,
        'protocolVersion': version, 'remoteSessionId': 'remote-1', 'connectionId': 'connection-1'})


def event(sequence, message, origin='live', direction='agent_to_client'):
    """Describe an archived source observation; no fixture invokes an actual agent."""
    return {'sequence': sequence, 'connectionId': 'connection-1', 'direction': direction,
            'origin': origin, 'payload': message}


def append(store, session_id, values):
    """Deliver events without a whole-batch command cache to exercise per-event identity."""
    return store.service.sessions.append({'sessionId': session_id, 'producerId': 'producer-1',
                                          'producerEpoch': 'epoch-1', 'events': values})


def update(value):
    """Wrap a native ACP update in its original wire envelope."""
    return {'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'remote-1', 'update': value}}


def test_issue_commands_are_atomic_stable_and_transport_independent(store):
    """One key creates one issue even when concurrent clients lose the original ACK."""
    payload = {'title': 'Same intended task'}
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda _: command(store, 'issue.create', payload, 'same-key'), range(12)))
    assert len({reply['id'] for reply in replies}) == 1
    assert len(store.list()) == 1
    assert len(store.service.commands.events()['items']) == 1
    assert store.service.commands.outcome('agent-1', 'same-key')['response'] == replies[0]
    with pytest.raises(DomainError, match='different data') as error:
        command(store, 'issue.create', {'title': 'Changed intent'}, 'same-key')
    assert error.value.code == 'idempotency_conflict'
    assert len(store.list()) == 1


def test_command_key_cannot_change_operation_and_clients_have_separate_namespaces(store):
    """Intent identity differs from payload hashes and does not globally merge identical tasks."""
    first = command(store, 'issue.create', {'title': 'Same'}, 'key', client='one')
    second = command(store, 'issue.create', {'title': 'Same'}, 'key', client='two')
    assert first['id'] != second['id']
    with pytest.raises(DomainError) as failure:
        command(store, 'session.register', {}, 'key', client='one')
    assert failure.value.code == 'idempotency_conflict'


def test_rejected_partial_effect_rolls_back_but_rejection_is_durable(store):
    """A domain failure rolls back nested mutations and stores exactly that rejected outcome."""
    calls = []
    def fail(operation, payload):
        """Simulate a validated domain operation discovering a failure after its first mutation."""
        calls.append(operation)
        store.create({'title': 'Must not survive'})
        raise DomainError('rejected', 'Stable rejection', 409)
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            store.service.commands.execute('client', 'reject-key', 'fixture', {}, fail)
        assert error.value.code == 'rejected'
    assert calls == ['fixture']
    assert not store.list()
    assert store.service.commands.events()['items'][0]['type'] == 'command.rejected'
    assert store.service.commands.outcome('client', 'reject-key')['status'] == 409


def test_storage_failure_cannot_commit_effect_without_outcome(store):
    """A real SQLite abort at command insertion rolls back the task created in the same transaction."""
    store.db.connection.execute("CREATE TEMP TRIGGER reject_outcome BEFORE INSERT ON service_commands BEGIN SELECT RAISE(ABORT,'storage fault'); END")
    with pytest.raises(sqlite3.Error):
        command(store, 'issue.create', {'title': 'No orphan effect'}, 'fault')
    assert not store.list()
    store.db.connection.execute('DROP TRIGGER reject_outcome')
    assert command(store, 'issue.create', {'title': 'No orphan effect'}, 'fault')['title'] == 'No orphan effect'


def test_restart_preserves_keys_events_and_additive_schema(tmp_path):
    """Reopening the same store preserves committed identities; repeated migration changes no event cursor."""
    first = IssueStore(tmp_path)
    source = register(first)
    append(first, source['id'], [event(1, {'jsonrpc': '2.0', 'method': '_future/extension', '_meta': {'future': {'kept': True}}})])
    created = command(first, 'issue.create', {'title': 'Durable'}, 'durable')
    first.close()
    second = IssueStore(tmp_path)
    try:
        assert command(second, 'issue.create', {'title': 'Durable'}, 'durable') == created
        events = second.service.sessions.events(source['id'])['items']
        assert len(events) == 1 and events[0]['payload']['_meta']['future']['kept'] is True
        assert append(second, source['id'], [event(1, events[0]['payload'])])['inserted'] == 0
    finally:
        second.close()


@pytest.mark.parametrize('protocol', ['acp', 'mcp', 'ui', 'internal'])
def test_generic_sessions_share_event_storage_without_task_creation(store, protocol):
    """Source protocols are observations, not alternative issue workflows or scheduling authorities."""
    source = register(store, protocol)
    message = {'jsonrpc': '2.0', 'method': 'future', 'params': {'todo': 'do not create issue'}}
    append(store, source['id'], [event(1, message)])
    assert store.service.sessions.events(source['id'])['items'][0]['payload'] == message
    assert source['mode'] == 'observed' and not store.list()
    assert store.service.capabilities()['managedExecution'] is False


def test_identical_chunks_are_distinct_and_event_retry_is_not(store):
    """Sequence identity deduplicates delivery, never two legitimate equal text chunks."""
    source = register(store)
    chunk = update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'same'}})
    values = [event(1, chunk), event(2, chunk)]
    assert append(store, source['id'], values)['inserted'] == 2
    assert append(store, source['id'], values)['acknowledgedThrough'] == 2
    assert store.service.sessions.get(source['id'])['eventCount'] == 2
    with pytest.raises(DomainError) as error:
        append(store, source['id'], [event(2, update({'sessionUpdate': 'changed'}))])
    assert error.value.code == 'idempotency_conflict'


def test_batch_gap_rolls_back_earlier_events_and_projection(store):
    """No ACK or partial event application survives a gap in the middle of a batch."""
    source = register(store, version=2)
    running = update({'sessionUpdate': 'state_update', 'state': 'running'})
    with pytest.raises(DomainError) as error:
        append(store, source['id'], [event(1, running), event(3, running)])
    assert error.value.code == 'sequence_gap'
    actual = store.service.sessions.get(source['id'])
    assert actual['eventCount'] == 0 and actual['projection']['foregroundState'] == 'unknown'
    assert append(store, source['id'], [event(1, running)])['acknowledgedThrough'] == 1


def test_v2_acceptance_and_foreground_end_never_release_workspace(store):
    """A zero JSON-RPC id is valid; acceptance, foreground outcome and background proof stay distinct."""
    source = register(store, version=2)
    values = [event(1, {'jsonrpc': '2.0', 'id': 0, 'method': 'session/prompt', 'params': {'sessionId': 'remote-1', 'prompt': []}}, direction='client_to_agent'),
              event(2, {'jsonrpc': '2.0', 'id': 0, 'result': {'messageId': 'message-1'}}),
              event(3, update({'sessionUpdate': 'state_update', 'state': 'idle', 'stopReason': 'end_turn'}))]
    append(store, source['id'], values)
    projection = store.service.sessions.get(source['id'])['projection']
    assert projection['submissionState'] == 'accepted'
    assert projection['foregroundState'] == 'idle'
    assert projection['ownedActivityState'] == 'unknown'
    assert not store.list()


def test_replay_and_foreign_session_cannot_rewrite_live_lifecycle(store):
    """Replay is archival; an unbound child does not masquerade as the root's idle state."""
    source = register(store, version=2)
    foreign = update({'sessionUpdate': 'state_update', 'state': 'idle'})
    foreign['params']['sessionId'] = 'foreign-child'
    append(store, source['id'], [event(1, update({'sessionUpdate': 'state_update', 'state': 'running'})),
        event(2, update({'sessionUpdate': 'state_update', 'state': 'idle'}), 'replay'), event(3, foreign)])
    assert store.service.sessions.get(source['id'])['projection']['foregroundState'] == 'running'


def test_rebuild_preserves_entity_types_and_cannot_regress_live_with_history(store):
    """Same ID across entity types is not a collision; replay cannot turn a completed live tool pending."""
    source = register(store, version=2)
    append(store, source['id'], [event(1, update({'sessionUpdate': 'tool_call', 'toolCallId': 'same', 'status': 'completed'})),
        event(2, update({'sessionUpdate': 'agent_message', 'messageId': 'same', 'content': []})),
        event(3, update({'sessionUpdate': 'tool_call_update', 'toolCallId': 'same', 'status': 'pending'}), 'replay')])
    before = store.service.sessions.get(source['id'])
    after = command(store, 'session.rebuild', {'sessionId': source['id']})
    assert before == after
    assert len(after['entities']) == 2
    tool = next(item for item in after['entities'] if item['kind'] == 'tool')
    assert tool['payload']['status'] == 'completed' and not store.list()


def test_known_credentials_redacted_but_unknown_metadata_preserved(store):
    """Archive policy reports omissions explicitly; it does not claim to detect arbitrary secrets in prose."""
    source = register(store)
    append(store, source['id'], [event(1, {'jsonrpc': '2.0', 'method': '_future',
        '_meta': {'api_key': 'PRIVATE', 'env': {'PRIVATE': 'VALUE'}, 'future': {'kept': 42}}})])
    actual = store.service.sessions.events(source['id'])['items'][0]
    assert actual['payload']['_meta']['future'] == {'kept': 42}
    assert actual['redactions'] == ['/_meta/api_key', '/_meta/env']
    assert 'PRIVATE' not in json.dumps(actual)


def test_linking_observations_uses_revision_without_changing_issue_workflow(store):
    """A human/agent may associate evidence with a task but cannot acquire runtime ownership through it."""
    issue = store.create({'title': 'Tracked task'})
    source = register(store)
    linked = command(store, 'session.link', {'sessionId': source['id'], 'issueId': issue['key'], 'expectedRevision': 1}, 'link')
    assert linked['issueId'] == issue['id'] and linked['revision'] == 2
    assert command(store, 'session.link', {'sessionId': source['id'], 'issueId': issue['key'], 'expectedRevision': 1}, 'link') == linked
    with pytest.raises(DomainError) as error:
        command(store, 'session.link', {'sessionId': source['id'], 'issueId': None, 'expectedRevision': 1})
    assert error.value.code == 'revision_conflict'
    assert store.get(issue['id'])['status'] == 'backlog'


@pytest.mark.parametrize('bad', [True, 0, -1, 1.5])
def test_invalid_sequence_is_not_a_delivery_identity(store, bad):
    """Numeric coercion must not merge booleans, fractional counters or invalid journal positions."""
    source = register(store)
    with pytest.raises(DomainError):
        append(store, source['id'], [event(bad, {'jsonrpc': '2.0', 'method': 'future'})])
    assert store.service.sessions.get(source['id'])['eventCount'] == 0


def test_unknown_version_is_archived_without_assuming_v2_semantics(store):
    """Forward-compatible storage does not grant a future protocol a guessed lifecycle codec."""
    source = register(store, version=99)
    append(store, source['id'], [event(1, update({'sessionUpdate': 'state_update', 'state': 'idle'}))])
    assert store.service.sessions.get(source['id'])['projection']['foregroundState'] == 'unknown'


def test_events_and_sessions_are_cursor_paged_without_loss(store, monkeypatch):
    """Stable cursors page facts without merging identical messages or skipping other-session cursor gaps."""
    stamps = iter(f'2026-11-13T13:00:0{second}+00:00' for second in range(5))
    monkeypatch.setattr('ag_tasks.service_events.timestamp', lambda: next(stamps))
    first, second = register(store), register(store)
    message = {'jsonrpc': '2.0', 'method': 'future'}
    append(store, first['id'], [event(1, message)])
    append(store, second['id'], [event(1, message)])
    append(store, first['id'], [event(2, message)])
    one = store.service.sessions.events(first['id'], limit=1)
    two = store.service.sessions.events(first['id'], limit=1, after=one['nextCursor'])
    assert [one['items'][0]['sequence'], two['items'][0]['sequence']] == [1, 2]
    page = store.service.sessions.list(limit=1)
    assert page['items'][0]['id'] == first['id']  # The last event refreshed this session's activity.
    assert store.service.sessions.list(limit=1, after=page['nextCursor'])['items'][0]['id'] == second['id']


def test_future_feature_schema_is_refused_before_startup_mutation(tmp_path):
    """A newer feature store cannot be silently rewritten by older reconciliation logic."""
    first = IssueStore(tmp_path)
    first.create({'title':'Existing task'})
    first.db.connection.execute('UPDATE service_data_version SET version=2')
    before = first.db.connection.execute('SELECT COUNT(*) FROM history').fetchone()[0]
    first.close()
    with pytest.raises(ValueError, match='future service data'):
        IssueStore(tmp_path)
    with sqlite3.connect(tmp_path/'issues.sqlite3') as connection:
        assert connection.execute('SELECT COUNT(*) FROM history').fetchone()[0] == before


def test_generic_issue_mutations_deduplicate_comments_links_and_updates(store):
    """Ordinary task behaviour uses the same command identity as agent observations."""
    first = store.create({'title':'One'})
    second = store.create({'title':'Two'})
    updates = [('issue.update',{'issueId':first['id'],'changes':{'title':'Updated'}}),
               ('issue.comment',{'issueId':first['id'],'text':'Exactly one comment'}),
               ('issue.link',{'issueId':first['id'],'relatedId':second['id'],'kind':'related'})]
    for index, (operation, payload) in enumerate(updates):
        original = command(store, operation, payload, f'key-{index}')
        assert command(store, operation, payload, f'key-{index}') == original
    actual = store.get(first['id'])
    assert actual['title'] == 'Updated' and len(actual['comments']) == 1 and len(actual['links']) == 1
    assert len(store.service.commands.events()['items']) == 3


def test_partial_feature_migration_rolls_back_all_new_tables(monkeypatch):
    """SQLite DDL and feature metadata form one migration transaction, not partially committed scripts."""
    from ag_tasks import service_schema
    with sqlite3.connect(':memory:') as connection:
        monkeypatch.setattr(service_schema, 'SCHEMA', service_schema.SCHEMA + ['INVALID SQL'])
        with pytest.raises(sqlite3.Error):
            service_schema.migrate(connection)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'service_%'").fetchall() == []


@pytest.mark.parametrize('version', [1, 2])
def test_old_prompt_outcome_cannot_become_the_next_submission_result(store, version):
    """A new submission clears prior outcome evidence; v1 RPC error does not prove no execution occurred."""
    source = register(store, version=version)
    prompt = {'jsonrpc':'2.0','id':1,'method':'session/prompt','params':{'sessionId':'remote-1','prompt':[]}}
    append(store, source['id'], [event(1,prompt,direction='client_to_agent'),
        event(2,{'jsonrpc':'2.0','id':1,'error':{'code':-32603,'message':'failed'}})])
    before = store.service.sessions.get(source['id'])['projection']
    assert before['outcome'] == 'rpc_error'
    assert before['submissionState'] == ('answered' if version == 1 else 'rejected')
    prompt['id'] = 2
    append(store, source['id'], [event(3,prompt,direction='client_to_agent')])
    after = store.service.sessions.get(source['id'])['projection']
    assert after['submissionState'] == 'submitted' and after['outcome'] is None


def test_raw_event_identity_rejects_changed_secret_without_archiving_it(store):
    """Redaction changes storage, never the source event's immutable delivery identity."""
    source = register(store)
    message = {'jsonrpc':'2.0','method':'_future','params':{'token':'secret-one','kept':1}}
    value = event(1, message)
    assert append(store, source['id'], [value])['inserted'] == 1
    assert append(store, source['id'], [value])['inserted'] == 0
    message['params']['token'] = 'secret-two'
    with pytest.raises(DomainError) as error:
        append(store, source['id'], [event(2, {'jsonrpc':'2.0','method':'_next'}), value])
    assert error.value.code == 'idempotency_conflict'
    archived = store.service.sessions.events(source['id'])['items']
    assert len(archived) == 1 and 'secret-' not in json.dumps(archived)
    assert append(store, source['id'], [event(2, {'jsonrpc':'2.0','method':'_next'})])['acknowledgedThrough'] == 2


@pytest.mark.parametrize('redacted', [False, True])
def test_legacy_event_retry_requires_verifiable_original_identity(store, redacted):
    """Old plain events remain retryable; an old redacted digest cannot prove the source identity."""
    from ag_tasks.service_values import fingerprint
    source = register(store)
    message = {'jsonrpc':'2.0','method':'_future','params':{'kept':1}}
    if redacted:
        message['params']['token'] = 'unrecoverable-original'
    value = event(1, message)
    append(store, source['id'], [value])
    archived = store.service.sessions.events(source['id'])['items'][0]['payload']
    legacy_digest = fingerprint({**value,'payload':archived})
    store.db.connection.execute('UPDATE service_events SET fingerprint=?', (legacy_digest,))
    if redacted:
        with pytest.raises(DomainError) as error:
            append(store, source['id'], [value])
        assert error.value.code == 'legacy_event_identity_unverifiable'
    else:
        assert append(store, source['id'], [value])['inserted'] == 0
        message['params']['kept'] = 2
        with pytest.raises(DomainError) as error:
            append(store, source['id'], [value])
        assert error.value.code == 'idempotency_conflict'
    assert store.service.sessions.get(source['id'])['eventCount'] == 1
