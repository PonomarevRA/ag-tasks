"""HTTP authority and issue lifecycle acceptance tests independent of the UI implementation."""

import asyncio
import json

from fastapi.testclient import TestClient
import httpx

from ag_tasks.app import create_app
from ag_tasks.mcp_server import build_mcp
from workflow_helpers import metadata, review_ready


def session(tmp_path):
    """Create an isolated local service; TestClient owns its lifecycle and data lock."""
    return TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8895")


def ui_headers(client):
    """Obtain the local UI guard as the first-party browser does."""
    return {"X-Ag-Tasks-UI": client.get('/api/v1/ui-session').json()['token']}


def test_agent_issue_evidence_hierarchy_and_audit(tmp_path):
    """An agent can create linked incident tasks and subtasks without contacting Observer."""
    with session(tmp_path) as client:
        parent = client.post('/api/v1/agent/issues', json={'title':'Investigate latency', 'externalRefs':[
            {'url':'http://127.0.0.1:8890/?incidentId=' + 'a'*32, 'title':'Observed incident', 'kind':'incident'}]})
        assert parent.status_code == 201, parent.text
        parent = parent.json()
        child = client.post('/api/v1/agent/issues', json={'title':'Check SQL', 'parentId':parent['id'], **metadata()}).json()
        assert child['parentId'] == parent['id']
        response = client.post('/api/v1/agent/issues/'+child['id']+'/work/start', json={'agentId':'agent-1','sessionKey':'actual-work'})
        assert response.status_code == 200, response.text
        response = client.post('/api/v1/agent/issues/'+child['id']+'/comments', json={'text':'Reported check: p95 450ms'})
        assert response.status_code == 200, response.text
        result = client.get('/api/v1/issues/'+child['id']).json()
        assert result['comments'][-1]['actor'] == 'agent'
        assert result['history']
        assert client.get('/api/v1/issues').json()['items']
        assert client.get('/api/v1/issues/'+parent['id']).json()['externalRefs'][0]['kind'] == 'incident'


def test_agent_has_no_approval_or_repository_registration_route(tmp_path):
    """Agent capabilities cannot grant a path, impersonate a user, or mark work approved."""
    with session(tmp_path) as client:
        issue = client.post('/api/v1/agent/issues',json={'title':'Work'}).json()
        assert client.post('/api/v1/repositories',json={'path':str(tmp_path)}).status_code == 403
        assert client.post('/api/v1/issues/'+issue['id']+'/approve-merge',json={'expectedHead':'x','expectedBase':'y'}).status_code == 403
        assert client.post('/api/v1/agent/issues',json={'title':'Spoof','actor':'user'}).status_code == 400
        assert client.patch('/api/v1/agent/issues/'+issue['id'],json={'status':'done'}).status_code >= 400
        assert client.patch('/api/v1/agent/issues/'+issue['id'],json={'status':'cancelled'}).status_code >= 400
        assert client.post('/api/v1/agent/issues/'+issue['id']+'/approve-merge',json={}).status_code in {404, 405}


def test_first_party_ui_noncode_completion(tmp_path):
    """An explicit UI user may complete a non-code issue without any Git action."""
    with session(tmp_path) as client:
        headers = ui_headers(client)
        issue = client.post('/api/v1/issues', headers=headers, json={'title':'Document finding', **metadata()}).json()
        review_ready(client.app.state.store, issue['id'])
        result = client.patch('/api/v1/issues/'+issue['id'], headers=headers, json={'status':'done'})
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'done'


def test_local_boundary_and_byte_limit(tmp_path):
    """Reject cross-origin writes, DNS-rebinding Hosts and oversized agent payloads."""
    with session(tmp_path) as client:
        assert client.get('/api/v1/status',headers={'Origin':'https://example.com'}).status_code == 403
        assert client.get('/api/v1/status',headers={'Host':'attacker.invalid'}).status_code == 403
        assert client.get('/api/v1/ui-session',headers={'Sec-Fetch-Site':'cross-site'}).status_code == 403
        assert client.post('/api/v1/agent/issues',content=b'x'*65537).status_code == 413
        assert client.post('/api/v1/agent/issues',content=b'not json').status_code == 400
        status = client.get('/api/v1/status')
        assert status.json()['application'] == 'ag-tasks'
        assert "unsafe-eval" not in status.headers['content-security-policy']
        assert status.headers['cache-control'] == 'no-store'


def test_cyclic_parent_and_unsafe_reference_rejected(tmp_path):
    """Issue relations cannot create a hierarchy cycle or executable evidence URL."""
    with session(tmp_path) as client:
        parent = client.post('/api/v1/agent/issues',json={'title':'Parent'}).json()
        child = client.post('/api/v1/agent/issues',json={'title':'Child','parentId':parent['id']}).json()
        response = client.patch('/api/v1/agent/issues/'+parent['id'],json={'parentId':child['id']})
        assert response.status_code >= 400
        response = client.post('/api/v1/agent/issues',json={'title':'Unsafe','externalRefs':[{'url':'javascript:alert(1)','title':'bad','kind':'incident'}]})
        assert response.status_code >= 400


def test_mcp_fixed_tools_use_agent_routes_only(tmp_path):
    """MCP exposes useful issue actions but no approval, generic HTTP or command execution tool."""
    calls=[]
    def handle(request):
        """Record bridge transport without launching an HTTP server or source service."""
        calls.append((request.method,request.url.path))
        if request.url.path == '/api/v1/status':
            return httpx.Response(200,json={'application':'ag-tasks','instanceId':'test'})
        return httpx.Response(200,json={'items':[], 'id':'a'*32, 'status':'backlog'})
    mcp=build_mcp(origin='http://127.0.0.1:8895', directory=tmp_path,
                  client=httpx.Client(transport=httpx.MockTransport(handle)))
    async def run():
        """Exercise tool calls via FastMCP's public tool dispatch."""
        tools=await mcp.list_tools()
        names={tool.name for tool in tools}
        expected = {'tasks_list','tasks_get','tasks_repositories','tasks_create','tasks_update','tasks_link','tasks_comment','tasks_prepare_branch','tasks_review','tasks_submit_review','tasks_spaces','tasks_graph','tasks_decompose','tasks_work_start','tasks_work_heartbeat','tasks_work_stop','tasks_submit_result','tasks_review_report','tasks_plan','tasks_document','tasks_write_document','tasks_problems','tasks_problem','tasks_resolve_problem','tasks_actual_complexity','tasks_planning_rules','tasks_status_audit','tasks_pending_reviews','tasks_pause_work','tasks_problems_digest','tasks_capabilities','tasks_work_history','tasks_service_capabilities','tasks_command','tasks_command_outcome','tasks_sessions','tasks_session','tasks_session_events','tasks_events','tasks_queues','tasks_queue','tasks_queue_items','tasks_attempt','tasks_project_tree', 'tasks_ensure_project_space','tasks_move_project_tree'}
        expected |= {'tasks_conversations', 'tasks_bind_conversation', 'tasks_reconnect_conversation',
                     'tasks_resolve_conversation', 'tasks_task_context'}
        expected |= {'tasks_process_evidence', 'tasks_process_manifest', 'tasks_process_run'}
        assert names == expected
        await mcp.call_tool('tasks_create', {'title':'Analysis result'})
        await mcp.call_tool('tasks_prepare_branch', {'issue_id':'a'*32})
        await mcp.call_tool('tasks_review', {'issue_id':'a'*32})
    asyncio.run(run())
    assert ('POST','/api/v1/agent/issues') in calls
    assert ('POST','/api/v1/agent/issues/'+'a'*32+'/prepare') in calls
    assert all('approve' not in path and 'ui-session' not in path for _,path in calls)


def test_discovery_mismatch_refuses_mutation(tmp_path):
    """A stale endpoint file cannot send issue creation to a different local instance."""
    (tmp_path/'endpoint.json').write_text(json.dumps({'origin':'http://127.0.0.1:8895','instanceId':'expected'}))
    calls=[]
    def handle(request):
        """Return the wrong identity and detect any unexpected mutation."""
        calls.append(request.method)
        return httpx.Response(200,json={'application':'ag-tasks','instanceId':'different'})
    mcp=build_mcp(directory=tmp_path,client=httpx.Client(transport=httpx.MockTransport(handle)))
    asyncio.run(mcp.call_tool('tasks_create', {'title':'Must not create'}))
    assert calls == ['GET']
