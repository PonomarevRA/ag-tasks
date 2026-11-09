"""Durable chat identities and task context survive host changes without execution."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from uuid import uuid4

import pytest

from ag_tasks.issues import DomainError, IssueStore
from ag_tasks.task_context import serialized


@pytest.fixture
def store(tmp_path):
    """Use a private owner with no installed user data or model processes."""
    owner = IssueStore(tmp_path)
    yield owner
    owner.close()


def command(store, operation, payload, key=None):
    """Exercise the same atomic journal used by all real adapters."""
    return store.service.execute({'clientId': 'companion-test', 'commandId': key or uuid4().hex,
                                  'operation': operation, 'payload': payload})


def bind(store, issue_id, **changes):
    """Create a caller-declared chat; tests never infer actual IDE conversation IDs."""
    payload = {'harness': 'codex', 'instanceId': 'profile-a', 'remoteSessionId': 'chat-a',
               'connectionId': 'connection-a', 'issueId': issue_id, 'title': 'Work', 'expectedRevision': 0}
    payload.update(changes)
    return command(store, 'conversation.bind', payload)


def test_many_chats_continue_same_task_without_replacing_each_other(store):
    """Codex, Cursor and unknown harness chats share a task while keeping independent identities."""
    issue = store.create({'title': 'Shared task'})
    first = bind(store, issue['id'])
    second = bind(store, issue['key'], harness='cursor', remoteSessionId='chat-b', continuedFrom=first['id'])
    third = bind(store, issue['id'], harness='custom-agent', remoteSessionId='chat-c', continuedFrom=second['id'])
    listed = store.service.conversations.list(issue_id=issue['key'])
    assert [item['id'] for item in listed['items']] == [first['id'], second['id'], third['id']]
    assert second['continuedFrom'] == first['id'] and third['continuedFrom'] == second['id']
    for chat in listed['items']:
        packet = store.service.task_context.get(conversation_id=chat['id'])
        assert packet['issue']['id'] == issue['id']
    assert store.get(issue['id'])['work']['state'] == 'never_started'
    assert store.get(issue['id'])['status'] == 'backlog'


def test_semantic_registration_and_atomic_retries_do_not_duplicate(store):
    """Different request IDs still resolve one stable identity; unrelated instances remain separate."""
    issue = store.create({'title': 'Stable'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: bind(store, issue['id']), range(8)))
    assert len({row['id'] for row in rows}) == 1
    other = bind(store, issue['id'], instanceId='profile-b')
    assert other['id'] != rows[0]['id']
    assert len(store.service.conversations.list()['items']) == 2


def test_restart_reconnect_preserves_chat_and_task(tmp_path):
    """A new service owner keeps durable identity but requires fresh connection provenance."""
    first = IssueStore(tmp_path)
    issue = first.create({'title': 'Resume'})
    chat = bind(first, issue['id'])
    first.close()
    second = IssueStore(tmp_path)
    try:
        saved = second.service.conversations.resolve('codex', 'profile-a', 'chat-a')
        assert saved['id'] == chat['id'] and saved['issueId'] == issue['id']
        assert saved['connection']['reportedToCurrentOwner'] is False
        renewed = command(second, 'conversation.reconnect', {'conversationId': saved['id'],
                          'expectedRevision': saved['revision'], 'connectionId': 'new-connection'}, 'reconnect')
        assert renewed['revision'] == chat['revision'] + 1
        assert renewed['connection']['reportedToCurrentOwner'] is True
        assert second.service.task_context.get(conversation_id=renewed['id'])['issue']['title'] == 'Resume'
        assert command(second, 'conversation.reconnect', {'conversationId': saved['id'],
                       'expectedRevision': saved['revision'], 'connectionId': 'new-connection'}, 'reconnect') == renewed
        assert len(second.service.conversations.list()['items']) == 1
    finally:
        second.close()


def test_concurrent_reconnect_has_one_winner_and_fences_old_binding(store):
    """Only one transport replaces a known revision; the stale connection cannot reassign it."""
    issue = store.create({'title': 'CAS'})
    chat = bind(store, issue['id'])
    def reconnect(index):
        """Capture the expected domain conflict rather than retrying a changed intention."""
        try:
            return command(store, 'conversation.reconnect', {'conversationId': chat['id'],
                           'expectedRevision': 1, 'connectionId': f'new-{index}'})
        except DomainError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reconnect, [1, 2]))
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert 'conversation_conflict' in outcomes
    with pytest.raises(DomainError) as failure:
        bind(store, issue['id'], expectedRevision=2, title='Stale writer')
    assert failure.value.code == 'conversation_reconnect_required'
    assert store.service.conversations.get(chat['id'])['title'] == 'Work'


def test_explicit_reassignment_unlink_and_lineage_validation(store):
    """Changing a binding uses CAS; continuation is recorded only at creation for the same task."""
    one, two = store.create({'title': 'One'}), store.create({'title': 'Two'})
    chat = bind(store, one['id'])
    with pytest.raises(DomainError) as failure:
        bind(store, two['id'], remoteSessionId='new', continuedFrom=chat['id'])
    assert failure.value.code == 'conversation_lineage_conflict'
    changed = bind(store, two['id'], expectedRevision=1)
    assert changed['id'] == chat['id'] and changed['revision'] == 2
    unlinked = bind(store, None, expectedRevision=2)
    assert unlinked['issueId'] is None
    with pytest.raises(DomainError) as failure:
        store.service.task_context.get(conversation_id=chat['id'])
    assert failure.value.code == 'conversation_unbound'
    assert store.service.conversations.list()['items'][0]['id'] == chat['id']


def test_transient_observation_cleanup_does_not_remove_durable_chats(store):
    """72-hour observation retention cannot destroy a task's chat list or continuation context."""
    issue = store.create({'title': 'Durable relation'})
    chat = bind(store, issue['id'])
    source = command(store, 'session.register', {'title': 'Temporary', 'agent': 'codex', 'protocol': 'custom',
                     'protocolVersion': 1, 'remoteSessionId': 'chat-a', 'connectionId': 'old'})
    store.db.connection.execute("UPDATE service_sessions SET updated_at='2020-01-01T00:00:00+00:00'")
    assert store.service.session_retention.sweep(now=datetime.now(timezone.utc) + timedelta(days=4)) == [source['id']]
    assert store.service.conversations.get(chat['id'])['issueId'] == issue['id']
    assert store.service.task_context.get(issue['id'])['sections']['chats']['items'][0]['id'] == chat['id']


def test_context_is_fresh_revisioned_and_does_not_change_task_state(store):
    """A second agent reads updated criteria/notes/docs; stable reads have stable content digests."""
    issue = store.create({'title': 'Context', 'description': 'Before', 'dor': [
                         {'id': 'ready', 'text': 'Know scope', 'passed': False, 'evidence': ''}]})
    chat = bind(store, issue['id'])
    first = store.service.task_context.get(conversation_id=chat['id'])
    assert first == store.service.task_context.get(conversation_id=chat['id'])
    before_activity = store.get(issue['id'])['lastActivityAt']
    store.service.task_context.get(issue['id'])
    assert store.get(issue['id'])['lastActivityAt'] == before_activity
    store.update(issue['id'], {'description': 'After', 'dor': [
                 {'id': 'ready', 'text': 'New requirement', 'passed': True, 'evidence': 'Checked'}]})
    store.comment(issue['id'], 'Continue here')
    store.documents.save_in(store.db.connection, issue['id'], {'name': 'notes.md', 'content': 'Evidence'}, 'agent')
    current = store.service.task_context.get(issue['key'])
    assert current['sourceRevision'] != first['sourceRevision']
    assert current['sections']['description'] == 'After'
    assert current['sections']['criteria']['dor'][0]['text'] == 'New requirement'
    assert current['sections']['notes']['items'][0]['text'] == 'Continue here'
    assert current['sections']['documents'][0]['name'] == 'notes.md'
    assert current['trust'] == 'task_data_not_execution_authority'
    assert store.get(issue['id'])['work']['state'] == 'never_started'


def test_utf8_budget_and_omitted_source_changes_are_explicit(store):
    """Multibyte data respects the requested byte budget, and hidden changes still alter source revision."""
    issue = store.create({'title': 'Bounded', 'description': '界' * 8000})
    for index in range(5):
        store.comment(issue['id'], f'Note {index}: ' + '界' * 2000)
    packet = store.service.task_context.get(issue['id'], max_bytes=8192)
    assert len(serialized(packet)) <= 8192
    assert packet['complete'] is False and packet['omitted']
    assert packet['issue']['id'] == issue['id'] and packet['sourceRevision']
    store.update(issue['id'], {'description': '界' * 7999 + 'changed'})
    assert store.service.task_context.get(issue['id'], max_bytes=8192)['sourceRevision'] != packet['sourceRevision']
    with pytest.raises(DomainError):
        store.service.task_context.get(issue['id'], max_bytes=True)


def test_future_schema_rejected_before_legacy_work_reconciliation(tmp_path):
    """A future chat schema cannot partially migrate or rewrite existing task history."""
    owner = IssueStore(tmp_path)
    issue = owner.create({'title': 'Preserved'})
    owner.db.connection.execute('UPDATE conversation_version SET version=99')
    owner.close()
    with pytest.raises(ValueError, match='conversation schema'):
        IssueStore(tmp_path)
    with sqlite3.connect(tmp_path / 'issues.sqlite3') as connection:
        assert connection.execute('SELECT status FROM issues WHERE id=?', (issue['id'],)).fetchone()[0] == 'backlog'


def test_legacy_schema_upgrade_preserves_original_rows(tmp_path):
    """An owner created before the companion feature gains only additive registry tables."""
    owner = IssueStore(tmp_path)
    issue = owner.create({'title': 'Legacy task', 'description': 'Retained content'})
    owner.comment(issue['id'], 'Retained note')
    owner.close()
    with sqlite3.connect(tmp_path / 'issues.sqlite3') as connection:
        connection.execute('DROP TABLE conversations')
        connection.execute('DROP TABLE conversation_version')
        before = {table: connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                  for table in ('issues', 'comments', 'history', 'work_sessions', 'results', 'review_reports', 'service_sessions')}
    owner = IssueStore(tmp_path)
    try:
        for table, rows in before.items():
            assert [tuple(row) for row in owner.db.connection.execute('SELECT * FROM ' + table + ' ORDER BY rowid')] == rows
        assert owner.service.conversations.list()['items'] == []
    finally:
        owner.close()


def test_storage_rollback_and_pagination_keep_distinct_chats(store):
    """Failed command journal insertion cannot leave a ghost binding; paging loses no identities."""
    issue = store.create({'title': 'List'})
    store.db.connection.execute("CREATE TEMP TRIGGER reject_chat_outcome BEFORE INSERT ON service_commands BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.Error):
        bind(store, issue['id'])
    assert store.service.conversations.list()['items'] == []
    store.db.connection.execute('DROP TRIGGER reject_chat_outcome')
    first = bind(store, issue['id'])
    second = bind(store, issue['id'], remoteSessionId='second')
    page = store.service.conversations.list(1)
    next_page = store.service.conversations.list(1, page['nextCursor'])
    assert [page['items'][0]['id'], next_page['items'][0]['id']] == [first['id'], second['id']]
    assert not next_page['hasMore']


@pytest.mark.parametrize('operation,payload', [
    ('conversation.bind', {'shell': 'run something'}),
    ('conversation.reconnect', {'conversationId': 'missing', 'expectedRevision': True, 'connectionId': 'x'}),
])
def test_invalid_inputs_cannot_become_execution(store, operation, payload):
    """Malformed registry data never reaches a process or a task workflow transition."""
    with pytest.raises(DomainError):
        command(store, operation, payload)
    assert store.list() == []
    assert store.service.capabilities()['companion']['startsProcesses'] is False
