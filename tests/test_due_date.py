"""Scheduling contract: optional UTC deadlines never alter default lists or acceptance gates."""

import asyncio
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp
from workflow_helpers import metadata, review_ready


@pytest.fixture
def client(tmp_path):
    """Own an isolated HTTP service and close all fixture state after the test."""
    with TestClient(create_app(tmp_path), base_url='http://localhost') as value:
        yield value


def _create(client, **fields):
    """Use the public agent route so deadline validation includes the HTTP allowlist."""
    response = client.post('/api/v1/agent/issues', json={'title': 'Schedule this task', **fields})
    assert response.status_code == 201, response.text
    return response.json()


def test_due_date_detail_roundtrip_omission_clear_and_default_list_shape(client):
    """Deadline metadata is visible on details while the agent's default list stays unchanged."""
    issue = _create(client, dueDate='2028-02-29')
    path = '/api/v1/agent/issues/' + issue['id']
    assert issue['dueDate'] == '2028-02-29'
    assert client.get('/api/v1/issues/' + issue['id']).json()['dueDate'] == '2028-02-29'
    listed_before = client.get('/api/v1/issues').json()['items'][0]
    assert 'dueDate' not in listed_before
    changed = client.patch(path, json={'priority': 'high'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['dueDate'] == '2028-02-29'
    cleared = client.patch(path, json={'dueDate': None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['dueDate'] is None
    listed_after = client.get('/api/v1/issues').json()['items'][0]
    assert set(listed_after) == set(listed_before)
    assert 'dueDate' not in listed_after
    unset = _create(client)
    assert unset['dueDate'] is None


@pytest.mark.parametrize('value', [
    '', '2026-2-01', '2026-02-30', '2027-02-29', '2026-11-05T00:00:00Z',
    '0000-01-01', '10000-01-01', '２０２６-11-05', True, 20261105, [], {},
])
def test_invalid_due_date_rejects_the_whole_update(client, value):
    """Invalid scheduling input cannot partially change priority, deadline or audit history."""
    issue = _create(client, dueDate='2026-12-01')
    before = client.get('/api/v1/issues/' + issue['id']).json()
    response = client.patch('/api/v1/agent/issues/' + issue['id'],
                            json={'priority': 'urgent', 'dueDate': value})
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'invalid_due_date'
    after = client.get('/api/v1/issues/' + issue['id']).json()
    assert after['priority'] == before['priority']
    assert after['dueDate'] == before['dueDate']
    assert after['definitionRevision'] == before['definitionRevision']
    assert after['historyCount'] == before['historyCount']


def test_schedule_and_assignment_only_edits_preserve_the_real_work_lease(client):
    """Changing managerial metadata cannot terminate or replace an external agent's session."""
    store = client.app.state.store
    issue = _create(client, **metadata())
    started = store.execution.start(issue['id'], {'agentId': 'real-fixture-agent', 'sessionKey': 'owned-run'})
    active_before = started['work']['activeSession']
    response = client.patch('/api/v1/agent/issues/' + issue['id'],
                            json={'dueDate': '2026-12-01', 'assignee': 'Team alpha', 'priority': 'urgent'})
    assert response.status_code == 200, response.text
    after = response.json()
    assert after['status'] == 'in_progress'
    assert after['definitionRevision'] == started['definitionRevision']
    assert after['resultGeneration'] == started['resultGeneration']
    assert after['work']['activeSession'] == active_before
    assert after['dueDate'] == '2026-12-01'
    heartbeat = store.execution.heartbeat(issue['id'], {
        'agentId': 'real-fixture-agent', 'sessionKey': 'owned-run',
        'sessionId': active_before['sessionId'],
    })
    assert heartbeat['work']['activeSession']['sessionId'] == active_before['sessionId']
    assert not any(row['action'] in ('work_interrupted', 'work_stopped') for row in after['history'])


def test_schedule_only_edit_preserves_accepted_result_and_distinct_critic(client):
    """A scheduling change on completed work cannot silently revoke current acceptance."""
    store = client.app.state.store
    issue = _create(client, **metadata())
    review_ready(store, issue['id'])
    before = store.get(issue['id'])
    assert before['status'] == 'done'
    assert before['currentResultIsCurrent'] is True
    response = client.patch('/api/v1/agent/issues/' + issue['id'], json={'dueDate': '2026-12-01'})
    assert response.status_code == 200, response.text
    after = response.json()
    assert after['status'] == 'done'
    assert after['definitionRevision'] == before['definitionRevision']
    assert after['resultGeneration'] == before['resultGeneration']
    assert after['currentResult'] == before['currentResult']
    assert after['criticReviewId'] == before['criticReviewId']
    assert after['criticReview'] == before['criticReview']
    assert after['currentResultIsCurrent'] is True


def test_mcp_deadline_set_clear_and_omission_have_distinct_wire_payloads():
    """Existing tools retain default list schema and expose unambiguous scheduling mutations."""
    calls = []

    def handle(request):
        """Record exact agent-route JSON without reading the user's store or starting processes."""
        if request.url.path == '/api/v1/status':
            return httpx.Response(200, json={'application': 'ag-tasks', 'instanceId': 'fixture'})
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        return httpx.Response(200, json={'id': 'a' * 32})

    mcp = build_mcp(origin='http://127.0.0.1:8895', client=httpx.Client(transport=httpx.MockTransport(handle)))

    async def exercise():
        """Negotiate tools, then dispatch old and new calls through FastMCP's public API."""
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        schema = tools['tasks_list'].inputSchema['properties']
        assert set(schema) == {'status', 'parent_id', 'repo_id', 'limit', 'offset', 'space_id', 'include_archived'}
        assert schema['limit']['default'] == 50
        assert schema['offset']['default'] == 0
        assert schema['include_archived']['default'] is False
        await mcp.call_tool('tasks_create', {'title': 'With deadline', 'due_date': '2026-12-01'})
        await mcp.call_tool('tasks_update', {'issue_id': 'a' * 32, 'due_date': '2026-12-02'})
        await mcp.call_tool('tasks_update', {'issue_id': 'a' * 32, 'priority': 'high'})
        await mcp.call_tool('tasks_update', {'issue_id': 'a' * 32, 'clear_due_date': True})
        count = len(calls)
        rejected = await mcp.call_tool('tasks_update', {
            'issue_id': 'a' * 32, 'due_date': '2026-12-03', 'clear_due_date': True,
        })
        assert len(calls) == count
        assert 'invalid_due_date' in str(rejected)

    asyncio.run(exercise())
    issue_calls = [entry for entry in calls if '/agent/issues' in entry[1]]
    assert len(issue_calls) == 4
    assert issue_calls[0][0:2] == ('POST', '/api/v1/agent/issues')
    assert issue_calls[0][2]['dueDate'] == '2026-12-01'
    assert issue_calls[1][2] == {'dueDate': '2026-12-02'}
    assert issue_calls[2][2] == {'priority': 'high'}
    assert issue_calls[3][2] == {'dueDate': None}


def test_deadline_metadata_cannot_bypass_archive_or_generated_review_readonly(client):
    """Scheduling convenience does not grant mutation authority over protected cards."""
    store = client.app.state.store
    archived = _create(client)
    store.db.connection.execute('UPDATE issues SET archived_at=created_at WHERE id=?', (archived['id'],))
    source = _create(client, **metadata())
    review_ready(store, source['id'])
    critic_id = store.get(source['id'])['criticReviewId']
    for identifier in (archived['id'], critic_id):
        response = client.patch('/api/v1/agent/issues/' + identifier, json={'dueDate': '2026-12-01'})
        assert response.status_code == 409, response.text
        assert response.json()['error']['code'] == 'issue_read_only'
        assert store.scheduling.read(identifier) is None
