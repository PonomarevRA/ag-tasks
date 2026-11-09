"""
Verify session order/retention only in owned synthetic Tasks databases.
"""

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.issues import DomainError, IssueStore
from ag_tasks.session_retention import SessionRetention
from test_service_data import register, append, event, command

NOW = datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    """
    Own every observation and mutation in a disposable database.
    """
    value = IssueStore(tmp_path)
    yield value
    value.close()


def age(store, session_id, instant):
    """
    Set a deterministic persisted activity boundary without changing production APIs.
    """
    store.db.connection.execute('UPDATE service_sessions SET updated_at=? WHERE id=?',
                               (instant.isoformat(), session_id))


def mcp(store):
    """
    Register a synthetic MCP binding, without opening an actual client.
    """
    return command(store, 'session.register', {'title':'MCP', 'agent':'test', 'protocol':'mcp',
        'protocolVersion':1, 'connectionId':'connection-1', 'remoteSessionId':'remote',
        'capabilities':{'sourceKind':'mcp_connection'}})


@pytest.mark.parametrize('sort', ['newest','oldest'])
def test_activity_order_pages_ties_and_deleted_boundary(store, sort):
    """
    Every equal-time row appears once; a deleted boundary row cannot break continuation.
    """
    sessions = [register(store) for _ in range(5)]
    for index, session in enumerate(sessions):
        age(store, session['id'], NOW - timedelta(hours=index//2))
    expected = sorted(sessions, key=lambda s:(store.service.sessions.get(s['id'])['updatedAt'],s['id']),
                      reverse=sort=='newest')
    first = store.service.sessions.list(limit=2, sort=sort)
    assert [s['id'] for s in first['items']] == [s['id'] for s in expected[:2]]
    boundary = first['items'][-1]['id']
    store.db.connection.execute('DELETE FROM service_sessions WHERE id=?', (boundary,))
    second = store.service.sessions.list(limit=2, after=first['nextCursor'], sort=sort)
    third = store.service.sessions.list(limit=2, after=second['nextCursor'], sort=sort)
    assert [s['id'] for s in first['items']+second['items']+third['items']] == [s['id'] for s in expected]
    assert third['hasMore'] is False
    with pytest.raises(DomainError) as wrong_order:
        store.service.sessions.list(after=first['nextCursor'], sort='oldest' if sort=='newest' else 'newest')
    assert wrong_order.value.code == 'invalid_page'
    with pytest.raises(DomainError):
        store.service.sessions.list(after=first['nextCursor'], sort=sort, active_only=True)


@pytest.mark.parametrize('value', [True,-1,'bad-token','-1','9'*30,'A'*1025])
def test_invalid_session_cursors_are_domain_errors(store, value):
    """
    Malformed bounded tokens cannot become unhandled server errors or SQL fragments.
    """
    with pytest.raises(DomainError) as error:
        store.service.sessions.list(after=value)
    assert error.value.code == 'invalid_page'


def test_retention_exact_boundary_dependents_and_command_replay(store):
    """
    Expiry removes observations atomically while retaining task/audit identities and idempotence.
    """
    session = register(store)
    append(store,session['id'],[event(1,{'jsonrpc':'2.0','id':1,'method':'session/prompt','params':{}},direction='client_to_agent')])
    append(store,session['id'],[event(2,{'jsonrpc':'2.0','method':'session/update','params':{
        'sessionId':'remote-1','update':{'sessionUpdate':'available_commands_update','availableCommands':[]}}})])
    before_commands = store.db.connection.execute('SELECT COUNT(*) FROM service_commands').fetchone()[0]
    before_facts = store.db.connection.execute('SELECT COUNT(*) FROM service_changes').fetchone()[0]
    age(store,session['id'],NOW-timedelta(days=3))
    assert store.service.session_retention.sweep(now=NOW-timedelta(microseconds=1)) == []
    assert store.service.session_retention.sweep(now=NOW) == [session['id']]
    assert store.service.session_retention.sweep(now=NOW) == []
    for table in ('service_sessions','service_events','service_streams','service_entities','service_requests'):
        assert store.db.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert store.db.connection.execute('SELECT COUNT(*) FROM service_commands').fetchone()[0] == before_commands
    assert store.db.connection.execute('SELECT COUNT(*) FROM service_changes').fetchone()[0] == before_facts
    assert store.db.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(DomainError) as missing:
        store.service.sessions.get(session['id'])
    assert missing.value.code == 'session_not_found'


def test_connected_mcp_and_restart_grace_are_protected(store):
    """
    Old connected metadata survives; unknown old MCP gets one lease to reconnect after restart.
    """
    active, unknown = mcp(store), mcp(store)
    cleanup_clock = [0]
    cleanup = SessionRetention(store,clock=lambda:cleanup_clock[0])
    store.service.sessions.touch_presence(active['id'],{'connectionId':'connection-1','state':'connected'})
    for item in (active,unknown):
        age(store,item['id'],NOW-timedelta(days=10))
    assert cleanup.sweep(now=NOW) == []
    cleanup_clock[0] = 90
    assert cleanup.sweep(now=NOW) == [unknown['id']]
    assert store.service.sessions.get(active['id'])['connection']['state'] == 'connected'
    before = store.db.connection.execute('SELECT COUNT(*) FROM service_events').fetchone()[0]
    store.service.sessions.touch_presence(active['id'],{'connectionId':'connection-1','state':'disconnected'})
    assert store.service.sessions.get(active['id'])['updatedAt'] > (NOW-timedelta(days=1)).isoformat()
    assert cleanup.sweep(now=NOW) == []
    assert store.db.connection.execute('SELECT COUNT(*) FROM service_events').fetchone()[0] == before


def test_queue_and_live_work_protect_sessions_but_terminal_links_can_expire(store):
    """
    Queued/quarantined ownership stays intact; optional terminal history can lose its expired link.
    """
    source = register(store)
    lane = command(store,'queue.create',{'laneId':'lane','title':'Lane','profile':{
        'id':'test','version':'1','protocol':'acp','protocolVersion':1}})
    item = command(store,'queue.enqueue',{'laneId':lane['id'],'text':'Work','sessionId':source['id']})
    age(store,source['id'],NOW-timedelta(days=4))
    assert store.service.session_retention.sweep(now=NOW) == []
    store.db.connection.execute("UPDATE agent_queue_items SET state='completed' WHERE id=?",(item['id'],))
    assert store.service.session_retention.sweep(now=NOW) == [source['id']]
    row = store.db.connection.execute('SELECT session_id,state FROM agent_queue_items WHERE id=?',(item['id'],)).fetchone()
    assert row['session_id'] is None and row['state'] == 'completed'


def test_running_task_lease_protects_its_linked_observation(store):
    """
    An observed binding linked to a live task cannot expire while work is running.
    """
    from workflow_helpers import metadata
    instant = datetime.now(timezone.utc)
    issue = store.create({'title':'Live task',**metadata()})
    active = store.execution.start(issue['id'],{'agentId':'test','sessionKey':'live'})
    source = register(store)
    store.service.sessions.link({'sessionId':source['id'],'issueId':issue['id'],'expectedRevision':1})
    age(store,source['id'],instant-timedelta(days=4))
    assert store.service.session_retention.sweep(now=instant) == []
    store.execution.stop(issue['id'],{'agentId':'test','sessionKey':'live',
        'sessionId':active['work']['activeSession']['sessionId']})
    assert store.service.session_retention.sweep(now=instant) == [source['id']]


def test_cleanup_failure_rolls_back_the_whole_observation(store):
    """
    A failed dependent delete must preserve every source row and its event checkpoint.
    """
    import sqlite3
    source = register(store)
    append(store,source['id'],[event(1,{'jsonrpc':'2.0','method':'future'})])
    age(store,source['id'],NOW-timedelta(days=4))
    store.db.connection.execute("CREATE TEMP TRIGGER retention_fault BEFORE DELETE ON service_sessions BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(sqlite3.Error):
        store.service.session_retention.sweep(now=NOW)
    assert store.service.sessions.get(source['id'])['eventCount'] == 1
    assert store.db.connection.execute('SELECT COUNT(*) FROM service_streams').fetchone()[0] == 1


def test_http_sort_contract_and_startup_retention(tmp_path):
    """
    A real app catches up on closed time and returns activity tokens through its HTTP contract.
    """
    data = tmp_path/'state'
    previous = IssueStore(data)
    expired = register(previous)
    age(previous,expired['id'],datetime.now(timezone.utc)-timedelta(days=4))
    previous.close()
    with TestClient(create_app(data),base_url='http://127.0.0.1:8895') as client:
        assert client.get('/api/v1/sessions').json()['items'] == []
        for index in range(3):
            response = client.post('/api/v1/agent/commands',json={'clientId':'sort','commandId':str(index),
                'operation':'session.register','payload':{'title':str(index),'agent':'test','protocol':'unknown',
                    'protocolVersion':1,'remoteSessionId':'remote','connectionId':'connection'}})
            assert response.status_code == 200
        first = client.get('/api/v1/sessions?limit=1').json()
        assert first['items'][0]['title'] == '2'
        next_page = client.get('/api/v1/sessions',params={'limit':1,'after':first['nextCursor']}).json()
        assert next_page['items'][0]['title'] == '1'
        assert client.get('/api/v1/sessions?sort=oldest').json()['items'][0]['title'] == '0'
        assert client.get('/api/v1/sessions?sort=wrong').status_code == 400
        assert client.get('/api/v1/sessions?after=bad').status_code == 400
        contract = client.get('/api/v1/service-data').json()['sessionHistory']
        assert contract['retentionDays'] == 3 and contract['defaultSort'] == 'newest'


def test_session_order_preserves_submillisecond_activity(store):
    """
    Activity a microsecond newer must win even when its ID sorts in the opposite direction.
    """
    from test_service_data import register
    sources=sorted([register(store),register(store)],key=lambda row:row['id'])
    older,newer=sources[1],sources[0]
    store.db.connection.execute('UPDATE service_sessions SET updated_at=? WHERE id=?',('2026-10-29T12:00:00.000001+00:00',older['id']))
    store.db.connection.execute('UPDATE service_sessions SET updated_at=? WHERE id=?',('2026-10-29T12:00:00.000002+00:00',newer['id']))
    first=store.service.sessions.list(limit=1)
    assert first['items'][0]['id']==newer['id']
    assert store.service.sessions.list(limit=1,after=first['nextCursor'])['items'][0]['id']==older['id']
