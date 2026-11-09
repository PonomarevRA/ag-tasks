"""Exercise the fixed workflow MCP surface without granting UI authority or touching real repositories/data."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp


TOOLS = {'tasks_list', 'tasks_get', 'tasks_repositories', 'tasks_create', 'tasks_update', 'tasks_link', 'tasks_comment',
         'tasks_prepare_branch', 'tasks_review', 'tasks_submit_review', 'tasks_spaces', 'tasks_graph', 'tasks_decompose',
         'tasks_plan', 'tasks_document', 'tasks_write_document', 'tasks_problems', 'tasks_problem', 'tasks_resolve_problem', 'tasks_actual_complexity', 'tasks_status_audit','tasks_pending_reviews','tasks_pause_work','tasks_problems_digest', 'tasks_capabilities','tasks_work_history','tasks_planning_rules',
         'tasks_work_start', 'tasks_work_heartbeat', 'tasks_work_stop', 'tasks_submit_result', 'tasks_review_report'}
TOOLS |= {'tasks_service_capabilities', 'tasks_command', 'tasks_command_outcome',
          'tasks_sessions', 'tasks_session', 'tasks_session_events', 'tasks_events', 'tasks_queues', 'tasks_queue', 'tasks_queue_items', 'tasks_attempt', 'tasks_project_tree', 'tasks_ensure_project_space', 'tasks_move_project_tree'}
IDENTIFIER = 'a' * 32
TOOLS |= {'tasks_conversations', 'tasks_bind_conversation', 'tasks_reconnect_conversation',
          'tasks_resolve_conversation', 'tasks_task_context'}
TOOLS |= {'tasks_process_evidence', 'tasks_process_manifest', 'tasks_process_run'}
SESSION = {'agent_id': 'producer', 'session_id': 'session-id', 'session_key': 'start-key'}


def call(mcp, name, **arguments):
    """Use public FastMCP dispatch and unwrap its normal structured/text result across SDK versions."""
    value = asyncio.run(mcp.call_tool(name, arguments))
    if isinstance(value, tuple):
        content, structured = value
        if structured is not None:
            return structured
        value = content
    if isinstance(value, dict):
        return value
    if hasattr(value, 'structuredContent') and value.structuredContent is not None:
        return value.structuredContent
    content = value.content if hasattr(value, 'content') else value
    return json.loads(next(item.text for item in content if getattr(item, 'type', None) == 'text'))


@pytest.fixture
def bridge(tmp_path):
    """Record only bounded mock HTTP calls; every MCP request first verifies the fixed service identity."""
    calls = []
    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.url.path == '/api/v1/status':
            return httpx.Response(200, json={'application': 'ag-tasks', 'instanceId': 'fixture'})
        return httpx.Response(200, json={'ok': True, 'body': body})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        yield build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client), calls


def test_tools_are_an_exact_capability_allowlist(bridge):
    mcp, _ = bridge
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == TOOLS
    for tool in tools:
        fields = tool.inputSchema.get('properties', {})
        assert not {'actor', 'origin', 'path', 'command', 'expectedHead', 'expectedBase'} & fields.keys()
    assert not any(word in name for name in TOOLS for word in ('approve', 'delete', 'archive', 'restore'))


def test_work_commands_keep_server_owned_session_identity_and_never_send_time(bridge):
    mcp, calls = bridge
    call(mcp, 'tasks_work_start', issue_id=IDENTIFIER, agent_id='producer', session_key='start-key')
    call(mcp, 'tasks_work_heartbeat', issue_id=IDENTIFIER, **SESSION)
    call(mcp, 'tasks_work_stop', issue_id=IDENTIFIER, **SESSION)
    posts = [entry for entry in calls if entry[0] == 'POST']
    assert [entry[1] for entry in posts] == [f'/api/v1/agent/issues/{IDENTIFIER}/work/{action}' for action in ('start', 'heartbeat', 'stop')]
    assert posts[0][3] == {'agentId': 'producer', 'sessionKey': 'start-key'}
    assert posts[1][3] == posts[2][3] == {'agentId': 'producer', 'sessionId': 'session-id', 'sessionKey': 'start-key'}
    assert all(not {'elapsedMs', 'startedAt', 'role', 'actor'} & entry[3].keys() for entry in posts)


def test_result_and_critic_report_preserve_exact_evidence_and_revision_fields(bridge):
    mcp, calls = bridge
    artifacts = [{'url': 'http://127.0.0.1:8890/incidents/fixture', 'title': 'Evidence', 'kind': 'incident'}]
    call(mcp, 'tasks_submit_result', issue_id=IDENTIFIER, **SESSION, summary='Implemented', changes=['changed'],
         validation=['reported check'], limitations='No native acceptance', artifacts=artifacts)
    result = [entry for entry in calls if entry[1].endswith('/results')][0]
    assert result[1].startswith('/api/v1/agent/')
    assert result[3]['result'] == {'summary': 'Implemented', 'changes': ['changed'], 'validation': ['reported check'],
                                   'limitations': 'No native acceptance', 'artifacts': artifacts}
    call(mcp, 'tasks_review_report', critic_id='b' * 32, agent_id='critic', session_id='critic-session', session_key='critic-key',
         generation=3, definition_revision=7, reviewed_head='c' * 40, verdict='pass', summary='Reviewed', findings=[], evidence='Inspected current changes')
    report = calls[-1]
    assert report[1] == '/api/v1/agent/issues/' + 'b' * 32 + '/review-report'
    assert report[3]['generation'] == 3 and report[3]['definitionRevision'] == 7 and report[3]['reviewedHead'] == 'c' * 40
    assert 'actor' not in report[3]


def test_spaces_graph_and_decomposition_do_not_grant_space_or_filesystem_authority(bridge):
    mcp, calls = bridge
    call(mcp, 'tasks_spaces')
    call(mcp, 'tasks_graph', space_id='space-x', limit=20)
    criteria = [{'id': 'ready', 'text': 'Source known', 'passed': True, 'evidence': 'Inspected'}]
    children = [{'title': 'Child', 'intendedRole': 'qa', 'dor': criteria}]
    call(mcp, 'tasks_decompose', issue_id=IDENTIFIER, children=children)
    assert ('GET', '/api/v1/spaces', {}, None) in calls
    assert ('GET', '/api/v1/issues/graph', {'spaceId': 'space-x', 'limit': '20'}, None) in calls
    assert calls[-1][1:] == (f'/api/v1/agent/issues/{IDENTIFIER}/decompose', {}, {'children': children})
    call(mcp, 'tasks_create', title='Inherited child', parent_id=IDENTIFIER, intended_role='developer', dor=criteria, dod=criteria)
    assert 'spaceId' not in calls[-1][3]
    call(mcp, 'tasks_update', issue_id=IDENTIFIER, intended_role='qa', space_id='space-x', dor=[])
    assert calls[-1][3] == {'intendedRole': 'qa', 'spaceId': 'space-x', 'dor': []}


def test_stale_discovery_identity_blocks_every_new_mutation(tmp_path):
    (tmp_path / 'endpoint.json').write_text(json.dumps({'origin': 'http://127.0.0.1:8895', 'instanceId': 'expected'}))
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={'application': 'ag-tasks', 'instanceId': 'different'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        mcp = build_mcp(directory=tmp_path, client=client)
        result = call(mcp, 'tasks_work_start', issue_id=IDENTIFIER, agent_id='producer', session_key='key')
    assert result['error']['code'] == 'tasks_unavailable'
    assert calls == [('GET', '/api/v1/status')]


def test_compatibility_submit_review_preserves_server_gate_failure_without_hidden_bypass(tmp_path):
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/v1/status':
            return httpx.Response(200, json={'application': 'ag-tasks'})
        if request.method == 'GET':
            return httpx.Response(200, json={'head': 'c' * 40})
        return httpx.Response(409, json={'error': {'code': 'result_required', 'message': 'Submit a current result first.'}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client)
        result = call(mcp, 'tasks_submit_review', issue_id=IDENTIFIER)
    assert result['error']['code'] == 'result_required'
    assert not any('approve' in path or '/results' in path or 'ui-session' in path for _, path in calls)


def test_criteria_failure_details_survive_the_bridge(tmp_path):
    def handle(request):
        if request.url.path == '/api/v1/status':
            return httpx.Response(200, json={'application': 'ag-tasks'})
        return httpx.Response(409, json={'error': {'code': 'criteria_failed', 'details': {
            'issueId': IDENTIFIER, 'status': 'rework', 'criterionFailures': [{'id': 'source', 'text': 'Source', 'reason': 'not_passed'}]}}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client)
        value = call(mcp, 'tasks_work_start', issue_id=IDENTIFIER, agent_id='producer', session_key='key')
    assert value['error']['details']['status'] == 'rework'
    assert value['error']['details']['criterionFailures'][0]['id'] == 'source'


def test_oversized_payload_and_invalid_status_object_never_mutate(tmp_path):
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json={'application': 'ag-tasks'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client)
        value = call(mcp, 'tasks_submit_result', issue_id=IDENTIFIER, **SESSION, summary='x' * 65536, changes=[], validation=[])
        assert value['error']['code'] == 'invalid_field' and calls == []
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))) as client:
        mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client)
        assert call(mcp, 'tasks_spaces')['error']['code'] == 'tasks_unavailable'


@pytest.fixture
def service_bridge(tmp_path):
    """Run the actual HTTP/domain composition in temporary storage; the MCP bridge alone never obtains a UI token."""
    calls = []
    with TestClient(create_app(tmp_path), base_url='http://127.0.0.1:8895') as service:
        def handle(request):
            calls.append((request.method, request.url.path))
            response = service.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client)
            yield mcp, service, calls


def ready_issue(mcp, title='Ready work'):
    """Create declared/evidenced readiness and completion criteria; these are fixture assertions, never executed proofs."""
    criteria = [{'id': 'checked', 'text': 'Inputs inspected', 'passed': True, 'evidence': 'Fixture review record'}]
    issue = call(mcp, 'tasks_create', title=title, intended_role='developer', dor=criteria, dod=criteria)
    assert 'error' not in issue, issue
    return call(mcp, 'tasks_update', issue_id=issue['id'], status='ready')


def test_actual_http_work_session_replay_pause_and_status_bypass_guards(service_bridge):
    mcp, _, calls = service_bridge
    issue = ready_issue(mcp)
    started = call(mcp, 'tasks_work_start', issue_id=issue['id'], agent_id='producer', session_key='work-1')
    assert 'error' not in started, started
    session_id = started['work']['activeSession']['sessionId']
    replay = call(mcp, 'tasks_work_start', issue_id=issue['id'], agent_id='producer', session_key='work-1')
    assert replay['work']['activeSession']['sessionId'] == session_id
    wrong = call(mcp, 'tasks_work_heartbeat', issue_id=issue['id'], agent_id='other', session_id=session_id, session_key='work-1')
    assert 'error' in wrong
    heartbeat = call(mcp, 'tasks_work_heartbeat', issue_id=issue['id'], agent_id='producer', session_id=session_id, session_key='work-1')
    assert heartbeat['work']['state'] == 'running'
    stopped = call(mcp, 'tasks_work_stop', issue_id=issue['id'], agent_id='producer', session_id=session_id, session_key='work-1')
    repeated = call(mcp, 'tasks_work_stop', issue_id=issue['id'], agent_id='producer', session_id=session_id, session_key='work-1')
    assert stopped['work']['state'] == repeated['work']['state'] == 'paused'
    assert stopped['work']['elapsedMs'] == repeated['work']['elapsedMs']
    bypass = call(mcp, 'tasks_update', issue_id=issue['id'], status='in_progress')
    assert bypass['error']['code'] == 'work_start_required'
    assert not any('ui-session' in path or 'approve' in path for _, path in calls)


def test_actual_http_result_creates_distinct_critic_and_never_grants_human_completion(service_bridge):
    mcp, _, calls = service_bridge
    issue = ready_issue(mcp)
    started = call(mcp, 'tasks_work_start', issue_id=issue['id'], agent_id='producer', session_key='produce')
    session_id = started['work']['activeSession']['sessionId']
    result = call(mcp, 'tasks_submit_result', issue_id=issue['id'], agent_id='producer', session_id=session_id,
                  session_key='produce', summary='Implemented fixture result', changes=['A bounded change'],
                  validation=['Caller-reported test'], limitations='Not authenticated runtime proof')
    assert 'error' not in result, result
    assert result['status'] == 'in_review' and result['criticReviewId']
    critic_id = result['criticReviewId']
    critic = call(mcp, 'tasks_get', issue_id=critic_id)
    assert critic['kind'] == 'review' and critic['intendedRole'] == 'critic'
    same_agent = call(mcp, 'tasks_work_start', issue_id=critic_id, agent_id='producer', session_key='self-review')
    assert 'error' in same_agent
    critic_work = call(mcp, 'tasks_work_start', issue_id=critic_id, agent_id='independent-critic', session_key='critic')
    assert 'error' not in critic_work, critic_work
    critic_session = critic_work['work']['activeSession']['sessionId']
    report_args = {'critic_id': critic_id, 'agent_id': 'independent-critic', 'session_id': critic_session, 'session_key': 'critic',
                   'generation': result['resultGeneration'], 'definition_revision': result['definitionRevision'],
                   'reviewed_head': result['currentResult']['reviewedHead'], 'verdict': 'pass', 'summary': 'Inspected',
                   'findings': [], 'evidence': 'Fixture source and checks inspected'}
    stale = call(mcp, 'tasks_review_report', **dict(report_args, generation=result['resultGeneration'] + 1))
    assert 'error' in stale
    denied = call(mcp,'tasks_update',issue_id=issue['id'],status='done')
    assert 'error' in denied
    reported = call(mcp, 'tasks_review_report', **report_args)
    assert 'error' not in reported, reported
    assert reported['criticReview']['verdict'] == 'pass'
    assert call(mcp, 'tasks_get', issue_id=critic_id)['status'] == 'done'
    done = call(mcp, 'tasks_update', issue_id=issue['id'], status='done')
    assert done['status']=='done'
    assert not any('ui-session' in path or 'approve' in path for _, path in calls)


def test_actual_http_decomposition_is_atomic_and_graph_keeps_parent_direction(service_bridge):
    mcp, _, _ = service_bridge
    parent = ready_issue(mcp, title='Parent')
    spaces = call(mcp, 'tasks_spaces')
    assert any(value['id'] == 'default' for value in spaces['items'])
    created = call(mcp, 'tasks_decompose', issue_id=parent['id'], children=[{'title': 'Child A'}, {'title': 'Child B'}])
    assert len(created['items']) == 2
    assert all(child['spaceId'] == 'default' and child['parentId'] == parent['id'] for child in created['items'])
    failed = call(mcp, 'tasks_decompose', issue_id=parent['id'], children=[{'title': 'Must rollback'}, {'title': ''}])
    assert 'error' in failed
    items = call(mcp, 'tasks_list', parent_id=parent['id'], space_id='default')['items']
    assert len(items) == 2
    graph = call(mcp, 'tasks_graph', space_id='default', limit=200)
    assert graph['nodeCount'] >= 3
    for child in created['items']:
        assert {'from': parent['id'], 'to': child['id'], 'kind': 'parent'} in graph['edges']


def test_oversized_local_response_is_bounded_without_any_write(tmp_path):
    """An accidental unbounded local response cannot flood MCP output or trigger a follow-up mutation."""
    calls = []
    def handle(request):
        calls.append(request.method)
        if request.url.path == '/api/v1/status':
            return httpx.Response(200, json={'application': 'ag-tasks'})
        return httpx.Response(200, content=b'{"items":["' + b'x' * 2_000_000 + b'"]}')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        mcp = build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path, client=client)
        value = call(mcp, 'tasks_spaces')
    assert value['error']['code'] == 'tasks_unavailable'
    assert calls == ['GET', 'GET']


def test_code_classification_uses_existing_fixed_mcp_routes(bridge):
    """Multiple language/system labels cross MCP unchanged, and omitted values do not clear existing metadata."""
    mcp, calls = bridge
    call(mcp, 'tasks_create', title='Code', code_task=True, languages=['Python', 'SQL'], systems=['Tasks', 'Atlas'])
    assert calls[-1][3]['codeTask'] is True and calls[-1][3]['languages'] == ['Python', 'SQL']
    call(mcp, 'tasks_update', issue_id=IDENTIFIER, systems=['Observer'])
    assert calls[-1][3] == {'systems': ['Observer']}
    call(mcp, 'tasks_update', issue_id=IDENTIFIER, code_task=False, languages=[])
    assert calls[-1][3] == {'codeTask': False, 'languages': []}


def test_new_capabilities_and_role_history_use_readonly_fixed_routes(service_bridge):
    mcp, _, calls = service_bridge
    capabilities=call(mcp,'tasks_capabilities')
    assert capabilities['agentContract']['sessionRoleHistory']
    issue=ready_issue(mcp)
    started=call(mcp,'tasks_work_start',issue_id=issue['id'],agent_id='producer',session_key='history')
    history=call(mcp,'tasks_work_history',issue_id=issue['id'],limit=1)
    assert history['items'][0]['intendedRole']=='developer'
    assert 'sessionKey' not in history['items'][0]
    assert call(mcp,'tasks_get',issue_id=issue['id'])['workHistory']['items'][0]['sessionId']==started['work']['activeSession']['sessionId']


@pytest.mark.parametrize('artifacts,field', [
    ([{'url': 'file:///private/work/report.md'}], 'artifacts[0].url'),
    ([{'url': 'https://user:secret@example.invalid/evidence'}], 'artifacts[0].url'),
    ([{'url': 'https://example.invalid', 'path': '/private/work'}], 'artifacts[0]'),
    ([{'title': 'Missing URL'}], 'artifacts[0].url'),
    ([{'url': 'https://example.invalid'}] * 21, 'artifacts'),
])
def test_result_references_are_validated_before_any_http(bridge, artifacts, field):
    """Malformed caller evidence is actionable without a service write or private URL echo."""
    mcp, calls = bridge
    result = call(mcp, 'tasks_submit_result', issue_id=IDENTIFIER, **SESSION, summary='Checked result',
                  changes=['Scoped change'], validation=['Reported check'], artifacts=artifacts)
    error = result['error']
    assert error['code'] == 'invalid_references'
    assert error['details']['field'] == field
    assert 'Markdown' in error['details']['recovery']
    assert 'secret' not in json.dumps(error) and '/private/work' not in json.dumps(error)
    assert calls == []


def test_failed_result_can_be_corrected_before_actual_complexity(service_bridge):
    """Rejected artifacts preserve producer work and result generation; only accepted results admit complexity."""
    mcp, _, _ = service_bridge
    issue = ready_issue(mcp)
    started = call(mcp, 'tasks_work_start', issue_id=issue['id'], agent_id='producer', session_key='correct-result')
    credentials = {'agent_id': 'producer', 'session_id': started['work']['activeSession']['sessionId'], 'session_key': 'correct-result'}
    content = {'summary': 'Checked result', 'changes': ['Scoped change'], 'validation': ['See /private/work/report.md']}
    rejected = call(mcp, 'tasks_submit_result', issue_id=issue['id'], **credentials, **content,
                    artifacts=[{'url': 'file:///private/work/report.md'}])
    assert rejected['error']['code'] == 'invalid_references'
    unchanged = call(mcp, 'tasks_get', issue_id=issue['id'])
    assert unchanged['resultGeneration'] == 0 and unchanged['work']['activeSession']['sessionId'] == credentials['session_id']
    premature = call(mcp, 'tasks_actual_complexity', issue_id=issue['id'], points=2, reason='Fixture evidence')
    assert premature['error']['code'] == 'result_required'
    accepted = call(mcp, 'tasks_submit_result', issue_id=issue['id'], **credentials, **content, artifacts=[])
    assert accepted['currentResult']['generation'] == 1 and accepted['work']['activeSession'] is None
    rated = call(mcp, 'tasks_actual_complexity', issue_id=issue['id'], points=2, reason='Fixture evidence')
    assert rated['actualComplexity']['points'] == 2
